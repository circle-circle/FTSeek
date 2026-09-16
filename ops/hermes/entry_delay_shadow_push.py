#!/usr/bin/env python3
"""D2' 入场确认门影子每日推送 (no_agent cron script, 2026-09-11 用户裁「要」).

[2026-09-16 用户裁「要」扩展] 增 D 案 (ADR-035 限价路由) 段 + B/D 分流人群分层读数
(RFC-081 §六批注) + 裁决窗口开启提醒 — 单推送承载入场门双影子, 不新增 cron (推送治理)。

读结算产物 (05:47/05:58 CST crontab 已落盘):
  - data/shadow/entry_delay_confirm.jsonl   逐笔反事实 (幂等键 trade_id+delay_min)
  - data/shadow/entry_delay_confirm_latest.json  汇总+预注册判据+by_branch/forward分层
  - data/shadow/pullback_limit_replay_v2_latest.json  D案 v2 读数卡 (05:47 replay cron)
  - data/ftseek_us.db (mode=ro)  trade_records.mfe_pct → 墓地捕获 (MFE<0.3%)

输出 8-10 行中文报告到 stdout (飞书推送)。record-only 影子的消费面:
每日亏损对比 (回避省$ vs 照进多付$) + forward 攒样进度 + 毕业判据状态 + D案裁决进度。
结算文件陈旧 (>26h) → 推警告行 (传感器失效可见化, 8/28 教训)。
本脚本零写入 (DB ro + 不碰 jsonl), 失败时输出错误行而非静默。
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
JSONL = REPO / "data/shadow/entry_delay_confirm.jsonl"
LATEST = REPO / "data/shadow/entry_delay_confirm_latest.json"
CF_LATEST = REPO / "data/shadow/pullback_limit_replay_v2_latest.json"
DB = REPO / "data/ftseek_us.db"

GRAVE_MFE = 0.3          # 墓地线: MFE<0.3% = 入场即错族
FORWARD_START = "2026-09-09"  # DEB-20260909 影子落地日, forward 攒样窗起点 (裁决日以 forward 冻结值为准)
STALE_HOURS = 26         # 结算 cron 05:58, 推送 06:10; >26h = 昨晨结算缺失
D_TARGET_N = 40          # D案配对裁决门槛 (RFC-081 批注二②: 统一 n≥40 最严优先, 溯源 DEB-20260907 §4.9; 30=中间态只说方向不得提请裁决)
B_TARGET_N = 20          # B案 forward 毕业门槛 (DEB §5.2: n≥20)


def _fmt_usd(v: float) -> str:
    return f"${v:+,.0f}"


def main() -> int:
    now = datetime.now()
    lines = [f"🔬 入场确认门影子(D2') 日报 {now:%m-%d}"]

    # ── 新鲜度门 ──
    if not LATEST.exists() or not JSONL.exists():
        print("\n".join(lines + ["⚠️ 影子产物缺失 — 结算链未跑, 查 crontab 58 5 行"]))
        return 0
    latest = json.loads(LATEST.read_text())
    gen_at = datetime.fromisoformat(latest["generated_at"].replace("Z", "+00:00").split("+")[0])
    age_h = (now - gen_at).total_seconds() / 3600
    if age_h > STALE_HOURS:
        print("\n".join(lines + [
            f"⚠️ 结算陈旧: latest.json 生成于 {gen_at:%m-%d %H:%M} ({age_h:.0f}h 前)",
            "→ 查 logs/entry_delay_confirm.log 与 crontab 58 5 * * 2-6 行"]))
        return 0

    rows = [json.loads(l) for l in JSONL.open() if l.strip()]
    r2 = [r for r in rows if r.get("delay_min") == 2 and r.get("status") == "ok"]
    if not r2:
        print("\n".join(lines + ["⚠️ jsonl 无 N=2 ok 行"]))
        return 0

    # ── 昨日批次 (最近一个已结算交易日) ──
    batch_day = max(r["date"] for r in r2)
    day = [r for r in r2 if r["date"] == batch_day]
    d_av = [r for r in day if not r.get("still_triggers")]
    d_dl = [r for r in day if r.get("still_triggers")]
    saved = -sum(r.get("pnl_net") or 0 for r in d_av)
    opp = sum((r.get("opp_cost_pct") or 0) / 100 * (r.get("notional") or 0) for r in d_dl)
    lines.append(f"结算日 {batch_day}: n={len(day)} | 回避 {len(d_av)} 笔(净省{_fmt_usd(saved)})"
                 f" / 照进 {len(d_dl)} 笔(多付{_fmt_usd(opp)}) → 假想差 {_fmt_usd(saved - opp)}")
    if not day:
        lines[-1] += " (零成交日)"

    # ── 墓地捕获 (MFE<0.3%, 需 join trade_records) ──
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        mfe = dict(db.execute("SELECT id, mfe_pct FROM trade_records WHERE mfe_pct IS NOT NULL"))
        db.close()
        day_g = [r for r in day if (mfe.get(r["trade_id"]) or 99) < GRAVE_MFE]
        g_av = [r for r in day_g if not r.get("still_triggers")]
        g_saved = -sum(r.get("pnl_net") or 0 for r in g_av)
        lines.append(f"墓地捕获(MFE<0.3%): 当日 {len(day_g)} 笔, 门回避 {len(g_av)} 笔"
                     + (f" (省{_fmt_usd(g_saved)})" if g_av else ""))
    except Exception as e:  # noqa: BLE001
        lines.append(f"墓地捕获: join 失败 ({e})")

    # ── forward 攒样进度 (裁决口径) ──
    fwd = [r for r in r2 if r["date"] >= FORWARD_START]
    f_av = [r for r in fwd if not r.get("still_triggers")]
    f_dl = [r for r in fwd if r.get("still_triggers")]
    if fwd:
        av_rate = len(f_av) / len(fwd) * 100
        f_opp = (sum(r.get("opp_cost_pct") or 0 for r in f_dl) / len(f_dl)) if f_dl else 0.0
        f_saved = -sum(r.get("pnl_net") or 0 for r in f_av)
        f_opp_usd = sum((r.get("opp_cost_pct") or 0) / 100 * (r.get("notional") or 0) for r in f_dl)
        lines.append(
            f"forward({FORWARD_START[5:]}起): n={len(fwd)}/20 | 回避率 {av_rate:.0f}%(门≥30%)"
            f" | 机会成本 {f_opp:.3f}%(门<0.3%) | 净差 {_fmt_usd(f_saved - f_opp_usd)}")
    else:
        lines.append(f"forward({FORWARD_START[5:]}起): n=0/20 攒样中")

    # ── 全窗判据 (latest.json 冻结值, 仅作参照) ──
    grad = latest.get("graduation", {}).get("2", {})
    g2 = latest.get("delays", {}).get("2", {}).get("ALL", {})
    lines.append(
        f"全窗参照: n={g2.get('n', '?')} 回避率 {(g2.get('avoidance_rate') or 0) * 100:.0f}%"
        f" 成本 {g2.get('opp_cost_mean_pct', '?')}% | 判据 ALL={'✅' if grad.get('ALL_pass') else '❌'}"
        f" SL_INIT={'✅' if grad.get('SL_INITIAL_pass') else '❌'}")

    # ── [2026-09-16] B/D 分流人群分层 (RFC-081 §六: 裁决日 B 主判读=B_only 人群) ──
    f_blk = latest.get("delays", {}).get("2", {}).get("forward", {})
    f_b = f_blk.get("B_only", {})
    if f_b.get("n") is not None:
        lines.append(
            f"B自身人群(forward): n={f_b.get('n')}/{B_TARGET_N} 回避率 {(f_b.get('avoidance_rate') or 0) * 100:.0f}%"
            f" 成本 {f_b.get('opp_cost_mean_pct') if f_b.get('opp_cost_mean_pct') is not None else '?'}%(门<0.3%)"
            f" | grad={'✅' if grad.get('forward_B_only_pass') else '❌'} (混合ALL过门≠分流后B过门)")

    # ── [2026-09-16] D案 (ADR-035 限价路由) 裁决进度 ──
    try:
        # 新鲜度: v2_latest 由 05:47 cron 窗口模式产出; >26h = 昨日结算缺失 (断头传感器可见化)
        _age_h = (datetime.now().timestamp() - CF_LATEST.stat().st_mtime) / 3600
        cf = json.loads(CF_LATEST.read_text())
        s = cf.get("summary", {})
        n_p = s.get("n_paired")
        crit = s.get("adr035_criteria") or s.get("criteria") or {}
        mk = s.get("d_branch_miskill") or {}
        rs = s.get("c3_reachability_split") or {}
        if _age_h > STALE_HOURS:
            lines.append(f"⚠️ D案产物陈旧 ({_age_h:.0f}h前) — 查 crontab 47 5 窗口模式段")
        if n_p is not None:
            lines.append(
                f"D案限价路由: 配对 n={n_p}/{D_TARGET_N} | 窗 {s.get('window_start')}→{s.get('window_end')}"
                f" | 配对差中位 {s.get('paired_diff_median') if s.get('paired_diff_median') is not None else '?'}$/笔")
            _extra = []
            if mk.get("n_expired"):
                _extra.append(f"误杀(expired口径,门≤40%) {mk.get('miskill_rate')}{'✅' if mk.get('pass') else '❌'}"
                              f" 放弃净利{_fmt_usd(mk.get('forgone_actual_net_usd') or 0)}")
            if rs.get("reachable_n") is not None:
                _extra.append(f"C3分层: 可达成交率{rs.get('reachable_fill_rate')} / 不可达{rs.get('unreachable_fill_rate')}")
            if _extra:
                lines.append("  " + " | ".join(_extra))
            # 裁决窗口开启提醒 (n 达门槛 → 提请用户裁决, 不自动接线)
            if n_p >= D_TARGET_N:
                lines.append(f"🔔 D案配对样本已达门槛 n={n_p}≥{D_TARGET_N} — 裁决材料齐备, 提请裁决 (读 RFC-081 §六 + ADR-035 §五.2 判读规则)")
        else:
            lines.append("D案限价路由: latest 无 n_paired 字段 (replay cron 05:47 待跑?)")
    except FileNotFoundError:
        lines.append("D案限价路由: 产物缺失 — 查 crontab 47 5 行")
    except Exception as e:  # noqa: BLE001
        lines.append(f"D案限价路由: 读取失败 ({e})")

    lines.append("record-only 零交易影响 | 裁决日按 forward 冻结值提请 (RFC-081)")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"🔬 入场确认门影子日报: 脚本异常 {exc!r}")
        sys.exit(0)
