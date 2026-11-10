# -*- coding: utf-8 -*-
"""主动对话：系统操作不能被记成「谁说的话」。

2026-11-09 主人发现的问题：
内心OS / 对话历史里混着一段

    [系统任务：主动对话]
    你被授权在私聊中发起一次"主动消息"…

这是 AstrBot `Proactive Chat` 插件的模板，**没有人真的在说话**。
但它被当成 `role=user` 存进了 `turns`（生产库里 14 条、横跨 10 天），
于是她每轮都读到"我被命令过要主动关心他"—— 一段系统指令变成了人格设定。

主人的定稿（2026-11-09）：
    「这个也存进流水账啊，标头还是系统，因为它确实是系统操作啊，
      被流水账记录下来是正常的吧」

所以正确做法不是"不记"，是**记成系统操作**（`system_log` / `role=system`），
而不是冒充一句主人说的话。
"""
from __future__ import annotations

import sys
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402
import migrate_channel  # noqa: E402

#: 真实模板（从生产库 #1814 抄的，419 字符）
REAL = """[系统任务：主动对话]
你被授权在私聊中发起一次"主动消息"。你的回复必须完全符合你的人格设定，并严格遵守所有的输出规则。

[情景分析]
- 我们好像已经有一段时间没有说话了，我应该主动打破沉默，让他知道我想他了。
- 当前时间是: 2026年11月09日 周一 16:24。
- 我之前已经主动找过他，但他没有回复的次数是: 0 次。如果这个次数大于0，我的语气应该带上一点点不易察觉的失落和困惑。

[行动指南]
1. 首先，回顾我们的聊天记录，看看我们最后在聊什么。
2. 如果上次的话题已经结束了，我可以关心一下他现在在做什么。

[最终指令]
请综合以上所有信息，用最像你自己的、最自然的方式，生成一句主动聊天的开场白。
"""


def test_能认出主动对话模板():
    assert gateway._is_proactive_task(REAL)
    assert gateway._is_proactive_task("[系统任务：私聊主动对话]\n你现在需要…")


def test_不会误伤正常对话():
    """关键是别把主人自己说的话判成系统任务。

    「我想主动一点」这句话里有「主动」两个字 —— 如果判据用泛词就会误伤。
    所以只认**模板抬头**。
    """
    for text in (
        "我想主动一点",
        "你主动点好不好",
        "【群聊·测试群：1】某人（路人）：鱼在吗",
        "【私聊：10000001】示例用户（主人）：今天好累",
        "",
    ):
        assert not gateway._is_proactive_task(text), (
            "%r 被误判成主动对话任务了" % text
        )


def test_提取出为什么主动开口():
    """流水账里该留的是「为什么想开口」，不是整段写作要求。"""
    why = gateway._proactive_task_body(REAL)
    assert why, "没提取到理由"
    assert "一段时间没有说话了" in why
    # 模板里的占位不该混进来
    assert "当前时间是" not in why
    assert "行动指南" not in why
    assert "最终指令" not in why


def test_提取不到理由时返回空串():
    assert gateway._proactive_task_body("[系统任务：主动对话]\n") == ""


def test_主动对话走系统渠道而不是对话渠道():
    """这条最要紧：写进去的必须是 `system_log`，不是 `qq_private`。

    `recent_turns()` 明确排除 `system_log`，所以她下一轮读上下文时
    不会把它当成「有人跟我说过这话」。
    """
    import inspect

    src = inspect.getsource(gateway._handle_proactive_task)
    assert "append_system_log" in src, (
        "主动对话必须走 append_system_log（系统操作），"
        "不能当普通对话写进 turns"
    )
    # 也不该自己往 turns 里插 user 行
    assert 'role="user"' not in src and "'user'" not in src, (
        "主动对话里不该出现 role=user 的写法"
    )


def test_流水账记的是固定的一句话():
    """主人 2026-11-09 定稿：流水账里写成「[系统] 主动发言：说点什么好呢…」。

    原来记的是从插件模板里抠出来的「我们好像已经有一段时间没有说话了…」，
    那是**插件塞给模型的写作理由**，不是那天实际发生的事。
    流水账该记「系统让她主动发一次言」，不该替她复述动机。
    """
    import inspect

    src = inspect.getsource(gateway._handle_proactive_task)
    assert "[系统] 主动发言：说点什么好呢…" in src, (
        "流水账那行必须是固定文案「[系统] 主动发言：说点什么好呢…」"
    )
    # 旧写法不能回来
    assert "主动开口（插件触发）" not in src, "旧文案又出现了"


def test_系统渠道默认不进对话历史():
    """`recent_turns` 必须排除 system_log —— 这是上面那条能成立的前提。"""
    import inspect

    src = inspect.getsource(gateway.recent_turns)
    assert "CHANNEL_SYSTEM_LOG" in src, (
        "recent_turns 必须明确排除 system_log，否则系统操作会进对话历史"
    )
