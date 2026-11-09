# -*- coding: utf-8 -*-
"""桌宠轮盘菜单「模型」页的静态检查（2026-11-09）。

为什么用静态检查而不是跑起来测：
这个项目的测试全是 Python 后端，没有 JS 运行环境。
而这次要防的是**结构性回归** —— 有人删掉「模型」入口、
或者把「暂停」写成了「隐藏」，那类错误用正则扫源码就能抓到，
而且跑得快、不依赖 Electron 启动。

真正的行为验证（暂停后 CPU 降多少）靠手工实测，
不在测试里做 —— 它需要起 Electron，不适合放进 CI。

主人的原话：
  「在模型轮盘菜单里新增一级菜单"模型"，里面新增二级菜单"暂停"，"隐藏"。
   可以暂停模型渲染，让她静止，或者隐藏模型，释放资源，
   点击状态栏"取消隐藏"就可以重新显示模型。」
"""
from __future__ import annotations

from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1]
PET_JS = DESKTOP / "web" / "pet.js"
MAIN_JS = DESKTOP / "desktop" / "main.js"
PRELOAD_JS = DESKTOP / "desktop" / "preload.js"


@pytest.fixture(scope="module")
def pet_src() -> str:
    return PET_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def main_src() -> str:
    return MAIN_JS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 菜单结构
# --------------------------------------------------------------------------- #
def test_一级菜单里有模型(pet_src):
    """root 里必须有「模型」这一项，且是子菜单。"""
    assert 'label: "模型", sub: "model"' in pet_src, (
        "一级菜单里找不到「模型」→ 二级的子菜单入口。"
        "它应该是 root.items 里的一项，形如 "
        '{ label: "模型", sub: "model" }'
    )


def test_模型页有暂停和隐藏(pet_src):
    """二级页必须同时有「暂停」和「隐藏」—— 两个都要，不能只留一个。

    它们解决的是不同问题：
      暂停 = 窗口还在、人看得见，只是不动了（省 61% 渲染 CPU）
      隐藏 = 整个窗口消失（省得更多，但也看不见人了）
    """
    # 截取 model 页那一段（从 sub: "model" 的定义处开始）
    start = pet_src.find('sub: "model"')
    assert start > 0, "找不到 model 页的定义"
    # 从定义往后找一段，覆盖整个 model 块
    block = pet_src[start:start + 1600]

    assert 'action: "pause"' in block, "model 页缺「暂停」"
    assert 'action: "hide"' in block, "model 页缺「隐藏」"
    assert 'sub: "root"' in block, "model 页缺「返回」"


def test_暂停按钮文案随状态变(pet_src):
    """暂停后按钮要变成「继续」，否则用户分不清当前状态。

    这要求 label 是个函数而不是静态字符串 —— 菜单每次渲染时求值。
    """
    start = pet_src.find('sub: "model"')
    block = pet_src[start:start + 1600]
    assert "() => (petPaused" in block or "()=> (petPaused" in block, (
        "「暂停」按钮的 label 应该是函数（按 petPaused 变文案），"
        "写成静态字符串的话，暂停之后就一直是「暂停」，没法继续。"
    )


def test_暂停有active标记(pet_src):
    """暂停中的那一项要能看出"当前生效"，靠 active() 打勾。"""
    start = pet_src.find('sub: "model"')
    block = pet_src[start:start + 1600]
    assert "active: () => petPaused" in block, (
        "「暂停」项应该有 active: () => petPaused，"
        "让轮盘能标出当前处于暂停状态"
    )


# --------------------------------------------------------------------------- #
# 实现要点
# --------------------------------------------------------------------------- #
def test_暂停是停ticker不是隐藏窗口(pet_src):
    """暂停必须调 `app.ticker.stop()`，而不是 `win.hide()`。

    这两个经常被混为一谈，但行为差很远：
      ticker.stop() -> 画面定住、人还在
      win.hide()    -> 窗口没了
    """
    assert "app.ticker.stop()" in pet_src, "暂停没有停 ticker"
    assert "app.ticker.start()" in pet_src, "恢复没有重启 ticker"


def test_恢复时重置帧时间避免物理跳变(pet_src):
    """恢复前要把 ticker 的 lastTime 重置。

    停了几分钟再 start，第一帧的 delta 会非常大，
    Live2D 的物理演算一步跳很远 —— 表现是「猛地抖一下」。
    """
    assert "ticker.lastTime = -1" in pet_src or "lastTime = -1" in pet_src, (
        "恢复时没有重置 ticker 的帧时间，物理会跳变"
    )


def test_页面启动时主动问暂停状态(pet_src, main_src):
    """页面重载后要主动问主进程当前是否暂停。

    不做这一步的话：主进程已经暂停、页面以为在跑，
    于是自己开始渲染 —— 状态就对不上了。
    """
    assert "getPaused" in pet_src, "pet.js 没有主动查暂停状态"
    assert "pet:get-paused" in main_src, "主进程没有处理 pet:get-paused"


def test_主进程是暂停状态的真源(main_src):
    """暂停状态存在主进程，因为托盘菜单也能触发它。

    如果只存在页面里，托盘点「暂停渲染」时页面收不到通知。
    """
    assert "let petPaused = false" in main_src, "主进程里没有 petPaused 状态"
    assert "function setPetPaused" in main_src, "主进程里没有 setPetPaused()"
    assert 'send("pet:paused"' in main_src or "pet:paused" in main_src, (
        "主进程没有把状态推给页面"
    )


def test_preload暴露了暂停通道():
    """preload 必须把 onPaused 和 getPaused 暴露给页面。"""
    src = PRELOAD_JS.read_text(encoding="utf-8")
    assert "onPaused" in src, "preload 没暴露 onPaused"
    assert "getPaused" in src, "preload 没暴露 getPaused"


# --------------------------------------------------------------------------- #
# 托盘入口（主人说的「状态栏取笑隐藏」）
# --------------------------------------------------------------------------- #
def test_托盘菜单有暂停勾选框(main_src):
    """托盘右键菜单里要有「暂停渲染」勾选框，checked 跟随 petPaused。

    主人原话：「点击状态栏"取消隐藏"就可以重新显示模型」——
    「状态栏」这里是托盘图标右键菜单。
    """
    assert 'label: "暂停渲染"' in main_src, "托盘菜单里没有「暂停渲染」"
    # checked 要跟着真实状态走，否则勾选状态会骗人
    idx = main_src.find('label: "暂停渲染"')
    block = main_src[idx:idx + 400]
    assert "checked: petPaused" in block, (
        "「暂停渲染」的 checked 没有绑定 petPaused，勾选状态会不准"
    )


def test_托盘菜单里暂停和显示是两个独立项(main_src):
    """「显示桌宠」和「暂停渲染」必须并存且互不影响。

    主人要求的是两种能力：暂停（还看得见）和隐藏（看不见）。
    如果把它们合成一项，就丢了一种。
    """
    assert 'label: "显示桌宠"' in main_src
    assert 'label: "暂停渲染"' in main_src
    # 两者都应该是 checkbox
    for label in ("显示桌宠", "暂停渲染"):
        idx = main_src.find('label: "%s"' % label)
        block = main_src[idx:idx + 300]
        assert 'type: "checkbox"' in block, "「%s」不是勾选框" % label
