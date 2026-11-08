"""turns 来源隔离（H4）的单测。

覆盖两个层面：

1. ``migrate_channel`` 的纯逻辑——来源键映射、老数据前缀反推、回填与幂等；
2. ``gateway.recent_turns`` 的过滤行为——**fail-closed** 的来源隔离。

为什么要有这个文件
------------------
H4 第一版（commit af9d1bd）的过滤条件写成了 ``channel = ? or channel is null``，
把「判不出来源的老行」当成 legacy 放行。真实库上跑出来是这样的：

    30 条群聊上下文里 8 条是 NULL，占 15031/15567 = 96.6% 的字符，
    其中 id=1079 是 9605 字符的私聊全量转写。

也就是说隔离看起来生效了，实际上群聊上下文几乎全是私聊内容。根因是
``infer_from_content`` 不认识 ``[系统任务：主动对话]`` 和 ``# 任务说明``
这两类插件注入的任务说明，于是它们留在 NULL 又被放行。

本文件的 ``test_群聊上下文不得包含超大私聊转写`` 就是钉死这个回归。

设计原则
--------
* 完全自包含：用 ``tmp_path`` 造独立数据库，**不碰** 真实的
  ``gateway/data/gateway.db``。
* 不依赖真实库内容：所有行都是本文件插入的合成数据。
* 只断言行为，不锁死 SQL 写法。

跑法::

    python -m pytest gateway/tests/test_channel_isolation.py -v
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# gateway/ 下才有 migrate_channel.py 与 gateway.py。
GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import migrate_channel as mc  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    """一个只有 turns 表（含 channel 列）的空库，返回路径。"""
    path = tmp_path / "gateway.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """create table turns(
               id integer primary key autoincrement,
               session text not null,
               role text not null,
               content text not null,
               path text,
               ts real not null,
               channel text)"""
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture()
def gateway_mod(tmp_path, monkeypatch):
    """导入 gateway 并把它的 DB_PATH 指到临时库，绝不碰真实记忆库。"""
    import gateway

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(gateway, "DB_PATH", str(data_dir / "gateway.db"))
    monkeypatch.setattr(gateway, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(gateway, "AUDIT_PATH", str(data_dir / "audit.jsonl"))
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            """create table turns(
                   id integer primary key autoincrement,
                   session text not null,
                   role text not null,
                   content text not null,
                   path text,
                   ts real not null,
                   channel text)"""
        )
        conn.commit()
    return gateway


def _insert(path: str, rows: list[tuple[str, str, str, str | None]]) -> None:
    """rows: (role, content, path, channel)"""
    with sqlite3.connect(path) as conn:
        for role, content, p, ch in rows:
            conn.execute(
                "insert into turns(session, role, content, path, channel, ts)"
                " values('main',?,?,?,?,1.0)",
                (role, content, p, ch),
            )
        conn.commit()


# --------------------------------------------------------------------------- #
# 1. 来源键映射
# --------------------------------------------------------------------------- #
def test_本机桌宠必须显式声明_不能靠标签为空推断():
    """channel 为空串在「群聊没取到群名」时同样成立，不能当成桌宠。"""
    assert mc.channel_key("", local=True) == mc.CHANNEL_PET
    assert mc.channel_key("", local=False) == mc.CHANNEL_QQ_UNKNOWN
    assert mc.channel_key("", local=False) != mc.CHANNEL_PET


@pytest.mark.parametrize(
    "label,expected",
    [
        ("私聊", mc.CHANNEL_QQ_PRIVATE),
        ("群聊·示例群", "qq_group:示例群"),
        ("群聊", mc.CHANNEL_QQ_GROUP),
        ("群聊·", mc.CHANNEL_QQ_GROUP),
        ("某个没见过的来源", "qq:某个没见过的来源"),
    ],
)
def test_标签映射(label, expected):
    assert mc.channel_key(label) == expected


def test_只有桌宠是可信本机来源():
    """`is_local_trusted()` 已删（2026-11-07，审计 G.1 第 1 条）。

    它原本是 `return key == CHANNEL_PET` 的一行包装，**生产零调用** ——
    真正的判据散在调用方，而且各处的口径并不相同：

      * `gateway.recent_turns()` 用 `migrate_channel.channel_key(channel,
        local=local_pet)` + `is_group_channel` + `hide_private` 三者组合判断
        （见那个函数里「来源过滤：fail-closed」那一大段），根本不需要这个包装；
      * `gateway._v1_guard()` 判"是不是本机来源"用的是 `_port_already_serving`
        式的东西，跟渠道键无关。

    删掉它以后**这条断言不能删** —— 它守的是一条安全性质：「只有 pet 是
    可信本机来源」。性质还在，只是执行点不在这个函数上了。所以改成直接
    断言 `channel_key()` 的实际行为（那才是生产在用的那个函数）：
    传 `local=True` 才是 pet，传任何标签都**不可能**得到 pet。
    """
    assert mc.channel_key("", local=True) == mc.CHANNEL_PET
    for label in (mc.LABEL_PRIVATE, f"{mc.LABEL_GROUP_PREFIX}某群", mc.LABEL_GROUP,
                  "", "某个没见过的来源"):
        assert mc.channel_key(label, local=False) != mc.CHANNEL_PET, label
    # 那个包装删了，就不该还有别的函数能绕出 pet 的判定
    assert not hasattr(mc, "is_local_trusted"), (
        "is_local_trusted 被加回来了？它零调用，加回来只会让来源判定又多一份口径"
    )


def test_未知标签不会退化成群聊或桌宠():
    """channel_key 对未知标签返回 qq:<label>，is_group_channel 必须判 False。

    如果这里判成群聊，recent_turns 就会按「本群」放行；
    如果判成桌宠，更是直接读全部。两种都是 fail-open。
    """
    key = mc.channel_key("没见过的来源")
    assert mc.is_group_channel(key) is False
    assert key != mc.CHANNEL_PET, "未知标签绝不能退化成桌宠（那是 fail-open）"
    assert mc.is_known_source(key) is False


def test_认不出来的来源必须走过滤分支():
    """is_known_source 是 fail-closed 的开关：认不出 → 过滤 → 精确匹配不存在的键。"""
    assert mc.is_known_source(mc.CHANNEL_PET) is True
    assert mc.is_known_source(mc.CHANNEL_QQ_PRIVATE) is True
    assert mc.is_known_source("qq_group:某群") is True
    for key in (mc.CHANNEL_QQ_UNKNOWN, "qq:没见过的来源", None, ""):
        assert mc.is_known_source(key) is False, key


# --------------------------------------------------------------------------- #
# 2. 老数据前缀反推
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "content,expected",
    [
        ("[群聊·示例群] 主人: 行，抱抱鱼", "qq_group:示例群"),
        ("[群聊] 群友: 在吗", "qq_group"),
        ("[私聊] 示例用户: 偷偷说", "qq_private"),
        ("主人: 桌宠里说的话", "pet"),
        ("主人：全角冒号也要认", "pet"),
        # 这两类是插件注入的任务说明，内容里带完整对话转写
        ("[系统任务：主动对话]\n你被授权在私聊中发起一次“主动消息”", "qq_private"),
        ("# 任务说明\n请以第一人称回顾并总结以下对话", "internal"),
        ("@小菲鱼 你好呀", "qq_group"),
        # 推不出来的保持 None（读取侧不放行）
        ("晓，早上好呀，昨晚你一直没回我", None),
        ("??????,???????", None),
    ],
)
def test_老数据前缀反推(content, expected):
    assert mc.infer_from_content(content) == expected


# --------------------------------------------------------------------------- #
# 3. 回填
# --------------------------------------------------------------------------- #
def test_回填_主动对话与总结任务必须归类_不能留NULL(db):
    """留 NULL = 在群聊里不可见；但更重要的是不能「留 NULL 又放行」。

    主动对话 → qq_private（它的回复跟着继承）；总结任务 → internal
    （纯内部噪音，连桌宠都不该看到）。
    """
    _insert(db, [
        ("user", "[系统任务：主动对话]\n你被授权在私聊中发起一次“主动消息”", "chat", None),
        ("assistant", "晓，都快十二点了，你是睡了还是在忙呀", "chat", None),
        ("user", "# 任务说明\n请以第一人称回顾并总结以下对话", "chat", None),
        ("assistant", "我记得昨天聊了很多", "chat", None),
    ])
    stats = mc.migrate(db)
    assert stats["from_prefix"] >= 2
    with sqlite3.connect(db) as conn:
        got = [r[0] for r in conn.execute("select channel from turns order by id")]
    assert got == ["qq_private", "qq_private", "internal", "internal"], got


def test_回填_继承要跳过中间的NULL行(db):
    """assistant 回复向前回溯到最近一条非空来源，不是只看紧邻上一条。"""
    _insert(db, [
        ("user", "[私聊] 示例用户: 有来源的话", "chat", None),
        ("user", "这条推不出来源", "chat", None),
        ("user", "这条也推不出来", "chat", None),
        ("assistant", "隔了两条 NULL 的回复", "chat", None),
    ])
    mc.migrate(db)
    with sqlite3.connect(db) as conn:
        got = [r[0] for r in conn.execute("select channel from turns order by id")]
    assert got[0] == "qq_private"
    assert got[3] == "qq_private", f"应回溯继承到 qq_private，实际 {got[3]}"


def test_已错标成私聊的总结任务会被纠正成internal(db):
    """v2 曾把 # 任务说明 归成 qq_private；relabel 要把它们改回来且幂等。"""
    _insert(db, [
        ("user", "# 任务说明\n请以第一人称回顾并总结以下对话", "chat", "qq_private"),
        ("user", "[私聊] 示例用户: 正常私聊", "chat", "qq_private"),
    ])
    first = mc.migrate(db)
    assert first["relabeled_internal"] == 1
    second = mc.migrate(db)
    assert second["relabeled_internal"] == 0
    with sqlite3.connect(db) as conn:
        got = [r[0] for r in conn.execute("select channel from turns order by id")]
    assert got == ["internal", "qq_private"], got


def test_回填_回溯继承跟随最近的非空来源(db):
    """assistant 行跟随「最近一条非空来源」，中间夹的 NULL 全部跳过。

    已知代价（有意接受，team-lead 指定）：隔了 NULL 的那条回复会被归到
    最近已知来源，如果那是群聊，回复就跟着进那个群。真实库里只有 1 条这种行
    （id=866，内容是「这么难的问题都让你搞定了，厉害呀」这类通用回复，
    不含私密事实）。换成「只继承非群来源」能消掉这 1 条，但会让主动对话的
    回复失去归属，权衡后选了简单的一档。
    """
    _insert(db, [
        ("user", "[群聊·某群] 群友: 群里的话", "chat", None),
        ("assistant", "群里的回复", "chat", None),
        ("user", "推不出来源的一句", "chat", None),
        ("assistant", "隔了一条 NULL 的回复", "chat", None),
    ])
    mc.migrate(db)
    with sqlite3.connect(db) as conn:
        got = [r[0] for r in conn.execute("select channel from turns order by id")]
    assert got[0] == "qq_group:某群"
    assert got[1] == "qq_group:某群"      # 紧邻继承
    assert got[2] is None                 # 推不出来源的 user 行留 NULL
    assert got[3] == "qq_group:某群"      # 回溯继承（已知代价，见 docstring）


def test_迁移幂等_连跑三次结果一致(db):
    _insert(db, [
        ("user", "[私聊] 示例用户: 私聊里的话", "chat", None),
        ("assistant", "私聊的回复", "chat", None),
        ("user", "[系统任务：主动对话] xxx", "chat", None),
    ])
    first = mc.migrate(db)
    second = mc.migrate(db)
    third = mc.migrate(db)
    assert first["added_column"] is False  # 建表时就带 channel 列
    assert first["from_prefix"] == 2
    assert second["from_prefix"] == 0 and second["inherited"] == 0
    assert third["from_prefix"] == 0 and third["inherited"] == 0
    assert first["with_channel"] == second["with_channel"] == third["with_channel"]


def test_迁移能在老库上补列(tmp_path):
    """没有 channel 列的老库：加列 + 回填，原始数据不动。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """create table turns(
               id integer primary key autoincrement,
               session text not null,
               role text not null,
               content text not null,
               path text,
               ts real not null)"""
    )
    conn.execute(
        "insert into turns(session, role, content, path, ts)"
        " values('main','user','[私聊] 示例用户: 老数据','chat',1.0)"
    )
    conn.commit()
    conn.close()

    stats = mc.migrate(str(path))
    assert stats["added_column"] is True
    with sqlite3.connect(path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(turns)")]
        assert "channel" in cols
        assert conn.execute("select content from turns").fetchone()[0] == "[私聊] 示例用户: 老数据"
        assert conn.execute("select channel from turns").fetchone()[0] == "qq_private"


# --------------------------------------------------------------------------- #
# 4. 读取侧：fail-closed 隔离
# --------------------------------------------------------------------------- #
def _seed_mixed(gateway_mod):
    """一条完整的混合来源样本，供下面的隔离用例共用。"""
    _insert(gateway_mod.DB_PATH, [
        ("user", "主人: 桌宠里说的私事", "chat", "pet"),
        ("user", "[私聊] 示例用户: 私聊里的秘密", "chat", "qq_private"),
        ("user", "[群聊·A] 群友甲: A群的话", "chat", "qq_group:A"),
        ("user", "[群聊·B] 群友乙: B群的话", "chat", "qq_group:B"),
        ("user", "[群聊·A] 工具调用", "tool", "qq_group:A"),
        ("user", "[群聊·A] 审批往返", "approval", "qq_group:A"),
    ])


def _contents(gateway_mod, channel, local_pet=False, hide_private=True):
    return [
        m["content"]
        for m in gateway_mod.recent_turns("main", 50, hide_private, channel, local_pet)
    ]


def test_群聊只看得见本群(gateway_mod):
    _seed_mixed(gateway_mod)
    got = _contents(gateway_mod, "群聊·A")
    assert any("A群的话" in x for x in got)
    assert not any("私聊里的秘密" in x for x in got)
    assert not any("桌宠里说的私事" in x for x in got)
    assert not any("B群的话" in x for x in got)


def test_群聊里主人说话也只看本群(gateway_mod):
    """过滤不看 speaker：群是广播场合，群友也能看见她的回复。"""
    _seed_mixed(gateway_mod)
    got = _contents(gateway_mod, "群聊·A", hide_private=False)
    assert not any("私聊里的秘密" in x for x in got)
    assert not any("桌宠里说的私事" in x for x in got)
    assert any("工具调用" in x for x in got)  # 主人自己的工具往返仍可见


def test_私聊只看得见私聊(gateway_mod):
    """陌生人给机器人发私聊时，不该读到主人的群聊与桌宠原话。"""
    _seed_mixed(gateway_mod)
    got = _contents(gateway_mod, "私聊")
    assert any("私聊里的秘密" in x for x in got)
    assert not any("A群的话" in x for x in got)
    assert not any("桌宠里说的私事" in x for x in got)


def test_主人在一对一私聊里保留全量上下文(gateway_mod):
    """主人自己私聊时（hide_private=False）仍能读全部——这是原有设计意图。

    注意这与「群聊里主人说话」不同：群是广播场合，即使主人在说话也只看本群
    （见 test_群聊里主人说话也只看本群）。两条一起才是完整语义。
    """
    _seed_mixed(gateway_mod)
    got = _contents(gateway_mod, "私聊", hide_private=False)
    assert len(got) == 6, got
    assert any("A群的话" in x for x in got)


def test_主人自己在群里说话也不放行私聊(gateway_mod):
    """owner_ids 命中时 hide_private=False，光靠 hide_private 拦不住。

    真实库里群聊来源 86 行有 38 行是主人自己说的，这是主路径。
    """
    _seed_mixed(gateway_mod)
    got = _contents(gateway_mod, "群聊·A", hide_private=False)
    assert not any("私聊里的秘密" in x for x in got)
    assert not any("桌宠里说的私事" in x for x in got)
    assert not any("B群的话" in x for x in got)


def test_取不到来源标签时什么都读不到(gateway_mod):
    """fail-closed：未知来源不能退化成「读全部」。"""
    _seed_mixed(gateway_mod)
    assert _contents(gateway_mod, "", local_pet=False) == []


def test_桌宠能读全部(gateway_mod):
    """桌宠是本机可信来源，不该受来源过滤限制。

    真实调用路径是 /chat → speaker="主人" → hide_private=False，
    所以这里也用 hide_private=False（True 会另外滤掉 tool/approval）。
    """
    _seed_mixed(gateway_mod)
    got = _contents(gateway_mod, "", local_pet=True, hide_private=False)
    assert len(got) == 6


def test_群聊上下文不得包含超大私聊转写(gateway_mod):
    """H4 v1 的真实回归：9605 字符的私聊转写占了群聊上下文 96.6% 的字符。

    这里把当时的现场复刻成一个用例——老数据（channel 为 NULL）里躺着一条
    巨大的私聊转写，群聊上下文必须一条都不放行。
    """
    big = "# 任务说明\n请以第一人称回顾并总结以下对话\n" + ("[晓 | ID: 10000001] 很长的私聊记录\n" * 400)
    assert len(big) > 9000
    _insert(gateway_mod.DB_PATH, [
        ("user", big, "chat", None),                       # 迁移前的老行，channel 为 NULL
        ("user", "[系统任务：主动对话] 你被授权在私聊中发起一次“主动消息”", "chat", None),
        ("user", "[群聊·示例群] 群友甲: 正常群消息", "chat", "qq_group:示例群"),
    ])
    got = _contents(gateway_mod, "群聊·示例群")
    assert got == ["[群聊·示例群] 群友甲: 正常群消息"], got
    assert not any("# 任务说明" in x for x in got)
    assert not any("[系统任务" in x for x in got)


def test_internal行在任何上下文都不可见(gateway_mod):
    """插件注入的「任务说明」是整段对话转写，不是聊天，连桌宠都不该看到。"""
    _insert(gateway_mod.DB_PATH, [
        ("user", "# 任务说明\n请以第一人称回顾并总结以下对话", "chat", "internal"),
        ("user", "[私聊] 示例用户: 正常私聊", "chat", "qq_private"),
        ("user", "[群聊·A] 群友: 正常群聊", "chat", "qq_group:A"),
    ])
    for channel, pet, hide in [
        ("私聊", False, True), ("私聊", False, False),
        ("群聊·A", False, True), ("群聊·A", False, False),
        ("", True, False),
    ]:
        got = _contents(gateway_mod, channel, local_pet=pet, hide_private=hide)
        assert not any("# 任务说明" in x for x in got), (channel, pet, hide, got)


def test_隔离是等值匹配_前缀相同的群不互相串(gateway_mod):
    """qq_group:A 与 qq_group:AB 不能被前缀匹配混在一起。"""
    _insert(gateway_mod.DB_PATH, [
        ("user", "A 群的话", "chat", "qq_group:A"),
        ("user", "AB 群的话", "chat", "qq_group:AB"),
    ])
    got = _contents(gateway_mod, "群聊·A")
    assert got == ["A 群的话"], got
