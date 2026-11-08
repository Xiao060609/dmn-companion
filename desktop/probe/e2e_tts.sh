#!/usr/bin/env bash
# 一次性端到端验证：起 server.py -> 打 /api/tts 两次（不同情绪）-> 比音色 -> 收尾
set -u
cd "$(dirname "$0")/.." || exit 1

python -B server.py > /tmp/srv.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

for i in $(seq 1 60); do
  s=$(curl -s --noproxy '*' -m 3 http://127.0.0.1:8848/api/status 2>/dev/null)
  case "$s" in *'"tts_ready": true'*) echo "[ok] tts ready (${i}x2s)"; break;; esac
  sleep 2
done

out=probe/voice_candidates
curl -s --noproxy '*' -m 120 -X POST http://127.0.0.1:8848/api/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"我把桌上的杯子挪了挪，腾出一块地方。","emotion":"calm"}' \
  -o "$out/e2e_calm.wav" -w "[tts1] HTTP %{http_code} %{size_download}B\n"

curl -s --noproxy '*' -m 120 -X POST http://127.0.0.1:8848/api/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"外面好像要下雨了，你出门带伞了吗？","emotion":"surprised"}' \
  -o "$out/e2e_surprised.wav" -w "[tts2] HTTP %{http_code} %{size_download}B\n"

curl -s --noproxy '*' -m 120 -X POST http://127.0.0.1:8848/api/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"我把桌上的杯子挪了挪，腾出一块地方。","emotion":"calm"}' \
  -o "$out/e2e_calm2.wav" -w "[tts3] HTTP %{http_code} %{size_download}B\n"

echo "--- WAV 头 ---"
python -B - "$out" <<'PY'
import sys
from pathlib import Path
d = Path(sys.argv[1])
for n in ("e2e_calm.wav", "e2e_surprised.wav", "e2e_calm2.wav"):
    p = d / n
    if not p.exists() or p.stat().st_size < 44:
        print(f"{n} 缺失"); continue
    b = p.read_bytes()
    riff = int.from_bytes(b[4:8], "little")
    print(f"{n:<22} {len(b):>7}B  RIFF={riff}  {'OK' if riff == len(b)-8 else 'BAD'}")
PY

echo "--- 音色相似度（参考 = seed_42）---"
python -B probe/timbre_check.py "$out/seed_42.wav" "$out/e2e_calm.wav" "$out/e2e_surprised.wav" "$out/e2e_calm2.wav"
