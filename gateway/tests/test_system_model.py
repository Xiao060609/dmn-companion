"""系统模型自定义（后台「系统模型」卡片）的单测。

背景：后台原来只能改「对话模型」（角色层），系统层用哪个模型只能改文件。
现在补一张对称的卡：选服务商 / 填地址 / 填模型名 / 填 key，保存前先试连。

这里钉住四件事：
1. **key 留空要沿用现在实际在用的那把** —— 不能只看 `SYSTEM["key"]`；
2. 试连失败**不动**当前配置（和角色层那张卡同一约定）；
3. 只改身份类字段，心跳节律那些不能被动到；
4. payload 里 key 只回掩码，绝不回明文。
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

FAKE_BASE = "https://fake.example.com/v1"


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """把系统层配置指到临时文件，别动真实的 system_model.json。

    ⚠ 2026-11-05：**还必须把 ROOT 也指到 tmp_path**。

    原因：`_save_system_cfg()` 从这一轮起会**同步回写 `config.json`**
    （修「后台改模型不同步 config.json」那个 BUG）。
    它算路径用的是 `os.path.join(ROOT, "config.json")` —— 只替换
    `DATA_DIR` / `SYSTEM_MODEL_PATH` 的话，回写仍然落到**真实的**
    `gateway/config.json`，跑一遍测试就把仓库里的配置改成
    `fake.example.com / m4` 了（实测踩到，git diff 里能看到）。

    所以这里造一份临时 config.json，让回写有地方去。
    """
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(system, "SYSTEM_MODEL_PATH", str(tmp_path / "system_model.json"))
    # ↓ 关键：回写 config.json 也要落在临时目录
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"port": 8898,
             "endpoint": {"base": "", "model": "", "timeout_s": 30},
             "system": {"base": "", "model": "", "key": "", "heartbeat_s": 300}},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system, "ROOT", str(tmp_path))
    # 深拷贝一份 SYSTEM，用例里改它不影响别的用例
    monkeypatch.setattr(system, "SYSTEM", dict(system.SYSTEM))
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. key 留空要沿用「现在实际在用的那把」
# --------------------------------------------------------------------------- #
def test_key留空沿用_system_key而不是_SYSTEM_key(isolated, monkeypatch):
    """这是实测踩到的坑。

    `SYSTEM["key"]` 默认是**空字符串**，真正的 key 由 `_system_key()`
    从 secrets.json / 环境变量回落。只读 `SYSTEM["key"]` 的话，
    用户不改 key 点一次保存，配置就被写成"无 key"，然后试连 401。
    """
    monkeypatch.setitem(system.SYSTEM, "key", "")          # 模拟真实默认状态
    monkeypatch.setattr(system, "_system_key", lambda: "sk-real-key")
    captured = {}

    def fake_probe(candidate):
        captured.update(candidate)
        return True, "", 100

    monkeypatch.setattr(system, "_probe_system", fake_probe)

    ok, msg = system.apply_system_model(
        {"provider": "dashscope", "base": FAKE_BASE, "model": "m1", "key": ""}
    )
    assert ok, msg
    assert captured["key"] == "sk-real-key", "key 留空时必须沿用现在在用的那把"


def test_显式传key时用传的(isolated, monkeypatch):
    monkeypatch.setattr(system, "_system_key", lambda: "sk-old")
    captured = {}

    def fake_probe(candidate):
        captured.update(candidate)
        return True, "", 100

    monkeypatch.setattr(system, "_probe_system", fake_probe)
    ok, _ = system.apply_system_model(
        {"provider": "custom", "base": FAKE_BASE, "model": "m2", "key": "sk-new"}
    )
    assert ok
    assert captured["key"] == "sk-new"


# --------------------------------------------------------------------------- #
# 2. 试连失败不动配置
# --------------------------------------------------------------------------- #
def test_试连失败不落盘也不改内存(isolated, monkeypatch):
    before = dict(system.SYSTEM)
    monkeypatch.setattr(
        system, "_probe_system", lambda c: (False, "HTTP 401 bad key", 0)
    )
    ok, msg = system.apply_system_model(
        {"provider": "custom", "base": FAKE_BASE, "model": "bad", "key": "x"}
    )
    assert ok is False
    assert "没连上" in msg
    assert system.SYSTEM.get("model") == before.get("model"), "失败不许改当前配置"
    assert not (isolated / "system_model.json").exists(), "失败不该落盘"


def test_地址或模型为空直接拒(isolated):
    ok, msg = system.apply_system_model({"provider": "custom", "base": "", "model": ""})
    assert ok is False and "服务地址" in msg


# --------------------------------------------------------------------------- #
# 3. 只改身份类字段
# --------------------------------------------------------------------------- #
def test_切换不动心跳节律(isolated, monkeypatch):
    monkeypatch.setattr(system, "_probe_system", lambda c: (True, "", 10))
    monkeypatch.setitem(system.SYSTEM, "heartbeat_s", 300)
    monkeypatch.setitem(system.SYSTEM, "cooldown_s", 1800)

    ok, _ = system.apply_system_model(
        {"provider": "custom", "base": FAKE_BASE, "model": "m3", "key": "k"}
    )
    assert ok
    assert system.SYSTEM["heartbeat_s"] == 300, "心跳节律不该被模型切换动到"
    assert system.SYSTEM["cooldown_s"] == 1800


def test_切换后写进system_model_json(isolated, monkeypatch):
    import json

    monkeypatch.setattr(system, "_probe_system", lambda c: (True, "", 10))
    ok, _ = system.apply_system_model(
        {"provider": "custom", "base": FAKE_BASE, "model": "m4", "key": "k4"}
    )
    assert ok
    saved = json.loads((isolated / "system_model.json").read_text(encoding="utf-8"))
    assert saved["model"] == "m4"
    assert saved["base"] == FAKE_BASE
    assert saved["key"] == "k4"


# --------------------------------------------------------------------------- #
# 4. payload 不回明文
# --------------------------------------------------------------------------- #
def test_payload里key只回掩码(isolated, monkeypatch):
    monkeypatch.setattr(system, "_system_key", lambda: "sk-abcdef1234567890")
    p = system.system_model_payload()
    assert p["key_set"] is True
    assert "sk-abcdef1234567890" not in str(p), "绝不能回明文 key"
    assert p["key_masked"].endswith("7890")


def test_payload带服务商清单(isolated):
    p = system.system_model_payload()
    assert "dashscope" in p["providers"]
    assert "custom" in p["providers"]
    # 自定义项地址留空（让用户自己填）
    assert p["presets"]["custom"] == ""
