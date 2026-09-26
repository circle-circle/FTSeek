"""Human-readable rendering for the evidence-bearing review bundle."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

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


def _shadow_lexicon() -> dict:
    """影子白话词典 (SSOT: app/review/shadow_lexicon.py)。

    取不到 ⇒ 返回空 dict, 调用方一律回落到该影子的**注册判据原文** ——
    绝不因为词典缺失就编一句白话 (禁自造叙事)。
    """
    try:
        from app.review.shadow_lexicon import SHADOW_LEXICON

        return SHADOW_LEXICON
    except Exception:  # 词典缺失不是致命错误: 回落判据原文即可, 不得中断判决书构建
        return {}


def clip_for_push(text: str, limit: int = 90) -> str:
    """推送用截断: 优先落在句读边界, 并留「…」标记 —— 截断要看得见, 全文在 MD/清单里。

    2026-09-26 提为公开名 (RFC-109④收口): 本函数是**推送截断的唯一实现** (SSOT),
    scripts/pending_adjudication_report.py 也复用它 —— 该处原先用裸切片 `conclusion[:96]`
    / `[:100]`, 既无句读边界也无「…」标记 ⇒ 长结论被**拦腰截成半句**且读者看不出被截断
    (违反本函数 docstring 自己写的纪律)。别名 `_clip` 保留向后兼容。
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    window = text[: limit + 30]
    cut = max(window.rfind("。"), window.rfind("；"), window.rfind("，"), window.rfind(" "))
    if cut < limit // 2:
        cut = limit
    return window[:cut].rstrip("。；， ") + "…"


_clip = clip_for_push  # 向后兼容别名 (本模块内旧调用点 + 可能的跨模块私有引用)


def _shadow_plain_lines(row: dict, lexicon: dict, *, width: int | None) -> list[str]:
    """一条待裁项的「是什么 / 在问什么 / 现在读数」两行白话。缺词典 ⇒ 回落判据原文并标注。"""
    entry = lexicon.get(row.get("id") or "") or {}
    plain = entry.get("plain")
    if not plain:
        plain = f"{row.get('id')}（未收录白话名 — 下面给注册判据原文）"
    reading = entry.get("conclusion") or (
        f"注册判据: {row.get('criteria') or '（无判据文本）'}"
    )
    if width is not None:
        plain, reading = _clip(plain, width), _clip(reading, width + 30)
    return [f"   {plain}", f"   读数: {reading}"]


def render_md(bundle: Bundle, escalated: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
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
    _lexicon = _shadow_lexicon()

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
            if row["ready"]:
                for plain_line in _shadow_plain_lines(row, _lexicon, width=None):
                    lines.append(f"  {plain_line.strip()}")
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
        f"FTSeek-US 复盘 {report_date}（已平仓·按退出日·美元）",
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
        oldest_since = str(ready_rows[0].get("ready_since") or "?")
        push_lines.append(
            f"待裁决 {len(ready_rows)} 项（最久 {oldest_since} 起）· 白话：这些影子是什么 / 在问什么 / 现在读数"
        )
        # 用户 2026-09-22 令: 「待裁决的影子背景和问题以及结论是不清楚的, 白话主要是对这些影子的解释」
        #   ⇒ 推送面按「每个影子讲三件事」排: 是什么(plain) / 在问什么(含在 plain 里) / 现读数(conclusion).
        #   只报最久等待前 3 项 (防刷屏, 与 DEB §279「只给计数 + 最久前 N」的防噪设计同宗),
        #   全文白话在 MD 与「影子验收清单」里; 词典缺项一律回落注册判据原文, 不编白话.
        for index, row in enumerate(ready_rows[:3], start=1):
            push_lines.append(f"{'①②③'[index - 1]} {row['id']}（{row['progress']}）")
            push_lines.extend(_shadow_plain_lines(row, _lexicon, width=90))
        if len(ready_rows) > 3:
            push_lines.append(
                f"（其余 {len(ready_rows) - 3} 项白话版见「影子验收清单」通知, 周六 09:15 有变化才发; "
                f"全量 {len(ready_rows)} 项逐条见判决书 MD）"
            )
        else:
            push_lines.append(
                "（全量逐条白话见判决书 MD; 「影子验收清单」周六 09:15 有变化才发）"
            )
        push_lines.append("样本攒够 ≠ 判据通过 —— 上面是读数, 留/停/转正要你裁。")
    # ── 逾期升级催办 (DEB-20260919 §13.4 / §20.2④, 2026-09-24 落地) ──────────
    # 每天最多 1 条 (caller 侧限流), 只点最久的那一项; 不替代上面的待裁清单,
    # 而是置顶一条"该裁了"的硬提醒。escalated=None 时整块不出现 (零噪声)。
    if escalated:
        push_lines.append("")
        push_lines.append(
            f"⏰ 逾期催办: `{escalated['id']}` 已 READY {escalated['days_ready']} 天"
            f"（自 {escalated['ready_since']}）— 超 14 天门槛, 请裁决 (留/停/转正)"
        )
    # 白话注释 (2026-09-22, 用户令「需要推送 添加容易理解的注释」):
    #   推送面必须自解释 — 收货人不在终端, 无法 grep/PRAGMA; 术语一律就地翻译, 数字就地给口径.
    #   与 shadow 验收清单同风格 (背景 30 秒 + 白话行), 两处口径一致: "待裁决" ≠ 判据通过.
    glossary = _push_glossary(headline, ready_rows)
    if glossary:
        push_lines.append("——")
        push_lines.append(glossary)
    push_text = "\n".join(push_lines)
    return full_md, push_text


def _push_glossary(headline: dict, ready_rows: list) -> str:
    """推送白话注释: 术语就地翻译 + 口径就地标注 (缺失即省, 绝不为文案编数)."""
    parts = []
    gross = (headline.get("gross_pnl") or {}).get("value")
    fees = (headline.get("fees") or {}).get("value")
    if gross is not None and fees is not None:
        parts.append(
            f"「净」=毛利−费用（本日 毛 {_money(gross)} / 费 {_money(fees)}）"
        )
    else:
        parts.append("「净」=毛利−费用")
    parts.append("「计划外」=系统自己加的单（越权警示），「错过」=计划了但没成交")
    parts.append("「P0 异常」=当日数据自审发现的严重不一致（0=无）")
    return "白话：" + "；".join(parts) + "。"
