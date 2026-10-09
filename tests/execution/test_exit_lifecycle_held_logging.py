# tests/execution/test_exit_lifecycle_held_logging.py
"""ExitLifecycle HELD 日志两级分流契约 (2026-10-09, 用户裁「请优化调整」).

病灶: `except ValueError` 对所有守卫 reason 一律 `logger.warning(exc_info=True)`,
而 `*_pending` 瞬态族 (成交证据/回报在途: exit_order_evidence_pending 等) 的
抛出点恒已知 (query 守卫), 宽限/超窗升级由 playbook 监控层持有 ⇒ 生产实盘
5 笔平仓 30min 内 14 条完整 Traceback, 纯噪音淹没真异常。

修法 (行为边界): *_pending 族降 INFO 不打栈; 其他守卫拒绝 (含归一后的
exit_evidence_invalid) 保留 WARNING+exc_info (9/13 事故教训: 栈=抛出点,
定位"哪个守卫拦的"所需)。W456 口径: except 内 INFO 不计 debug_swallow /
except_stack 任一维, 棘轮零扰动。

本文件钉住 (防回流):
  1. 归一 (exit_evidence_invalid) 必须先于分流 — 否则未知错误被当瞬态静音;
  2. 瞬态分支存在且用 logger.info (无 exc_info);
  3. 非瞬态分支保留 logger.warning + exc_info=True;
  4. 瞬态判定键是 `_pending` 后缀 (新增瞬态 reason 自动入族, 非瞬态不受影响)。
"""
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2]
        / "app" / "execution" / "exit_lifecycle.py").read_text(encoding="utf-8")


def _except_block() -> str:
    idx = _SRC.index("except ValueError as exc:")
    return _SRC[idx: idx + 2600]


def test_normalization_precedes_split():
    blk = _except_block()
    assert blk.index("error = 'exit_evidence_invalid'") < blk.index("error.endswith('_pending')"), \
        "归一必须先于瞬态分流 (未知错误绝不能被当瞬态降 INFO)"


def test_transient_pending_family_uses_info_without_stack():
    blk = _except_block()
    assert "if error.endswith('_pending'):" in blk
    assert "logger.info('Exit held intent=%s reason=%s (transient, 在途)', intent_id, error)" in blk
    # 瞬态行不得带栈 (带栈 = 回到旧噪音形态)
    info_line = [l for l in blk.splitlines() if "logger.info('Exit held" in l][0]
    assert "exc_info" not in info_line


def test_non_transient_guard_rejections_keep_warning_with_stack():
    blk = _except_block()
    assert "logger.warning('Exit held intent=%s reason=%s', intent_id, error, exc_info=True)" in blk, \
        "非瞬态守卫拒绝必须保留 WARNING+栈 (9/11 exit_identity_required 事故教训)"
