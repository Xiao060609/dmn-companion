# -*- coding: utf-8 -*-
"""提交前脱敏检查。

用法：
    python tools/check_privacy.py            # 检查工作区
    python tools/check_privacy.py --staged   # 只检查已暂存的

为什么要有这个脚本
------------------
这个项目是在一个真实的私人使用场景里长出来的：真实的 QQ 号、
真实的昵称、和真实的人之间的对话，都曾散落在代码和文档里。

公开前手工清理过一遍，但那是**一次性的**。以后每次改代码、
加测试、写文档，都可能重新把这些东西带回来 —— 而且
**一旦 push 就撤不回来**，git 历史里永远留着。

所以把检查做成脚本，每次提交前跑一次。有命中就返回非零退出码。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 要检查的文件类型
TEXT_SUFFIX = {
    ".py", ".js", ".json", ".md", ".html", ".css", ".txt", ".bat", ".ps1",
    ".toml", ".cfg", ".ini", ".yml", ".yaml", ".sh", ".cjs", ".mjs",
}

#: 这些目录不检查（依赖 / 素材 / 第三方 / 本地私有）
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    "assets", "vendor", "astrbot", "SnowLuma", "qq-bridge", "_归档",
    "dist", "build", "probe", "data", "tts_cache", "_private",
}

#: 命中即视为问题。每条都写清「为什么」。
#:
#: ⚠ 敏感词**不能字面量写在这里** —— 否则本文件会命中自己，
#: 每次运行都报一遍假警，久了就没人看这个脚本了。
#: 所以拆成片段拼接 + `\\u` 转义，让它「运行时能匹配、字面量搜不到」。
CHECKS: list[tuple[str, re.Pattern, str]] = [
    (
        "真实 QQ 号",
        re.compile(r"\b" + "3047" + "980857" + r"\b"),
        "主人的真实 QQ 号。公开后任何人都能用它冒充主人。"
    ),
    (
        "真实 QQ 号（bot）",
        re.compile(r"\b" + "1075" + "92691" + r"\b"),
        "机器人自己的 QQ 号。"
    ),
    (
        "真实昵称（特殊字符）",
        re.compile("\u0489"),   # 组合字符，你昵称里的那个符号
        "你 QQ 昵称里的特殊组合字符，能反查到账号。"
    ),
    (
        "真实昵称",
        re.compile("大" + "肥鱼"),
        "你用过的昵称。占位名请用「示例用户」。"
    ),
    (
        "真实群名",
        re.compile("网络" + "避难所"),
        "你所在的 QQ 群名。占位名请用「示例群」。"
    ),
    (
        "真实聊天记录",
        re.compile(r"\[[^\]\n]{1,40}/(?:aiocqhttp|bot)[^\]\n]*\]"),
        "这是插件日志格式的真实聊天原文，含发言人。"
    ),
    (
        "本机绝对路径",
        re.compile(r"[Gg]:[\\/]" + "小" + "鱼"),
        "暴露作者本机目录结构。请用 <repo>/ 或相对路径。"
    ),
    (
        "本机工作区路径",
        re.compile(r"[A-Za-z]:[\\/]workplase", re.I),
        "暴露作者本机的工具目录。请用 <workspace>/ 占位。"
    ),
    (
        "本机用户目录",
        re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s`'\"]{2,}", re.I),
        "暴露 Windows 用户名。请用 <home> 占位。"
    ),
    (
        "疑似 API 密钥",
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
        "看起来像真 key。测试用的假 key 请用 sk-test-xxx 这种明显形式。"
    ),
    (
        "私密目录内容",
        re.compile(r"audit[/\\]_private"),
        "这个目录里是含真实聊天记录的原始文档，不该被引用或复制。"
    ),
]

#: 扫描时要跳过的**本工具自身**：它的规则里必然含这些词。
#: `publish.py` 同理 —— 它包含脱敏规则和 .gitignore 模板。
SELF_SKIP = {"tools/check_privacy.py", "tools/publish.py"}

#: 允许的例外：这些具体字符串是刻意的测试假值
ALLOW_EXACT = {
    "sk-abcdefghijklmnopqrstuvwxyz0123456789",   # 字母表顺序，明显是假的
    "sk-abcdef1234567890",
}


def iter_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_SUFFIX:
            continue
        # 跳过本工具自身（它的规则里必然含这些词）
        if str(p.relative_to(ROOT)).replace("\\", "/") in SELF_SKIP:
            continue
        yield p


def check_file(path: Path) -> list[tuple[str, int, str, str]]:
    """返回 [(类别, 行号, 该行内容, 原因)]。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    out = []
    lines = text.splitlines()
    for label, pat, why in CHECKS:
        for m in pat.finditer(text):
            snippet = m.group(0)
            if any(a in text for a in ALLOW_EXACT if a == snippet):
                continue
            line_no = text[:m.start()].count("\n") + 1
            line = lines[line_no - 1].strip() if line_no <= len(lines) else ""
            # 假 key 放行
            if label == "疑似 API 密钥" and any(a in line for a in ALLOW_EXACT):
                continue
            out.append((label, line_no, line[:100], why))
    return out


def main() -> int:
    hits: dict[Path, list] = {}
    scanned = 0
    for p in iter_files():
        scanned += 1
        found = check_file(p)
        if found:
            hits[p] = found

    if not hits:
        print("✓ 扫描 %d 个文件，没有发现私人信息。" % scanned)
        print()
        print("  可以安全提交。")
        return 0

    total = sum(len(v) for v in hits.values())
    print("✗ 发现 %d 处问题，分布在 %d 个文件里。" % (total, len(hits)))
    print("=" * 70)
    for p, found in sorted(hits.items()):
        rel = p.relative_to(ROOT)
        print()
        print(rel)
        for label, line_no, line, why in found[:6]:
            print("  L%-5d [%s] %s" % (line_no, label, line))
            print("         原因：%s" % why)
        if len(found) > 6:
            print("         ...（还有 %d 处）" % (len(found) - 6))

    print()
    print("=" * 70)
    print("这些都**不要**提交。真实昵称换成「示例用户」，")
    print("群名换成「示例群」，QQ 号换成 10000001。")
    print()
    print("⚠ 已经 push 上去的东西删不掉 —— git 历史里永远留着。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
