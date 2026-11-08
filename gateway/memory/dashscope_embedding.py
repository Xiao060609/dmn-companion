"""DashScope 文本向量化 —— 替代插件里 AstrBot 的 embedding provider。

接口对齐 `provider.EmbeddingProvider`，方法名与 AstrBot 一致
（`get_dim` / `get_embedding` / `get_embeddings` / `get_embeddings_batch`），
这样从插件搬过来的 `FaissVecDB` 不用改调用点。

Key 从哪来
----------
按网关现有的优先级取（**顺序不能反**，见 `gateway._role_key`）：
    ① 显式传入的 key
    ② 环境变量 DASHSCOPE_API_KEY
    ③ gateway/secrets.json 的 dashscope_api_key
    ④ gateway/data/keys.json 里选中的那把

失败要说话
----------
401/403 是「密钥不认」，重试没用；429/5xx 是「暂时性」，可以退避重试。
两者必须区分——网关以前把它们一起吞成「我这会儿有点卡」，
用户根本想不到是 key 过期（`项目地图` 第 51 条）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "text-embedding-v4"
DEFAULT_DIM = 1024

#: DashScope 单次 embeddings 请求最多接受的文本条数。
#: 实测 25 条一批很稳；再大容易触发限流，且失败重试的代价更高。
MAX_BATCH = 25


class EmbeddingAuthError(RuntimeError):
    """密钥不认（401/403）。重试没用，必须换 key。"""


class EmbeddingTransientError(RuntimeError):
    """暂时性失败（429/5xx/网络）。可以退避重试。"""


class DashScopeEmbedding:
    """调 DashScope 的 `/embeddings` 拿向量。

    Args:
        api_key: 显式 key。空则按上面说的优先级回落。
        base: 兼容 OpenAI 的服务地址。
        model: 模型名。
        dim: 向量维度。**必须与建索引时一致**，否则 faiss 会报维度不匹配。
        timeout: 单次请求超时（秒）。
        max_retries: 暂时性失败的重试次数。
    """

    def __init__(
        self,
        api_key: str = "",
        base: str = DEFAULT_BASE,
        model: str = DEFAULT_MODEL,
        dim: int = DEFAULT_DIM,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key or ""
        self.base = base.rstrip("/")
        self.model = model
        self._dim = int(dim)
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self._resolved_key: str | None = None

    # ------------------------------------------------------------------ key
    @property
    def api_key(self) -> str:
        """惰性解析：构造时不读盘，避免 import 期副作用。"""
        if self._api_key:
            return self._api_key
        if self._resolved_key is None:
            self._resolved_key = _resolve_key()
        return self._resolved_key

    # ------------------------------------------------------------- 接口实现
    def get_dim(self) -> int:
        return self._dim

    async def get_embedding(self, text: str) -> list[float]:
        vecs = await self.get_embeddings([text])
        return vecs[0]

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。空输入返回空列表（不浪费一次网络请求）。"""
        if not texts:
            return []
        return await self.get_embeddings_batch(texts)

    async def get_embeddings_batch(
        self,
        texts: list[str],
        batch_size: int = MAX_BATCH,
    ) -> list[list[float]]:
        """按批切分后逐批请求，结果顺序与入参严格一致。"""
        if not texts:
            return []
        size = max(1, min(int(batch_size or MAX_BATCH), MAX_BATCH))
        out: list[list[float]] = []
        for start in range(0, len(texts), size):
            chunk = texts[start : start + size]
            out.extend(self._request(chunk))
        return out

    # ------------------------------------------------------------- 底层请求
    def _request(self, texts: list[str]) -> list[list[float]]:
        """一次 HTTP 请求。同步 urllib —— 由 `loop_bridge` 放到线程里跑。

        为什么用同步 urllib 而不是 aiohttp：
        网关现有代码全是同步 urllib（`call_model` / `tools._web_search`），
        保持一致，少一个依赖。异步外壳由 loop_bridge 提供。
        """
        key = self.api_key
        if not key:
            raise EmbeddingAuthError(
                "没有可用的 DashScope key（检查 DASHSCOPE_API_KEY / secrets.json / keys.json）"
            )

        body = json.dumps(
            {"model": self.model, "input": texts, "dimensions": self._dim},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/embeddings",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return _parse_embeddings(payload, len(texts))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except OSError:
                    pass
                if exc.code in (401, 403):
                    # 密钥问题：重试多少次都一样，立刻抛
                    raise EmbeddingAuthError(
                        f"HTTP {exc.code} 密钥不认：{detail}"
                    ) from exc
                if exc.code == 429 or exc.code >= 500:
                    last_err = EmbeddingTransientError(f"HTTP {exc.code} {detail}")
                else:
                    # 400 之类是请求本身有问题，重试无用
                    raise RuntimeError(f"HTTP {exc.code} {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = EmbeddingTransientError(f"{type(exc).__name__}: {exc}")
            except json.JSONDecodeError as exc:
                last_err = EmbeddingTransientError(f"返回的不是 JSON: {exc}")

            if attempt < self.max_retries - 1:
                time.sleep(min(2 ** attempt, 8))

        raise last_err or EmbeddingTransientError("未知失败")


def _parse_embeddings(payload: dict, expected: int) -> list[list[float]]:
    """从 OpenAI 兼容响应里取向量，并按 `index` 排序。

    服务端不保证 `data` 顺序与入参一致，所以**必须按 index 排**，
    否则向量和文本会错位——那是最难查的一类 bug。
    """
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingTransientError(
            f"返回条数不符：期望 {expected}，实际 "
            f"{len(data) if isinstance(data, list) else type(data).__name__}"
        )
    ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
    out: list[list[float]] = []
    for item in ordered:
        emb = item.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise EmbeddingTransientError("返回里有空向量")
        out.append([float(x) for x in emb])
    return out


def _resolve_key() -> str:
    """按网关既有优先级找 key。找不到返回空串（由调用方报错）。"""
    env = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if env:
        return env

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "secrets.json"), encoding="utf-8") as fh:
            key = str(json.load(fh).get("dashscope_api_key", "")).strip()
        if key:
            return key
    except (OSError, json.JSONDecodeError):
        pass

    data_dir = os.environ.get("GATEWAY_DATA_DIR") or os.path.join(root, "data")
    try:
        with open(os.path.join(data_dir, "keys.json"), encoding="utf-8") as fh:
            store = json.load(fh)
        active = str(store.get("active") or "")
        for item in store.get("keys") or []:
            if item.get("id") == active and item.get("key"):
                return str(item["key"]).strip()
        # 没选中任何一把时，退回第一把（总比没有强）
        for item in store.get("keys") or []:
            if item.get("key"):
                return str(item["key"]).strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return ""
