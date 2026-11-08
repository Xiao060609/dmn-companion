"""角色层主动说话模块（role_speak）的测试。

覆盖验收标准：
1. [不说] → 返回 None，不投递
2. 说了 → 投递一次，返回 text
3. 投递失败 → 返回 None（不推进计数，下一轮还能再试）
4. 边界守卫：system.py 里没有投递动作（_deliver_pet / 8848 / api/say）
"""
from __future__ import annotations

import sys
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402
import role_speak  # noqa: E402


def _state(**kw):
    st = system._default_state()
    st.update(kw)
    return st


def test_maybe_speak_不说_不投递(monkeypatch):
    import gateway
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "call_model", lambda msgs, use_tools=False: ("[不说]", {}, None))
    delivered = []
    monkeypatch.setattr(role_speak, "deliver_to_pet", lambda r: delivered.append(r) or True)
    assert role_speak.maybe_speak({"now": "x"}, _state()) == (None, "declined")
    assert delivered == []


def test_maybe_speak_说了_投递一次(monkeypatch):
    import gateway
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "call_model", lambda msgs, use_tools=False: ("{happy}我在呢。", {}, None))
    monkeypatch.setattr(gateway, "parse_reply", lambda raw, ms: {"text": "我在呢。", "emotion": "happy", "tone": ""})
    monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})
    delivered = []
    monkeypatch.setattr(role_speak, "deliver_to_pet", lambda r: delivered.append(r) or True)
    r, status = role_speak.maybe_speak({"now": "x"}, _state())
    assert r is not None and r["text"] == "我在呢。"
    assert status == "spoken"
    assert len(delivered) == 1


def test_maybe_speak_投递失败_状态可区分(monkeypatch):
    """投递失败必须和「她不想说」分开：前者是会反复发生的故障，后者是正常状态。"""
    import gateway
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "call_model", lambda msgs, use_tools=False: ("{happy}我在呢。", {}, None))
    monkeypatch.setattr(gateway, "parse_reply", lambda raw, ms: {"text": "我在呢。", "emotion": "happy", "tone": ""})
    monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})
    monkeypatch.setattr(role_speak, "deliver_to_pet", lambda r: False)  # 投递失败（无订阅者）
    assert role_speak.maybe_speak({"now": "x"}, _state()) == (None, "delivery_failed")


def test_maybe_speak_解析失败_状态可区分(monkeypatch):
    import gateway
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "call_model", lambda msgs, use_tools=False: ("{happy}", {}, None))
    monkeypatch.setattr(gateway, "parse_reply", lambda raw, ms: {"text": "", "emotion": "happy", "tone": ""})
    monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})
    monkeypatch.setattr(role_speak, "deliver_to_pet", lambda r: True)
    assert role_speak.maybe_speak({"now": "x"}, _state()) == (None, "parse_failed")


def test_系统层无投递动作_边界守卫():
    """投递动作必须已完全移出 system.py（触发是机制，说话是人格）。

    ⚠ 2026-11-08 改判据：从「源码里有没有某个字符串」改成「AST 里有没有
    真正的调用/属性访问」。

    原判据（`assert "8848" not in src`）太脆 —— 我第一次踩是给后台加了
    `proactive_speak_payload()`（探活桌宠，注释提到 8848），第二次踩是
    注释里写了 `deliver_to_pet()` 这个函数名。**注释不是代码，但字符串检查
    分不出来。** 这种测试久了会变成"改注释就红"，最后没人敢写注释。

    现在的判据只认真实节点：
      * 有没有 `deliver_to_pet` 的 **Name/Attribute 引用**（不含注释）
      * 有没有字符串字面量 `/api/say`
    """
    import ast

    src = (GATEWAY_DIR / "system.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ① 有没有真正引用 deliver_to_pet（注释和 docstring 不算）
    referenced: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "deliver_to_pet":
            referenced.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr == "deliver_to_pet":
            referenced.append(node.attr)

    # ② 有没有字符串字面量里写着 /api/say
    say_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "api/say" in node.value
    ]

    assert not referenced, (
        f"system.py 里不该真正引用投递函数，找到：{referenced}")
    assert not say_literals, (
        f"system.py 里不该有推 /api/say 的字面量，找到：{say_literals}")
    assert "_deliver_pet" not in src
