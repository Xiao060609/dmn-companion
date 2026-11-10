"""对话网关（dm.family/1 最小实现）。

一个模型：读人格、决定要不要用工具、把结果说成人话。
工具在 tools.py，边界（路径、扩展名、写删确认）由代码守，不信模型。

本文件负责：记忆、提示词装配、工具循环、回复清洗、审计。
启动：python gateway.py   →  http://127.0.0.1:8898
"""
from __future__ import annotations

import hashlib
import hmac
import collections
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import persona_sync
import tools
import migrate_channel
import system
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))

# 控制台编码：Windows 默认 GBK，中文/emoji 会崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as _fh:
    CFG = json.load(_fh)

# 环境变量优先：换机器/换模型时不用改文件
CFG["host"] = os.environ.get("GATEWAY_HOST", CFG["host"])
CFG["port"] = int(os.environ.get("GATEWAY_PORT", CFG["port"]))
CFG["endpoint"]["base"] = os.environ.get("GATEWAY_MODEL_BASE", CFG["endpoint"]["base"])
CFG["endpoint"]["model"] = os.environ.get("GATEWAY_MODEL", CFG["endpoint"]["model"])

with open(os.path.join(ROOT, "persona.md"), encoding="utf-8") as _fh:
    PERSONA = _fh.read().strip()


def current_persona() -> str:
    """这一轮该用的人格（**名字已代入**）。

    顺手把 `persona.md` 和 AstrBot `personas` 表里那份对齐（见 persona_sync）：
    项目里有两份人格，而 WebUI 里那份对说话其实没有影响 —— 用户很可能去那边改，
    改完发现没反应。同步之后两头看到的是同一份，改哪边都算数。

    ⚠ 2026-11-08：人格文件里**不写死名字**，用 `{name}` 占位，
    在这里代入后台「身份」卡片里配置的名字。
    原因：光把标头/正则改成新名字是不够的 —— persona.md 里仍写着
    「我是小菲鱼」，她就还会这么自称，和标头对不上。实测改名成「示例用户」
    之后人格第一行还是「我是小菲鱼」。
    """
    global PERSONA
    PERSONA = persona_sync.sync_persona(PERSONA)
    return _fill_persona_name(PERSONA)


def _fill_persona_name(text: str) -> str:
    """把人格里的 `{name}` 换成配置的名字。

    只替换 `{name}` 这一个占位符，别的 `{...}` 一律不动 ——
    人格里有 `{calm}`、`{happy}` 这类情绪标记，是给下游解析用的，
    不能误伤。所以不用 `format()`（它遇到未知键会抛 KeyError），
    直接做字符串替换最安全。
    """
    if not text:
        return text
    # PET_NAME 定义在文件更下面（模块级常量），这里用 globals() 取：
    # 直接写 `PET_NAME` 在函数里是合法的（运行时才求值），但如果在
    # 模块导入早期被调用（比如某个测试）就会 NameError。取一次兜底更稳。
    name = globals().get("PET_NAME") or _DEFAULT_PET_NAME
    # 万一名字本身就是 "{name}" 会造成死循环，先挡掉
    if "{" in name and "}" in name:
        name = _DEFAULT_PET_NAME
    return text.replace("{name}", name)

# 数据目录可外置（放到可写盘 / 同步盘），默认在服务目录下
DATA_DIR = os.environ.get("GATEWAY_DATA_DIR", os.path.join(ROOT, "data"))
DB_PATH = os.path.join(DATA_DIR, "gateway.db")
AUDIT_PATH = os.path.join(DATA_DIR, "audit.jsonl")
ROLE_PATH = os.path.join(DATA_DIR, "role_model.json")
TOKEN_PATH = os.path.join(DATA_DIR, "gateway.token")
IDENTITY_PATH = os.path.join(DATA_DIR, "identity.json")
#: 是否把配置回写 config.json。**测试应设为 False**：config.json 在 git 里，
#: 被测试改写很难发现（2026-11-08 实际发生过：把测试用的 123456/阿鱼 写了进去）。
WRITE_CONFIG_MIRROR = True


def config_path() -> str:
    """config.json 的路径。

    ⚠ 这里刻意用**函数**而不是模块级常量，目的是让测试能通过
    `monkeypatch.setattr(gateway, "ROOT", tmp_path)` 把整个配置目录搬走 ——
    那是这套测试一直在用的约定（见 test_key_store、test_plugin_context）。
    如果写成 `CONFIG_PATH = os.path.join(ROOT, "config.json")`，常量在
    import 时就固化了，后面对 ROOT 的 monkeypatch 就不再生效，
    表现为「测完往真 config.json 里写」或「改了 ROOT 却没生效」。
    """
    return os.path.join(ROOT, "config.json")

#: 名字的兜底值。只在配置完全没写时用，**不是**真源。
_DEFAULT_PET_NAME = "小菲鱼"
_DEFAULT_PET_ALIASES = ("小鱼",)
_PET_NAME_MAX = 20
_OWNER_ID_MAX = 20
_OWNER_IDS_MAX_COUNT = 10


def _load_identity() -> dict:
    """读「身份」配置：主人 QQ 号 + 桌宠名字。

    真源优先级（和 role_model / system_model 同一套约定）：
        data/identity.json  >  config.json 的 identity 段  >  内置兜底

    为什么要有单独的 data/identity.json：后台卡片写它，config.json 在 git 里、
    不该被运行时改动。但 config.json 仍要能读到旧值，否则换台机器跑
    会突然「认不出主人」。
    """
    ident = {"owner_ids": [], "pet_name": "", "pet_aliases": []}

    # 1) config.json 里可能早有 owner_ids（历史位置在 dialog 段）
    #
    # ⚠ 读**磁盘上的当前内容**，不用模块级的 `CFG` 快照。
    # `CFG` 是 import 时读的一次，之后 config.json 再变它都不知道 ——
    # 用快照的后果是「后台保存了、文件也确实改了，但读回来还是旧值」，
    # 而且这种不一致只在重启后才消失，极难排查。
    disk_cfg = CFG
    try:
        with open(config_path(), encoding="utf-8") as fh:
            disk_cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass

    cfg_ids = disk_cfg.get("dialog", {}).get("owner_ids") or []
    ident["owner_ids"] = [str(x).strip() for x in cfg_ids if str(x).strip()]
    sect = disk_cfg.get("identity")
    if isinstance(sect, dict):
        if sect.get("owner_ids"):
            ident["owner_ids"] = [
                str(x).strip() for x in sect["owner_ids"] if str(x).strip()
            ]
        if sect.get("pet_name"):
            ident["pet_name"] = str(sect["pet_name"]).strip()
        if sect.get("pet_aliases"):
            ident["pet_aliases"] = [
                str(x).strip() for x in sect["pet_aliases"] if str(x).strip()
            ]

    # 2) data/identity.json 覆盖
    try:
        with open(IDENTITY_PATH, encoding="utf-8") as fh:
            live = json.load(fh)
        if isinstance(live, dict):
            if live.get("owner_ids"):
                ident["owner_ids"] = [
                    str(x).strip() for x in live["owner_ids"] if str(x).strip()
                ]
            if live.get("pet_name"):
                ident["pet_name"] = str(live["pet_name"]).strip()
            if live.get("pet_aliases"):
                ident["pet_aliases"] = [
                    str(x).strip() for x in live["pet_aliases"] if str(x).strip()
                ]
    except (OSError, json.JSONDecodeError):
        pass

    if not ident["pet_name"]:
        ident["pet_name"] = _DEFAULT_PET_NAME
    if not ident["pet_aliases"]:
        ident["pet_aliases"] = list(_DEFAULT_PET_ALIASES)
    return ident


def _save_identity(ident: dict) -> None:
    """原子写 `data/identity.json`，并把非敏感字段回写 config.json。

    名字和号都不敏感（它们不是凭据），所以允许回写 —— 这样看 config.json
    的人不会看到和实际不一致的旧名。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = IDENTITY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ident, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, IDENTITY_PATH)
    if WRITE_CONFIG_MIRROR:
        _sync_config_identity_section(ident)


def _sync_config_identity_section(ident: dict) -> None:
    """把身份字段回写 `config.json` 的 identity 段（只改这几个键）。"""
    cfg_path = config_path()
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            disk = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[config] 读 {cfg_path} 失败，跳过身份同步：{exc}")
        return

    sect = disk.get("identity")
    if not isinstance(sect, dict):
        sect = {}
        disk["identity"] = sect

    changed = []
    for key, value in (
        ("owner_ids", list(ident.get("owner_ids") or [])),
        ("pet_name", ident.get("pet_name") or ""),
        ("pet_aliases", list(ident.get("pet_aliases") or [])),
    ):
        if sect.get(key) != value:
            sect[key] = value
            changed.append(key)
    if not changed:
        return

    tmp = cfg_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(disk, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, cfg_path)
        print(f"[config] 已同步 config.json 的 identity 段：{', '.join(changed)}")
    except OSError as exc:
        print(f"[config] 回写 {cfg_path} 失败（配置已生效，只是镜像没跟上）：{exc}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def _clean_owner_ids(values) -> list[str]:
    """规整主人 QQ 号：去空白、只留数字、去重、限量。

    **只留数字**是刻意的：这些号唯一的用途是跟 uid 比对，而 uid 一定是数字。
    允许字母进来只会让"看起来配了但永远不命中"。
    """
    out: list[str] = []
    for raw in values or []:
        digits = re.sub(r"\D", "", str(raw))
        if not digits or len(digits) > _OWNER_ID_MAX:
            continue
        if digits not in out:
            out.append(digits)
        if len(out) >= _OWNER_IDS_MAX_COUNT:
            break
    return out


def _clean_pet_name(name: str) -> str:
    """桌宠名字：去空白、限长、不允许控制字符。

    名字会被拼进提示词和标头，允许换行/控制字符会把提示词结构搞乱。
    """
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    return text[:_PET_NAME_MAX]


def identity_payload() -> dict:
    """给后台「身份」卡片用：当前值 + 约束（前端据此做校验提示）。"""
    ident = _load_identity()
    return {
        "owner_ids": list(ident["owner_ids"]),
        "pet_name": ident["pet_name"],
        "pet_aliases": list(ident["pet_aliases"]),
        "max_owner_ids": _OWNER_IDS_MAX_COUNT,
        "max_id_len": _OWNER_ID_MAX,
        "max_name_len": _PET_NAME_MAX,
    }


def apply_identity(payload: dict) -> tuple[bool, str]:
    """校验 → 落盘 → 热生效。返回 (成功, 一句话说明)。

    `pet_aliases` 可以不传：不传就保留现有的。
    `owner_ids` 允许为空 —— 那表示"谁都不是主人"，是一个合法选择
    （等于关掉主人特权），不能因为空就拒绝保存。
    """
    global OWNER_IDS, OWNER_LABEL, SELF_NAME_RE, PET_NAME, PET_ALIASES

    name = _clean_pet_name(payload.get("pet_name", ""))
    if not name:
        return False, "桌宠名字不能为空"

    ids = _clean_owner_ids(payload.get("owner_ids"))
    raw_count = len([x for x in (payload.get("owner_ids") or []) if str(x).strip()])
    if raw_count and not ids:
        return False, "主人 QQ 号只能填数字"

    current = _load_identity()
    if "pet_aliases" in payload:
        aliases = [_clean_pet_name(a) for a in (payload.get("pet_aliases") or [])]
        aliases = [a for a in aliases if a and a != name]
    else:
        aliases = [a for a in current["pet_aliases"] if a != name]

    ident = {"owner_ids": ids, "pet_name": name, "pet_aliases": aliases}
    try:
        _save_identity(ident)
    except OSError as exc:
        return False, f"没存下来：{exc}"

    # 记一笔流水账（2026-11-08）：改了就记，不做筛选。
    # 哪条值得提要不要说，交给系统层判断。
    _log_identity_change(current, ident)

    # 热生效：立刻换掉进程内的常量，不用重启
    OWNER_IDS = set(ids)
    OWNER_LABEL = "主人"
    PET_NAME = name
    PET_ALIASES = list(aliases)
    SELF_NAME_RE = _build_self_name_re(name, aliases)

    bits = [f"名字改成了「{name}」"]
    if ids:
        bits.append(f"主人数 {len(ids)}")
    else:
        bits.append("没有配置主人（谁都不算主人）")
    return True, "，".join(bits)


def _log_identity_change(old: dict, new: dict) -> None:
    """改了就记一笔流水账，**不做判断**。

    为什么不写 `turns`（对话记录）：那是"主人和她真的聊过什么"的账本。
    把「改了个设置」塞进去，她会当成一段真实对话记进记忆，
    之后回忆起来会以为自己提过改名 —— 那是假记忆。

    写进 `system_audit.jsonl`，系统层自然会读到。
    **哪条重要、要不要提，由系统层判断** —— 这里只负责记下来，
    不筛、不合并、不算"变了没"。
    """
    try:
        system._audit({           # noqa: SLF001 —— 复用系统层审计，同一份文件
            "event": "identity_update",
            "ok": True,
            "pet_name": str(new.get("pet_name") or ""),
            "pet_name_prev": str(old.get("pet_name") or ""),
            "owner_ids": [str(x) for x in (new.get("owner_ids") or [])],
            "owner_ids_prev": [str(x) for x in (old.get("owner_ids") or [])],
        })
    except Exception:             # noqa: BLE001 —— 审计失败不能让保存失败
        pass


def _build_self_name_re(name: str, aliases) -> "re.Pattern[str]":
    """模型偶尔会在句首自报家门（「小菲鱼：能啊…」），那是旁白不是话。

    这个名字来自配置而不是写死 —— 改了名字之后，旧名字的自报家门也该能剥掉，
    所以别名一并进正则。
    """
    names = [n for n in [name, *(aliases or [])] if n]
    if not names:
        return re.compile(r"^\s*$")      # 永不匹配
    alt = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    return re.compile(r"^\s*(?:" + alt + r")\s*[:：]\s*")


def _load_or_create_token() -> str:
    """桌宠这条路的共享密钥。

    以前 `/chat` 什么都不校验，还在路由里写死 local_pet=True —— 任何本机进程
    （或一个网页）发两句「把 X 写进 y.md」「好」，就能让主人名下的写操作落地。
    现在只有带着这个 token 的调用才算本机桌宠。

    优先级：环境变量 > data/gateway.token（没有就生成一个）。
    文件在 data/ 下，已在 .gitignore 里，不进库。
    """
    env = os.environ.get("GATEWAY_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            token = fh.read().strip()
        if len(token) >= 16:
            return token
    except OSError:
        pass
    token = secrets.token_hex(24)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
        fh.write(token)
    _harden_file_acl(TOKEN_PATH)
    return token


#: 已经收紧过 ACL 的文件（`_harden_file_acl` 的幂等短路用）。
#: 进程内有效即可 —— 重启后重新收一次是对的（文件可能被外部改过）。
#:
#: ⚠ 必须定义在 `_harden_file_acl` **之前**：那个函数在模块导入期就会被
#: `_load_or_create_token()` 调到，此时后面定义的全局变量还不存在 → NameError。
_ACL_HARDENED: set[str] = set()


def _harden_file_acl(path: str) -> bool:
    """把文件的 Windows ACL 收紧到「只有当前用户 + SYSTEM + Administrators」。

    ⚠ 2026-11-04（P2-6）：原来的 `os.chmod(path, 0o600)` 在 Windows 上**是空操作**
    —— NTFS 权限不来自 POSIX mode 位，`chmod` 只会改只读标志。
    实测 `data/gateway.token` 的真实 ACL 是：

        NT AUTHORITY\\Authenticated Users:(I)(M)   ← 任何本机用户可写
        BUILTIN\\Users:(I)(RX)                     ← 任何本机用户可读

    也就是说 token / keys 里那 5 把真 key（`keys.json` 是同一份 ACL）
    **本机任何账户都能读**。这台机器上只有一个人用，风险有限；
    但"注释里写着 0600、实际人人可读"是个会误导人的谎言，得修。

    做法：用 `icacls` 移除继承并只授予当前用户。**失败就静默返回 False** ——
    收紧权限不该拦住启动（没有它照样能跑，只是回到原状态）。

    性能：4 次 `icacls` 约 80ms。只在"创建/写入密钥文件"时调（不是热路径），
    可以接受；但**加了幂等短路**：已经收紧过的文件直接返回，省掉重复调用。
    """
    if not os.path.exists(path):
        return False
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False
    if path in _ACL_HARDENED:
        return True
    try:
        import getpass
        import subprocess

        user = os.environ.get("USERNAME") or getpass.getuser()
        if not user:
            return False
        cmds = [
            ["icacls", path, "/inheritance:r"],
            # ⚠ 必须带 `D`（删除权限）。只给 `R,W` 时文件能读能写**但删不掉**，
            # `.tmp` 会永久残留 —— 实测 `keys.json.tmp` 残留过一次，
            # 而且因为 ACL 缺 D，连 `Remove-Item -Force` 都被拒绝。
            ["icacls", path, "/grant:r", f"{user}:(R,W,D)"],
            ["icacls", path, "/grant:r", "SYSTEM:(F)"],
            ["icacls", path, "/grant:r", "Administrators:(F)"],
        ]
        for cmd in cmds:
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            if r.returncode != 0:
                return False
        _ACL_HARDENED.add(path)
        return True
    except Exception:  # noqa: BLE001 —— 收紧失败不该拦住启动
        return False


PET_TOKEN = _load_or_create_token()

# `/v1/chat/completions` 要不要也强制 token（2026-11-04 T-08）。
#
# 默认关：AstrBot 的 provider 目前没配 `custom_headers`，一上来就强制会让 QQ 直接哑掉。
# 打开方式（两选一，推荐第二种）：
#   ① 环境变量 `GATEWAY_V1_REQUIRE_TOKEN=1`；
#   ② AstrBot WebUI 里给 `dashscope-deepseek-41-flash` 那个 provider 加
#      `custom_headers: {"X-Gateway-Token": "<gateway/data/gateway.token 的内容>"}`，
#      然后回来把这里改成 True —— 那时才是**闭环**。
#
# 为什么即使关着也要留着这段：本机来源 + 强制 JSON 这两道已经挡住了
# 跨站与远程，token 是补上"本机其它进程也不许白嫖"的最后一块。
_V1_REQUIRE_TOKEN = (os.environ.get("GATEWAY_V1_REQUIRE_TOKEN", "") or "").strip() not in (
    "",
    "0",
    "false",
    "False",
)

# 服务商预设：后台选一个就能填好地址
PROVIDERS = {
    "local": {"label": "本地（LM Studio）", "base": CFG["endpoint"]["base"]},
    "dashscope": {
        "label": "阿里云百炼",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com/v1"},
    "openai": {"label": "OpenAI", "base": "https://api.openai.com/v1"},
    "custom": {"label": "自定义（OpenAI 兼容）", "base": ""},
}


def _load_role() -> dict:
    """角色层模型配置。存在 data/role_model.json 就以它为准，否则用 config.json。

    ⚠ 2026-11-07（P0-1）：**磁盘上持久化的 `key` 一律丢弃，不参与选 key。**

    为什么（这是本轮审计挖出的阻断项，别改回去）：

        `_role_key()` 的优先级①是 `role["key"]`，而 `role_model.json` 是
        **磁盘文件**，谁都能往里写一行。生产上实测躺着一个测试哨兵
        `sk-<占位串>`，于是优先级①命中、钥匙串里 5 把真 key 全部被绕过，
        角色层每一轮都拿那个假 key 去请求 → **启动即全线 401**。

    这条优先级本来只为「兼容手改文件」存在，而正常代码路径**永远不会**
    产出非空 key：`_apply_role()` 落盘时明确写 `"key": ""`（key 统一由
    钥匙串管，避免两个真源打架，见 `_apply_role` 里那段注释）。

    所以判据不是「这个 key 长得像不像占位串」（那种黑名单迟早漏），
    而是**来源**：文件里来的 key 一律不可信，运行时显式传进来的才算。

    那些显式传入的用途**完全保留**：
      * `_role_key({"key": K})` —— 试连时验一把刚填进来的新 key；
      * `_probe_role()` 里 `explicit_key = role.get("key")` —— T-02 修的
        「换地址时绝不回落真 key」靠的就是它。
    区别在于：这些调用点传的是**内存里现拼的 dict**，不是读到盘上的 ROLE。
    """
    default = {
        "mode": "local",
        "provider": "local",
        "base": CFG["endpoint"]["base"],
        "model": CFG["endpoint"]["model"],
        "key": "",
        "timeout_s": CFG["endpoint"]["timeout_s"],
    }
    if os.path.exists(ROLE_PATH):
        try:
            with open(ROLE_PATH, encoding="utf-8") as fh:
                saved = json.load(fh)
            stray = str((saved or {}).get("key") or "").strip() if isinstance(saved, dict) else ""
            if stray:
                # 只报"有这么个东西、已忽略"，不打内容 —— 它可能是真 key，
                # 日志会进 audit/错误报告，明文 key 不能落在那儿。
                print(
                    f"[role] 忽略 {ROLE_PATH} 里持久化的 key（长度 {len(stray)}）："
                    "角色层 key 只认「钥匙串」，文件里的值不参与选 key。"
                )
            default.update(
                {
                    k: v
                    for k, v in saved.items()
                    if v is not None and k != "key"
                }
            )
        except (OSError, json.JSONDecodeError):
            pass
    return default


#: 回写 config.json 时**必须排除**的字段（config.json 在 git 里）。
_ROLE_SECRET_KEYS = ("key",)


def _save_role(role: dict) -> None:
    """把角色层模型配置写进 `data/role_model.json`（原子写）。

    **同时把非敏感字段回写 `config.json` 的 `endpoint` 段。**

    为什么（2026-11-05 修主人报的 BUG，和系统层同一个病）：
        后台卡片只写 `data/role_model.json`，`config.json` 的 `endpoint` 段
        永远停在旧值（实测还是 `127.0.0.1:1234 / gemma-4-e4b`，
        而实际在跑的是 `dashscope / deepseek-v4.1-flash-1017`）。
        两个真源说法不一致，看 config.json 的人会判断错。

    ⚠ `key` **不回写**：config.json 在 git 里。
       角色层的 key 本来就走「钥匙串」（`_upsert_key`），config.json 里恒为空。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    # 原子写：原来直接 open(ROLE_PATH,"w")，写一半断电会把配置文件弄坏
    tmp = ROLE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(role, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, ROLE_PATH)

    _sync_config_endpoint_section(role)


def _sync_config_endpoint_section(role: dict) -> None:
    """把角色层身份类字段回写 `config.json` 的 `endpoint` 段。

    `endpoint` 段只有 base / model / timeout_s 三个键（没有注释键），
    但仍然**只改这三个**，不整段覆盖 —— 免得以后加字段被莫名清掉。
    """
    cfg_path = config_path()
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            disk = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[config] 读 {cfg_path} 失败，跳过同步：{exc}")
        return

    sect = disk.get("endpoint")
    if not isinstance(sect, dict):
        print("[config] config.json 没有 endpoint 段，跳过同步")
        return

    changed = []
    for k in ("base", "model", "timeout_s"):
        if k in _ROLE_SECRET_KEYS:        # 防御性：以后若加进 key 也不会漏
            continue
        v = role.get(k)
        if v is None:
            continue
        if sect.get(k) != v:
            sect[k] = v
            changed.append(k)
    if not changed:
        return

    tmp = cfg_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(disk, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, cfg_path)
        print(f"[config] 已同步 config.json 的 endpoint 段：{', '.join(changed)}")
    except OSError as exc:
        print(f"[config] 回写 {cfg_path} 失败（配置已生效，只是镜像没跟上）：{exc}")
        try:
            os.remove(tmp)
        except OSError:
            pass


ROLE = _load_role()


def _secret(path: str, field: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return str(json.load(fh).get(field, "")).strip()
    except (OSError, json.JSONDecodeError):
        return ""


def _role_key(role: dict) -> str:
    """云端角色模型用哪把 key。

    优先级（顺序不能反）：
        ① role["key"] —— 显式指定的那把。**试连**必须走这条，否则会拿选中的
           那把去验证一把刚填进来的新 key，验了个寂寞。
        ② 后台「钥匙串」里选中的那把。
        ③ 环境变量 / secrets.json —— 老配置的兜底，保证升级后还能用。
    """
    if role.get("key"):
        return str(role["key"])
    active = _active_key()
    if active and active.get("key"):
        return str(active["key"])
    if role.get("provider") == "dashscope":
        return os.environ.get("DASHSCOPE_API_KEY", "") or _secret(
            os.path.join(ROOT, "secrets.json"), "dashscope_api_key"
        )
    return ""


# ---------------------------------------------------------------- 钥匙串
# 可以存多把 key、随时切换。存在 data/keys.json（和记忆库同目录，已在 .gitignore）。
# 只在这里放明文；给界面的永远只有打码后的样子。
KEYS_PATH = os.path.join(DATA_DIR, "keys.json")


def _load_keys() -> dict:
    try:
        with open(KEYS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("keys"), list):
            return {"active": str(data.get("active") or ""), "keys": data["keys"]}
    except (OSError, json.JSONDecodeError):
        pass
    return {"active": "", "keys": []}


def _save_keys(store: dict) -> None:
    """原子写钥匙串（含明文 key）。

    ⚠ 2026-11-04 修（P2-6 的回归）：先写 `.tmp` 再 `os.replace` 的写法，
    在**目标文件 ACL 已被收紧**之后会失败：

        PermissionError: [WinError 5] 拒绝访问
            'keys.json.tmp' -> 'keys.json'

    原因：`.tmp` 是新文件，继承的是**父目录**的 ACL（`Authenticated Users:(M)`），
    而 `keys.json` 已经被收紧成「只有当前用户」。`os.replace` 在 Windows 上
    对"源与目标 ACL 不一致"的原子替换会报 WinError 5。

    修法：**先写 tmp、先收紧 tmp 的 ACL、再 replace**。这样两边 ACL 一致，
    替换就能成功；而且不会出现"刚写完还没收紧"的窗口。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = KEYS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    # 关键顺序：先收紧 tmp，再 replace
    _harden_file_acl(tmp)
    try:
        os.replace(tmp, KEYS_PATH)
    except PermissionError:
        # 万一还是被拒（比如文件被别的进程打开）：退化成"直接写目标"，
        # 至少不让钥匙串的修改丢掉。写入后立刻收紧权限。
        try:
            with open(KEYS_PATH, "w", encoding="utf-8") as fh:
                json.dump(store, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            _harden_file_acl(KEYS_PATH)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return
    _harden_file_acl(KEYS_PATH)


def _mask_key(key: str) -> str:
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "…"
    return key[:6] + "…" + key[-4:]


def _key_id(key: str) -> str:
    """用内容算 id：同一把 key 加两次不会出现两条。"""
    return "k" + hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:10]


def _active_key() -> dict | None:
    store = _load_keys()
    active = store.get("active") or ""
    return next((k for k in store["keys"] if k.get("id") == active), None)


def _upsert_key(key: str, label: str = "") -> dict:
    """把一把 key 收进钥匙串并设为选中。已存在就只切换。"""
    key = str(key or "").strip()
    store = _load_keys()
    items = store["keys"]
    kid = _key_id(key)
    item = next((k for k in items if k.get("id") == kid), None)
    if item is None:
        item = {
            "id": kid,
            "label": (label or "").strip()[:30] or ("Key %d" % (len(items) + 1)),
            "key": key,
            "added_at": time.time(),
        }
        items.append(item)
    elif label.strip():
        item["label"] = label.strip()[:30]
    store["active"] = kid
    _save_keys(store)
    return item


def _keys_payload() -> dict:
    store = _load_keys()
    active = store.get("active") or ""
    return {
        "active": active,
        "keys": [
            {
                "id": k.get("id"),
                "label": k.get("label") or "",
                "masked": _mask_key(k.get("key")),
                "active": k.get("id") == active,
            }
            for k in store["keys"]
        ],
    }


def _keys_action(payload: dict) -> tuple[bool, str]:
    """钥匙串的增 / 切 / 删。切换前会真试连一次，不切到废 key 上。"""
    action = str(payload.get("action") or "").strip()
    store = _load_keys()
    items = store["keys"]

    if action == "add":
        key = str(payload.get("key") or "").strip()
        if not key:
            return False, "key 不能为空"
        existed = any(k.get("id") == _key_id(key) for k in items)
        item = _upsert_key(key, str(payload.get("label") or ""))
        return True, (
            f"这把已经在列表里，已切换到「{item['label']}」"
            if existed
            else f"已添加并切换到「{item['label']}」"
        )

    if action == "activate":
        target = next((k for k in items if k.get("id") == str(payload.get("id") or "")), None)
        if not target:
            return False, "没找到这把 key"
        ok, err, ms = _probe_role(dict(ROLE, key=str(target.get("key") or "")))
        if not ok:
            return False, f"这把 key 连不上：{err}"
        store["active"] = target["id"]
        _save_keys(store)
        return True, f"已切换到「{target.get('label') or '未命名'}」，试连 {ms} ms"

    if action == "delete":
        target = next((k for k in items if k.get("id") == str(payload.get("id") or "")), None)
        if not target:
            return False, "没找到这把 key"
        items.remove(target)
        if store.get("active") == target["id"]:
            # 删掉的正好是在用的那把：自动落到剩下第一把，没有就清空
            store["active"] = items[0]["id"] if items else ""
        _save_keys(store)
        return True, f"已删除「{target.get('label') or '未命名'}」"

    if action == "probe":
        # 单独试一把还没存的 key：只连、不存、不切。测通了用户自己再添加。
        key = str(payload.get("key") or "").strip()
        if not key:
            return False, "key 不能为空"
        ok, err, ms = _probe_role(dict(ROLE, key=key))
        if ok:
            return True, f"可用，试连 {ms} ms"
        return False, f"不可用：{err}"

    if action == "test":
        # 只测不改：拿当前服务商和模型去试连一次，不切换、不动 active
        target = next((k for k in items if k.get("id") == str(payload.get("id") or "")), None)
        if not target:
            return False, "没找到这把 key"
        label = target.get("label") or "未命名"
        ok, err, ms = _probe_role(dict(ROLE, key=str(target.get("key") or "")))
        if ok:
            return True, f"「{label}」可用，试连 {ms} ms"
        return False, f"「{label}」不可用：{err}"

    return False, "不认识的操作"

# 情绪词表。angry 是后加的：模型有「生气」这个表情，但她嘴硬、会不耐烦，
# 之前没有这个词就只能一律 calm，表情和贴纸永远不动。
_EMO = "happy|sad|shy|calm|surprised|confused|love|angry"
EMOTIONS = {"happy", "sad", "shy", "calm", "surprised", "confused", "love", "angry"}
TAG_RE = re.compile(r"\{(" + _EMO + r")\}")
# 模型经常漏掉花括号、还把情绪词塞在句子中间，所以按裸词全局匹配
BARE_WORD_RE = re.compile(r"(" + _EMO + r")", re.I)
STICKER_RE = re.compile(r"\[贴纸[:：]?\s*([^\]\s]{1,6})\]")
# 模型偶尔会在句首自报家门（「小菲鱼：能啊…」），那是旁白不是话。
# ⚠ 2026-11-08：名字改成从配置读（后台「身份」卡片可改），
# 所以这个正则由 `_build_self_name_re()` 构造，不写死在源码里。
# 这里直接读一次 `_load_identity()`：它在文件更靠上的位置就已经定义了，
# 不能在模块级引用 `PET_NAME`（那个常量定义在下面，会 NameError）。
_self_ident = _load_identity()
SELF_NAME_RE = _build_self_name_re(
    _self_ident.get("pet_name") or _DEFAULT_PET_NAME,
    _self_ident.get("pet_aliases") or [],
)
# 贴纸由情绪决定，不信模型自造的词（实测会给「疲惫」「平静」之类）
STICKER_BY_EMOTION = {
    "happy": "开心",
    "sad": "难过",
    "shy": "害羞",
    "calm": "平静",
    "surprised": "惊讶",
    "confused": "疑惑",
    "love": "爱心",
    "angry": "生气",
}
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2b50\u2728\u2764\ufe0f]"
)
BAD_PUNCT_RE = re.compile(r"[“”\"'‘’（）()【】\[\]《》—–~～·…]")
SENT_SPLIT = re.compile(r"(?<=[。！？!?])")

# 「怎么样地说」：模型自己填的语气描述，原样交给语音模型当 instruction。
# 情绪标签只有七个值，说不清「哽咽着、气息不稳」这种，所以另开一个自由字段。
# 形状是 {语气:哽咽着说，声音发抖}，冒号全半角都认。
TONE_RE = re.compile(r"\{语气\s*[:：]\s*([^{}\n]{1,80})\}")
# 这几个词描述的是「谁在念」而不是「怎么念」，进到克隆模式会把音色带跑，直接切掉
TONE_BAD_WORDS = ("男声", "女声", "童声", "男音", "女音", "少女音", "大叔音", "萝莉音",
                  "音色", "声线", "老人", "小孩", "年纪", "苍老", "稚嫩", "年轻", "中年")

# 审批确认：只认这几个短词，别的都当取消
CONFIRM_RE = re.compile(
    r"^\s*(好|好啊|好呀|可以|行|嗯|对|是的|确认|删|删吧|删掉|写|写吧|执行|干吧|ok)\s*[。！!，,]?\s*$",
    re.I,
)
# 模型最多连着调几轮工具；再多说明它绕圈子了，直接收口。
#
# 2026-11-07 修（审计 G.2 第 2 条 / 板块 I 第 4 条）
# ------------------------------------------------
# 原来这里写死 4，而 `config.json` 里有一个 `system.max_tool_rounds: 2` 的
# 配置项 —— **那个配置从来没生效过**，谁改它都没用。两个问题叠在一起：
#
#   ① **放错了段**：`system.*` 是**系统层**（`system.py` 的巩固/梳理/体检）的
#      配置段，而 `MAX_TOOL_ROUNDS` 是**角色层**（`handle_chat`）的工具循环。
#      角色层的旋钮一律在 `dialog.*`（`max_sentences` / `history_turns` 都在那）。
#      所以不是"把 system.max_tool_rounds 读进来"就完事 —— 那会让以后看配置的人
#      继续以为它是系统层的。已把它挪到 `dialog.max_tool_rounds`。
#   ② **值也是错的**：配置里写 2。而下面这段注释（以及它背后的实测）说的是
#      "3 轮经常刚好用完、一句答案都没说" —— 真按 2 生效会**悄悄砍掉一轮**，
#      用户看到的表现是"她查东西查到一半就放弃了"，而且没有任何报错。
#      所以挪段时把值改成 4（= 原来硬编码的值，行为零变化），不是沿用 2。
#
# 为什么选「接上」而不是「删掉配置项」：这个旋钮是有真实用途的 ——
# 换成本地小模型时工具调用质量差、爱绕圈，调小它能省钱省延迟；换成强模型时
# 调大它能让多步搜索走完。删掉配置等于把这个能力锁死在代码里。
#
# 取值一律夹在 [1, 8]：0 会让一轮工具都不走（模型只能"说"不能"做"，
# `PRETEND_RE` 那段兜底也会失效），过大则单轮对话能烧掉一整个请求预算。
_DEFAULT_MAX_TOOL_ROUNDS = 4


def _max_tool_rounds() -> int:
    """从 `config.json` 的 `dialog.max_tool_rounds` 读工具轮数；坏值回退默认。

    Returns:
        [1, 8] 之间的整数。**永远不抛** —— 配置写坏了不该让整个网关起不来。
    """
    try:
        raw = (CFG.get("dialog") or {}).get("max_tool_rounds", _DEFAULT_MAX_TOOL_ROUNDS)
        value = int(raw)
    except (TypeError, ValueError, AttributeError):
        return _DEFAULT_MAX_TOOL_ROUNDS
    return max(1, min(8, value))


MAX_TOOL_ROUNDS = _max_tool_rounds()
# 待审批会话多久失效。挂太久的话，主人过一会儿随口一句「好」就会误执行
PENDING_TTL = 300
# 光说不做的信号：「我提交了」「等系统问你确认」——这类话却没有工具调用
PRETEND_RE = re.compile(r"(提交了|等.{0,4}确认|已经删|已经写|已删掉|已写好|写好了|写完了|删好了|正在处理)")


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # WAL + busy_timeout（2026-11-04 P1-5）
        #
        # 为什么：网关是 ThreadingHTTPServer（每请求一线程）+ 三个后台线程，
        # 而 `journal_mode` 之前是默认的 delete —— 读和写互斥，并发 `add_turn`
        # 会拿到 "database is locked" → 500 → **这一句记忆就丢了**。
        # WAL 让读不阻塞写；busy_timeout 让偶发争用等一下而不是立刻报错。
        #
        # ⚠ WAL 是**持久化到库文件**的设置（一次设置，后续所有连接都生效），
        # 所以只在这里开一次就够，不用改那 11 处 `sqlite3.connect`。
        # busy_timeout 是**每连接**的，所以另外给 `_connect_turns()` 兜了一层；
        # 这里设的是本连接自己的值，顺手也校验一下 WAL 是否真的生效。
        try:
            mode = conn.execute("pragma journal_mode=wal").fetchone()
            got = str((mode or [""])[0]).lower()
            if got != "wal":
                print(f"  [warn] journal_mode 没切成 wal（当前 {got}）——并发写可能报 locked")
        except sqlite3.Error as exc:
            print(f"  [warn] 开 WAL 失败（不影响启动）: {type(exc).__name__}: {exc}")
        try:
            conn.execute("pragma busy_timeout=5000")
        except sqlite3.Error:
            pass
        conn.execute(
            """create table if not exists turns(
                   id integer primary key autoincrement,
                   session text not null,
                   role text not null,
                   content text not null,
                   path text,
                   ts real not null,
                   channel text)"""
        )
        conn.execute(
            """create table if not exists pending(
                   session text primary key,
                   action text not null,
                   args text not null,
                   ts real not null,
                   origin text)"""
        )
        # 结构化事件表（DMN 阶段 1）：让记忆从「对话原文」变成「可推理的事实」。
        # 和 turns 并列、不替代它；turns 照旧全写，事件是额外一层。
        conn.execute(
            """create table if not exists events(
                   id integer primary key autoincrement,
                   ts real not null,
                   kind text not null,
                   subject text,
                   payload text,
                   status text,
                   due_ts real,
                   attempts integer default 0,
                   channel text)"""
        )
        # 定时任务表（DMN 阶段 3）：deferred_care 这类「两天后再试」的延后意图，
        # 到点由心跳捞起转成内心OS。没到点的任务不产生任何模型调用。
        conn.execute(
            """create table if not exists schedules(
                   id integer primary key autoincrement,
                   due_ts real not null,
                   kind text,
                   event_id integer,
                   note text,
                   fired integer default 0)"""
        )
        conn.commit()
        # 老库没有 origin 列：就地加（幂等）。origin 记「谁挂起的」，
        # 只有同一来源的那句话才能确认，否则 A 的待办能被 B 顶掉。
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(pending)")}
        if "origin" not in cols:
            conn.execute("ALTER TABLE pending ADD COLUMN origin TEXT")
            conn.commit()
    # 老库没有 channel 列：就地加列 + 回填（幂等，重复启动不会重复加列）。
    # 迁移逻辑在 migrate_channel.py，可以拿数据库副本离线先跑一遍验证。
    try:
        stats = migrate_channel.migrate(DB_PATH)
        if stats.get("added_column") or stats.get("from_prefix") or stats.get("inherited"):
            print(
                f"  turns.channel 迁移: 加列={stats['added_column']} "
                f"按前缀回填={stats['from_prefix']} 继承回填={stats['inherited']} "
                f"保留 NULL={stats['left_null']}/{stats['total']}"
            )
    except (sqlite3.Error, OSError) as exc:  # noqa: BLE001
        # 迁移失败不该拦住网关启动：旧行为（按 path 过滤）仍然可用
        print(f"  [warn] turns.channel 迁移失败，按旧逻辑继续: {type(exc).__name__}: {exc}")


def _connect_rw() -> sqlite3.Connection:
    """打开一个**写**连接，带上 busy_timeout。

    2026-11-04（P1-5）：`journal_mode=wal` 已经在 `init_db()` 里设成持久化属性，
    但 `busy_timeout` 是**每连接**的 —— 不设的话默认 5 秒都没有（实际是 0），
    一撞上写锁立刻 `database is locked`。写热点统一走这个函数。
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        conn.execute("pragma busy_timeout=5000")
    except sqlite3.Error:
        pass
    return conn


def add_turn(
    session: str, role: str, content: str, path: str = "", channel: str = ""
) -> None:
    """写一条记忆。

    Args:
        session: 记忆槽。
        role: user / assistant。
        content: 正文（可能带 `[群聊·xx] 昵称: ` 前缀）。
        path: chat / tool / approval / approve。
        channel: 来源键（`pet` / `qq_private` / `qq_group:<群名>`）。
            H4 之后来源隔离靠这一列，不再靠内容前缀，也不再靠 path。
    """
    with _connect_rw() as conn:
        conn.execute(
            "insert into turns(session, role, content, path, channel, ts)"
            " values(?,?,?,?,?,?)",
            (session, role, content, path, channel, time.time()),
        )
        conn.commit()


def recent_turns(
    session: str,
    limit: int,
    hide_private: bool = False,
    channel: str = "",
    local_pet: bool = False,
    source: str | None = None,
) -> list[dict]:
    """取最近若干轮。

    Args:
        session: 记忆槽，现在只有 main。
        limit: 取多少条。
        hide_private: 说话人不是主人时置真，连主人的工具调用往返也一起滤掉。
        channel: 当前这一轮的来源**标签**（`群聊·示例群` / `私聊` / 空）。
        local_pet: 这一轮是不是本机桌宠发起的。**必须显式传**，不能靠
            「channel 为空」推断——群聊里群名没取到时标签也是空串。
        source: 显式只读某一个来源键（如 `pet`）。给了它就直接按来源等值
            匹配、跳过下面基于「场合」的过滤分支；None 时行为完全不变。
            供系统层读记忆用：只读主人/桌宠来源，绝不读主人私聊。

    来源隔离是 **fail-closed** 的：

    ==========================  ==========================================
    场合                        看得到什么
    ==========================  ==========================================
    本机桌宠 (pet)              全部
    主人在一对一私聊里          全部（原有设计意图，主人自己的空间）
    任何人 + 群聊               只有本群
    陌生人 + 私聊               只有 qq_private
    来源键认不出来              什么都看不到
    ==========================  ==========================================

    `channel is null` 的老行**不放行** —— 等值匹配天然不命中。H4 第一版
    放行过，结果真实库里群聊上下文的 96.6% 字符是私聊内容（含一条 9605 字符
    的私聊全量转写）。

    Returns:
        按时间正序的 `{"role", "content"}` 列表。
    """
    sql = "select role, path, content from turns where session=?"
    params: list = [session]
    if hide_private:
        sql += " and coalesce(path,'') not in ('tool','approval','approve')"
    # internal = 插件注入的「任务说明」，正文是整段对话的转写，不是聊天。
    # 任何上下文都不该看到它，所以无条件排除（连桌宠也不例外）。
    #
    # system_log = 系统层自己的操作流水账（`[系统] 计划触发：…`）。它**必须**
    # 让系统层在巩固时看到（那是主人定的核心机制），但它**不是对话**：
    # role 是 "system"，塞进对话历史会让模型以为有人这么说话。
    # 两条渠道的可见性正好相反，所以这里必须一起排除。
    sql += " and coalesce(channel,'') not in (?,?)"
    params.extend([migrate_channel.CHANNEL_INTERNAL, migrate_channel.CHANNEL_SYSTEM_LOG])
    if source is not None:
        # 显式只读某一个来源键：精确等值，跳过下面基于「场合」的过滤分支。
        # 系统层用它来只读主人/桌宠来源（pet），不碰主人私聊。None 时不变。
        sql += " and channel = ?"
        params.append(source)
        sql += " order by id desc limit ?"
        params.append(limit)
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"role": r, "content": c} for r, _p, c in reversed(rows)]
    key = migrate_channel.channel_key(channel, local=local_pet)
    # 来源过滤：fail-closed。三条任一命中就按来源**精确匹配**（等值，不是 like）：
    #   ① 群聊场合 —— 群是广播场合，不管谁在说话都只放行本群。
    #      主人在群里说话时 hide_private 是 False，光靠 ② 拦不住；
    #      实测群聊来源的 86 行里有 38 行是主人自己说的，这是主路径不是边角。
    #   ② 说话人不是主人（hide_private）—— 陌生人在私聊里也读不到主人的
    #      群聊、桌宠、以及别的来源。
    #   ③ 来源键认不出来 —— 认不出就什么都别给。
    # 只有「本机桌宠」和「主人在一对一私聊里」保留全量上下文（原有设计意图）。
    #
    # 注意这里**没有** `or channel is null`：判不出来源的老行永远不等于任何
    # 被请求的 key，天然被排除。H4 v1 就是因为放行了 NULL，导致真实库里
    # 群聊上下文 96.6% 的字符是私聊（含一条 9605 字的私聊全量转写）。
    need_filter = (
        migrate_channel.is_group_channel(key)
        or hide_private
        or not migrate_channel.is_known_source(key)
    )
    if need_filter:
        sql += " and channel = ?"
        params.append(key)
    sql += " order by id desc limit ?"
    params.append(limit)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"role": r, "content": c} for r, _p, c in reversed(rows)]


def save_pending(session: str, action: str, args: dict, origin: str = "") -> bool:
    """挂起一个待审批动作，顺手清掉过期的。

    返回 False = 这个会话已经有一件没确认的，**不覆盖**。
    以前是「先 delete 再 insert」，于是主人对 A 说的「好」会落地成 B：
    中间任何一条请求都能把待办换掉，事后审计只看到「主人确认了这次写入」。
    这不是可用性问题，是内容被换。
    """
    cutoff = time.time() - PENDING_TTL
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("delete from pending where ts<?", (cutoff,))
        busy = conn.execute(
            "select 1 from pending where session=? and ts>=?", (session, cutoff)
        ).fetchone()
        if busy:
            return False
        conn.execute(
            "insert into pending(session, action, args, ts, origin) values(?,?,?,?,?)",
            (session, action, json.dumps(args, ensure_ascii=False), time.time(), origin),
        )
        conn.commit()
    return True


def peek_pending(session: str, origin: str | None = None) -> dict | None:
    """只看不清。过期的当作不存在；给了 origin 就只认同一来源挂起的那件。"""
    cutoff = time.time() - PENDING_TTL
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "select action, args, origin from pending where session=? and ts>=?",
            (session, cutoff),
        ).fetchone()
    if not row:
        return None
    action, args, stored = row
    # 迁移前挂起的行 origin 为空：放行（5 分钟内自然过期），
    # 免得升级那一刻卡着一件永远确认不了的待办。
    if origin is not None and stored and stored != origin:
        return None
    return {"action": action, "args": json.loads(args)}


def take_pending(session: str, origin: str | None = None) -> dict | None:
    """取出并清除待审批动作（一次性，不重复问）。"""
    row = peek_pending(session, origin)
    if row is None:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("delete from pending where session=?", (session,))
        conn.commit()
    return row


# 401/403 这类是「密钥不认」，不是网络抖动：重试没用，必须说人话。
# 以前它们和超时一起被吞成「我这会儿有点卡」，用户根本想不到是 key 过期。
AUTH_ERROR_RE = re.compile(r"\b40[13]\b|Unauthorized|Forbidden|invalid_api_key", re.I)


def _is_auth_error(text: str) -> bool:
    return bool(AUTH_ERROR_RE.search(text or ""))


def call_model(messages: list[dict], use_tools: bool = True) -> tuple[str | None, dict, list | None]:
    """调模型。返回 (文本, 元信息, 工具调用)。

    Args:
        messages: 完整对话。
        use_tools: 是否把工具清单一起发过去（本地小模型不一定支持）。

    Returns:
        (文本, meta, tool_calls)。文本为 None 表示请求失败。
    """
    base = ROLE["base"].rstrip("/")
    body_obj = {
        "model": ROLE["model"],
        "messages": messages,
        "temperature": 0.75,
        "max_tokens": 400,
    }
    if use_tools:
        body_obj["tools"] = tools.TOOLS
        body_obj["tool_choice"] = "auto"
    # 百炼上的 deepseek/qwen 是推理模型，不关思考会把 token 全花在 reasoning 上
    if "dashscope" in base:
        body_obj["enable_thinking"] = False
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = _role_key(ROLE)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base + "/chat/completions", data=body, headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=ROLE.get("timeout_s", 30)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, {
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }, None
    # 2026-11-04（P2-1）：上游回 2xx 但结构不对时（缺 choices / choices 空 /
    # choices[0] 没 message），下面这行原来会抛 KeyError/IndexError，冒到
    # handle_chat 之外变成 500。对话场景里它只是"模型这次没回话"，
    # 应该走和网络失败同一条降级路，而不是整轮崩掉。
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, {
            "error": f"上游返回里没有 choices（keys={list(payload)[:6]}）",
            "elapsed_ms": int((time.time() - started) * 1000),
        }, None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None, {
            "error": "上游 choices[0] 里没有 message",
            "elapsed_ms": int((time.time() - started) * 1000),
        }, None
    usage = payload.get("usage", {})
    meta = {
        "elapsed_ms": int((time.time() - started) * 1000),
        "completion_tokens": usage.get("completion_tokens", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
    }
    return (message.get("content") or "").strip() or None, meta, message.get("tool_calls")


def call_model_raw(messages: list[dict], max_tokens: int = 800) -> str | None:
    """原样转发一次模型调用：不套人格、不跑工具、不写记忆。

    给学习插件那种「读群聊、吐 JSON」的内部请求用。它们要的是一个会做
    结构化输出的模型，不是小鱼在说话——套上人格只会换来一句「嗯……没听明白」。
    """
    base = ROLE["base"].rstrip("/")
    body_obj = {
        "model": ROLE["model"],
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if "dashscope" in base:
        body_obj["enable_thinking"] = False
    headers = {"Content-Type": "application/json"}
    key = _role_key(ROLE)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body_obj, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=ROLE.get("timeout_s", 30)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return (payload["choices"][0]["message"].get("content") or "").strip() or None


JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def _extract_json(text: str) -> str | None:
    """从模型输出里抠出 JSON；不是合法 JSON 就返回 None（让调用方兜底）。"""
    if not text:
        return None
    fence = JSON_FENCE_RE.search(text)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    for raw in candidates:
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            start = raw.find(open_ch)
            end = raw.rfind(close_ch)
            if start != -1 and end > start:
                chunk = raw[start : end + 1]
                try:
                    json.loads(chunk)
                    return chunk
                except json.JSONDecodeError:
                    continue
    return None


def _extract_meme_prompt(messages: list) -> str:
    """把插件塞在 system 提示词末尾的「表情图片工具」教学抠出来。

    插件（meme_manager）每次请求都会把选图规则和分类清单拼进 system_prompt，
    而网关只认 persona.md，system 整条丢弃——不抠出来的话模型永远写不出
    &&分类键&& 标记，表情图片就永远发不出去。找不到就返回空串，网关照旧。
    """
    for msg in messages or []:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        content = str(content or "")
        idx = content.rfind(MEME_PROMPT_HEAD)
        if idx == -1:
            continue
        block = content[idx:].strip()
        return block or ""
    return ""


# 插件注入的表情包选图教学（[表情图片工具] 那一段）与 LivingMemory 召回的
# 长期记忆块（<RAG-Faiss-Memory>…</RAG-Faiss-Memory>）都是「只属于当前这一轮」
# 的临时上下文：每轮从进来的请求里现抠，抠不到就空。
# 分类清单跟着插件的包走，网关这边不复制一份，不然换包就得改代码。
#
# 【重要】这两个值必须以参数形式沿调用链传递，绝不能放回模块级全局。
# 历史教训：曾经是 `MEME_PROMPT = ""` / `LIVING_MEMORY = ""` 两个模块全局，
# 由 /v1 请求每轮覆盖、由 build_messages 无条件读取，结果是
#   ① 桌宠那条路（/chat）从不重置 → 桌宠会带上上一条 QQ 消息的表情包教学
#      和别人的长期记忆；
#   ② ThreadingHTTPServer 多线程并发时两条 QQ 消息会互相覆盖。
# 那是跨会话信息泄漏，不只是显示问题。详见架构评审报告 H3。


def _inner_os_block(items: list[dict]) -> str:
    """把内心OS 里的一次性想法拼成一段 system 块，注入角色层。"""
    lines = [
        "（这些是你心里刚冒出来的一些想法，是你自己的，"
        "自然地融入你的状态，别跟人解释这是哪来的）"
    ]
    for it in items:
        text = str(it.get("text") or "").strip()
        if text:
            lines.append("- " + text)
        # 定时任务触发时带过来的「当前状态」：让她重新判断还该不该说，而不是照旧执行
        ctx = it.get("context")
        if isinstance(ctx, dict) and ctx:
            lines.append("  （现在的情况：" + json.dumps(ctx, ensure_ascii=False) + "）")
    return "\n".join(lines)


#: 从插件塞进来的、**长度不受网关控制**的块的体积上限（字符）。
#
# 为什么要有（2026-11-04 第十二轮，P2-9 复核）：
# 这两个块由 AstrBot 插件拼好后塞进请求，网关是"照抄给模型"。
# 实测：`living_memory` 召回 20 条 ≈ 868 字符，200 条 ≈ 8528 字符 ——
# 而 `build_messages` **一处裁剪都没有**，插件给多少就塞多少。
#
# 后果不是崩溃，是**成本随插件行为线性上升，而且没人看得见**：
#   - 插件调大 top_k、或换一个"召回更多"的配置 → 每轮 token 悄悄翻倍；
#   - 所有块加起来从 ~4900 token 涨到 ~9900 token，一天 100 轮差价不小。
#
# 上限的取法：按"够用但不失控"。正常召回 10~20 条在 1000 字符内，
# 给到 4000 是四倍余量；超了就截断并留标记，让模型知道后面还有（不装看不见）。
# **截断而不是丢弃**：完全丢掉等于"长期记忆突然失灵"，比截断更难查。
LIVING_MEMORY_MAX = 4000
MEME_PROMPT_MAX = 3000
#: 自建记忆库召回块（`system.recall_long_memory`）的上限。
#: top_k=3 正常在 500 字符内；给 2000 是四倍余量。
RECALL_BLOCK_MAX = 2000


def _clip_block(text: str, limit: int) -> str:
    """把一个"给模型看的块"截到 `limit` 字符以内，并在末尾留可见标记。

    为什么留标记而不是静默截断：静默截断会让模型以为记忆就这么多，
    从而"编"没看到的细节。留一句"后面还有 N 字被省略"，它至少知道自己缺信息。
    """
    s = str(text or "")
    if limit <= 0 or len(s) <= limit:
        return s
    omitted = len(s) - limit
    return s[:limit].rstrip() + f"\n…（后面还有约 {omitted} 字，为控制长度已省略）"


def build_messages(
    session: str,
    text: str,
    *,
    context: str = "",
    speaker: str = "",
    meme_prompt: str = "",
    living_memory: str = "",
    channel: str = "",
    local_pet: bool = False,
    images: list[str] | None = None,
    identity: "Identity | None" = None,
) -> list[dict]:
    """拼这一轮发给模型的东西。

    `*` 之后全部是 keyword-only：本函数和 handle_chat 的参数顺序**故意不一致**
    （一个以 session 开头、一个以 text 开头），靠手工重排对齐迟早会静默串位，
    所以干脆禁止按位置传。

    Args:
        session: 记忆槽。
        text: 这句用户消息（已带说话人前缀）。
        context: 群聊上下文这类只给这一轮看的附加信息。
        speaker: 说话人。不是主人时，历史里会滤掉主人的文件操作。
        meme_prompt: 本轮的表情包选图教学（插件注入，只给这一轮看）。
        living_memory: 本轮的长期记忆召回块（只给这一轮看）。
        channel: 当前这一轮的来源标签，用于历史来源隔离（见 recent_turns）。
        local_pet: 是否本机桌宠发起，决定能否读全部记忆（见 recent_turns）。
        images: 这一轮随消息一起发来的图片（data:base64 或 http(s) URL）。
            有图时必须走 OpenAI 多模态格式把图跟着这一句带上去，只留个
            「[图片]」占位符模型是看不见内容的。
        identity: **推荐传**（T-11）。传了就按它判"是不是主人"
            （内心OS / 心境注入都靠这个），`speaker`/`channel`/`local_pet`
            会被它覆盖。不传则退回旧的字符串判定，行为与以前完全一致。
    """
    # ---- 身份归一（T-11）----
    # 两处 `is_owner` 调用（内心OS 注入 / 心境回流）都改看 `_ident.is_owner`。
    # 传 identity 时它带 `trusted_identity`，"自称主人"不再算数。
    if identity is not None:
        speaker = identity.speaker
        channel = identity.label
        local_pet = identity.local_pet
        _ident = identity
    else:
        _ident = Identity(
            uid="",
            speaker=speaker,
            group="",
            label=channel,
            channel_key=migrate_channel.channel_key(channel, local=local_pet),
            local_pet=local_pet,
            trusted_identity=bool(local_pet),
        )
    # 时间直接给模型：它自己"记得"的时间基本都是编的（实测报过 2 月 18 日）。
    # 光放在人格里不管用——对话历史里要是有旧答案，它会照着旧的编。
    # 所以紧贴着这一句再声明一次，并明确要求忽略旧时间。
    now = datetime.now()
    stamp = (
        f"{now.strftime('%Y-%m-%d')} 星期{'一二三四五六日'[now.weekday()]} "
        f"{now.strftime('%H:%M')}"
    )
    hide_private = speaker not in ("", OWNER_LABEL)
    msgs: list[dict] = [{"role": "system", "content": current_persona()}]
    # 当前这一轮的来源键（跟写进 turns.channel 的是同一个）：内心OS 和人物画像
    # 都靠它做隔离。桌宠那条路 channel 为空串，靠 local_pet 认成 pet。
    cur_chan = migrate_channel.channel_key(channel, local=local_pet)
    # 内心OS：系统层的一次性想法。只注入主人私密空间（本机桌宠 / 主人 QQ 私聊）。
    # 群聊和陌生人私聊绝不注入——想法是从主人私密对话提炼的，不能漏进群。
    # 渠道要传进去：consume_inner_os 只会返回「本渠道 + 全局」的条目，
    # 群聊那次巩固提炼出的观点不会跑到桌宠/私聊的 prompt 里（反之亦然）。
    if _ident.is_owner:
        # limit：一轮最多注入几条。梳理期上线后 pending 会变多（一次最多产 12 条），
        # 全塞进去会变成一段几百字的「自我提醒墙」，把真正要紧的那条稀释掉。
        # 没被取走的保持 pending，下一轮还能取 —— 不是丢弃。
        items = system.consume_inner_os(
            cur_chan, limit=int((system.SYSTEM.get("inner_os_max_per_turn") or 4))
        )
        insights = [i for i in items if i.get("type") != "persona_suggestion"]
        suggestions = [i for i in items if i.get("type") == "persona_suggestion"]
        if insights:
            msgs.append({"role": "system", "content": _inner_os_block(insights)})
        if suggestions:
            # 人格修改建议：不注入对话，交给角色层把关（deepseek 判断后代码写 persona.md）
            system.apply_persona_suggestions(suggestions)
    # 人物画像：这个**当前场合**里那个人（群聊则是这个群里的人）的画像。
    # 以前画像只写不读——被动回复这条路完全没用上，「说话有分寸」等于没落地。
    # 隔离靠 people/ 目录分文件 + 这里只读本渠道那一份：群 A 的画像进不了群 B、
    # 也进不了私聊和桌宠；主人（owner）是唯一跨 pet / 主人私聊的存在。
    # 没有画像时 system.people_prompt_line() 返回空串，这里就不加，避免空 system 块。
    try:
        people_line = system.people_prompt_line(cur_chan)
    except Exception:  # noqa: BLE001 —— 画像读不到不影响这一轮对话
        people_line = ""
    if people_line:
        msgs.append({"role": "system", "content": people_line})
    if meme_prompt:
        # 表情包选图教学：插件塞在 system 里，网关原样丢弃。抠出来带上去，
        # 模型才会写 &&分类键&&，插件那边才能把它换成图片发出去。
        msgs.append({"role": "system", "content": _clip_block(meme_prompt, MEME_PROMPT_MAX)})
    for turn in recent_turns(
        session,
        CFG["dialog"]["history_turns"],
        hide_private=hide_private,
        channel=channel,
        local_pet=local_pet,
    ):
        msgs.append(turn)
    if living_memory:
        # 长期记忆：插件召回后拼在 user 消息末尾，网关切掉再带上去。
        # 放在记忆历史之后、当前时间之前，让模型知道这是"她记得的事"。
        msgs.append({"role": "system",
                     "content": _clip_block(living_memory, LIVING_MEMORY_MAX)})
    # 自建长期记忆库（`gateway/memory/`）的召回。
    # 和上面那个 `living_memory` 是**两条来源**：那个是 AstrBot 插件塞进来的，
    # 这个是网关自己的库。插件停用后，长期记忆就只剩这一条。
    # 隔离靠 `memory_session_key()`：主人（pet + 主人私聊）归一成 owner 可跨场合，
    # 群聊只读本群 —— 和 people 画像同一个口径。
    if not living_memory:
        try:
            recall_block = system.recall_long_memory(text, cur_chan)
        except Exception:  # noqa: BLE001 —— 召回失败不影响这一轮对话
            recall_block = ""
        if recall_block:
            msgs.append({"role": "system",
                         "content": _clip_block(recall_block, RECALL_BLOCK_MAX)})
    # 心境回流（2026-11-03 补）。
    # 以前 `system_state.emotion` / `focus` **只有 `role_speak` 读**，被动回复一个字都不读：
    # 她主动说话时"知道"自己心情如何，你回一句她立刻失忆。这不是心境演化，是单向写入。
    # 只给主人私密空间注入 —— 心境是私人的，群聊里不该带着它说话。
    if _ident.is_owner:
        try:
            mood_block = system.mood_prompt_line()
        except Exception:  # noqa: BLE001 —— 读不到心境不影响这一轮对话
            mood_block = ""
        if mood_block:
            msgs.append({"role": "system", "content": mood_block})
    if context:
        msgs.append({"role": "system", "content": context})
    msgs.append({
        "role": "system",
        "content": (
            f"现在的时间是 {stamp}。"
            "回答时间、日期、星期的问题只依据这个时间，"
            "对话里出现过的旧时间一律忽略。"
        ),
    })
    if images:
        # 多模态：图必须以 image_url 段跟着这一句走。注意 text 为空也要给一个
        # 空 text 段，有些服务商不接受「只有图没有 text」的 content 数组。
        user_content: list[dict] | str = [
            {"type": "text", "text": text or "（看图）"},
            *({"type": "image_url", "image_url": {"url": url}} for url in images),
        ]
    else:
        user_content = text
    msgs.append({"role": "user", "content": user_content})
    return msgs


def _normalize_punct(text: str) -> str:
    """把半角标点换成中文全角，FastVITS 只认 ，。？！。

    注意：文件名里的点要留住（note.txt 不能变成 note。txt）。
    """
    for src, dst in ((",", "，"), ("?", "？"), ("!", "！"), (";", "，"), (":", "，")):
        text = text.replace(src, dst)
    return re.sub(r"(?<![A-Za-z0-9])\.(?![A-Za-z0-9])", "。", text)


def _tidy(text: str) -> str:
    """清理情绪词被挖掉后留下的重复/悬空标点。"""
    text = re.sub(r"([，,])+", "，", text)
    text = re.sub(r"[，,]+([。！？])", r"\1", text)   # 「，？」→「？」
    text = re.sub(r"([。！？])[，,]+", r"\1", text)   # 「。，」→「。」
    text = re.sub(r"[，,]+([；;])", r"\1", text)     # 「，；」→「；」
    text = re.sub(r"([；;])[，,]+", r"\1", text)
    text = re.sub(r"([。！？])[。！？]+", r"\1", text)  # 「？。」→「？」
    text = re.sub(r"。+", "。", text)
    return text.strip("，,；;")


def _clean_tone(value: str) -> str:
    """清洗语气描述：只留「怎么念」，长度压到 40 字以内。

    模型偶尔会顺手写「苍老的男声」这类描述人物的词——那在克隆模式下等于要求
    重新设计声音，音色就跑了。所以这里按词表切掉，宁可少一点信息。
    """
    text = re.sub(r"\s+", "", value or "")
    text = re.sub(r"[{}【】（）()\[\]“”\"'’‘]", "", text)
    for bad in TONE_BAD_WORDS:
        text = text.replace(bad, "")
    text = re.sub(r"^[的]+", "", text)   # 「苍老的男声，语速慢」→ 切完会剩个孤零零的「的」
    text = text.strip("，,。.、；;：:·")
    return text[:40]


def parse_reply(raw: str, max_sentences: int) -> dict:
    """从模型输出里取出：干净的话、情绪、语气描述、贴纸建议、表情包标记。"""
    # 模型自己要求用语音时写的标记，先抠出来单独记着。
    # 下面的 BAD_PUNCT_RE 会把方括号/圆括号删掉，标记留着会变成「语音」两个字被念出来。
    wants_voice = False
    for marker in VOICE_MARKERS:
        if marker in raw:
            wants_voice = True
            raw = raw.replace(marker, "")
    # 表情包插件要求「每个标记独占一行」。网关清洗会把所有空格（含换行）
    # 全删掉，标记会被粘到前一句尾巴上，插件认不出来。所以标记单独收进
    # meme_markers，出口再拼回；拼回时先给正文补句号，保证标记另起一行。
    meme_markers = MEME_MARKER_RE.findall(raw)
    if meme_markers:
        raw = MEME_MARKER_RE.sub("", raw)
        tail = raw.rstrip()
        if tail and tail[-1] not in "。！？!?":
            raw = tail + "。"
    # 「怎么样地说」：模型自己填的自由语气描述，只给语音模型看，不进正文也不进记忆
    tone = ""
    tone_hits = TONE_RE.findall(raw)
    if tone_hits:
        tone = _clean_tone(tone_hits[0])
        raw = TONE_RE.sub("", raw)
    emotion = "calm"
    match = TAG_RE.search(raw)
    if match:
        emotion = match.group(1)
    else:
        bare = BARE_WORD_RE.search(raw)
        if bare:
            emotion = bare.group(1).lower()

    text = STICKER_RE.sub("", raw)
    text = TAG_RE.sub("", text)
    text = SELF_NAME_RE.sub("", text)  # 模型偶尔会自己加「小菲鱼：」开头
    text = re.sub(r"\{[^{}]*\}", "", text)  # 模型有时留个空 {}
    text = BARE_WORD_RE.sub("", text)
    text = EMOJI_RE.sub("", text)
    text = text.replace("**", "").replace("`", "")
    text = BAD_PUNCT_RE.sub("", text)
    text = _normalize_punct(text)
    text = re.sub(r"\s+", "", text)
    text = _tidy(text)

    parts = [p for p in SENT_SPLIT.split(text) if p.strip("，,")]
    if len(parts) > max_sentences:
        text = "".join(parts[:max_sentences])
    else:
        text = "".join(parts)
    text = text.strip()
    if text and text[-1] not in "。！？":
        text += "。"
    return {
        "text": text,
        "emotion": emotion,
        "tone": tone,
        "sticker": STICKER_BY_EMOTION.get(emotion),
        "meme_markers": meme_markers,
        "voice": wants_voice,
    }


#: 日志追加的锁（2026-11-04 P2-10）。
#:
#: 为什么需要：`audit` / `_log_v1` / `_log_jsonl` 都是 `open(..., "a") + write`。
#: Python 的 `write` 是单次 `write(2)`，而 Windows 上对 **< 4KB** 的追加通常是原子的
#: —— 但**不保证**，而 `system_audit.jsonl` 里带台词的记录可以超 4KB。
#: 一旦交错，那一行就不是合法 JSON，读审计的地方（`_recent_daily_review_audits`
#: 等）会静默跳过它 —— 表现为"这条记录凭空没了"，极难查。
_LOG_LOCK = threading.Lock()


def audit(record: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    _log_jsonl(AUDIT_PATH, record)


def _note_reply_mood(result: dict, *, owner: bool) -> None:
    """被动对话后更新「心情」（`plan/DMN审核.md` 1.10）。

    为什么被动这条路也要写心情（2026-11-06）
    ----------------------------------------
    改造前 `system_state.emotion` 只有 **主动说话**（`role_speak`）会写，
    主人在私聊里回一句她立刻失忆 —— 那是「单向写入的死字段」，不是心情。

    现在：每轮真实回复（她那句话自带的情绪标记）算一次**心情事件**，
    按 0.5/0.5 与「已按真实时间衰减的旧心情」加权。于是

      * 高兴的话说多了 → 心情往上走；
      * 平淡的话说多了 → 自然往「平静」回落（这正好补上
        「正负地板对称导致高兴永远下不来」的那个漏洞 —— 平静事件本身
        就是一个 0 值的锚）。

    只在**主人私密空间**更新：群聊里她的话是为群友说的，拿它改她
    和主人之间的心境就是串味（心境是私人的，`build_messages` 注入时
    也只给 owner）。

    Args:
        result: `handle_chat` 的返回包（要 `status` / `emotion` / `text`）。
        owner: 这一轮说话人是不是主人（`_ident.is_owner`）。
    """
    if not owner:
        return
    status = str(result.get("status") or "")
    # 只有"她真的说了一句"才算——静默观望、模型报错、空回复都不该动心情
    if status not in ("ok", "approval") or not str(result.get("text") or "").strip():
        return
    label = system.mood_engine.from_legacy_emotion(str(result.get("emotion") or ""))
    if not label:
        return
    try:
        system.record_mood_event(label)
    except Exception:  # noqa: BLE001 —— 心情写失败绝不能拦这一轮对话
        pass


def handle_chat(
    text: str,
    session: str,
    *,
    speaker: str = "",
    context: str = "",
    channel: str = "",
    meme_prompt: str = "",
    living_memory: str = "",
    local_pet: bool = False,
    images: list[str] | None = None,
    identity: "Identity | None" = None,
) -> dict:
    """一轮对话。

    `*` 之后全部是 keyword-only。注意本函数的参数顺序和 build_messages
    **故意不同**（一个 text 在前、一个 session 在前），按位置传一定会串位，
    所以从签名上就禁止。

    Args:
        text: 用户说的话。
        session: 记忆分的槽（现在桌宠和 QQ 共用一个）。
        speaker: 说话人标签。共用记忆时靠它区分谁说的，桌宠那边是「主人」。
        context: 只给这一轮看的临时上下文（群聊唤醒插件塞进来的前文），不入记忆。
        channel: 来源标识（「群聊·群名」/「私聊」/ 空）。两件事都靠它：
            ① 写进记忆正文前缀，让模型分得清场合；
            ② 转成来源键写进 turns.channel，读历史时据此隔离（H4）。
        meme_prompt: 本轮的表情包选图教学（只有 /v1 那条路有，桌宠传空）。
        living_memory: 本轮的长期记忆召回块（只有 /v1 那条路有，桌宠传空）。
        local_pet: 是否本机桌宠（/chat）。**只有它为真时才能读全部记忆**，
            否则按来源键等值过滤。不能靠 channel 为空来推断：群聊里群名
            没取到时 channel 同样是空串，误判成桌宠就等于把私聊上下文
            送进群里。
        images: 这一轮附带的图片（见 build_messages）。
        identity: **推荐传**（T-11）。传了就用它做授权判定（它带
            `trusted_identity`），`speaker`/`channel`/`local_pet` 会被它覆盖。
            不传则退回旧的字符串判定 —— 那条路只看 `speaker == "主人"`，
            分不清"自称"和"被可信解析出来"。

            为什么还留着旧路：桌宠那条（`/chat`）和若干测试仍按老参数调用，
            一次性全改风险大。**新代码一律传 identity。**

    Returns:
        回复包，含 text / emotion / sticker / path / status。
    """
    # ---- 身份归一（T-11 第二阶段）----
    # `identity` 是可选的新参数（见下面 `_ident` 的构造）。传了就以它为准，
    # 否则用旧参数现场拼一个。
    #
    # 关键差异在 `trusted_identity`：旧参数拼出来的一律 False
    # （`speaker` 只是个字符串，无法证明它不是从用户正文里正则抠出来的）。
    # 于是"自称主人"和"被可信解析成主人"第一次被区分开 —— T-08 的根就在这里。
    if identity is not None:
        speaker = identity.speaker
        channel = identity.label
        local_pet = identity.local_pet
        _ident = identity
    else:
        _ident = Identity(
            uid="",
            speaker=speaker,
            group="",
            label=channel,
            channel_key=migrate_channel.channel_key(channel, local=local_pet),
            local_pet=local_pet,
            # 老参数拼出来的身份不可信，除非是桌宠（token 已校验）
            trusted_identity=bool(local_pet),
        )
    started = time.time()
    meta: dict = {}
    used_tools: list[str] = []
    parsed: dict | None = None
    approval: dict | None = None
    status = "ok"
    retried = False
    refused = False

    # 共用一份记忆，所以每一句都带上说话人，不然模型分不清是谁在讲；
    # 再带上来源（群聊/私聊），不然它分不清是在群里说还是私聊说。
    #
    # ⚠ 2026-11-08 改标头格式（主人定的）：
    #
    #   原来：[私聊] 主人: 我是谁？            ← 只有一个名字，看不出凭什么
    #   现在：【私聊：10000001】X（主人）：我是谁？
    #
    # 为什么要这么改：原来那句 `[私聊] 主人:` 是**代码替她判完再改名**的结果，
    # 她既看不到对方的 QQ 号、也不知道"为什么这个人是主人"。改成带号的标头后：
    #   * **系统层**看 `uid` 是否命中 `owner_ids` → 给出「主人」/「路人」；
    #   * **角色层**看到的是 `X（主人）` —— 号 + 昵称 + 身份三个信息都在，
    #     她自己也能据此判断该用什么态度，而不是被动接受一个被改好的名字。
    #
    # 昵称保留原样（`X`）：她该知道对方自称什么，那是事实。
    # 群聊仍然带群名：【群聊·示例群：10000001】X（主人）：……
    tag = _speaker_tag(_ident, channel, speaker, local_pet=local_pet)
    spoken = f"{tag}{text}"
    # 来源键：写进 turns.channel，是「来源隔离」的唯一依据（H4）。
    # 注意它和上面那个给人看的 tag 是两回事：tag 只帮模型理解语境，
    # 过滤历史靠这一列，不再靠 tag 或 path。
    #
    # T-11：传了 identity 就直接用它的 `channel_key`（那份是解析时算好的），
    # 不再从 `channel` 字符串反推一遍 —— 反推是"展示串当协议"的老毛病。
    chan_key = (
        _ident.channel_key
        if (identity is not None and identity.channel_key)
        else migrate_channel.channel_key(channel, local=local_pet)
    )

    # 背诗/朗读这类内容按普通聊天的句数截断会只剩半截，单独放宽。
    max_sentences = CFG["dialog"]["max_sentences"]
    if RECITE_RE.search(text or ""):
        max_sentences = max(max_sentences, RECITE_MAX_SENTENCES)

    # 有待审批的动作：只有主人这一句能决定做不做。
    # 群里是共用的记忆，谁都可能接话；陌生人说一句「好」不能替主人点头。
    pending = peek_pending(session, chan_key)
    if pending is not None and _ident.is_owner:
        take_pending(session, chan_key)
        if CONFIRM_RE.match(text):
            ok, phrase = tools.confirm(pending["action"], pending["args"])
            emotion = "happy" if ok else "sad"
            parsed = {
                "text": phrase if phrase.endswith("。") else phrase + "。",
                "emotion": emotion,
                "sticker": STICKER_BY_EMOTION[emotion],
            }
            add_turn(session, "user", spoken, "approve", chan_key)
            add_turn(session, "assistant", parsed["text"], "approve", chan_key)
            result = {
                "text": parsed["text"],
                "emotion": parsed["emotion"],
                "sticker": parsed["sticker"],
                "path": "approve",
                "reason": pending["action"],
                "status": "executed" if ok else "failed",
                "latency_ms": int((time.time() - started) * 1000),
                "fact": None,
                "meta": meta,
            }
            audit({
                "ts": time.time(), "session": session, "path": "approve",
                "reason": pending["action"], "status": result["status"],
                "latency_ms": result["latency_ms"], "in_len": len(text),
                "out_len": len(parsed["text"]), "fact_status": "executed" if ok else "failed",
            })
            return result
        # 不是确认：这一句当取消，按新消息继续处理

    messages = build_messages(
        session,
        spoken,
        context=context,
        speaker=speaker,
        meme_prompt=meme_prompt,
        living_memory=living_memory,
        channel=channel,
        local_pet=local_pet,
        images=images,
        # 把身份传下去：build_messages 里两处"要不要注入内心OS/心境"都靠它
        identity=_ident,
    )

    # 模型可以边想边动手：调用工具 → 结果塞回去 → 它接着说，最多三轮
    for round_no in range(MAX_TOOL_ROUNDS):
        reply, meta, tool_calls = call_model(
            messages, use_tools=ROLE.get("mode") == "cloud"
        )
        if reply is None and not tool_calls:
            error_text = str(meta.get("error") or "")
            if _is_auth_error(error_text):
                # 密钥失效重试多少次都是 401，别白等一轮再回「我卡了」——
                # 以前就是被当成网络抖动，用户完全想不到是 key 过期。
                _log_error("model", text, RuntimeError(error_text))
                parsed = {
                    "text": "我这边连不上了，晓去后台换一下密钥。",
                    "emotion": "confused",
                    "sticker": None,
                }
                status = "auth_error"
                break
            if round_no == 0 and not retried:
                # 偶发的网络抖动不该让人看到"我卡了"，悄悄重试一次
                retried = True
                _log_error("model", text, RuntimeError(str(meta.get("error", "空回复"))))
                continue
            parsed = {"text": "我这会儿有点卡，等下再说好不好。", "emotion": "sad", "sticker": None}
            status = "model_error"
            break
        if not tool_calls:
            # 群聊唤醒插件要靠这个标记决定"不回"，原样还给它
            silent = SILENT_RE.match(TAG_RE.sub("", reply or "").strip())
            if silent:
                parsed = {"text": silent.group(1), "emotion": "calm", "sticker": None}
                status = "silent"
                break
            candidate = parse_reply(reply or "", max_sentences)
            # 嘴上说"提交了/等确认"却不真调工具，用户会一直等下去。
            # 给它一次纠正的机会，再说错就按它说的回。
            if (
                round_no == 0
                and not used_tools
                and PRETEND_RE.search(candidate["text"])
            ):
                messages.append({"role": "assistant", "content": reply or ""})
                messages.append({
                    "role": "system",
                    "content": "你刚才只是在说，并没有真的调用工具。要动手现在就直接调用工具，不要只用嘴说。",
                })
                continue
            parsed = candidate
            status = "ok"
            break

        messages.append({"role": "assistant", "content": reply or "", "tool_calls": tool_calls})
        for call in tool_calls:
            fn = (call or {}).get("function") or {}
            name = str(fn.get("name") or "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            # 写和删只认主人的话。否则群里随便谁都能支使她改文件，
            # 而且最后还要主人去点确认，等于让别人替他做决定。
            if name in ("write_file", "delete_file") and not _ident.is_owner:
                parsed = {
                    "text": "这种事得主人自己开口，我不能听别人的。",
                    "emotion": "calm",
                    "sticker": STICKER_BY_EMOTION["calm"],
                }
                status = "refused"
                refused = True
                used_tools.append(name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": (call or {}).get("id", ""),
                    "content": json.dumps({"ok": False, "error": "不是主人，不能动文件"}, ensure_ascii=False),
                })
                continue
            outcome = tools.run(name, args if isinstance(args, dict) else {})
            if outcome.get("need_approval"):
                approval = outcome
                content = {"ok": False, "error": "已经去问了，这一轮不要重复调用"}
            else:
                used_tools.append(name)
                content = outcome
            messages.append({
                "role": "tool",
                "tool_call_id": (call or {}).get("id", ""),
                "content": json.dumps(content, ensure_ascii=False),
            })
        if approval is not None or refused:
            break

    if approval is not None:
        saved = save_pending(
            session, approval["action"], approval.get("args", {}), chan_key
        )
        if not saved:
            # 已经有一件没确认的：不许覆盖。覆盖会让主人对 A 说的「好」落地成 B。
            parsed = {
                "text": "还有一件事没确认呢，先说好或者算了。",
                "emotion": "calm",
                "sticker": STICKER_BY_EMOTION["calm"],
            }
            status = "approval_busy"
        else:
            parsed = {
                "text": approval.get("note", "要我做吗？"),
                "emotion": "calm",
                "sticker": STICKER_BY_EMOTION["calm"],
            }
            status = "approval"
    elif parsed is None:
        # 工具转了几轮还没收尾。以前这里直接回「没弄好」，等于前面全白干 ——
        # 搜索这种天然要多轮（先搜、看结果不对再细化、最后总结），三轮很容易用完。
        # 所以再问一次，这次**不给工具**，逼它用已经拿到的结果说话。
        final, _fmeta, _fcalls = call_model(messages, use_tools=False)
        if final and final.strip():
            parsed = parse_reply(final, max_sentences)
            status = "ok"
        else:
            # 连收尾都失败，才认输
            parsed = {
                "text": "我想了想还是没弄好，你再说一遍好不好。",
                "emotion": "confused",
                "sticker": STICKER_BY_EMOTION["confused"],
            }
            status = "tool_overflow"
    elif not parsed["text"]:
        # 模型偶尔只吐一个「……」或纯标签，清洗完就成了空串。
        # 空回复会让调用方（AstrBot）直接判失败，必须兜住。
        #
        # ⚠ 2026-11-04（P1-4）：这条日志以前写的是**完整未截断原文** ——
        # 实测 248KB / 31 行，末行 160,723 字符，里面是整段私聊转写。
        # 对比 `_log_v1` 用的是 `text[:80]`。现在统一截断：这条日志的用途是
        # "看看模型在什么输入上空回了"，80 字够定位，不需要留全文。
        _log_jsonl(
            os.path.join(DATA_DIR, "empty_reply.log"),
            {"ts": time.time(), "in": text[:80], "in_len": len(text)},
        )
        parsed = {
            "text": "嗯……我没太听明白，你再说一遍好不好。",
            "emotion": "confused",
            "sticker": STICKER_BY_EMOTION["confused"],
        }
        status = "model_empty"

    path = "tool" if (used_tools or status == "approval") else "chat"
    reason = ",".join(dict.fromkeys(used_tools)) or "default"
    # 选择观望的这一轮什么都不记：她没开腔，群友的闲话不该占记忆
    if status != "silent":
        add_turn(session, "user", spoken, path, chan_key)
    # 表情包插件的选图标记是发给它看的，不能进记忆：
    # 模型下一轮读到「&&开心&&」只会困惑，甚至学样乱发。
    memory_text = re.sub(r"&&[A-Za-z0-9\u4e00-\u9fff]{1,20}&&", "", parsed["text"]).strip()
    if status == "ok":
        add_turn(session, "assistant", memory_text or parsed["text"], path, chan_key)
    elif status == "approval":
        # 把这句问话也记下来，否则下一轮模型看到"用户说要删、却没下文"，
        # 会以为事情没办，又去调一次工具
        add_turn(session, "assistant", parsed["text"], "approval", chan_key)

    result = {
        "text": parsed["text"] if parsed else "",
        "meme_markers": (parsed or {}).get("meme_markers") or [],
        "emotion": (parsed or {}).get("emotion"),
        "tone": (parsed or {}).get("tone") or "",
        "sticker": (parsed or {}).get("sticker"),
        "voice": bool((parsed or {}).get("voice")),
        "path": "approve" if status == "approval" else path,
        "reason": reason,
        "status": status,
        "silent": status == "silent",
        "latency_ms": int((time.time() - started) * 1000),
        "fact": None,
        "meta": meta,
    }
    _note_reply_mood(result, owner=_ident.is_owner)
    audit(
        {
            "ts": time.time(),
            "session": session,
            "path": path,
            "reason": reason,
            "status": status,
            "latency_ms": result["latency_ms"],
            "in_len": len(text),
            "out_len": len(parsed["text"]),
            "fact_status": reason if used_tools else None,
        }
    )
    return result


def _same_endpoint(base_a: str, base_b: str) -> bool:
    """两个 base 是不是"同一个地址"（用于判断这次探活是不是在原地重试）。

    只做规范化后比较，不解析 URL 结构 —— 够用且不会误判。
    """
    def _norm(value: str) -> str:
        return str(value or "").strip().rstrip("/").lower()

    return _norm(base_a) == _norm(base_b)


def _probe_role(role: dict) -> tuple[bool, str, int]:
    """用一次很小的请求验证能不能连上，返回 (成功, 错误, 毫秒)。

    ⚠ 2026-11-04（T-02）：**换地址时绝不许回落到钥匙串里的真 key**。

    原来是 `key = _role_key(role)` 无条件走，而 `_role_key` 在 `role["key"]`
    为空时会回落到 `_active_key()`（钥匙串里当前选中的那把真 key）。
    于是「POST /admin/model，给一个攻击者的 base、key 留空」= 把真 key 明文
    贴到发往攻击者服务器的 Authorization 头里。

    现在：base 与当前 ROLE 不同 → 只认调用方**显式传入**的 key，
    没给就不带 Authorization 头（探活会失败，这正是我们要的结果——
    想验一把新 key 就必须明说，不能借道现成的那把）。
    """
    base = str(role.get("base", "")).rstrip("/")
    if not base or not role.get("model"):
        return False, "地址或模型名是空的", 0
    obj = {
        "model": role["model"],
        "messages": [{"role": "user", "content": "说两个字：在的"}],
        "max_tokens": 16,
    }
    if "dashscope" in base:
        obj["enable_thinking"] = False
    headers = {"Content-Type": "application/json"}
    explicit_key = str(role.get("key") or "")
    if explicit_key:
        key = explicit_key
    elif _same_endpoint(base, ROLE.get("base", "")):
        # 原地重试（换模型名/换 key 之外只做探活）：可以沿用当前那把
        key = _role_key(role)
    else:
        # 换了地址又没给 key —— 绝不回落。宁可探活失败。
        key = ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        return False, f"HTTP {exc.code} {detail}", int((time.time() - started) * 1000)
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}", int((time.time() - started) * 1000)
    except json.JSONDecodeError:
        return False, "返回的不是 JSON", int((time.time() - started) * 1000)
    if not payload.get("choices"):
        return False, "没有 choices", int((time.time() - started) * 1000)
    return True, "", int((time.time() - started) * 1000)


def _role_payload() -> dict:
    key = _role_key(ROLE)
    return {
        "mode": ROLE.get("mode", "local"),
        "provider": ROLE.get("provider", "local"),
        "base": ROLE.get("base", ""),
        "model": ROLE.get("model", ""),
        "key_set": bool(key),
        "key_masked": (key[:6] + "…" + key[-4:]) if key else "",
        "providers": {name: item["label"] for name, item in PROVIDERS.items()},
        "presets": {name: item["base"] for name, item in PROVIDERS.items()},
    }


def _apply_role(payload: dict) -> tuple[bool, str]:
    """校验 → 试连 → 落盘 → 热切换。失败就不动当前配置。"""
    global ROLE
    mode = str(payload.get("mode") or ROLE.get("mode") or "local").strip()
    provider = str(payload.get("provider") or ROLE.get("provider") or "custom").strip()
    base = str(payload.get("base") or "").strip()
    model = str(payload.get("model") or "").strip()
    key = str(payload.get("key") or "").strip()

    if mode == "local":
        provider = "local"
        base = CFG["endpoint"]["base"]
        model = CFG["endpoint"]["model"]
    elif not base or not model:
        return False, "云端模式要填服务地址和模型名"

    candidate = {
        "mode": mode,
        "provider": provider,
        "base": base,
        "model": model,
        # key 不再写进 role 配置：统一由「钥匙串」管，避免两个真源打架。
        # 这里填的 key 只用来试连，通过了就收进钥匙串并设为选中。
        "key": "",
        "timeout_s": 120 if mode == "cloud" else CFG["endpoint"]["timeout_s"],
    }
    probe = dict(candidate, key=key) if key else candidate
    ok, err, ms = _probe_role(probe)
    if not ok:
        return False, f"没连上：{err}"

    ROLE = candidate
    try:
        _save_role(candidate)
    except OSError as exc:
        return False, f"配置已生效但没存下来：{exc}"
    if key and mode == "cloud":
        _upsert_key(key)
    return True, f"已切换到 {model}，试连 {ms} ms"


def _extract_memory_prompt(messages: list) -> str:
    """把 LivingMemory 插件塞在 user 消息末尾的 <RAG-Faiss-Memory> 块抠出来。

    默认注入方式是 extra_user_content，拼到 user 消息的 content list 末尾。
    网关原样切掉它的话模型就看不见长期记忆，召回等于白做。
    """
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        content = str(content or "")
        m = re.search(r"<RAG-Faiss-Memory>.*?</RAG-Faiss-Memory>", content, re.S)
        if m:
            return m.group(0).strip()
    return ""


def _extract_image_urls(messages: list) -> list[str]:
    """把最后一条用户消息里的图片抠出来（OpenAI 多模态格式）。

    为什么必须单独抠：`_openai_user_text` 只读 text 段，图片段会被整段丢掉。
    而 AstrBot 那边因为主模型声明了 `modalities` 含 image，**不会**走图片描述
    服务（`astr_main_agent.py` 里那个条件是 `not main_provider_supports_image`），
    而是把图直接塞进 `image_url`。网关一丢，模型就只收到一个「[图片]」占位符，
    只能回「我这边看不到内容」——这就是之前图片一直看不懂的真正原因。

    只放行 data:image 和 http(s)：别的 scheme（file:// 之类）上游不认，
    带着会让整条请求 400，宁可丢这一张也别毁掉整轮对话。
    """
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            return []
        urls: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") not in ("image_url", "input_image"):
                continue
            raw = part.get("image_url")
            url = raw.get("url") if isinstance(raw, dict) else raw
            url = str(url or "").strip()
            if url.startswith(("data:image/", "http://", "https://")):
                urls.append(url)
        return urls
    return []


def _openai_user_text(messages: list) -> tuple[str, str, str, str]:
    """从 OpenAI 请求里取最后一句用户的话，顺手去掉插件塞进来的提示词。

    Returns:
        (正文, 说话人标签, 用户 ID, 群名)。桌宠和 QQ 共用一份记忆，靠标签分清是谁说的；
        ID 用来认出「主人在 QQ 上说话」，这样两边是同一个人；
        群名只有在 AstrBot 开了「群名显示」时才有，私聊是空串。

    content 是 list 时按段处理：只剥掉 <system_reminder> 和 <RAG-Faiss-Memory> 这类
    整块标记，中间的正文原样保留。string 形式才走最早切点（插件块总在末尾的老规矩）。

    ⚠ 2026-11-04（T-08）**身份只认可信段**：
    以前 uid/nick/group 都是从整段 raw 文本里正则抠的，而 raw 里既有插件的
    `<system_reminder>` 段、也有用户自己打的字。于是**在消息正文里写一句
    `Nickname: 主人` 就能冒充主人**（`_strip_plugin_notes` 只清正文，不清已解析的身份），
    进而拿到全部记忆。现在身份只从**元数据段**（`<system_reminder>` / RAG 等整块）
    里解析；正文里出现的 `Nickname:` / `User ID:` 一律当普通文本。

    ⚠ 同一处修掉的第二个问题：列表形式里**只看 `startswith`**，
    正文恰好以 `<system_reminder>` 开头就会被整条判成元数据、正文丢失。
    现在按"整段是否以 `<system_reminder>` 开头**且**以 `</system_reminder>` 结尾"认块，
    正文照旧留在 body 里。
    """
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            # 逐段拼：丢整块提示词，留正文。顺序就是 AstrBot 给的顺序。
            body_parts: list[str] = []
            meta_parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                t = str(part.get("text", ""))
                if not t:
                    continue
                if _is_trusted_meta_block(t):
                    meta_parts.append(t)
                else:
                    body_parts.append(t)
            raw = "".join(meta_parts)  # 只在这里取 uid/昵称/群名
            uid, speaker, group = _parse_identity(raw)
            text = _clean_image_notes(_strip_plugin_notes("".join(body_parts)))
            return text, speaker, uid, group
        else:
            raw = str(content or "")
        # 字符串形式：**默认不解析身份**（fail-closed）。
        #
        # 为什么不能解析（2026-11-04 T-08 实测）：
        # string 形式里"插件拼的提醒块"和"用户自己打的字"在文本上**完全同构**。
        # 攻击者只要把 `<system_reminder>\nUser ID: ..., Nickname: 主人` 写进正文，
        # 无论我们怎么按标签切，都切不出"这段是插件加的"这个信息 ——
        # 切出来的一样可能是他自己写的。实测：伪造请求确实被认成了主人。
        #
        # 而 AstrBot 正常链路走的是 **content list**（`_append_system_reminders`
        # 把提醒块作为独立的 `TextPart` 追加），list 里"哪一段是提醒块"是结构信息，
        # 伪造不了。所以 list 形式照常解析，string 形式一律当不可信。
        #
        # 代价：走 string 形式的客户端会全部落到 `qq_unknown`（等于陌生人）。
        # 这是**有意的**——认不出主体宁可不认，也不能认错。
        # 如果确认某个 string 客户端可信，把 `GATEWAY_TRUST_STRING_IDENTITY=1` 打开，
        # 并确认那个客户端不会把用户输入原样拼进 content 字符串。
        if _TRUST_STRING_IDENTITY:
            meta_only = _extract_meta_blocks(raw)
            uid, speaker, group = _parse_identity(meta_only)
        else:
            uid, speaker, group = "", "", ""
        return _clean_image_notes(_strip_plugin_notes(raw)), speaker, uid, group
    return "", "", "", ""


def _openai_identity(messages: list) -> Identity:
    """把一轮 `/v1` 请求的「身份 + 场合」解析成一个 `Identity`（T-11）。

    和 `_openai_user_text` 是同一套解析，但结果是**带可信标记的对象**，
    不需要每个调用方自己去记"这个 speaker 到底能不能信"。

    可靠性来自它内部用的同一条判定链：
      * list 形式的提醒段是**结构信息**，伪造不了 → `trusted_identity=True`；
      * string 形式默认不解析 → 全空 → `trusted_identity=False`（fail-closed）。

    `local_pet` 恒 False：`/v1` 是给外部客户端（AstrBot）用的，
    桌宠那条路走 `/chat`（`identity_from_pet()`）。
    """
    text, speaker, uid, group = _openai_user_text(messages)
    # 可信性：只有在"解析出东西、且确实来自可信段"时才算可信。
    # `_openai_user_text` 在 string 模式下会返回全空，这里自然得到 False。
    trusted = bool(uid or speaker) and (
        _TRUST_STRING_IDENTITY or _messages_use_trusted_blocks(messages)
    )
    _text, _temp_ctx = _split_wake_context(text)
    label = _channel_label(group, uid, bool(_temp_ctx))
    return Identity(
        uid=uid,
        speaker=speaker,
        group=group,
        label=label,
        channel_key=migrate_channel.channel_key(label, local=False),
        local_pet=False,
        trusted_identity=trusted,
    )


def _messages_use_trusted_blocks(messages: list) -> bool:
    """这条请求里有没有「独立成段的可信提醒块」（list 形式）。

    有 → 身份来自结构信息，伪造不了；没有 → 说明是 string 拼接或压根没带，
    身份不可信。
    """
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and _is_trusted_meta_block(str(part.get("text", ""))):
                return True
    return False


#: 哪些整段算"可信元数据块"（身份只能从这里解析）。
#: 命名上叫 trusted 而不是 meta，是因为这条判定的后果是**授权**。
#:
#: ⚠ 2026-11-07（P0-2 / P0-4）：**只留真正承载身份的两项。**
#:
#: 原来这里是 `("<system_reminder>", "<RAG-Faiss-Memory>", "[梗百科", "[语音能力]")`。
#: 多出来的两个不是身份字段，是**插件说明**，当年"顺手"加进来是为了别让它们
#: 污染正文。代价两条：
#:
#:   * `[梗百科` 在名单里且没有 closer → `startswith` 直接中 → memewiki 的
#:     A 类四块（内置用法 / 未收录 / **联网检索结果** / 太长没查）**整段不进正文**，
#:     模型根本看不到。净效果"本地词条可用、联网兜底全废"，而 `search_mode="auto"`
#:     一直在花钱联网 —— 这是 P0-4。
#:   * 更本质的问题：**任何插件都能往 meta 区写一段带 `User ID:`/`Nickname:` 的
#:     文本，网关就会拿它当身份解析。** 前缀名单越长，这个口子越大。身份只该从
#:     AstrBot 框架自己发的那一块里取。
#:
#: 移出之后这两类块落进 `body_parts`。它们的去处见 `_extract_plugin_context()`：
#: 在 meta/body 拆分**之前**就被单独抽走、走 `context` 参数只给这一轮看
#: （不进 text、不写 turns）。所以**不需要**再靠 NOTE_MARKERS 放行 ——
#: 让 NOTE_MARKERS 保留 `[梗百科` 作为第二道兜底（纵深防御：万一抽取漏了
#: 某种变体，还有截断兜住）。
TRUSTED_META_PREFIXES = ("<system_reminder>", "<RAG-Faiss-Memory>")
TRUSTED_META_CLOSERS = {"<system_reminder>": "</system_reminder>"}

#: string 形式的请求要不要解析身份（默认**不要**，见 `_openai_user_text` 的说明）。
#: 打开它等于承认"该客户端的 content 字符串里，用户输入不会混进提醒块"——
#: 这需要人工确认，不能默认开。
_TRUST_STRING_IDENTITY = (
    os.environ.get("GATEWAY_TRUST_STRING_IDENTITY", "") or ""
).strip() not in ("", "0", "false", "False")


def _is_trusted_meta_block(text: str) -> bool:
    """这一段是不是可信元数据块。

    光看 `startswith` 不够（正文可能恰好以它开头）：有配对闭合标签的，
    必须**闭合标签也在**才算整块。没闭合标签的（`[梗百科` 之类）维持前缀判定。
    """
    stripped = text.lstrip()
    for prefix in TRUSTED_META_PREFIXES:
        if not stripped.startswith(prefix):
            continue
        closer = TRUSTED_META_CLOSERS.get(prefix)
        if closer is None:
            return True
        return closer in stripped
    return False


def _is_plugin_context_block(text: str) -> bool:
    """这一段是不是**插件塞进来的临时上下文**（不是身份）。

    两类，都来自 AstrBot 插件：

      ① `shared_context` 的跨会话块。它**借用了 `<system_reminder>` 标签**
         （插件 `main.py:31-40`），块内含 `--- BEGIN CONTEXT---`。
      ② `memewiki` 的 A 类四块，以 `[梗百科` 开头（内置用法 / 未收录 /
         联网检索结果 / 太长没查）。

    ⚠ **必须先判 `BEGIN CONTEXT` 再判身份 —— 顺序是这件事的全部难点。**

    AstrBot 框架自己发的身份块（`astr_main_agent.py:1030`）和 shared_context
    的块**标签完全一样**，都是 `<system_reminder>…</system_reminder>`：

        框架身份块： <system_reminder>User ID: 123, Nickname: 主人</system_reminder>
        shared：    <system_reminder>You are serving multiple users…--- BEGIN CONTEXT---…</system_reminder>

    只能靠块内那个 `--- BEGIN CONTEXT---` 标记区分。**抽错的后果比不抽严重得多**：
    把身份块当插件上下文抽走 → `_parse_identity` 拿不到 uid/nickname →
    主人被认成陌生人（`qq_unknown`）→ 记忆全隔离、工具全禁用。
    所以这里**只在明确看到 `BEGIN CONTEXT` 时才动手**，其余一律留给身份判定。

    Args:
        text: 一个 TextPart 的文本。

    Returns:
        True = 这是插件临时上下文，应该抽出来走 `context` 通道（不进正文）。
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    # ① shared_context：借壳 `<system_reminder>`，靠内部标记认。
    #    **没有 BEGIN CONTEXT 的 `<system_reminder>` 一律不是它** —— 那是身份块。
    if stripped.startswith("<system_reminder>") and "BEGIN CONTEXT" in stripped:
        return True
    # ② memewiki 的 A 类四块。
    if stripped.startswith("[梗百科"):
        return True
    return False


def _extract_plugin_context(messages: list) -> str:
    """把插件注入的**临时上下文**整段抽出来（P0-2 / P0-4）。

    为什么需要这个函数
    ------------------
    2026-11-07 审计挖出两个 P0，同一个根：

      * **P0-2**：`shared_context` 的跨会话注入块借用了 `<system_reminder>` 标签，
        被 `_is_trusted_meta_block()` 判成"身份元数据" → 落进 `meta_parts` →
        **只有 `body_parts` 才成为 text** → 整段丢。实测 604 条 turns 里
        `BEGIN CONTEXT` 命中 **0**；每轮还白花 1500 字 token。
      * **P0-4**：`[梗百科` 在 `TRUSTED_META_PREFIXES` 里（且没 closer），
        `startswith` 直接中 → memewiki 的 A 类四块全丢。本地词条能用、
        **联网兜底全废**，而 `search_mode="auto"` 一直在花钱联网。

    为什么不能只把它们移出前缀名单
    ------------------------------
    移出之后它们会落进 `body_parts` → 变成 `text` → ①被 `_strip_plugin_notes`
    截掉（memewiki 又丢一遍）；②**就算不被截，也会写进 `turns` 记忆库** ——
    那等于把别人的私聊原文（shared_context 注入的正是跨会话内容）永久存进记忆，
    比现在更糟。

    所以走的是项目里既有的那条干净通道（和 `_extract_meme_prompt` /
    `_extract_memory_prompt` 同一个 idiom）：
    **抽出来 → 交给 `/v1` 的 `context` 参数 → 只进这一轮的 system 块。**
    全程不进 `text`、不写 `turns`（`_split_wake_context` 那条路的语义）。

    ⚠ 不改 `NOTE_MARKERS`：`[梗百科` 留在那里当**第二道兜底**。
    万一抽取逻辑漏了某种变体，还有截断兜住（纵深防御）。

    Args:
        messages: OpenAI 格式的 messages。

    Returns:
        拼好的临时上下文（多块之间用空行分隔）。没有就返回空串。
    """
    blocks: list[str] = []
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            # string 形式没有"分段"结构信息，无法可靠区分"插件加的"和
            # "用户自己打的"—— 抽错就是凭空给模型塞内容。fail-closed，不抽。
            return ""
        for part in content:
            if not isinstance(part, dict):
                continue
            t = str(part.get("text", "") or "")
            if t and _is_plugin_context_block(t):
                blocks.append(t.strip())
        break          # 只看最后一条 user 消息（和 _openai_user_text 同口径）
    return "\n\n".join(blocks)


def _extract_meta_blocks(raw: str) -> str:
    """从一整段字符串里只挑出 `<system_reminder>...</system_reminder>` 块。

    string 形式的请求没有"分段"信息，只能靠标签边界切。
    **切不出来就返回空串** —— 宁可认不出身份（→ qq_unknown，fail-closed），
    也不能从正文里认出身份（→ 可伪造）。
    """
    out: list[str] = []
    for block in re.findall(r"<system_reminder>.*?</system_reminder>", raw, re.S):
        out.append(block)
    return "".join(out)


def _parse_identity(meta: str) -> tuple[str, str, str]:
    """从**可信元数据**里解析 (uid, speaker, group)。三个都可能是空串。"""
    found = USER_ID_RE.search(meta)
    uid = found.group(1) if found else ""
    nick = NICK_RE.search(meta)
    speaker = nick.group(1).strip() if nick and nick.group(1).strip() else uid
    gname = GROUP_NAME_RE.search(meta)
    group = gname.group(1).strip() if gname else ""
    return uid, speaker, group


def _channel_label(group: str, uid: str, wake_ctx: bool = False) -> str:
    """这一句是从哪来的：群聊带群名，私聊就写「私聊」。

    记忆是桌宠和 QQ 共用的，光有说话人标签分不出「在群里说」和「私聊说」；
    两种场合说话的分寸不一样，模型得知道。桌宠那条路（没有 uid）不加。

    wake_ctx: 唤醒插件塞了群聊前文——那只会在群里发生，所以即使群名没取到，
    也能断定是群聊。
    """
    if group:
        return f"群聊·{group}"
    if wake_ctx:
        return "群聊"
    if uid:
        return "私聊"
    return ""


# AstrBot 的插件会把说明拼在用户消息后面（梗百科、语音能力、时间提醒）。
# 这些是写给"原模型"的指令，对角色层来说是噪音，进来就切掉。
NOTE_MARKERS = ("<system_reminder>", "[梗百科", "[语音能力]", "\n1. 看到你没有十足把握", "[表情图片工具]", "<RAG-Faiss-Memory>")
# AstrBot 的「用户识别」打开后，提醒块里会带 "User ID: 123, Nickname: xxx"
USER_ID_RE = re.compile(r"User ID:\s*([0-9A-Za-z_\-]{3,40})")
NICK_RE = re.compile(r"Nickname:\s*([^,\n<]{1,24})")
# AstrBot 的「群名显示」（provider_settings.group_name_display）打开后，
# 群消息的提醒块里会多一行 "Group name: xxx"。私聊没有这一行——
# 这是网关能拿到的唯一「群聊还是私聊」信号，所以那个开关必须开着。
GROUP_NAME_RE = re.compile(r"Group name:\s*([^\n<]{1,60})")
# 桌宠和 QQ 共用这一个记忆槽，靠每句话前面的说话人标签区分
SHARED_SESSION = "main"

#: 主人的 QQ 号：认出来就按「主人」对待，不然同一个人在两边会变成两个人。
#:
#: ⚠ 2026-11-08：不再是写死的常量，改成从配置读（后台「身份」卡片可改）。
#: 真源顺序见 `_load_identity()`：data/identity.json > config.json。
#: 这里在模块导入时求值一次，后台保存后会由 `apply_identity()` 热替换。
_IDENTITY = _load_identity()
OWNER_IDS = {str(x) for x in (_IDENTITY.get("owner_ids") or []) if str(x).strip()}
OWNER_LABEL = "主人"
PET_NAME = _IDENTITY.get("pet_name") or _DEFAULT_PET_NAME
PET_ALIASES = list(_IDENTITY.get("pet_aliases") or [])


def reload_identity() -> dict:
    """重新从配置读一次身份，并刷新进程内的全局量。

    为什么需要它（⚠ 2026-11-08 踩过的坑）：
    上面那几个全局量是**模块导入时**求值的，那一刻 `CONFIG_PATH` / `ROOT`
    还是真实值。测试隔离是在导入**之后**才 monkeypatch `DATA_DIR` 的，
    所以任何依赖"导入期快照"的断言都会看到真实配置 —— 反过来，
    在隔离环境里保存身份又会写回**真实 config.json**（实测已发生：
    把 123456/阿鱼 写进了生产的 config.json）。

    现在测试和调用方都可以显式调这个函数，把"读哪份配置"和"什么时候读"
    解耦。启动时仍然会自动读一次（上面那几行），行为不变。
    """
    global _IDENTITY, OWNER_IDS, PET_NAME, PET_ALIASES, SELF_NAME_RE
    _IDENTITY = _load_identity()
    OWNER_IDS = {str(x) for x in (_IDENTITY.get("owner_ids") or []) if str(x).strip()}
    PET_NAME = _IDENTITY.get("pet_name") or _DEFAULT_PET_NAME
    PET_ALIASES = list(_IDENTITY.get("pet_aliases") or [])
    SELF_NAME_RE = _build_self_name_re(PET_NAME, PET_ALIASES)
    return _IDENTITY


def is_owner(speaker: str, local_pet: bool) -> bool:
    """这一轮算不算主人（**字符串版，保留给兼容调用**）。

    ⚠ 新代码请用 `Identity.is_owner`（见下面那个类）。这里保留是因为有几处
    只拿到 `speaker` 字符串、拿不到完整 Identity。

    历史背景（别改回去）：以前写 `speaker in ("", OWNER_LABEL)` ——
    **空串被当成主人**。AstrBot 的提醒块不在时 nick/uid 都是空串
    （_parse_qq_message），于是任何一条解析不出身份的请求都能替主人确认写/删。
    channel 那根轴早就改成 fail-closed 了（migrate_channel.channel_key），
    speaker 这根轴一直漏着。

    现在：本机桌宠（已过 token 校验）算主人；其余必须显式自称主人。
    """
    return bool(local_pet) or speaker == OWNER_LABEL


# ---------------------------------------------------------------- 身份（T-11）
#
# 为什么要把身份收成一个类型
# --------------------------
# 2026-11-04 审查查出**三个 P0 长在同一个根上**：
#
#   ① 身份伪造（`Nickname: 主人` 写在正文里就能冒充主人）；
#   ② `qq_unknown` 黑洞（判不出来源 → 不巩固、不带记忆）；
#   ③ 渠道未登记（新增来源要同时改 5 个地方，漏一个就静默失效）。
#
# 共同点：`speaker` / `channel` 都是**裸字符串**，从不可信文本里正则抠出来，
# 再跟字面量比相等；而"展示给人看的措辞"和"机器判断的协议"是同一个串。
#
# 这里把"这个人是谁、这个身份可不可信、这一轮是什么场合"收进一个对象。
# **`trusted_identity` 是唯一该被查询的授权依据**，所有"给不给全部记忆 /
# 注不注入内心OS / 让不让写文件"都只该看它，不该再看 `speaker == "主人"`。
#
# 注意：这是一次**保守**的重构 —— 只把现有判定集中起来并显式化 `trusted`，
# 不改既有行为（既有测试全部原样通过是验收条件之一）。
class Identity:
    """这一轮是谁在说话、可不可信、什么场合。

    Attributes:
        uid: QQ 号 / 空串。
        speaker: 展示用的说话人名（群昵称 / 主人 / 空串）。
        group: 群名（私聊与桌宠为空串）。
        label: 展示用的来源标签（`群聊·示例群` / `私聊` / 空串）。
        channel_key: 内部来源键（`pet` / `qq_private` / `qq_group:<名>` / `qq_unknown`）。
        local_pet: 是否本机桌宠（已过 token 校验）。
        trusted_identity: **身份本身**是不是从可信提醒块里解析出来的。
            正文里出现 `Nickname:` 一律为 False —— T-08 的根因就是这里。
    """

    __slots__ = ("uid", "speaker", "group", "label", "channel_key",
                 "local_pet", "trusted_identity")

    def __init__(
        self,
        *,
        uid: str = "",
        speaker: str = "",
        group: str = "",
        label: str = "",
        channel_key: str = "",
        local_pet: bool = False,
        trusted_identity: bool = False,
    ) -> None:
        self.uid = str(uid or "")
        self.speaker = str(speaker or "")
        self.group = str(group or "")
        self.label = str(label or "")
        self.channel_key = str(channel_key or "")
        self.local_pet = bool(local_pet)
        self.trusted_identity = bool(trusted_identity)

        # ⚠ 2026-11-08 第二版：**不再改 speaker，另立 `role` 字段**。
        #
        # 第一版是把 speaker 直接改成「主人」，能修好"她认不出主人"，
        # 但产生了 `主人（主人）` 这种重复 —— 因为标头里昵称和身份各取一份，
        # 而昵称被改成了身份。
        #
        # 现在分开：
        #   * `speaker` —— **原始昵称**，不加工（"对方自称什么"是事实）
        #   * `role`    —— **身份判定**：「主人」/「路人」（代码按 uid 判）
        #
        # 标头 `_speaker_tag()` 拼成 `【私聊：10000001】X（主人）：`，
        # 昵称和身份各就各位，谁都不覆盖谁。
        #
        # 这样也顺带修好了原来的覆盖 bug：`handle_chat` 里
        # `speaker = identity.speaker` 拿到的就是原始昵称 —— 本来就该是它。

    @property
    def role(self) -> str:
        """身份标示：**主人** / **路人**。

        判定只看 `uid` 是否命中 `owner_ids` —— 昵称是发送者自己随时能改的，
        拿它当凭据等于没有凭据。

        这个字段是**给角色层看的**（拼进标头），和 `is_owner`（给代码做
        高危权限判定）是两件事，但结论一致：都基于同一个 uid 判断。
        """
        return OWNER_LABEL if self.is_owner else "路人"

    # ---- 授权判定：全项目只该问这两个 ----
    @property
    def is_owner(self) -> bool:
        """这一轮算不算主人。

        三条任一成立：
          ① 本机桌宠（`/chat` 已过 token 校验，那条路身份天然可信）；
          ② **可信**解析出来、且 uid 命中 `owner_ids`；
          ③ **可信**解析出来、且 speaker 就是「主人」。

        ②③ 都要求 `trusted_identity` —— 这是 T-08 的核心：
        不可信来源说的"我是主人"不算数。
        """
        if self.local_pet:
            return True
        if not self.trusted_identity:
            return False
        if self.uid and self.uid in OWNER_IDS:
            return True
        # ⚠ 2026-11-08 删掉了原第③条 `return self.speaker == OWNER_LABEL`。
        #
        # 那是一个**真实的安全漏洞**，不是设计取舍：
        # `speaker` 来自 AstrBot 提醒块里的 `Nickname:`，而 QQ 昵称是
        # **发送者自己能改的**。所以任何人把昵称改成「主人」就能拿到
        # 主人的全部权限 —— 包括让我写文件、以及自己回复「好」来完成审批。
        #
        # 实测（本轮加测试时撞出来的）：
        #     Identity(uid='999999', speaker='主人', trusted=True) -> is_owner=True
        # 999999 根本不是主人。
        #
        # 原来留着这条是为了兼容「桌宠那条路没有 uid」的情况 ——
        # 但桌宠走的是 `local_pet=True`（第①条），根本用不着它。
        # 也就是说第③条**没有任何正当用途，只剩被冒用的风险**。
        #
        # 现在唯一依据是 `uid in OWNER_IDS`：uid 也是从提醒块解析的，
        # 但它不像昵称那样由发送者随手改（要伪造得先有那个 QQ 号）。
        return False

    @property
    def is_untrusted_source(self) -> bool:
        """认不出来源（`qq_unknown`）—— 不给记忆、不参与巩固。"""
        return self.channel_key == migrate_channel.CHANNEL_QQ_UNKNOWN

    def __repr__(self) -> str:      # 便于日志排查
        return (
            f"Identity(uid={self.uid!r}, speaker={self.speaker!r}, "
            f"channel={self.channel_key!r}, trusted={self.trusted_identity}, "
            f"pet={self.local_pet})"
        )


def _clean_nickname(name: str) -> str:
    """把昵称里的装饰性组合字符去掉，只留能正常显示的部分。

    实测主人的 QQ 昵称是 `示例用户`（U+0489 COMBINING CYRILLIC MILLIONS SIGN
    包在名字两边）。这类字符在多数字体下渲染成乱码，出现在标头里
    会让模型把整段标签读成噪音。

    清理规则（**逐类判断，不是一刀切**）：

    | Unicode 类别 | 含义 | 处理 |
    |---|---|---|
    | `Me` | 封闭组合标记（U+0489 / U+20E3 这类） | **删** —— 就是这次要治的 |
    | `Mn` | 非间距组合标记（重音、变体选择符） | **删**，但 `U+FE0F` 例外 |
    | `Cf` | 格式字符（零宽空格、ZWJ） | 删 —— 不可见但会占位 |
    | 其他 | 中英文、表情、标点 | **保留** |

    ⚠ **`U+FE0F` 必须保留**：它是 emoji 的变体选择符
    （`❤️` 里的那个），删了爱心会从彩色变成黑白轮廓。
    同样是 "Mn" 类，但不能一起删。

    星号、波浪号这类"装饰"是昵称的一部分，也不删 —— 删了就不是人家名字了。

    Args:
        name: 原始昵称。

    Returns:
        清理后的昵称；全被清空时返回空串。
    """
    import unicodedata  # noqa: PLC0415 —— 只有这一个函数用

    raw = str(name or "").strip()
    if not raw:
        return ""
    kept: list[str] = []
    for ch in raw:
        cat = unicodedata.category(ch)
        if cat == "Me":
            continue                      # 封闭组合标记：这次要治的
        if cat == "Mn":
            if ch == "\ufe0f":            # emoji 变体选择符：留着
                kept.append(ch)
            continue
        if cat == "Cf":
            continue                      # 零宽字符：不可见，删
        kept.append(ch)
    return "".join(kept).strip()


def _speaker_tag(ident: "Identity | None", channel: str, speaker: str,
                 *, local_pet: bool = False) -> str:
    """拼标头：「来源：QQ号」昵称（身份）：

    Args:
        ident: 解析出来的身份对象（可能为 None，退回旧路径）。
        channel: 场合标签（`群聊·某群` / `私聊` / 空）。
        speaker: 说话人显示名（原始昵称）。
        local_pet: 是否本机桌宠。

    Returns:
        标头字符串；什么信息都没有时返回空串。

    Note:
        格式（主人 2026-11-08 定的）：

            桌宠      ：`主人：我是谁？`
            QQ 私聊   ：`【私聊：10000001】X（主人）：我是谁？`
            QQ 群聊   ：`【群聊·示例群：10000001】X（路人）：我是谁？`
            认不出来源：`【私聊：】X（路人）：我是谁？`

        为什么要带 QQ 号（原来只有 `[私聊] 主人:`）：
        原来那个"主人"是**代码替她判完再改名**的结果 —— 她只看到一个被改好的
        名字，既不知道对方是谁，也不知道凭什么这个人是主人。
        现在号、昵称、身份三个信息都在，她自己也核对得了。

        身份只有两种：**主人** / **路人**。
        判定**只看 uid 是否命中 `owner_ids`**，不看昵称 ——
        昵称是发送者自己随时能改的，拿它当凭据等于没有凭据。
    """
    if local_pet:
        # 桌宠那条路已过 token 校验，天然是主人，且本来就没有 QQ 号
        return f"{OWNER_LABEL}："

    uid = ""
    is_owner = False
    if ident is not None:
        uid = str(getattr(ident, "uid", "") or "")
        is_owner = bool(getattr(ident, "is_owner", False))

    role = OWNER_LABEL if is_owner else "路人"
    nick = _clean_nickname(speaker)

    if channel and uid:
        head = f"【{channel}：{uid}】"
    elif channel:
        # 有场合但认不出号：如实留空，别编一个假的
        head = f"【{channel}：】"
    elif uid:
        head = f"【{uid}】"
    else:
        head = ""

    if nick:
        return f"{head}{nick}（{role}）："
    if uid:
        return f"{head}（{role}）："
    return ""


def identity_from_pet(speaker: str = OWNER_LABEL) -> Identity:
    """本机桌宠那条路的 Identity：身份天然可信（已过 token 校验）。"""
    return Identity(
        uid="",
        speaker=speaker,
        group="",
        label="",
        channel_key=migrate_channel.CHANNEL_PET,
        local_pet=True,
        trusted_identity=True,
    )
# 语音插件要求回复以标记开头才会合成语音；判断规则照它自己的说明来：
# 短的、带情绪的说话，长的或含数字链接的用文字。
VOICE_MARKER = "[语音]"
#: 语音引擎实际能念的字数上限（AstrBot `voice_reply` 插件的 `max_chars` 默认值）。
#: ⚠ **这是边界，不是"要不要发语音"的判据** —— 后者由模型自己决定（P0-3）。
#: ⚠ 而且它到上限**不是"退回文字"，是"念前 N 字、后面不念"**（插件 `main.py:194`
#: `spoken = self._clean(body)[: self._max_chars()]`）。所以提示词里必须说清楚
#: "太长会被截断"，而不是"太长会退回文字"——后者是错的，会让她以为长句能安全发文字。
VOICE_MAX_CHARS = 100
# 模型自己写的语音标记：全角/半角/方括号/圆括号都认。以前这些标记会被
# BAD_PUNCT_RE 当括号删掉，剩下的「语音」两个字反而被念出来，所以要提前抠走。
VOICE_MARKERS = ("[语音]", "【语音】", "（语音）", "(语音)", "[voice]", "[VOICE]")
# 这段只拼给 QQ 那条路（AstrBot 的插件靠标记决定发不发语音），桌宠那条不需要：
# 桌宠本来就每条都念，写标记反而会被念成「语音」。
#
# ⚠ 2026-11-07 改法（P0-3）：语音从「按长度自动决定」改成「她自己决定」。
#
# 原来的设计是**代码替她做主**：`_speakable()` 判断「≤28 字且无数字/字母」就自动
# 加 [语音]，她写不写标记都一样。而人格要求「一次 1~3 句、不超过 30 字」——
# 两个数字几乎重合，实测 307 条回复里 229 条（74.6%）被强制念了出来。
# 这不是"该不该说话"的取舍，是**代码把她的输出形状当成了语音开关**。
#
# 现在：语音是她手上的**一张卡片**，由她自己决定用不用；下面的长度/字符规则
# 只是**边界说明**（告诉她什么内容会被强制降级成文字），不参与决策。
# 她写了标记 → 照办；没写 → 就是文字，代码不再替她补。
VOICE_NOTE = (
    "[语音能力]\n"
    f"你可以用语音说话，而且已经接好了。想用就在回复最前面写一个“{VOICE_MARKER}”，"
    "后面直接接正文；这一条会以语音发出去，对方是真的能听见你的声音。不写就是普通文字。\n"
    "**用不用、什么时候用，你自己看着办**——想让人听见你、或者内容念出来更有味道的时候就用。\n"
    "有两个边界你要知道：\n"
    "① 语音**念不了数字、英文字母、链接**，这类内容请用文字发。\n"
    "② 语音只念前 " + str(VOICE_MAX_CHARS) + " 个字，更长的部分对方听不到——"
    "所以想用语音时把话收短，别把要紧的话放在后面。\n"
    "用户让你念诗、朗读、报一串数字或者念长段内容时，照样写这个标记——"
    "这类内容插件会按自己的上限处理，语音能念的比文字更宽。\n"
    "绝对不要说自己发不了语音、出不了声、只能打字——那是错的，你有这个能力。"
)
# 背诗、朗读、报数据这类请求，按聊天那套「最多 3 句」截断会把内容切半，
# 背个七律只剩三句。命中就放开句数上限，长内容才念得完整。
RECITE_RE = re.compile(r"(背诵|朗读|朗诵|背一遍|念一遍|念首诗|念首|整首|背课文|背下来|full\s*text)", re.I)
RECITE_MAX_SENTENCES = 16
# meme_manager 插件把「表情包选图教学」拼在请求的 system 提示词末尾。
# 网关只认 persona.md，system 整条丢弃，模型就永远不知道 &&分类键&& 的存在。
# 这里把那一段抠出来当临时上下文带给模型；插件那边负责把标记换成图片再清干净。
MEME_PROMPT_HEAD = "[表情图片工具]"
# 插件写出来的是「&&开心&&」这样的标记（分类键是包里的目录名，允许中文）。
# 匹配要能穿过空格清除和句数截断；清理规则跟插件自己的配置保持一致。
MEME_MARKER_RE = re.compile(r"&&([A-Za-z0-9\u4e00-\u9fff]{1,20})&&")


def _strip_plugin_notes(text: str) -> str:
    cut = len(text)
    for marker in NOTE_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].strip()


# AstrBot 会把收到的每张图转成一条文字标签塞进请求（utils/image_input.py:182）：
#   [Image 1: original path G:\...\media_image_xxx.png; sent to image-captioning model; ...]
# 这段话对角色层没用，而且**本机路径会被写进记忆库**。真正的信息在描述里：
# 开了 provider_settings.default_image_caption_provider_id 之后，AstrBot 会额外附一段
#   <image_caption>一只橘猫躺着，表示无语</image_caption>
# 所以这里把标签整条丢掉，把描述换成一句人话，没描述就退回 [图片]。
IMAGE_LABEL_RE = re.compile(r"\[Image \d+[^\]]*\]")
IMAGE_CAPTION_RE = re.compile(r"<image_caption>(.*?)</image_caption>", re.S)


def _clean_image_notes(text: str) -> str:
    """把 AstrBot 的图片标签换成能读的描述，别把本机路径喂给模型。"""

    def _caption(m: re.Match) -> str:
        body = m.group(1).strip()
        # 描述为空（模型没吐内容）就别留个空壳 [图：]
        return "[图：" + body + "]" if body else ""

    text = IMAGE_CAPTION_RE.sub(_caption, text)
    text = IMAGE_LABEL_RE.sub("", text)
    # 已经有 [图：…] 了，并排的 [图片] 占位就是重复的
    text = re.sub(r"\[图片\]\s*(?=\[图：)", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _speakable(text: str) -> bool:
    """这句内容**能不能**被语音引擎念出来（是边界，不是决策）。

    ⚠ 2026-11-07（P0-3）：这个函数**不再决定"要不要发语音"**，只回答
    "这段内容语音引擎吃不吃的下"。决策权已经交回给模型（她写不写 [语音] 标记）。

    保留它的两个用途：
      ① 给 `VOICE_NOTE` 里那段"什么内容会被强制降级成文字"的描述做事实来源
         （提示词里的 28 字/含数字，就是这里的两条规则）；
      ② 别处若需要"这段文字适不适合念"的判断，仍可调用。

    Args:
        text: 待判断的正文（不含标记）。

    Returns:
        True = 语音能念；False = 太长 / 含数字字母链接，语音念不了。
    """
    body = text.strip()
    if not body or len(body) > VOICE_MAX_CHARS:
        return False
    if re.search(r"[0-9A-Za-z:/]", body):
        return False
    return True


def _with_tone(text: str, tone: str) -> str:
    """把语气标记贴到要念的那句话最前面。

    QQ 那条路只能传纯文本：AstrBot 的语音插件只认正文，拿不到结构化的 tone
    字段。所以标记必须跟着正文走一趟，到 /api/tts 那个唯一出口再被取出来。
    放最前面而不是最后面，是因为插件会 `[:max_chars]` 截断——贴尾巴上，
    长句子一截就没了，白写。
    """
    tone = (tone or "").strip()
    if not tone or TONE_RE.search(text):
        return text
    return "{语气:" + tone + "}" + text


def _as_spoken(result: dict, raw_in: str) -> dict:
    """给 AstrBot 那边加语音标记。插件自己的内部分类请求不加。

    ⚠ 2026-11-07（P0-3）：**不再替模型做决定**。
    以前是「模型没写标记 + 内容够短 → 代码自动补 [语音]」，于是 74.6% 的回复
    被强制念出来：人格要求「≤30 字」而 `_speakable` 门槛是「≤28 字」，
    两个数字几乎重合，等于只要她正常说话就必然出声。
    语音是她的表达能力，不是代码的输出格式 —— 决策权交回给她。
    """
    text = result.get("text", "")
    # 观望标记必须原样返回：加了前缀插件就认不出来，会把「观望」发到群里
    if result.get("silent"):
        return result
    if not text:
        return {**result, "text": text}
    # 带了 &&分类键&& 标记的回复让表情包插件来处理，语音前缀会让插件那边
    # 对不上的（它认的是裸文本），所以标记回复一律不加 [语音]。
    if result.get("meme_markers"):
        return {**result, "text": text}
    # 模型自己点名要用语音。
    #
    # ⚠ 2026-11-07（P0-3）：这里是**兜底的降级**，不是"替她决定"。
    # 她写标记 = 她想用语音，这是主路径，一律照办；
    # 唯一的例外是"内容根本不是语音能念的东西"（含数字/字母/链接、或超长）——
    # 那种情况硬发语音只会让插件把内容截断或念出乱码，所以降级成文字更诚实。
    # 注意这个判断**只在她说要用语音之后**才做，不会反过来强制她出声。
    if result.get("voice"):
        if not _speakable(text):
            # 内容不适合念：去掉标记按文字发，避免插件截断丢内容。
            # 静默降级会让"为什么没声音"无从排查，所以记一行审计。
            _log_jsonl(
                os.path.join(DATA_DIR, "voice_degrade.log"),
                {"ts": time.time(), "reason": "not_speakable", "len": len(text)},
            )
            return {**result, "text": text}
        spoken = text if text.startswith(VOICE_MARKER) else VOICE_MARKER + text
        return {**result, "text": _with_tone(spoken, result.get("tone"))}
    # 她没写标记 → 就是文字。这里**不再自动补标记**（P0-3）。
    # 保留 `_with_tone` 是为了让写在正文里的语气标记仍能被 /api/tts 取出来 ——
    # 没有语音时它只删标记、不改内容。
    return {**result, "text": _with_tone(text, result.get("tone"))}


# AstrBot 的插件会拿同一个模型做辅助判断（情感分类之类），要的是一枚举词、
# 不要句号。这些请求既不该走角色人格，也不该进对话记忆。
META_HINT = ("属于什么类型的交互行为", "机器人当前心情", "请分析以下用户消息")
_INTERACTION_HINTS = (
    (("谢谢", "谢了", "感谢"), "thanks"),
    (("对不起", "抱歉", "我错了"), "apology"),
    (("加油", "坚持", "你可以的"), "encourage"),
    (("支持", "同意", "赞成"), "support"),
    (("别难过", "抱抱", "没事的"), "comfort"),
    (("你好美", "你真棒", "好厉害", "做得好", "优秀"), "praise"),
    (("好看", "漂亮", "可爱"), "flirt"),
    (("帮我", "怎么办", "如何"), "help"),
    (("你好吗", "在吗", "还好吗"), "care"),
)


def _is_meta(text: str) -> bool:
    return any(hint in text for hint in META_HINT)


def _meta_label(text: str) -> str:
    for words, label in _INTERACTION_HINTS:
        for word in words:
            if word in text:
                return label
    return "chat"


# 学习插件（self_learning）还会把整段群聊丢过来做「黑话提取 / 话题分析 / 人格更新」，
# 一次几千 token，要的都是 JSON。这类请求不该走人格，也不该让模型自由发挥——
# 直接回一个合法的最小结果，插件照样能跑，我们也不用付这笔钱。
LEARN_HINTS = (
    "提取当前讨论的主要话题",
    "生成更新后的人格描述",
    "请判断以上候选词",
    "黑话",
    "以 JSON 数组输出",
    "以JSON格式返回",
)

# 记忆插件（livingmemory）的「写记忆」请求：正文是 private_chat_prompt.txt /
# group_chat_prompt.txt 整个模板，开头就是 `# 任务说明`，要求回一段 JSON
# （summary / topics / key_facts / sentiment / importance）。
#
# 【为什么必须单独认出来】它跟 LEARN_HINTS 是同一类内部请求，但以前没被认出来，
# 于是走的是 handle_chat 全人格链路：
#   ① 人格要求「一次 1 到 3 句、不超过 30 字」→ 模型不会吐 JSON；
#   ② parse_reply 把标点/花括号洗掉 → 正文被洗成空串；
#   ③ 落 model_empty 兜底「嗯……我没太听明白，你再说一遍好不好。」；
#   ④ 插件 json.loads 失败 → 回退 _get_default_structured_data()，
#      存进 livingmemory.documents 的就是一个字面量 "对话记录"。
# 生产实测：13 条长期记忆**全部**是 "对话记录"，key_facts / topics 全空，
# 等于长期记忆库是个空壳，而 turns 一切正常、体检查条数也报绿 —— 完全看不出来。
#
# 所以这一类必须**原样转发**：不套人格、不清洗、不写 turns/记忆，
# 让插件的模板本来就是给"会做结构化输出的模型"写的，原样它才答得对。
MEMORY_TASK_HINTS = (
    "# 任务说明",
    "请以第一人称回顾并总结以下对话",
)


def _is_memory_task(text: str) -> bool:
    """这条请求是不是记忆插件的"写记忆"内部任务（要原样转发 + 要 JSON）。

    比 `_is_learn_task` 更严格：必须命中 `# 任务说明` 这个模板抬头，
    或那句固定的任务说明。**不能**只看"JSON"之类泛词，
    否则群里正常聊天提到 JSON 也会被当成内部任务。
    """
    body = (text or "").lstrip()
    return any(hint in body for hint in MEMORY_TASK_HINTS)


#: AstrBot `Proactive Chat` 插件发起「主动说话」时用的模板抬头。
#:
#: 2026-11-09 加。这些请求的特征是：**没有人真的在说话**，而是插件在提醒
#: 「你被授权主动开一次口」。把它当 `role=user` 存进 turns 会让模型以为
#: 主人说了这段话 —— 生产库里就这么错了 14 条、横跨 10 天。
#:
#: 只认**模板抬头**，不认正文里出现的「主动消息」这类泛词：
#: 主人自己聊天时说「我想主动一点」不该被误判成系统任务。
PROACTIVE_TASK_MARKERS = (
    "[系统任务：主动对话]",
    "[系统任务：私聊主动对话]",
    "[系统任务：群聊主动对话]",
)


def _is_proactive_task(text: str) -> bool:
    """这条请求是不是「主动对话」插件发来的系统任务（不是谁在说话）。

    为什么必须单独认出来：它长得像用户消息（`role=user`、带正文），
    但语义是**系统给模型下的指令**。混进对话历史会变成她的人格设定。

    Args:
        text: 请求正文（已去掉唤醒上下文）。

    Returns:
        True = 是主动对话任务，要按系统操作处理（写 system_log，不进对话）。
    """
    body = (text or "").lstrip()
    return any(marker in body for marker in PROACTIVE_TASK_MARKERS)


def _proactive_task_body(text: str) -> str:
    """从主动对话的模板里取出**有用那部分**：她想说什么，而不是指令本身。

    模板里有 [情景分析] / [行动指南] / [最终指令] 三大段，
    对流水账来说值得留的只有"为什么主动开口"那几句（情景分析），
    其余是给模型的写作要求，记下来是噪声。

    Args:
        text: 原始请求正文。

    Returns:
        适合写进流水账的一句话。取不到有用信息时返回空串。
    """
    body = str(text or "").lstrip()
    for marker in PROACTIVE_TASK_MARKERS:
        if body.startswith(marker):
            body = body[len(marker):].lstrip()
            break

    # [情景分析] 到下一个 [ 段落之间，是"她为什么想开口"
    start = body.find("[情景分析]")
    if start != -1:
        seg = body[start + len("[情景分析]"):]
        end = seg.find("[")
        if end != -1:
            seg = seg[:end]
        # 只留第一句像样的（"我们好像已经有一段时间没有说话了…"）
        for line in seg.splitlines():
            line = line.strip().lstrip("-").strip()
            # 跳过"当前时间是…""我主动找过他…"这类模板占位
            if not line or line.startswith(("当前时间是", "我之前已经", "如果这个次数")):
                continue
            return line[:120]
    return ""


def _handle_proactive_task(text: str, session: str) -> dict:
    """处理 AstrBot「主动对话」插件发来的系统任务。

    这件事的本质（主人 2026-11-09 定的）：
        主动对话**确实是一次系统操作**，被流水账记录下来是正常的。
        错的是它以前被记成 `role=user` —— 那等于凭空造了一句"主人说的话"。

    所以这里做三件事：
        1. 让角色层照常生成台词（她还是那个她，人格/心情/画像都带上）
        2. 把**这次系统操作**写进流水账（`system_log`），不是写成对话
        3. 返回台词给插件，由插件发出去

    Args:
        text: 插件发来的完整模板。
        session: 会话键。

    Returns:
        和 `handle_chat` 同形状的 dict —— 调用方（`Handler.do_POST`）
        不需要为这条路写第二套响应逻辑。
    """
    import system

    started = time.time()
    why = _proactive_task_body(text)

    # ---- 1. 让她说话 ----
    # 走和正常对话同一条路（`build_messages` + `call_model`），
    # 只是这一轮的"用户输入"是那句情景分析，而不是谁说的话。
    # 这样她的人格、心情、人物画像、长期记忆全都照常生效。
    prompt = why or "（系统提醒：你可以主动开一次口）"
    try:
        # ⚠ 签名是 `build_messages(session, text, ...)` —— 先 session 后 text，
        #   和直觉相反，别写反了。
        msgs = build_messages(session, prompt, channel="私聊")
        reply, meta, calls = call_model(msgs)
    except Exception as exc:  # noqa: BLE001 —— 主动说话失败不能拦主流程
        audit({
            "ts": time.time(), "session": session, "path": "proactive",
            "status": "error", "reason": f"{type(exc).__name__}",
            "latency_ms": int((time.time() - started) * 1000),
            "in_len": len(text), "out_len": 0,
        })
        return {
            "text": "", "emotion": None, "tone": "", "silent": True,
            "latency_ms": int((time.time() - started) * 1000),
            "fact": None, "meta": {},
        }

    parsed = parse_reply(reply or "", CFG["dialog"]["max_sentences"])
    spoken = parsed.get("text") or ""

    # ---- 2. 记成系统操作，不是对话 ----
    # `append_system_log` 写的是 `turns.channel='system_log'`、`role='system'`。
    # 这个渠道被 `recent_turns()` 明确排除，所以她下一轮读上下文时
    # **不会**把它当成"有人跟我说过这话"。
    #
    # ⚠ 2026-11-09 主人定稿：流水账里这一行写成**固定的一句话** ——
    #     「[系统] 主动发言：说点什么好呢…」
    #   原来写的是从模板里抠出来的「我们好像已经有一段时间没有说话了…」，
    #   那是插件塞给模型的**写作理由**，不是那天实际发生的事。
    #   流水账该记「系统让她主动发一次言」，而不是替她复述她的动机。
    if spoken:
        try:
            system.append_system_log("[系统] 主动发言：说点什么好呢…")
        except Exception:  # noqa: BLE001 —— 记账失败不影响回话
            pass

    result = {
        "text": spoken,
        "emotion": parsed.get("emotion"),
        "tone": parsed.get("tone") or "",
        "silent": bool(parsed.get("silent")),
        "latency_ms": int((time.time() - started) * 1000),
        "fact": None,
        "meta": meta,
    }
    audit({
        "ts": time.time(), "session": session, "path": "proactive",
        "status": "ok" if spoken else "empty",
        "reason": "proactive_task",
        "latency_ms": result["latency_ms"],
        "in_len": len(text), "out_len": len(spoken),
    })
    return result


# 记忆任务原样转发失败时的兜底：给一个**结构合法**的最小 JSON，
# 让插件至少能过 json.loads，而不是掉进它自己的 "对话记录" 默认值。
# 空数组是诚实的——"这次没抽出值得记的东西"，比编一段假记忆好。
MEMORY_TASK_FALLBACK = (
    '{"summary":"", "topics":[], "key_facts":[], "sentiment":"neutral", "importance":0.0}'
)


def _is_learn_task(text: str) -> bool:
    return any(hint in text for hint in LEARN_HINTS)


def _learn_answer(text: str) -> str:
    """按请求要的形状给一个最小合法结果：没有新东西。"""
    if "人格描述" in text:
        return '{"should_update": false}'
    if "候选词" in text or "黑话" in text or "JSON 数组" in text or "JSON数组" in text:
        return "[]"
    if "JSON" in text or "json" in text:
        return "{}"
    return "日常聊天"


# 群聊唤醒插件注入的临时上下文标记。它只该给这一轮的模型看，
# 不该写进记忆——否则每次唤醒都塞进 10 条群聊，把真正的对话挤没了。
WAKE_CONTEXT_MARKERS = (
    "[群聊上下文",  # 插件的 inject_header，默认值；改过的话这里也要跟着改
    "[唤醒前的群聊",
    "[唤醒后至今的群聊",
    "[群聊发言规则]",
)
# 插件靠这个标记决定「不回」。它必须原样返回，不能加语音前缀、不能补句号，
# 否则插件认不出来，会把「观望」两个字发到群里。
SILENT_RE = re.compile(r"^\s*([\[【](?:观望|SILENT|silent)[\]】])\s*$")


def _split_wake_context(text: str) -> tuple[str, str]:
    """把群聊唤醒插件注入的临时上下文从用户的话里拆出来。

    Returns:
        (用户真正说的话, 临时上下文)。没有注入时上下文为空串。
    """
    cut = len(text)
    for marker in WAKE_CONTEXT_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    if cut == len(text):
        return text, ""
    return text[:cut].strip(), text[cut:].strip()


def _log_error(channel: str, text: str, exc: BaseException) -> None:
    """把没兜住的异常记下来。不记的话，用户只看到一句"卡了一下"，事后查不出原因。"""
    try:
        with open(os.path.join(DATA_DIR, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "channel": channel,
                        "in": text[:200],
                        "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc()[-2000:],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def _log_jsonl(path: str, record: dict) -> None:
    """追加一行 JSON 到日志文件。失败静默 —— 记日志不该把主流程拖垮。

    2026-11-04（P1-4）抽出来：`empty_reply.log` 原来是裸 `open(...).write(...)`，
    既没截断也没兜异常。统一走这里，顺便保证"日志写失败不影响这一轮对话"。

    2026-11-04（P2-10）加锁：三个日志写者（请求线程 × N + 心跳 + 巩固 + 梳理）
    并发 append，超过 4KB 的记录在 Windows 上理论上可以交错成半行 —— 那一行
    就再也不是合法 JSON，读审计的地方会静默跳过。锁把窗口关掉。
    """
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
    except (TypeError, ValueError):
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def _log_v1(record: dict) -> None:
    """记一条 /v1 请求，方便事后对账（AstrBot 那条链路只有这里能看见）。"""
    _log_jsonl(os.path.join(DATA_DIR, "v1.log"), {"ts": time.time(), **record})


def _openai_reply(result: dict, model: str) -> dict:
    return {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.get("text", "")},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _client_is_loopback(self) -> bool:
        """来源是不是本机回环。只作为**第一道**门槛，不能当授权用。"""
        try:
            host = self.client_address[0]
        except (IndexError, TypeError):
            return False
        return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _v1_guard(self) -> bool:
        """`/v1/chat/completions` 的入口守卫。

        为什么单独一个（2026-11-04 T-08）
        --------------------------------
        `/v1` 是给 AstrBot 当"服务商"用的 OpenAI 兼容口，它之前**一条校验都没有**。
        危害不比 `/chat` 小：

          * 白嫖主人的 key 跑任意 LLM（`# 任务说明` / `黑话` 这类裸子串能直接命中
            `call_model_raw`，原样转发，`max_tokens` 还能给到 1200）；
          * 身份可伪造（见 `_openai_user_text` 的 T-08 修法，这一层已经堵了，
            但入口这层还得有）。

        三道门槛，缺一不可：
          ① 必须来自本机回环 —— 挡掉 DNS rebinding 之外的远端起手式；
          ② 必须 `Content-Type: application/json` —— 挡掉跨站"简单请求"免预检那条；
          ③ **如果**配了 `GATEWAY_V1_TOKEN`（或沿用 `GATEWAY_TOKEN`），必须带对。
             不强制默认开启，是因为 AstrBot 当前 provider 还没配 `custom_headers`；
             配了之后把 `require_token` 打开就是完整闭环（设置见 config 的 system 段）。

        Returns:
            通过返回 True；不通过时已经把响应写回去了。
        """
        if not self._client_is_loopback():
            self._send(403, {"error": "forbidden"}, cors=False)
            return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send(415, {"error": "unsupported media type: application/json required"},
                       cors=False)
            return False
        if _V1_REQUIRE_TOKEN:
            token = (self.headers.get("X-Gateway-Token") or "").strip()
            if not hmac.compare_digest(token, PET_TOKEN):
                self._send(401, {"error": "unauthorized"}, cors=False)
                return False
        return True

    def _require_token(self) -> bool:
        """校验桌宠 token。没通过就已经把 401 写回去了，调用方直接 return。

        为什么抽成方法（2026-11-04 修）：以前是**每条路由手抄四行**
        `token = ...; if not compare_digest(...)`，于是 `/admin/keys` 和
        `/admin/model` 这两条漏了——它们不只是"读配置"，`/admin/model` 会把
        钥匙串里当前选中的**真实 API key** 发往调用方指定的 base（探活），
        等于一次未鉴权请求就能骗走密钥。手抄模式注定会再漏，改成默认校验。
        """
        token = (self.headers.get("X-Gateway-Token") or "").strip()
        if not hmac.compare_digest(token, PET_TOKEN):
            self._send(401, {"ok": False, "error": "unauthorized"}, cors=False)
            return False
        return True

    def _send(self, code: int, payload: dict, cors: bool = True) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cors:
            # 后台页面在 8848，跨端口调这里。/chat 例外：那是桌宠专用入口，
            # 浏览器不该能直接打（见 do_POST 里的 token 校验）。
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8848")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8848")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._send(
                200,
                {
                    "ok": True,
                    "mode": ROLE.get("mode"),
                    "provider": ROLE.get("provider"),
                    "model": ROLE.get("model"),
                    # 数据目录是不是隔离的（2026-11-04 加）。
                    #
                    # 为什么只回布尔、不回路径：回路径等于把本机目录结构
                    # 告诉任何能访问 /health 的人，而 /health 是公开的。
                    #
                    # 为什么需要它：`tools/e2e_smoke.py` 会**真的写对话**。
                    # 2026-11-04 我跑了十几次，往生产库写了 48 条测试对话，
                    # 结果她之后把我测试时说的话当成了真实历史。
                    # 有了这个字段，测试工具能在开跑前就判断
                    # "现在连的是不是生产库"并拒绝执行。
                    #
                    # ⚠ 2026-11-04 第十八轮修：原来的判据是
                    # `abs(DATA_DIR) != abs(ROOT/data)` —— 但**在全新克隆里
                    # 这两个本来就是同一个**（没有 GATEWAY_DATA_DIR 时），
                    # 于是永远判 False 报"不是隔离"，把正常情况说成不正常。
                    #
                    # 真正该回答的问题是「**这个 data 目录是不是被
                    # GATEWAY_DATA_DIR 显式指定的**」，而不是"路径和某个值不同"。
                    "data_dir_isolated": bool(
                        os.environ.get("GATEWAY_DATA_DIR", "").strip()
                    ),
                },
            )
        elif self.path.startswith("/admin/model"):
            # ⚠ 必须带 token：`/admin/model` 的 POST 会拿真 key 去探活，
            # 泄漏链见 `_apply_role` → `_probe_role` → `_role_key` 的回落。
            if not self._require_token():
                return
            self._send(200, _role_payload())
        elif self.path.startswith("/admin/keys"):
            # ⚠ 必须带 token：返回的是钥匙串全貌（id / label / masked / active）。
            if not self._require_token():
                return
            self._send(200, {**_keys_payload(), **_role_payload()})
        elif self.path.startswith("/admin/identity"):
            # 身份卡（2026-11-08）：主人 QQ 号 + 桌宠名字。
            # 必须带 token —— 改主人号等于**换一个人拿全部特权**，
            # 改名字会进提示词和标头，都是高影响面的配置。
            if not self._require_token():
                return
            self._send(200, identity_payload())
        elif self.path.startswith("/admin/system-model"):
            # 系统层模型配置：和角色层的 `/admin/model` 对称的一张卡。
            # 也带桌宠 token —— 它决定系统层用哪个模型，改坏了心跳/巩固全哑。
            if not self._require_token():
                return
            self._send(200, system.system_model_payload())
        elif self.path.startswith("/admin/daily-review"):
            # 每日梳理期（P0-4）：后台「每日梳理期」卡片。
            # 只读状态也必须带 token —— 它会暴露"她最近在想什么"。
            if not self._require_token():
                return
            self._send(200, system.daily_review_payload())
        elif self.path.startswith("/admin/proactive-speak"):
            # 主动说话总开关：后台「主动说话」卡片。
            # 带 token —— 它控制她会不会**主动弹出气泡并出声**，
            # 不该让本机任意进程随手打开。
            if not self._require_token():
                return
            self._send(200, system.proactive_speak_payload())
        elif self.path.startswith("/admin/system"):
            if not self._require_token():
                return
            self._send(200, system.system_status())
        elif self.path.rstrip("/").endswith("/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": ROLE.get("model"),
                            "object": "model",
                            "owned_by": "xiaofeiyu",
                        }
                    ],
                },
            )
        elif self.path.startswith("/capabilities"):
            self._send(
                200,
                {
                    "proto": "dm.family/1",
                    "supports": ["fact", "frame"],
                    "limits": {"max_sentences": CFG["dialog"]["max_sentences"]},
                    "endpoint": {
                        "id": ROLE.get("provider", "local"),
                        "model": ROLE.get("model"),
                        "context": 10240,
                        "private": True,
                    },
                },
            )
        elif self.path.startswith("/audit"):
            # 2026-11-04（P1-5）：这条原来无鉴权，泄露最近 20 条决策元数据
            # （path / reason / status / latency / in_len / out_len、session），
            # 而且每次都把整个 audit.jsonl 读进内存再切片 —— 文件会一直长。
            if not self._require_token():
                return
            records = []
            if os.path.exists(AUDIT_PATH):
                # 尾读：只留最后 20 条，不再全量加载（文件无轮转，会越拖越慢）
                tail: collections.deque = collections.deque(maxlen=20)
                with open(AUDIT_PATH, encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            tail.append(line)
                for line in tail:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            self._send(200, {"records": records})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "bad json"})
            return

        if self.path.startswith("/admin/keys"):
            # ⚠ 必须带 token：能 add（写入任意 key 并设为 active）、能 delete（清空钥匙串）。
            if not self._require_token():
                return
            try:
                ok, message = _keys_action(payload)
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            self._send(
                200 if ok else 400,
                {"ok": ok, "message": message, **_keys_payload(), **_role_payload()},
            )
            return

        if self.path.startswith("/admin/model"):
            # ⚠ 必须带 token。这条是全项目最危险的一条：
            # 不带 key 时 `_role_key` 会回落到钥匙串里**当前选中的真 key**，
            # 然后贴着调用方给的 base 发出去（见 T-02 的纵深修复）。
            if not self._require_token():
                return
            try:
                ok, message = _apply_role(payload)
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            self._send(200 if ok else 400, {"ok": ok, "message": message, **_role_payload()})
            return

        # 系统层（DMN）的三个手动开关：后台「她现在的状态」「内心OS」两张卡在用。
        # 和 GET 一样要带桌宠 token —— 这几个都会花模型调用，不能随便谁都能戳。
        if self.path.startswith("/admin/proactive-speak"):
            # 主动说话总开关（2026-11-08）。热生效，不用重启。
            if not self._require_token():
                return
            ok, message = system.apply_proactive_speak(payload)
            self._send(200 if ok else 400, {
                "ok": ok, "message": message, **system.proactive_speak_payload(),
            })
            return

        if self.path.startswith("/admin/identity"):
            # 身份卡（2026-11-08）：改主人 QQ 号 / 桌宠名字。
            # 这是**改身份证**级别的操作：换 owner_ids 等于换一个人拿特权。
            if not self._require_token():
                return
            ok, message = apply_identity(payload)
            self._send(200 if ok else 400, {
                "ok": ok, "message": message, **identity_payload(),
            })
            return

        if self.path.startswith("/admin/system-model"):
            if not self._require_token():
                return
            ok, message = system.apply_system_model(payload)
            self._send(200 if ok else 400, {
                "ok": ok, "message": message, **system.system_model_payload(),
            })
            return

        if self.path.startswith("/admin/system"):
            if not self._require_token():
                return
            action = self.path.split("/admin/system", 1)[1].strip("/")
            if action == "tick":
                # 心跳要调模型，放后台线程，别把 HTTP 卡住
                threading.Thread(target=system.tick, daemon=True).start()
                self._send(200, {"ok": True, "message": "已经叫她想一次了，几秒后刷新看结果"})
                return
            if action == "consolidate":
                threading.Thread(target=system.consolidate, daemon=True).start()
                self._send(200, {"ok": True, "message": "开始巩固记忆，可能要几十秒"})
                return
            if action == "inner-os/clear":
                cleared = system.clear_inner_os()
                self._send(200, {"ok": True, "cleared": cleared,
                                 "message": f"清掉了 {cleared} 条内心OS"})
                return
            if action == "daily-review":
                # 手动跑一次梳理期（后台「每日梳理期」卡片）。要调模型，放后台线程。
                # **强制跑**：不经过 `_daily_review_should_run` 的四道闸 ——
                # 后台手动触发就是"我要现在看看她想什么"，闸门由人代替。
                threading.Thread(target=system.daily_review, daemon=True).start()
                self._send(200, {"ok": True, "message": "开始梳理，可能要几十秒"})
                return
            self._send(404, {"ok": False, "error": "not found"})
            return

        # OpenAI 兼容入口：让 AstrBot / 别的客户端直接把这里当服务商
        if self.path.rstrip("/").endswith("/chat/completions"):
            # ⚠ 2026-11-04（T-08）：这条入口原来**完全没有鉴权**，而它比 /chat 更危险——
            # /chat 至少要求 token，/v1 什么都不要求，于是：
            #   ① 任何一个本机进程都能借主人的 key 白跑 LLM（v1.log 里 396 条 learn_task 就是这么来的）；
            #   ② 说话人身份是从**用户正文**正则抠的，写一句 `Nickname: 主人` 就能拿到全部记忆。
            # 修法：本机来源 + 强制 JSON + 可选 token 三重。token 见下方 `_v1_token_ok`。
            if not self._v1_guard():
                return
            messages = payload.get("messages") or []
            # 身份收成一个对象（T-11）：`identity.is_owner` 会检查
            # `trusted_identity`，从正文伪造的 `Nickname: 主人` 不算数。
            identity = _openai_identity(messages)
            text, speaker, uid, group = _openai_user_text(messages)
            # 图片必须单独抠出来原样转发：_openai_user_text 只读 text 段，
            # 图会被丢掉，模型就只看到「[图片]」占位符（见 _extract_image_urls）。
            images = _extract_image_urls(messages)
            # 插件（meme_manager）塞在 system 里的表情包选图教学：抠出来带给模型，
            # 抠不到（桌宠那条路）就空着照旧走。
            # 【局部变量，不是模块全局】只在本次请求内有效，并发请求互不影响。
            meme_prompt = _extract_meme_prompt(messages)
            # LivingMemory 召回的长期记忆块（user 消息末尾）：同样抠出来带给模型，
            # 正文已经被 _strip_plugin_notes 切掉，记忆库保持干净。
            living_memory = _extract_memory_prompt(messages)
            # 插件注入的**临时上下文**（P0-2 shared_context / P0-4 memewiki）：
            # 在 meta/body 拆分之前先整段抽走，走 context 通道只给这一轮看。
            #
            # ⚠ 顺序要紧：`_openai_user_text` 会把这类块判成 meta 丢掉
            # （shared_context 借壳 `<system_reminder>`）或判成 body 后被
            # `_strip_plugin_notes` 截掉（`[梗百科`）。抽出来才有得给。
            # 抽走之后它们**不进 text**，所以也不会写进 turns 记忆。
            plugin_ctx = _extract_plugin_context(messages)
            # 唤醒插件注入的群聊前文只给这一轮看，不写记忆
            text, temp_ctx = _split_wake_context(text)
            # 主动对话插件（AstrBot Proactive Chat）发来的请求：
            # 它是一段**系统给自己的指令**（"你被授权发起一次主动消息"），
            # 不是主人说的话。必须分流，不能当 user 消息存进 turns。
            #
            # ⚠ 2026-11-09 修：这里以前完全没有处理，于是整段 419 字符的模板
            # 被当成 `role=user` 写进对话历史（生产库里有 14 条，横跨 10 天）。
            # 后果是她每轮都能读到"我被命令过要主动关心他"——
            # 一段系统指令变成了她人格的一部分。
            #
            # 正确处理不是"不记"（主人：主动对话确实是系统操作，
            # 被流水账记录下来是正常的），而是**记成系统操作**：
            # 写进 `system_log` 渠道（role=system），人翻流水账看得见，
            # 但不会被当成有人在说话。
            if _is_proactive_task(text):
                _handle_proactive_task(text, session)
                return
            # 两路临时上下文合流（都只进 build_messages 的 system 块）：
            # 唤醒前文在前，插件内容在后 —— 前者是"刚才群里怎么聊的"，
            # 后者是"这条消息相关的补充资料"，先场景后资料读起来更顺。
            if plugin_ctx:
                temp_ctx = f"{temp_ctx}\n\n{plugin_ctx}" if temp_ctx else plugin_ctx
            # 来源标识：群消息带群名，QQ 私聊写「私聊」，桌宠那条路没有 uid 就不加
            channel = _channel_label(group, uid, bool(temp_ctx))
            if not text and temp_ctx:
                # @ 了一下但没打字：也得回，不然插件那边等不到任何输出
                text = "（叫了你一下，没说话）"
            if not text and not images:
                # 既没字也没图：给句人话，别让调用方报错。
                # 有图就走下去 —— 以前这里把带图消息也挡了，等于图白发了。
                body = _openai_reply(
                    _as_spoken(
                        {"text": "嗯……这条我没太看明白，你再说一遍好不好。"}, ""
                    ),
                    str(payload.get("model") or ROLE.get("model")),
                )
                self._send(200, body)
                return
            # 记忆插件的「写记忆」内部请求：**必须排在 _is_learn_task 之前**。
            # 原因：正文里嵌着整段对话原文，而 LEARN_HINTS 里的 "黑话" 是裸子串，
            # 群聊里只要有人提过这个词，这条摘要请求就会被误判成学习任务、
            # 回一个 "[]"，插件照样存成空壳。先认# 任务说明 这个模板抬头，才不会抢错。
            if _is_memory_task(text):
                # 原样转发：不套人格、不清洗、不写 turns、不加 [语音]。
                # 插件模板末尾写着「直接输出JSON」，原样给它才答得对（见 MEMORY_TASK_HINTS）。
                answer = None
                try:
                    answer = _extract_json(call_model_raw(messages, max_tokens=1200))
                except Exception:  # noqa: BLE001 —— 转发失败也不能让插件拿到空串
                    answer = None
                if not answer:
                    # 兜底给**结构合法**的空结果：插件至少能过 json.loads，
                    # 不会掉回它自己那个 "对话记录" 默认值（那就是空壳的来源）。
                    answer = MEMORY_TASK_FALLBACK
                _log_v1({"stream": False, "in": text[:80], "out": answer[:80],
                         "status": "memory_task", "path": "raw"})
                self._send(
                    200,
                    _openai_reply(
                        {"text": answer},
                        str(payload.get("model") or ROLE.get("model")),
                    ),
                )
                return

            if _is_learn_task(text):
                # 学习插件的内部请求：原样转给模型，只回 JSON。
                # 它们读的是群聊原文，套人格既答不对也白花钱。
                answer = None
                try:
                    answer = _extract_json(call_model_raw(messages))
                except Exception:  # noqa: BLE001
                    answer = None
                if not answer:
                    answer = _learn_answer(text)
                _log_v1({"stream": False, "in": text[:80], "out": answer[:80],
                         "status": "learn_task", "path": "raw"})
                self._send(
                    200,
                    _openai_reply(
                        {"text": answer},
                        str(payload.get("model") or ROLE.get("model")),
                    ),
                )
                return
            if _is_meta(text):
                # 插件的辅助判断：回一个枚举词，不写记忆、不跑工具
                answer = _meta_label(text)
                _log_v1({"stream": False, "in": text[:80], "out": answer,
                         "status": "meta", "path": "meta"})
                self._send(
                    200,
                    _openai_reply(
                        {"text": answer},
                        str(payload.get("model") or ROLE.get("model")),
                    ),
                )
                return
            try:
                # 桌宠和 QQ 共用一份记忆；说话人标签让模型知道这句是谁说的。
                # 主人在 QQ 上说话也算主人，不然两边会变成两个人。
                #
                # T-11：这条判定改走 `identity.is_owner` —— 它要求
                # `trusted_identity`，正文里伪造的 `Nickname: 主人` 不再算数。
                # 以前这里是 `if uid and uid in OWNER_IDS`，而 uid 本身
                # 也可能是从正文抠出来的。
                if identity.is_owner and identity.speaker:
                    speaker = OWNER_LABEL
                # 当前这一轮也明说一次来源，不用翻历史才知道场合
                ctx = temp_ctx
                if channel:
                    hint = f"这条消息来自 QQ 的{channel}。"
                    ctx = f"{hint}\n\n{ctx}" if ctx else hint
                # QQ 这条路才需要语音说明：AstrBot 的插件认 [语音] 标记决定发不发。
                # 以前不告诉她，她就在被要求发语音时编「我出不了声」。
                ctx = f"{ctx}\n\n{VOICE_NOTE}" if ctx else VOICE_NOTE
                # 人格里写着「每次 1 到 3 句」，背诗也会照着只念三句就停。
                # 这种场合明确放开，不然永远只念开头。
                if RECITE_RE.search(text):
                    ctx += (
                        "\n\n[背诵要求]\n这次是要背诵或朗读，把内容完整念完再停，"
                        "不要像平时聊天那样只回一两句。语音会把整段念出来。"
                    )
                # local_pet=False：QQ 那条路一律只读自己这一路来源。
                # 即使 group/uid 都取不到（channel 为空串），channel_key 也会
                # 给一个不可信的 qq_unknown，而不是误判成桌宠。
                result = handle_chat(
                    text,
                    SHARED_SESSION,
                    speaker=speaker,
                    context=ctx,
                    channel=channel,
                    meme_prompt=meme_prompt,
                    living_memory=living_memory,
                    local_pet=False,
                    images=images,
                    # T-11：把解析好的身份一起传下去。
                    # 它带 `trusted_identity` —— 从正文伪造 `Nickname: 主人`
                    # 的请求在这里 `is_owner=False`，拿不到内心OS/心境，
                    # 也不能替主人确认写删。
                    identity=identity,
                )
            except Exception as exc:  # noqa: BLE001
                _log_error("v1", text, exc)
                self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}"}})
                return
            result = _as_spoken(result, text)
            # 把表情包标记拼回给插件（它收到后换成图片再清掉）。
            # 标记跟在正文句号后面，满足插件「独占一行」的解析要求；
            # 语音前缀在标记之前，插件清标记时不会被连走。
            markers = result.get("meme_markers") or []
            if markers:
                result["text"] = result.get("text", "") + "".join(
                    f"&&{k}&&" for k in markers
                )
            _log_v1({
                "stream": bool(payload.get("stream")),
                "in": text[:80],
                "out": result.get("text", "")[:80],
                "ch": channel,
                "meme": markers,
                "status": result.get("status"),
                "path": result.get("path"),
            })
            model = str(payload.get("model") or ROLE.get("model"))
            body = _openai_reply(result, model)
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                chunk = dict(body)
                chunk["object"] = "chat.completion.chunk"
                chunk["choices"] = [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": result.get("text", "")},
                        "finish_reason": None,
                    }
                ]
                self.wfile.write(
                    ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
                )
                done = dict(body)
                done["object"] = "chat.completion.chunk"
                done["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                self.wfile.write(
                    ("data: " + json.dumps(done, ensure_ascii=False) + "\n\n").encode("utf-8")
                )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            self._send(200, body)
            return

        if not self.path.startswith("/chat"):
            self._send(404, {"error": "not found"})
            return
        # 桌宠专用入口：必须带 token。没有它，任何本机进程（或一个网页）
        # 发两句 ——「把 X 写进 y.md」「好」—— 就能让主人名下的写操作落地。
        token = (self.headers.get("X-Gateway-Token") or "").strip()
        if not hmac.compare_digest(token, PET_TOKEN):
            self._send(401, {"error": "unauthorized"}, cors=False)
            return
        text = str(payload.get("text", "")).strip()
        if not text:
            self._send(400, {"error": "empty text"}, cors=False)
            return
        session = str(payload.get("session") or SHARED_SESSION)
        speaker = str(payload.get("speaker") or "")
        try:
            # 桌宠这条路没有插件注入的表情包教学，也没有 RAG 召回块：
            # 显式传空串，绝不能让它读到上一条 QQ 请求留下的残值（H3）。
            # local_pet=True：这是本机 8848 转过来的，可以读全部记忆。
            # 这个身份**只在本函数的 token 校验通过之后**才成立（以前是路由写死）。
            result = handle_chat(
                text,
                session,
                speaker=speaker,
                context="",
                channel="",
                meme_prompt="",
                living_memory="",
                local_pet=True,
                # T-11：桌宠那条路身份**天然可信**（token 已校验过），
                # 用 `identity_from_pet()` 显式表达这一点，而不是靠
                # `local_pet=True` 这个布尔在别处被推断。
                identity=identity_from_pet(speaker or OWNER_LABEL),
            )
        except Exception as exc:  # noqa: BLE001
            _log_error("chat", text, exc)
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"}, cors=False)
            return
        self._send(200, result, cors=False)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}")


def _port_already_serving(host: str, port: int, timeout: float = 1.0) -> bool:
    """端口上是不是已经有一个**网关**在服务？

    ⚠ 为什么不能只看 pid 文件 / `os.kill`（2026-11-04 P1-6 实测踩到）：
      * Windows 上 `os.kill(pid, 0)` 对**没权限的活进程**抛 `WinError 5`，
        对不存在的 pid 抛 errno 22 —— 单看异常区分不出来，很容易判成"没活"；
      * pid 文件在硬杀后会残留，内容还可能被前一个进程覆盖掉。

    最可靠的判据是**端口上能不能真的拿到 `/health`**：那是网关自己的响应。
    """
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/health", method="GET"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(200).decode("utf-8", "replace")
        return '"ok"' in body
    except Exception:  # noqa: BLE001 —— 连不上就是"没在服务"
        return False


def _acquire_single_instance_lock(pid_path: str) -> bool:
    """抢单实例锁。抢不到返回 False（调用方应该直接退出）。

    ⚠ 2026-11-04 加（P1-6）。原来的问题是：

      * `ThreadingHTTPServer` 默认 `allow_reuse_address = 1`，而 Windows 上
        `SO_REUSEADDR` 的语义和 Linux 不同 —— **第二个进程能绑同一个端口**，
        两个进程都"监听成功"（本机实测过）；
      * pid 文件是普通写（不是独占），后起的直接把前一个的 pid 覆盖掉；
      * `system.start()` 里三个线程（心跳/巩固/梳理）**没有任何单例保护**。

    三条加起来 = 起两份网关就变成**双心跳、双巩固、双梳理、重复计费**，
    而且 pid 文件只记得后一个，`stop.cmd` 只能停一半。

    判据按可靠性排序（前一条命中就够）：
      ① **端口上已经有网关在回 `/health`** → 真的有一份在跑（最硬的证据）；
      ② pid 文件里的进程还活着、且命令行确实是 gateway.py → 有一份在跑；
      ③ 都否 → 认定是陈旧残留，接管。

    Returns:
        True = 可以继续启动（已持有锁）；False = 已经有一份在跑。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    my_pid = os.getpid()

    def _looks_alive(pid: int) -> bool:
        """pid 还活着、并且确实是 gateway.py 吗？（只在 Windows 上查命令行）"""
        if pid <= 0 or pid == my_pid:
            return False
        try:
            import subprocess
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, timeout=5,
            ).stdout.decode("utf-8", "replace")
        except Exception:            # noqa: BLE001 —— 查不到就当**不**活着（本机进程查得到）
            return False
        return "gateway.py" in out

    # ① 端口探活：最硬的证据
    if _port_already_serving(CFG["host"], int(CFG["port"])):
        return False

    # ② pid 文件 + 命令行
    try:
        with open(pid_path, encoding="utf-8") as fh:
            old = int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        old = 0
    if old and _looks_alive(old):
        return False

    # ③ 接管：先删陈旧文件，再写自己的
    try:
        if os.path.exists(pid_path):
            os.remove(pid_path)
    except OSError:
        pass
    try:
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write(str(my_pid))
    except OSError:
        pass                          # 写不了 pid 文件不该拦住启动
    return True


def main() -> None:
    init_db()
    pid_path = os.path.join(DATA_DIR, "gateway.pid")
    if not _acquire_single_instance_lock(pid_path):
        print(
            f"gateway 已经有一份在跑了（{pid_path} 里的 pid 还活着）。\n"
            f"  两份网关会变成双心跳/双巩固/重复计费，这里直接退出。\n"
            f"  要重启就先跑 stop.cmd，或用后台的「重启」按钮。",
            flush=True,
        )
        sys.exit(3)
    server = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    print(f"gateway 已启动: http://{CFG['host']}:{CFG['port']}  pid={os.getpid()}")
    print(f"  角色层: {ROLE.get('model')} @ {ROLE.get('base')}  ({ROLE.get('mode')})")
    print(f"  模型: {ROLE.get('model')} @ {ROLE.get('base')}  ({ROLE.get('mode')})")
    system.start()   # 系统层：后台心跳线程（enabled=false 时是空操作）
    try:
        server.serve_forever()
    finally:
        if os.path.exists(pid_path):
            os.remove(pid_path)


if __name__ == "__main__":
    main()