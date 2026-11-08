"""钥匙串（多把 API key 的增/切/删）的单测。

需求：桌宠后台要能存多把 key、随时切换、删掉不用的。
设计上有两个点必须钉住，否则会出事：

1. **切换前要真试连**。不试就切过去，等于把一只废 key 设成在用，
   之后每条消息都 401 —— 用户只会看到「我这会儿有点卡」。
2. **明文只留在网关**。返回给浏览器的永远只有打码后的样子，
   否则后台页面一刷新就把 key 打到屏幕上。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))


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
    monkeypatch.setattr(gateway, "KEYS_PATH", str(data_dir / "keys.json"))
    monkeypatch.setattr(gateway, "AUDIT_PATH", str(data_dir / "audit.jsonl"))
    return gateway


@pytest.fixture()
def probe_ok(monkeypatch, gw):
    """默认让试连成功，个别用例自己改成失败。"""
    monkeypatch.setattr(gw, "_probe_role", lambda role: (True, "", 42))
    return gw


K1 = "sk-ws-AAAA1111"
K2 = "sk-ws-BBBB2222"


class TestAdd:
    def test_添加并设为选中(self, gw):
        ok, msg = gw._keys_action({"action": "add", "key": K1, "label": "主号"})
        assert ok
        store = gw._load_keys()
        assert len(store["keys"]) == 1
        assert store["active"] == store["keys"][0]["id"]
        assert store["keys"][0]["label"] == "主号"
        assert "主号" in msg

    def test_同一把加两次不会变两条(self, gw):
        gw._keys_action({"action": "add", "key": K1})
        ok, msg = gw._keys_action({"action": "add", "key": K1})
        assert ok and "已经在列表里" in msg
        assert len(gw._load_keys()["keys"]) == 1

    def test_空_key_被拒(self, gw):
        ok, msg = gw._keys_action({"action": "add", "key": "  "})
        assert not ok and "不能为空" in msg
        assert gw._load_keys()["keys"] == []

    def test_没填备注自动起名(self, gw):
        gw._keys_action({"action": "add", "key": K1})
        assert gw._load_keys()["keys"][0]["label"] == "Key 1"


class TestActivate:
    def test_切到另一把(self, probe_ok):
        gw = probe_ok
        gw._keys_action({"action": "add", "key": K1})
        gw._keys_action({"action": "add", "key": K2})
        first = gw._load_keys()["keys"][0]["id"]
        ok, msg = gw._keys_action({"action": "activate", "id": first})
        assert ok and "已切换" in msg
        assert gw._load_keys()["active"] == first

    def test_试连失败就不切(self, gw, monkeypatch):
        gw._keys_action({"action": "add", "key": K1})
        gw._keys_action({"action": "add", "key": K2})
        before = gw._load_keys()["active"]
        monkeypatch.setattr(gw, "_probe_role", lambda role: (False, "401 废 key", 0))
        ok, msg = gw._keys_action({"action": "activate", "id": gw._load_keys()["keys"][0]["id"]})
        assert not ok
        assert "连不上" in msg
        assert gw._load_keys()["active"] == before, "试连失败绝不能改选中项"

    def test_试连用的是这把的_key(self, gw, monkeypatch):
        """很容易写错：拿「当前选中」那把去验证「想切过去」的那把。"""
        gw._keys_action({"action": "add", "key": K1})
        gw._keys_action({"action": "add", "key": K2})
        target = next(k for k in gw._load_keys()["keys"] if k["key"] == K1)
        seen = {}

        def spy(role):
            seen["key"] = role.get("key")
            return True, "", 1

        monkeypatch.setattr(gw, "_probe_role", spy)
        gw._keys_action({"action": "activate", "id": target["id"]})
        assert seen["key"] == K1

    def test_不存在的_id(self, gw):
        ok, msg = gw._keys_action({"action": "activate", "id": "nope"})
        assert not ok and "没找到" in msg


class TestDelete:
    def test_删掉在用的会落到剩下第一把(self, gw):
        gw._keys_action({"action": "add", "key": K1})
        gw._keys_action({"action": "add", "key": K2})
        store = gw._load_keys()
        active = store["active"]
        ok, _ = gw._keys_action({"action": "delete", "id": active})
        assert ok
        after = gw._load_keys()
        assert len(after["keys"]) == 1
        assert after["active"] == after["keys"][0]["id"], "删掉在用的要自动落到剩下的那把"

    def test_删光后_active_清空(self, gw):
        gw._keys_action({"action": "add", "key": K1})
        gw._keys_action({"action": "delete", "id": gw._load_keys()["active"]})
        store = gw._load_keys()
        assert store["keys"] == []
        assert store["active"] == ""

    def test_删不存在的_id(self, gw):
        ok, msg = gw._keys_action({"action": "delete", "id": "nope"})
        assert not ok and "没找到" in msg


class TestPayload:
    def test_不泄露明文(self, gw):
        gw._keys_action({"action": "add", "key": K1, "label": "主号"})
        payload = gw._keys_payload()
        blob = json.dumps(payload, ensure_ascii=False)
        assert K1 not in blob, "明文 key 绝不能出现在返回给浏览器的数据里"
        assert payload["keys"][0]["masked"].endswith(K1[-4:])
        assert payload["keys"][0]["active"] is True

    def test_空钥匙串(self, gw):
        payload = gw._keys_payload()
        assert payload["keys"] == [] and payload["active"] == ""


class TestRoleKey:
    def test_显式指定的优先(self, gw, monkeypatch):
        """试连走的就是这条：显式 key 不能被「选中那把」盖掉。"""
        monkeypatch.setattr(gw, "_active_key", lambda: {"id": "x", "key": K2})
        assert gw._role_key({"key": K1}) == K1

    def test_没显式就用选中的(self, gw, monkeypatch):
        monkeypatch.setattr(gw, "_active_key", lambda: {"id": "x", "key": K2})
        assert gw._role_key({"key": ""}) == K2

    def test_钥匙串空时回落到_secrets(self, gw, monkeypatch):
        monkeypatch.setattr(gw, "_active_key", lambda: None)
        monkeypatch.setattr(gw, "_secret", lambda path, field: "sk-from-secret")
        assert gw._role_key({"key": "", "provider": "dashscope"}) == "sk-from-secret"

    def test_非_dashscope_且都没有就是空(self, gw, monkeypatch):
        monkeypatch.setattr(gw, "_active_key", lambda: None)
        assert gw._role_key({"key": "", "provider": "custom"}) == ""


class TestPersistence:
    def test_重启后还在(self, gw):
        gw._keys_action({"action": "add", "key": K1})
        # 直接读文件，模拟另一个进程
        data = json.loads(Path(gw.KEYS_PATH).read_text(encoding="utf-8"))
        assert data["keys"][0]["key"] == K1

    def test_不留临时文件(self, gw):
        gw._keys_action({"action": "add", "key": K1})
        leftovers = list(Path(gw.KEYS_PATH).parent.glob("keys.json.tmp"))
        assert leftovers == []

# --------------------------------------------------------------------------- #
# 写盘健壮性（2026-11-04 回归）
# --------------------------------------------------------------------------- #
def test_save_keys_能覆盖已收紧ACL的文件(gw):
    """写第二次不能因为 ACL 不一致而失败。

    踩过的坑：`_save_keys` 是「写 .tmp → os.replace」，而目标文件被
    `_harden_file_acl` 收紧过（只有当前用户）。`.tmp` 是新文件、继承的是
    **父目录**的 ACL（Authenticated Users 可写），于是 `os.replace` 在 Windows 上
    报 `PermissionError: [WinError 5] 拒绝访问`。

    后果很重：**改钥匙串会静默失败**（第一次写成功、之后每次都炸），
    表现是"后台点了保存但 key 没进去"。

    修法是先收紧 tmp 再 replace。这条测试连写三次，确保它一直能成功。
    """
    import gateway

    store = gateway._load_keys()
    for i in range(3):
        gateway._save_keys(store)
        again = gateway._load_keys()
        assert again.get("active") == store.get("active"), f"第 {i+1} 次写后 active 变了"
        assert len(again.get("keys") or []) == len(store.get("keys") or [])


def test_save_keys_内容完整不丢(gw):
    """三次写之后，key 的**内容**必须一字不差（不是只有条数对）。"""
    import gateway

    store = gateway._load_keys()
    before = [k.get("key") for k in (store.get("keys") or [])]
    gateway._save_keys(store)
    gateway._save_keys(gateway._load_keys())
    after = [k.get("key") for k in (gateway._load_keys().get("keys") or [])]
    assert before == after

# --------------------------------------------------------------------------- #
# ACL 必须带删除权限（2026-11-04 第十轮）
# --------------------------------------------------------------------------- #
def test_收紧后的文件能被删掉(gw, tmp_path):
    """`_harden_file_acl` 必须给当前用户 **D（删除）** 权限。

    踩过的坑：一开始只给 `(R,W)`，结果：
      * `keys.json.tmp` 写完后**删不掉**，永久残留；
      * 而且因为 ACL 缺 D，连 `Remove-Item -Force` / `os.remove` 都被拒绝
        —— 只有先 `icacls /grant:r ...:F` 拿回权限才行。
    这个坑很隐蔽：不影响功能（文件能读能写），只会在 data/ 里堆垃圾，
    而垃圾里含**明文 API key**（`.tmp` 是 keys.json 的副本）。
    """
    import os

    import gateway

    if os.name != "nt":
        pytest.skip("ACL 行为只在 Windows 上有意义")

    p = tmp_path / "acl_probe.txt"
    p.write_text("probe", encoding="utf-8")
    ok = gateway._harden_file_acl(str(p))
    if not ok:
        pytest.skip("icacls 不可用")

    # 收紧后必须还能删掉自己刚创建的文件
    try:
        os.remove(str(p))
    except PermissionError as exc:
        pytest.fail(f"收紧 ACL 后文件删不掉（缺 D 权限）：{exc}")
    assert not p.exists()


# --------------------------------------------------------------------------- #
# 后台改模型必须同步 config.json（2026-11-05 修主人报的 BUG）
# --------------------------------------------------------------------------- #
def test_保存系统模型会同步config_json(tmp_path, monkeypatch):
    """`_save_system_cfg` 必须把 base/model 回写 config.json 的 system 段。

    BUG 表现（主人 2026-11-05 报的）：
        后台「系统模型」卡片只写 `data/system_model.json`，
        `config.json` 的 system 段永远停在旧值。
        实测：config.json 里还是 `dashscope + deepseek-v4.1-flash-1017`，
        而实际在跑的是 `apihub.agnes-ai.com + agnes-3.0-flash`。

    危害：看 config.json 的人（审计脚本、排障时的我）会以为模型还是旧的。
    **两个真源说法不一致，迟早踩坑。**
    """
    import json
    from pathlib import Path

    import system

    # 造一个隔离的 config.json + data 目录
    cfg = {
        "port": 8898,
        "endpoint": {"base": "http://old/v1", "model": "old-model", "timeout_s": 30},
        "system": {
            "base": "http://old-sys/v1",
            "model": "old-sys-model",
            "key": "",
            "heartbeat_s": 300,
            "//一个注释键": "说明文字",
        },
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setattr(system, "ROOT", str(tmp_path))
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(system, "SYSTEM_MODEL_PATH", str(tmp_path / "data" / "system_model.json"))

    system._save_system_cfg({
        "base": "https://apihub.agnes-ai.com/v1",
        "model": "agnes-3.0-flash",
        "key": "sk-must-not-leak",
        "timeout_s": 30,
        "temperature": 0.2,
        "max_tokens": 300,
    })

    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    sect = after["system"]
    assert sect["base"] == "https://apihub.agnes-ai.com/v1", "base 应该同步到 config.json"
    assert sect["model"] == "agnes-3.0-flash", "model 应该同步到 config.json"
    # 其余键必须原样保留
    assert sect["heartbeat_s"] == 300, "心跳节律字段不该被改动"
    assert sect["//一个注释键"] == "说明文字", "注释键必须保留"


def test_保存系统模型不许把key写进config_json(tmp_path, monkeypatch):
    """`key` 绝不能写进 `config.json`。

    因为 `gateway/config.json` **在 git 里**（`data/` 被 gitignore）。
    写进去 = 下次 commit 就把明文 API key 提交了。

    这是个安全边界，不是风格问题。
    """
    import json

    import system

    cfg = {"system": {"base": "", "model": "", "key": "", "heartbeat_s": 300}}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(system, "ROOT", str(tmp_path))
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(system, "SYSTEM_MODEL_PATH", str(tmp_path / "data" / "system_model.json"))

    real_key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    system._save_system_cfg({
        "base": "https://x/v1", "model": "m", "key": real_key,
        "timeout_s": 30, "temperature": 0.2, "max_tokens": 300,
    })

    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert real_key not in json.dumps(after), (
        "API key 泄漏进 config.json 了 —— 这个文件在 git 里，会被提交！"
    )


def test_保存角色模型会同步config_json(tmp_path, monkeypatch):
    """`_save_role` 必须把 base/model/timeout_s 回写 config.json 的 endpoint 段。

    和系统层同一个病：后台只写 `data/role_model.json`，
    `config.json` 的 endpoint 段停在旧值
    （实测是 `127.0.0.1:1234 / gemma-4-e4b`，而实际在用 dashscope）。
    """
    import json

    import gateway

    cfg = {
        "port": 8898,
        "endpoint": {"base": "http://127.0.0.1:1234/v1", "model": "gemma-4-e4b", "timeout_s": 30},
        "system": {"heartbeat_s": 300},
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setattr(gateway, "ROOT", str(tmp_path))
    monkeypatch.setattr(gateway, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(gateway, "ROLE_PATH", str(tmp_path / "data" / "role_model.json"))

    gateway._save_role({
        "mode": "cloud", "provider": "dashscope",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "deepseek-v4.1-flash-1017",
        "key": "", "timeout_s": 120,
    })

    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    ep = after["endpoint"]
    assert ep["base"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert ep["model"] == "deepseek-v4.1-flash-1017"
    assert ep["timeout_s"] == 120
    assert "key" not in ep, "endpoint 段不该出现 key 字段"
    # 其它段不动
    assert after["system"]["heartbeat_s"] == 300


def test_保存角色配置是原子写(tmp_path, monkeypatch):
    """`_save_role` 必须是原子写（tmp + os.replace）。

    原来是直接 `open(ROLE_PATH, "w")` —— 写一半断电就把配置文件弄坏了，
    下次启动读到半个 json，角色层直接用默认值，用户会以为"配置丢了"。
    """
    import json

    import gateway

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"endpoint": {}, "system": {}}), encoding="utf-8")
    monkeypatch.setattr(gateway, "ROOT", str(tmp_path))
    monkeypatch.setattr(gateway, "DATA_DIR", str(tmp_path / "data"))
    role_path = tmp_path / "data" / "role_model.json"
    monkeypatch.setattr(gateway, "ROLE_PATH", str(role_path))

    gateway._save_role({"base": "b", "model": "m", "timeout_s": 30})

    assert role_path.exists()
    assert not (tmp_path / "data" / "role_model.json.tmp").exists(), (
        "临时文件应该被 os.replace 消费掉，不该残留"
    )
    assert json.loads(role_path.read_text(encoding="utf-8"))["model"] == "m"


# --------------------------------------------------------------------------- #
# P0-1（2026-11-07 全量审计）：磁盘里的 key 不许压过钥匙串
#
# 现场：生产 `data/role_model.json` 里躺着一个测试哨兵 `sk-<占位串>`，
# 而 `_role_key()` 的优先级①是 `role["key"]` → **钥匙串里 5 把真 key
# 全部被绕过**，角色层每一轮都 401。
#
# 这道闸的形状和本轮其他几道一样，**必须双向验**：
#   ① 磁盘来的 key 不可信（闸关住了）；
#   ② 试连时显式传入的 key 仍然优先（闸没误伤正常路径）。
# --------------------------------------------------------------------------- #
class Test磁盘上的key不参与选key:
    def test_落盘的key被剥离(self, gw, tmp_path, monkeypatch):
        """`role_model.json` 里有 key → `_load_role()` 读出来是空串。"""
        role_path = tmp_path / "role_model.json"
        role_path.write_text(
            json.dumps({
                "mode": "cloud", "provider": "dashscope",
                "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "deepseek-v4.1-flash-1017",
                "key": "sk-SHOULD-NOT-WRITE",
                "timeout_s": 120,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(gw, "ROLE_PATH", str(role_path))

        role = gw._load_role()
        assert role.get("key") == "", "磁盘上的 key 必须被剥离"
        # 除 key 外的字段一个都不能少（闸不能顺手把正常配置也吃掉）
        assert role["mode"] == "cloud"
        assert role["provider"] == "dashscope"
        assert role["model"] == "deepseek-v4.1-flash-1017"
        assert role["timeout_s"] == 120

    def test_正常磁盘配置照旧生效(self, gw, tmp_path, monkeypatch):
        """**正常路径仍通**：没有 key 字段的文件必须照常整份读进来。

        这是防"闸误伤"的那一条 —— 修 P0-1 时最容易顺手把 `_load_role`
        写成"只认白名单字段"，那以后新增字段就会被静默丢掉。
        """
        role_path = tmp_path / "role_model.json"
        role_path.write_text(
            json.dumps({
                "mode": "cloud", "provider": "custom",
                "base": "https://example.com/v1", "model": "some-model",
                "timemout_typo_field": 42, "timeout_s": 77,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(gw, "ROLE_PATH", str(role_path))

        role = gw._load_role()
        assert role["base"] == "https://example.com/v1"
        assert role["model"] == "some-model"
        assert role["timeout_s"] == 77
        # 未知字段也要保留：将来加字段不该被这一版代码吃掉
        assert role["timemout_typo_field"] == 42

    def test_哨兵key不再压过钥匙串(self, gw, tmp_path, monkeypatch):
        """端到端：磁盘写哨兵 + 钥匙串有真 key → `_role_key(ROLE)` 拿到真 key。"""
        role_path = tmp_path / "role_model.json"
        role_path.write_text(
            json.dumps({"provider": "dashscope", "base": "https://x/v1",
                        "model": "m", "key": "sk-SHOULD-NOT-WRITE"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(gw, "ROLE_PATH", str(role_path))
        monkeypatch.setattr(gw, "ROLE", gw._load_role())
        monkeypatch.setattr(gw, "_active_key", lambda: {"id": "x", "key": K1})

        assert gw._role_key(gw.ROLE) == K1, "钥匙串里的真 key 必须生效"

    def test_试连时显式传入的key仍然优先(self, gw, monkeypatch):
        """**正常路径仍通**：`_role_key({"key": 新key})` 必须还是新 key。

        `_probe_role` 靠这一条验"刚填进来的那把 key 能不能用"。
        如果为了修 P0-1 把优先级①整个删掉，试连就会拿旧 key 去验新 key ——
        验了个寂寞（`_role_key` docstring 里写的就是这个理由）。
        """
        monkeypatch.setattr(gw, "_active_key", lambda: {"id": "x", "key": K1})
        assert gw._role_key({"key": "sk-brand-new-9999"}) == "sk-brand-new-9999"

    def test_试连拿到的确实是显式那把(self, gw, monkeypatch):
        """`_apply_role` 那条路：试连用的 key 应该是调用方填的，不是钥匙串的。"""
        seen = {}

        def fake_probe(role):
            seen["key"] = role.get("key")
            return True, "", 12

        monkeypatch.setattr(gw, "_probe_role", fake_probe)
        monkeypatch.setattr(gw, "_active_key", lambda: {"id": "x", "key": K1})
        monkeypatch.setattr(gw, "_save_role", lambda role: None)
        monkeypatch.setattr(gw, "_upsert_key", lambda key, label="": {})

        ok, msg = gw._apply_role({
            "mode": "cloud", "provider": "custom",
            "base": "https://example.com/v1", "model": "m",
            "key": "sk-new-7777",
        })
        assert ok, msg
        assert seen["key"] == "sk-new-7777", "试连必须用用户刚填的那把"

    def test_系统层同样不吃磁盘上的key(self, tmp_path, monkeypatch):
        """系统层同一个病：`system_model.json` 的 key 也不能压过钥匙串/密钥文件。"""
        import system

        model_path = tmp_path / "system_model.json"
        model_path.write_text(
            json.dumps({"base": "https://y/v1", "model": "sys-model",
                        "key": "sk-TESTSECRET-should-not-be-written",
                        "timeout_s": 30}),
            encoding="utf-8",
        )
        monkeypatch.setattr(system, "SYSTEM_MODEL_PATH", str(model_path))

        cfg = system._load_system_cfg()
        assert cfg.get("key") in (None, ""), (
            "system_model.json 里持久化的 key 不该被 load 进来"
        )
        # 正常字段照旧
        assert cfg["base"] == "https://y/v1"
        assert cfg["model"] == "sys-model"
