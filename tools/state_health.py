#!/usr/bin/env python
"""出厂体检：一条命令确认「她的状态是干净的」。

为什么需要这个
--------------
2026-11-04 那天我自己踩了两个坑，都不在产品代码里，而在**我给产品留下的副作用**里：

  ① 临时脚本把 `novelty_grams.json` 写成 100 个测试残留（真实是 3851 个）
     → 所有真实对话被算成"新" → 每天误触发梳理 → 白花钱；
  ② `_harden_file_acl` 只给 `(R,W)` 不给 `D`
     → `keys.json.tmp`（含明文 key）删不掉，永久堆在 data/ 里。

这两个都是**跑单测查不出来的**：单测只看函数逻辑，不看"磁盘上现在是什么"。
所以这里把「数据文件的健康度」做成可复现的检查。

它查什么
--------
| 检查 | 为什么 |
|---|---|
| 有没有 `.tmp` / `.bak` 残留 | 残留的 keys.json.tmp **含明文 API key** |
| 敏感文件权限 | token / keys 不该让本机任何账户可读 |
| 新颖度基线合理性 | 太小 → 全是"新"（误触发）；太大 → 全是"旧"（永不触发） |
| 记忆索引一致性 | documents 与 faiss 劈开时**不报错**，召回只是悄悄变差 |
| 状态文件有没有测试残留键 | 我写坏过 novelty，同类问题要能一次看见 |
| 审计日志可解析率 | 半行 JSON 会被读取方静默跳过（等于记录丢了） |
| 数据库完整性 | `PRAGMA quick_check` |

用法
----
    python G:\\小鱼\\tools\\state_health.py          # 人看的
    python G:\\小鱼\\tools\\state_health.py --json    # 机器可读
    python G:\\小鱼\\tools\\state_health.py --fix     # 顺手清掉可安全清理的残留

**它只读，除非你给 `--fix`。** `--fix` 只删明确安全的东西（`.tmp`、孤儿 `.bak`），
不动任何真实数据。

退出码：0 = 全绿；1 = 有 warn；2 = 有 error。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = os.path.join(ROOT, "gateway")
DATA = os.path.join(GATEWAY, "data")

#: 含密文的文件（不该允许本机其它账户读取）
SECRET_FILES = ("gateway.token", "keys.json", "secrets.json")
#: 可以放心删的残留后缀
SAFE_TO_CLEAN = (".tmp",)
#: **永远不删**的备份：这些是"清理动作之前"留下的安全网。
#: 删了就没法回退 —— 而"能不能回退"比"目录干净"重要得多。
KEEP_BACKUPS = (
    ".bak-before-",
    "-before-cleanup",
    ".polluted-by-",
)

results: list[dict] = []


def report(level: str, area: str, msg: str, fix: str = "") -> None:
    """记一条检查结果。level ∈ ok/warn/error。"""
    results.append({"level": level, "area": area, "msg": msg, "fix": fix})
    icon = {"ok": "✅", "warn": "⚠️ ", "error": "❌"}.get(level, "  ")
    print(f"  {icon} [{area}] {msg}")
    if level != "ok" and fix:
        print(f"       → {fix}")


# ---------------------------------------------------------------- 残留文件
def check_leftovers(fix: bool) -> None:
    print("\n=== 残留文件 ===")
    if not os.path.isdir(DATA):
        # ⚠ 全新克隆/首次安装时 data/ 本来就不存在 —— 那不是故障。
        # 它是**还没跑过**。以前这里报 error，导致"刚 clone 下来跑体检"
        # 直接一片红，人会以为项目坏了。
        report("ok", "残留", "data 目录还没生成（项目还没跑过，正常）")
        return

    stray: list[str] = []
    keep: list[str] = []
    for name in sorted(os.listdir(DATA)):
        path = os.path.join(DATA, name)
        if not os.path.isfile(path):
            continue
        # ① 这些是**安全网**，永远不删、也不报 warn —— 它们是好事
        if any(k in name for k in KEEP_BACKUPS):
            keep.append(name)
            continue
        if name.endswith(SAFE_TO_CLEAN):
            stray.append(name)
        # ② 普通 .bak：只在"对应主文件存在"时才算残留（否则可能是唯一的备份）
        elif ".bak" in name:
            base = name.split(".bak")[0]
            if os.path.exists(os.path.join(DATA, base)):
                stray.append(name)

    if keep:
        report("ok", "残留",
               f"{len(keep)} 个「清理前备份」在（这些是安全网，别删）："
               + ", ".join(keep[:3]) + ("…" if len(keep) > 3 else ""))

    if not stray:
        report("ok", "残留", "没有 .tmp / 孤儿 .bak")
        return

    for name in stray:
        path = os.path.join(DATA, name)
        size = os.path.getsize(path)
        sensitive = any(s in name for s in SECRET_FILES)
        note = "（**含明文密钥**）" if sensitive else ""
        if fix:
            try:
                # ACL 可能缺 D（就是踩过的坑），先补权限再删
                subprocess.run(
                    ["icacls", path, "/grant", f"{os.environ.get('USERNAME','')}:F"],
                    capture_output=True, timeout=10,
                )
                os.remove(path)
                report("warn", "残留", f"已删除 {name}（{size}B）{note}")
            except OSError as exc:
                report("error", "残留",
                       f"删不掉 {name}（{size}B）{note}：{type(exc).__name__}",
                       f"手动: icacls \"{path}\" /grant %USERNAME%:F 然后 del")
        else:
            report("warn", "残留", f"{name}（{size}B）{note}",
                   "加 --fix 自动删")


# ---------------------------------------------------------------- 敏感文件权限
def check_secret_perms() -> None:
    print("\n=== 敏感文件权限 ===")
    if os.name != "nt":
        report("ok", "权限", "非 Windows，跳过 ACL 检查")
        return
    for name in SECRET_FILES:
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            continue
        try:
            r = subprocess.run(["icacls", path], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=10)
            out = r.stdout or ""
        except Exception as exc:  # noqa: BLE001
            report("warn", "权限", f"{name} 查不到 ACL：{type(exc).__name__}")
            continue
        # 这两个组能读/写 = 本机任何账户都能拿到内容
        leaky = [g for g in ("Authenticated Users", "BUILTIN\\Users", "Everyone")
                 if g in out and "(DENY)" not in out.split(g)[-1][:40]]
        if leaky:
            report("error", "权限",
                   f"{name} 对本机所有账户开放（{', '.join(leaky)}）",
                   f"icacls \"{path}\" /inheritance:r /grant:r \"%USERNAME%:(R,W,D)\" "
                   f"/grant:r \"SYSTEM:(F)\" /grant:r \"Administrators:(F)\"")
        else:
            report("ok", "权限", f"{name} 已收紧")


# ---------------------------------------------------------------- 新颖度基线
def check_novelty() -> None:
    print("\n=== 新颖度基线（T-12 的度量基础）===")
    path = os.path.join(DATA, "novelty_grams.json")
    if not os.path.exists(path):
        report("ok", "新颖度", "还没生成（项目还没跑过梳理期，正常）")
        return
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        grams = data.get("grams") or []
    except (OSError, json.JSONDecodeError) as exc:
        report("error", "新颖度", f"文件读不出来：{type(exc).__name__}",
               "基线坏了会导致重新学习：可删掉让它重建（会短暂把老话当新的）")
        return

    n = len(grams)
    size_kb = os.path.getsize(path) / 1024

    # 测试残留特征：全是 A_/B_/S0_ 这种前缀，或含明显的 probe 字样
    sample = grams[:200]
    looks_like_test = bool(sample) and all(
        (g.startswith(("A_", "B_", "S0_", "S1_")) or g == "test" or "probe" in g)
        for g in sample
    )
    if looks_like_test:
        report("error", "新颖度",
               f"基线是**测试残留**（{n} 个，样本 {sample[:3]}）",
               "从 turns 重建：删掉该文件后跑一次梳理，或用 tools 里的重建脚本")
        return

    if n == 0:
        report("warn", "新颖度", "基线为空",
               "下次梳理会把所有历史都算成新的一次；正常使用后会自己长起来")
    elif n < 200:
        report("warn", "新颖度",
               f"基线偏小（{n} 个，{size_kb:.1f} KB）",
               "太小 → 老话被当新 → 可能误触发梳理。确认是不是刚清过")
    else:
        report("ok", "新颖度", f"{n} 个 gram，{size_kb:.1f} KB")

    # 上限检查
    try:
        sys.path.insert(0, GATEWAY)
        import system  # noqa: PLC0415
        cap = int(getattr(system, "NOVELTY_MAX_GRAMS", 50000))
        pct = 100.0 * n / cap if cap else 0
        if pct >= 95:
            report("warn", "新颖度",
                   f"已到上限 {pct:.0f}%（{n}/{cap}）",
                   "超上限会滚动丢弃最旧的 gram → 很久以前的对话会重新变成'新'")
        else:
            report("ok", "新颖度", f"上限占用 {pct:.1f}%（{n}/{cap}）")
    except Exception as exc:  # noqa: BLE001
        report("warn", "新颖度", f"拿不到上限常量：{type(exc).__name__}")


# ---------------------------------------------------------------- 记忆索引
def check_memory_index() -> None:
    print("\n=== 长期记忆索引一致性 ===")
    try:
        sys.path.insert(0, GATEWAY)
        import memory_bridge  # noqa: PLC0415
        h = memory_bridge.MemoryBridge().health()
    except Exception as exc:  # noqa: BLE001
        report("warn", "记忆", f"读不到记忆库：{type(exc).__name__}: {exc}")
        return

    if h.get("unreadable"):
        report("error", "记忆", f"库在但读不出来：{h.get('error') or '未知'}")
        return
    docs = int(h.get("docs") or 0)
    vecs = int(h.get("vectors") or 0)
    orphan = int(h.get("orphan_vectors") or 0)
    missing = int(h.get("missing_vectors") or 0)

    if docs == 0 and vecs == 0:
        report("ok", "记忆", "还没有文档（正常使用后会自己长起来）")
        return
    if orphan or missing:
        report("error", "记忆",
               f"不一致：documents {docs} / vectors {vecs}"
               f"（孤儿 {orphan}，缺向量 {missing}）",
               "孤儿会白占 top-k 名额，让召回命中变差；"
               "用 memory_bridge 的 drop_orphan_vectors() 清")
    else:
        report("ok", "记忆", f"一致：documents {docs} == vectors {vecs}")


# ---------------------------------------------------------------- 状态文件
def check_state_files() -> None:
    print("\n=== 状态文件 ===")
    checks = [
        ("system_state.json", "system-state"),
        ("inner_os.json", "inner-os"),
        ("persona_sync.json", "persona-sync"),
    ]
    for name, area in checks:
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            report("ok", area, f"{name} 还没生成（正常）")
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            report("error", area, f"{name} 解析失败：{type(exc).__name__}",
                   "状态文件坏了会导致水位/心境丢失（不会崩，但会重复抽取）")
            continue
        if not isinstance(data, dict):
            report("warn", area, f"{name} 顶层不是对象")
            continue
        # 测试残留键：名字带 test / 下划线开头（我的临时脚本爱用这些）
        weird = [k for k in data if k.startswith("_") and k not in ("_meta",)]
        weird += [k for k in data if "test" in k.lower()]
        if weird:
            report("warn", area,
                   f"{name} 里有可疑键：{weird[:5]}",
                   "可能是测试或临时脚本写进去的；确认后手动清")
        else:
            report("ok", area, f"{name} 正常（{len(data)} 个键）")


# ---------------------------------------------------------------- 审计日志
def check_logs() -> None:
    print("\n=== 审计日志可解析率 ===")
    for name in ("system_audit.jsonl", "audit.jsonl", "v1.log"):
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            continue
        total = bad = 0
        size = os.path.getsize(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                # 只扫尾部 2MB，别把大日志全读进内存
                if size > 2 * 1024 * 1024:
                    fh.seek(size - 2 * 1024 * 1024)
                    fh.readline()          # 丢掉可能被截断的第一行
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    total += 1
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        bad += 1
        except OSError as exc:
            report("warn", "日志", f"{name} 读不了：{type(exc).__name__}")
            continue
        if total == 0:
            report("ok", "日志", f"{name} 空")
            continue
        pct = 100.0 * bad / total
        if bad == 0:
            report("ok", "日志", f"{name}：{total} 行全部可解析")
        elif pct <= 1:
            report("warn", "日志",
                   f"{name}：{bad}/{total} 行解析失败（{pct:.1f}%）",
                   "极少数可能是写日志时被硬杀；若持续增长则日志锁有问题")
        else:
            report("error", "日志",
                   f"{name}：{bad}/{total} 行解析失败（{pct:.1f}%）",
                   "读取方会静默跳过这些行 → 等于记录丢了")


# ---------------------------------------------------------------- 数据库
def check_db() -> None:
    print("\n=== 数据库完整性 ===")
    for name in ("gateway.db", "livingmemory.db"):
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            report("ok", "数据库", f"{name} 还没生成（正常）")
            continue
        try:
            uri = "file:" + path.replace("\\", "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                row = con.execute("pragma quick_check").fetchone()
                status = row[0] if row else "?"
                mode = con.execute("pragma journal_mode").fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error as exc:
            report("error", "数据库", f"{name} 打不开：{exc}")
            continue
        if status == "ok":
            extra = ""
            if name == "gateway.db":
                extra = f"，journal_mode={mode}"
                if mode != "wal":
                    report("warn", "数据库",
                           f"{name} 完整但 journal_mode={mode}（不是 wal）",
                           "并发写可能报 locked → 丢一条记忆；"
                           "检查 init_db 有没有开 WAL")
                    continue
            report("ok", "数据库", f"{name} quick_check ok{extra}")
        else:
            report("error", "数据库", f"{name} quick_check 失败：{status}",
                   "先备份再修：sqlite3 file.db '.recover'")


# ---------------------------------------------------------------- 汇总
def main() -> int:
    ap = argparse.ArgumentParser(description="出厂体检（只读，除非 --fix）")
    ap.add_argument("--json", action="store_true", help="机器可读")
    ap.add_argument("--fix", action="store_true",
                    help="顺手清掉可安全清理的残留（.tmp / 孤儿 .bak）")
    args = ap.parse_args()

    if not args.json:
        print("=== 出厂体检 · 她的状态干净吗 ===")
        print(f"  数据目录：{DATA}")

    check_leftovers(args.fix)
    check_secret_perms()
    check_novelty()
    check_memory_index()
    check_state_files()
    check_logs()
    check_db()

    errors = [r for r in results if r["level"] == "error"]
    warns = [r for r in results if r["level"] == "warn"]
    oks = [r for r in results if r["level"] == "ok"]

    if args.json:
        print(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "summary": {"ok": len(oks), "warn": len(warns), "error": len(errors)},
            "results": results,
        }, ensure_ascii=False, indent=2))
    else:
        print()
        print(f"=== 汇总：{len(oks)} 项正常，{len(warns)} 项注意，{len(errors)} 项错误 ===")
        if errors:
            print("\n需要处理的错误：")
            for r in errors:
                print(f"  ❌ [{r['area']}] {r['msg']}")
                if r["fix"]:
                    print(f"      {r['fix']}")
        if warns:
            print("\n可以留意的：")
            for r in warns:
                print(f"  ⚠️  [{r['area']}] {r['msg']}")
                if r["fix"]:
                    print(f"      {r['fix']}")
        if not errors and not warns:
            print("\n🎉 她的状态是干净的。")

    return 2 if errors else (1 if warns else 0)


if __name__ == "__main__":
    sys.exit(main())
