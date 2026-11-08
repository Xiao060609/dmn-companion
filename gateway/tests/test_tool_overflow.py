"""工具轮数用尽时必须还能给出答案（回归：搜完三轮一句结论都没有）。

真实现象：用户让鱼上网查东西，模型连续三轮都调 `web_search`（先搜、结果不对
再细化、再搜），`MAX_TOOL_ROUNDS` 用完，直接落到 `tool_overflow` 的兜底
「我想了想还是没弄好」—— 前面几次搜索全白做。

修法：轮数用尽后再问一次，这次**不给工具**，逼它用已有结果说话。
本文件钉住这个收尾路径。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

#: 本文件**故意**不在顶层 import gateway：`conftest.py` 要在 import 之前
#: 改 `GATEWAY_DATA_DIR`，顶层 import 会把生产路径烤进模块常量。
#: 下面 `gw` fixture 负责真正 import；新加的 `TestMaxToolRoundsConfig`
#: 用 autouse fixture 把它绑到这个名字上。
gateway = None


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    import gateway

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "gateway.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """create table turns(
               id integer primary key autoincrement,
               session text not null, role text not null, content text not null,
               path text, ts real not null, channel text)"""
    )
    conn.execute(
        """create table pending(
               session text primary key, action text not null,
               args text not null, ts real not null, origin text)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(gateway, "DB_PATH", str(db))
    monkeypatch.setattr(gateway, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(gateway, "AUDIT_PATH", str(data_dir / "audit.jsonl"))
    # 工具循环只在角色层 cloud 模式下才会带上工具
    # （gateway.handle_chat: use_tools=ROLE["mode"] == "cloud"）。
    # 以前 ROLE 是 import 期从生产 `data/role_model.json` 读的，这里原先是在偷偷
    # 依赖用户的那份配置；测试隔离掉 DATA_DIR 之后就必须自己声明清楚。
    monkeypatch.setattr(gateway, "ROLE", {**gateway.ROLE, "mode": "cloud"})
    # 工具本身不在这里测，直接给个固定结果
    monkeypatch.setattr(
        gateway.tools, "run", lambda name, args: {"ok": True, "result": "1. 某结果｜摘要｜https://e.com"}
    )
    return gateway


def _tool_call(name="web_search"):
    return [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": name, "arguments": '{"query": "测试"}'},
        }
    ]


def test_轮数用尽后会强制收尾作答(gw, monkeypatch):
    """给工具时永远只调工具；一旦不给工具，就必须说话。"""
    seen = []

    def fake_call(messages, use_tools=True):
        seen.append(use_tools)
        if use_tools:
            return None, {}, _tool_call()
        return "查到了，是多模态的。", {}, None

    monkeypatch.setattr(gw, "call_model", fake_call)

    result = gw.handle_chat("主人：帮我查一下", "s1", speaker="主人", local_pet=True)

    assert result["text"], "必须给出非空回答"
    assert "没弄好" not in result["text"], "不该落到 tool_overflow 的兜底话术"
    assert seen[-1] is False, "最后一次必须是不带工具的收尾调用"
    assert seen.count(True) == gw.MAX_TOOL_ROUNDS


def test_收尾也失败才认输(gw, monkeypatch):
    """连收尾调用都拿不到内容，才允许回兜底话术。"""
    monkeypatch.setattr(gw, "call_model", lambda m, use_tools=True: (None, {}, _tool_call() if use_tools else None))

    result = gw.handle_chat("主人：帮我查一下", "s2", speaker="主人", local_pet=True)
    assert "没弄好" in result["text"]

# --------------------------------------------------------------------------- #
# 九、`dialog.max_tool_rounds` 配置真的生效（审计 G.2 第 2 条 / 板块 I 第 4 条）
#
# 修前：`config.json` 里写着 `system.max_tool_rounds = 2`，代码里
#       `gateway.MAX_TOOL_ROUNDS = 4` 硬编码 —— **那个配置从来没生效过**。
#
# 两个问题叠在一起，所以修法不是"把配置读进来"就完事：
#   ① **放错段**：`system.*` 是系统层（巩固/梳理/体检）的配置段，
#      而工具循环是**角色层**（`handle_chat`）的。角色层旋钮都在 `dialog.*`。
#   ② **值也是错的**：配置写 2，而注释和实测说的是"3 轮经常刚好用完" ——
#      真按 2 生效会悄悄砍掉一轮，表现是"她查东西查到一半就放弃"。
# 所以：挪到 `dialog.max_tool_rounds` 并改成 4（= 原来的硬编码值，行为零变化）。
#
# 为什么选"接上"而不是"删配置"：这个旋钮有真实用途 —— 换本地小模型时调小省钱，
# 换强模型时调大让多步搜索走完。删掉等于把能力锁死在代码里。
# --------------------------------------------------------------------------- #
class TestMaxToolRoundsConfig:
    """需要 `gateway` 模块本身（配置读取在 import 期就跑过了）。

    本文件模块顶层没 `import gateway`（既有 fixture 里才 import，为了
    在 conftest 改了环境变量之后再导入）。所以这里用一个 autouse fixture
    把那一步补上，别在每个用例签名里重复写。
    """

    @pytest.fixture(autouse=True)
    def _gw(self, gw):
        global gateway
        gateway = gw
        return gw

    def test_配置项在dialog段且值是4(self):
        """`system.*` 是系统层的段，角色层旋钮不该长在那里。"""
        cfg = gateway.CFG
        assert "max_tool_rounds" in cfg["dialog"], (
            "配置项应该在 dialog 段（角色层）—— 放 system 段会让看配置的人"
            "以为它是系统层的旋钮"
        )
        assert cfg["dialog"]["max_tool_rounds"] == 4, (
            "值必须是 4（原来的硬编码值）。改小会悄悄砍掉一轮工具调用"
        )
        assert "max_tool_rounds" not in (cfg.get("system") or {}), (
            "system 段里那个 2 是死配置，必须删掉 —— 留着就还会有人去改它"
        )

    def test_硬编码常量来自配置(self):
        """`MAX_TOOL_ROUNDS` 必须**算出来**，不能是字面量。"""
        assert gateway.MAX_TOOL_ROUNDS == gateway._max_tool_rounds()
        assert gateway.MAX_TOOL_ROUNDS == 4

    @pytest.mark.parametrize("raw,expected", [
        (2, 2),
        (4, 4),
        (8, 8),
        (1, 1),
        (0, 1),        # 0 会让一轮工具都不走：模型只能"说"不能"做"
        (-5, 1),
        (99, 8),       # 过大能烧掉一整个请求预算
        ("3", 3),      # JSON 里可能被写成字符串
        ("坏值", 4),    # 坏值回退默认，不抛
        (None, 4),
        (3.7, 3),
    ])
    def test_取值一律夹在1到8之间(self, raw, expected):
        """配置写坏了不能让网关起不来 —— 也绝不能让它变成 0 或天文数字。"""
        gate = pytest.MonkeyPatch()
        try:
            gate.setitem(gateway.CFG, "dialog",
                         {**gateway.CFG["dialog"], "max_tool_rounds": raw})
            assert gateway._max_tool_rounds() == expected, raw
        finally:
            gate.undo()

    def test_配置整个缺了也不抛(self):
        gate = pytest.MonkeyPatch()
        try:
            dialog = {k: v for k, v in gateway.CFG["dialog"].items()
                      if k != "max_tool_rounds"}
            gate.setattr(gateway, "CFG", {**gateway.CFG, "dialog": dialog})
            assert gateway._max_tool_rounds() == 4
            # 连 dialog 段都没有
            gate.setattr(gateway, "CFG", {k: v for k, v in gateway.CFG.items()
                                          if k != "dialog"})
            assert gateway._max_tool_rounds() == 4
        finally:
            gate.undo()

    def test_工具循环真的按这个数跑(self, gw, monkeypatch):
        """端到端：把配置调到 2，`handle_chat` 就只调 2 轮工具。

        这条是**验收标准**：配置改了，行为跟着改。修前它恒是 4，
        `seen.count(True)` 永远是 4，配置写了等于没写。

        ⚠ 用 `gw` fixture（它建好隔离库 + cloud 模式 + 桩工具），
        别自己造一个 `db` —— 本文件没有那个 fixture。
        """
        monkeypatch.setitem(gateway.CFG, "dialog",
                            {**gateway.CFG["dialog"], "max_tool_rounds": 2})
        monkeypatch.setattr(gateway, "MAX_TOOL_ROUNDS", gateway._max_tool_rounds())
        seen: list = []

        def fake_call(messages, use_tools=True):
            seen.append(use_tools)
            if use_tools:
                return None, {}, [{"id": "c1", "type": "function",
                                   "function": {"name": "web_search",
                                                "arguments": '{"query": "x"}'}}]
            return "查完了。", {}, None

        monkeypatch.setattr(gateway, "call_model", fake_call)
        result = gateway.handle_chat("主人：帮我查一下", "s1",
                                     speaker="主人", local_pet=True)
        assert result["text"]
        assert seen.count(True) == 2, f"配置写 2，实际跑了 {seen.count(True)} 轮"
        assert seen[-1] is False, "最后一轮必须是不带工具的收尾调用"
