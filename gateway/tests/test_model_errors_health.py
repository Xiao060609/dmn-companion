"""模型调用失败必须能在体检里看见（2026-11-04 第二十六轮）。

为什么单独一组
--------------
`errors.log` 里记的是**真实的**模型调用失败（401 / 503 / 超时）。
而在这一轮之前，**没有任何体检项覆盖它** —— 体检读的是
`system_audit.jsonl`，那条线根本不含模型调用错误。

实测证据（这次就是从日志里挖出来的）：
    11-01 17:13~17:15 连续 **8 次** `HTTP 401 Unauthorized`，
    她每条都只能回「我这会儿有点卡，等下再说好不好。」，
    而体检**全绿**。用户完全不知道是 key 失效了。

这是典型的监控盲区：**故障真实发生，监控看不见。**
和项目里另外两次教训同类（"长期记忆全挂也绿" / "装完给了虚假安全感"）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


@pytest.fixture()
def err_log(tmp_path, monkeypatch):
    """把 errors.log 指到临时文件，别碰生产日志。"""
    p = tmp_path / "errors.log"
    p.write_text("", encoding="utf-8")
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    return p


def _write(path: Path, recs: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_统计401次数(err_log):
    """401 要单独统计 —— 它和 503 的处置完全不同。"""
    _write(err_log, [
        {"ts": time.time(), "channel": "model", "in": "a",
         "error": "RuntimeError: HTTPError: HTTP Error 401: Unauthorized"},
        {"ts": time.time(), "channel": "model", "in": "b",
         "error": "RuntimeError: HTTPError: HTTP Error 401: Unauthorized"},
        {"ts": time.time(), "channel": "model", "in": "c",
         "error": "RuntimeError: HTTPError: HTTP Error 503: Service Unavailable"},
    ])
    s = system._model_error_stats()
    assert s["total"] == 3
    assert s["unauthorized"] == 2, "401 应该单独计数"
    assert s["server_error"] == 1


def test_超过24小时的不算(err_log):
    """窗口是 24 小时 —— 昨天的错误不该一直报警。"""
    _write(err_log, [
        {"ts": time.time() - 25 * 3600, "channel": "model", "in": "旧",
         "error": "HTTP Error 401: Unauthorized"},
        {"ts": time.time(), "channel": "model", "in": "新",
         "error": "HTTP Error 401: Unauthorized"},
    ])
    s = system._model_error_stats()
    assert s["total"] == 1, "25 小时前那条不该算进 24h 窗口"
    assert s["unauthorized"] == 1


def test_不算v1渠道的失败(err_log):
    """只看 channel="model" —— v1 那条路的失败是"外部调用方出错"，另算。"""
    _write(err_log, [
        {"ts": time.time(), "channel": "v1", "in": "x",
         "error": "HTTP Error 401: Unauthorized"},
        {"ts": time.time(), "channel": "model", "in": "y",
         "error": "HTTP Error 401: Unauthorized"},
    ])
    s = system._model_error_stats()
    assert s["total"] == 1, "v1 渠道不该混进模型调用统计"


def test_401会报成error级别(err_log, monkeypatch):
    """401 必须报 `error` 级别，且消息里说清"key 无效"。

    只报 warn 不够 —— key 失效时她**完全没法说话**，
    那是 error 不是 warn。
    """
    _write(err_log, [
        {"ts": time.time(), "channel": "model", "in": "x",
         "error": "RuntimeError: HTTPError: HTTP Error 401: Unauthorized"},
    ])
    rep = system.health_check()
    probs = [p for p in (rep.get("problems") or []) if p.get("area") == "model"]
    assert probs, "401 必须出现在体检 problems 里（以前这里是空的，所以全绿）"
    assert probs[0]["level"] == "error", "key 失效是 error，不是 warn"
    assert "key" in probs[0]["msg"].lower() or "API key" in probs[0]["msg"], (
        "消息里必须点出是 key 的问题，否则用户不知道该改什么"
    )


def test_503多次只报warn(err_log):
    """5xx 抖动报 warn 就够了 —— 等一会儿可能就好，不必制造恐慌。"""
    _write(err_log, [
        {"ts": time.time(), "channel": "model", "in": f"x{i}",
         "error": "HTTP Error 503: Service Unavailable"} for i in range(6)
    ])
    rep = system.health_check()
    probs = [p for p in (rep.get("problems") or []) if p.get("area") == "model"]
    assert probs, "6 次 5xx 应该报出来"
    assert probs[0]["level"] == "warn", "5xx 是 warn 不是 error"


def test_没有失败时不报(err_log):
    """没有失败就一条都不该报 —— 否则又变成"永远在报警"的噪音。"""
    rep = system.health_check()
    probs = [p for p in (rep.get("problems") or []) if p.get("area") == "model"]
    assert not probs


# --------------------------------------------------------------------------- #
# 主动说话投递失败必须进体检（2026-11-04 第二十七轮）
# --------------------------------------------------------------------------- #
def test_speak_fail算软失败():
    """`speak_fail` 必须在 `_SOFT_FAIL_EVENTS` 里。

    踩过的坑（从 system_audit.jsonl 挖出来的）：
    11-04 那天有 **6 次 speak_fail** —— 她主动说了话，但主人一个字都没听到：
        18:36「你回来啦我还记着11月1号呢。」  deliver_error: URLError
        19:09「你回来啦刚才去哪了。」         deliver_error: URLError
        20:59「你还在忙吗都这个点了。」       deliver_error: URLError
        另 3 次是 no_subscriber（桌宠开了但页面没订阅）
    而**体检全绿** —— 因为 speak_fail 根本不在软失败统计里。

    这是"监控看不见故障"的第四次（前三次：长期记忆全挂 / 不查 pip 包 / 401 未监控）。
    """
    assert "speak_fail" in system._SOFT_FAIL_EVENTS, (
        "speak_fail 应该算软失败 —— 她说了话但没送到，是真实丢话"
    )


def test_speak_fail的reason不能进设计内跳过():
    """`deliver_error` / `no_subscriber` 都不能算"设计内跳过"。

    它们不是"这一轮没活干"，而是**真的没送到**：
      deliver_error  = 桌宠没开 / 连接被拒 / 超时
      no_subscriber  = 桌宠开着，但页面没订阅（没人听）
    两种都是丢话，必须报出来。
    """
    for reason in ("deliver_error", "no_subscriber"):
        assert reason not in system._DESIGNED_SKIPS, (
            f"{reason} 不应该被当成设计内跳过 —— 那是真的没送到"
        )
    # 带前缀的也得覆盖到（reason 实际是 "deliver_error: URLError"）
    assert not any(
        str(r).startswith("deliver_error") for r in system._DESIGNED_SKIPS
    ), "不能有任何 deliver_error* 混进设计内跳过"


def test_busy算设计内跳过():
    """`daily_review` 返回 `busy` 不是失败，是并发保护。

    第二十五轮给 `daily_review` 加了非阻塞互斥：手动按钮撞上定时线程时
    直接返回 busy，**恰恰是不让它重复花钱**。
    把它算成"该成功但失败了"是误报 —— 实测 24h 内 8 次 busy 全是
    探索/验证时反复手动触发造成的，一次都不是故障。
    """
    assert "busy" in system._DESIGNED_SKIPS, (
        "busy 是并发保护，不是失败。算成失败会让体检天天报假警"
    )


# --------------------------------------------------------------------------- #
# 每个 audit 事件都必须有归属（2026-11-04 第二十八轮）
# --------------------------------------------------------------------------- #
def test_每个audit事件都有监控归属():
    """代码里每一种 `_audit({"event": ...})` 都必须在下面某一类里。

    为什么会有这条测试：
    前 27 轮我一直是「碰巧看到某个失败率」才补监控 ——
    长期记忆全挂、401 未监控、说话投递失败，都是这样被发现的。
    这次改成**系统化枚举**：扫代码里所有事件，减去
    `_error` 后缀自动覆盖的、减去设计内跳过，看还有谁没人管。

    结果又找到 3 个（recall_muted / recall_timeout / persona_update）。
    `recall_muted` 尤其严重：熔断期间**记忆检索完全停摆**，
    她会「记不住你」，而体检全绿。

    这条测试的作用：**以后新加一个 _audit 事件，忘了归类就会红**。
    等于是把「碰巧发现」变成了「结构性保证」。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "assets", "SnowLuma", "astrbot", "_归档", "vendor",
                 "dist", "build", "probe", "tests"}

    events: set[str] = set()
    pat = re.compile(r'"event"\s*:\s*"([a-z_]+)"')
    for p in root.rglob("*.py"):
        if any(part in skip_dirs for part in p.parts):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in txt.splitlines():
            if line.lstrip().startswith("#"):
                continue
            events.update(pat.findall(line))

    assert events, "没扫到任何事件（扫描逻辑坏了？）"

    # 已知不需要监控的：要么 _error 后缀自动覆盖，要么成功事件/设计内跳过
    OK_EVENTS = {
        # _error 后缀：_audit_stats 自动计入 errors
        "consolidate_error", "daily_review_error", "persona_review_error",
        "heartbeat_error",
        # 成功事件（ok 恒为 True 或 ok=false 但不是故障）
        "speak", "health_check", "schedules_fired", "inner_os_delivered",
        "care_outcomes", "people_profile_hint", "deferred_care_skipped",
        "people_profile",     # 设计性拒绝（qq_private 缺 speaker 列），不是故障
        # 设计内跳过
        "daily_review_skip", "schedules_skipped",
        "tick",               # ok=false 且 reason=no_change 是正常节律
        # 计划（能力 5）。
        #
        # 🔧 2026-11-07 改：`plan_closed` **已经从这行搬走了** —— 它现在
        # 在 `system._SOFT_FAIL_EVENTS` 里（真故障，要被体检盯着）。
        #
        # 理由：原来判定是纯代码词表，`plan_closed` 只在真收尾时写、恒 ok=True，
        # 所以列进 OK_EVENTS 是对的。现在成败由**模型**给裁决、本层落库
        # （`apply_plan_verdicts()`），就多了一条 `ok=False, reason=exception:…`
        # 的分支 —— 那意味着「模型判完了、但计划没真的关掉」，下一轮还会把它
        # 再喂一遍、再判一遍：白花钱且不收尾。**这种必须有人看。**
        #
        # 留在这里的只有 `plan_fired`（真触发才写，恒 ok=True）。`plan_skipped`
        # 是设计性跳过：群渠道没有投递通道，所以群里的计划不触发（落 closed）,
        # 和 deferred_care_skipped 同一个理由、同一个口径。
        #
        # ⚠ 别把 `plan_closed` 加回这一行 —— 加回来等于关掉它的监控，
        # 而且和 `_SOFT_FAIL_EVENTS` 里的登记**互相矛盾**（同一个事件两处结论）。
        "plan_fired",
        "plan_skipped",
        # 主动说话总开关（2026-11-08）：后台点了开关才写一条。
        #
        # 为什么放 OK_EVENTS 而不是 SOFT_FAIL：这个事件恒 ok=True ——
        # 只有"开关真的改了"才写，改失败会走 HTTP 400 直接回给前台，
        # 根本不进审计。所以它失败不可能是故障，列这儿是对的。
        "proactive_speak_toggle",
        # 身份变更（2026-11-08）：后台改主人 QQ 号 / 桌宠名字时写一条。
        #
        # 放 OK_EVENTS 而不是 SOFT_FAIL，理由和上面那个开关一样：
        # 这个事件**恒 ok=True** —— 只有"真的改了"才写，写不进去的失败
        # 会走 HTTP 400 直接回给前台，根本进不到审计。
        # 它的用途是"留痕"（旧值→新值），不是"报错"。
        "identity_update",
    }

    uncovered = events - set(system._SOFT_FAIL_EVENTS) - OK_EVENTS
    assert not uncovered, (
        f"这些 audit 事件的失败**没有任何监控**：{sorted(uncovered)}\n"
        f"要么加进 `_SOFT_FAIL_EVENTS`（如果失败算故障），"
        f"要么加进本测试的 OK_EVENTS（如果失败不是故障）并写清理由。\n"
        f"⚠ 别直接扔 OK_EVENTS 图省事 —— recall_muted 当初就是这么漏掉的，"
        f"而它意味着「记忆检索整个停摆」。"
    )


# --------------------------------------------------------------------------- #
# /chat 兜底异常必须能被看见（2026-11-04 第二十九轮）
# --------------------------------------------------------------------------- #
def test_chat兜底异常会被单独统计(err_log):
    """`channel="chat"` 是 /chat 整条链路的**兜底异常**。

    `gateway.py` 里 `/chat` 的 except 会 `_log_error("chat", ...)`。
    它一响，意味着**用户那一轮完全没收到回复**。

    而以前 `_model_error_stats` 只看 `channel == "model"`，
    chat 渠道的异常**一条都不统计** —— 这是比 signal 事件更底层的盲区：
    signal 是「某功能出错」，这个直接是「她根本不回你」。
    """
    _write(err_log, [
        {"ts": time.time(), "channel": "chat", "in": "x",
         "error": "RuntimeError: boom"},
    ])
    s = system._model_error_stats()
    assert s["chat_errors"] == 1, "chat 渠道的兜底异常应该单独计数"
    assert s["total"] == 0, "chat 不该混进 model 的统计（两者处置不同）"


def test_chat兜底异常报error级(err_log):
    """chat 兜底异常必须报 error —— 用户那一轮完全没回复。"""
    _write(err_log, [
        {"ts": time.time(), "channel": "chat", "in": "x",
         "error": "RuntimeError: boom"},
    ])
    rep = system.health_check()
    probs = [p for p in (rep.get("problems") or []) if p.get("area") == "chat"]
    assert probs, "chat 兜底异常必须出现在体检 problems 里"
    assert probs[0]["level"] == "error", "用户没收到回复是 error，不是 warn"
    assert "没收到" in probs[0]["msg"] or "未收到" in probs[0]["msg"], (
        "消息要说清后果：那一轮用户完全没收到回复"
    )


def test_v1错误不混进model统计(err_log):
    """`channel="v1"` 是外部调用方出错，不能算进"模型调用失败"。

    混在一起会让 `model_errors_24h` 虚高，掩盖真正的模型故障。
    """
    _write(err_log, [
        {"ts": time.time(), "channel": "v1", "in": "x",
         "error": "HTTP Error 500"},
    ])
    s = system._model_error_stats()
    assert s["v1_errors"] == 1
    assert s["total"] == 0, "v1 的错误不该算进 model 统计"
