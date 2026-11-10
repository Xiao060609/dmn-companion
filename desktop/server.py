#!/usr/bin/env python3
"""Digital Mate 浏览器版 —— 本地服务（纯标准库，零第三方依赖）。

打开方式：
    python server.py
    浏览器访问 http://127.0.0.1:8848

提供：
    /                     前端页面（web/）
    /model/settings.json  自动生成的 Live2D 模型配置（不修改工坊原文件）
    /model/*              直接映射工坊模型目录（不复制 7MB moc3 / 6MB 贴图）
    /api/status           语音引擎状态
    /api/tts              POST {"text": "..."} → audio/wav
    /api/chat             POST {"text": "..."} → {"reply", "emotion"}
    /api/shutdown         POST（也接 GET）优雅退出：停语音引擎释放显存，然后进程自退
"""
from __future__ import annotations

import datetime
import hashlib
import inspect
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import (
    BASE, WEB, CFG,
    WORKSHOP, LIVE2D_DIR, FASTVITS_EXE, VITS_MODEL, VITS_CONFIG, TTS_OUTDIR,
    BREEZE_DIR, BREEZE_SERVER, BREEZE_MODEL, TTS_ENGINE,
)

# 情绪 → Breeze 指令（instruction）。注意：现在走的是**克隆**（config 里配了 voice_id），
# 音色由参考音频锁死，instruction 只当「语气方向」用——快慢、轻重、情绪。
# 所以这里绝不能写「轻快明亮的女声」这类描述音色的词：那会被当成重新设计声音，
# 一句话就能把音色带跑，正是之前「每次声音都不一样」的根源。
EMOTION_INSTRUCTION = {
    "happy": "语速稍快，带出藏不住的笑意",
    "sad": "语速放慢，声音放轻，带着心疼",
    "shy": "语气怯一点，气息轻，语速放慢",
    "calm": "平稳温和，语速舒缓，从容不迫",
    "surprised": "语气上扬，带一点惊讶",
    "confused": "语气迟疑，带一点不确定",
    "love": "语气甜一点，尾音带一点上扬，语速放慢",
    "angry": "语气冲一点，带着不耐烦，语速偏快",
}
# 后台改台词时给用户看的中文名（顺序就是下拉框顺序）
EMOTION_LABELS = {
    "happy": "开心", "shy": "害羞", "love": "喜欢", "surprised": "惊讶",
    "confused": "困惑", "calm": "平静", "sad": "难过", "angry": "生气",
}

# ---------------------------------------------------------------- 触摸语音预合成
# 摸她时说的那几句短话，提前合成好放着：点第三下时直接播，不用等引擎现算
# （现算一次要好几秒，反馈感全没了）。「两次动作一次语音」的节奏在前端控制。
TOUCH_REGIONS_DEFAULT = [
    {
        "id": "arm_l", "label": "左臂",
        "x0": 0.00, "x1": 0.20, "y0": 0.26, "y1": 0.86, "motion": "Angry",
        "lines": [
            {"text": "手臂也不行啦", "emotion": "angry"},
            {"text": "痒，别挠", "emotion": "shy"},
        ],
    },
    {
        "id": "arm_r", "label": "右臂",
        "x0": 0.80, "x1": 1.00, "y0": 0.26, "y1": 0.86, "motion": "Angry",
        "lines": [
            {"text": "手臂也不行啦", "emotion": "angry"},
            {"text": "痒，别挠", "emotion": "shy"},
        ],
    },
    {
        "id": "head", "label": "头",
        "x0": 0.00, "x1": 1.00, "y0": 0.00, "y1": 0.30, "motion": "Shy",
        "lines": [
            {"text": "唔……别戳啦", "emotion": "shy"},
            {"text": "头发要乱了", "emotion": "shy"},
        ],
    },
    {
        "id": "chest", "label": "胸",
        "x0": 0.00, "x1": 1.00, "y0": 0.30, "y1": 0.52, "motion": "Surprise",
        "lines": [
            {"text": "嗯？干嘛呀", "emotion": "surprised"},
            {"text": "别乱碰那里", "emotion": "angry"},
        ],
    },
    {
        "id": "belly", "label": "腹",
        "x0": 0.00, "x1": 1.00, "y0": 0.52, "y1": 0.72, "motion": "Tilt",
        "lines": [
            {"text": "痒……", "emotion": "shy"},
            {"text": "摸头要收费的", "emotion": "confused"},
        ],
    },
    {
        "id": "legs", "label": "腿",
        "x0": 0.00, "x1": 1.00, "y0": 0.72, "y1": 0.92, "motion": "Angry",
        "lines": [
            {"text": "腿也不行", "emotion": "angry"},
            {"text": "在的在的", "emotion": "happy"},
        ],
    },
    {
        "id": "feet", "label": "脚",
        "x0": 0.00, "x1": 1.00, "y0": 0.92, "y1": 1.00, "motion": "Shake",
        "lines": [
            {"text": "脚下也不行", "emotion": "angry"},
            {"text": "别踩啦", "emotion": "confused"},
        ],
    },
]
TOUCH_VOICE_DIR = TTS_OUTDIR / "touch"

# ---------------------------------------------------------------- 自定义触摸区域
# 后台「自定义触摸区域」把画好的图形存到这里；没有这个文件就用上面那份内置的，
# 「恢复默认」= 删掉文件。坐标一律相对命中框（pet.js 的 HIT_BOX）归一化，0~1。
TOUCH_REGIONS_PATH = BASE / "data" / "touch_regions.json"
TOUCH_REGIONS_MAX = 16
TOUCH_SHAPES_MAX = 8
# 命中框：模型包围盒里裁出「头 + 身子」那一块。以前只在 pet.js:34 写死，
# 现在由服务端下发 —— 后台编辑器和桌宠必须用同一份，否则画好的形状会整体错位。
# 纵向原本 0.10~0.64（长 0.54）：腿和脚被挤在框底最后 4% 里，脚根本够不到。
# 先延到 0.856（×1.4）还是不到脚，现在直接到 1.0 —— 框底就是模型底，脚在框内。
TOUCH_HIT_BOX = {"x0": 0.33, "x1": 0.67, "y0": 0.10, "y1": 1.0}
_TOUCH_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def _clamp01(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(1.0, max(0.0, number))


def _normalize_shape(raw) -> dict | None:
    """一个图形：矩形或椭圆。

    椭圆也按外接矩形存，判定用椭圆方程 —— 这样前端画出来的形状和命中范围
    是同一个东西，不会出现「看着在里面、点着不算」。
    """
    if not isinstance(raw, dict):
        return None
    kind = "ellipse" if str(raw.get("type") or "") == "ellipse" else "rect"
    x, y = _clamp01(raw.get("x")), _clamp01(raw.get("y"))
    w, h = _clamp01(raw.get("w")), _clamp01(raw.get("h"))
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w < 0.01 or h < 0.01:
        return None
    return {
        "type": kind,
        "x": round(x, 4), "y": round(y, 4),
        "w": round(w, 4), "h": round(h, 4),
    }


def _default_regions() -> list[dict]:
    """内置那份 + 由 x0..x1 折出来的矩形，让前端只认 shapes 一种形状。"""
    out = []
    for region in TOUCH_REGIONS_DEFAULT:
        item = dict(region)
        item["shapes"] = [{
            "type": "rect",
            "x": region["x0"], "y": region["y0"],
            "w": round(region["x1"] - region["x0"], 4),
            "h": round(region["y1"] - region["y0"], 4),
        }]
        out.append(item)
    return out


def _normalize_regions(raw) -> list[dict]:
    """洗一遍外部传进来的区域表：非法 id / 越界坐标 / 超量一律丢掉。"""
    if not isinstance(raw, list):
        return []
    defaults = {r["id"]: r for r in _default_regions()}
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw[:TOUCH_REGIONS_MAX]:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or "").strip()
        if not _TOUCH_ID_RE.match(rid) or rid in seen:
            continue
        base = defaults.get(rid, {})
        label = str(item.get("label") or base.get("label") or rid).strip()[:8]
        motion = str(item.get("motion") or base.get("motion") or "Touch")[:24]
        shapes = []
        for shape in (item.get("shapes") or [])[:TOUCH_SHAPES_MAX]:
            norm = _normalize_shape(shape)
            if norm:
                shapes.append(norm)
        lines = item.get("lines")
        if not isinstance(lines, list):
            lines = base.get("lines") or []
        clean_lines = []
        for line in lines[:8]:
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip()[:60]
            if not text:
                continue
            clean_lines.append({
                "text": text,
                "emotion": str(line.get("emotion") or "calm")[:16],
            })
        seen.add(rid)
        out.append({
            "id": rid, "label": label, "motion": motion,
            "lines": clean_lines, "shapes": shapes,
        })
    return out


def load_touch_regions() -> list[dict]:
    try:
        data = json.loads(TOUCH_REGIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_regions()
    raw = data.get("regions") if isinstance(data, dict) else data
    return _normalize_regions(raw) or _default_regions()


def save_touch_regions(regions) -> list[dict]:
    clean = _normalize_regions(regions)
    if not clean:
        raise ValueError("区域表是空的")
    TOUCH_REGIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "//note": "由桌宠后台「自定义触摸区域」维护；坐标相对命中框，0~1",
        "regions": clean,
    }
    tmp = TOUCH_REGIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TOUCH_REGIONS_PATH)
    return clean


def reset_touch_regions() -> list[dict]:
    try:
        TOUCH_REGIONS_PATH.unlink()
    except OSError:
        pass
    return _default_regions()


TOUCH_REGIONS = load_touch_regions()


def regions_payload() -> dict:
    """发给前端的区域表（桌宠和后台编辑器共用这一份）。

    x0..y1 是按图形并集算出来的，只作一版兼容；新前端一律读 shapes。
    """
    regions = []
    for region in TOUCH_REGIONS:
        shapes = [dict(s) for s in region.get("shapes") or []]
        regions.append({
            "id": region["id"],
            "label": region["label"],
            "motion": region.get("motion", ""),
            "lines": [dict(line) for line in region["lines"]],
            "shapes": shapes,
            "voice": touch_voice_status(region),
            "x0": round(min((s["x"] for s in shapes), default=0.0), 4),
            "y0": round(min((s["y"] for s in shapes), default=0.0), 4),
            "x1": round(max((s["x"] + s["w"] for s in shapes), default=0.0), 4),
            "y1": round(max((s["y"] + s["h"] for s in shapes), default=0.0), 4),
        })
    return {
        "hit_box": dict(TOUCH_HIT_BOX),
        "custom": TOUCH_REGIONS_PATH.is_file(),
        "emotions": [{"key": k, "label": v} for k, v in EMOTION_LABELS.items()],
        "regions": regions,
    }

# ---------------------------------------------------------------- 群聊唤醒关键词
# 真源放在这里，由桌宠后台写、AstrBot 那个插件按 mtime 现读（见插件的
# _refresh_keywords）。为什么不直接改插件配置：AstrBot 的热重载只监听插件
# 目录内的文件，而配置在 data/config/ 下，改了既不会触发重载、也不会生效。
ASTRBOT_DATA = BASE.parent / "qq" / "astrbot" / "data"
WAKE_KEYWORDS_PATH = ASTRBOT_DATA / "plugin_data" / "group_wake_keywords.json"
WAKE_PLUGIN_CONFIG = ASTRBOT_DATA / "config" / "astrbot_plugin_group_wake_window_config.json"
WAKE_KEYWORDS_MAX = 20      # 关键词最多几个
WAKE_KEYWORD_LEN_MAX = 20   # 单个关键词最长几个字


def _clean_keywords(raw) -> list[str]:
    """清洗关键词：去空白、去重、丢空的，限制个数和长度。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        word = str(item or "").strip()
        if not word:
            continue
        word = word[:WAKE_KEYWORD_LEN_MAX]
        if word not in out:
            out.append(word)
        if len(out) >= WAKE_KEYWORDS_MAX:
            break
    return out


def load_wake_keywords() -> tuple[list[str], str]:
    """返回 (关键词, 来源)。来源是 'file' 还是 'plugin-config'，方便后台显示。"""
    try:
        data = json.loads(WAKE_KEYWORDS_PATH.read_text(encoding="utf-8"))
        words = _clean_keywords(data.get("keywords") if isinstance(data, dict) else data)
        if words:
            return words, "file"
    except (OSError, json.JSONDecodeError):
        pass
    # 还没有那份文件：把插件配置里的现有值读出来当默认，别让界面空着
    try:
        cfg = json.loads(WAKE_PLUGIN_CONFIG.read_bytes().decode("utf-8-sig"))
        return _clean_keywords(cfg.get("keywords")), "plugin-config"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return [], "none"


def save_wake_keywords(words: list[str]) -> None:
    """写关键词文件。插件靠 mtime 发现变化，写完立刻生效，不用重启 AstrBot。"""
    WAKE_KEYWORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"keywords": words, "//note": "由桌宠后台维护；改完即时生效"}
    tmp = WAKE_KEYWORDS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(WAKE_KEYWORDS_PATH)


# ---------------------------------------------------------------- QQ 托管
# 轮盘里那个「开/关托管」挪到后台「运行状态」了。后台是系统浏览器打开的
# （main.js 用 shell.openExternal），拿不到 Electron 的 IPC，所以只能由
# 8848 这个服务端来做，动作和主进程那套保持一致：
#   停 = 杀掉 6185/6199/5099/3000/3001 上的监听进程
#   起 = 跑 qq\启动-小鱼.ps1
QQ_PORTS = (6185, 6199, 5099, 3000, 3001)
QQ_SCRIPT = BASE.parent / "qq" / "启动-小鱼.ps1"


def _port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def qq_online(timeout: float = 0.4) -> bool:
    """按 6185（AstrBot）判断 QQ 那套在不在跑 —— 和轮盘用的是同一个口径。"""
    return _port_open(6185, timeout)


def qq_any_running() -> bool:
    """退出时用：只看 6185 会漏掉「AstrBot 已经死了、SnowLuma 还活着」的半截状态。

    那种状态下 qq_online() 是 False，但 SnowLuma 还在占着 5099 / 3000，
    不清理的话它会在桌宠退出后一直挂着（而且再也连不上 OneBot）。
    """
    return _port_open(6185) or _port_open(5099)


def _run_powershell(args: list[str], wait: bool = False, timeout: float = 20) -> int:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *args]
    if wait:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
        return proc.returncode
    # 不 wait：脚本自己会去拉 SnowLuma / AstrBot，跑起来就行
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    return 0


def qq_stop(timeout: float = 20, orphans: bool = True) -> int:
    """停掉 QQ 那套：按端口停 +（可选）按命令行补杀孤儿。别碰 8848 / 8898。

    Args:
        timeout: 给 PowerShell 进程的上限（秒）。
        orphans: 要不要顺带按命令行扫孤儿进程。

    2026-11-04（第二十五轮）修：这个函数以前**必然超时**。

    实测（本机，服务全在跑）：
        整条命令的**查询部分**就要 4.2 秒，加上 taskkill 循环必然超过 6 秒，
        而退出路径传的正是 timeout=6。
        日志里那一堆 subprocess.TimeoutExpired 就是这么来的。
    更糟的是**超时之后什么都没做**：PowerShell 被 kill，QQ 留在原地。

    逐段实测耗时：
        PowerShell 进程启动本身        ~1.1 秒
        端口查询（foreach 5 个端口）    +2.6 秒
        Get-CimInstance 全量扫进程      +1.2 秒

    所以拆成两段，各自有说得清的超时：
      1. 端口段（快）：退出路径只跑这段 —— 端口上的进程是主要目标。
      2. 孤儿段（慢）：只在非退出路径跑，或留给下次启动时清理。
    """
    # ---- 1. 端口段：一次查询所有端口（比 foreach 逐个查快一倍）----
    ports = ",".join(str(port) for port in QQ_PORTS)
    ps_ports = "\n".join([
        # 不要加 -State Listen（第二十二轮）：本机有些监听器的 State 是空串，
        # 带这个过滤会漏掉它们，端口上的进程就找不到。
        #
        # 一次 Get-NetTCPConnection 再 Where LocalPort -in，
        # 比 foreach 逐个查快一倍（实测 4.2s -> 1.9s）。
        "$pids = Get-NetTCPConnection -ErrorAction SilentlyContinue |",
        "  Where-Object { $_.LocalPort -in " + ports + " -and $_.OwningProcess -ne 0 } |",
        "  Select-Object -ExpandProperty OwningProcess -Unique",
        "foreach ($procId in $pids) { taskkill /PID $procId /T /F 2>$null | Out-Null }",
    ])
    code = _run_powershell(["-Command", ps_ports], wait=True, timeout=max(timeout, 8.0))

    if not orphans:
        return code

    # ---- 2. 孤儿段：按命令行补一刀 ----
    # 端口全死、进程还活着的孤儿（SnowLuma 崩过就是这样，见项目地图坑 #25）。
    # 只认 qq\SnowLuma 和 qq\astrbot 两个目录，不按解释器名字匹配 ——
    # 那样会杀掉 IDE 和别的项目的 node / python。
    ps_orphans = "\n".join([
        "$pids = Get-CimInstance Win32_Process -Filter \"Name='node.exe' OR Name='python.exe'\" -ErrorAction SilentlyContinue |",
        "  Where-Object { $_.CommandLine -and ($_.CommandLine -like '*\\qq\\SnowLuma*' -or $_.CommandLine -like '*\\qq\\astrbot*') } |",
        "  Select-Object -ExpandProperty ProcessId",
        "foreach ($procId in $pids) { taskkill /PID $procId /T /F 2>$null | Out-Null }",
    ])
    _run_powershell(["-Command", ps_orphans], wait=True, timeout=10.0)
    return code


def qq_start() -> dict:
    if not QQ_SCRIPT.is_file():
        return {"ok": False, "error": f"找不到 {QQ_SCRIPT}"}
    _run_powershell(["-File", str(QQ_SCRIPT)])
    return {"ok": True}


# ---------------------------------------------------------------- 网关代理
# 后台是浏览器页面，不该持有 gateway.token。所有要进网关管理接口的请求
# 都从这里代转：token 只在服务端读、只在服务端用。
GATEWAY_BASE = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8898").rstrip("/")
GATEWAY_TOKEN_PATH = BASE.parent / "gateway" / "data" / "gateway.token"


def gateway_token() -> str:
    env = os.environ.get("GATEWAY_TOKEN", "").strip()
    if env:
        return env
    try:
        return GATEWAY_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def gateway_request(path: str, method: str = "GET", payload: dict | None = None,
                    timeout: float = 15) -> tuple[int, dict]:
    """代后台调网关，返回 (状态码, JSON)。网关没起来就是 502。"""
    headers = {"Content-Type": "application/json"}
    token = gateway_token()
    if token:
        headers["X-Gateway-Token"] = token
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(GATEWAY_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {"error": f"HTTP {exc.code}"}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 502, {"error": f"网关没连上：{exc}"}


def touch_region_at(x: float, y: float) -> dict | None:
    """按命中框内的归一化坐标找区域（0~1，x 从左到右、y 从上到下）。

    判定顺序 = 区域顺序，区域内再按图形顺序：先命中先算。
    一个都没命中返回 None —— 以前退回 TOUCH_REGIONS[0]（左臂），
    等于点空白处被判成摸手臂，说的还是「手臂也不行啦」。
    """
    for region in TOUCH_REGIONS:
        for shape in region.get("shapes") or []:
            if _shape_hit(shape, x, y):
                return region
    return None


def _shape_hit(shape: dict, x: float, y: float) -> bool:
    """矩形直接比，椭圆用外接矩形套椭圆方程 —— 画出来什么样就按什么样判。"""
    sx, sy = float(shape["x"]), float(shape["y"])
    sw, sh = float(shape["w"]), float(shape["h"])
    if shape.get("type") == "ellipse":
        rx, ry = sw / 2, sh / 2
        if rx <= 0 or ry <= 0:
            return False
        dx = (x - (sx + rx)) / rx
        dy = (y - (sy + ry)) / ry
        return dx * dx + dy * dy <= 1
    return sx <= x <= sx + sw and sy <= y <= sy + sh


def _region_by_id(region_id: str) -> dict | None:
    return next((r for r in TOUCH_REGIONS if r["id"] == region_id), None)


def _touch_voice_path_for(region: dict, index: int) -> Path | None:
    """某个区域第 index 句预合成语音的缓存路径。

    文件名里带 hash：文本、情绪、引擎、声线任一变了就换文件，旧缓存自动失效。
    改台词时旧文件会先被清掉（见 rebuild_touch_voices），不留垃圾。
    """
    if not (0 <= index < len(region.get("lines") or [])):
        return None
    item = region["lines"][index]
    engine_cfg = CFG.get(TTS_ENGINE)
    base_instr = engine_cfg.get("instruction", "") if isinstance(engine_cfg, dict) else ""
    seed = "|".join([
        item["text"], item["emotion"], str(TTS_ENGINE),
        str(getattr(tts, "voice_id", "") or ""), str(base_instr),
    ])
    key = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return TOUCH_VOICE_DIR / f"{region['id']}-{index}-{key}.wav"


def _touch_voice_path(region_id: str, index: int) -> Path | None:
    region = _region_by_id(region_id)
    if region is None:
        return None
    return _touch_voice_path_for(region, index)


# 合成任务的状态：{"state": running|done|error, "error": "..."}
# 后台线程写、接口读，用锁兜住；卡片上的「语音 2/2」就是从这里来的。
_TOUCH_VOICE_STATUS: dict[str, dict] = {}
_TOUCH_VOICE_LOCK = threading.Lock()


def touch_voice_files(region_id: str) -> list[Path]:
    if not TOUCH_VOICE_DIR.is_dir():
        return []
    return sorted(TOUCH_VOICE_DIR.glob(f"{region_id}-*.wav"))


def clear_touch_voices(region_id: str) -> int:
    """删掉这个区域的所有缓存语音，返回删了几个。

    改台词后必须删：文件名带 hash，不删的话旧句子会一直躺在目录里，
    以后翻缓存会看到一堆已经不存在的话。
    """
    removed = 0
    for path in touch_voice_files(region_id):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def synthesize_touch_voices(region: dict) -> dict:
    """把一个区域的台词合成到缓存。已经存在的（同 hash）跳过。"""
    total = len(region.get("lines") or [])
    ready = 0
    engine_cfg = CFG.get(TTS_ENGINE)
    default_instr = engine_cfg.get("instruction", "") if isinstance(engine_cfg, dict) else ""
    for index, item in enumerate(region.get("lines") or []):
        path = _touch_voice_path_for(region, index)
        if path is None:
            continue
        if path.exists():
            ready += 1
            continue
        if not (tts and tts.ready):
            return {"total": total, "ready": ready, "error": "语音引擎还没准备好"}
        try:
            instruction = EMOTION_INSTRUCTION.get(item["emotion"], default_instr)
            wav = tts.speak_wav(item["text"], instruction=instruction)
            TOUCH_VOICE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(wav), str(path))
            ready += 1
            print(f"[tts] 合成触摸语音 {region['id']}[{index}]: {item['text']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            return {
                "total": total, "ready": ready,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {"total": total, "ready": ready, "error": ""}


def rebuild_touch_voices(region: dict) -> None:
    """先删旧语音，再合成新的。后台线程里跑，不挡接口。"""
    region_id = region["id"]
    with _TOUCH_VOICE_LOCK:
        # 连点两次「确定」不该跑两遍：已经在合成就直接返回，
        # 否则两次 clear 交错，会把刚写好的文件删掉。
        if (_TOUCH_VOICE_STATUS.get(region_id) or {}).get("state") == "running":
            return
        _TOUCH_VOICE_STATUS[region_id] = {"state": "running", "error": ""}
    removed = clear_touch_voices(region_id)
    print(f"[tts] {region_id}: 清掉旧语音 {removed} 个，开始重合成 {len(region.get('lines') or [])} 句", flush=True)
    result = synthesize_touch_voices(region)
    with _TOUCH_VOICE_LOCK:
        _TOUCH_VOICE_STATUS[region_id] = {
            "state": "error" if result["error"] else "done",
            "error": result["error"],
        }


def touch_voice_status(region: dict) -> dict:
    total = len(region.get("lines") or [])
    ready = 0
    for index in range(total):
        path = _touch_voice_path_for(region, index)
        if path is not None and path.is_file():
            ready += 1
    with _TOUCH_VOICE_LOCK:
        job = dict(_TOUCH_VOICE_STATUS.get(region["id"]) or {})
    state = job.get("state")
    if not state:
        state = "done" if total and ready >= total else ("empty" if not ready else "partial")
    return {"total": total, "ready": ready, "state": state, "error": job.get("error", "")}


def prewarm_touch_voices() -> None:
    """引擎就绪后把各区域的触摸语音合成一遍。

    全程失败都不影响任何功能：播不出语音就只做动作（前端 404 会静默降级），
    下次启动再试。
    """
    if not tts_ready.wait(timeout=300):
        return
    # 刚起来那一小会儿先让给真正的对话请求，别一开机就跟人抢引擎
    time.sleep(8)
    for region in TOUCH_REGIONS:
        if not (tts and tts.ready):
            return
        result = synthesize_touch_voices(region)
        if result["error"]:
            print(f"[tts] 预合成 {region['id']} 停在中途: {result['error']}", flush=True)

# 「怎么样地说」：模型自己填的语气描述，原样当 instruction 用。
# 只描述怎么念（快慢、气息、哭腔），描述「谁在念」的词会把克隆音色带跑，所以切掉。
TONE_BAD_WORDS = ("男声", "女声", "童声", "男音", "女音", "少女音", "大叔音", "萝莉音",
                  "音色", "声线", "老人", "小孩", "年纪", "苍老", "稚嫩", "年轻", "中年")

# QQ 那条路（AstrBot 插件）只能传纯文本，拿不到结构化的 tone 字段，所以网关
# 把语气标记塞在正文最前面一起带过来。这里是全项目唯一的合成出口，不管谁调、
# 走哪条路，只要带了标记都在这儿取出来当 instruction，并从文本里删掉——
# 标记绝不能漏进合成文本，否则会被念成「语气 哽咽着说」。
TONE_INLINE_RE = re.compile(r"[{｛]\s*语气\s*[:：]\s*([^{}｛｝\n]{1,80})\s*[}｝]")
# 插件会自己删 [语音]，但直接调 /api/tts 的（探针脚本、以后别的入口）不一定，
# 念出来就是「语音」两个字，在这儿一并清掉。
VOICE_MARKER_INLINE_RE = re.compile(r"[\[【（(]\s*(?:语音|voice)\s*[\]】）)]", re.I)


def _split_inline_tone(text: str) -> tuple[str, str]:
    """从正文里取出 {语气:...}，返回 (干净正文, 语气)。没有标记就是原样返回。"""
    tone = ""
    hit = TONE_INLINE_RE.search(text)
    if hit:
        tone = hit.group(1).strip()
        text = TONE_INLINE_RE.sub("", text)
    text = VOICE_MARKER_INLINE_RE.sub("", text)
    return text.strip(), tone


def _clean_tone(value) -> str:
    """语气描述的长度和内容兜底。网关已经洗过一遍，这里是第二道。"""
    text = re.sub(r"\s+", "", str(value or ""))
    text = re.sub(r"[{}【】（）()\[\]“”\"'’‘]", "", text)
    for bad in TONE_BAD_WORDS:
        text = text.replace(bad, "")
    text = re.sub(r"^[的]+", "", text)   # 切掉「苍老」后可能剩个孤零零的「的」
    return text.strip("，,。.、；;：:·")[:40]


HOST = CFG.get("host", "127.0.0.1")
PORT = int(CFG.get("port", 8848))
MAX_CHARS = int(CFG.get("max_chars", 200))
# 语音引擎端口（排障信息里要用；8848 是 HTTP，别混）
ENGINE_PORT = int((CFG.get("breeze") or {}).get("port", 8849))

tts = None
tts_ready = threading.Event()
tts_error: str | None = None
_tts_lock = threading.Lock()
# 收到 /api/shutdown 后置位：让 _tts_supervisor 停止重拉引擎（见那边的注释）
_shutting_down = threading.Event()

# 工坊里这个模型的 .moc3 被 XOR 0x73 混淆过（DigitalMate 自己解密后加载）。
# 我们只在内存里解密后发送，绝不修改原文件。
MOC3_XOR_KEY = 0x73
_moc_cache: dict[str, tuple[float, bytes]] = {}


def read_model_asset(target: Path) -> bytes:
    if target.suffix.lower() != ".moc3":
        return target.read_bytes()

    mtime = target.stat().st_mtime
    cached = _moc_cache.get(str(target))
    if cached and cached[0] == mtime:
        return cached[1]

    data = target.read_bytes()
    if not data.startswith(b"MOC3"):
        decoded = bytes(b ^ MOC3_XOR_KEY for b in data)
        if decoded.startswith(b"MOC3"):
            data = decoded
    _moc_cache[str(target)] = (mtime, data)
    return data


MIME = {
    ".moc3": "application/octet-stream",
    ".model3.json": "application/json",
    ".json": "application/json",
    ".js": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".wav": "audio/wav",
    ".ico": "image/x-icon",
}


# ---------------------------------------------------------------- 事件总线（SSE）
class EventBus:
    """极简发布/订阅：桌宠页面通过 SSE 订阅后端事件。

    队列满就丢最旧的一条，宁可丢事件也不阻塞聊天请求。
    """

    def __init__(self, maxsize: int = 64):
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subs)


EVENTS = EventBus()


# ---------------------------------------------------------------- 模型配置生成
def _q(name: str) -> str:
    return urllib.parse.quote(name)


def _asset(rel: str) -> str:
    """相对路径 + mtime 版本号，文件变了浏览器就会重新拉。"""
    try:
        version = int((LIVE2D_DIR / rel).stat().st_mtime)
    except OSError:
        version = 0
    return f"{_q(rel)}?v={version}"


def build_model_settings() -> dict:
    """根据工坊目录实际内容生成 model3.json，不改动原文件。"""
    entries = list(LIVE2D_DIR.iterdir())
    files = {p.name for p in entries if p.is_file()}
    dirs = [p for p in entries if p.is_dir()]

    moc = next((n for n in sorted(files) if n.endswith(".moc3")), None)
    if not moc:
        raise RuntimeError(f"在 {LIVE2D_DIR} 找不到 .moc3")

    texture = None
    for d in dirs:
        for f in sorted(d.iterdir()):
            if f.name.lower().startswith("texture") and f.suffix.lower() == ".png":
                texture = f"{d.name}/{f.name}"
                break
        if texture:
            break
    if not texture:
        for n in sorted(files):
            if n.lower().startswith("texture") and n.lower().endswith(".png"):
                texture = n
                break
    if not texture:
        raise RuntimeError("找不到贴图 texture_*.png")

    physics = next((n for n in files if n.endswith(".physics3.json")), None)
    display = next((n for n in files if n.endswith(".cdi3.json")), None)
    pose = next((n for n in files if n.endswith(".pose3.json")), None)

    refs: dict = {
        "Moc": _asset(moc),
        "Textures": [_asset(texture)],
    }
    if physics:
        refs["Physics"] = _asset(physics)
    if display:
        refs["DisplayInfo"] = _asset(display)
    if pose:
        refs["Pose"] = _asset(pose)

    exp_dir = LIVE2D_DIR / "EXP"
    if exp_dir.is_dir():
        exprs = []
        for f in sorted(exp_dir.glob("*.exp3.json")):
            name = f.name[: -len(".exp3.json")]
            exprs.append({"Name": name, "File": _asset(f"EXP/{f.name}")})
        if exprs:
            refs["Expressions"] = exprs

        idle = exp_dir / "Idle.motion3.json"
        if idle.exists():
            refs["Motions"] = {
                "Idle": [{"File": _asset("EXP/Idle.motion3.json"),
                          "FadeInTime": 0.6, "FadeOutTime": 0.6}]
            }

    # 工坊模型通常只带一个 Idle 动作，摸不到反馈、表情也没有动作配合。
    # 这里把仓库里自带的小动作（web/motions/*.motion3.json）挂上去：
    # 文件放在仓库里可以版本管理，工坊目录那 5.8G 是不入库的。
    motions_dir = WEB / "motions"
    if motions_dir.is_dir():
        extra = {}
        for f in sorted(motions_dir.glob("*.motion3.json")):
            extra[f.name[: -len(".motion3.json")]] = [{
                "File": "/model/motions/" + _q(f.name) + "?v=" + str(int(f.stat().st_mtime)),
                "FadeInTime": 0.25,
                "FadeOutTime": 0.35,
            }]
        if extra:
            refs.setdefault("Motions", {}).update(extra)

    return {
        "Version": 3,
        "FileReferences": refs,
        "Groups": [
            {"Target": "Parameter", "Name": "LipSync", "Ids": ["ParamMouthOpenY"]},
            {"Target": "Parameter", "Name": "EyeBlink",
             "Ids": ["ParamEyeLOpen", "ParamEyeROpen"]},
        ],
    }


# ---------------------------------------------------------------- 语音引擎
def _cleanup_tts_cache():
    """启动时收一下缓存：只留最近若干个，**不要整锅倒掉**。

    以前这里是无条件全删，结果「语音缓存」点开永远是空的 —— 连重启前刚生成的
    那几句也一起没了，跟这个按钮的用途正好相反。现在交给 _prune_tts_cache，
    和生成时的策略保持一致。
    """
    TTS_OUTDIR.mkdir(parents=True, exist_ok=True)
    _prune_tts_cache()


# 语音文件保留最近这么多个，供后台「语音缓存」翻听。
# 以前是服务完就删，文件夹永远是空的；现在留一点，又不至于无限长。
TTS_KEEP_FILES = 60


def _prune_tts_cache(keep: int | None = None) -> None:
    """只留最近的 keep 个 wav（按修改时间）。只动顶层文件，不碰 touch/ 子目录。

    keep 默认取模块级的 TTS_KEEP_FILES，**在调用时读**而不是定义时绑定，
    这样调这个值立刻生效（测试也靠这点）。
    """
    keep = TTS_KEEP_FILES if keep is None else keep
    try:
        files = [p for p in TTS_OUTDIR.glob("*.wav") if p.is_file()]
    except OSError:
        return
    if len(files) <= keep:
        return
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _launch_tts() -> None:
    """（重新）创建 TTS 引擎实例并等它就绪。调用方负责加锁。"""
    global tts, tts_error
    sys.path.insert(0, str(BASE))

    breeze_cfg = CFG.get("breeze") or {}
    if TTS_ENGINE == "breeze":
        from tts_breeze import Breeze

        tts = Breeze(
            server=str(BREEZE_SERVER),
            model=str(BREEZE_MODEL),
            port=int(breeze_cfg.get("port", 8849)),
            outdir=str(TTS_OUTDIR),
            instruction=breeze_cfg.get("instruction", ""),
            voice_id=breeze_cfg.get("voice_id", ""),
            cfg_scale=float(breeze_cfg.get("cfg_scale", 1.0)),
            seed=int(breeze_cfg.get("seed", 42)),
            max_new_tokens=int(breeze_cfg.get("max_new_tokens", 0)),
            chunk_chars=int(breeze_cfg.get("chunk_chars", 28)),
            chunk_gap_ms=int(breeze_cfg.get("chunk_gap_ms", 250)),
            # 「已有实例正在加载」时最多等它多久。加载期间端口还没 bind，
            # 不等就会 spawn 出第二个引擎，白吃 3.5GB 显存（见 tts_breeze.start() ③）
            # 默认 45：和 tts_breeze.Breeze 保持一致，比真实冷加载上限多留 15s 余量
            load_wait=float(breeze_cfg.get("load_wait", 45)),
        )
    else:
        from tts_fastvits import FastVITS

        tts = FastVITS(
            exe=str(FASTVITS_EXE),
            model=str(VITS_MODEL),
            config=str(VITS_CONFIG),
            outdir=str(TTS_OUTDIR),
            speaker=int(CFG.get("speaker", 126)),
            length_scale=float(CFG.get("length_scale", 1.0)),
            noise_scale=float(CFG.get("noise_scale", 0.67)),
            noise_scale_w=float(CFG.get("noise_scale_w", 0.8)),
        )
    tts.start(log=lambda m: print(m, flush=True))
    tts_error = None
    tts_ready.set()


def boot_tts():
    global tts_error
    if not CFG.get("start_tts_on_boot", True):
        return
    try:
        with _tts_lock:
            _launch_tts()
        if tts is not None and getattr(tts, "adopted", False):
            print(
                f"[server] 语音引擎就绪（复用已在运行的实例 PID="
                f"{getattr(tts, 'adopted_pid', None) or '未知'}，未新建进程）",
                flush=True,
            )
        else:
            print("[server] 语音引擎就绪", flush=True)
    except Exception as exc:  # noqa: BLE001
        tts_error = f"{type(exc).__name__}: {exc}"
        print(f"[server] 语音引擎启动失败: {tts_error}", flush=True)
        traceback.print_exc()


def _tts_supervisor():
    """语音引擎掉线（崩溃 / 被系统回收 / 复用的实例没了）时自动重拉。

    「复用的引擎」（tts.adopted=True，见 tts_breeze.start()）有两种情况要分开：
      - 还健康 → tts.ready 为真，这里直接跳过，**绝不重拉**。
        以前 ready 只认 self.proc，adopted 时 proc 是 None，会被判成掉线，
        于是每 5~60 秒白拉一个必死的 exe（端口还被孤儿占着）。
      - 已掉线 → 说明那个半常驻实例真的没了，正常拉起一个新的。
    """
    global tts, tts_error
    backoff = 5.0
    fails = 0
    max_fails = 3
    while True:
        time.sleep(3)
        # /api/shutdown 已经把引擎停了，这时候绝不能再拉起来
        # （否则退出流程白做，3.5GB 显存又被占回去）
        if _shutting_down.is_set():
            return
        if tts and tts.ready:
            backoff = 5.0
            fails = 0
            continue
        failed = None
        with _tts_lock:
            if _shutting_down.is_set():
                return
            if tts and tts.ready:
                fails = 0
                continue
            if tts:
                if getattr(tts, "adopted", False):
                    # 复用的实例没了：不是我们杀错了，是它自己退了
                    print("[tts] 复用的语音引擎已不在线，重新拉起一个新实例", flush=True)
                else:
                    code = tts.proc.poll() if tts.proc else None
                    print(f"[tts] 引擎不在线（退出码 {code}），重启中…", flush=True)
                try:
                    tts.stop()
                except Exception:  # noqa: BLE001
                    pass
                tts = None
            try:
                _launch_tts()
                if tts is not None and getattr(tts, "adopted", False):
                    print("[tts] 已复用运行中的引擎，未新建进程", flush=True)
                else:
                    print("[tts] 引擎已重新就绪", flush=True)
                backoff = 5.0
                fails = 0
            except Exception as exc:  # noqa: BLE001
                failed = f"{type(exc).__name__}: {exc}"
        # 退避等待放在锁外：否则重试期间 /api/tts 会被一起堵住
        if failed:
            tts_error = failed
            fails += 1
            if fails >= max_fails:
                # 熔断：连续失败说明不是「偶发掉线」，而是端口被占 / 显存不够这类
                # 结构性故障。再拉只会每次白吃 3.5GB 显存（引擎先加载模型再 bind
                # 端口），把整张卡顶满。停在这里，把原因留在 /api/status 里等人看。
                print(
                    f"[tts] 连续 {fails} 次重启失败，停止自动重拉：{failed}。"
                    f"请检查 {ENGINE_PORT} 端口占用 / 显存是否够，然后重启桌宠。",
                    flush=True,
                )
                return
            print(f"[tts] 重启失败：{failed}，{backoff:.0f}s 后重试", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ---------------------------------------------------------------- 退出（/api/shutdown）
def stop_tts_force() -> bool:
    """停语音引擎，并尽量真正释放显存（只给退出用）。

    支持 force 的实现（Breeze）会连「复用来的」引擎一起终止 —— 复用来的引擎
    平时 stop() 只解引用不杀进程，但退出场景用户要的就是把 3.5GB 显存还回去。
    不支持 force 的实现（FastVITS）退回无参 stop()。
    返回是否成功停掉。
    """
    engine = tts
    if engine is None:
        return False
    try:
        params = inspect.signature(engine.stop).parameters
        if "force" in params:
            engine.stop(force=True)
        else:
            engine.stop()
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


def _stop_qq_for_exit() -> bool:
    """退出时把 QQ 托管一起停掉。

    为什么必须一起停：AstrBot 的 provider 指着网关 8898，而网关归桌宠管
    （Electron 启动时 spawn、退出时收尾）。桌宠一退网关就没了，留着
    SnowLuma + AstrBot 只会让群里每条消息都失败——用户看到的是「她不回话」，
    日志里是一串连接被拒。所以「退出」的语义是整套退出。

    失败只打日志：清理 QQ 是顺带，绝不能因为它挡住退出。
    """
    if not qq_any_running():
        return False
    try:
        # 6 秒：要小于 main.js 那边 /api/shutdown 的 8 秒超时，
        # 留 2 秒给停引擎，否则响应还没发出去客户端就已经放弃了。
        # ⚠ 2026-11-04（第二十五轮）：退出路径**只做端口段**（orphans=False）。
        # 理由：整条命令（端口 + 全量扫进程 + taskkill）单纯查询就要 4.2 秒，
        # 加上 taskkill 必然超 6 秒 → 超时后什么都没做，QQ 原地留着。
        # 端口上的进程是退出的主要目标；孤儿留给「停 QQ」按钮或下次启动收。
        qq_stop(timeout=6, orphans=False)
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


def _exit_now(delay: float = 0.2) -> None:
    """让进程自己退出。

    用 os._exit 跳过 main() 的 finally —— 清理必须在调用前已经做完。
    延迟是为了让 /api/shutdown 的响应先写回客户端，否则 Electron 那边
    只会看到「连接被重置」，没法判断优雅退出是否成功。
    """
    time.sleep(delay)
    print("[server] /api/shutdown 已处理，进程退出", flush=True)
    os._exit(0)


# ---------------------------------------------------------------- 聊天
_AGENT_CFG = CFG.get("agent") or {}
chat_agent = None
chat_agent_error: str | None = None
_chat_agent_lock = threading.Lock()


def get_chat_agent():
    """懒加载 agent_bridge（失败不影响模型/语音）。"""
    global chat_agent, chat_agent_error
    if chat_agent is not None or not _AGENT_CFG.get("enabled", True):
        return chat_agent
    with _chat_agent_lock:
        if chat_agent is None:
            try:
                sys.path.insert(0, str(BASE))
                from agent_bridge import ChatAgent

                cwd = _AGENT_CFG.get("cwd", "workspace")
                cwd_path = Path(cwd) if Path(cwd).is_absolute() else BASE / cwd
                chat_agent = ChatAgent(
                    cwd=str(cwd_path),
                    audit_file=str(BASE / "audit.log"),
                    approval_mode=_AGENT_CFG.get("approval_mode", "auto"),
                    max_history=int(_AGENT_CFG.get("max_history", 8)),
                )
                print(f"[server] 智能体就绪，工作目录 {cwd_path}", flush=True)
            except Exception as exc:  # noqa: BLE001
                chat_agent_error = f"{type(exc).__name__}: {exc}"
                print(f"[server] 智能体初始化失败: {chat_agent_error}", flush=True)
                traceback.print_exc()
    return chat_agent


def handle_chat(text: str) -> dict:
    agent = get_chat_agent()
    if agent is None:
        return {"reply": f"智能体不可用：{chat_agent_error or '未启用'}", "emotion": None}
    return agent.chat(text)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "DMBrowser/0.1"
    protocol_version = "HTTP/1.1"

    # ---- 工具
    #: 后台页面 / Electron 窗口都在这个 origin 下；跨站的 Origin 一律拒绝。
    #:
    #: 2026-11-04（T-03）：以前 8848 这个口子**没有任何身份概念**，
    #: `_client_is_local()` 只判来源 IP，而浏览器发起的跨站请求 client_address
    #: 同样是 127.0.0.1 —— 那道检查对 CSRF 完全无效。于是：
    #:   * 任何网页一张 `<img src="http://127.0.0.1:8848/api/shutdown">` 就能关掉整套；
    #:   * 跨站 text/plain POST `/api/chat` 能借 local_pet=True 以主人身份对话。
    #: 现在统一在入口拦 Origin + Content-Type + 安全响应头。
    ALLOWED_ORIGINS = frozenset({
        "http://127.0.0.1:8848",
        "http://localhost:8848",
    })
    #: 请求体上限。`_read_json` 原来完全不看大小，一个 Content-Length 就能打满内存。
    MAX_BODY_BYTES = 8 * 1024 * 1024

    #: 这些路径的 **GET/HEAD** 不需要 Origin 校验（只读、无副作用、或本来给外部探活）。
    #:
    #: ⚠ 注意只豁免 GET/HEAD。`/api/wake-keywords` 和 `/api/touch-regions` 两个路径
    #: GET 是读、POST 是**写盘**，如果按路径统一豁免，POST 就跟着漏过去了。
    #: 所以豁免要**按方法**判，不能按路径判。
    PUBLIC_GET_PATHS = frozenset({
        "/api/status",
        "/api/pet-config",
        "/api/wake-keywords",
        "/api/touch-regions",
        "/api/touch-voice-status",
        "/health",
    })

    def _origin_ok(self) -> bool:
        """Origin / Sec-Fetch-Site 判定。

        规则（fail-closed）：
          * 有 Origin 且不在白名单 → 拒；
          * 没有 Origin 但 `Sec-Fetch-Site: cross-site` → 拒（现代浏览器的跨站请求一定带）；
          * 没有 Origin 且没有 Sec-Fetch-* → **放行**。这是 curl / 本机进程 /
            EventSource / 老客户端的形态，它们本来就能读 `data/` 下的东西，
            不靠 Origin 防护；真正挡跨站的是下面"有 Origin 就必须在白名单里"。

        ⚠ 2026-11-08（后台白屏的真凶）：
        白名单原来只认带端口的 `http://127.0.0.1:8848`，但实测有浏览器给
        **同源子资源**（/console.css、/console.js）发的是 `origin: http://127.0.0.1`
        —— 没有端口。于是 HTML 能拿到（导航请求不带 Origin），
        CSS/JS 全被判 403，页面就是一张没样式的白页，而且从服务器侧
        怎么看都是"资源好好的"。

        现在放宽成：**同源判定不看端口写法**。只要 Origin 的
        scheme+host 是本机，且端口要么省略、要么就是本服务的端口，就算同源。
        跨站（别的 host，或本机但第三方端口）依旧拒绝 —— 这条放宽堵不住 CSRF，
        因为攻击页面不可能把 Origin 伪造成 127.0.0.1。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            return self._is_self_origin(origin)
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return fetch_site != "cross-site"

    #: 本机各种等价写法。同源判定只比较 scheme+host，端口另行校验。
    SELF_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

    def _is_self_origin(self, origin: str) -> bool:
        """Origin 是不是"本服务自己"。

        允许的形态（都视为同源）：
            http://127.0.0.1:8848   http://localhost:8848      ← 完全匹配
            http://127.0.0.1        http://localhost          ← 浏览器省略默认端口
            http://127.0.0.1:<别的端口>  同 host 但不是本服务 → 拒
        """
        low = origin.lower().rstrip("/")
        if low in self.ALLOWED_ORIGINS:
            return True
        try:
            parsed = urllib.parse.urlsplit(low)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if host not in [h.strip("[]") for h in self.SELF_HOSTS]:
            return False
        # 端口：省略时视为默认（浏览器对 80/443 会省略），或就是本服务端口
        port = parsed.port
        return port is None or port == self.server.server_address[1]

    def _guard_request(self, path: str) -> bool:
        """入口统一校验。不通过时已经把响应写回去了，调用方直接 return。

        Args:
            path: 已 unquote 的路径。

        ⚠ 2026-11-08：**只读的静态资源不再做 Origin 校验。**
        原因（实测证据，后台白屏的真凶）：浏览器给 /console.css、/console.js
        这类同源子资源发的 origin 是 `http://127.0.0.1`（不带端口），
        被白名单判 403，于是页面只有 HTML、没有样式和脚本。
        静态资源是**只读**的，跨站读走也不构成 CSRF写入；
        真正需要防的是带副作用的 /api/*，那些照旧校验。
        """
        read_only = self.command in ("GET", "HEAD")
        if read_only and path in self.PUBLIC_GET_PATHS:
            return True
        if read_only and not path.startswith("/api/"):
            # 只读静态资源：放行。渲染页面必需的 CSS/JS/模型贴图都在这一类里。
            return True
        if not self._origin_ok():
            self._log_rejected(path)
            self._send_json(403, {"ok": False, "error": "forbidden origin"})
            return False
        return True

    def _log_rejected(self, path: str) -> None:
        """把被 Origin 判定拒掉的请求记下来（含全部请求头）。

        为什么要记：后台页曾出现「HTML 拿到了、CSS/JS 全是 403」，
        但复现不出来 —— 因为 403 取决于浏览器到底发了哪个 Origin /
        Sec-Fetch-Site，而这个值在服务器侧不记下来就永远看不见。
        log_message 是静音的，所以这里单独写文件。
        """
        try:
            lines = [
                "===== %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "%s %s" % (self.command, path),
                "client=%s" % (self.client_address,),
            ]
            for k, v in self.headers.items():
                lines.append("%s: %s" % (k, v))
            lines.append("")
            with open(BASE / "data" / "rejected_requests.log", "a",
                      encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:  # noqa: BLE001
            pass

    def _require_json_content_type(self) -> bool:
        """**带 body** 的 POST 必须 `application/json`。

        这一条专门挡"跨站简单请求"：`text/plain` / `application/x-www-form-urlencoded`
        是 CORS 安全列表类型，**不触发预检**，浏览器会真的把请求发出去。
        要求 JSON 之后，跨站想打带 body 的请求就必须过预检，而预检我们不给 CORS 头。

        没有 body 的 POST（`/api/reset`、`/api/open-tts-cache` 这类纯动作）不做此项要求——
        它们由 `_guard_request` 的 Origin 判定守着，加一道 Content-Type 只会误伤自家前端。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return True
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(415, {"ok": False, "error": "application/json required"})
            return False
        return True

    def _send_bytes(self, code: int, data: bytes, ctype: str,
                    cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        # 安全响应头（T-03）：后台页面跑在系统浏览器里，没有 X-Frame-Options
        # 就能被任意站点 iframe 套住做点击劫持（「清空记忆」「关闭托管」都在那页上）。
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, code: int, obj, cache: str = "no-store"):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(code, data, "application/json; charset=utf-8", cache)

    def _read_json(self) -> dict:
        """读请求体并当 JSON 解析。

        ⚠ 2026-11-04 改：引入**请求体缓存**（`_body_cache`）。
        原来这里是各分支"想读才读"，于是任何没调它的 POST 分支（`/api/reset`、
        `/api/open-tts-cache`、`/api/shutdown`…）都会把 body 留在 socket 里，
        keep-alive 下引发 RST（客户端看到 `ConnectionAbortedError [WinError 10053]`）。
        现在 `do_POST` 一进来就把 body **读完并存下**，这里只解析缓存，
        读不读都不再有残留。
        """
        if getattr(self, "_body_cache", None) is not None:
            raw = self._body_cache
            self._body_cache = b""
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        if length > self.MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(413, {"ok": False, "error": "payload too large"})
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _serve_file(self, path: Path, ctype: str | None = None,
                    cache: str = "no-store", decoder=None):
        try:
            data = decoder(path) if decoder else path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        if ctype is None:
            ctype = MIME.get(path.suffix.lower())
            if ctype is None:
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send_bytes(200, data, ctype, cache)

    # ---- SSE
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        q = EVENTS.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            EVENTS.unsubscribe(q)

    # ---- 退出
    def _client_is_local(self) -> bool:
        """只允许本机调用：/api/shutdown 会终止服务进程，不能暴露给外部。"""
        try:
            host = self.client_address[0]
        except (IndexError, TypeError):
            return False
        return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _shutdown(self):
        if not self._client_is_local():
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return
        # 先置位，避免 supervisor 在下面这 200ms 里又把引擎拉起来
        _shutting_down.set()
        # 顺序：先停 QQ（它正在往 8898 发请求），再停引擎，最后才回响应。
        # 两件都必须在这条线程里同步做完 —— _exit_now 走的是 os._exit，
        # 异步线程根本来不及跑。
        qq_stopped = _stop_qq_for_exit()
        stopped = stop_tts_force()
        self._send_json(200, {
            "ok": True,
            "tts_stopped": stopped,
            "qq_stopped": qq_stopped,
        })
        threading.Thread(target=_exit_now, daemon=True).start()

    # ---- 路由
    def do_GET(self):
        try:
            self._route_get()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json(500, {"error": "internal"})
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if not self._guard_request(path):
            return

        if path == "/api/wake-keywords":
            words, source = load_wake_keywords()
            self._send_json(200, {
                "keywords": words,
                "source": source,
                "max": WAKE_KEYWORDS_MAX,
                "max_len": WAKE_KEYWORD_LEN_MAX,
            })
            return

        if path == "/api/touch-regions":
            # 前端要跟服务端用同一份区域和文案：语音、气泡、判定范围不能对不上。
            # hit_box 也一起下发：以前只在 pet.js 里写死，编辑器拿不到就会整体错位。
            self._send_json(200, regions_payload())
            return

        if path == "/api/touch-voice-status":
            # 改完台词要等后台合成，卡片靠这个轮询「语音 2/2」
            self._send_json(200, {
                "regions": {r["id"]: touch_voice_status(r) for r in TOUCH_REGIONS}
            })
            return

        if path == "/api/touch-voice":
            query = urllib.parse.parse_qs(parsed.query)
            region_id = (query.get("r") or [""])[0]
            try:
                index = int((query.get("i") or ["-1"])[0])
            except (TypeError, ValueError):
                index = -1
            target = _touch_voice_path(region_id, index)
            if target is None or not target.is_file():
                # 还没合成好（引擎在加载/预合成没跑完）——前端会静默降级成只做动作
                self._send_json(404, {"error": "还没合成好"})
                return
            self._serve_file(target, "audio/wav")
            return

        if path == "/api/events":
            self._sse()
            return

        # 2026-11-04（T-03）：**GET 不再接 shutdown**。
        # 原来这里留了一个"方便 curl 调试"的 GET 分支，后果是任何网页
        # `<img src="http://127.0.0.1:8848/api/shutdown">` 就能关掉整套
        # （桌宠 + 网关 + QQ 托管 + 语音引擎），而且 log_message 是静音的，
        # 事后查不到是谁关的。退出这种有强副作用的动作只该走 POST。
        if path == "/api/shutdown":
            self._send_json(405, {"ok": False, "error": "use POST"})
            return

        if path == "/api/pet-config":
            self._send_json(200, {
                "hidden_parts": CFG.get("hidden_parts") or [],
                "model": LIVE2D_DIR.name,
            })
            return

        if path == "/api/qq-hosting":
            # 会拉起/杀掉进程，只允许本机
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            self._send_json(200, {
                "online": qq_online(),
                "ports": list(QQ_PORTS),
                "script": str(QQ_SCRIPT),
                "script_exists": QQ_SCRIPT.is_file(),
            })
            return

        if path == "/api/system-model":
            # 系统层用哪个模型（后台「系统模型」卡片）。
            # 和 /api/system 一样只允许本机：它会读/写网关的模型配置。
            # token 由 gateway_request 在服务端加 —— 浏览器拿不到也不该拿到。
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            code, body = gateway_request("/admin/system-model")
            self._send_json(code, body)
            return

        if path == "/api/role-model":
            # 角色层用哪个模型（后台「对话模型」卡片）。
            #
            # ⚠ 2026-11-09 补：前端原来**直连** `http://127.0.0.1:8898/admin/model`，
            # 而那个接口要 `X-Gateway-Token`，浏览器里拿不到 —— 于是永远 401，
            # 卡片显示「网关未连接」，下拉框是空的、地址也是空的。
            # 系统层早就有 `/api/system-model` 代理，角色层这张卡一直漏了。
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            code, body = gateway_request("/admin/model")
            self._send_json(code, body)
            return

        if path == "/api/keys":
            # API 密钥列表（后台「API 密钥」卡片）。返回的是打码后的样子，
            # 但仍然是敏感信息，只允许本机。
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            code, body = gateway_request("/admin/keys")
            self._send_json(code, body)
            return

        if path == "/api/system":
            # 系统层（DMN）状态：后台「她现在的状态」「内心OS」两张卡
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            code, body = gateway_request("/admin/system")
            self._send_json(code, body)
            return

        if path == "/api/identity":
            # 身份卡（2026-11-08）：主人 QQ 号 + 桌宠名字。
            # 和 /api/proactive-speak 一样只允许本机 —— 它暴露主人的 QQ 号。
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            code, body = gateway_request("/admin/identity")
            self._send_json(code, body)
            return

        if path == "/api/proactive-speak":
            # 主动说话总开关（2026-11-08）：后台「主动说话」卡片。
            # 只允许本机 —— 它控制她会不会主动弹气泡并出声，
            # 这是"打扰主人"的权限，不该让网页上的任意来源随手打开。
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            code, body = gateway_request("/admin/proactive-speak")
            self._send_json(code, body)
            return

        if path == "/api/daily-review":
            # 每日梳理期（P0-4）：后台「每日梳理期」卡片。
            # 和 /api/system 一样只允许本机 —— 它会暴露"她最近在想什么"。
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            code, body = gateway_request("/admin/daily-review")
            self._send_json(code, body)
            return

        if path == "/api/status":
            self._send_json(200, {
                "tts_ready": tts_ready.is_set(),
                "tts_error": tts_error,
                "tts_alive": bool(tts and tts.ready),
                "tts_engine": TTS_ENGINE,
                # True = 复用了启动前就在 8849 上的引擎（多半是上一次留下的孤儿），
                # 我们没 spawn、也不会 kill 它。排障时一眼能看出「声音是谁在出」。
                "tts_adopted": bool(tts and getattr(tts, "adopted", False)),
                "tts_pid": getattr(tts, "adopted_pid", None) if tts else None,
                "agent_enabled": bool(_AGENT_CFG.get("enabled", True)),
                "agent_ready": chat_agent is not None,
                "agent_error": chat_agent_error,
                "model": LIVE2D_DIR.name,
                "events": EVENTS.count,
            })
            return

        if path == "/model/settings.json":
            try:
                self._send_json(200, build_model_settings())
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path.startswith("/model/motions/"):
            # 仓库自带的小动作（web/motions/），不在工坊目录里。
            # 必须放在下面 /model/ 的通配分支之前，否则会被当成工坊里的相对路径 404。
            rel = path[len("/model/motions/"):]
            root = (WEB / "motions").resolve()
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                self._send_json(403, {"error": "forbidden"})
                return
            if target.is_file():
                self._serve_file(target, "application/json; charset=utf-8")
            else:
                self._send_json(404, {"error": "not found"})
            return

        if path.startswith("/model/"):
            rel = path[len("/model/"):]
            target = (LIVE2D_DIR / rel).resolve()
            try:
                target.relative_to(LIVE2D_DIR.resolve())
            except ValueError:
                self._send_json(403, {"error": "forbidden"})
                return
            if target.is_file():
                self._serve_file(target, cache="public, max-age=3600",
                                 decoder=read_model_asset)
            else:
                self._send_json(404, {"error": "not found"})
            return

        if path in ("/", "/index.html"):
            self._serve_file(WEB / "index.html", "text/html; charset=utf-8")
            return

        if path in ("/pet", "/pet.html"):
            self._serve_file(WEB / "pet.html", "text/html; charset=utf-8")
            return

        if path in ("/stage", "/stage.html"):
            self._serve_file(WEB / "stage.html", "text/html; charset=utf-8")
            return

        if path in ("/chatbox", "/chatbox.html"):
            self._serve_file(WEB / "chatbox.html", "text/html; charset=utf-8")
            return

        rel = path.lstrip("/")
        target = (WEB / rel).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        if target.is_file():
            self._serve_file(target)
        else:
            self._send_json(404, {"error": "not found"})

    def _drain_body(self) -> None:
        """把请求体读掉丢弃。

        ⚠ 2026-11-04（修偶发 `ConnectionAbortedError`）：
        `protocol_version = "HTTP/1.1"` + keep-alive 时，**服务端必须把请求体
        读完**，否则剩余的 body 字节还躺在 socket 缓冲区里：
          * 下一次 `handle_one_request` 会把它当成新的请求行来解析（得到垃圾）；
          * 或者我们直接关掉连接，客户端那边正在 `getresponse()` 的 socket 被 RST，
            抛 `ConnectionAbortedError [WinError 10053]`。

        任何"提前 return 不处理 body"的分支（403 / 415 / 404 / 405）都必须先调这个。

        实测症状：`test_非本机被拒` 单跑 10/10 过，但整个类连跑 **1/8 失败**、
        全量跑 **2/5 失败** —— 典型时序竞态，且失败点看起来像"服务端没响应"。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return
        if length > self.MAX_BODY_BYTES:
            # 太大就不断读了（读进来等于被打），直接关连接
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except (OSError, ValueError):
            self.close_connection = True

    def do_POST(self):
        # ① 进来先把请求体**整段读掉并存起来**（2026-11-04 修）。
        # 见 `_read_json` / `_drain_body` 的说明：不读完就必然在 keep-alive
        # 下留下残留字节，客户端 `getresponse()` 会被 RST。
        self._body_cache = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if 0 < length <= self.MAX_BODY_BYTES:
            try:
                self._body_cache = self.rfile.read(length)
            except (OSError, ValueError):
                self._body_cache = b""
                self.close_connection = True
        elif length > self.MAX_BODY_BYTES:
            # 太大不读（读进来等于被打），直接拒并关连接
            self.close_connection = True
            self._send_json(413, {"ok": False, "error": "payload too large"})
            return
        try:
            self._route_post()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json(500, {"error": "internal"})
            except Exception:
                pass
        finally:
            self._body_cache = b""

    def _tts_with_emotion(self, text: str, emotion: str | None, tone: str | None = None) -> None:
        """合成并返回 WAV。

        声线由 voice_id 锁死，instruction 只当语气方向：
        tone（模型自己写的「怎么样地说」）优先，没有才退回 emotion 的固定映射。
        """
        if not (tts and tts.ready):
            return
        instruction = None
        if TTS_ENGINE == "breeze":
            default = CFG.get("breeze", {}).get("instruction", "")
            custom = _clean_tone(tone)
            if custom:
                instruction = custom
            else:
                instruction = EMOTION_INSTRUCTION.get((emotion or "").lower(), default)
        wav = tts.speak_wav(text, instruction=instruction)
        data = wav.read_bytes()
        # 以前这里是立刻 unlink：文件夹永远是空的，后台想看/想听刚才那句就没得看。
        # 现在留着，只保留最近若干个，别让缓存无限长。
        _prune_tts_cache()
        self._send_bytes(200, data, "audio/wav")

    def _route_post(self):
        global TOUCH_REGIONS
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 入口统一校验（T-03）：先过 Origin，再要求 JSON。
        # 顺序有讲究：Origin 先过，挡掉跨站；Content-Type 后过，挡掉免预检的简单请求。
        #
        # ⚠ 每一步失败都先 `_drain_body()`：不读掉请求体就 return 会让
        # keep-alive 的 socket 里残留字节，客户端 `getresponse()` 会被 RST
        # （`ConnectionAbortedError [WinError 10053]`）。见 `_drain_body` 的说明。
        if not self._guard_request(path):
            self._drain_body()
            return
        if not self._require_json_content_type():
            self._drain_body()
            return

        if path == "/api/shutdown":
            self._shutdown()
            return

        if path == "/api/wake-keywords":
            body = self._read_json()
            words = _clean_keywords(body.get("keywords"))
            try:
                save_wake_keywords(words)
            except OSError as exc:
                self._send_json(500, {"error": f"写不进去：{exc.strerror or exc}"})
                return
            # 空列表是合法选择：那就只靠 @ 唤醒。提示一句，免得以为是没保存上。
            message = (
                "已保存。关键词清空了，以后只有 @ 她才会醒。"
                if not words
                else "已保存，下一条群消息就按新关键词判断。"
            )
            self._send_json(200, {"ok": True, "keywords": words, "message": message})
            return

        if path == "/api/touch-regions":
            body = self._read_json()
            # 改台词前先记一份，存完比对哪些区域的台词真的变了
            before = {r["id"]: [dict(x) for x in r["lines"]] for r in TOUCH_REGIONS}
            try:
                saved = save_touch_regions(body.get("regions"))
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": f"写不进去：{exc.strerror or exc}"})
                return
            TOUCH_REGIONS = saved
            # 台词变了的区域：旧语音立刻删掉，再让引擎按新台词重合成。
            # 文件名带 hash，新台词天然指向新文件；不删旧的就是一堆永远用不上的 wav。
            rebuilding: list[str] = []
            voice_errors: list[dict] = []
            for region in saved:
                if before.get(region["id"]) == region["lines"]:
                    continue
                if not (tts and tts.ready):
                    # 引擎没起来：旧语音先删（它们已经不可能被命中），
                    # 但别假装合成成功，让界面提示稍后重试。
                    clear_touch_voices(region["id"])
                    with _TOUCH_VOICE_LOCK:
                        _TOUCH_VOICE_STATUS[region["id"]] = {
                            "state": "error",
                            "error": "语音引擎还没准备好，稍后点「重生成」",
                        }
                    voice_errors.append({
                        "id": region["id"],
                        "error": "语音引擎还没准备好，稍后点「重生成」",
                    })
                    continue
                threading.Thread(
                    target=rebuild_touch_voices, args=(dict(region),), daemon=True
                ).start()
                rebuilding.append(region["id"])
            # 桌宠窗口是常驻的，只在启动时拉过一次区域表；不推这一条，
            # 用户点完「确定」会发现怎么摸都没变化 —— 这是最容易翻车的地方。
            EVENTS.publish({"type": "touch-regions"})
            self._send_json(200, {
                "ok": True,
                "rebuilding": rebuilding,
                "voice_errors": voice_errors,
                **regions_payload(),
            })
            return

        if path == "/api/touch-voices/rebuild":
            body = self._read_json()
            region = _region_by_id(str(body.get("id") or "").strip())
            if region is None:
                self._send_json(404, {"ok": False, "error": "没有这个区域"})
                return
            if not (tts and tts.ready):
                self._send_json(503, {"ok": False, "error": "语音引擎还没准备好"})
                return
            threading.Thread(
                target=rebuild_touch_voices, args=(dict(region),), daemon=True
            ).start()
            self._send_json(200, {"ok": True, "state": "running"})
            return

        if path == "/api/touch-regions/reset":
            TOUCH_REGIONS = reset_touch_regions()
            EVENTS.publish({"type": "touch-regions"})
            self._send_json(200, {"ok": True, **regions_payload()})
            return

        if path == "/api/qq-hosting":
            if not self._client_is_local():
                self._send_json(403, {"ok": False, "error": "只允许本机调用"})
                return
            body = self._read_json()
            action = str(body.get("action") or "").strip()
            if action == "toggle":
                action = "stop" if qq_online() else "start"
            if action == "stop":
                code = qq_stop()
                self._send_json(200, {
                    "ok": code == 0,
                    "action": "stop",
                    "online": qq_online(),
                    "message": "已发出关闭指令，几秒后生效" if code == 0 else "关闭指令没跑成功",
                })
                return
            if action == "start":
                result = qq_start()
                if not result["ok"]:
                    self._send_json(400, {"ok": False, **result})
                    return
                self._send_json(200, {
                    "ok": True,
                    "action": "start",
                    "online": qq_online(),
                    "message": "正在拉起 QQ 托管，十几秒后连上",
                })
                return
            self._send_json(400, {"ok": False, "error": "action 只能是 start / stop / toggle"})
            return

        if path == "/api/system-model":
            # 保存系统层模型配置（服务商 / 地址 / 模型名 / key）。
            # 网关那边会先试连再落盘，连不上不动当前配置。
            if not self._client_is_local():
                self._send_json(403, {"ok": False, "error": "只允许本机调用"})
                return
            payload = self._read_json()
            code, body = gateway_request("/admin/system-model", method="POST", payload=payload)
            self._send_json(code, body)
            return

        if path == "/api/role-model":
            # 保存角色层模型配置。和上面系统层那条对称。
            if not self._client_is_local():
                self._send_json(403, {"ok": False, "error": "只允许本机调用"})
                return
            payload = self._read_json()
            code, body = gateway_request("/admin/model", method="POST", payload=payload)
            self._send_json(code, body)
            return

        if path in ("/api/system/tick", "/api/system/consolidate",
                    "/api/system/inner-os/clear"):
            if not self._client_is_local():
                self._send_json(403, {"ok": False, "error": "只允许本机调用"})
                return
            # 先把 body 读掉：不读就回包，Windows 上连接会被重置（客户端看到 10053）
            self._read_json()
            tail = path[len("/api/system"):]
            code, body = gateway_request("/admin/system" + tail, method="POST", payload={})
            self._send_json(code, body)
            return

        if path == "/api/identity":
            # 保存身份（后台「身份」卡片）：主人 QQ 号 + 桌宠名字。
            # 只允许本机 —— 改主人号等于换一个人拿全部特权。
            if not self._client_is_local():
                self._send_json(403, {"ok": False, "error": "只允许本机调用"})
                return
            payload = self._read_json()
            code, body = gateway_request("/admin/identity",
                                         method="POST", payload=payload)
            self._send_json(code, body)
            return

        if path == "/api/proactive-speak/toggle":
            # 开关主动说话（后台「主动说话」卡片）。
            if not self._client_is_local():
                self._send_json(403, {"ok": False, "error": "只允许本机调用"})
                return
            payload = self._read_json()
            code, body = gateway_request("/admin/proactive-speak",
                                         method="POST", payload=payload)
            self._send_json(code, body)
            return

        if path == "/api/daily-review/run":
            # 手动跑一次每日梳理（后台「每日梳理期」卡片）。
            # 只允许本机：它会真花一次模型调用，而且产出会进 inner_os。
            if not self._client_is_local():
                self._send_json(403, {"ok": False, "error": "只允许本机调用"})
                return
            self._read_json()
            code, body = gateway_request("/admin/system/daily-review",
                                         method="POST", payload={})
            self._send_json(code, body)
            return

        if path == "/api/open-tts-cache":
            # 打开语音缓存文件夹。只允许本机：这是个会弹出资源管理器的动作。
            if not self._client_is_local():
                self._send_json(403, {"error": "只允许本机调用"})
                return
            if not hasattr(os, "startfile"):
                self._send_json(501, {"error": "这个系统不支持直接打开文件夹"})
                return
            try:
                TTS_OUTDIR.mkdir(parents=True, exist_ok=True)
                os.startfile(str(TTS_OUTDIR))  # noqa: S606 —— 就是要交给资源管理器
            except OSError as exc:
                self._send_json(500, {"error": f"打不开：{exc.strerror or exc}"})
                return
            count = len([p for p in TTS_OUTDIR.glob("*.wav") if p.is_file()])
            self._send_json(200, {"ok": True, "path": str(TTS_OUTDIR), "count": count})
            return

        if path == "/api/tts":
            body = self._read_json()
            # 结构化 tone 优先；没有就从正文里的 {语气:...} 取（QQ 那条路走这里）
            text, inline_tone = _split_inline_tone(str(body.get("text") or ""))
            tone = body.get("tone") or inline_tone
            if not text:
                self._send_json(400, {"error": "empty text"})
                return
            if not tts_ready.is_set():
                self._send_json(503, {
                    "error": tts_error or "语音引擎还在加载，请稍等",
                    "loading": True,
                })
                return
            if not (tts and tts.ready):
                self._send_json(503, {
                    "error": tts_error or "语音引擎正在重启，请稍等",
                    "loading": True,
                })
                return
            try:
                emotion = body.get("emotion")
                log_tone = f"，语气「{_clean_tone(tone)}」" if _clean_tone(tone) else ""
                print(f"[tts] {text[:24]}…（{len(text)}字{log_tone}）", flush=True)
                self._tts_with_emotion(text[:MAX_CHARS], emotion, tone)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return

        if path == "/api/chat":
            body = self._read_json()
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json(400, {"error": "empty text"})
                return
            client = str(body.get("client") or "")
            try:
                result = handle_chat(text)
                reply = str(result.get("reply") or "")
                emotion = result.get("emotion")
                EVENTS.publish({
                    "type": "say",
                    "text": reply,
                    "emotion": emotion,
                    "tone": result.get("tone") or "",
                    "client": client,
                    "source": "chat",
                })
                self._send_json(200, result)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return

        if path == "/api/say":
            body = self._read_json()
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json(400, {"error": "empty text"})
                return
            event = {
                "type": "say",
                "text": text,
                "emotion": body.get("emotion"),
                "tone": body.get("tone") or "",
                "client": str(body.get("client") or ""),
                "source": "say",
            }
            EVENTS.publish(event)
            self._send_json(200, {"ok": True, "subscribers": EVENTS.count})
            return

        if path == "/api/reset":
            agent = get_chat_agent()
            if agent is not None:
                agent.reset()
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        # 静音默认访问日志（模型贴图很大，日志会刷屏）
        pass

    # ---- 连接健壮性（2026-11-04）
    def handle_one_request(self):
        """包一层，把「客户端正常断连」当正常事，别刷栈。

        ⚠ 为什么要覆盖（实测证据）：`protocol_version = "HTTP/1.1"` 意味着
        连接会复用，服务端在 `_send_json()` 之后会回到 `handle_one_request`
        等下一条请求。而浏览器 / urllib / Electron 发完就走，`rfile.readline()`
        直接抛 `ConnectionAbortedError [WinError 10053]` 或
        `ConnectionResetError [WinError 10054]`，`socketserver` 的兜底会把
        **整个栈**打印到 stderr。

        实测 `_t.err.log` 2,872 字节全是同一个栈反复刷 —— 真错误被噪音淹没。
        而且那条日志里中文还是乱码（WinError 的中文消息被以 UTF-8 写进 8-bit 输出）。

        这里把这两种异常转成"正常收工"：`close_connection = True` 就够，
        不需要打日志、也不需要回任何响应（对端已经走了）。
        """
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True


def main():
    if not LIVE2D_DIR.is_dir():
        print(f"[server] 模型目录不存在: {LIVE2D_DIR}")
        return 2

    _cleanup_tts_cache()

    if CFG.get("start_tts_on_boot", True):
        threading.Thread(target=boot_tts, daemon=True).start()
        threading.Thread(target=_tts_supervisor, daemon=True).start()
        # 触摸语音预合成：等引擎就绪后在后台慢慢做，不挡服务启动
        threading.Thread(target=prewarm_touch_voices, daemon=True).start()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"[server] http://{HOST}:{PORT}", flush=True)
    print(f"[server] 模型: {LIVE2D_DIR}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 退出", flush=True)
    finally:
        httpd.server_close()
        if tts:
            tts.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
