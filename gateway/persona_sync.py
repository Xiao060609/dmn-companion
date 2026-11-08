"""人格的两头同步：`gateway/persona.md` ↔ AstrBot 数据库里的 personas 表。

为什么要这个
------------
项目里有两份人格：

1. `gateway/persona.md` —— 网关**真正在用**的那份，作为第一条 system 拼给模型；
2. AstrBot `data_v4.db` 的 `personas` 表 —— 摆在 WebUI 里，看着像能用，
   其实网关会把传入的 system 整条丢掉（`build_messages` 自己重建 system），
   所以**改它对说话没有任何影响**。

两份各改各的迟早不一致，而且人很容易去 WebUI 那边改然后纳闷为什么没反应。
这里做双向同步：**谁真的改了以谁为准，并写回另一头**。

为什么不比时间戳
----------------
一开始想用「文件 mtime vs 库 updated_at」，实测直接翻车：库里那行的
`updated_at` 是 2026-11-01 09:13，而 persona.md 是 10-31 14:25 —— 库里「更新」，
但它的内容只有 296 字，persona.md 有 1633 字。按时间戳判断会把真正的人格覆盖掉。
AstrBot 自己也会碰那一行，时间戳根本不能证明「有人改过」。

所以改成**比内容快照**：把上次同步时两边的 hash 记在
`data/persona_sync.json`，只有内容真的变了才算「有人改」。

同步规则（顺序很重要）
----------------------
1. 两边内容一致 → 收手（必须先比内容，否则写文件会更新 mtime，来回乒乓）。
2. 只有库变了 → 以库为准，写回文件（先留 `.bak`）。
3. 只有文件变了 → 以文件为准，写回库。
4. 两边都变了 / 第一次同步 → **文件赢**：它是 git 跟踪的真源，能回滚。

任何一步失败都只打日志、不抛：同步是锦上添花，绝不能因为 AstrBot 库被锁住
就让人说不出话。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
PERSONA_PATH = os.path.join(ROOT, "persona.md")
# AstrBot 的库在仓库里，网关在 gateway/ 下，所以往上一级再进 qq/
ASTRBOT_DB = os.path.abspath(os.path.join(ROOT, "..", "qq", "astrbot", "data", "data_v4.db"))
STATE_PATH = os.path.join(ROOT, "data", "persona_sync.json")
PERSONA_ID = "default"


def _log(msg: str) -> None:
    print(f"[persona] {msg}", flush=True)


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def read_db_persona() -> str | None:
    """读库里那份人格。读不到（没库 / 没这行 / 被锁）返回 None。"""
    if not os.path.isfile(ASTRBOT_DB):
        return None
    try:
        conn = sqlite3.connect(f"file:{ASTRBOT_DB}?mode=ro", uri=True, timeout=3)
        try:
            row = conn.execute(
                "select system_prompt from personas where persona_id = ?",
                (PERSONA_ID,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return str(row[0] or "") if row else None


def _write_file(text: str) -> None:
    """把库里的内容写回文件。先留一份 .bak，再原子替换。"""
    try:
        shutil.copy2(PERSONA_PATH, PERSONA_PATH + ".bak")
    except OSError:
        pass
    tmp = PERSONA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, PERSONA_PATH)


def _write_db(text: str) -> bool:
    """把文件内容写进库，让 WebUI 里看到的也是同一份。

    `updated_at` 用 strftime 而不是 datetime()，是为了和原来的
    '2026-11-01 09:13:20.410753' 格式一致（带毫秒），免得 ORM 解析出岔子。
    """
    try:
        conn = sqlite3.connect(ASTRBOT_DB, timeout=3)
        try:
            conn.execute("pragma busy_timeout=3000")
            conn.execute(
                "update personas set system_prompt = ?, "
                "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime') "
                "where persona_id = ?",
                (text, PERSONA_ID),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error as exc:
        _log(f"写 AstrBot 库失败（不影响说话）：{exc}")
        return False


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(db_hash: str, file_hash: str) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"db": db_hash, "file": file_hash}, fh)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def note_file_written(text: str) -> None:
    """系统层人格演化写 persona.md 后调用：把 file hash 刷新进同步水位。

    不刷的话，下一次 sync_persona 会把「演化刚写的新人格」误判成
    「文件又变了」，和库撞车时可能走错分支（窄窗口覆盖）。这里只更新
    file 一侧，db 侧保持不动。
    """
    try:
        state = _load_state()
        _save_state(str(state.get("db") or ""), _hash(text))
    except Exception:  # noqa: BLE001 —— 同步是锦上添花，不能影响演化
        pass


def sync_persona(current: str) -> str:
    """对齐两头，返回**当前应该用**的人格文本。

    Args:
        current: 内存里现有的那份（读写失败时原样返回，不至于没话说）。
    """
    try:
        with open(PERSONA_PATH, encoding="utf-8") as fh:
            file_text = fh.read()
    except OSError:
        return current

    # 文件被清空（误删内容、编辑器还没写完）时什么都别做：
    # 一旦把空字符串同步过去，两边就都没人格了。
    if not file_text.strip():
        return current

    try:
        db_text = read_db_persona()
        file_hash = _hash(file_text)

        if db_text is None:
            # 库里没有这行（AstrBot 没装好 / 还没建库）：把文件写过去，让 WebUI 有东西看
            if _write_db(file_text):
                _log("AstrBot 库里没有 default 人格，已用 persona.md 建上")
                _save_state(file_hash, file_hash)
            return file_text

        db_hash = _hash(db_text)
        # ① 内容一致 → 收手。必须先比内容，否则写文件更新 mtime 会来回乒乓。
        if db_hash == file_hash:
            _save_state(db_hash, file_hash)
            return file_text

        state = _load_state()
        db_changed = state.get("db") != db_hash
        file_changed = state.get("file") != file_hash

        # ② 只有库变了 → 有人在 WebUI 里改的，以库为准
        if db_changed and not file_changed:
            _write_file(db_text)
            _save_state(db_hash, db_hash)
            _log("AstrBot 里的人格改过了，已同步到 persona.md（旧文件留在 persona.md.bak）")
            return db_text

        # ③ 只有文件变了 → 以文件为准，写回库
        if file_changed and not db_changed:
            if _write_db(file_text):
                _log("persona.md 改过了，已同步到 AstrBot 的 personas 表")
                _save_state(file_hash, file_hash)
            return file_text

        # ④ 两边都变了，或者第一次同步（没有 state）：文件赢 —— 它是 git 跟踪的真源
        if _write_db(file_text):
            _log("首次同步 / 两边都改过：以 persona.md 为准，已写进 AstrBot")
            _save_state(file_hash, file_hash)
        return file_text
    except Exception:  # noqa: BLE001
        # 同步是锦上添花，出任何事都不能影响她说话
        traceback.print_exc()
        return current
