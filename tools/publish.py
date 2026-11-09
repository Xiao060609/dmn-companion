# -*- coding: utf-8 -*-
"""publish.py —— 发布到公开仓库（同步 + 自动脱敏 + 校验）。

用法：
    python tools/publish.py            # 同步 + 脱敏 + 检查，不提交
    python tools/publish.py --commit   # 追加 git add/commit

为什么要有这个脚本
------------------
之前每次发布都是「手工挑文件 + 手工脱敏 + 手工检查」，做完了脚本就删，
下次重来。而实际踩过两次坑：

  1. `git add -A` 把 Chrome 临时配置目录收了进去（220 个文件，
     含 Cookies / Login Data）；
  2. 同步时 `.gitignore` 被源项目的旧版本覆盖，`audit/_private/`
     的忽略规则丢了，含真实聊天记录的文档差点被提交。

所以把「同步 → 脱敏 → 校验」三步固化成脚本，漏一步就不让过。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: 源项目目录（含素材/依赖的那份）
SRC = Path(r"<repo>")
#: 公开仓库目录（本仓库）
DST = Path(__file__).resolve().parents[1]

#: 公开仓库专有，同步时绝不覆盖
KEEP = {
    "README.md", "LICENSE", "THIRD-PARTY-NOTICES.md", "WORKFLOW.md",
    "requirements.txt", "requirements-local-embed.txt",
    "tools/check_privacy.py", "tools/publish.py",
}

#: 源项目有、但不该公开
SKIP = {
    "qq/部署记录.md",
    "具体场景演示效果.html",
    "启动.bat", "安装.bat", "_launch.vbs",
}

#: 脱敏规则（一一对应，保持文档内部自洽）
DESENSITIZE = [
    # 先长后短，避免前缀被短规则先吃掉
    ("示例用户", "示例用户"),
    ("部署记录.md", "部署记录.md"),
    ("示例用户", "示例用户"),
    ("\u0489X \u0489", "示例用户"),
    ("示例群", "示例群"),
    ("群友A", "群友A"),
    ("10000001", "10000001"),
    ("10000002", "10000002"),
    ("旧项目", "旧项目"),
]

#: 路径替换（正则，先长后短）
#:
#: ⚠ 2026-11-09 补：原来只覆盖 `<repo>`（本机主目录），漏了 `workplase` ——
#: 那也是个本机路径（工具/边车放的地方），审计文档里引了 45 处。
#: 同类路径还会出现新的，所以这里按「盘符 + 常见工作区名」兜底。
PATH_RULES = [
    (re.compile(r"[Gg]:[\\/]小鱼[\\/]qq[\\/]astrbot[\\/]\.venv", re.I), "<venv>"),
    (re.compile(r"[Gg]:[\\/]小鱼[\\/]"), "<repo>/"),
    (re.compile(r"[Gg]:[\\/]小鱼(?![\\/])"), "<repo>"),
    # 本机的工作区目录（工具、边车、下载等都在它下面）
    (re.compile(r"[A-Za-z]:[\\/]workplase[\\/]?", re.I), "<workspace>/"),
    # 用户主目录的常见写法
    (re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s`'\"]+", re.I), "<home>"),
]

TEXT_SUFFIX = {
    ".py", ".js", ".json", ".md", ".html", ".css", ".txt", ".bat", ".ps1",
    ".toml", ".cfg", ".ini", ".yml", ".yaml", ".sh", ".cjs", ".mjs",
}

#: 必须始终存在的 .gitignore 规则（同步覆盖后要补回来）
REQUIRED_IGNORE = """

# ============================================================================
# 含真实聊天记录的原始文档，只留本地，绝不进公开仓库
# ============================================================================
audit/_private/
audit/_private/**

# 排障时生成的临时产物（Chrome 配置、截图）
audit/_chrome*
audit/_shot*
audit/_clean_*
audit/_*test*.py
"""


def run(cmd: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, encoding="utf-8")
    return r.stdout or ""


def sync_files() -> list[str]:
    """把源项目里该公开的文件同步过来，返回改动的文件列表。"""
    tracked = [x for x in run(
        ["git", "-C", str(SRC), "ls-files", "-z"]).split("\0") if x]
    # plan/ 全带上（源项目里未跟踪但属文档）
    for p in (SRC / "plan").glob("*.md"):
        tracked.append(str(p.relative_to(SRC)).replace("\\", "/"))

    changed = []
    for rel in tracked:
        if rel in KEEP or rel in SKIP:
            continue
        if rel.startswith(("audit/_", "tmp/")):
            continue
        s = SRC / rel
        if not s.is_file():
            continue
        d = DST / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.is_file():
            try:
                if d.read_bytes() == s.read_bytes():
                    continue
            except OSError:
                pass
        shutil.copy2(s, d)
        changed.append(rel)
    return changed


def desensitize() -> int:
    """对公仓里所有文本文件做脱敏。返回修改的文件数。"""
    n_files = 0
    for p in DST.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIX:
            continue
        if any(x in p.parts for x in
               (".git", "node_modules", ".venv", "__pycache__", "_private")):
            continue
        # 不脱敏检查器自己（它的规则里必然含敏感词）
        if p.name == "check_privacy.py":
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        orig = txt
        for a, b in DESENSITIZE:
            if a in txt:
                txt = txt.replace(a, b)
        for pat, rep in PATH_RULES:
            txt = pat.sub(rep, txt)
        if txt != orig:
            p.write_text(txt, encoding="utf-8", newline="")
            n_files += 1
    return n_files


def ensure_ignore_rules() -> bool:
    """保证 .gitignore 里有关键规则（同步可能把它覆盖成旧版）。"""
    gi = DST / ".gitignore"
    if not gi.is_file():
        gi.write_text(REQUIRED_IGNORE.lstrip(), encoding="utf-8")
        return True
    t = gi.read_text(encoding="utf-8")
    if "audit/_private" in t:
        return False
    gi.write_text(t.rstrip() + REQUIRED_IGNORE, encoding="utf-8", newline="")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="发布到公开仓库")
    ap.add_argument("--commit", action="store_true", help="自动 git add + commit")
    ap.add_argument("--message", "-m", default="", help="提交信息")
    args = ap.parse_args()

    print("=" * 60)
    print("1. 同步")
    print("=" * 60)
    changed = sync_files()
    print("   同步 %d 个文件" % len(changed))
    for rel in changed[:20]:
        print("     ", rel)
    if len(changed) > 20:
        print("      ...（还有 %d 个）" % (len(changed) - 20))

    print()
    print("=" * 60)
    print("2. 脱敏")
    print("=" * 60)
    n = desensitize()
    print("   处理 %d 个文件" % n)

    print()
    print("=" * 60)
    print("3. 补 .gitignore 规则")
    print("=" * 60)
    print("   %s" % ("已补" if ensure_ignore_rules() else "无需补"))

    print()
    print("=" * 60)
    print("4. 校验")
    print("=" * 60)
    r = subprocess.run(
        [sys.executable, str(DST / "tools" / "check_privacy.py")],
        capture_output=True, text=True, encoding="utf-8",
    )
    print("   " + (r.stdout or "").strip().split("\n")[0])
    if r.returncode != 0:
        print()
        print(r.stdout)
        print("!! 脱敏未通过，中止。")
        return 1

    # 确认私密文件不在暂存区
    ignored = run(["git", "check-ignore", "audit/_private"], cwd=DST).strip()
    if not ignored:
        print("   !! audit/_private 没有被忽略，中止")
        return 1
    print("   audit/_private 已被忽略 ✔")

    if args.commit:
        print()
        print("=" * 60)
        print("5. 提交")
        print("=" * 60)
        run(["git", "add", "-A"], cwd=DST)
        msg = args.message or "sync: 从主项目同步"
        subprocess.run(["git", "commit", "-m", msg], cwd=str(DST))
        print(run(["git", "log", "--oneline", "-1"], cwd=DST).strip())

    print()
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
