"""[DEB-20261111 §13] legacy trail 回写不得覆盖 playbook 自管仓的 open_positions 真值。

病灶 (2026-11-11 实盘 US.LLY):
  `_check_positions` 的 trail 回写循环遍历 `monitored_positions` (来自
  position_store.managed(), 含 sync recovery 残留的 playbook 仓), 用 recovery 合成的
  SL/TP 调 `upsert_open_position(action="update_trail")`, 覆盖 RFC-195 已对齐的指令真值
  (LLY: 合成 1394.96/1851.99 vs 真值 1352.35/1530.29)。出场链读 trade_records 不受影响,
  但 open_positions 是看板/旧风控取值面 ⇒ 两表两个止损。

修复: 回写循环对 playbook 自管仓跳过 (退出仍由 monitor_playbook_sim_exits 独占)。
本契约钉: 跳过判定必须出现在 upsert 回写之前。零交易行为 (只防显示面被覆盖)。
"""
import inspect

from app.pipeline import position_monitor


def test_trail_write_loop_skips_playbook_before_upsert():
    src = inspect.getsource(position_monitor._check_positions)
    anchor = src.find("_all_trail = list(monitored_positions)")
    assert anchor > 0, "未找到 trail 回写循环锚点"
    seg = src[anchor:]
    skip = seg.find("_pb_chk_trail(pos.symbol")
    write = seg.find('action="update_trail"')
    assert skip > 0, "trail 回写循环缺少 playbook 自管仓跳过判定"
    assert write > 0, "未找到 update_trail 回写点"
    assert skip < write, "playbook 跳过必须在 upsert 回写之前生效"


def test_trail_write_uses_ssot_playbook_check():
    """跳过判据必须走 playbook_open_check SSOT (与退出/一致性同口径), 不得自造字符串判定."""
    src = inspect.getsource(position_monitor._check_positions)
    anchor = src.find("_all_trail = list(monitored_positions)")
    seg = src[anchor:anchor + 1200]
    assert "load_playbook_open_symbols" in seg
    assert "playbook_open_check" in seg
