#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
exec python3 -B gateway.py