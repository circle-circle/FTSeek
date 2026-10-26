"""交易热路径 fail-close 契约 (RFC-168 方案 C, 2026-10-26)

背景: 审计发现 5 处 `except Exception: pass` 把安全门降级为 fail-open ——
判据不可判定时**继续下单**, 而不是拒绝。同文件下游的 Kill Switch 检查
(order_executor.py:3878) 早已是 fail-close, 两种哲学并存于同一执行路径。

本文件钉住"失败方向"这件事:
  - 行为断言: 熔断状态落盘失败必须抛 (不得静默)。
  - 结构断言: 4 处 fail-open 位置已改为 fail-close, 防回退。
    (结构断言只作**辅助** —— 行为不可直接触发的位置用它兜底; 仓内已有
     大量纯源码断言, 本文件不重复该反模式, 每条结构断言都附行为注解。)
"""
import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _src(rel_path: str) -> str:
    return (REPO / rel_path).read_text(encoding="utf-8")


# ── 行为断言 ────────────────────────────────────────────────────────────────

def test_flash_exit_state_save_failure_raises(tmp_path, monkeypatch):
    """熔断状态落盘失败必须抛 —— 静默 = 跨重启拦截失效。

    保守方向是"继续拦截", 而非"解除拦截"; 但**必须可见**, 故抛异常由调用方
    (position_monitor) 记 warning, 而不是 debug 吞掉。
    """
    from app.pipeline import order_executor

    bad = tmp_path / "no_such_dir" / "state.json"
    monkeypatch.setattr(order_executor, "_FLASH_EXIT_STATE_FILE", str(bad))

    with pytest.raises(Exception):
        order_executor._save_flash_exit_state()


def test_flash_exit_state_save_success_writes(tmp_path, monkeypatch):
    """对照: 正常路径仍应落盘 (保证上一个测试不是因为恒抛而绿)。"""
    import json

    from app.pipeline import order_executor

    target = tmp_path / "state.json"
    monkeypatch.setattr(order_executor, "_FLASH_EXIT_STATE_FILE", str(target))
    monkeypatch.setattr(order_executor, "_FLASH_EXIT_TIMESTAMPS", [])
    monkeypatch.setattr(order_executor, "_FLASH_EXIT_PAUSE_UNTIL", 0)

    order_executor._save_flash_exit_state()
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "timestamps" in data


# ── 结构断言 (辅助, 防回退) ────────────────────────────────────────────────

def test_high_vol_gate_is_fail_close():
    """HIGH_VOL 是硬拦截 (HIGH_VOL regime 133 笔累计亏 $39,537)。
    门禁判据不可判定时必须拒绝开仓, 而非放行。"""
    text = _src("app/pipeline/order_executor.py")
    idx = text.find("HIGH_VOL门禁")
    assert idx > 0
    snippet = text[idx: idx + 1200]
    assert "return False" in snippet, "HIGH_VOL 门异常路径未拒绝开仓"
    assert "fail-close" in snippet, "HIGH_VOL 门异常路径缺少 fail-close 标注"
    assert "except Exception as _hv_err:" in snippet, "HIGH_VOL 门仍为裸 except"


def test_duplicate_position_guard_is_fail_close():
    """持仓查询异常 ⇒ 无法确认非重复开仓 ⇒ 拒绝开仓 (RIVN 22:11 开 6 仓同型)。"""
    text = _src("app/pipeline/order_executor.py")
    idx = text.find("多仓拦截")
    assert idx > 0
    snippet = text[idx: idx + 900]
    assert "except Exception as _dup_err:" in snippet, "多仓拦截仍为裸 except"
    assert "_r1_release_if_held()" in snippet, "多仓拦截异常路径未释放标的锁"


def test_auto_execute_position_multipliers_degrade_conservatively():
    """水位/regime 取不到时仓位乘数必须降档, 不得停在 1.0 (回撤期仓位反向放大)。"""
    text = _src("app/playbook/pipeline_parts/auto_execute_flow.py")
    assert "_water_mult = 0.25" in text, "水位乘数失败未降为 ORANGE 保守档"
    assert "_regime_mult = 0.5" in text, "regime 乘数失败未降为 CAUTIOUS 保守档"
    # 降档必须伴随告警 —— 静默降档与静默放大同样不可接受
    assert "资本安全水位读取失败" in text
    assert "regime 评估失败" in text


def test_known_fail_open_count_is_bounded():
    """仓内仍有裸 except/pass, 但**不得新增**到这 5 个位置。

    棘轮式断言 (只降不升): 上界 = 本批实测基线 (2026-10-26 修掉 4 处后仍有
    22 处其它位置)。下一批若悄悄加回来即红。逐处收敛属独立批次, 不在本批范围。
    """
    bound = 22
    total = 0
    for rel in ("app/pipeline/order_executor.py",
                "app/playbook/pipeline_parts/auto_execute_flow.py"):
        text = _src(rel)
        lines = text.splitlines()
        for i, line in enumerate(lines[:-1]):
            if line.strip() == "except Exception:" and lines[i + 1].strip() == "pass":
                total += 1
    assert total <= bound, f"裸 except/pass 增至 {total} 处 (上界 {bound}) — 新的 fail-open"
