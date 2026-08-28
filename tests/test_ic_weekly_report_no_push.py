"""scripts/ic_weekly_report.py --no-push 回归测试 (2026-08-28).

背景: 同一份 IC 周报每周被推两遍 — crontab (周日 17:00 CST 独立跑并自推) 与 Hermes
「周日综合复盘」Step 3 (周日 10:00 CST 跑, 该 job 的最终消息本身就是一次交付, 脚本
却又自己 send_text 一遍)。加 --no-push 让合并路径只打印不推, 缺省行为不变。

本测试只验开关接线, 不碰真实数据 (generate_report 打桩)。
回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "ic_weekly_report", PROJECT_ROOT / "scripts" / "ic_weekly_report.py"
)
iwr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iwr)


@pytest.fixture
def stub(monkeypatch):
    """打桩 generate_report + 飞书发送, 记录调用次数 (绝不让测试真发消息)."""
    import app.notifier.feishu as feishu
    sent = []
    monkeypatch.setattr(iwr, "generate_report", lambda *a, **k: "IC=0.03 报告正文")
    monkeypatch.setattr(feishu, "send_text", lambda *a, **k: sent.append(a) or True)
    return sent


def test_default_still_pushes(stub, capsys):
    iwr.main([])
    out = capsys.readouterr().out
    assert "IC=0.03 报告正文" in out and "已推送飞书" in out
    assert len(stub) == 1


def test_no_push_prints_only(stub, capsys):
    iwr.main(["--no-push"])
    out = capsys.readouterr().out
    assert "IC=0.03 报告正文" in out
    assert "推送" not in out
    assert stub == []


def test_unknown_flag_rejected(stub):
    with pytest.raises(SystemExit):
        iwr.main(["--push-everything"])
