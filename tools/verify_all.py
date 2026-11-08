#!/usr/bin/env python
"""一键验收：把四个工具串起来，只看一个结果。

为什么需要这个
--------------
2026-11-04 这一天下来，`tools/` 里有四个独立的验收工具：

| 工具 | 回答什么 | 退出码 |
|---|---|---|
| `acceptance_matrix.py` | 前面那些任务的成果还在吗 | 0=全过 / 1=有失败 |
| `state_health.py`      | 她的数据干净吗 | 0=全绿 / 1=warn / 2=error |
| `e2e_smoke.py`         | 链路通、隔离对吗 | 0=全过 / 1=失败 / 2=服务没起 / 3=生产库被拒 |
| `m6_acceptance.py`     | 三段对话达成没有 | 0=全中 / 1=未全中 |

**四套退出码语义不一致**，直接 `&&` 串起来会得出错误结论
（比如 m6 返回 1 是"还没到能判的时候"，不是"坏了"）。

这个脚本做的就是：统一语义、按依赖排序、给出**一句结论**。

用法
----
    python G:\\小鱼\\tools\\verify_all.py              # 全跑（默认跳过要服务的）
    python G:\\小鱼\\tools\\verify_all.py --with-e2e    # 连 e2e 也跑（需服务在）
    python G:\\小鱼\\tools\\verify_all.py --quick      # 只跑零成本的两个
    python G:\\小鱼\\tools\\verify_all.py --json

退出码统一成三种：
    0 = 全部通过（或有"还没到判的时候"这类非失败项）
    1 = 有真正的失败
    2 = 有需要人看一眼的警告（数据不干净等）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
PY = sys.executable

#: 每一项 = (名字, 脚本, 参数, 是否需要活服务, 退出码含义)
#:
#: `meaning` 把各工具自己的退出码翻译成统一等级：
#:   ok    = 通过
#:   warn  = 需要人看一眼（不是坏了）
#:   fail  = 真失败
#:   skip  = 条件不满足，跳过（不算失败）
STEPS = [
    {
        "name": "数据健康",
        "script": "state_health.py",
        "args": [],
        "needs_service": False,
        "cost": "零成本",
        # state_health: 0=全绿 1=warn 2=error
        "meaning": {0: "ok", 1: "warn", 2: "fail"},
        "why": "残留文件 / 敏感文件权限 / 新颖度基线 / 记忆索引 / 日志可解析率 / SQLite",
    },
    {
        "name": "验收矩阵",
        "script": "acceptance_matrix.py",
        "args": [],
        "needs_service": False,
        "cost": "零成本",
        # acceptance_matrix: 0=全过 1=有失败
        "meaning": {0: "ok", 1: "fail"},
        "why": "T-01~T-12 + P1×11 的验收条件是否仍然成立",
    },
    {
        "name": "端到端冒烟",
        "script": "e2e_smoke.py",
        "args": ["--no-model"],
        "needs_service": True,
        "cost": "零成本（--no-model）",
        # e2e_smoke: 0=全过 1=失败 2=服务没起 3=生产库被拒
        "meaning": {0: "ok", 1: "fail", 2: "skip", 3: "skip"},
        "why": "鉴权 / 跨站守卫 / 渠道隔离 / 身份伪造",
    },
    {
        "name": "三段对话",
        "script": "m6_acceptance.py",
        "args": [],
        "needs_service": False,
        "cost": "零成本（只读库和审计）",
        # m6: 0=三段全中 1=未全中
        # ⚠ 1 不等于"坏了" —— 它可能只是"还没用够天数"。
        # 所以这里映射成 warn 而不是 fail，真正的判定交给输出里的"数据不足"提示。
        # 退出码 3 = 还没有对话库（全新克隆/首次安装），归到 skip
        "meaning": {0: "ok", 1: "warn", 3: "skip"},
        # 输出里出现这句 = "还没到能判的时候"，不是"没做到"。
        # 两者的区别很重要：前者该等，后者该查。
        "pending_marker": "数据不足，暂时不能判",
        "why": "A 时间感 / B 接得上昨天 / C 数得清天数（需要真实使用几天）",
    },
]


def _run(script: str, args: list[str], timeout: int = 600) -> tuple[int, str]:
    """跑一个工具，返回 (退出码, 输出)。超时按失败处理。"""
    path = os.path.join(TOOLS, script)
    if not os.path.exists(path):
        return 127, f"找不到 {path}"
    try:
        r = subprocess.run(
            [PY, path, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=ROOT, timeout=timeout,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return 125, f"{type(exc).__name__}: {exc}"


def _service_up() -> tuple[bool, list[str]]:
    """8898 / 8848 都活着吗。返回 (是否齐, 缺哪些)。"""
    import urllib.request

    missing: list[str] = []
    for name, url in (("8898 网关", "http://127.0.0.1:8898/health"),
                      ("8848 桌宠", "http://127.0.0.1:8848/api/status")):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status != 200:
                    missing.append(name)
        except Exception:  # noqa: BLE001
            missing.append(name)
    return (not missing), missing


def _summary_line(level: str, out: str) -> str:
    """从工具输出里捞它自己的结论行（比我重新算更可信）。

    各工具的输出格式不一样：
      * state_health / acceptance_matrix → `=== 汇总：... ===`
      * m6_acceptance → `🎉 三段全部命中` 或 `⬜ 还没全中`
      * e2e_smoke → `🎉 端到端冒烟全过` 或 `❌ 有检查没过`
    """
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # 优先找 "=== 汇总" 那种
    for line in reversed(lines):
        if line.startswith("===") and ("汇总" in line or "项通过" in line):
            return line.strip("= ").strip()
    # 再找 emoji 结论行
    for line in reversed(lines):
        if line.startswith(("🎉", "⬜", "❌ 有", "⚠ **")):
            return line
    # 最后找"数据不足"这类关键提示
    for line in lines:
        if "数据不足" in line or "暂时不能判" in line:
            return line
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="一键验收（串起四个工具）")
    ap.add_argument("--with-e2e", action="store_true",
                    help="连端到端冒烟也跑（需要 8848 + 8898 在跑）")
    ap.add_argument("--quick", action="store_true",
                    help="只跑零成本、不需要服务的两个")
    ap.add_argument("--json", action="store_true", help="机器可读")
    args = ap.parse_args()

    steps = STEPS
    if args.quick:
        steps = [s for s in STEPS if not s["needs_service"] and s["script"] != "m6_acceptance.py"]
    if not args.with_e2e:
        steps = [s for s in steps if s["script"] != "e2e_smoke.py"]

    service_ok = True
    missing: list[str] = []
    if any(s["needs_service"] for s in steps):
        service_ok, missing = _service_up()

    results = []
    if not args.json:
        print("=== 一键验收 ===")
        print(f"  项目：{ROOT}")
        print(f"  要跑 {len(steps)} 项\n")

    for step in steps:
        name = step["name"]
        if step["needs_service"] and not service_ok:
            results.append({
                "name": name, "level": "skip", "exit": None,
                "summary": f"服务没起齐（缺 {', '.join(missing)}）",
                "why": step["why"], "cost": step["cost"],
            })
            if not args.json:
                print(f"  ⏭  [{name}] 跳过 —— 服务没起齐（缺 {', '.join(missing)}）")
            continue

        code, out = _run(step["script"], step["args"])
        level = step["meaning"].get(code, "fail")
        summary = _summary_line(level and out, out)

        # "还没到能判的时候" ≠ "没做到"。
        # 前者该等（她还没在新代码下说过话），后者该查（说了但不像）。
        pending = step.get("pending_marker")
        if pending and pending in out:
            summary = f"还没到判定时机（{pending}）—— 等真实用过几天再跑"

        results.append({
            "name": name, "level": level, "exit": code,
            "summary": summary or (out.strip().splitlines()[-1] if out.strip() else ""),
            "why": step["why"], "cost": step["cost"],
            "pending": bool(pending and pending in out),
        })
        if not args.json:
            icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌", "skip": "⏭ "}[level]
            print(f"  {icon} [{name}] {summary or f'退出码 {code}'}")
            print(f"       {step['why']}（{step['cost']}）")
            # 失败时把它的结论段打出来，别让人再去单独跑一遍
            if level in ("fail", "warn"):
                tail = [ln for ln in out.splitlines() if ln.strip().startswith(("❌", "⚠️", "  ❌", "  ⚠️"))]
                for ln in tail[:6]:
                    print(f"       {ln.strip()}")

    fails = [r for r in results if r["level"] == "fail"]
    skips = [r for r in results if r["level"] == "skip"]
    oks = [r for r in results if r["level"] == "ok"]
    # pending 项（"还没到判定时机"）从 warn 里摘出来 —— 它不该让整体判红。
    # 这是这个脚本存在的核心价值：**"还没到时候"和"坏了"是两件事**。
    warns = [r for r in results if r["level"] == "warn" and not r.get("pending")]
    pending = [r for r in results if r["level"] == "warn" and r.get("pending")]

    if args.json:
        print(json.dumps({
            "ok": len(oks), "warn": len(warns), "fail": len(fails),
            "skip": len(skips), "results": results,
        }, ensure_ascii=False, indent=2))
    else:
        print()
        print("=" * 60)
        extra = f" / {len(pending)} 还没到判定时机" if pending else ""
        print(f"  结论：{len(oks)} 通过 / {len(warns)} 需留意 / "
              f"{len(fails)} 失败 / {len(skips)} 跳过{extra}")
        print("=" * 60)
        if fails:
            print("\n真失败（要修）：")
            for r in fails:
                print(f"  ❌ {r['name']}：{r['summary']}")
        if warns:
            print("\n需留意（不一定是坏了）：")
            for r in warns:
                print(f"  ⚠️  {r['name']}：{r['summary']}")
        if pending:
            print("\n还没到判定时机（等就行，不是坏了）：")
            for r in pending:
                print(f"  ⏳ {r['name']}：{r['summary']}")
        if skips:
            print("\n跳过（条件不满足，不算失败）：")
            for r in skips:
                print(f"  ⏭  {r['name']}：{r['summary']}")

        if not fails and not warns:
            print("\n🎉 全部通过。")
            # ⚠ 措辞要跟着"实际跑了哪些项"走。
            # 踩过的坑（2026-11-04 第十五轮）：--quick 只跑两个工具，
            # 却打印"包括三段对话在内，全部达成" —— 那是**谎报**，
            # 它压根没检查三段对话。宁可少说，也不能说没验过的话。
            # 措辞必须跟"实际验到了什么"一致。踩过两次坑：
            #   ① --quick 没跑 m6 却说"包括三段对话在内，全部达成"
            #   ② m6 因"还没有库"被 skip，又说了一遍同样的话
            # 宁可少说，也不能说没验过的话。
            m6 = next((r for r in results if r["name"] == "三段对话"), None)
            if pending:
                print("   产品代码这条线是干净的。")
                print("   唯一还没完成的是 T-13 三段对话 —— 那需要真实使用几天。")
            elif m6 is not None and m6["level"] == "ok":
                print("   包括三段对话在内，全部达成。")
            elif m6 is not None and m6["level"] == "skip":
                print("   产品代码这条线是干净的。")
                print("   （三段对话这次跳过了 —— 库里还没有数据，用几天再来）")
            else:
                print("   产品代码这条线是干净的。")
                print("   （这次没跑「三段对话」—— 去掉 --quick 才会查）")
        elif fails:
            print("\n修完上面标 ❌ 的，再跑一次这个命令。")
        else:
            print("\n没有真失败。标 ⚠️ 的看一眼就知道要不要动。")

    # 统一退出码：
    #   0 = 全过   1 = 有真失败   2 = 只有需留意项
    #
    # ⚠ 注意顺序：warn 必须在 fail 之后判，否则"既有 fail 又有 warn"会返回 2，
    # 调用方会以为"没坏"。有真失败就必须是 1。
    if fails:
        return 1
    if warns:
        return 2
    # pending 不算失败 —— 返回 0，否则 CI/人会被"还没到时候"卡住
    return 0


if __name__ == "__main__":
    sys.exit(main())
