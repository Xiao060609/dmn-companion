# -*- coding: utf-8 -*-
"""「谁负责主动说话」的唯一真源（2026-11-09）。

主人问的问题：
    插件有配置文件吗？能不能链接到同一个配置文件，
    然后在里面定义网关和插件的开启或关闭状态？

真正「两个程序读同一个文件」做不到 —— 它们各读各的，而且 AstrBot
会覆盖插件配置。所以走**启动时同步**：`proactive_speak.owner` 是唯一
真源，网关启动时把插件开关设成对应值。

为什么必须有它
--------------
原来两边各有一个开关：

    网关   proactive_speak.enabled
    插件   friend_settings.enable

改一个忘一个，就会**两边同时开、重复说话**。这不是理论风险：
11-09 那天两个开关就是同时开着的。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


# --------------------------------------------------------------------------- #
# owner 的读取
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("owner,expect", [
    ("gateway", "gateway"),
    ("plugin", "plugin"),
    ("none", "none"),
    ("GATEWAY", "gateway"),     # 大小写不敏感
    ("  plugin  ", "plugin"),   # 两边空格
])
def test_读得出合法的owner(monkeypatch, owner, expect):
    monkeypatch.setitem(system.SYSTEM, "proactive_speak",
                        {"enabled": True, "owner": owner})
    assert system.speak_owner() == expect


def test_非法owner退化成none(monkeypatch):
    """配置写错时宁可都不说，也不要两边同时说。"""
    monkeypatch.setitem(system.SYSTEM, "proactive_speak",
                        {"enabled": True, "owner": "两边都开"})
    assert system.speak_owner() == "none"


def test_没有owner字段时兼容旧配置(monkeypatch):
    """老配置文件没有 owner —— `enabled=true` 就按网关负责。"""
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": True})
    assert system.speak_owner() == "gateway"

    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": False})
    assert system.speak_owner() == "none"


def test_空配置也不崩(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {})
    assert system.speak_owner() == "none"


# --------------------------------------------------------------------------- #
# 同步给插件
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_plugin_cfg(tmp_path, monkeypatch):
    """造一份假的插件配置，别碰生产文件。"""
    path = tmp_path / "astrbot_plugin_proactive_chat_config.json"
    path.write_text(json.dumps({
        "friend_settings": {"enable": True, "session_list": ["x"], "keep": 1},
        "group_settings": {"enable": True, "other": "不动"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(system, "_plugin_speak_cfg_path", lambda: path)
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_owner是gateway就把插件关掉(fake_plugin_cfg):
    res = system.sync_plugin_speak("gateway")
    assert res["ok"] is True
    assert res["changed"] is True
    cfg = _read(fake_plugin_cfg)
    assert cfg["friend_settings"]["enable"] is False
    assert cfg["group_settings"]["enable"] is False


def test_owner是plugin就把插件打开(fake_plugin_cfg):
    system.sync_plugin_speak("gateway")   # 先关
    res = system.sync_plugin_speak("plugin")
    assert res["changed"] is True
    cfg = _read(fake_plugin_cfg)
    assert cfg["friend_settings"]["enable"] is True
    assert cfg["group_settings"]["enable"] is True


def test_owner是none也关插件(fake_plugin_cfg):
    """谁都不负责时，插件当然也不能开。"""
    system.sync_plugin_speak("none")
    cfg = _read(fake_plugin_cfg)
    assert cfg["friend_settings"]["enable"] is False


def test_同步不动插件的其它配置(fake_plugin_cfg):
    """插件配置几百行，只改 enable 那两个键，别的一律不碰。

    碰了的话会把人家配好的 prompt / 间隔 / 分段规则全弄丢。
    """
    before = _read(fake_plugin_cfg)
    system.sync_plugin_speak("gateway")
    after = _read(fake_plugin_cfg)

    assert after["friend_settings"]["session_list"] == \
        before["friend_settings"]["session_list"]
    assert after["friend_settings"]["keep"] == before["friend_settings"]["keep"]
    assert after["group_settings"]["other"] == before["group_settings"]["other"]
    # 只该有 enable 变了
    assert set(after["friend_settings"]) == set(before["friend_settings"])


def test_已经是目标状态就不重复写(fake_plugin_cfg):
    """幂等：第二次同步应该报 already，不改文件（省得每次启动都写盘）。"""
    system.sync_plugin_speak("gateway")
    mtime = fake_plugin_cfg.stat().st_mtime_ns
    res = system.sync_plugin_speak("gateway")
    assert res["ok"] is True
    assert res["changed"] is False
    assert res["reason"] == "already"
    assert fake_plugin_cfg.stat().st_mtime_ns == mtime


def test_插件配置不存在时不崩(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "_plugin_speak_cfg_path",
                        lambda: tmp_path / "nope.json")
    res = system.sync_plugin_speak("gateway")
    assert res["ok"] is False
    assert res["reason"] == "plugin_config_missing"


def test_插件配置是坏的json时也不崩(fake_plugin_cfg):
    fake_plugin_cfg.write_text("{ 这不是 json", encoding="utf-8")
    res = system.sync_plugin_speak("gateway")
    assert res["ok"] is False
    assert res["reason"].startswith("read_fail")


# --------------------------------------------------------------------------- #
# 闸门必须看 owner
# --------------------------------------------------------------------------- #
def test_不是gateway负责时网关不说话(monkeypatch):
    """插件负责时，网关绝不能插嘴 —— 否则就是两份声音。"""
    monkeypatch.setitem(system.SYSTEM, "proactive_speak",
                        {"enabled": True, "owner": "plugin"})
    st = {"last_speak_ts": 0, "speak_count_today": 0, "next_speak_ts": 0}
    assert system._allow_speak(st) is False


def test_owner是none时也不说话(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "proactive_speak",
                        {"enabled": True, "owner": "none"})
    st = {"last_speak_ts": 0, "speak_count_today": 0, "next_speak_ts": 0}
    assert system._allow_speak(st) is False


# --------------------------------------------------------------------------- #
# 启动时必须同步
# --------------------------------------------------------------------------- #
def test_启动时会同步插件开关():
    """不同步的话，改了 owner 要等下次重启才生效 ——
    而"忘记重启"正是两边同时开的直接原因。
    """
    import inspect

    src = inspect.getsource(system.start)
    assert "sync_plugin_speak" in src, "start() 必须调 sync_plugin_speak"
    assert "speak_owner" in src


# --------------------------------------------------------------------------- #
# 切换开关不能抹掉同段里的其它字段
# --------------------------------------------------------------------------- #
def test_切换开关不丢owner和间隔配置(tmp_path, monkeypatch):
    """后台点一下「主动说话」开关，不能把 `owner` / 间隔 / 安静时段抹掉。

    ⚠ 2026-11-09 真实事故：`apply_proactive_speak()` 里写的是
        sect["proactive_speak"] = {"enabled": enabled}
    —— 整个替换那一段，注释却写着「保留注释键和其余字段」。
    实测就这么把 owner / min_interval_minutes / max_interval_minutes /
    quiet_hours 全丢了，配置退回 {"enabled": false}。
    """
    import json

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "system": {
            "proactive_speak": {
                "owner": "gateway",
                "enabled": False,
                "min_interval_minutes": 30,
                "max_interval_minutes": 600,
                "quiet_hours": "1-7",
                "//owner": "注释键也要留着",
            }
        }
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setattr(system, "ROOT", str(tmp_path))
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", {"enabled": False})

    ok, _msg = system.apply_proactive_speak({"enabled": True})
    assert ok is True

    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    sect = after["system"]["proactive_speak"]

    assert sect["enabled"] is True, "开关没写进去"
    assert sect["owner"] == "gateway", "owner 被抹掉了"
    assert sect["min_interval_minutes"] == 30
    assert sect["max_interval_minutes"] == 600
    assert sect["quiet_hours"] == "1-7"
    assert sect["//owner"] == "注释键也要留着"
