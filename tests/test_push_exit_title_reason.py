"""平仓推送标题的原因尾缀 + 正文生效SL行 (2026-09-18).

背景: 钉钉 markdown 消息的 `title` 是通知栏/会话列表摘要 —— 正文再全, 不展开也只看到
净额。9/18 COIN (SL_INITIAL 砍在规则0.5 紧SL线 184.7378) 的标题只有
`🔴 US.COIN 平仓 净$-161.65 (毛$-157.34/费$4.31)`, 与 9/15 ARM 案同类: 读标题无法
区分"初始线砍"还是"紧SL砍"。本测试钉住两件事:
  ① `trade_exit` 标题必须带出场原因标签 (未知原因原样透出, 不丢信息);
  ② 正文的 `原因` 与 `触发线 ← 早期紧SL(规则0.5)` 行不得回退 (钉住 2026-09-16 落地件)。

纪律: 标签只是显示映射, 触发与否由 `tight_sl_triggered` 元数据决定 ——
无该元数据时不得自称"紧SL"(哨兵测试)。
"""
from app.push.message import trade_exit, _exit_reason_cn


def _coin_like(**over):
    kw = dict(symbol="US.COIN", direction="SHORT", pnl=-157.34,
              exit_reason="SL_INITIAL", strategy="playbook_sim",
              trace_id="1538", source_type="PLAYBOOK_SIM",
              fees=4.31, pnl_net=-161.65)
    kw.update(over)
    return kw


def test_title_net_carries_tight_sl_label():
    msg = trade_exit(extra={"entry_price": 180.232, "exit_price": 185.0,
                            "effective_sl": 184.7378, "tight_sl_triggered": True},
                     **_coin_like())
    assert msg.title.endswith("· 止损(紧SL)")
    assert "净$-161.65" in msg.title and "🔴" in msg.title


def test_title_initial_line_label_when_not_tight():
    msg = trade_exit(extra={"effective_sl": 189.9}, **_coin_like(pnl=-60.0, pnl_net=-64.2))
    assert msg.title.endswith("· 止损(初始线)")


def test_title_gross_only_path_carries_reason():
    """无 pnl_net (旧库/估算失败) 时毛口径标题同样带原因, 不因降级丢字段。"""
    msg = trade_exit(**{**_coin_like(), "pnl_net": None, "fees": None})
    assert "净" not in msg.title.split("平仓")[1]
    assert msg.title.endswith("· 止损(初始线)")


def test_unknown_reason_is_passed_through_verbatim():
    """未映射的原因码原样透出 —— 宁可显示生码, 不可吞掉信息 (fail-open)。"""
    msg = trade_exit(**_coin_like(exit_reason="BRAND_NEW_REASON", pnl=1.0, pnl_net=0.5))
    assert msg.title.endswith("· BRAND_NEW_REASON")
    assert _exit_reason_cn("", None, None) == "?"


def test_be_gap_marker_and_breakeven_label():
    msg = trade_exit(**_coin_like(exit_reason="SL_BREAKEVEN", pnl=-2.0, pnl_net=-6.4,
                                  extra={"be_exit_class": "GAP", "be_drift_bps": -12.0}))
    assert msg.title.endswith("· 保本线🪂")


def test_effective_sl_line_requires_real_tight_flag():
    """哨兵: 没有 tight_sl_triggered 元数据时, 正文不得自称"早期紧SL(规则0.5)"。"""
    msg = trade_exit(extra={"entry_price": 180.232, "exit_price": 185.0,
                            "effective_sl": 184.7378}, **_coin_like())
    body = msg.render_text()
    assert "触发线: $184.74" in body
    assert "早期紧SL(规则0.5)" not in body
    assert "← 生效止损" in body


def test_body_keeps_reason_and_tight_marker():
    """钉住 2026-09-16 落地件: 正文原因 + 生效线来源标注不得回退 (两种渲染器)。"""
    msg = trade_exit(extra={"entry_price": 180.232, "exit_price": 185.0,
                            "effective_sl": 184.7378, "tight_sl_triggered": True,
                            "mfe_pct": 0.0, "mae_pct": 2.65},
                     **_coin_like())
    for rendered in (msg.render_text(), msg.render_markdown()):
        # 文本版 `原因: SL_INITIAL` / markdown 版 `**📉 原因**: SL_INITIAL` — 两种都算在场
        assert "原因" in rendered and "SL_INITIAL" in rendered
        assert "触发线" in rendered and "$184.74" in rendered
        assert "早期紧SL(规则0.5)" in rendered