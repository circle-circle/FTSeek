"""Human-readable rendering for the evidence-bearing review bundle."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from app.review.bundle_schema import Bundle


_STAGE_LABEL = {
    "premarket": "盘前",
    "regular": "盘中",
    "afterhours": "盘后",
    "": "未标注",
}


def _money(value: Any) -> str:
    return f"-${abs(float(value)):,.2f}" if float(value) < 0 else f"+${float(value):,.2f}"


def _ratio(value: Any) -> str:
    return f"{float(value) * 100:.1f}%"


def _direction_label(value: Any) -> str:
    return "做多" if str(value or "").upper() in ("LONG", "BUY") else "做空"


def _iter_metric_payload(
    node: Any, path: str = ""
):
    if isinstance(node, dict):
        if {"name", "value", "unit", "evidence"} <= node.keys():
            yield path, node
            return
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_metric_payload(value, child_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_metric_payload(value, f"{path}[{index}]")


def render_md(bundle: Bundle) -> Tuple[str, str]:
    payload = bundle.to_dict()
    report_date = payload["report_date"]
    headline = payload["headline"]
    slices = payload["sections"]["slices"]
    plan = payload["sections"]["plan_vs_actual"]
    diagnostics = payload["sections"]["decision_diagnostics"]
    actionability = payload["sections"]["actionability"]
    questions = diagnostics["three_questions"]
    stage_direction_reason = diagnostics["stage_direction_reason"]
    price_band_fees = diagnostics["price_band_fees"]
    bridge = diagnostics["sim_real_bridge"]
    actionable_mfe = actionability["actionable_mfe"]
    regret_price = actionability["regret_price"]
    delay_5 = actionability["delay_5"]
    pretrade_card = actionability["pretrade_card"]
    anomalies = payload["anomalies"]
    pending = payload.get("pending_adjudications", [])

    lines = [
        f"# FTSeek-US 每日复盘 {report_date}",
        "",
        "## 结果",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| 净收益 | {_money(headline['net_pnl']['value'])} |",
        f"| 毛收益 | {_money(headline['gross_pnl']['value'])} |",
        f"| 费用 | {_money(headline['fees']['value'])} |",
        f"| 费毛比 | {_ratio(headline['fee_to_gross']['value'])} |",
        f"| 交易数 | {headline['trade_count']['value']} |",
        f"| 胜率 | {_ratio(headline['win_rate']['value'])} |",
        f"| MFE=0 | {headline['mfe0_count']['value']} |",
        "",
        "## 阶段切片",
        "",
    ]
    if slices:
        lines.extend(
            [
                "| 阶段 | n | 净额 | 毛额 | 胜率 | MFE=0 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for stage, row in slices.items():
            lines.append(
                f"| {_STAGE_LABEL.get(stage, stage)} | {row['n']['value']} | "
                f"{_money(row['net']['value'])} | {_money(row['gross']['value'])} | "
                f"{_ratio(row['win_rate']['value'])} | {row['mfe0']['value']} |"
            )
    else:
        lines.append("无已平仓样本。")

    lines.extend(
        [
            "",
            "## 三问诊断",
            "",
            "以下只列证据充分的问题候选，不直接等于策略裁决。",
            "",
            f"1. **哪些指令疑似不该发**：{questions['should_not_fire_count']['value']} 笔 MFE=0 且净亏损。",
            f"2. **哪些入场疑似追高**：{questions['chase_entry_count']['value']} 笔高于计划价 2% 或越过 chase cap。",
            f"3. **哪些坑在重复**：{questions['repeated_pitfall_count']['value']} 个 prompt 类达到重复亏损簇条件。",
            "",
        ]
    )
    for case in questions["should_not_fire_cases"]["value"]:
        lines.append(
            f"- 疑似错发 `{case['symbol']}` trade_id={case['trade_id']} / "
            f"{case['stage']} / {case['prompt_class']} / 净 {_money(case['net'])} / "
            f"MFE={case['mfe_pct']} / MAE={case['mae_pct']}"
        )
    for case in questions["chase_entry_cases"]["value"]:
        lines.append(
            f"- 疑似追高 `{case['symbol']}` trade_id={case['trade_id']} / "
            f"{case['prompt_class']} / 初始计划 {case['initial_planned_entry']} → "
            f"reprice {case['effective_planned_entry']} → 实际 {case['actual_entry']} "
            f"({case['entry_premium_pct'] * 100:.1f}%) / "
            f"{'越过 chase cap' if case['above_chase_cap'] else '未越 cap但溢价高'} / "
            f"净 {_money(case['net'])}"
        )
    for cluster in questions["repeated_pitfall_clusters"]["value"]:
        if cluster["qualifies"]["value"]:
            lines.append(
                f"- 重复亏损簇 `{cluster['prompt_class']}` / {cluster['window']} / "
                f"n={cluster['n']['value']} / 当前连亏 {cluster['current_loss_streak']['value']} 笔 / "
                f"连亏额 {_money(cluster['current_streak_loss']['value'])} / "
                f"标的 {', '.join(cluster['symbols']['value'])}"
            )

    lines.extend(
        [
            "",
            "## 阶段 × 方向 × 出场原因",
            "",
        ]
    )
    if stage_direction_reason:
        lines.extend(
            [
                "| 阶段 | 方向 | 出场原因 | n | 净额 | 胜率 | MFE=0 |",
                "|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for stage, directions in stage_direction_reason.items():
            for direction, reasons in directions.items():
                for reason, row in reasons.items():
                    lines.append(
                        f"| {_STAGE_LABEL.get(stage, stage)} | {direction} | {reason} | "
                        f"{row['n']['value']} | {_money(row['net']['value'])} | "
                        f"{_ratio(row['win_rate']['value'])} | {row['mfe0']['value']} |"
                    )
    else:
        lines.append("无已平仓样本。")

    lines.extend(["", "## 价格带 × 费用", ""])
    if price_band_fees:
        lines.extend(
            [
                "| 价格带 | n | 净额 | 毛额 | 费用 | 费毛比 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for band, row in price_band_fees.items():
            lines.append(
                f"| {band} | {row['n']['value']} | {_money(row['net']['value'])} | "
                f"{_money(row['gross']['value'])} | {_money(row['fees']['value'])} | "
                f"{_ratio(row['fee_to_gross']['value'])} |"
            )
    else:
        lines.append("无可用入场价样本。")

    lines.extend(
        [
            "",
            "## SIM ↔ 实盘桥",
            "",
            f"- 同信号配对：{bridge['paired_signal_count']['value']} 组（SIM {bridge['sim_trade_count']['value']} / 实盘 {bridge['real_trade_count']['value']}）。",
            f"- 近 20 组累计差：{_money(bridge['rolling_20_cumulative_difference']['value'])}，按 $5,000 名义本金折算 {_money(bridge['normalized_per_5000']['value'])}。",
            f"- 超过 $500 差异线：{'是' if bridge['rolling_20_exceeds_500']['value'] else '否'}。",
            "",
            "口径：只按同 signal/decision 配对；无配对时不推断实盘差异。",
            "",
            "## 可行动性诊断",
            "",
            "以下均为候选证据，不直接改变交易规则。",
            "",
            f"- 可行动 MFE：{actionable_mfe['actionable_count']['value']} / {actionable_mfe['evaluated_count']['value']}（缺 bar {actionable_mfe['bar_unavailable_count']['value']}）。",
            f"- Regret price：{regret_price['profitable_candidate_count']['value']} / {regret_price['evaluated_count']['value']}（缺 bar {regret_price['bar_unavailable_count']['value']}）。",
            f"- 延迟 5 分钟：实际更优 {delay_5['actual_better_count']['value']}，延迟更优 {delay_5['delayed_better_count']['value']}，持平 {delay_5['neutral_count']['value']}，缺 bar {delay_5['bar_unavailable_count']['value']}。",
            f"- Pretrade 卡片完整率：{pretrade_card['complete_card_count']['value']} / {pretrade_card['instruction_count']['value']}（{_ratio(pretrade_card['coverage_ratio']['value'])}）。",
            "",
        ]
    )
    if actionable_mfe["cases"]["value"]:
        lines.extend(
            [
                "### 可行动 MFE 候选",
                "",
                "| 标的 | 方向 | 持续bar | 救援动作 | 估算净额 |",
                "|---|---|---:|---|---:|",
            ]
        )
        for case in actionable_mfe["cases"]["value"][:5]:
            lines.append(
                f"| {case['symbol']} | {_direction_label(case['direction'])} | "
                f"{case['persistent_bars']} | {case['rescue_action']} | "
                f"{_money(case['estimated_rescue_net'])} |"
            )
    if regret_price["cases"]["value"]:
        lines.extend(
            [
                "### Regret price 候选",
                "",
                "| 标的 | 方向 | regret价 | 距实际入场 | 估算净额 | 口径 |",
                "|---|---|---:|---:|---:|---|",
            ]
        )
        for case in regret_price["cases"]["value"][:5]:
            gap = (
                _ratio(case["regret_gap_pct"])
                if case.get("regret_gap_pct") is not None
                else "—"
            )
            net = (
                _money(case["estimated_net_at_regret"])
                if case.get("estimated_net_at_regret") is not None
                else "—"
            )
            lines.append(
                f"| {case['symbol']} | {_direction_label(case['direction'])} | "
                f"{case.get('regret_price', '—')} | {gap} | {net} | {case['status']} |"
            )
    if delay_5["cases"]["value"]:
        lines.extend(
            [
                "### 延迟 5 分钟反事实",
                "",
                "| 标的 | 延迟价 | 延迟净额 | 实际-延迟 | 读法 |",
                "|---|---:|---:|---:|---|",
            ]
        )
        for case in delay_5["cases"]["value"][:5]:
            lines.append(
                f"| {case['symbol']} | {case['delayed_entry_price']} | "
                f"{_money(case['delayed_net'])} | "
                f"{_money(case['actual_minus_delayed'])} | {case['result']} |"
            )
    if pretrade_card["cases"]["value"]:
        missing_counts = {}
        for case in pretrade_card["cases"]["value"]:
            for field in case["missing_fields"]:
                missing_counts[field] = missing_counts.get(field, 0) + 1
        lines.extend(
            [
                "### Pretrade 卡片缺口",
                "",
                "| 缺失字段 | n |",
                "|---|---:|",
            ]
        )
        for field, count in sorted(
            missing_counts.items(), key=lambda item: (-item[1], item[0])
        ):
            lines.append(f"| {field} | {count} |")
    lines.extend(
        [
            "## 计划 vs 实际",
            "",
            f"- 计划指令：{plan['planned_count']['value']}",
            f"- 已执行计划：{plan['executed_count']['value']}",
            f"- 错过计划：{plan['missed_count']['value']}",
            f"- 计划外交易：{plan['unplanned_count']['value']}"
            + (
                f"（{', '.join(plan['unplanned_symbols']['value'])}）"
                if plan["unplanned_symbols"]["value"]
                else ""
            ),
            f"- 执行率：{_ratio(plan['execution_rate']['value'])}",
            f"- 方向错配：{', '.join(plan['direction_mismatch']['value']) or '无'}",
            "",
            "## 待裁决",
            "",
        ]
    )
    if pending:
        for row in pending:
            ready_text = "READY" if row["ready"] else "OBSERVING"
            ready_since = f" / since {row['ready_since']}" if row.get("ready_since") else ""
            lines.append(
                f"- {ready_text} `{row['id']}` {row['progress']}{ready_since} — "
                f"{row['criteria']}"
            )
    else:
        lines.append("无待裁决事项。")

    lines.extend(
        [
            "",
            "## 异常",
            "",
        ]
    )
    if anomalies:
        for anomaly in anomalies:
            lines.append(
                f"- **{anomaly['severity']}** `{anomaly['kind']}` "
                f"trade_id={anomaly['trade_id']} "
                f"raw={anomaly['sqlite_date']} ET={anomaly['et_date']}"
            )
    else:
        lines.append("无 P0 异常。")

    lines.extend(["", "## 证据", ""])
    for path, metric in _iter_metric_payload(payload):
        evidence = metric["evidence"]
        lines.append(f"- `{path}`：{evidence['kind']} — {evidence['ref']}")
    lines.extend(
        [
            "",
            "完整 SQL/derived 证据、样本完整性与参数见同日 JSON 产物。",
            "",
        ]
    )
    full_md = "\n".join(lines)

    stage_text = "；".join(
        f"{_STAGE_LABEL.get(stage, stage)} {row['n']['value']}笔 {_money(row['net']['value'])}"
        for stage, row in slices.items()
    ) or "无已平仓样本"
    push_lines = [
        f"FTSeek-US 复盘 {report_date}",
        f"净 {_money(headline['net_pnl']['value'])} / {headline['trade_count']['value']}笔"
        f" / 胜率 {_ratio(headline['win_rate']['value'])}",
        stage_text,
        f"计划 {plan['planned_count']['value']}，执行 {plan['executed_count']['value']}，"
        f"错过 {plan['missed_count']['value']}，计划外 {plan['unplanned_count']['value']}",
    ]
    if anomalies:
        push_lines.append(f"P0 异常 {len(anomalies)}：{anomalies[0]['kind']}")
    else:
        push_lines.append("P0 异常 0")
    ready_rows = sorted(
        (row for row in pending if row.get("ready")),
        key=lambda row: str(row.get("ready_since", "")),
    )
    if ready_rows:
        oldest = "; ".join(
            f"{row['id']}@{row.get('ready_since', '?')}" for row in ready_rows[:5]
        )
        push_lines.append(f"待裁决 {len(ready_rows)}：{oldest}")
    push_text = "\n".join(push_lines)
    return full_md, push_text
