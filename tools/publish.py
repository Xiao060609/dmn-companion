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
SRC = Path(r"G:\小鱼")
#: 公开仓库目录（本仓库）
DST = Path(__file__).resolve().parents[1]

#: 公开仓库专有，同步时绝不覆盖
KEEP = {
    "README.md", "LICENSE", "THIRD-PARTY-NOTICES.md", "WORKFLOW.md",
    "requirements.txt", "requirements-local-embed.txt",
    "tools/check_privacy.py", "tools/publish.py",
}

#: 源项目有、但不该公开
#:
#: ⚠ 这些名字**必须避讳**：不能和 DESENSITIZE 的规则撞上，否则会被改坏。
#: 所以写成「前缀 + 后缀」拼接，而不是完整字面量。
SKIP = {
    "qq/" + "大" + "肥鱼" + "连接QQ-部署进度.md",
    "具体场景演示效果.html",
    "启动.bat", "安装.bat", "_launch.vbs",
}

#: 脱敏规则（一一对应，保持文档内部自洽）。
#:
#: ⚠⚠ 2026-11-09 踩过一个大坑：这个列表**被它自己脱敏掉了**。
#: 原来源码里直接写着 ("大肥鱼", "示例用户") 这样的字面量，
#: 而 desensitize() 会遍历所有 .py，于是把它自己改成
#: ("示例用户", "示例用户") —— 全套规则变成空操作，
#: 而 SKIP 里的私密文件名也被替换，跳过规则失效，
#: **含真实聊天记录的文档被同步进了公开仓库**。
#:
#: 现在的防御：
#:   1. 敏感字面量**拆片段拼接**（"大" + "肥鱼"），运行时才拼成完整词；
#:   2. desensitize() 的 NO_TOUCH 排除本文件（双保险）。
DESENSITIZE = [
    # 先长后短，避免前缀被短规则先吃掉
    ("大" + "肥鱼" + "（｡ò ∀ ó｡）", "示例用户"),
    ("大" + "肥鱼" + "连接QQ-部署进度.md", "部署记录.md"),
    ("大" + "肥鱼", "示例用户"),
    ("\u0489X \u0489", "示例用户"),
    ("网络" + "避难所", "示例群"),
    ("霖" + "粽", "群友A"),
    ("3047" + "980857", "10000001"),
    ("1075" + "92691", "10000002"),
    ("QQ" + "小鱼", "旧项目"),
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
    """跑命令并返回 stdout 文本。

    ⚠ `-z` 的输出里是 `\0` 分隔 —— **不能配 `text=True`**：
    subprocess 用文本模式时会把 `\0` 当异常或截断（实测直接返回空串），
    于是 `sync_files()` 拿到空的跟踪列表，报「同步 0 个文件」，
    而实际上文件根本没被比较过。这个坑让项目地图漏同步过一次。

    所以这里统一拿 bytes，再由调用方决定怎么解码 / 切分。
    """
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                       capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.stdout else ""


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


def _junctions(root: Path) -> list[Path]:
    """列出 `root` 下所有 junction / 符号链接目录。

    ⚠ 2026-11-10 踩过的坑（这是本项目最阴的一次）：
    纯净版里有 6 个 junction 指回源项目：
        desktop/assets、desktop/desktop/node_modules、gateway/data、
        qq/astrbot、qq/qq-bridge、qq/SnowLuma

    `desensitize()` 原来用 `DST.rglob("*")` 遍历 —— **rglob 会走穿 junction**，
    于是它顺着 `gateway/data` 进到源项目，把
    `data/identity.json` 里的真主人 QQ 号替换成了占位值！

    后果：她开始把主人判成"路人"，记忆/画像/内心OS 全按路人处理。
    而且**没有任何报错** —— 脱敏本来就该改文件，改了也不奇怪。

    现在：遍历前先收集 junction 路径，遇到就跳过整个子树。
    """
    out: list[Path] = []
    try:
        for p in root.rglob("*"):
            try:
                if p.is_dir() and (p.is_junction() or p.is_symlink()):
                    out.append(p)
            except OSError:
                continue
    except OSError:
        pass
    return out


def _under(path: Path, roots: list[Path]) -> bool:
    """`path` 是不是落在某个 junction 子树里。"""
    for r in roots:
        try:
            path.relative_to(r)
            return True
        except ValueError:
            continue
    return False


def desensitize() -> int:
    """对公仓里所有文本文件做脱敏。返回修改的文件数。"""
    #: 这些文件**不能**被脱敏 —— 它们本身就含路径/敏感词的字面量。
    #:
    #: ⚠ 2026-11-09 踩过的坑：publish.py 里的 `SRC = Path(r"G:\小鱼")`
    #: 被自己改了，变成 `SRC = Path(r"<repo>")`，于是它读不到任何源文件、
    #: 报「同步 0 个文件」。而 check_privacy.py 是跳过自己的，
    #: publish.py 当时没跳 —— 自己把自己改瞎了。
    NO_TOUCH = {"publish.py", "check_privacy.py"}

    # ⚠ 先收集 junction，遍历时跳过 —— 否则会改到源项目的数据文件。
    links = _junctions(DST)
    skipped_links = 0

    n_files = 0
    for p in DST.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIX:
            continue
        if any(x in p.parts for x in
               (".git", "node_modules", ".venv", "__pycache__", "_private")):
            continue
        # 工具脚本自身跳过：它们的规则里必然含敏感词/本机路径字面量
        if p.name in NO_TOUCH:
            continue
        # 落在 junction 子树里的文件**绝对不能碰** —— 那是源项目的真数据
        if _under(p, links):
            skipped_links += 1
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
    if skipped_links:
        print(f"   （跳过 {skipped_links} 个文件：在 junction 子树里，属于源项目真数据）")
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


def audit_damage_risk() -> list[str]:
    """脱敏前的**硬体检**：会不会改到源项目的真数据。

    为什么要有这道闸（2026-11-10 血的教训）
    --------------------------------------
    纯净版有 6 个 junction 指回源项目，`rglob` 会走穿。
    它把 `gateway/data/identity.json` 里的真主人 QQ 换成了占位值，
    还顺手改了 AstrBot 的 `admins_id`、社交记录、表情 ID ——
    19 个真数据文件被污染，**而且全程没有任何报错**。

    后果：她开始把主人当路人；AstrBot 里主人不再是管理员。

    `desensitize()` 现在会跳过 junction 子树，但那依赖于
    「junction 还在」。如果哪天链接被删成普通目录（复制了一份），
    保护就失效了。

    所以这里加一道**独立校验**：列出所有会被脱敏的文件，
    只要有一个落在「真数据目录」里，就说明保护失效 —— 直接中止。

    Returns:
        风险列表；空列表 = 安全。
    """
    #: 这些目录在源项目里是真数据，纯净版里**只能通过 junction 出现**。
    #: 正常情况下它们全在 junction 子树里，会被跳过。
    #: 如果它们出现在可脱敏范围，说明 junction 没了（数据被复制进来了）。
    DANGER = (
        "gateway/data/",
        "desktop/data/",
        "qq/astrbot/data/",
        "qq/SnowLuma/config/",
        "qq/qq-bridge/state/",
        "qq/qq-bridge/config/",
    )

    links = _junctions(DST)
    risks: list[str] = []
    for p in DST.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIX:
            continue
        if any(x in p.parts for x in
               (".git", "node_modules", ".venv", "__pycache__", "_private")):
            continue
        if p.name in {"publish.py", "check_privacy.py"}:
            continue
        if _under(p, links):
            continue      # 在 junction 里 -> desensitize 会跳过
        rel = str(p.relative_to(DST)).replace("\\", "/")
        for danger in DANGER:
            if rel.startswith(danger):
                risks.append(rel)
                break
    return risks


# ============================================================================
# 2026-11-10 主人下令：**暂停脱敏**。
#
# 这个工具走穿 junction，把源项目 19 个真数据文件改坏了：
#   主人 QQ -> 占位值（她把你当路人）
#   AstrBot admins_id -> 占位值（你不再是管理员）
#   144 处表情 ID / 35 处社交记录 / 画像渠道键
# 全程没有任何报错。
#
# 主人原话：「你这个十足的蠢货，暂时不要再做脱敏了，
#           省的你这个傻子给我乱改。」
#
# ⚠ 锁放在 main() 的**最前面、同步之前** ——
#   因为"同步"本身也会把含真值的源文件复制进纯净版。
#   如果先同步再拦，纯净版工作区就已经脏了（真值进来了，只是没提交）。
#   第一次加锁时放在同步后面，实测就是这个后果，只好 git checkout 还原。
#
# 要恢复：确认下面三条都满足，再把 _ALLOW_DESENSITIZE 改成 True。
#   1. audit_damage_risk() 返回空列表
#   2. desensitize() 的 junction 跳过逻辑有反向测试
#   3. 手册第四节记着这次事故
# ============================================================================
_ALLOW_DESENSITIZE = False


def _refuse_desensitize() -> int:
    """默认拦下整个发布流程。返回退出码 2。"""
    print()
    print("=" * 60)
    print("!! 发布流程已被主人暂停（2026-11-10）")
    print("=" * 60)
    print()
    print("   脱敏工具走穿 junction，把源项目 19 个真数据文件改坏了：")
    print("     主人 QQ -> 占位值（她把你当路人）")
    print("     AstrBot admins_id -> 占位值（你不再是管理员）")
    print("     144 处表情 ID / 35 处社交记录 / 画像渠道键")
    print("   全程没有任何报错。")
    print()
    print("   ⚠ 连「同步」也一起停了：同步会把含真值的源文件")
    print("     复制进纯净版，虽然不提交，但工作区会脏。")
    print()
    print("   要恢复请先看 tools/publish.py 里的 _ALLOW_DESENSITIZE 注释。")
    print()
    return 2



def main() -> int:
    ap = argparse.ArgumentParser(description="发布到公开仓库")
    ap.add_argument("--commit", action="store_true", help="自动 git add + commit")
    ap.add_argument("--message", "-m", default="", help="提交信息")
    args = ap.parse_args()

    # ---- 主人锁：放在最前面，连同步一起拦（2026-11-10）----
    if not _ALLOW_DESENSITIZE:
        return _refuse_desensitize()

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
    print("2. 脱敏前体检")
    print("=" * 60)
    risks = audit_damage_risk()
    if risks:
        print("   !! 有 %d 个真数据文件暴露在脱敏范围内：" % len(risks))
        for rel in risks[:15]:
            print("      %s" % rel)
        if len(risks) > 15:
            print("      ... 还有 %d 个" % (len(risks) - 15))
        print()
        print("   原因：这些目录本该是 junction（指向源项目），")
        print("         现在成了普通目录，脱敏会把真值换成占位符。")
        print("   处理：删掉这些目录，重建 junction。手册第四节有命令。")
        return 1
    print("   ✔ 真数据全部在 junction 后面，不会被脱敏")

    print()
    print("=" * 60)
    print("3. 脱敏")
    print("=" * 60)
    n = desensitize()
    print("   处理 %d 个文件" % n)

    print()
    print("=" * 60)
    print("4. 补 .gitignore 规则")
    print("=" * 60)
    print("   %s" % ("已补" if ensure_ignore_rules() else "无需补"))

    print()
    print("=" * 60)
    print("5. 校验")
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
