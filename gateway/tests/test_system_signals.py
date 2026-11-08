"""系统信号（_system_signals）与「体检是否可被模型选中」的测试。

这一组测试针对一个真问题：模型有没有资格/依据去选「体检」这张能力卡。
覆盖：
1. 系统正常时 abnormal 为空（不误导模型去体检）
2. 心跳停 → abnormal 报心跳
3. 内心OS 积压 → abnormal 报积压
4. 审计错误多 → abnormal 报错误
5. no_change 的心跳也更新 last_tick_ts（否则长时间没人说话会误报心跳停）
6. _save_events 写库失败返回 -1，不假成功
7. 两个任务提示词里真的给了 system_health_check 这条分支
"""
from __future__ import annotations

import json
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


def test_系统正常时_abnormal_为空(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    st = system._default_state()
    st["last_tick_ts"] = time.time()
    system.save_state(st)
    sig = system._system_signals()
    assert sig["abnormal"] == []
    assert sig["inner_os_pending"] == 0


def test_心跳停了_会被报出来(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    st = system._default_state()
    st["last_tick_ts"] = time.time() - 3600  # 一小时没跳，远超 3 个 300s 周期
    system.save_state(st)
    sig = system._system_signals()
    assert any("心跳" in a for a in sig["abnormal"])


def test_内心OS积压_会被报出来(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    st = system._default_state()
    st["last_tick_ts"] = time.time()
    system.save_state(st)
    # 塞满到 80% 上限
    many = [{"id": f"i{n}", "type": "insight", "text": f"观点{n}", "state": "pending"} for n in range(system.INNER_OS_MAX)]
    system._write_inner_os(many)
    sig = system._system_signals()
    assert any("积压" in a for a in sig["abnormal"])


def test_审计错误多_会被报出来(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    st = system._default_state()
    st["last_tick_ts"] = time.time()
    system.save_state(st)
    with open(system.SYSTEM_AUDIT_PATH, "w", encoding="utf-8") as fh:
        for _ in range(12):
            fh.write(json.dumps({"ts": time.time(), "event": "speak_fail", "ok": False}) + "\n")
    sig = system._system_signals()
    assert sig["audit_errors_24h"] >= 10
    assert any("错误" in a for a in sig["abnormal"])


def test_no_change_也更新心跳时间(tmp_path, monkeypatch):
    """没变化时也要记 last_tick_ts——它代表心跳活着，不是代表有事发生。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(system, "_detect_change", lambda st: {})
    monkeypatch.setattr(system, "_fire_due_schedules", lambda: 0)
    st = system._default_state()
    st["last_tick_ts"] = 0
    system.save_state(st)
    system._tick_body()
    assert system.load_state()["last_tick_ts"] > 0


def test_写事件失败不假成功(tmp_path, monkeypatch):
    """库里没有 events 表时必须返回 -1，而不是悄悄返回 0 让上层记成功。

    用 `promise` 而不是 `emotion`：2026-11-03 起 events 只当状态机用，
    `observation`/`emotion` 在 `_save_events` 里就被过滤掉、根本走不到 DB。
    那条过滤会让本用例变成「测不到 DB 错误路径」，所以必须挑一个状态机类型。
    """
    _isolate(tmp_path, monkeypatch)
    # 指向一个没建表的空库
    monkeypatch.setattr(system, "sqlite3", sys.modules["sqlite3"])
    import gateway
    monkeypatch.setattr(gateway, "DB_PATH", str(tmp_path / "empty.db"))
    saved = system._save_events(
        [{"kind": "promise", "subject": "主人", "evidence": "答应明天弄"}], "pet"
    )
    assert saved == -1


def test_两个任务提示词都给了体检分支():
    """提示词里不写这条，模型就永远不敢选体检——工具再好也是死的。"""
    assert "system_health_check" in system.CONSOLIDATE_PROMPT
    assert "system_health_check" in system.PERSONA_REVIEW_PROMPT
    assert "abnormal" in system.CONSOLIDATE_PROMPT
    assert "abnormal" in system.PERSONA_REVIEW_PROMPT
