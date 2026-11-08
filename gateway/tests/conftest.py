"""测试进程的全局数据隔离：任何测试都**不允许**写进 `gateway/data/`（生产数据）。

为什么需要这个文件（2026-11-02 数据事故复盘）
--------------------------------------------------
跑一次 `python -m pytest gateway/tests` 就往生产数据里写了东西：

* `gateway/data/gateway.db` 的 `events` 表多了 `kind=care_given, channel=pet`
  的行 —— 来自 `role_speak.maybe_speak()` 成功后的 `system.record_care_given()`，
  而 `test_role_speak.py` / `test_system_decide.py` 里有两条用例只 mock 了
  `deliver_to_pet`，没把 `gateway.DB_PATH` 指走；
* `gateway/data/persona_sync.json` 每次跑都被重写 —— `gateway.handle_chat()`
  会调 `current_persona()`，后者必定走一遍 `persona_sync.sync_persona()`，
  而它的路径常量是 `ROOT` 拼死的，`monkeypatch` 不到（多数测试也没想到要管它）。
  更危险的是：如果哪天 AstrBot 库和 `persona.md` 内容不一致，`sync_persona`
  会**直接改写 `gateway/persona.md` 本体或 AstrBot 的 personas 表**。

根因不是某一条用例写错，而是「默认路径常量指向生产目录」——只要有一条用例
漏了 `monkeypatch.setattr`，写操作就落到用户真实数据上。靠人工逐条检查靠不住，
所以在 conftest 里做**兜底隔离**，分两层：

1. **导入期兜底**（本文件顶层代码）：在 import `gateway` / `system` / `persona_sync`
   之前就把 `GATEWAY_DATA_DIR` 指向进程级临时目录，并改写 `persona_sync` 的三个
   路径常量。必须在导入期做，因为 `gateway.py` 在 import 时就跑了
   `_load_or_create_token()`（可能写 `data/gateway.token`），而 `DATA_DIR` 是
   导入期常量——等到 fixture 再改就晚了。
2. **用例级兜底**（`_isolate_data_paths` autouse fixture）：把上述路径全部指到
   当前用例自己的 `tmp_path`，并在那里建好 schema（turns/events/schedules/pending）。
   任何没有显式隔离的用例，写操作都落在临时库上。

与已有用例的 `monkeypatch.setattr(...)` **不冲突**：autouse fixture 先执行，
用例体里的显式覆盖在后面，按 pytest 的 monkeypatch 语义后写的赢。
所以显式隔离照旧生效（例如 `test_care_outcomes.py` 自己挑的库），本文件只兜住
「没人管」的那部分。

本文件只改测试侧，不碰 `system.py` / `gateway.py` 的业务代码。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
#: 用户的真实数据目录。测试进程里出现的任何写入都不该落在这里。
PRODUCTION_DATA_DIR = (GATEWAY_DIR / "data").resolve()

if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))


# --------------------------------------------------------------------------- #
# 第 1 层：导入期兜底
# --------------------------------------------------------------------------- #
_BOOT_DIR = Path(tempfile.mkdtemp(prefix="gateway-tests-boot-"))
atexit.register(shutil.rmtree, str(_BOOT_DIR), True)


def _force_data_dir() -> None:
    """在 import gateway/system 之前把 DATA_DIR 指到临时目录。

    `GATEWAY_DATA_DIR` 是 `DATA_DIR` 的唯一外部开关（两个模块都用它算默认值），
    所以这里设一次，导入期常量就整体落在临时目录里：
    gateway.DB_PATH / AUDIT_PATH / ROLE_PATH / TOKEN_PATH / KEYS_PATH，
    system.SYSTEM_STATE_PATH / SYSTEM_AUDIT_PATH / INNER_OS_PATH / PEOPLE_DIR …
    """
    current = os.environ.get("GATEWAY_DATA_DIR")
    if current:
        warnings.warn(
            f"测试进程忽略外部 GATEWAY_DATA_DIR={current!r}，改用临时目录 {_BOOT_DIR}"
            "（避免误写生产数据）",
            stacklevel=2,
        )
    os.environ["GATEWAY_DATA_DIR"] = str(_BOOT_DIR)


_force_data_dir()

import gateway  # noqa: E402  —— 必须在 _force_data_dir() 之后导入
import persona_sync  # noqa: E402
import system  # noqa: E402


def _persona_sync_shadow(shadow_dir: Path) -> dict[str, str]:
    """把人格同步的三个落点全部搬到 shadow_dir，返回要写入的模块常量。

    `sync_persona()` 按内容比对决定写文件还是写库，写入目标写死在模块常量里：
      * PERSONA_PATH  → `gateway/persona.md`（git 跟踪的真人格，绝不能被测坏）
      * ASTRBOT_DB    → 仓里 AstrBot 的 data_v4.db（真实人格表）
      * STATE_PATH    → `gateway/data/persona_sync.json`
    这里把文件换成副本、库指到一个不存在的临时路径（`read_db_persona()` 返回 None，
    只会走「只读文件」分支，不会写库），状态文件放进临时目录。
    """
    shadow_dir.mkdir(parents=True, exist_ok=True)
    shadow_persona = shadow_dir / "persona.md"
    try:
        shutil.copy2(str(GATEWAY_DIR / "persona.md"), str(shadow_persona))
    except OSError:
        shadow_persona.write_text("", encoding="utf-8")

    # 影子 AstrBot 库：内容与影子人格**一致**，这样 sync_persona 走「已对齐」分支，
    # 既不会去写库，也不会因为写失败而打日志。库本身是临时文件，跟仓里的
    # qq/astrbot/data/data_v4.db 没有关系。
    shadow_db = shadow_dir / "astrbot-shadow" / "shadow.db"
    os.makedirs(str(shadow_db.parent), exist_ok=True)
    persona_text = shadow_persona.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(shadow_db))
    try:
        conn.execute(
            """create table if not exists personas(
                   created_at text, updated_at text, id integer primary key,
                   persona_id text, system_prompt text)"""
        )
        row = conn.execute(
            "select system_prompt from personas where persona_id = 'default'"
        ).fetchone()
        if row is None:
            conn.execute(
                "insert into personas(created_at, updated_at, id, persona_id, system_prompt)"
                " values ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 1, 'default', ?)",
                (persona_text,),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "PERSONA_PATH": str(shadow_persona),
        "ASTRBOT_DB": str(shadow_db),
        "STATE_PATH": str(shadow_dir / "persona_sync.json"),
    }


# 导入期兜底：直接改模块常量（此时还没有 monkeypatch 可用）
for _name, _value in _persona_sync_shadow(_BOOT_DIR).items():
    setattr(persona_sync, _name, _value)


# --------------------------------------------------------------------------- #
# 第 2 层：用例级兜底
# --------------------------------------------------------------------------- #
#: 和 `gateway.init_db()` 建出来的表结构一致（少了任何一个，兜底库就跑不通写入）。
_SCHEMA = """
create table if not exists turns(
    id integer primary key autoincrement,
    session text not null,
    role text not null,
    content text not null,
    path text,
    ts real not null,
    channel text);
create table if not exists pending(
    session text primary key,
    action text not null,
    args text not null,
    ts real not null,
    origin text);
create table if not exists events(
    id integer primary key autoincrement,
    ts real not null,
    kind text not null,
    subject text,
    payload text,
    status text,
    due_ts real,
    attempts integer default 0,
    channel text);
create table if not exists schedules(
    id integer primary key autoincrement,
    due_ts real not null,
    kind text,
    event_id integer,
    note text,
    fired integer default 0);
"""


def _create_schema(db_path: Path) -> None:
    os.makedirs(str(db_path.parent), exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _point_to(data_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把三个模块里所有会落盘的路径常量指到 data_dir。"""
    for name, value in _persona_sync_shadow(data_dir / "persona").items():
        monkeypatch.setattr(persona_sync, name, value)

    monkeypatch.setattr(gateway, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(gateway, "DB_PATH", str(db_path))
    monkeypatch.setattr(gateway, "AUDIT_PATH", str(data_dir / "audit.jsonl"))
    monkeypatch.setattr(gateway, "ROLE_PATH", str(data_dir / "role_model.json"))
    monkeypatch.setattr(gateway, "TOKEN_PATH", str(data_dir / "gateway.token"))
    monkeypatch.setattr(gateway, "KEYS_PATH", str(data_dir / "keys.json"))
    # 身份卡（2026-11-08）：不隔离的话 `apply_identity()` 会写
    # `gateway/data/identity.json`，而且还会回写 **git 里的 config.json** ——
    # 那会把生产的主人号/名字改掉，是比丢数据更难发现的事故。
    #
    # 注意：config.json 的路径不在这里改。`gateway.config_path()` 是**函数**，
    # 从模块级的 `ROOT` 现算，所以只要把 ROOT 指到 tmp 就够了 ——
    # 也正是 test_key_store / test_plugin_context 一直在用的那套约定。
    monkeypatch.setattr(gateway, "IDENTITY_PATH", str(data_dir / "identity.json"))

    monkeypatch.setattr(system, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(system, "SYSTEM_STATE_PATH", str(data_dir / "system_state.json"))
    monkeypatch.setattr(system, "SYSTEM_MODEL_PATH", str(data_dir / "system_model.json"))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(data_dir / "system_audit.jsonl"))
    monkeypatch.setattr(system, "INNER_OS_PATH", str(data_dir / "inner_os.json"))
    # 新颖度基线（T-12）。**必须隔离**：不隔离的话它会读写
    # `gateway/data/novelty_grams.json` —— 既污染生产基线，
    # 也让用例之间互相影响（前一条把内容并进 seen，后一条就算成"不新颖"，
    # 表现为"单跑通过、全量失败"这种最难查的抖）。
    monkeypatch.setattr(system, "NOVELTY_PATH", str(data_dir / "novelty_grams.json"))
    monkeypatch.setattr(system, "PEOPLE_DIR", str(data_dir / "people"))
    monkeypatch.setattr(system, "HEALTH_REPORT_PATH", str(data_dir / "health_report.json"))

    # 长期记忆库（阶段 2 新增）：默认路径是 `gateway/data/livingmemory.db`，
    # 不隔离的话跑一次测试就往真实记忆库里灌几十条「测试事件」「路过」「摘录」
    # —— 2026-11-03 实际发生过：生产库被灌进 49 条，其中 30 多条是测试 stub。
    # 这里把配置里的库路径指到临时目录，并清掉进程级单例（否则沿用上一个用例的库）。
    monkeypatch.setitem(system.SYSTEM, "long_memory_db_path", str(data_dir / "livingmemory.db"))
    monkeypatch.setattr(system, "_MEMORY_BRIDGE", None, raising=False)

    # 身份（2026-11-08）：只隔离**会被写的那两个东西**。
    #   * IDENTITY_PATH  —— apply_identity 写它，指到 tmp 就不会改到生产；
    #   * WRITE_CONFIG_MIRROR —— 关掉 config.json 回写（那文件在 git 里）。
    #
    # ⚠ 不要 `monkeypatch.setattr(gateway, "ROOT", tmp)`：试过，78 条连环失败，
    # 因为 persona、模型目录等很多路径都从 ROOT 派生，全搬走等于换了个项目。
    # 也不要在这里 `reload_identity()`：隔离目录里没有身份配置，读出来是空的，
    # 会让所有「主人」相关用例失去前提。
    monkeypatch.setattr(gateway, "IDENTITY_PATH", str(data_dir / "identity.json"))
    monkeypatch.setattr(gateway, "WRITE_CONFIG_MIRROR", False)


@pytest.fixture(autouse=True)
def _isolate_data_paths(tmp_path, monkeypatch):
    """每条用例一个干净的 data 目录 + 建好 schema 的空库，谁也不碰生产数据。

    用例自己再 `monkeypatch.setattr(gateway, "DB_PATH", ...)` 会覆盖这里的值
    （同一个 monkeypatch 实例，后写覆盖先写），所以显式隔离照旧优先。
    """
    data_dir = Path(tmp_path) / "isolated_data"
    db_path = data_dir / "gateway.db"
    _create_schema(db_path)
    _point_to(data_dir, db_path, monkeypatch)
    yield


# --------------------------------------------------------------------------- #
# 出网点守门（2026-11-04 加）
# --------------------------------------------------------------------------- #
# 为什么需要这一层
# ----------------
# 上面 `_isolate_data_paths` 管的是**文件路径**：库、状态、审计都指到 tmp。
# 但它管不住**出网点** —— `memory/store.py:99` 在不传 `embedding` 时会默认建
# 一个真的 `DashScopeEmbedding`，而它读的是 `gateway/secrets.json`（按 `__file__`
# 算的绝对路径，跟 `GATEWAY_DATA_DIR` 无关）。于是：
#
#   * 跑一次测试 = 用主人的真 key 打一次真实阿里云 embedding（**计费**）；
#   * 测试文本离开本机发到公网；
#   * `assert n == 1` 要求请求成功 → 测试基线依赖外网可用性。
#
# 2026-11-04 实测就抓到过这样的用例（`test_daily_review_sinks.py` 的
# `test_分寸念头写进长期记忆`，现已改成注入 FakeEmbedding）。
#
# 这道守门的思路和 `test_data_isolation.py` 一致：**不靠自觉，靠红**。
# 谁忘了 mock 就立刻收到一个明确的报错，而不是偷偷联网。

_DASHSCOPE_PATCHED = False


@pytest.fixture(autouse=True)
def _block_dashscope_network(monkeypatch):
    """默认禁止真实 DashScope 调用；需要的用例显式传 `embedding=FakeEmbedding()`。

    实现方式：把 `DashScopeEmbedding` 的**网络方法**换成抛错。
    这样 `open_store()` 仍然能正常建对象（构造不发请求），只是真去算向量时才炸。
    """
    try:
        from dashscope_embedding import DashScopeEmbedding  # noqa: PLC0415
    except Exception:  # noqa: BLE001 —— 没装/没这个模块时不用守
        yield
        return

    def _boom(self, *args, **kwargs):
        raise RuntimeError(
            "测试里禁止调用真实 DashScope embedding（会计费且依赖外网）。"
            "请给 open_store / MemoryBridge 传 embedding=FakeEmbedding()，"
            "参见 gateway/tests/test_memory_bridge.py 与 test_daily_review_sinks.py。"
        )

    for name in ("get_embedding", "get_embeddings", "get_embeddings_batch"):
        if hasattr(DashScopeEmbedding, name):
            monkeypatch.setattr(DashScopeEmbedding, name, _boom, raising=False)
    yield
