# -*- coding: utf-8 -*-
"""身份标头格式（2026-11-08 主人定的规格）。

## 要求

1. QQ 聊天里能成功解析发言者的 QQ 号；
2. 有一个字段盯着这个号 —— **等于预留号才返回「主人」，否则「路人」**；
3. 这个身份标示要作为**标头**给角色层：
       【私聊：10000001】X（主人）：我是谁？
   这样**系统层和角色层都知道**对方是不是主人。

## 为什么要这样（和旧实现对比）

旧标头：`【私聊：10000001】示例用户（主人）： 我是谁？`
  —— "主人"是代码替她判完再改名的结果。她只看到一个被改好的名字，
     既不知道对方是谁，也不知道凭什么这个人是主人。

新标头：`【私聊：10000001】X（主人）：我是谁？`
  —— 号、昵称、身份三个信息都在，她自己也能核对。
     昵称保留原样（那是她该知道的事实），身份是代码按号判的。
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

import gateway  # noqa: E402

OWNER_UID = next(iter(gateway.OWNER_IDS)) if gateway.OWNER_IDS else "10000001"
GUEST_UID = "999999999"
#: 主人真实的 QQ 昵称带 U+0489（Me 类组合标记）
OWNER_NICK_RAW = chr(0x0489) + "X " + chr(0x0489)


def _ident(uid: str, nick: str, label: str = "私聊") -> gateway.Identity:
    return gateway.Identity(uid=uid, speaker=nick, label=label, trusted_identity=True)


def _tag(uid: str, nick: str, label: str = "私聊") -> str:
    return gateway._speaker_tag(_ident(uid, nick, label), label, nick)


# ------------------------------------------------------------ 第 2 条：身份字段

def test_号命中预留号返回主人():
    assert "（主人）" in _tag(OWNER_UID, "随便什么昵称")


def test_号没命中返回路人():
    assert "（路人）" in _tag(GUEST_UID, "随便什么昵称")


def test_昵称叫主人但号不对_仍是路人():
    """闸不能靠昵称 —— 昵称是发送者自己随时能改的。"""
    tag = _tag(GUEST_UID, "主人")
    assert "（路人）" in tag
    assert "（主人）" not in tag


# ------------------------------------------------------------ 第 3 条：标头格式

def test_私聊标头带号和昵称和身份():
    tag = _tag(OWNER_UID, OWNER_NICK_RAW)
    assert tag == f"【私聊：{OWNER_UID}】X（主人）："


def test_群聊标头带群名():
    tag = _tag(GUEST_UID, "路人甲", label="群聊·示例群")
    assert tag == f"【群聊·示例群：{GUEST_UID}】路人甲（路人）："


def test_桌宠没有号_只写主人():
    tag = gateway._speaker_tag(gateway.identity_from_pet(), "", "主人", local_pet=True)
    assert tag == "主人："


def test_认不出号时如实留空不编():
    tag = gateway._speaker_tag(None, "私聊", "王五")
    assert tag == "【私聊：】王五（路人）："


# ------------------------------------------------------------ 昵称清理

def test_装饰组合字符被清掉():
    """主人真实昵称 示例用户 里的 U+0489 是 Me 类，必须清掉。"""
    assert gateway._clean_nickname(OWNER_NICK_RAW) == "X"


def test_emoji变体选择符要保留():
    """U+FE0F 也是组合字符，但删了 emoji 会从彩色变黑白轮廓。"""
    assert gateway._clean_nickname("小鱼\u2764\ufe0f") == "小鱼\u2764\ufe0f"


def test_零宽字符被清掉():
    assert gateway._clean_nickname("张\u200b三") == "张三"


def test_普通装饰不删():
    """星号波浪号是昵称的一部分，删了就不是人家名字了。"""
    assert gateway._clean_nickname("*星号*") == "*星号*"


# ------------------------------------------------------------ 端到端

def test_真实请求走完标头正确():
    """用 AstrBot 的真实结构（list + 身份块）跑完整链路。"""
    block = (
        f"<system_reminder>User ID: {OWNER_UID}, Nickname: {OWNER_NICK_RAW}\n"
        "Current datetime: 2026-11-08 15:40 (CST), Weekday: Sunday</system_reminder>"
    )
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "我是谁"},
        {"type": "text", "text": block},
    ]}]
    ident = gateway._openai_identity(msgs)
    assert ident.uid == OWNER_UID
    assert ident.is_owner is True

    text, speaker, uid, group = gateway._openai_user_text(msgs)
    channel = gateway._channel_label(group, uid)
    tag = gateway._speaker_tag(ident, channel, speaker)
    assert tag == f"【私聊：{OWNER_UID}】X（主人）："
