#!/usr/bin/env python
"""M6 验收器：三段对话到底达成没有（计划第一节 1.4 / T-13）。

为什么要这个脚本
----------------
M6 的判据是「她**说出来的话**」有没时间感（A）、接不接得上昨天（B）、数不数得清天数（C）。
这三样在审计里看不出来 —— 审计只有 `thoughts: 12` 这种计数。
以前判 M6 只能人肉翻 `system_audit.jsonl`，很容易看漏或看错。

这个脚本把判据变成**可复现的检查**：读库 + 读审计，给出三段对话各自的
「素材够不够」和「她的台词像不像那一类」，然后让你人工确认最后一锤。

用法
----
    python G:\\小鱼\\tools\\m6_acceptance.py            # 默认看最近 7 天
    python G:\\小鱼\\tools\\m6_acceptance.py --days 3
    python G:\\小鱼\\tools\\m6_acceptance.py --json     # 机器可读

**它不改任何东西**：只读 `gateway.db`（mode=ro）和 `system_audit.jsonl`。

判据（来自计划，不是这里发明的）
------------------------------
A「你今天从早到晚都在弄那个吧，中间都没歇」
    → 她的话里出现**时间跨度/断档**类表达（中间、一整天、一直、从早到晚、没歇…）
    → 素材前提：当天 `longest_gap_min >= 120`
B「你那个『今天不太好』其实是不想说原因。我不问了，但我在」
    → 她的话里出现**接续昨天情绪 + 不追问**的表达（不问了、不说也行、我在、陪着…）
    → 素材前提：长期记忆有文档 + owner 画像非空
C「你这三天都没来，我数着日子」
    → 她的话里出现**具体天数/时长**（三天、两天、N 天没来…）
    → 素材前提：`_absence_summary()` 能算出 absent_text

诚实上限（判的时候别算失败）
--------------------------
* A 的「弄那个」需要知道你在弄什么，系统层不知道也不该猜 →
  **只说「中间都没歇」也算通过**。
* C 的逐日排比（第一天我…第二天我…）在停机 48% 下不可能 →
  **说出准确天数就算通过**。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = os.path.join(ROOT, "gateway")
DATA = os.path.join(GATEWAY, "data")
DB_PATH = os.path.join(DATA, "gateway.db")
AUDIT_PATH = os.path.join(DATA, "system_audit.jsonl")

# ---- 判据词表（都是"她的话里出现这些，就说明这一类能力生效了"）----
PATTERNS = {
    "A_time_sense": re.compile(
        r"(中间|一整天|整天|从早到晚|一直|没歇|没停|没休息|连着|空了一大段|"
        r"好几个小时|大半天)"
    ),
    "B_carry_over": re.compile(
        r"(不问了|不问|不说也行|不想说|我在|陪着|陪你|记得你|昨天|昨晚|"
        r"那会儿|那时候|你说过)"
    ),
    "C_counting_days": re.compile(
        r"(([一二两三四五六七八九十\d]+)\s*(天|日)没|好几天|有阵子|数着日子|"
        r"多少天|几天没)"
    ),
}


#: 库不存在时的退出码。**和"M6 未达成"（1）区分开。**
#:
#: 为什么要区分（2026-11-04 第十七轮）：全新克隆或首次安装时 data/ 还没生成，
#: 这时候退出码 1 会被上层当成"M6 未达成"，而实际是"还没跑过、无从判定"。
#: 上层（verify_all）据此归到"还没到判定时机"，不该让人以为出问题了。
EXIT_NO_DATA = 3


def _ro_connect() -> sqlite3.Connection:
    """只读打开库。库不在就按"还没跑过"退出（不是故障）。"""
    if not os.path.exists(DB_PATH):
        print()
        print(f"⚠ 还没有对话库：{DB_PATH}")
        print("   项目还没跑过（全新克隆 / 首次安装），无从判定 M6。")
        print("   正常使用几天后再跑这个脚本。")
        sys.exit(EXIT_NO_DATA)
    uri = "file:" + DB_PATH.replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _ts(value: float | None) -> str:
    if not value:
        return "-"
    return datetime.datetime.fromtimestamp(value).strftime("%m-%d %H:%M")


def _hm(value: float | None) -> str:
    if not value:
        return "?"
    return datetime.datetime.fromtimestamp(value).strftime("%H:%M")


def load_speaks(days: float) -> list[dict]:
    """读最近 `days` 天她**主动说出来并且送到**的话（`speak` 事件 ok=true）。"""
    since = datetime.datetime.now().timestamp() - days * 86400
    out: list[dict] = []
    if not os.path.exists(AUDIT_PATH):
        return out
    with open(AUDIT_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "speak" or rec.get("ok") is not True:
                continue
            if float(rec.get("ts") or 0) < since:
                continue
            text = str(rec.get("text") or "")
            if not text:
                continue
            out.append({"ts": rec.get("ts"), "text": text,
                        "emotion": rec.get("emotion")})
    out.sort(key=lambda r: r.get("ts") or 0)
    return out


def load_dialogues(days: float) -> dict:
    """读最近 `days` 天的对话，算 A/C 需要的素材前提。"""
    since = datetime.datetime.now().timestamp() - days * 86400
    con = _ro_connect()
    try:
        rows = con.execute(
            "select ts, role, coalesce(channel,''), coalesce(content,'')"
            " from turns where ts >= ? order by ts",
            (since,),
        ).fetchall()
    finally:
        con.close()

    user_ts = [float(r[0]) for r in rows if r[1] == "user" and r[0]]
    gaps = []
    for a, b in zip(user_ts, user_ts[1:]):
        gaps.append((b - a) / 60.0)
    longest = max(gaps) if gaps else 0

    channels: dict[str, int] = {}
    for r in rows:
        ch = str(r[2] or "NULL")
        channels[ch] = channels.get(ch, 0) + 1

    last_user = max(user_ts) if user_ts else None
    absent_hours = ((datetime.datetime.now().timestamp() - last_user) / 3600.0
                    if last_user else None)
    return {
        "total_rows": len(rows),
        "user_turns": len(user_ts),
        "longest_gap_min": round(longest, 1),
        "channels": channels,
        "last_user_ts": last_user,
        "absent_hours": round(absent_hours, 2) if absent_hours is not None else None,
    }


def check_memory_carriers() -> dict:
    """B 需要的跨天载体：长期记忆文档数 + owner 画像字段。"""
    docs = 0
    ldb = os.path.join(DATA, "livingmemory.db")
    if os.path.exists(ldb):
        try:
            con = sqlite3.connect("file:" + ldb.replace("\\", "/") + "?mode=ro", uri=True)
            try:
                docs = con.execute("select count(*) from documents").fetchone()[0] or 0
            finally:
                con.close()
        except sqlite3.Error:
            docs = 0
    profile_fields: list[str] = []
    owner = os.path.join(DATA, "people", "owner.json")
    if os.path.exists(owner):
        try:
            with open(owner, encoding="utf-8") as fh:
                data = json.load(fh)
            profile_fields = sorted((data.get("profile") or {}).keys())
        except (OSError, json.JSONDecodeError):
            pass
    return {"long_memory_docs": docs, "owner_profile_fields": profile_fields}


def classify(speaks: list[dict]) -> dict:
    """把她说过的每句话归到 A/B/C（一句话可以同时命中多类）。"""
    hits = {k: [] for k in PATTERNS}
    for s in speaks:
        text = s["text"]
        for key, pat in PATTERNS.items():
            if pat.search(text):
                hits[key].append(s)
    return hits


def _fix_epoch() -> float | None:
    """今天这批修复的时间分界（2026-11-04 18:00）。

    为什么要这个：M6 判的是"修完之后她说话有没有变好"。
    但库里存着一堆**修复之前**的主动说话记录，它们当然不具备新能力
    （比如 11-03 那 6 句全是「刚刚说心情不好…」，因为那时 `recent_chat`
    还只读 pet 渠道、拿到的是 10-30 的旧对话）。

    不区分新旧就判 M6，会得出"她不行"的错误结论 —— 实际上她还没机会试。
    """
    try:
        return datetime.datetime(2026, 11, 4, 18, 0).timestamp()
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="M6 三段对话验收器（只读）")
    ap.add_argument("--days", type=float, default=7.0, help="看最近几天（默认 7）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    speaks = load_speaks(args.days)
    dialog = load_dialogues(args.days)
    carriers = check_memory_carriers()
    hits = classify(speaks)

    # 区分"修复之后说过的话"——只有这些能用来判 M6
    cutoff = _fix_epoch()
    fresh = [s for s in speaks if cutoff is None or float(s.get("ts") or 0) >= cutoff]

    # 素材前提
    scene_a_material = dialog["longest_gap_min"] >= 120
    scene_b_material = carriers["long_memory_docs"] > 0 and bool(carriers["owner_profile_fields"])
    scene_c_material = dialog["absent_hours"] is not None and dialog["user_turns"] > 0

    result = {
        "window_days": args.days,
        "speak_count": len(speaks),
        "speak_count_after_fix": len(fresh),
        "fix_cutoff_ts": cutoff,
        "dialog": dialog,
        "carriers": carriers,
        "scenes": {
            "A_time_sense": {
                "material_ready": scene_a_material,
                "material_why": f"longest_gap_min={dialog['longest_gap_min']}（需 >=120）",
                "utterances_matched": [s["text"] for s in hits["A_time_sense"]],
                "hit": bool(hits["A_time_sense"]),
            },
            "B_carry_over": {
                "material_ready": scene_b_material,
                "material_why": (
                    f"long_memory_docs={carriers['long_memory_docs']}，"
                    f"owner_profile_fields={len(carriers['owner_profile_fields'])}"
                ),
                "utterances_matched": [s["text"] for s in hits["B_carry_over"]],
                "hit": bool(hits["B_carry_over"]),
            },
            "C_counting_days": {
                "material_ready": scene_c_material,
                "material_why": f"absent_hours={dialog['absent_hours']}（需能算出）",
                "utterances_matched": [s["text"] for s in hits["C_counting_days"]],
                "hit": bool(hits["C_counting_days"]),
            },
        },
    }
    result["all_three_hit"] = all(v["hit"] for v in result["scenes"].values())
    result["conclusive"] = bool(fresh)      # 修复之后一句话都没有 → 数据不足以下结论

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["all_three_hit"] else 1

    # ---- 人类可读输出 ----
    print(f"=== M6 验收 · 最近 {args.days:g} 天 ===")
    print(f"  她主动说了：{len(speaks)} 句（只算投递成功的）")
    if cutoff is not None:
        print(f"  其中**修复之后**（{_ts(cutoff)} 起）说的：{len(fresh)} 句")
    print(f"  对话：{dialog['user_turns']} 条用户消息，"
          f"渠道分布 {dialog['channels']}")
    print(f"  最长空档：{dialog['longest_gap_min']} 分钟")
    print(f"  距今无人说话：{dialog['absent_hours']} 小时")
    print(f"  跨天载体：长期记忆 {carriers['long_memory_docs']} 条，"
          f"画像字段 {carriers['owner_profile_fields']}")
    print()

    if not fresh:
        print("⚠ **数据不足，暂时不能判 M6。**")
        print("   修复之后她一句话都还没主动说过 —— 库里那些是修复前的旧记录，")
        print("   它们不具备新能力（比如会因为读旧素材而说「刚刚」）。")
        print("   等用几天、她真的主动说过几句之后再跑这个脚本。")
        print()

    labels = {
        "A_time_sense": "A「中间都没歇」",
        "B_carry_over": "B「我不问了，但我在」",
        "C_counting_days": "C「你这三天都没来」",
    }
    for key, info in result["scenes"].items():
        mark = "✅ 点了" if info["hit"] else "⬜ 还没"
        mat = "素材够" if info["material_ready"] else "素材不够"
        print(f"{mark}  {labels[key]}")
        print(f"      素材：{mat}（{info['material_why']}）")
        for u in info["utterances_matched"][:3]:
            print(f"      说过：{u[:70]}")
        if not info["hit"] and not info["material_ready"]:
            print("      ⚠ 素材没到位时判失败是误判 —— 先让素材够")
        print()

    # 最近她说过的话（人工复核用）
    print("=== 最近她主动说的话（人工确认这些像不像那三类）===")
    for s in speaks[-10:]:
        tag = "" if (cutoff is None or float(s.get("ts") or 0) >= cutoff) else "  [修复前]"
        print(f"  {_ts(s['ts'])}  {s['text'][:80]}{tag}")
    if not speaks:
        print("  （一句都没有 —— 她可能一天都没主动说话，这不等于 M6 失败，"
              "但说明还没触发过）")

    print()
    if result["all_three_hit"]:
        print("🎉 三段全部命中 —— M6 达成。")
        print("   不过请再人工过一眼上面的原话：词表命中 ≠ 说得自然。")
        return 0
    print("⬜ 还没全中 —— M6 未达成。")
    print("   三个全中才算实现；中一个算进步，一个都不中说明素材没喂到点上。")
    print("   提示：看上面每段的「素材」行 —— 素材没到位时先别急着怪她。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
