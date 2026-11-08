"""人格两头同步的单测。

项目里有两份人格：`gateway/persona.md`（网关真正用的）和 AstrBot `personas` 表里那份
（网关会丢弃传入的 system，所以改它没效果，但它摆在 WebUI 里，很容易有人去那儿改）。
`persona_sync.sync_persona()` 负责对齐。

**这里最要紧的一条是「不能靠时间戳判断谁改了」**：真实环境里库那行的 updated_at
是 11-01 09:13，persona.md 是 10-31 14:25 —— 库里「更新」，但内容只有 296 字，
persona.md 有 1633 字。按时间戳判断会把真正的人格覆盖掉。
所以改成比内容快照，本文件里专门有一条用例钉这个。
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

import persona_sync  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    persona = tmp_path / "persona.md"
    persona.write_text("旧人格\n", encoding="utf-8")
    db = tmp_path / "data_v4.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """create table personas(
               created_at text, updated_at text, id integer primary key,
               persona_id text, system_prompt text)"""
    )
    conn.execute(
        "insert into personas(created_at, updated_at, id, persona_id, system_prompt) "
        "values ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 1, 'default', '旧人格')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(persona_sync, "PERSONA_PATH", str(persona))
    monkeypatch.setattr(persona_sync, "ASTRBOT_DB", str(db))
    monkeypatch.setattr(persona_sync, "STATE_PATH", str(tmp_path / "state.json"))
    return persona, db, tmp_path / "state.json"


def _db_text(db) -> str:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "select system_prompt from personas where persona_id='default'"
        ).fetchone()[0]
    finally:
        conn.close()


def _set_db(db, text: str, ts: str = "2099-01-01 00:00:00") -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "update personas set system_prompt=?, updated_at=? where persona_id='default'",
            (text, ts),
        )
        conn.commit()
    finally:
        conn.close()


class TestEqual:
    def test_内容一致就都不动(self, env):
        persona, db, state = env
        out = persona_sync.sync_persona("内存里的")
        assert out.strip() == "旧人格"
        assert _db_text(db) == "旧人格"
        assert state.is_file(), "同步过一次应该记下快照"


class TestDbWins:
    def test_只有库变了才写回文件(self, env):
        persona, db, _ = env
        persona_sync.sync_persona("x")        # 先建立快照
        _set_db(db, "WebUI 里改的")
        out = persona_sync.sync_persona("x")
        assert out.strip() == "WebUI 里改的"
        assert persona.read_text(encoding="utf-8").strip() == "WebUI 里改的"
        bak = Path(str(persona) + ".bak")
        assert bak.is_file() and bak.read_text(encoding="utf-8").strip() == "旧人格"


class TestFileWins:
    def test_只有文件变了才写回库(self, env):
        persona, db, _ = env
        persona_sync.sync_persona("x")        # 先建立快照
        persona.write_text("文件里改的\n", encoding="utf-8")
        out = persona_sync.sync_persona("x")
        assert out.strip() == "文件里改的"
        assert _db_text(db).strip() == "文件里改的"


class TestTimestampsAreNotTrusted:
    def test_库时间戳更新但内容没变时不覆盖文件(self, env):
        """真实翻车场景：库里 updated_at 比文件新，但文件才是真正的人格。"""
        persona, db, _ = env
        persona.write_text("很长很完整的真人格\n", encoding="utf-8")
        _set_db(db, "旧人格", ts="2099-01-01 00:00:00")   # 库时间戳更"新"
        out = persona_sync.sync_persona("x")
        assert out.strip() == "很长很完整的真人格", "不能被时间戳骗着覆盖文件"
        assert _db_text(db).strip() == "很长很完整的真人格", "应该反过来把库修好"

    def test_两边都改了以文件为准(self, env):
        persona, db, _ = env
        persona_sync.sync_persona("x")
        persona.write_text("文件改的\n", encoding="utf-8")
        _set_db(db, "库里改的")
        out = persona_sync.sync_persona("x")
        assert out.strip() == "文件改的"
        assert _db_text(db).strip() == "文件改的"


class TestNoPingPong:
    def test_写回文件之后不会再反复写(self, env):
        persona, db, _ = env
        persona_sync.sync_persona("x")
        _set_db(db, "库里的新内容")
        persona_sync.sync_persona("x")
        first = persona.read_text(encoding="utf-8")
        state1 = json.loads(Path(persona_sync.STATE_PATH).read_text(encoding="utf-8"))
        persona_sync.sync_persona("x")
        persona_sync.sync_persona("x")
        assert persona.read_text(encoding="utf-8") == first
        state2 = json.loads(Path(persona_sync.STATE_PATH).read_text(encoding="utf-8"))
        assert state1 == state2, "快照不该被无意义地刷来刷去"


class TestSafety:
    def test_文件为空时什么都不做(self, env):
        persona, db, _ = env
        persona.write_text("   \n", encoding="utf-8")
        _set_db(db, "库里的人格")
        out = persona_sync.sync_persona("内存里的")
        assert out == "内存里的"
        assert _db_text(db) == "库里的人格", "库不该被清空"

    def test_库不存在也不抛(self, tmp_path, monkeypatch):
        persona = tmp_path / "persona.md"
        persona.write_text("只有文件\n", encoding="utf-8")
        monkeypatch.setattr(persona_sync, "PERSONA_PATH", str(persona))
        monkeypatch.setattr(persona_sync, "ASTRBOT_DB", str(tmp_path / "nope.db"))
        monkeypatch.setattr(persona_sync, "STATE_PATH", str(tmp_path / "s.json"))
        assert persona_sync.sync_persona("x").strip() == "只有文件"

    def test_文件不存在时返回传入值(self, tmp_path, monkeypatch):
        monkeypatch.setattr(persona_sync, "PERSONA_PATH", str(tmp_path / "nope.md"))
        monkeypatch.setattr(persona_sync, "ASTRBOT_DB", str(tmp_path / "nope.db"))
        monkeypatch.setattr(persona_sync, "STATE_PATH", str(tmp_path / "s.json"))
        assert persona_sync.sync_persona("内存里的") == "内存里的"
