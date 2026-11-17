"""DA 研究样本分层助手（只读，零交易行为变更）。

为什么需要它（2026-11-14 复查实证）：
  position_sync 恢复路径补录的 trade_record（entry_reason='sync_recovery_backfill'）、
  入场记账补偿（playbook_entry_bookkeeping_compensation）与手动收养（adopt_to_playbook）
  这三类「收养/补录」行，其 peak_price/trough_price 在收养时刻被重置为 entry、ATR 用
  代理值（_proxy_atr_from_stop）、market_regime 硬编码 'RANGE'、entry_vix 取收养时刻值。
  依据：app/core/position_sync.py（_meta_p3 / entry_price_source='sync_recovery_backfill'
  口径告警）与 scripts/entry_geometry_review.py（caveats：「ATR/peak/MFE 可能是收养时
  近似值，须先修口径再评估退出」）。故这些行的 MFE/ATR/regime 与正常信号入场
  （playbook_futu_simulate）**不同量纲**，直接混入 DA 研究度量会：
    ① MFE 被系统性低估 ⇒ 虚高「入场即错」死单率（MFE<0.3%）；
    ② 硬编码 RANGE 污染 regime 分层。
  实证：11 月 DA 表观死单率 22.8%（21/92）中 13 笔死单来自补录行；剔除后真实信号
  死单率 15.8%（6/38），与 10 月 15.6%（19/122）持平——「回升」是尺子被污染，
  并非 LLM 信号质量退化。同理 11 月「57% RANGE」亦为补录硬编码所致（真实信号
  为 cautious/risk_off）。

纪律（分层≠抹账）：补录行是**真实持仓、真实盈亏**（非重复行：futu_order_id 各异、
  与正常信号无 (symbol,trade_date) 重叠），其 PnL 仍须计入总账。本助手只把它们从
  「入场质量/信号期望/edge 发现」度量中分出单列，绝不静默丢弃。只读，零交易行为变更。
"""

ARTIFACT_ENTRY_REASONS = frozenset({
    "sync_recovery_backfill",
    "playbook_entry_bookkeeping_compensation",
    "adopt_to_playbook",
})

# 降频反事实评估真实成交顺序与净盈亏，不使用受恢复污染的 MFE/regime。
# sync_recovery_backfill 仍是实际策略成交且 entry_time 优先取券商订单时间，必须留在
# capped/uncapped 对照；补偿立即平仓与人工收养不代表自然策略结局，单列排除。
FREQUENCY_EXCLUDED_ENTRY_REASONS = frozenset({
    "playbook_entry_bookkeeping_compensation",
    "adopt_to_playbook",
})


def is_recovery_artifact(row):
    """收养/补录记账行 ⇒ True（其 MFE/ATR/regime 为收养时近似值，不可与信号入场混算）。"""
    return (row.get("entry_reason") or "") in ARTIFACT_ENTRY_REASONS


def split_signal_artifact(rows):
    """按入场来源把样本分成 (真实信号, 收养/补录记账) 两份，保持原顺序。"""
    signal, artifact = [], []
    for row in rows:
        (artifact if is_recovery_artifact(row) else signal).append(row)
    return signal, artifact


def split_frequency_population(rows):
    """降频人群：保留正常信号及恢复补录的真实 PnL，排除补偿/人工收养。"""
    included, excluded = [], []
    for row in rows:
        excluded_row = (row.get("entry_reason") or "") in FREQUENCY_EXCLUDED_ENTRY_REASONS
        target = excluded if excluded_row else included
        target.append(row)
    return included, excluded
