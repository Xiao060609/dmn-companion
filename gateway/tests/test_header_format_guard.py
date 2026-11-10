# -*- coding: utf-8 -*-
"""守卫测试：标头格式改了，所有解析它的地方必须同步。

这次的真事故（2026-11-09）：
  11-08 按主人要求把标头从
      `[私聊] 主人: xxx`  /  `[群聊·群名] 某人: xxx`
  改成了
      `【私聊：QQ号】昵称（主人）：xxx`  /  `【群聊·群名：QQ号】昵称（路人）：xxx`

  但**三处解析代码没跟着改**：
    1. `system.OWNER_PRIVATE_RE`   → 巩固读不到任何主人私聊
    2. `system.GROUP_LABEL_RE`     → 群聊渠道永远没有增量
    3. `_read_recent_chat_multi` 里硬编码的 `startswith("[私聊] 主人")`
    4. `migrate_channel.infer_from_content` 的前缀常量

  后果：巩固 455 次只成功 28 次、人格复盘 40 小时没成功、
        每日梳理没有素材 —— 看起来像"新对话不够"，
        实际上是**一条都认不出来**。

  这个测试的作用：**以 _speaker_tag() 的真实输出为唯一真源**，
  断言每一处解析都能吃下它。以后谁再改标头格式，这里立刻红。
"""
from __future__ import annotations

import sys
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402
import migrate_channel  # noqa: E402
import system  # noqa: E402

OWNER_UID = "10000001"
OTHER_UID = "10000002"


def _tag(uid: str, speaker: str, channel_key: str, label: str) -> str:
    """用生产代码生成标头，不手写字符串。"""
    ident = gateway.Identity(
        uid=uid, speaker=speaker,
        channel_key=channel_key, trusted_identity=True,
    )
    return gateway._speaker_tag(ident, label, speaker)


# --------------------------------------------------------------------------- #
# 真源：_speaker_tag 的输出格式
# --------------------------------------------------------------------------- #
def test_主人私聊标头能被OWNER_PRIVATE_RE认出():
    """`OWNER_PRIVATE_RE` 是巩固/复盘认主人的唯一依据。

    它要是认不出真实标头，整条"主人私聊"渠道就废了
    —— 巩固报 no_new_turns，而你看到的是"她不记事"。
    """
    tag = _tag(OWNER_UID, "示例用户", "qq_private", "私聊")
    assert system.OWNER_PRIVATE_RE.match(tag), (
        "生产标头 %r 匹配不上 OWNER_PRIVATE_RE(%r)。"
        "标头格式改了，但识别它的正则没跟着改 —— 巩固会全线失效。"
        % (tag, system.OWNER_PRIVATE_RE.pattern)
    )


def test_路人私聊标头不能被当成主人():
    """fail-closed：路人的私聊绝不能按主人去抽事实。"""
    tag = _tag(OTHER_UID, "张三", "qq_private", "私聊")
    assert not system.OWNER_PRIVATE_RE.match(tag), (
        "路人的标头 %r 被当成主人了 —— 陌生人说的话会被记到主人头上" % tag
    )


def test_群聊标头能被GROUP_LABEL_RE认出且群名正确():
    """`GROUP_LABEL_RE` 要取到群名，且和渠道键后缀一致。

    取不到群名 → 群聊渠道整条没有增量。
    取错群名 → 把别的群的话记到这个群头上。
    """
    tag = _tag(OTHER_UID, "黑崎小雪", "qq_group:测试群", "群聊·测试群")
    m = system.GROUP_LABEL_RE.match(tag)
    assert m, (
        "生产标头 %r 匹配不上 GROUP_LABEL_RE(%r)"
        % (tag, system.GROUP_LABEL_RE.pattern)
    )
    assert m.group(1).strip() == "测试群", (
        "群名取错了：拿到 %r，应该是 '测试群'" % m.group(1)
    )


def test_群名带特殊字符也能取对():
    """群名可能带空格、英文、数字。"""
    for name in ("示例群", "My Group", "群-A1"):
        tag = _tag(OWNER_UID, "X", "qq_group:%s" % name, "群聊·%s" % name)
        m = system.GROUP_LABEL_RE.match(tag)
        assert m and m.group(1).strip() == name, (
            "群名 %r 没取对，标头是 %r" % (name, tag)
        )


# --------------------------------------------------------------------------- #
# 所有解析入口都要认
# --------------------------------------------------------------------------- #
def test_migrate_channel能认出新格式的私聊():
    tag = _tag(OWNER_UID, "示例用户", "qq_private", "私聊")
    got = migrate_channel.infer_from_content(tag)
    assert got == migrate_channel.CHANNEL_QQ_PRIVATE, (
        "infer_from_content(%r) = %r，应该是 %r"
        % (tag, got, migrate_channel.CHANNEL_QQ_PRIVATE)
    )


def test_migrate_channel能认出新格式的群聊():
    tag = _tag(OWNER_UID, "X", "qq_group:测试群", "群聊·测试群")
    got = migrate_channel.infer_from_content(tag)
    assert got == "%s:测试群" % migrate_channel.CHANNEL_QQ_GROUP, (
        "infer_from_content(%r) = %r，群名应该完整保留" % (tag, got)
    )


def test_migrate_channel仍然认老格式():
    """老数据（11-08 之前）只有旧格式，回填/回溯要用它 —— 不能为了新格式丢掉。"""
    cases = [
        ("[私聊] 主人: 老数据", migrate_channel.CHANNEL_QQ_PRIVATE),
        ("[群聊·某群] 某人: 老数据", "%s:某群" % migrate_channel.CHANNEL_QQ_GROUP),
        ("[群聊] 没群名", migrate_channel.CHANNEL_QQ_GROUP),
        ("主人: 桌宠里说的", migrate_channel.CHANNEL_PET),
    ]
    for text, expect in cases:
        got = migrate_channel.infer_from_content(text)
        assert got == expect, "老格式 %r 解析成 %r，应该是 %r" % (
            text, got, expect)


# --------------------------------------------------------------------------- #
# 过滤函数端到端
# --------------------------------------------------------------------------- #
def test_主人私聊经过滤后保留_路人被丢():
    """`_filter_owner_turns` 是巩固前的最后一道筛子。"""
    owner_tag = _tag(OWNER_UID, "示例用户", "qq_private", "私聊")
    other_tag = _tag(OTHER_UID, "张三", "qq_private", "私聊")
    turns = [
        {"id": 1, "role": "user", "content": owner_tag + "我在忙"},
        {"id": 2, "role": "assistant", "content": "好，你先忙。"},
        {"id": 3, "role": "user", "content": other_tag + "在吗"},
        {"id": 4, "role": "assistant", "content": "你是？"},
    ]
    out = system._filter_owner_turns(turns)
    ids = [t["id"] for t in out]
    assert 1 in ids, "主人的话被丢了"
    assert 2 in ids, "主人之后的回复应该跟着"
    assert 3 not in ids, "路人的话不能留"
    assert 4 not in ids, "路人之后的回复也不该跟进来"


def test_群聊过滤只留本群():
    tag_ok = _tag(OTHER_UID, "甲", "qq_group:群A", "群聊·群A")
    tag_other = _tag(OTHER_UID, "乙", "qq_group:群B", "群聊·群B")
    turns = [
        {"id": 1, "role": "user", "content": tag_ok + "本群的话"},
        {"id": 2, "role": "assistant", "content": "回应"},
        {"id": 3, "role": "user", "content": tag_other + "别群的话"},
    ]
    out = system._filter_group_turns("qq_group:群A", turns)
    ids = [t["id"] for t in out]
    assert 1 in ids, "本群发言应该保留"
    assert 3 not in ids, "别的群的话混进来了"
