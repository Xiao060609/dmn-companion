# -*- coding: utf-8 -*-
"""检查纯净版有没有落后于源项目。

## 为什么需要这个工具

同步是**手动**的：改完源项目要跑 `publish.py`，然后推纯净版。
没有任何机制保证人记得跑（没有 git hook、没有定时任务）。

后果是"静静落后"：源项目改了 20 个文件，纯净版还是旧的，
而 GitHub 上看不出任何异常 —— 只有对比才知道。

## 判断方法（关键：不能简单比字节）

纯净版的文本文件**本来就该和源项目不同** —— 它们是脱敏版。
所以直接 `read_bytes() != read_bytes()` 会把每个含人名的文件都算成"落后"。

正确做法：**把源文件按 publish 的规则脱敏，再和纯净版比。**
一致 = 已同步，不一致 = 真的落后。

## 用法

    python tools/check_sync.py          # 只看，不改
    python tools/check_sync.py --quiet  # 只在落后时输出（给脚本用）

返回码：0 = 已同步，1 = 有落后（或读取出错）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import publish  # noqa: E402


def _scrub(text: str) -> str:
    """按 publish 的规则脱敏一段文本。"""
    for src, dst in publish.DESENSITIZE:
        text = text.replace(src, dst)
    for pat, rep in publish.PATH_RULES:
        text = pat.sub(rep, text)
    return text


def _tracked(root: Path) -> list[str]:
    """git 跟踪的文件列表（那才是发布出去的东西）。"""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        capture_output=True, text=True, encoding="utf-8",
    )
    return [x.strip() for x in out.stdout.splitlines() if x.strip()]


def _skipped(rel: str) -> bool:
    """发布时会跳过的路径（和 sync_files 同一个口径）。"""
    return rel in publish.KEEP or rel in publish.SKIP or \
        rel.startswith(("audit/_", "tmp/"))


#: 纯净版**本来就该和源项目不同**的文件。
#:
#: `.gitignore`：纯净版多了 `audit/_private/` 等只在公开仓才有意义的规则。
#: 这些规则不该回流到源项目（源项目里 `audit/_private` 是真在用的目录）。
#:
#: `tools/publish.py` / `tools/check_privacy.py` / `tools/check_sync.py`：
#: 这类工具源码里必然含敏感字面量（它们就是干脱敏的），publish 的
#: NO_TOUCH 会跳过它们，所以两边内容本来就该一致 —— 但 `.gitignore`
#: 不一样，它是**有意分叉**的。
_EXPECTED_DIVERGENCE = {".gitignore"}


def check() -> dict:
    """对比两边，返回落后明细。

    Returns:
        `{"behind": [(rel, why)], "missing": [rel], "checked": n}`。
    """
    src, dst = publish.SRC, publish.DST
    missing: list[str] = []
    behind: list[tuple[str, str]] = []
    checked = 0

    for rel in _tracked(src):
        if _skipped(rel):
            continue
        if rel.replace("\\", "/") in _EXPECTED_DIVERGENCE:
            continue
        s = src / rel
        if not s.is_file():
            continue
        d = dst / rel
        checked += 1

        if not d.is_file():
            missing.append(rel)
            continue

        # 二进制文件：只能比字节
        if s.suffix.lower() not in publish.TEXT_SUFFIX:
            try:
                if s.read_bytes() != d.read_bytes():
                    behind.append((rel, "二进制内容不同"))
            except OSError:
                behind.append((rel, "读取失败"))
            continue

        try:
            stxt = s.read_text(encoding="utf-8")
            dtxt = d.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            behind.append((rel, "读取失败"))
            continue

        # 核心：脱敏后再比
        if _scrub(stxt) != dtxt:
            behind.append((rel, "内容落后"))

    return {"behind": behind, "missing": missing, "checked": checked}


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    res = check()

    behind, missing = res["behind"], res["missing"]
    total = len(behind) + len(missing)

    if total == 0:
        if not quiet:
            print("✓ 纯净版已同步（检查 %d 个文件）" % res["checked"])
        return 0

    print("!! 纯净版落后了：%d 个文件" % total)
    print("   跑 `python tools/publish.py` 同步，然后提交推送。")
    print()
    for rel in missing:
        print("  [缺] %s" % rel)
    for rel, why in behind[:30]:
        print("  [旧] %-56s %s" % (rel, why))
    if len(behind) > 30:
        print("  ... 还有 %d 个" % (len(behind) - 30))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
