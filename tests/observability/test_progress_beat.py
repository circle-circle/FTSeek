"""主线程进度心跳 (progress_beat) 回归测试 (2026-11-03, 冻结长轮次豁免).

守什么:
1. 节流: 默认 5s 内的第二次 ping 不写文件 (主路径上不能变成 syscall 风暴);
2. force 可绕过节流 (诊断/首轮用);
3. **绝不抛异常** —— 它处于交易主路径, 文件系统异常只能降级 (返回 False),
   不许把主循环带走。这条是"打点不制造新故障"的底线。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.observability import progress_beat as pb  # noqa: E402


@pytest.fixture()
def tmp_progress(tmp_path, monkeypatch):
    f = tmp_path / "progress-main"
    monkeypatch.setattr(pb, "_progress_path", f)
    monkeypatch.setattr(pb, "_last_ping_ts", 0.0)
    monkeypatch.setenv("FTSEEK_PROGRESS_THROTTLE_S", "5")
    return f


def test_ping_writes_file(tmp_progress):
    assert pb.progress_ping(force=True) is True
    assert tmp_progress.exists()


def test_throttle_blocks_second_ping(tmp_progress):
    assert pb.progress_ping(force=True) is True
    assert pb.progress_ping() is False, "5s 节流内的第二次 ping 不得落盘"


def test_throttle_expires(tmp_progress, monkeypatch):
    monkeypatch.setattr(pb, "_last_ping_ts", 0.0)
    assert pb.progress_ping(force=True) is True
    # 手动把上次时间推到 6s 前 ⇒ 节流应放行
    import time as _t
    monkeypatch.setattr(pb, "_last_ping_ts", _t.monotonic() - 6.0)
    assert pb.progress_ping() is True


def test_ping_never_raises_on_bad_path(monkeypatch, tmp_path):
    """父路径被一个普通文件占住 ⇒ mkdir(parents=True) 必失败 ⇒ 只能降级, 不许抛。

    (注: 不能拿"目标是目录"造错 —— pathlib.touch() 对已存在目录走 os.utime 成功返回。)
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(pb, "_progress_path", blocker / "progress-main")
    monkeypatch.setattr(pb, "_last_ping_ts", 0.0)
    assert pb.progress_ping(force=True) is False  # 降级, 不抛