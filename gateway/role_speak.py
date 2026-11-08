"""角色层的主动说话：被系统层叫醒后，自己决定要不要说、说什么，并投递给桌宠。

职责边界（触发是机制，说话是人格）：
- 系统层只做「触发 + 刹车」——纯代码变化检测 + 额度/冷却，把「事实信号」递给这里；
- 本模块承载角色层的完整动作：决策（说不说）→ 渲染（怎么说）→ 投递（推桌宠 /api/say）。

台词永远只从这里（角色层）出，系统层不碰任何正文文本。
"""
from __future__ import annotations

import json
import urllib.request


def deliver_to_pet(rendered: dict) -> bool:
    """把台词推给桌宠（8848 /api/say）。返回是否真的送达（有订阅者）。
    桌宠没开/超时/无人订阅都记审计、返回 False，不重试、不抛穿。
    无论成败都留痕台词（说了什么不能查不到）。"""
    import gateway
    import system  # 复用系统层审计，写进 system_audit.jsonl（保证历史查询不失效）

    text = str(rendered.get("text") or "")
    emotion = rendered.get("emotion")
    tone = rendered.get("tone") or ""
    base = str(
        gateway.CFG.get("system", {}).get("desktop_base", "http://127.0.0.1:8848")
    ).rstrip("/")
    body = json.dumps(
        {"text": text, "emotion": emotion, "tone": tone},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/say", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 —— 桌宠没开、连接拒绝、超时等
        system._audit({
            "event": "speak_fail", "ok": False, "text": text, "emotion": emotion,
            "reason": f"deliver_error: {type(exc).__name__}",
        })
        return False
    # /api/say 返回 {"ok": true, "subscribers": N}。N<=0 说明没人订阅（桌宠页没开），降级。
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    subs = data.get("subscribers")
    if subs is not None and int(subs) <= 0:
        system._audit({
            "event": "speak_fail", "ok": False, "text": text, "emotion": emotion,
            "reason": "no_subscriber",
        })
        return False
    system._audit({
        "event": "speak", "ok": True, "text": text, "emotion": emotion,
        "tone": tone, "subscribers": subs,
    })
    return True


def _mood_line(st: dict) -> str:
    """只取「此刻心情」那一段（`mood_*` 四字段，按真实时间衰减后）。

    为什么单独抽出来（2026-11-06 / 项目地图坑 #63）
    --------------------------------------------
    主动说话原来读的是 `st.get("emotion")` —— 旧的 8 值枚举。被动回话那条路
    （`gateway.build_messages()` → `system.mood_prompt_line()`）读的却是新的
    四字段。结果是：**她主动开口时"知道"的还是旧 emotion，被动回话时才知道
    新心情**。现在两条路统一读新字段。

    ⚠ 但**不能**直接调 `system.mood_prompt_line()`：它还会注入 `focus`
    （「你心里还挂着刚才那句：…」）和「你 N 分钟前刚主动找他说过话」。
    主动说话时注入 focus = 让她复读自己上一句（主人明确否掉的）。
    所以这里用 `mood.prompt_line()`：它**只**读心情四字段，不含 focus。

    ⚠ 平静时也照常给一行（「你现在的心情是「平静」。」），**不会返回空串**：
    主动说话必须知道自己此刻是平静的，否则只能靠猜。

    Args:
        st: 调用方刚 `load_state()` 出来的状态（心跳那条路）。

    Returns:
        一段可直接拼进 system 的文本；`st` 里没有可用的 `mood_updated_ts`
        时退回 `system.load_state()` 再读一次（它对旧格式做迁移归一）。
    """
    import mood
    import system

    state = st if isinstance(st, dict) else {}
    try:
        updated = float(state.get("mood_updated_ts") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if updated <= 0:
        # 旧格式 / 没迁移过的状态：让 system 读一次（顺带把迁移结果落盘）。
        # 读不到就退回原状态，绝不让"读心情失败"拦住她说话。
        try:
            state = system.load_state()
        except Exception:  # noqa: BLE001
            state = st if isinstance(st, dict) else {}
    return mood.prompt_line(state)


def maybe_speak(signals: dict, st: dict) -> tuple[dict | None, str]:
    """角色层被叫醒后，自己决定要不要开口、说什么、怎么说，并投递给桌宠。

    返回 (rendered, status)。status 取值：

      "spoken"           说了，而且送到了
      "declined"         她选择不说（输出 [不说]）
      "parse_failed"     模型说了，但没解析出可用台词
      "delivery_failed"  说了，但没送到（桌宠没开 / 无人订阅）

    为什么要把这几种分开：之前统一返回 None，调用方只能记成「declined」，
    结果「她不想说」和「桌宠没开导致没送到」在审计里长得一模一样。
    前者是正常状态，后者是会反复发生的故障——混在一起就答不出
    「她最近是没话说，还是话根本没送出去」。
    """
    import gateway
    import system

    # 当前场合的人物画像（主动说话目前只针对桌宠/主人，所以用 "pet"）。
    # 措辞、7 天降权阈值、300/400 字截断全部由 system.people_prompt_line 提供——
    # 这里以前另写了一份（自己读 pet、自己算 days、自己拼「你对他的了解」），
    # 同一口径两份实现，改一处必然漂一处：口径只留一份，改就直接改 system 那边。
    try:
        profile_line = system.people_prompt_line("pet")
    except Exception:  # noqa: BLE001 —— 画像读不到不影响说话
        profile_line = ""
    if profile_line:
        profile_line += "\n"
    # 上一次关心的结果：被拒过就别用同样的方式再来一遍。
    # 按渠道取（主动说话目前只针对桌宠，所以传 pet）：不限渠道的话，
    # 将来 QQ 侧开了主动关心，群里被拒一次会让桌宠不敢说话。
    try:
        care_line = system.recent_care_hint("pet")
    except Exception:  # noqa: BLE001
        care_line = ""
    if care_line:
        care_line += "\n"

    # 此刻的心情（2026-11-06 修，项目地图坑 #63）。
    #
    # 原来这一行读的是 `st.get('emotion')` —— 那个 8 值旧枚举，只有"什么表情"
    # 没有"有多深、为什么"。于是**她主动开口时"知道"的还是旧 emotion，
    # 被动回话时才知道新心情**（`gateway.build_messages()` 走的是
    # `system.mood_prompt_line()`，读的是 `mood_*` 四字段、且已按真实时间衰减）。
    #
    # ⚠ 为什么不用 `mood_prompt_line()` 本体（坑 #63 的明确警告）：
    #   它会把 `focus`（"你心里还挂着刚才那句：…"）和"刚主动说过话"一并注入。
    #   主动说话这条路注入 focus = 让她**复读自己上一句**，正是主人明确否掉的。
    #   所以这里只取心情那一段：`mood.prompt_line()` 只读四字段、不含 focus。
    #
    # ⚠ 平静时返回的那一行**必须照常注入**（和 `mood_prompt_line()` 的
    #   "平静且没什么可说就不注入"不同）：主动说话必须知道「我现在是平静的」，
    #   否则她只能靠猜。这里也就不存在"拿到空串"的情况 ——
    #   `mood.prompt_line()` 至少给一句「你现在的心情是「平静」。」
    try:
        mood_line = _mood_line(st)
    except Exception:  # noqa: BLE001 —— 读不到心情不影响说话
        mood_line = ""
    if mood_line:
        mood_line += "\n"

    msgs = [
        {"role": "system", "content": gateway.current_persona()},
        {
            "role": "system",
            "content": (
                "这是你心里自己冒出来的感觉，不是谁在跟你说话。\n"
                f"你观察到的：{json.dumps(signals, ensure_ascii=False)}\n"
                + mood_line
                + profile_line
                + care_line
                + "如果你想跟他说句话，就直接说，1 到 2 句，句尾带情绪标记。\n"
                "如果这一刻没什么想说的，只输出「[不说]」四个字符，不要解释。"
            ),
        },
    ]
    reply, _meta, _calls = gateway.call_model(msgs, use_tools=False)
    if not reply or "[不说]" in reply:
        return None, "declined"
    rendered = gateway.parse_reply(reply, gateway.CFG["dialog"]["max_sentences"])
    if not rendered or not rendered.get("text"):
        return None, "parse_failed"
    if not deliver_to_pet(rendered):
        return None, "delivery_failed"
    # 阶段 4：记一条 care_given 事件，等下次对话判定「有回应 / 被拒绝」。
    # 渠道显式传 pet：台词是 `deliver_to_pet` 投到桌宠的，回应也应该去桌宠
    # （+ 主人私聊，见 system.CARE_RESPONSE_SIBLINGS）那条线上找。
    # 以前这里是写死的，主人在 QQ 私聊回她，三态判定永远看不到。
    try:
        import system
        system.record_care_given(rendered, channel="pet")
    except Exception:  # noqa: BLE001 —— 记事件失败不影响说话
        pass
    return rendered, "spoken"
