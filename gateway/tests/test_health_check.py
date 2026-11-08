"""系统体检（health_check）的测试。

覆盖：
1. 报告结构正确（ts/ok/problems/stats）
2. 空记忆库报 warn
3. 人格为空报 error（ok=False）
4. 内心OS 积压报 warn
5. 心跳停了报 warn
6. 报告能落盘和读回
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(system, "SYSTEM_STATE_PATH", str(tmp_path / "system_state.json"))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))
    monkeypatch.setattr(system, "INNER_OS_PATH", str(tmp_path / "inner_os.json"))
    monkeypatch.setattr(system, "HEALTH_REPORT_PATH", str(tmp_path / "health_report.json"))


def _ok_db():
    return {"total": 10, "last_24h": 1, "null_channel": 0, "sources": {}}


def test_health_check_结构正确(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(system, "_db_stats", _ok_db)
    r = system.health_check()
    assert {"ts", "ok", "problems", "stats"} <= set(r)
    assert isinstance(r["problems"], list)
    assert "turns" in r["stats"]


def test_空记忆库报warn(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(
        system, "_db_stats",
        lambda: {"total": 0, "last_24h": 0, "null_channel": 0, "sources": {}},
    )
    r = system.health_check()
    assert any(p["area"] == "turns" for p in r["problems"])


def test_人格为空报error(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(system, "_db_stats", _ok_db)
    monkeypatch.setattr(system, "_read_persona", lambda: "")
    r = system.health_check()
    assert any(p["level"] == "error" and p["area"] == "persona" for p in r["problems"])
    assert r["ok"] is False


def test_内心OS积压报warn(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(system, "_db_stats", _ok_db)
    monkeypatch.setattr(system, "_read_inner_os", lambda: [{"text": "x"}] * system.INNER_OS_MAX)
    r = system.health_check()
    assert any(p["area"] == "inner_os" for p in r["problems"])


def test_心跳停了报warn(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(system, "_db_stats", _ok_db)
    st = system._default_state()
    st["last_tick_ts"] = time.time() - 3600 * 5  # 5 小时前，远超 300s*3
    monkeypatch.setattr(system, "load_state", lambda: st)
    r = system.health_check()
    assert any(p["area"] == "heartbeat" for p in r["problems"])


def test_报告能落盘和读回(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(system, "_db_stats", _ok_db)
    r = system.health_check()
    back = system._read_health_report()
    assert back.get("ts") == r["ts"]
    assert back.get("stats", {}).get("turns") == 10


# ---------------------------------------------------------------- 体检由模型按需调用（不启动定时）
def _turns10():
    return [{"id": i, "role": "user", "content": "x", "ts": 0} for i in range(10)]


def test_consolidate_工具清单含体检_且tool_choice为auto(tmp_path, monkeypatch):
    """巩固链路的工具清单里要有体检能力，且由模型自己选（auto），不再强制。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(system, "_read_turns_since", lambda sid, limit: _turns10())
    captured = {}

    def fake_call_system(msgs, *, tool_choice="auto", tools=None, max_tokens=None):
        captured["tool_choice"] = tool_choice
        captured["tools"] = [t["function"]["name"] for t in (tools or [])]
        return None, {}, None  # 模型什么都没选

    monkeypatch.setattr(system, "call_system", fake_call_system)
    system._consolidate_body()
    assert captured["tool_choice"] == "auto"
    assert "system_health_check" in captured["tools"]
    assert "system_consolidate" in captured["tools"]


def test_consolidate_模型选体检时执行体检_不巩固(tmp_path, monkeypatch):
    """模型自主选了体检 -> 执行体检，这一轮不再 push 观点。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(system, "_read_turns_since", lambda sid, limit: _turns10())
    monkeypatch.setattr(
        system, "call_system",
        lambda msgs, **kw: (None, {}, [{"id": "1", "function": {"name": "system_health_check", "arguments": "{}"}}]),
    )
    events = []
    monkeypatch.setattr(system, "health_check", lambda: events.append("health") or {"ok": True})
    monkeypatch.setattr(system, "push_inner_os", lambda items: events.append("push"))
    system._consolidate_body()
    assert events == ["health"]  # 只体检，没巩固


def test_start_不再挂体检线程(monkeypatch):
    """体检不再定时：start() 挂的线程里没有 system-health。"""
    # mock 线程体，避免真的起后台线程
    monkeypatch.setattr(system, "_heartbeat_loop", lambda: None)
    monkeypatch.setattr(system, "_persona_apply_worker", lambda: None)
    monkeypatch.setattr(system, "_consolidate_loop", lambda: None)
    names = [t.name for t in system.start()]
    assert "system-health" not in names
    assert "system-heartbeat" in names
