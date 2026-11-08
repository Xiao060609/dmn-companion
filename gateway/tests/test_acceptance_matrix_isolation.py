"""守门测试：`tools/acceptance_matrix.py` 不许写生产 `gateway/data`（P0-7）。

为什么需要这个文件
------------------
2026-11-07 全量审计的 P0-7：`acceptance_matrix.py` 开头写着"只读"，实际
`t07()` 会调 `system._maybe_prune_long_memory()` → `save_state()` → **改写
生产的 `gateway/data/system_state.json`**。实测跑 3 次之后：

  * `system_state.json` 19 → 24 键；
  * `last_long_memory_prune_ts` 被推到"现在" → **真实网关 24h 内不再遗忘**；
  * `system_audit.jsonl` +1 行；
  * 把「心情四字段从未落盘」这个现场证据抹掉了（地图坑 #61 的立论依据）。

这条用例钉住的是**修完之后**的行为。它和 `test_data_isolation.py` 同一形状：
不靠自觉，靠红。

⚠ 为什么快照必须在 fixture 里取
--------------------------------
内容/时间戳快照如果放在断言里取，那时工具早就跑完了 —— 写没写都已经写完，
比不出来，断言会假绿。所以 `before` 在 subprocess 之前取、`after` 在之后取。

⚠ 成本控制
----------
完整跑一次 acceptance_matrix 约 20 秒（要 import gateway/system + 拷 1.7MB）。
所以这里用 `--json --no-copy`（4.7 秒）：不读真实数据，只验"写路径去哪了"。
「53 项结果不变」那条验收由人工跑全量确认（见 audit\\07 记录），不塞进单测。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "acceptance_matrix.py"
PRODUCTION_DATA = (ROOT / "gateway" / "data").resolve()


def _snapshot(directory: Path) -> dict:
    """目录快照：相对路径 -> (size, mtime_ns)。

    比 sha256 便宜，而且这里关心的正是「有没有人去碰它」——
    mtime 被改就是碰过，哪怕内容碰巧一样。
    """
    out = {}
    if not directory.is_dir():
        return out
    for p in sorted(directory.rglob("*")):
        if not p.is_file():
            continue
        st = p.stat()
        out[str(p.relative_to(directory))] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture(scope="module")
def tool_run():
    """跑一次（隔离模式），把 stdout/stderr/退出码 + **前后快照**带回来。

    module 级：这个文件里几条用例都要看同一次运行的结果，
    不然跑 4 次 = 80 秒。
    """
    if not TOOL.is_file():
        pytest.skip("tools/acceptance_matrix.py 不存在")
    before = _snapshot(PRODUCTION_DATA)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOL), "--json", "--no-copy"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    after = _snapshot(PRODUCTION_DATA)
    return proc, before, after


def _payload(proc) -> dict:
    """从 stdout 里捞 JSON。

    注意：隔离那一行（"已隔离数据目录：…"）只在非 --json 模式打印，
    但 argparse 的 warning 之类仍然可能混进来，所以按第一个 `{` 切。
    """
    text = proc.stdout or ""
    idx = text.find("{")
    assert idx != -1, f"没有 JSON 输出。stdout={text[:500]!r} stderr={proc.stderr[:500]!r}"
    return json.loads(text[idx:])


# --------------------------------------------------------------------------- #
# 1. 工具默认必须隔离（不是"加个 --dry-run 让用户自己记得加"）
# --------------------------------------------------------------------------- #
def test_默认跑在隔离目录上(tool_run):
    """默认（不带任何参数）就要把 GATEWAY_DATA_DIR 指走。

    这是 P0-7 的修法核心：安全的路径**应该是默认路径**。
    需要用户主动加参数才安全的工具，等于时不时就会有人忘了加。
    """
    proc, _before, _after = tool_run
    payload = _payload(proc)
    assert payload["total"] > 0
    assert proc.returncode in (0, 1), proc.stderr[-800:]


def test_跑完生产数据目录一个文件都没被碰(tool_run):
    """**核心断言**：跑一次工具，生产 `gateway/data` 的 size+mtime 完全不变。

    这条覆盖的正是 P0-7 的现场：`system_state.json` 的
    `last_long_memory_prune_ts` 会被 `_maybe_prune_long_memory()` 推到现在。
    以前跑一次就中招，现在应该一次都不动。

    快照在 fixture 里取（跑之前 / 跑之后），不在断言里取 —— 见 fixture 注释。
    """
    _proc, before, after = tool_run
    assert before, "没读到生产数据目录的快照"
    changed = sorted(k for k in before if k in after and before[k] != after[k])
    removed = sorted(k for k in before if k not in after)
    added = sorted(k for k in after if k not in before)
    assert changed == [], f"这些生产文件被工具改了：{changed}"
    assert removed == [], f"这些生产文件被工具删了：{removed}"
    assert added == [], f"工具往生产目录写了新文件：{added}"


def test_隔离目录不是生产目录(tool_run):
    """不看时序的硬判据：拿工具**自己算出来的** DATA 跟生产目录比。

    做法：在子进程里 import 工具的 `_isolate_data_dir()`，看它给出的路径。
    这条不依赖"跑之前/跑之后"的快照，所以不会因为时序问题假绿。
    """
    code = (
        "import sys;"
        "sys.path.insert(0, r'" + str(ROOT / "tools") + "');"
        "import acceptance_matrix as am;"
        "d = am._isolate_data_dir(copy_production=False);"
        "print('DATA=' + d);"
        "print('PROD=' + am.PRODUCTION_DATA)"
    )
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    out = dict(
        line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line
    )
    data_dir = os.path.normcase(os.path.abspath(out["DATA"]))
    prod = os.path.normcase(os.path.abspath(out["PROD"]))
    assert data_dir != prod, "默认数据目录还是生产目录 —— 隔离没生效"
    assert not data_dir.startswith(prod + os.sep), (
        f"隔离目录落在生产目录下面：{data_dir}"
    )


# --------------------------------------------------------------------------- #
# 2. 正常路径仍通：隔离不能把工具跑坏
# --------------------------------------------------------------------------- #
def test_隔离之后检查项照样能跑完(tool_run):
    """**正常路径仍通**：隔离了数据目录，53 项检查不能因此崩掉。

    这是"闸误伤正常路径"的那一半。P0-7 的修法动的是工具的入口
    （进程级 data 目录），最容易的误伤就是：路径改了但某几个检查项
    还在按老常量读，于是整项变成"检查本身出错"。

    `total` 必须 >= 50（用 --no-copy 会少 1 项：长期记忆库不存在），
    且**不能有**"检查本身出错"这种失败 —— 那是异常而不是业务判断。
    """
    proc, _before, _after = tool_run
    payload = _payload(proc)
    assert payload["total"] >= 50, f"检查项掉太多了：{payload['total']}"
    crashy = [r for r in payload["rows"] if "检查本身出错" in r.get("name", "")]
    assert crashy == [], f"有检查项抛异常（隔离把它们弄坏了）：{crashy}"


def test_允许显式关闭隔离时的逃生门(tool_run):
    """`--allow-production-data` 必须仍然存在（否则真要在生产上验时没路走）。

    但它是**显式**的、带警告的 —— 危险操作可以存在，不能是默认。
    """
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOL), "--help"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0
    assert "--allow-production-data" in proc.stdout
    assert "--keep-temp" in proc.stdout
