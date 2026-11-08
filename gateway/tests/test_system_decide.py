"""主动说话职责边界的回归测试（决策权下移 + 投递移出系统层）。

覆盖：
1. _allow_speak 刹车（额度/冷却）纯代码，不经过模型
2. role_speak.maybe_speak 的角色层「[不说]」出口 → 不投递、不计数
3. memory 去重（历史复读墙清理）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


# ---------------------------------------------------------------- _allow_speak 刹车
def _state(**kw):
    st = system._default_state()
    st.update(kw)
    return st


def test_allow_speak_额度用满拦截():
    assert system._allow_speak(_state(speak_count_today=6)) is False


def test_allow_speak_冷却拦截():
    assert system._allow_speak(_state(last_speak_ts=time.time())) is False


def test_allow_speak_通过(monkeypatch):
    """⚠ 2026-11-08：必须先打开主动说话总开关。

    主人要求这个能力默认关（它会弹气泡+出声，不该装上就开着）。
    所以"没额度、没冷却"还不够 —— 总开关开着才谈得上放行。
    """
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": True})
    assert system._allow_speak(_state(speak_count_today=0, last_speak_ts=0)) is True


def test_allow_speak_总开关关着就一律拒绝(monkeypatch):
    """回归：默认关的语义 —— 额度和冷却都没问题也不行。"""
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": False})
    assert system._allow_speak(_state(speak_count_today=0, last_speak_ts=0)) is False


# ---------------------------------------------------------------- role_speak.maybe_speak
def test_maybe_speak_不说(monkeypatch):
    import gateway
    import role_speak
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "call_model", lambda msgs, use_tools=False: ("[不说]", {}, None))
    assert role_speak.maybe_speak({"now": "2026-11-02 18:00"}, system._default_state()) == (None, "declined")


def test_maybe_speak_说话(monkeypatch):
    import gateway
    import role_speak
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "call_model", lambda msgs, use_tools=False: ("{happy}我在呢。", {}, None))
    monkeypatch.setattr(gateway, "parse_reply", lambda raw, ms: {"text": "我在呢。", "emotion": "happy", "tone": ""})
    monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})
    monkeypatch.setattr(role_speak, "deliver_to_pet", lambda rendered: True)
    r, status = role_speak.maybe_speak({"now": "2026-11-02 18:00"}, system._default_state())
    assert r is not None
    assert status == "spoken"
    assert r["text"] == "我在呢。"
    assert r["emotion"] == "happy"


# ---------------------------------------------------------------- _tick_body 决策闭环
def _patch_tick(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "SYSTEM_STATE_PATH", str(tmp_path / "system_state.json"))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))
    monkeypatch.setattr(system, "_detect_change", lambda st: {"new_turns": 1, "last_turn_id": 1})
    monkeypatch.setattr(system, "_read_recent_chat", lambda n=5: [])
    monkeypatch.setattr(system, "_seconds_since_last_turn", lambda: None)
    monkeypatch.setattr(system, "_allow_speak", lambda st: True)


def test_tick_角色层不说_不投递不计数(tmp_path, monkeypatch):
    _patch_tick(monkeypatch, tmp_path)
    monkeypatch.setattr(system.role_speak, "maybe_speak", lambda signals, st: (None, "declined"))
    system._tick_body()
    st = system.load_state()
    assert st["speak_count_today"] == 0
    assert st["last_speak_ts"] == 0
    assert st["recent_thoughts"][-1]["declined"] is True
    assert st["recent_thoughts"][-1]["speak_status"] == "declined"


def test_tick_角色层说话_投递计数并给心情(tmp_path, monkeypatch):
    _patch_tick(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system.role_speak, "maybe_speak",
        lambda signals, st: ({"text": "你好呀", "emotion": "happy", "tone": ""}, "spoken"),
    )
    system._tick_body()
    st = system.load_state()
    assert st["speak_count_today"] == 1
    assert st["emotion"] == "happy"
    assert st["focus"] == "你好呀"
    assert st["recent_thoughts"][-1]["speak_status"] == "spoken"


def test_tick_投递失败不算她拒绝(tmp_path, monkeypatch):
    """投递失败必须和「不想说」分开记：前者是会反复发生的故障。"""
    _patch_tick(monkeypatch, tmp_path)
    monkeypatch.setattr(system.role_speak, "maybe_speak", lambda signals, st: (None, "delivery_failed"))
    system._tick_body()
    st = system.load_state()
    assert st["speak_count_today"] == 0
    assert st["last_speak_ts"] == 0
    assert st["recent_thoughts"][-1]["declined"] is False, "投递失败不是她拒绝"
    assert st["recent_thoughts"][-1]["speak_status"] == "delivery_failed"


def test_tick_刹车拦截_不调角色层(tmp_path, monkeypatch):
    _patch_tick(monkeypatch, tmp_path)
    monkeypatch.setattr(system, "_allow_speak", lambda st: False)
    called = []
    monkeypatch.setattr(
        system.role_speak, "maybe_speak",
        lambda signals, st: called.append(1) or (None, "declined"),
    )
    system._tick_body()
    assert called == []  # 刹车拦住，根本没去问角色层
    st = system.load_state()
    assert st["recent_thoughts"][-1]["speak_status"] == "blocked"


# ---------------------------------------------------------------- memory 去重
def test_load_state_去重历史复读(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "SYSTEM_STATE_PATH", str(tmp_path / "system_state.json"))
    st = system._default_state()
    st["memory"] = [{"ts": 1000 + i, "text": "同一句"} for i in range(33)]
    st["memory"].append({"ts": 2000, "text": "另一句"})
    system.save_state(st)
    loaded = system.load_state()
    texts = [m["text"] for m in loaded["memory"]]
    assert texts == ["同一句", "另一句"]
