# -*- coding: utf-8 -*-
"""主动说话总开关（2026-11-08，主人要求：默认关，开关放后台）。

## 为什么要总开关

主动说话是**会打扰人**的能力：桌面弹出气泡 + 出声。
这种能力不该装上就开着 —— 得主人明确点头。

## 三道闸

    总开关（proactive_speak.enabled）  ← 这次新加的，默认 False
        ↓
    每日额度（max_speak_per_day）
        ↓
    冷却（cooldown_s）
        ↓
    role_speak.maybe_speak —— 她自己决定说不说

关掉总开关时**整条链路不跑，连模型都不调**，不只是"沉默"。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
for _p in (str(GATEWAY_DIR), str(GATEWAY_DIR / "memory")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import system  # noqa: E402


@pytest.fixture(autouse=True)
def _default_off(monkeypatch):
    """每条用例都从"关着"开始，且不动真实 config.json。"""
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": False})
    # 把回写路径改成 tmp，别动真实配置
    yield


def test_默认是关的():
    """开箱即关 —— 这是主人明确要求的默认值。"""
    cfg = system.SYSTEM.get("proactive_speak") or {}
    assert cfg.get("enabled") is False


def test_关着时_allow_speak_直接拒绝():
    st = system.load_state()
    assert system._allow_speak(st) is False


def test_打开后_allow_speak_会看额度和冷却(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": True})
    st = dict(system.load_state())
    st["speak_count_today"] = 0
    st["last_speak_ts"] = 0
    assert system._allow_speak(st) is True, "开着且没额度/冷却限制就该放行"


def test_额度用完仍然拦(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": True})
    st = dict(system.load_state())
    st["speak_count_today"] = int(system.SYSTEM.get("max_speak_per_day", 6))
    st["last_speak_ts"] = 0
    assert system._allow_speak(st) is False


def test_冷却中仍然拦(monkeypatch):
    import time

    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": True})
    st = dict(system.load_state())
    st["speak_count_today"] = 0
    st["last_speak_ts"] = time.time()  # 刚说过
    assert system._allow_speak(st) is False


def test_payload_关着时报_disabled():
    p = system.proactive_speak_payload()
    assert p["enabled"] is False
    assert p["blocked_by"] == "disabled"
    assert p["can_speak_now"] is False


def test_payload_打开但冷却中时报_cooldown(monkeypatch):
    import time

    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": True})
    monkeypatch.setattr(system, "load_state",
                        lambda: {"speak_count_today": 0, "last_speak_ts": time.time()})
    p = system.proactive_speak_payload()
    assert p["enabled"] is True
    assert p["blocked_by"] == "cooldown"


def test_payload_打开且冷却过了报空(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": True})
    monkeypatch.setattr(system, "load_state",
                        lambda: {"speak_count_today": 0, "last_speak_ts": 0})
    p = system.proactive_speak_payload()
    assert p["blocked_by"] == ""
    assert p["can_speak_now"] is True


def test_切换会改内存里的开关(monkeypatch, tmp_path):
    """热生效：改完不用重启，下一次心跳就按新值走。"""
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": False})
    monkeypatch.setattr(system, "ROOT", str(tmp_path))
    (tmp_path / "config.json").write_text('{"system": {}}', encoding="utf-8")

    ok, msg = system.apply_proactive_speak({"enabled": True})
    assert ok is True
    assert system.SYSTEM["proactive_speak"]["enabled"] is True

    ok, msg = system.apply_proactive_speak({"enabled": False})
    assert ok is True
    assert system.SYSTEM["proactive_speak"]["enabled"] is False


def test_切换参数不对时报错(monkeypatch):
    ok, msg = system.apply_proactive_speak({"wrong": 1})
    assert ok is False
