"""hands.py 的安全边界单测（纯函数、无网络、无外部依赖，秒级可跑）。

覆盖架构评审报告第 6 节 L1 的四道闸：
    路径白名单 / 敏感路径黑名单 / 写扩展名白名单 / 读取与列举上限
外加 L1 加固（commit 4384e83）新增的符号链接 / NTFS junction 解析。

设计原则：
    * 完全自包含——每个用例用 ``scratch`` 造独立 sandbox，**不碰** 真实的
      ``gateway/sandbox/``，也不会写任何真实文件。
    * 不依赖真实 ``gateway/config.json`` 的取值：``isolated_hands`` fixture
      把 ``ROOTS`` / ``DENY_GLOBS`` / ``WRITE_EXT`` / ``MAX_READ`` / ``MAX_LIST``
      / ``ROOT`` 全部换成临时值，跑测试时的行为与线上配置无关。
    * 只断言行为（抛不抛 PolicyDenied、返回什么），不锁死实现细节。

跑法::

    python -m pytest gateway/tests/test_hands.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# hands.py 在 gateway/ 下，且 import 时会读 gateway/config.json（非敏感文件）。
GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import hands  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
# 注意：不用 pytest 的 tmp_path。它落在 %LOCALAPPDATA%\Temp 下，而真实的
# DENY_GLOBS 里有 `*/appdata/*` 这一条——sandbox 会被自己那条黑名单命中，
# 于是所有用例都变成「越界拒绝」，测不出任何东西。
# 所以临时目录放在仓库的 tmp/qa/_pytest_scratch 下（已在 .gitignore 里），
# 每个用例一个 uuid 子目录，用完即删。仍然完全不碰真实的 gateway/sandbox/。
SCRATCH_BASE = Path(__file__).resolve().parents[2] / "tmp" / "qa" / "_pytest_scratch"


@pytest.fixture()
def scratch() -> Path:
    """一个独立的临时工作目录（不在 AppData 下，用完删除）。"""
    SCRATCH_BASE.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="hands-", dir=str(SCRATCH_BASE)))
    try:
        yield workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture()
def sandbox(scratch: Path) -> Path:
    """一个干净的临时 sandbox 目录。"""
    root = scratch / "sandbox"
    root.mkdir()
    return root


@pytest.fixture()
def isolated_hands(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 hands 的模块级策略全部指向临时 sandbox，与真实配置解耦。

    Returns:
        临时 sandbox 的 realpath（Windows 上临时路径可能含 8.3 短名或
        junction，必须用 realpath 才能和 resolve() 的返回值比较）。
    """
    real_root = Path(os.path.realpath(sandbox))
    monkeypatch.setattr(hands, "ROOT", str(real_root))
    monkeypatch.setattr(hands, "ROOTS", [str(real_root)])
    monkeypatch.setattr(
        hands,
        "DENY_GLOBS",
        [
            "*/.ssh", "*/.ssh/*", "*/id_rsa*", "*/id_ed25519*",
            "*/.env", "*/.env.*", "*/secrets.json", "*/credentials*",
            "*/.git/*", "*/appdata/*", "*/ntuser.dat", "*/cookies*",
            "*/desktop.ini",
        ],
    )
    monkeypatch.setattr(hands, "WRITE_EXT", {".txt", ".md", ".json", ".log", ".csv"})
    monkeypatch.setattr(hands, "MAX_READ", 200_000)
    monkeypatch.setattr(hands, "MAX_LIST", 50)
    return real_root


def _norm(p) -> str:
    return os.path.realpath(str(p)).replace("\\", "/").lower()


# --------------------------------------------------------------------------- #
# 1. 正常路径放行（回归：别把合法路径也拒了）
# --------------------------------------------------------------------------- #
class TestAllowedPaths:
    def test_relative_path_inside_root(self, isolated_hands: Path):
        assert _norm(hands.resolve("a.txt")) == _norm(isolated_hands / "a.txt")

    def test_absolute_path_inside_root(self, isolated_hands: Path):
        target = isolated_hands / "sub" / "b.md"
        assert _norm(hands.resolve(str(target))) == _norm(target)

    def test_root_itself_is_allowed(self, isolated_hands: Path):
        assert _norm(hands.resolve(str(isolated_hands))) == _norm(isolated_hands)

    def test_nonexistent_file_still_resolves(self, isolated_hands: Path):
        """写新文件时目标还不存在，也必须能通过白名单。"""
        target = isolated_hands / "new" / "fresh.txt"
        assert _norm(hands.resolve("new/fresh.txt")) == _norm(target)

    def test_quotes_are_stripped(self, isolated_hands: Path):
        assert _norm(hands.resolve('"a.txt"')) == _norm(isolated_hands / "a.txt")

    def test_write_txt_md_json_allowed(self, isolated_hands: Path):
        for name in ("a.txt", "b.md", "c.json", "d.log", "e.csv"):
            hands.write_file(name, "内容")
            assert (isolated_hands / name).read_text(encoding="utf-8") == "内容"

    def test_read_file_returns_content(self, isolated_hands: Path):
        (isolated_hands / "note.txt").write_text("你好\n世界", encoding="utf-8")
        out = hands.read_file("note.txt")
        assert "note.txt" in out
        assert "你好" in out and "世界" in out

    def test_list_dir_lists_entries(self, isolated_hands: Path):
        (isolated_hands / "x.txt").write_text("1", encoding="utf-8")
        (isolated_hands / "sub").mkdir()
        out = hands.list_dir(".")
        assert "x.txt" in out and "sub/" in out


# --------------------------------------------------------------------------- #
# 2. 路径越界拒绝
# --------------------------------------------------------------------------- #
class TestPathEscapeDenied:
    def test_absolute_outside_root(self, isolated_hands: Path, scratch: Path):
        outside = scratch / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with pytest.raises(hands.PolicyDenied):
            hands.resolve(str(outside))

    def test_parent_traversal(self, isolated_hands: Path, scratch: Path):
        (scratch / "outside.txt").write_text("secret", encoding="utf-8")
        with pytest.raises(hands.PolicyDenied):
            hands.resolve("../outside.txt")

    def test_deep_traversal(self, isolated_hands: Path):
        with pytest.raises(hands.PolicyDenied):
            hands.resolve("../../../../../../etc/passwd")

    def test_empty_path(self, isolated_hands: Path):
        with pytest.raises(hands.PolicyDenied):
            hands.resolve("")

    def test_sibling_prefix_is_not_inside(self, isolated_hands: Path, scratch: Path):
        """`sandbox2` 不能被当成 `sandbox` 的子目录（前缀比较的经典坑）。"""
        sibling = scratch / "sandbox2"
        sibling.mkdir()
        (sibling / "x.txt").write_text("x", encoding="utf-8")
        with pytest.raises(hands.PolicyDenied):
            hands.resolve(str(sibling / "x.txt"))

    def test_read_file_outside_denied(self, isolated_hands: Path, scratch: Path):
        outside = scratch / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with pytest.raises(hands.PolicyDenied):
            hands.read_file(str(outside))


# --------------------------------------------------------------------------- #
# 3. 敏感路径黑名单
# --------------------------------------------------------------------------- #
class TestSensitiveDenied:
    @pytest.mark.parametrize(
        "rel",
        [
            ".ssh/id_rsa",
            ".ssh/id_ed25519",
            ".env",
            ".env.local",
            "secrets.json",
            "credentials.json",
            "cookies.sqlite",
            "desktop.ini",
        ],
    )
    def test_deny_globs_inside_root(self, isolated_hands: Path, rel: str):
        """这些路径即便落在 sandbox 内也必须被拒（黑名单是独立的第二道闸）。"""
        target = isolated_hands / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        with pytest.raises(hands.PolicyDenied):
            hands.resolve(rel)

    def test_git_dir_denied(self, isolated_hands: Path):
        target = isolated_hands / ".git" / "config"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        with pytest.raises(hands.PolicyDenied):
            hands.resolve(".git/config")


# --------------------------------------------------------------------------- #
# 4. 写扩展名白名单
# --------------------------------------------------------------------------- #
class TestWriteExtension:
    @pytest.mark.parametrize("name", ["evil.exe", "evil.bat", "evil.cmd", "evil.ps1", "evil.dll"])
    def test_executable_write_denied(self, isolated_hands: Path, name: str):
        with pytest.raises(hands.PolicyDenied):
            hands.write_file(name, "x")

    def test_no_extension_denied(self, isolated_hands: Path):
        with pytest.raises(hands.PolicyDenied):
            hands.write_file("noext", "x")

    def test_uppercase_extension_allowed(self, isolated_hands: Path):
        """.TXT 应当按 .txt 处理（实现里有 .lower()）。"""
        hands.write_file("UP.TXT", "x")
        assert (isolated_hands / "UP.TXT").exists()


# --------------------------------------------------------------------------- #
# 5. 读取 / 列举上限
# --------------------------------------------------------------------------- #
class TestLimits:
    def test_read_over_200kb_denied(self, isolated_hands: Path):
        big = isolated_hands / "big.txt"
        big.write_text("a" * 200_001, encoding="utf-8")
        with pytest.raises(hands.PolicyDenied):
            hands.read_file("big.txt")

    def test_read_exactly_at_limit_allowed(self, isolated_hands: Path):
        ok = isolated_hands / "ok.txt"
        ok.write_text("a" * 200_000, encoding="utf-8")
        assert "ok.txt" in hands.read_file("ok.txt")

    def test_read_reply_is_truncated_to_120_chars(self, isolated_hands: Path):
        """数据外泄面控制：回给模型的正文截到 120 字 + 提示语。"""
        long_text = "字" * 500
        (isolated_hands / "long.txt").write_text(long_text, encoding="utf-8")
        out = hands.read_file("long.txt")
        body = out.split("：", 1)[1]
        assert body.startswith("字" * 120)
        assert "后面还有" in body

    def test_list_dir_capped_at_max_list(self, isolated_hands: Path, monkeypatch):
        monkeypatch.setattr(hands, "MAX_LIST", 5)
        for i in range(12):
            (isolated_hands / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
        out = hands.list_dir(".")
        assert out.startswith("这里有 5 项")

    def test_list_dir_empty(self, isolated_hands: Path):
        empty = isolated_hands / "empty"
        empty.mkdir()
        assert hands.list_dir("empty") == "这个文件夹是空的"


# --------------------------------------------------------------------------- #
# 6. sandbox/sandbox 嵌套剥离（hands.py 原 :56-67 的逻辑，不能被改坏）
# --------------------------------------------------------------------------- #
class TestSandboxPrefixStripping:
    def test_model_written_root_name_is_stripped(self, isolated_hands: Path):
        """模型习惯写 `sandbox/a.txt`，必须落到 `sandbox/a.txt` 而不是 `sandbox/sandbox/a.txt`。"""
        resolved = Path(hands.resolve("sandbox/a.txt"))
        assert _norm(resolved) == _norm(isolated_hands / "a.txt")
        assert "sandbox/sandbox" not in _norm(resolved)

    def test_stripped_path_is_writable(self, isolated_hands: Path):
        hands.write_file("sandbox/written.txt", "x")
        assert (isolated_hands / "written.txt").exists()
        assert not (isolated_hands / "sandbox" / "written.txt").exists()


# --------------------------------------------------------------------------- #
# 7. 符号链接 / NTFS junction 绕过（L1 加固，commit 4384e83）
# --------------------------------------------------------------------------- #
def _make_junction(link: Path, target: Path) -> bool:
    """用 mklink /J 建 NTFS junction（不需要管理员权限）。失败返回 False。

    用 ``cwd`` + 相对路径传参：仓库路径里有中文（``G:\\小鱼``），
    cmd.exe 收到含非 ASCII 的绝对路径时会报「无效开关」，相对路径可以绕开。
    """
    try:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link.name, os.path.relpath(target, link.parent)],
            cwd=str(link.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and link.exists()


def _make_symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.fixture()
def escape_target(scratch: Path) -> Path:
    """sandbox 外面的一个「机密」目录。"""
    outside = scratch / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("机密内容", encoding="utf-8")
    return outside


class TestLinkEscape:
    @pytest.mark.parametrize("maker", [_make_junction, _make_symlink], ids=["junction", "symlink"])
    def test_link_escape_denied(self, isolated_hands: Path, escape_target: Path, maker):
        link = isolated_hands / "escape"
        if not maker(link, escape_target):
            pytest.skip("当前环境不支持创建 junction / symlink")
        try:
            # 绝对路径穿过链接
            with pytest.raises(hands.PolicyDenied):
                hands.resolve(str(link / "secret.txt"))
            # 相对路径穿过链接
            with pytest.raises(hands.PolicyDenied):
                hands.resolve("escape/secret.txt")
            # 真的去读也必须被拦
            with pytest.raises(hands.PolicyDenied):
                hands.read_file("escape/secret.txt")
        finally:
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                os.rmdir(link)

    def test_link_escape_target_untouched(self, isolated_hands: Path, escape_target: Path):
        """拒绝之后外部文件必须原样存在（不能因为误判而删了它）。"""
        link = isolated_hands / "escape2"
        if not _make_junction(link, escape_target):
            pytest.skip("当前环境不支持创建 junction")
        try:
            with pytest.raises(hands.PolicyDenied):
                hands.delete_file("escape2/secret.txt")
            assert (escape_target / "secret.txt").exists()
        finally:
            os.rmdir(link)

    def test_resolve_returns_realpath(self, isolated_hands: Path):
        """resolve() 必须返回解析后的真实路径，否则下游拿到的是链接路径。"""
        target = isolated_hands / "real.txt"
        target.write_text("x", encoding="utf-8")
        assert _norm(hands.resolve("real.txt")) == _norm(target)


# --------------------------------------------------------------------------- #
# 8. delete_file / execute 的边界
# --------------------------------------------------------------------------- #
class TestDeleteAndExecute:
    def test_delete_file_ok(self, isolated_hands: Path):
        (isolated_hands / "gone.txt").write_text("x", encoding="utf-8")
        hands.delete_file("gone.txt")
        assert not (isolated_hands / "gone.txt").exists()

    def test_delete_missing_raises_filenotfound(self, isolated_hands: Path):
        with pytest.raises(FileNotFoundError):
            hands.delete_file("nope.txt")

    def test_delete_directory_denied(self, isolated_hands: Path):
        (isolated_hands / "adir").mkdir()
        with pytest.raises(hands.PolicyDenied):
            hands.delete_file("adir")

    def test_delete_outside_denied(self, isolated_hands: Path, escape_target: Path):
        with pytest.raises(hands.PolicyDenied):
            hands.delete_file(str(escape_target / "secret.txt"))

    def test_execute_rejects_write_actions(self, isolated_hands: Path):
        """execute() 只跑读类动作，写/删必须走审批。"""
        with pytest.raises(hands.PolicyDenied):
            hands.execute("write_file", {"path": "x.txt", "content": "x"})

    def test_describe_delete_asks_for_confirmation(self, isolated_hands: Path):
        assert "删掉" in hands.describe("delete_file", {"path": "note.txt"})
