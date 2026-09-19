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
