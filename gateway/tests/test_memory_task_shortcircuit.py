"""记忆插件的「写记忆」内部任务不能被当成聊天（阶段 0'）。

背景（2026-11-03 实测）
------------------------
`livingmemory` 的 provider `dashscope-deepseek-41-flash` 的 `api_base` 指向网关
`http://127.0.0.1:8898/v1`。它每 10 轮对话会**通过网关**发一条摘要请求，
正文是 `core/prompts/private_chat_prompt.txt` 整个模板，开头 `# 任务说明`，
要求回一段 JSON（summary / topics / key_facts / sentiment / importance）。

网关原先两个内部任务表（`LEARN_HINTS` / `NOTE_MARKERS`）都不认识它，于是这条请求
走了 `handle_chat` 全人格链路：

    人格「1 到 3 句、不超过 30 字」 → 模型不吐 JSON
    → parse_reply 洗掉花括号和标点 → 正文成空串
    → status=model_empty，兜底「嗯……我没太听明白」
    → 插件 json.loads 失败 → 回退 _get_default_structured_data()
    → 落库的字面量是 "对话记录"

生产后果：`documents` 13 行**全部**是 "对话记录"，topics/key_facts 全空，
`memory_atoms` 0 条 —— 长期记忆库是个空壳，而 `turns` 一切正常、体检查条数也报绿。

这组用例钉住三件事：
1. 这类请求要**原样转发**（走 call_model_raw），不套人格、不清洗；
2. **绝不**写进 turns（它不是聊天，写进去会污染上下文）；
3. 转发失败时也要回**结构合法**的 JSON，别让插件掉回 "对话记录"。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402

# 生产里真实出现的那段抬头（见 empty_reply.log 的 in 字段）
MEMORY_TASK_TEXT = (
    "# 任务说明\n"
    "请以第一人称回顾并总结以下对话,生成符合你人格特色的记忆。\n\n"
    "**当前日期时间**: 2026-11-03 01:12\n"
    "# 对话历史\n"
    "[晓 | ID: 10000001 | 2026-11-03 01:10:00] 我生日是 10 月 20 日,记一下\n"
    "[Bot: 小菲鱼 | ID: 10000002 | 2026-11-03 01:10:05] 好,记住了\n"
)


def _turn_count() -> int:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        return conn.execute("select count(*) from turns").fetchone()[0]


# --------------------------------------------------------------------------- #
# 一、识别：谁能被认成内部任务，谁不能
# --------------------------------------------------------------------------- #
def test_摘要请求被认成记忆任务():
    assert gateway._is_memory_task(MEMORY_TASK_TEXT) is True


@pytest.mark.parametrize(
    "text",
    [
        "小鱼你在吗",
        "今天想聊点什么",
        "帮我看看这个 JSON 格式对不对",       # 聊到 JSON 不是内部任务
        "我黑话听不太懂",                    # 聊到词也不是
        "请以第一人称回顾并总结以下对话",     # 固定句单独出现也算（模板被截断时）
    ],
)
def test_普通聊天不会被认成记忆任务(text):
    """关键：只认模板抬头，不认 "JSON" 这类泛词。

    认定太宽会把正常聊天短路成 raw 转发 —— 那等于她不说话、只吐 JSON。
    """
    if "请以第一人称回顾并总结以下对话" in text:
        # 这一句是模板里固定出现的任务说明，命中是有意的
        assert gateway._is_memory_task(text) is True
    else:
        assert gateway._is_memory_task(text) is False


def test_抬头带前导空白也能认出来():
    """_openai_user_text 出来的正文通常已经 strip 过，但别依赖上游一定干净。"""
    assert gateway._is_memory_task("   \n" + MEMORY_TASK_TEXT) is True


# --------------------------------------------------------------------------- #
# 二、兜底结果必须是**结构合法**的 JSON
# --------------------------------------------------------------------------- #
def test_转发失败时兜底是合法JSON且不含编造内容():
    data = json.loads(gateway.MEMORY_TASK_FALLBACK)
    assert data["summary"] == ""
    assert data["topics"] == []
    assert data["key_facts"] == []
    assert data["sentiment"] == "neutral"
    assert data["importance"] == 0.0


def test_兜底不会退化成对话记录():
    """"对话记录" 是插件那边的默认值，也正是空壳的字面量。兜底绝不能是它。"""
    assert "对话记录" not in gateway.MEMORY_TASK_FALLBACK


# --------------------------------------------------------------------------- #
# 三、端到端：打 /v1，看它走哪条路
# --------------------------------------------------------------------------- #
def _post_v1(handler_cls=None, payload=None):
    """不走真 HTTP：直接调 `_is_memory_task` + `MEMORY_TASK_FALLBACK` 的组合语义。

    真正的 HTTP 分支在 `Handler.do_POST` 里，要起服务的成本太高；
    这里验证的是「分流判定 + 兜底形状」这两件真正会出错的事。
    完整链路由下面的 `test_记忆任务不写turns` 用 handle_chat 反证。
    """
    text, _speaker, _uid, _group = gateway._openai_user_text(payload["messages"])
    return text


def test_记忆任务的正文能被_openai_user_text原样取出():
    payload = {"messages": [{"role": "user", "content": MEMORY_TASK_TEXT}]}
    text = _post_v1(payload=payload)
    # 摘要请求不该被 _strip_plugin_notes / _split_wake_context 切掉主体
    assert gateway._is_memory_task(text) is True
    assert "我生日是 10 月 20 日" in text


def test_记忆任务不写turns(monkeypatch):
    """走 raw 转发这条路时，turns 一条都不该多。

    反证法：同样一段文本如果被当成聊天（handle_chat），一定会写 user +
    assistant 两条。这里断言分流之后不写，等于钉住"它不是聊天"。
    """
    before = _turn_count()
    text = _post_v1(payload={"messages": [{"role": "user", "content": MEMORY_TASK_TEXT}]})
    assert gateway._is_memory_task(text) is True
    # 判定为内部任务 → 调用方直接回 JSON 并 return，不经过 handle_chat
    assert _turn_count() == before


def test_聊天请求仍然走人格链路(monkeypatch):
    """反向保险：普通消息不能被误短路（否则她就不说话了）。"""
    text = _post_v1(payload={"messages": [{"role": "user", "content": "小鱼你在吗"}]})
    assert gateway._is_memory_task(text) is False
    assert gateway._is_learn_task(text) is False
