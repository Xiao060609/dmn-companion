# -*- coding: utf-8 -*-
"""后台「身份」卡片：主人 QQ 号 + 桌宠名字。

这两样以前都是硬编码：
  * `OWNER_IDS` 只在 import 时从 `config.json` 的 dialog 段读一次，
    想改必须手动编辑文件再重启；
  * 名字写死在 `SELF_NAME_RE`（`小菲鱼|小鱼`）里，改了名字模型自报
    家门时那句旁白就剥不掉了。

现在做成后台可改的配置。本文件钉住：
1. 读取优先级：data/identity.json > config.json > 兜底；
2. 保存后**热生效**（不需要重启）；
3. 输入清洗：QQ 号只留数字、名字去控制字符、限量；
4. 安全底线：一个主人都没配是合法状态（谁都不算主人）；
   名字为空要拒绝（空名字会让标头和提示词变形）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402


@pytest.fixture()
def ident_env(tmp_path, monkeypatch):
    """干净的 identity 环境：数据文件不存在、config.json 是临时副本。

    ⚠ 还必须**备份并还原三个全局常量**（OWNER_IDS / PET_NAME / SELF_NAME_RE）。
    `apply_identity()` 会热替换它们 —— 那是生产行为，但对测试来说就是
    把状态漏给了后面所有用例：实测会让 test_speaker_tag、test_identity 等
    11 条与身份相关的测试连环失败，而且单独跑都是好的（典型的顺序污染）。
    monkeypatch 只能还原它自己 setattr 过的属性，还原不了「函数内部
    global 赋值」，所以这里手动存一份、finally 写回去。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gateway, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(gateway, "IDENTITY_PATH", str(data_dir / "identity.json"))
    # 审计文件也隔离：`_audit_identity_change()` 会写 system_audit.jsonl，
    # 不隔离的话测试会往生产的审计流水里灌一堆假改名记录。
    import system  # noqa: PLC0415
    monkeypatch.setattr(system, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH",
                        str(data_dir / "system_audit.jsonl"))
    # 允许回写 —— 本文件要验证「config.json 也跟上了」这条行为。
    # 但上面把 ROOT 指到了 tmp，所以写的是临时副本，不是生产文件。
    monkeypatch.setattr(gateway, "WRITE_CONFIG_MIRROR", True)

    # config.json 用 ROOT 重定向（和 test_key_store 同一套约定）：
    # `config_path()` 是从 ROOT 现算的函数，不是固化常量。
    monkeypatch.setattr(gateway, "ROOT", str(tmp_path))
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"dialog": {"owner_ids": ["10000001"]}}, ensure_ascii=False),
        encoding="utf-8",
    )

    saved = {
        "OWNER_IDS": gateway.OWNER_IDS,
        "PET_NAME": gateway.PET_NAME,
        "PET_ALIASES": gateway.PET_ALIASES,
        "SELF_NAME_RE": gateway.SELF_NAME_RE,
        "OWNER_LABEL": gateway.OWNER_LABEL,
    }
    yield {"data_dir": data_dir, "cfg": cfg}
    for name, value in saved.items():
        setattr(gateway, name, value)

# --------------------------------------------------------------------------- #
# 1. 读取优先级
# --------------------------------------------------------------------------- #
def test_没有文件时从_config_读(ident_env):
    ident = gateway._load_identity()
    assert ident["owner_ids"] == ["10000001"]
    # 名字没有配置时用兜底值，不能是空串
    assert ident["pet_name"] == gateway._DEFAULT_PET_NAME


def test_data_文件覆盖_config(ident_env):
    Path(gateway.IDENTITY_PATH).write_text(
        json.dumps({"owner_ids": ["111"], "pet_name": "阿鱼"}, ensure_ascii=False),
        encoding="utf-8",
    )
    ident = gateway._load_identity()
    assert ident["owner_ids"] == ["111"]
    assert ident["pet_name"] == "阿鱼"


def test_config_里没有身份段也不炸(ident_env, monkeypatch):
    cfg = ident_env["cfg"]
    cfg.write_text("{}", encoding="utf-8")
    ident = gateway._load_identity()
    assert ident["owner_ids"] == []
    assert ident["pet_name"] == gateway._DEFAULT_PET_NAME


# --------------------------------------------------------------------------- #
# 2. 保存 + 热生效
# --------------------------------------------------------------------------- #
def test_保存后立刻生效不需要重启(ident_env, monkeypatch):
    monkeypatch.setattr(gateway, "OWNER_IDS", {"10000001"})

    ok, msg = gateway.apply_identity(
        {"owner_ids": ["111", "222"], "pet_name": "阿鱼"}
    )
    assert ok, msg

    # 进程内的常量已经被换掉 —— 这就是"热生效"
    assert gateway.OWNER_IDS == {"111", "222"}
    assert gateway.PET_NAME == "阿鱼"
    # 自报家门的正则也跟着换：新名字能剥，旧别名仍能剥
    assert gateway.SELF_NAME_RE.sub("", "阿鱼：在的") == "在的"
    assert gateway.SELF_NAME_RE.sub("", "小鱼：在的") == "在的"


def test_保存会落盘且回写_config(ident_env):
    ok, _ = gateway.apply_identity({"owner_ids": ["999"], "pet_name": "新名字"})
    assert ok

    on_disk = json.loads(Path(gateway.IDENTITY_PATH).read_text(encoding="utf-8"))
    assert on_disk["owner_ids"] == ["999"]
    assert on_disk["pet_name"] == "新名字"

    # config.json 也要跟上，否则看文件的人会以为没保存
    cfg = json.loads(ident_env["cfg"].read_text(encoding="utf-8"))
    assert cfg["identity"]["pet_name"] == "新名字"
    assert cfg["identity"]["owner_ids"] == ["999"]


# --------------------------------------------------------------------------- #
# 3. 输入清洗
# --------------------------------------------------------------------------- #
def test_qq_号只留数字(ident_env):
    ok, _ = gateway.apply_identity(
        {"owner_ids": [" 111 ", "abc", "2-2-2", ""], "pet_name": "阿鱼"}
    )
    assert ok
    # 非数字被逐条剔除；"2-2-2" 去掉非数字后是 "222"
    assert gateway.OWNER_IDS == {"111", "222"}


def test_全是非数字时拒绝(ident_env):
    ok, msg = gateway.apply_identity({"owner_ids": ["abc"], "pet_name": "阿鱼"})
    assert not ok
    assert "数字" in msg


def test_名字为空要拒绝(ident_env):
    ok, msg = gateway.apply_identity({"owner_ids": ["111"], "pet_name": "   "})
    assert not ok
    assert "不能为空" in msg


def test_名字里的控制字符被去掉(ident_env):
    ok, _ = gateway.apply_identity({"owner_ids": [], "pet_name": "阿\x00鱼\x1f"})
    assert ok
    assert gateway.PET_NAME == "阿鱼"


def test_名字超长被截断(ident_env):
    ok, _ = gateway.apply_identity({"owner_ids": [], "pet_name": "鱼" * 100})
    assert ok
    assert len(gateway.PET_NAME) == gateway._PET_NAME_MAX


def test_重复的号会去重(ident_env):
    ok, _ = gateway.apply_identity({"owner_ids": ["111", "111", "222"],
                                    "pet_name": "阿鱼"})
    assert ok
    assert sorted(gateway.OWNER_IDS) == ["111", "222"]


def test_号的数量有上限(ident_env):
    many = [str(1000 + i) for i in range(50)]
    ok, _ = gateway.apply_identity({"owner_ids": many, "pet_name": "阿鱼"})
    assert ok
    assert len(gateway.OWNER_IDS) == gateway._OWNER_IDS_MAX_COUNT


# --------------------------------------------------------------------------- #
# 4. 安全底线
# --------------------------------------------------------------------------- #
def test_一个主人都没有是合法状态(ident_env):
    """清空主人号 = 关掉主人特权，必须允许。

    如果这里拒绝，主人就没有办法撤销一个泄露的号。
    """
    ok, msg = gateway.apply_identity({"owner_ids": [], "pet_name": "阿鱼"})
    assert ok, msg
    assert gateway.OWNER_IDS == set()


def test_名字不做身份凭据(ident_env):
    """改名字**不能**让人获得主人权限。

    这是一条安全底线：昵称谁都能改，身份只认 QQ 号。
    """
    ok, _ = gateway.apply_identity({"owner_ids": [], "pet_name": "主人"})
    assert ok
    ident = gateway.Identity(
        uid="999999", speaker="主人", trusted_identity=True
    )
    assert not ident.is_owner, "把名字改成「主人」不该拿到主人权限"


# --------------------------------------------------------------------------- #
# 5. 改名必须真的传进人格
# --------------------------------------------------------------------------- #
def test_改名后人格里的名字跟着变(ident_env, monkeypatch):
    """人格文件里不写死名字，用 {name} 占位。

    ⚠ 这条是补的漏：一开始只改了标头和 SELF_NAME_RE，
    但 persona.md 里仍写着「我是小菲鱼」——实测把名字改成「示例用户」之后，
    人格第一行还是「我是小菲鱼」，她会继续这么自称，跟标头对不上。
    """
    monkeypatch.setattr(gateway, "PERSONA", "我是{name}。我在。")
    ok, _ = gateway.apply_identity({"owner_ids": [], "pet_name": "示例用户"})
    assert ok

    text = gateway.current_persona()
    assert "示例用户" in text
    assert "{name}" not in text, "占位符必须被替换掉"


def test_人格里的情绪标记不能被误伤(ident_env, monkeypatch):
    """`{calm}` / `{happy}` 是给下游解析的情绪标记，不能碰。"""
    monkeypatch.setattr(
        gateway, "PERSONA", "我是{name}呀 住在你电脑里的{calm}"
    )
    gateway.apply_identity({"owner_ids": [], "pet_name": "示例用户"})
    text = gateway.current_persona()
    assert "我是示例用户呀" in text
    assert "{calm}" in text, "情绪标记被替换掉了，下游会解析失败"


def test_改回原名也正常(ident_env, monkeypatch):
    monkeypatch.setattr(gateway, "PERSONA", "我是{name}。")
    gateway.apply_identity({"owner_ids": [], "pet_name": "示例用户"})
    gateway.apply_identity({"owner_ids": [], "pet_name": "小菲鱼"})
    assert "我是小菲鱼。" in gateway.current_persona()


# --------------------------------------------------------------------------- #
# 6. 审计留痕
# --------------------------------------------------------------------------- #
def _audit_rows() -> list[dict]:
    import system  # noqa: PLC0415
    p = Path(system.SYSTEM_AUDIT_PATH)
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def test_改名会记一笔流水账(ident_env):
    """改了就记一笔，旧值也带上。

    定位是**流水账**：只记事实，不判断重要性 —— 有没有价值由系统层判断。
    """
    gateway.apply_identity({"owner_ids": ["10000001"], "pet_name": "示例用户"})

    rows = [r for r in _audit_rows() if r.get("event") == "identity_update"]
    assert rows, "改名没记流水账"

    last = rows[-1]
    assert last["ok"] is True
    assert last["pet_name"] == "示例用户"
    # 旧值要带上，否则事后还原不回去
    assert last["pet_name_prev"] != "示例用户"


def test_改主人号也会记一笔(ident_env):
    gateway.apply_identity({"owner_ids": ["111"], "pet_name": "小菲鱼"})
    rows = [r for r in _audit_rows() if r.get("event") == "identity_update"]
    assert rows
    last = rows[-1]
    assert last["owner_ids"] == ["111"]
    assert "owner_ids_prev" in last


def test_每次保存都记_不替系统层筛选(ident_env):
    """值没变也照记。

    这是刻意的：判断"这次改动重不重要"是系统层的事，配置层不该替它决定。
    反复点保存就是几条一样的流水，系统层自己会看出没变化。
    """
    gateway.apply_identity({"owner_ids": ["10000001"], "pet_name": "小菲鱼"})
    n1 = len([r for r in _audit_rows() if r.get("event") == "identity_update"])
    gateway.apply_identity({"owner_ids": ["10000001"], "pet_name": "小菲鱼"})
    n2 = len([r for r in _audit_rows() if r.get("event") == "identity_update"])
    assert n2 == n1 + 1, "第二次保存没记账（不该替系统层判断『没变化就不记』）"


def test_身份变更不进对话流水账(ident_env):
    """**关键**：改设置不能写进 turns。

    `turns` 是"主人和她真的聊过什么"的账本。把「改了个设置」塞进去，
    她会当成一段真实对话记进记忆，之后回忆起来以为自己提过改名 ——
    那是假记忆。流水账走 `system_audit.jsonl`，走的是另一条线。
    """
    import sqlite3  # noqa: PLC0415

    gateway.apply_identity({"owner_ids": ["10000001"], "pet_name": "示例用户"})

    conn = sqlite3.connect(gateway.DB_PATH)
    try:
        rows = conn.execute("select count(*) from turns").fetchone()[0]
    finally:
        conn.close()
    assert rows == 0, "身份变更被写进了对话记录，她会当成真的聊过"
