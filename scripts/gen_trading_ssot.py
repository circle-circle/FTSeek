#!/usr/bin/env python3
"""交易框架 SSOT 参数快照生成器 (2026-07-12)。

把散落在 config/代码/cron 里的真实参数值抽取出来, 注入
docs/trading-pipeline-flowchart.md 的 <!-- AUTOGEN:params --> 块,
每个值都带**出处** (yaml键 / 文件:行 / cron)。让流程图文档成为
"结构 SSOT + 自动注入的实时参数", 永不手写数值、永不过期。

来源真相 (只读, 不改任何配置/代码):
  1. config/playbook_horizon.yaml  — vol_buckets(SL桶/TP倍数/持仓), factor_exit_rules, rr_ratio
  2. config/unified_config.yaml     — playbook.auto_execute / fallback_when_no_bridge
  3. app/playbook/pipeline.py       — 风控护栏代码默认值(正则抽取 + 行号)
  4. crontab -l + ~/.hermes/cron/jobs.json — 调度与指令来源状态

用法:
  ./venv/bin/python scripts/gen_trading_ssot.py            # 重生成参数块 (原地替换)
  ./venv/bin/python scripts/gen_trading_ssot.py --check     # 漂移守卫: 配置变了没重生成 → 退出码1
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from app.utils.timezone import ET
ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "trading-pipeline-flowchart.md"
PIPELINE = ROOT / "app" / "playbook" / "pipeline.py"
HORIZON = ROOT / "config" / "playbook_horizon.yaml"
UNIFIED = ROOT / "config" / "unified_config.yaml"
HERMES_JOBS = Path("/root/.hermes/cron/jobs.json")
START = "<!-- AUTOGEN:params:start -->"
END = "<!-- AUTOGEN:params:end -->"


def _code_default(key, cast="s"):
    """从 pipeline.py 抽取 **pb_cfg.get("key", DEFAULT)** 的默认值 + 行号。

    锁定 `pb_cfg.get` (auto_execute 护栏路径), 避开同名但不同默认的其他路径
    (如 L947 `_gpc().get("risk_pct", 1.0)` 属指令数值计算, 不是执行护栏)。"""
    try:
        for i, line in enumerate(PIPELINE.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(rf'pb_cfg\.get\("{re.escape(key)}",\s*([\d.]+)', line)
            if m:
                return m.group(1), f"pipeline.py:{i} 代码默认"
    except Exception:
        pass
    return "?", "未找到"


def _regex_val(pattern, cast="s"):
    """通用: 在 pipeline.py 抽 pattern 第一组 + 行号。"""
    try:
        for i, line in enumerate(PIPELINE.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(pattern, line)
            if m:
                return m.group(1), f"pipeline.py:{i}"
    except Exception:
        pass
    return "?", "未找到"


def _yaml(path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:
        return "?"


def _crontab_lines():
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout
        return [l for l in out.splitlines() if l and not l.strip().startswith("#")]
    except Exception:
        return []


def _sec_guardrails():
    buf, buf_src = _regex_val(r"_market_open_min \+ (\d+)")
    rows = [("开盘后缓冲", f"{buf} 分钟", buf_src, "避开开盘噪声, 之后才执行")]
    for key, unit, note in [
        ("max_position_pct", "×100%", "单笔仓位上限(占基准资金)"),
        ("daily_notional_cap_pct", "×100%", "单日总敞口上限"),
        ("max_daily_positions", "笔", "单日最多开仓数"),
        ("risk_pct", "%", "单笔风险预算(占基准资金)"),
        ("min_confidence", "", "信心基准(仓位加权中性点)"),
    ]:
        v, src = _code_default(key)
        disp = f"{float(v)*100:.0f}%" if unit == "×100%" and v != "?" else f"{v}{unit}"
        rows.append((key, disp, src, note))
    return "执行护栏 (Execution Guardrails)", ["参数", "值", "出处", "说明"], rows


def _sec_vol_buckets():
    h = _yaml(HORIZON)
    rows = []
    for hz in ("short_horizon", "long_horizon"):
        vb = (h.get(hz) or {}).get("vol_buckets") or {}
        for name, b in vb.items():
            rows.append((
                f"{hz.split('_')[0]}/{name}",
                f"ATR<{b.get('atr_pct_max')}",
                f"{b.get('sl_floor_pct')}~{b.get('sl_ceil_pct')}%",
                str(b.get("tp_multiplier")),
                f"{b.get('max_hold_hours')}h",
            ))
    return ("止损止盈波动率桶 (playbook_horizon.yaml::vol_buckets)",
            ["桶", "ATR%上限", "SL范围", "TP倍数", "最长持仓"], rows)


def _sec_exit_rules():
    h = _yaml(HORIZON)
    fer = h.get("factor_exit_rules") or {}
    rows = []
    for factor, cfg in fer.items():
        if not isinstance(cfg, dict):
            continue
        te = cfg.get("time_exit_hours")
        rows.append((
            factor,
            f"{cfg.get('breakeven_at_pct')}%" if cfg.get("breakeven_at_pct") is not None else "-",
            f"{te}h" if te not in (None, 999) else ("禁用" if te == 999 else "-"),
            "on" if cfg.get("trailing_enabled") else "off",
        ))
    return ("退出规则 factor_exit_rules (playbook_horizon.yaml)",
            ["因子", "保本触发", "时间退出", "trailing"], rows)


def _sec_schedule():
    rows = []
    # DA 指令来源 (hermes)
    try:
        d = json.loads(HERMES_JOBS.read_text())
        jobs = d if isinstance(d, list) else d.get("jobs", d.get("crons", []))
        for j in (jobs if isinstance(jobs, list) else []):
            nm = (j.get("name") or "")
            nl = nm.lower()
            if nl.startswith("deep-stock") or nl.startswith("deep-analysis") or j.get("id") in ("e0d574cc1a3c", "13dde1f65703"):
                st = "✅启用" if j.get("enabled") else "⏸暂停"
                sch = j.get("schedule", "?")
                if isinstance(sch, dict):
                    sch = sch.get("display") or sch.get("expr") or "?"
                rows.append((nm[:32], sch, f"{st} ({j.get('state')})", "jobs.json"))
    except Exception:
        pass
    # FTSeek 侧 cron
    for line in _crontab_lines():
        for tag in ("shadow_board", "divergence_arbitration", "da_chain_health", "sltp_shadow_pnl", "da_bridge_dingtalk"):
            if tag in line:
                sched = " ".join(line.split()[:5])
                rows.append((tag, sched, "crontab", "crontab -l"))
                break
    return ("调度与指令来源状态 (cron / hermes)", ["作业", "计划(cron/机器时钟=北京)", "状态", "出处"], rows)


def _sec_misc():
    u = _yaml(UNIFIED).get("playbook") or {}
    rows = [
        ("auto_execute", str(u.get("auto_execute", "?")), "unified_config.yaml::playbook", "自动执行总开关"),
        ("fallback_when_no_bridge", str(u.get("fallback_when_no_bridge", "?")),
         "unified_config.yaml::playbook", "无bridge时兜底策略"),
    ]
    for hz in ("short_horizon", "long_horizon"):
        hc = _yaml(HORIZON).get(hz) or {}
        rr = hc.get("rr_ratio")
        rows.append((f"{hz}.rr_ratio", str(rr) + (" (999=禁TP靠EOD)" if rr == 999 else ""),
                     "playbook_horizon.yaml", "盈亏比"))
        rows.append((f"{hz}.max_hold_days", str(hc.get("max_hold_days")), "playbook_horizon.yaml", "最长持仓天数"))
    return ("总开关与时间框架 (unified_config.yaml / playbook_horizon.yaml)",
            ["参数", "值", "出处", "说明"], rows)


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def render_block(with_stamp=True):
    lines = [START]
    if with_stamp:
        lines.append(f"> 🤖 自动生成 · 请勿手改此块 · 生成时间 {datetime.now(ET):%Y-%m-%d %H:%M} ET · commit `{_git_commit()}`")
        lines.append("> 数值均从真实配置/代码抽取, 带出处; 结构(上方流程图)才是手维护的 SSOT。")
    lines.append("")
    for builder in (_sec_guardrails, _sec_vol_buckets, _sec_exit_rules, _sec_schedule, _sec_misc):
        title, headers, rows = builder()
        lines.append(f"#### {title}")
        lines.append(_table(headers, rows) if rows else "_(无数据/读取失败)_")
        lines.append("")
    lines.append(END)
    return "\n".join(lines)


def _split_doc():
    text = DOC.read_text(encoding="utf-8")
    if START in text and END in text:
        pre = text[:text.index(START)]
        post = text[text.index(END) + len(END):]
        cur = text[text.index(START):text.index(END) + len(END)]
        return pre, cur, post
    return text.rstrip() + "\n\n", "", ""


def _strip_stamp(block):
    """去掉时间戳/commit 行, 供 --check 稳定比较。"""
    return "\n".join(l for l in block.splitlines() if not l.startswith("> 🤖 自动生成"))


def main(check=False):
    pre, cur, post = _split_doc()
    new_block = render_block()
    if check:
        if _strip_stamp(cur) != _strip_stamp(new_block):
            print("❌ 漂移: 配置/代码已变但文档参数块未重生成。请运行 gen_trading_ssot.py 后提交。")
            sys.exit(1)
        print("✅ 参数快照与配置/代码一致 (无漂移)。")
        return
    DOC.write_text(pre + new_block + post, encoding="utf-8")
    print(f"✅ 已重生成参数块 → {DOC}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="交易框架 SSOT 参数快照生成器 (只读源, 只写文档AUTOGEN块)")
    ap.add_argument("--check", action="store_true", help="漂移守卫(CI/hook用): 不一致则退出码1")
    a = ap.parse_args()
    main(check=a.check)
