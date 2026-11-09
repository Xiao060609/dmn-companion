import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
CFG_PATH = BASE / "config.json"

CFG = json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))

# TTS 资源默认用包内副本（assets\steam_workshop），不再依赖本机 Steam 库
WORKSHOP = Path(CFG.get("steam_workshop", "assets/steam_workshop/content/2488350"))
if not WORKSHOP.is_absolute():
    WORKSHOP = BASE / WORKSHOP
LIVE2D_DIR = WORKSHOP / CFG["live2d_item"]
FASTVITS_EXE = WORKSHOP / CFG["fastvits_item"] / "FastVITS.exe"
VITS_MODEL = WORKSHOP / CFG["vits_model_item"] / "G_953000.pth"
VITS_CONFIG = WORKSHOP / CFG["vits_model_item"] / "config.json"
TTS_OUTDIR = BASE / CFG.get("tts_outdir", "tts_cache")

# Breeze-TTS-2.cpp（Vulkan GPU + GGUF，3.5GB 模型常驻子进程）
BREEZE_DIR = Path(CFG.get("breeze_dir", r"<workspace>/Breeze-TTS-2.cpp\breeze-tts-2-v0.1.3-windows-x64-vulkan"))
BREEZE_SERVER = BREEZE_DIR / "breeze-server.exe"
BREEZE_MODEL = BREEZE_DIR / "models" / CFG.get("breeze_model", "breeze-tts-2-q8_0.gguf")
TTS_ENGINE = CFG.get("tts_engine", "breeze")   # "breeze" | "fastvits"
