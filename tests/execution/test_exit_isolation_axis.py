"""退出链隔离守卫的「轴一致性」契约 (2026-09-27, 环境型假红修复的回归钉).

病史: tests/execution/test_exit_lifecycle.py 的 isolated fixture 原硬编码
`startswith('/tmp/')`, 而 pytest tmp_path/basetemp 跟随 TMPDIR — 本机
TMPDIR=/root/.hermes/cache/scratch (不在 /tmp 下) ⇒ **112 个退出链用例恒红**
(实测 102 failed + 110 errors; violations=[('sqlite', '.../pytest-of-root/.../exits.db')])。
恒红 = 隔离守卫失效: 真回归会被淹没在环境噪音里。修复 = 允许的临时根与
tempfile.gettempdir() 同轴。

本文件钉住两件事:
① 守卫强度不放松: 生产 DB 路径 (仓内 data/、/opt、~) 必须仍被拦;
② 轴一致性: fixture 的放行根必须覆盖 pytest 实际使用的 basetemp (tmp_path 所在树),
   防未来有人再退回硬编码 /tmp/ (变异 B 形态)。
判定表变异测试 (scratch 实测 3/3 抓住): A 无条件放行/B 退回硬编码/C 放行过宽。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

EXIT_TEST = REPO / "tests" / "execution" / "test_exit_lifecycle.py"


def _get_sql_connect():
    """从真 fixture 里捕获 sql_connect (不复制逻辑, 防逻辑漂移)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_t_exit_probe", EXIT_TEST)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_t_exit_probe"] = m
    spec.loader.exec_module(m)

    captured = {}

    class FakeMP:
        def setattr(self, *args, **kwargs):
            if len(args) >= 3 and args[0] is sqlite3 and args[1] == "connect":
                captured["sql_connect"] = args[2]

    fn = m.isolated
    w = getattr(fn, "__pytest_wrapped__", None)
    if w is not None:
        fn = getattr(w, "obj", w)
    try:
        gen = fn(FakeMP())
        next(gen)
    except Exception:
        pass  # futu/socket 段可能抛 — 只要 sql_connect 已捕获
    assert "sql_connect" in captured, "fixture 结构变了 — 更新本探针"
    return captured["sql_connect"]


@pytest.fixture(scope="module")
def sql_connect():
    return _get_sql_connect()


def _blocked(sc, path) -> bool:
    try:
        sc(path)
        return False
    except AssertionError:
        return True


@pytest.mark.parametrize("path", [
    "/opt/ftseek/FTSeek-US/data/ftseek_us.db",
    "data/exits.db",
])
def test_production_paths_are_blocked(sql_connect, path):
    """守卫强度: 生产/仓内相对路径必须拦 (变异 A/C 的回归钉)."""
    assert _blocked(sql_connect, path), f"生产路径未被拦 ⇒ 隔离守卫失效: {path}"


def test_home_path_is_blocked(sql_connect):
    assert _blocked(sql_connect, str(Path.home() / "trades.db"))


def test_memory_is_allowed(sql_connect):
    assert not _blocked(sql_connect, ":memory:")


def test_tempfile_root_is_allowed(sql_connect):
    """轴一致性: tempfile.gettempdir() (= pytest basetemp 的根) 必须放行.

    变异 B 钉: 退回硬编码 '/tmp/' 时, 本机 TMPDIR 下此测试红。
    """
    assert not _blocked(sql_connect, str(Path(tempfile.gettempdir()) / "probe.db"))


def test_tmp_path_is_allowed(sql_connect, tmp_path):
    """pytest 自己的 tmp_path (真 fixture 用的形态) 必须放行 — 这是 112 用例恒红的直接病因."""
    assert not _blocked(sql_connect, str(tmp_path / "exits.db"))


def test_fixture_does_not_hardcode_tmp_prefix():
    """源码级钉: 不得再出现硬编码 `startswith('/tmp/')` (变异 B 的形态).

    比行为测试更早失败且报错更直白 — 行为测试红时要读判定表才知道是轴问题。
    """
    src = EXIT_TEST.read_text(encoding="utf-8")
    assert "startswith('/tmp/')" not in src, (
        "isolated fixture 又硬编码了 /tmp/ 前缀 ⇒ 本机 TMPDIR 下 112 个退出链用例会恒红 "
        "(守卫失效)。放行根必须与 tempfile.gettempdir() 同轴。")
    assert "tempfile.gettempdir()" in src, "fixture 未使用 gettempdir() ⇒ 轴一致性靠什么保证?"
