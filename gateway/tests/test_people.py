"""人物建模（DMN 阶段 5）的测试。

覆盖：
1. _people_key：pet → owner（主人固定 key）
2. 画像读写往返
3. **隔离**：不同 channel 分文件，互不影响
4. update_people_profile：模型返回 JSON 就写入
5. _extract_json_obj：纯 JSON / 带前后缀 / 非 JSON
"""
from __future__ import annotations

import sys
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "PEOPLE_DIR", str(tmp_path / "people"))


def test_people_key_pet是owner():
    """pet 固定 owner；其余按「可读前缀__哈希」编码。

    2026-11-04 改（P1-10）：原来断言 `qq_private == "qq_private"`（原样）。
    现在多了 `__<sha1 前 10 位>` 后缀 —— 因为原样命名有三个撞名通道
    （`a/b` 与 `a_b` 同文件、NTFS 大小写不敏感、>80 字符截断），
    撞上就是跨群画像串味。可读前缀保留是为了人打开 people 目录还认得出。
    """
    assert system._people_key("pet") == "owner"
    key = system._people_key("qq_private")
    assert key.startswith("qq_private__"), key
    assert len(key.split("__")[-1]) == 10, "哈希后缀应该是 10 位"


def test_people_key不同channel绝不撞名():
    """P1-10 的三个撞名通道，逐个钉住。"""
    # ① 不可逆替换：a/b 与 a_b 原来都变成 a_b
    assert system._people_key("qq_group:a/b") != system._people_key("qq_group:a_b")
    # ② NTFS 大小写不敏感：ABC 与 abc 原来同一个文件
    assert system._people_key("qq_group:ABC") != system._people_key("qq_group:abc")
    # ③ 长名字截断：共享前 80 字符的两个群原来合并
    long_a = "qq_group:" + "甲" * 100 + "A"
    long_b = "qq_group:" + "甲" * 100 + "B"
    assert system._people_key(long_a) != system._people_key(long_b)


def test_people_key稳定且可复现():
    """同一个 channel 每次算出来必须一样，否则每次重启都认成新的人。"""
    a = system._people_key("qq_group:示例群")
    b = system._people_key("qq_group:示例群")
    assert a == b
    assert a.startswith("qq_group_示例群__"), a


def test_画像读写往返(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    system._write_people_profile("pet", {"channel": "pet", "profile": {"称呼": "主人"}})
    assert system._read_people_profile("pet")["profile"]["称呼"] == "主人"


def test_不同channel隔离(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    system._write_people_profile("pet", {"profile": {"称呼": "主人"}})
    system._write_people_profile("qq_private", {"profile": {"称呼": "陌生人"}})
    # 各读各的，互不串
    assert system._read_people_profile("pet")["profile"]["称呼"] == "主人"
    assert system._read_people_profile("qq_private")["profile"]["称呼"] == "陌生人"


def test_update_写入画像(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    monkeypatch.setattr(
        system, "call_system",
        lambda msgs, **kw: ('{"称呼":"主人","作息":"常熬夜"}', {}, None),
    )
    assert system.update_people_profile("pet", [{"role": "user", "content": "x", "ts": 0}]) is True
    assert system._read_people_profile("pet")["profile"]["称呼"] == "主人"


def test_extract_json_obj():
    assert system._extract_json_obj('{"a":1}') == {"a": 1}
    assert system._extract_json_obj('前面 {"a":2} 后面') == {"a": 2}
    assert system._extract_json_obj("不是 JSON") is None
    assert system._extract_json_obj(None) is None
