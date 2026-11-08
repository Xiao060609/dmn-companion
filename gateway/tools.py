"""工具：本地动作，交给模型用 function calling 直接调。

分工很简单：
- 模型决定调哪个、参数是什么；
- 这里只负责执行，以及守住边界（路径、扩展名、写删要人工确认）；
- 结果原样回给模型，由它组织成人话。

写和删不在这里执行，只回一个"要问用户"，真正落地在 confirm()。
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

import hands

ROOT = os.path.dirname(os.path.abspath(__file__))

_CN_DIGITS = "零一二三四五六七八九"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜一下。用户让你上网查、问最新消息、问你不确定的事实、"
                "或者问今天发生什么的时候用。不要凭印象编答案。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，像人在搜索引擎里输的那样"},
                    "count": {
                        "type": "integer",
                        "description": "要几条结果，1~8，默认 5",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查某个城市今天或明天的天气和气温。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，例如 重庆"},
                    "when": {
                        "type": "string",
                        "enum": ["today", "tomorrow"],
                        "description": "今天还是明天",
                    },
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "看一个文件夹里有什么。用户问某个目录里有哪些文件时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件夹路径，相对或绝对"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读一个文本文件的内容。用户问某个文件里写了什么时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对或绝对"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写进一个文件。调用后系统会先问用户，不会立刻写。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删掉一个文件。调用后系统会先问用户，不会立刻删。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
        },
    },
]


def _ok(text: str) -> dict:
    return {"ok": True, "result": text}


def _err(text: str) -> dict:
    return {"ok": False, "error": text}


def _cn_number(value: int) -> str:
    if value < 10:
        return _CN_DIGITS[value]
    tens, ones = divmod(value, 10)
    prefix = "" if tens == 1 else _CN_DIGITS[tens]
    return prefix + "十" + (_CN_DIGITS[ones] if ones else "")


def _json_body(resp, what: str = "接口") -> dict:
    """读 HTTP 响应体并解成 JSON；拿到的不是 JSON 就抛 ValueError（带人话）。

    为什么需要它（2026-11-03 22:50 修 P2 技术债 #6）：
    `_weather` / `_search_brave` / `_search_bing` 三处都是裸 `json.loads(...)`。
    上游抽风时回的是一整页 HTML 错误页（"502 Bad Gateway"、限流页、维护页），
    于是抛 `JSONDecodeError` —— 外层 `run()` 有兜底不会 500，但会**原样回给模型
    「ValueError: ...」这种它看不懂的东西**，对话就废了。

    这里把前面几百字 HTML 截一段出来，变成
    「天气接口返回的不是 JSON（502 Bad Gateway），可能被限流或上游故障」——
    模型能读懂、能据此换话术（"我查不到"），用户也不会看到一串栈信息。

    Args:
        resp: `urllib.request.urlopen()` 返回的响应对象。
        what: 给模型看的名字（"天气" / "搜索"），进错误消息。

    Raises:
        ValueError: 响应体不是合法 JSON。
    """
    raw = resp.read()
    try:
        text = raw.decode("utf-8", errors="replace")
    except (UnicodeError, AttributeError):
        text = ""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        snippet = " ".join(str(text or "").split())[:120]
        raise ValueError(
            f"{what}接口返回的不是 JSON（{snippet or '空响应'}）——"
            "可能是被限流、维护中，或者这个地址已经不是 API 了"
        ) from None
    if not isinstance(data, dict):
        raise ValueError(f"{what}接口返回的 JSON 不是对象（是 {type(data).__name__}）")
    return data


_WEATHER_CACHE: dict[str, tuple[float, str]] = {}
_WEATHER_TTL = 600

# wttr.in 的 lang_zh 对新代码位不翻译（149 仍是 Smoky haze），按 WW 代码自己映射。
_WEATHER_CN = {
    113: "晴", 116: "多云", 119: "阴", 122: "阴天",
    143: "薄雾", 149: "烟霾", 248: "雾", 260: "冻雾",
    176: "局部有雨", 179: "局部有雪", 182: "局部雨夹雪",
    185: "局部冻毛毛雨", 200: "雷阵雨",
    227: "风吹雪", 230: "暴风雪",
    263: "局部小毛毛雨", 266: "小毛毛雨", 281: "冻毛毛雨", 284: "强冻毛毛雨",
    293: "局部小雨", 296: "小雨", 299: "间歇中雨", 302: "中雨",
    305: "间歇大雨", 308: "大雨",
    311: "小冻雨", 314: "中到大冻雨", 317: "小雨夹雪", 320: "中到大雨夹雪",
    323: "局部小雪", 326: "小雪", 329: "局部中雪", 332: "中雪",
    335: "局部大雪", 338: "大雪", 350: "冰粒",
    353: "小阵雨", 356: "中到大阵雨", 359: "暴雨",
    362: "小阵雨夹雪", 365: "中到大阵雨夹雪",
    368: "小阵雪", 371: "中到大阵雪",
    374: "小冰粒阵", 377: "中到大冰粒阵",
    386: "局部雷雨", 389: "中到大雷雨", 392: "局部雷雪", 395: "中到大雷雪",
}


def _weather(city: str, when: str = "today") -> str:
    cache_key = f"{city}|{when}"
    hit = _WEATHER_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _WEATHER_TTL:
        return hit[1]

    url = "https://wttr.in/" + urllib.parse.quote(city) + "?format=j1"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        # 用 _json_body 而不是裸 json.loads：wttr.in 抽风时回的是 HTML 错误页，
        # 裸解会抛 JSONDecodeError，模型收到「ValueError: ...」完全没法用。
        data = _json_body(resp, "天气")

    days = data.get("weather") or []
    index = 1 if when == "tomorrow" else 0
    if index >= len(days):
        raise ValueError("没有那天的预报")
    day = days[index]
    hourly = day.get("hourly") or []
    sample = hourly[4] if len(hourly) > 4 else (hourly[0] if hourly else {})
    try:
        desc = _WEATHER_CN.get(int(sample.get("weatherCode", 0)), "")
    except (TypeError, ValueError):
        desc = ""

    low = _cn_number(int(day["mintempC"]))
    high = _cn_number(int(day["maxtempC"]))
    prefix = "明天" if when == "tomorrow" else "今天"
    text = (
        f"{city}{prefix}{desc}，气温{low}到{high}度"
        if desc
        else f"{city}{prefix}气温{low}到{high}度"
    )
    _WEATHER_CACHE[cache_key] = (time.time(), text)
    return text


# --------------------------------------------------------------- 联网搜索
_SEARCH_CACHE: dict[str, tuple[float, str]] = {}
_SEARCH_TTL = 300          # 同一个词 5 分钟内不重复搜，省配额也省时间
_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _tavily_key() -> str:
    """Tavily 搜索的 key：环境变量优先，其次 secrets.json。都没有就返回空。"""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.join(ROOT, "secrets.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("tavily_api_key", "")).strip()
    except (OSError, json.JSONDecodeError):
        return ""


def _search_tavily(query: str, count: int) -> tuple[list[tuple[str, str, str]], str]:
    """Tavily 搜索。返回 ([(标题, 摘要, 链接)], 内置摘要)。

    为什么首选它（2026-11-03 换）：
    * 响应自带 `answer` 字段 —— 一个已经综合过来源的短答案。
      模型拿到它就能直接作答，不用再从 5 条摘要里自己拼。
    * `content` 摘要是**正文片段**而不是 SEO 描述，信息密度高得多。
    * `score` 带相关性分数，可以只取靠前的。

    Args:
        query: 搜索词。
        count: 要几条结果（Tavily 上限跟套餐走，这里按需传）。

    Returns:
        (结果列表, 内置摘要)。摘要可能为空串。
    """
    body_obj = {
        "api_key": _tavily_key(),
        "query": query,
        "max_results": max(1, min(10, int(count or 5))),
        # include_answer=basic：要一个简短的综合性答案（不是逐条罗列）
        "include_answer": True,
        "search_depth": "basic",  # advanced 更贵更慢，日常够用
    }
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(body_obj, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = _json_body(resp, "搜索")
    rows = [
        (
            str(item.get("title") or ""),
            str(item.get("content") or ""),
            str(item.get("url") or ""),
        )
        for item in (data.get("results") or [])
    ]
    return rows, str(data.get("answer") or "").strip()


def _brave_key() -> str:
    """Brave 搜索的 key：环境变量优先，其次 secrets.json。都没有就返回空。"""
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.join(ROOT, "secrets.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("brave_api_key", "")).strip()
    except (OSError, json.JSONDecodeError):
        return ""


def _search_brave(query: str, count: int) -> list[tuple[str, str, str]]:
    """Brave 官方 API。返回 [(标题, 摘要, 链接)]。"""
    url = (
        "https://api.search.brave.com/res/v1/web/search?q="
        + urllib.parse.quote(query)
        + f"&count={count}"
    )
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "X-Subscription-Token": _brave_key()},
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = _json_body(resp, "搜索")
    return [
        (
            str(item.get("title") or ""),
            str(item.get("description") or ""),
            str(item.get("url") or ""),
        )
        for item in ((data.get("web") or {}).get("results") or [])[:count]
    ]


def _search_bing(query: str, count: int) -> list[tuple[str, str, str]]:
    """Bing 网页版兜底：没配 key、或 Brave 被网络挡住时用。

    HTML 会变，所以只认 ``li.b_algo`` 这个结果块；解析不到就返回空，
    由调用方决定怎么跟用户交代（绝不假装搜到了）。
    """
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=zh-CN"
    req = urllib.request.Request(
        url, headers={"User-Agent": _SEARCH_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        page = resp.read().decode("utf-8", "replace")
    rows: list[tuple[str, str, str]] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S)[:count]:
        hit = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not hit:
            continue
        title = re.sub(r"<[^>]+>", "", hit.group(2))
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        text = re.sub(r"<[^>]+>", "", snip.group(1)) if snip else ""
        rows.append(
            (html.unescape(title).strip(), html.unescape(text).strip(), hit.group(1))
        )
    return rows


def _search_bing_rss(query: str, count: int) -> list[tuple[str, str, str]]:
    """Bing 的 RSS 出口：结构比 HTML 稳定，作为 HTML 解析失败时的兜底。

    实测 HTML 版偶尔会返回一个没有 ``li.b_algo`` 的页面（反爬/同意页），
    这时 RSS 一般还能拿到结果。相关性比 HTML 差一点，所以排在后面。
    """
    url = (
        "https://www.bing.com/search?q="
        + urllib.parse.quote(query)
        + "&format=rss&setlang=zh-CN"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _SEARCH_UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        xml = resp.read().decode("utf-8", "replace")
    rows: list[tuple[str, str, str]] = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S)[:count]:
        title = re.search(r"<title>(.*?)</title>", block, re.S)
        link = re.search(r"<link>(.*?)</link>", block, re.S)
        desc = re.search(r"<description>(.*?)</description>", block, re.S)
        if not (title and link):
            continue
        snippet = re.sub(r"<[^>]+>", "", html.unescape(desc.group(1))) if desc else ""
        rows.append(
            (
                html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip(),
                re.sub(r"\s+", " ", snippet).strip(),
                link.group(1).strip(),
            )
        )
    return rows


def _web_search(query: str, count: int = 5) -> str:
    """搜一次，返回给模型看的纯文本。

    后端优先级（2026-11-03 调整）：

        ① **Tavily** —— 首选。响应自带 `answer`（已综合过的短答案），
           正文摘要信息密度高。有它时模型不用自己从 5 条里拼。
        ② Brave   —— 有 key 才走；不再当主选（key 曾经存错格式导致 422）。
        ③ Bing HTML → ④ Bing RSS —— 兜底，不需要任何 key。

    四条都挂就抛错 —— 让模型转告用户「没搜到」，而不是凭印象编。
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("没有搜索词")
    count = max(1, min(8, int(count or 5)))
    cache_key = f"{query}|{count}"
    hit = _SEARCH_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _SEARCH_TTL:
        return hit[1]

    rows: list[tuple[str, str, str]] = []
    answer = ""

    # ① Tavily（首选）
    if _tavily_key():
        try:
            rows, answer = _search_tavily(query, count)
        except Exception:  # noqa: BLE001 —— 超时/额度/被挡都走兜底
            rows, answer = [], ""

    # ② Brave（有 key 且 Tavily 没结果时才走）
    if not rows and _brave_key():
        try:
            rows = _search_brave(query, count)
        except Exception:  # noqa: BLE001
            rows = []

    # ③④ Bing 两级兜底
    for backend in (_search_bing, _search_bing_rss):
        if rows:
            break
        try:
            rows = backend(query, count)
        except Exception:  # noqa: BLE001 —— 一个后端挂了还有下一个
            rows = []
    if not rows:
        raise ValueError("搜索引擎没有返回结果")

    # 有 answer 就先给它 —— 它是已经综合过来源的答案，
    # 比一堆待拼的摘要省事得多。同时仍附来源，便于模型核对/引用。
    blocks: list[str] = []
    if answer:
        blocks.append("【综合】" + answer)
    lines = []
    for i, (title, snippet, link) in enumerate(rows, 1):
        title = re.sub(r"\s+", " ", title)[:80]
        snippet = re.sub(r"\s+", " ", snippet)[:160]
        lines.append(
            f"{i}. {title}｜{snippet}｜{link}" if snippet else f"{i}. {title}｜{link}"
        )
    blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    _SEARCH_CACHE[cache_key] = (time.time(), text)
    return text


def run(name: str, args: dict) -> dict:
    """执行一个工具调用。返回给模型的 JSON。

    Args:
        name: 工具名，来自 TOOLS。
        args: 模型给的参数。

    Returns:
        {"ok": True, "result": ...} / {"ok": False, "error": ...} /
        {"need_approval": True, ...}（写删要人工确认）。
    """
    args = args or {}
    try:
        if name == "web_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return _err("没听清要搜什么")
            return _ok(_web_search(query, args.get("count") or 5))

        if name == "get_weather":
            city = str(args.get("city") or "").strip()
            if not city:
                return _err("没听出是哪个城市")
            when = "tomorrow" if str(args.get("when") or "today") == "tomorrow" else "today"
            return _ok(_weather(city, when))

        if name == "list_dir":
            return _ok(hands.list_dir(args.get("path", "")))

        if name == "read_file":
            return _ok(hands.read_file(args.get("path", "")))

        if name in ("write_file", "delete_file"):
            path = str(args.get("path", ""))
            resolved = hands.resolve(path)
            if name == "delete_file" and not os.path.exists(resolved):
                return _err("没找到这个文件")
            payload_args = {"path": path}
            if name == "write_file":
                payload_args["content"] = str(args.get("content", ""))
            return {
                "need_approval": True,
                "action": name,
                "args": payload_args,
                "note": hands.describe(name, payload_args),
            }
    except hands.PolicyDenied as exc:
        return _err(str(exc))
    except FileNotFoundError:
        return _err("没找到这个文件")
    except OSError as exc:
        return _err(f"打不开：{exc.strerror}")
    except ValueError as exc:
        # 搜索没结果、参数不合法之类：交给模型去跟用户解释，
        # 别让异常冒到 handle_chat 把整轮对话打断（踩过：搜不到直接 500）。
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"{type(exc).__name__}: {exc}")
    return _err("没有这个工具")


def confirm(action: str, args: dict) -> tuple[bool, str]:
    """用户确认后执行写/删。返回 (成功, 一句话)。"""
    try:
        if action == "delete_file":
            return True, hands.delete_file(args.get("path", ""))
        if action == "write_file":
            return True, hands.write_file(args.get("path", ""), str(args.get("content", "")))
    except hands.PolicyDenied as exc:
        return False, str(exc)
    except FileNotFoundError:
        return False, "没找到这个文件"
    except OSError as exc:
        return False, f"没成功：{exc.strerror}"
    return False, "这个动作我不认识"