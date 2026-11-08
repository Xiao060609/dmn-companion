"""hands：本地动手工具。

L0 策略在这里强制执行，不依赖模型判断：
- 路径必须落在允许的根目录里
- 敏感路径（.ssh / .env / secrets.json ...）一律拒绝
- 写操作用扩展名白名单，可执行文件不许写
- 删除、写入都需要人工审批（由网关发起）

任何越界都抛 PolicyDenied，调用方只能把它翻译成"不能做"，不能重试。
"""
from __future__ import annotations

import fnmatch
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as _fh:
    _HANDS = json.load(_fh).get("hands", {})


def _abs_root(value: str) -> str:
    """相对路径按服务目录解析，换机器不用改配置。"""
    return value if os.path.isabs(value) else os.path.join(ROOT, value)


ROOTS = [os.path.realpath(_abs_root(p)) for p in _HANDS.get("roots", ["sandbox"])]
DENY_GLOBS = [g.lower() for g in _HANDS.get(
    "deny_globs",
    [
        "*/.ssh", "*/.ssh/*", "*/id_rsa*", "*/id_ed25519*",
        "*/.env", "*/.env.*", "*/secrets.json", "*/credentials*",
        "*/.git/*", "*/appdata/*", "*/ntuser.dat", "*/cookies*",
        "*/desktop.ini",
    ],
)]
WRITE_EXT = {e.lower() for e in _HANDS.get("write_ext", [".txt", ".md", ".json", ".log", ".csv"])}
MAX_READ = int(_HANDS.get("max_read_bytes", 200_000))
MAX_LIST = int(_HANDS.get("max_list_entries", 50))


class PolicyDenied(Exception):
    """L0 拒绝。消息会原样变成对用户的解释。"""


def _norm(path: str) -> str:
    """归一化用于比较：解析 symlink / junction 后再比，否则可以拿链接绕过白名单。"""
    return os.path.realpath(path).replace("\\", "/").lower()


def resolve(path: str) -> str:
    """把用户给的路径解析成绝对路径，并检查是否越界。

    这里必须用 realpath 而不是 abspath：abspath 只做字符串层面的规范化，
    不解析 symlink / NTFS junction。只要在 sandbox 里放一个指向外部的 junction
    （`mklink /J link C:/Users/x/.ssh`），abspath 就会认为路径还在 sandbox 内，
    白名单形同虚设。realpath 会把链接解析成真实目标，越界就能被下面那道
    「必须落在 ROOTS 里」的检查拦住。
    """
    raw = os.path.expandvars(os.path.expanduser(str(path).strip().strip("'\"")))
    if not raw:
        raise PolicyDenied("没给路径")
    raw = raw.replace("\\", "/")
    # 相对路径：先按第一个根目录解析，再按服务目录解析，取真实存在的那个
    if not os.path.isabs(raw):
        # 模型习惯把根目录名也写进路径（sandbox/a.txt），剥掉再拼，
        # 否则会套成 sandbox/sandbox/a.txt，人去找文件时找不到
        for root in ROOTS:
            base = os.path.basename(root)
            if base and (raw == base or raw.startswith(base + "/")):
                raw = raw[len(base):].lstrip("/")
                break
        candidates = [os.path.join(root, raw) for root in ROOTS] + [os.path.join(ROOT, raw)]
        existing = [c for c in candidates if os.path.exists(c)]
        full = os.path.realpath(existing[0] if existing else candidates[0])
    else:
        full = os.path.realpath(raw)

    normalized = _norm(full)
    for pattern in DENY_GLOBS:
        if fnmatch.fnmatch(normalized, pattern):
            raise PolicyDenied("这个位置属于敏感路径，我不能碰")
    if not any(_norm(full) == _norm(root) or _norm(full).startswith(_norm(root) + "/") for root in ROOTS):
        raise PolicyDenied("这个位置不在我能访问的范围里")
    # NTFS 备用数据流（ADS）：`secrets.json:note.txt` 会被当成"一个叫
    # note.txt 的文件"，**绕过上面所有按路径匹配的黑名单**（它不以
    # `secrets.json` 结尾），但打开时写的其实是 secrets.json 的备用流。
    # 实测：`resolve("secrets.json:note.txt")` 当年 denied_by=[]，写入成功。
    #
    # 根白名单仍然有效（打不到 sandbox 外），所以这是"第二道闸形同虚设"，
    # 但四道闸里有一道是假的就不该留着。判定放在 realpath **之后**，
    # 因为 realpath 不处理流名，冒号会原样留下。
    if ":" in full:
        # 盘符（`C:\...`）是允许的，只否决"盘符之外的冒号"
        tail = full[2:] if len(full) > 1 and full[1] == ":" else full
        if ":" in tail:
            raise PolicyDenied("这个位置属于敏感路径，我不能碰")
    return full


def _check_ext(path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext not in WRITE_EXT:
        raise PolicyDenied(f"不能写 {ext or '无扩展名'} 这类文件")


def read_file(path: str) -> str:
    full = resolve(path)
    if not os.path.isfile(full):
        raise FileNotFoundError(full)
    size = os.path.getsize(full)
    if size > MAX_READ:
        raise PolicyDenied(f"文件太大（{size} 字节）")
    with open(full, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    # 压成一行：换行和多余空白会毁掉 TTS 的断句
    lines = [line.strip().rstrip("：:，,；; ") for line in content.splitlines() if line.strip()]
    body = "；".join(lines)
    body = " ".join(body.split())
    if len(body) > 120:
        body = body[:120] + "……后面还有"
    name = os.path.basename(full)
    return f"{name} 里写的是：{body}" if body else f"{name} 是空文件"


def list_dir(path: str) -> str:
    full = resolve(path)
    if not os.path.isdir(full):
        raise FileNotFoundError(full)
    names = sorted(os.listdir(full))[:MAX_LIST]
    if not names:
        return "这个文件夹是空的"
    shown = [name + "/" if os.path.isdir(os.path.join(full, name)) else name for name in names]
    return f"这里有 {len(names)} 项：{'、'.join(shown)}"


def write_file(path: str, content: str) -> str:
    full = resolve(path)
    _check_ext(full)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)
    return f"写好了 {os.path.basename(full)}，共 {len(content)} 个字"


def delete_file(path: str) -> str:
    full = resolve(path)
    if not os.path.exists(full):
        raise FileNotFoundError(full)
    if os.path.isdir(full):
        raise PolicyDenied("文件夹我还不删")
    os.remove(full)
    return f"删掉了 {os.path.basename(full)}"


READ_ACTIONS = {"read_file", "list_dir"}
WRITE_ACTIONS = {"write_file", "delete_file"}


def describe(action: str, args: dict) -> str:
    """审批话术。只描述将要发生什么，不解释技术细节。"""
    path = str(args.get("path", ""))
    name = os.path.basename(path.replace("\\", "/")) or path
    if action == "delete_file":
        return f"要我把 {name} 删掉吗？回一个「好」我就删。"
    if action == "write_file":
        return f"要我把内容写进 {name} 吗？回一个「好」我就写。"
    return f"要我做 {action} 吗？"


def execute(action: str, args: dict) -> str:
    """执行读类动作，返回一句给角色层转述的事实。"""
    if action == "read_file":
        return read_file(args.get("path", ""))
    if action == "list_dir":
        return list_dir(args.get("path", ""))
    raise PolicyDenied("这个动作不能直接执行")