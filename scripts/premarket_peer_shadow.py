#!/usr/bin/env python3
"""盘前存储峰群影子 (PreMarket Peer Shadow) — 只读, 不下单, 不改 live。

思路 (2026-07-13, 用户提出):
  用富途美股盘前趋势 + 成交量, 对 **存储/内存高波动峰群 (MU/SNDK/WDC/STX)** 做
  "同向 + 放量" 双条件确认, 与 DA 指令方向对照, join pnl_pct, 量化以下假设:
    - 峰群盘前确认 DA 方向时, 该笔胜率/收益是否更好?
    - 峰群盘前否决(背离/缩量)时, 是否更该 SKIP?
  打法同 divergence_arbitration_shadow: 先影子攒证据, 有 edge 再切执行前置闸门。

数据源 (富途优先, Finnhub 兜底):
  1. 富途盘前快照: ManualOrderService._quote_ctx.get_market_snapshot → pre_change_rate / volume_ratio
  2. 兜底: app.data.premarket_enrichment.collect_premarket_data 的 premarket_movers

输出: data/shadow/premarket_peer_shadow.jsonl (按 date upsert)
用法: PYTHONPATH=. ./venv/bin/python scripts/premarket_peer_shadow.py [--push]
"""
import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.utils.timezone import ET

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
SHADOW_DIR = ROOT / "data" / "shadow"
SHADOW_FILE = SHADOW_DIR / "premarket_peer_shadow.jsonl"

# 存储/内存峰群 (用户选定); NVDA 作参考峰(联动半导体), 默认纳入观察
PEERS = ["MU", "SNDK", "WDC", "STX"]
PEERS_REF = ["NVDA"]  # 参考, 计入快照但不投票 group 方向
VOL_RATIO_MIN = 1.5   # 放量阈值 (volume_ratio ≥ 此值算放量); 影子期可调

# 2026-07-22: 板块组配置化 — 不锁死存储, 读 unified_config.yaml
#   universe_expansion.peer_surge_groups (用户要求: 峰群覆盖多行业可扩展).
#   回滚: 配置删 peer_surge_groups 块 → 回落内置默认 ({"存储": PEERS+参考NVDA} 原行为).
DEFAULT_GROUPS = {"存储": PEERS + PEERS_REF}


def _load_groups() -> dict:
    """读配置的板块组 {板块名: [成分票]}, 失败/缺失回落 DEFAULT_GROUPS (原存储组行为)."""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
        groups = ((cfg or {}).get("universe_expansion") or {}).get("peer_surge_groups") or {}
        groups = {str(k): [str(s).upper() for s in v] for k, v in groups.items() if isinstance(v, list) and v}
        return groups if groups else dict(DEFAULT_GROUPS)
    except Exception as e:
        print(f"[warn] peer_surge_groups 读取失败, 回落默认存储组: {e}", file=sys.stderr)
        return dict(DEFAULT_GROUPS)


def _today_et():
    return datetime.now(ET).strftime("%Y-%m-%d")


# ── 1. 盘前取数 (富途优先) ────────────────────────────────────────────
def _fetch_futu(symbols):
    """返回 {sym(裸): {pre_change, volume_ratio, last, prev_close}} 或 {}。"""
    out = {}
    try:
        from app.web_dashboard.manual_order_service import ManualOrderService
        from futu import RET_OK
        svc = ManualOrderService()
        if not svc.connect():
            return {}
        codes = [f"US.{s}" for s in symbols]
        ret, data = svc._quote_ctx.get_market_snapshot(codes)
        if ret == RET_OK and not data.empty:
            for _, row in data.iterrows():
                r = row.to_dict()
                sym = str(r.get("code", "")).replace("US.", "")
                out[sym] = {
                    "pre_change": round(float(r.get("pre_change_rate", 0) or 0), 3),
                    "volume_ratio": round(float(r.get("volume_ratio", 0) or 0), 3),
                    "last": float(r.get("last_price", 0) or 0),
                    "prev_close": float(r.get("prev_close_price", 0) or 0),
                }
        svc.disconnect()
    except Exception as e:
        print(f"[warn] 富途盘前取数失败: {e}", file=sys.stderr)
    return out


def _fetch_fallback(symbols):
    """Finnhub 兜底: 复用 collect_premarket_data 的 premarket_movers。"""
    out = {}
    try:
        from app.data.premarket_enrichment import collect_premarket_data
        enr = collect_premarket_data(watchlist_symbols=symbols) or {}
        movers = (enr.get("premarket_movers") or {})
        for bucket in ("gainers", "losers"):
            for m in movers.get(bucket, []):
                sym = str(m.get("symbol", "")).replace("US.", "")
                if sym in symbols:
                    out[sym] = {
                        "pre_change": round(float(m.get("premarket_change", 0) or 0), 3),
                        "volume_ratio": round(float(m.get("volume_ratio", 0) or 0), 3),
                        "last": float(m.get("price", 0) or 0),
                        "prev_close": 0.0,
                    }
    except Exception as e:
        print(f"[warn] Finnhub 兜底失败: {e}", file=sys.stderr)
    return out


def _fetch(symbols):
    data = _fetch_futu(symbols)
    src = "futu"
    if not data or all(v["pre_change"] == 0 and v["volume_ratio"] == 0 for v in data.values()):
        fb = _fetch_fallback(symbols)
        if fb:
            data, src = fb, "finnhub_fallback"
    return data, src


# ── 2. 峰群信号: 同向 + 放量 双条件 ──────────────────────────────────
def _group_signal(snap, members=None):
    """用 members(默认 PEERS, 不含参考峰)投票。返回 (verdict, detail)。"""
    core = {s: snap[s] for s in (members or PEERS) if s in snap}
    if not core:
        return "NO_DATA", {}
    up = [s for s, v in core.items() if v["pre_change"] > 0]
    dn = [s for s, v in core.items() if v["pre_change"] < 0]
    if len(up) > len(dn):
        gdir, dir_peers = "UP", up
    elif len(dn) > len(up):
        gdir, dir_peers = "DOWN", dn
    else:
        return "NO_CONFIRM_MIXED", {"up": len(up), "dn": len(dn), "n": len(core)}
    # 放量: 同向峰中 volume_ratio≥阈值 的数量 ≥ 同向数的一半(向上取整)
    vol_ok = sum(1 for s in dir_peers if core[s]["volume_ratio"] >= VOL_RATIO_MIN)
    need = math.ceil(len(dir_peers) / 2)
    verdict = f"CONFIRM_{gdir}" if vol_ok >= need else f"NO_CONFIRM_LOWVOL_{gdir}"
    return verdict, {"up": len(up), "dn": len(dn), "n": len(core),
                     "vol_ok": vol_ok, "need": need, "dir_peers": dir_peers}


# ── 3. join DA 指令 + pnl_pct ─────────────────────────────────────────
def _da_instructions(date, symbols):
    """取当天这些票的 DA 指令(DB symbol 带 US. 前缀)。"""
    out = {}
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        codes = [f"US.{s}" for s in symbols]
        q = f"SELECT symbol,direction,pnl_pct,outcome,triggered,executed FROM playbook_instructions WHERE date=? AND symbol IN ({','.join('?' * len(codes))})"
        for r in con.execute(q, [date] + codes).fetchall():
            out[str(r[0]).replace("US.", "")] = {
                "direction": r[1], "pnl_pct": r[2], "outcome": r[3],
                "triggered": r[4], "executed": r[5],
            }
        con.close()
    except Exception as e:
        print(f"[warn] 读 DA 指令失败: {e}", file=sys.stderr)
    return out


def _agree(instr_dir, verdict):
    """指令方向 vs 峰群确认。BUY↔UP, SELL_SHORT↔DOWN。"""
    if not verdict.startswith("CONFIRM_"):
        return None
    gdir = verdict.split("_")[1]
    if instr_dir == "BUY":
        return gdir == "UP"
    if instr_dir == "SELL_SHORT":
        return gdir == "DOWN"
    return None  # SKIP 等


# ── 4. 采集 → 记录 ────────────────────────────────────────────────────
def _collect(date):
    groups = _load_groups()
    allsyms = sorted({s for ms in groups.values() for s in ms})
    snap, src = _fetch(allsyms)
    # 逐组判定; 顶层 group_verdict/group_detail = 首组(兼容旧消费端: clue/refresh-join)
    gmap = {}
    for name, members in groups.items():
        v, d = _group_signal(snap, members)
        gmap[name] = {"verdict": v, "detail": d}
    first = next(iter(groups)) if groups else "存储"
    verdict, detail = gmap.get(first, {}).get("verdict", "NO_DATA"), gmap.get(first, {}).get("detail", {})
    # 指令 join: 按票所属组的 verdict 判定一致性 (多组时代不用单一 verdict)
    sym2group = {s: name for name, ms in groups.items() for s in ms}
    instr = _da_instructions(date, allsyms)
    joined = {}
    for sym, ins in instr.items():
        gv = gmap.get(sym2group.get(sym, first), {}).get("verdict", verdict)
        ag = _agree(ins["direction"], gv)
        # 闸门本可避免亏损? 方向与峰群不一致 且 已成交亏损
        gate_help = bool(ag is False and ins.get("executed") and (ins.get("pnl_pct") or 0) < 0)
        joined[sym] = {**ins, "agree_with_peer": ag, "gate_would_help": gate_help,
                       "peer_group": sym2group.get(sym), "group_verdict": gv}
    return {
        "date": date,
        "collected_at": datetime.now(ET).isoformat(),
        "source": src,
        "peers_snapshot": snap,
        "groups": gmap,
        "groups_members": {k: list(v) for k, v in groups.items()},  # 自描述成分, clue 侧免读 yaml
        "group_verdict": verdict,
        "group_detail": detail,
        "instructions": joined,
        "vol_ratio_min": VOL_RATIO_MIN,
    }


def _upsert(record):
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    rows = {}
    if SHADOW_FILE.exists():
        for line in SHADOW_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    rows[r["date"]] = r
                except Exception:
                    pass
    rows[record["date"]] = record
    with SHADOW_FILE.open("w", encoding="utf-8") as f:
        for d in sorted(rows):
            f.write(json.dumps(rows[d], ensure_ascii=False) + "\n")
    return rows


# ── 5. 报告 ───────────────────────────────────────────────────────────
def _report(rows):
    today = _today_et()
    cur = rows.get(today)
    groups = _load_groups()
    lines = [f"📡 盘前峰群影子 ({'/'.join(groups.keys())})", ""]
    if cur:
        gmap = cur.get("groups") or {next(iter(groups)): {"verdict": cur.get("group_verdict"), "detail": {}}}
        lines.append(f"【{today} · 源={cur['source']}】")
        for name, members in groups.items():
            gv = (gmap.get(name) or {}).get("verdict", "NO_DATA")
            lines.append(f"[{name}] {gv}")
            for s in members:
                v = cur["peers_snapshot"].get(s)
                if v:
                    tag = "📈" if v["pre_change"] > 0 else "📉" if v["pre_change"] < 0 else "➖"
                    vtag = "🔊" if v["volume_ratio"] >= cur["vol_ratio_min"] else "🔈"
                    lines.append(f"  {tag} {s}: {v['pre_change']:+.2f}% 量比{v['volume_ratio']:.2f}{vtag}")
        if cur["instructions"]:
            lines.append("  DA指令对照:")
            for s, ins in cur["instructions"].items():
                ag = {True: "✅一致", False: "❌背离", None: "—"}[ins["agree_with_peer"]]
                pnl = f"{ins['pnl_pct']:+.4f}" if ins.get("pnl_pct") is not None else "n/a"
                gh = " [闸门本可避亏]" if ins["gate_would_help"] else ""
                lines.append(f"    {s}: DA={ins['direction']} vs 峰群{ag} · pnl={pnl}{gh}")
        else:
            lines.append("  DA指令对照: 今日无这些票的指令(覆盖缺口)")
    else:
        lines.append(f"【{today}】今日暂无采集(非盘前时段或数据不可用)")

    # 累计统计
    n = len(rows)
    confirms = sum(1 for r in rows.values() if r["group_verdict"].startswith("CONFIRM_"))
    agree_pnls = [ins["pnl_pct"] for r in rows.values() for ins in r["instructions"].values()
                  if ins["agree_with_peer"] is True and ins.get("pnl_pct") is not None]
    conflict_pnls = [ins["pnl_pct"] for r in rows.values() for ins in r["instructions"].values()
                     if ins["agree_with_peer"] is False and ins.get("pnl_pct") is not None]
    gate_helps = sum(1 for r in rows.values() for ins in r["instructions"].values() if ins["gate_would_help"])
    lines += ["", f"【累计 {n} 交易日】峰群确认 {confirms} 次"]

    def _avg(x):
        return f"{sum(x) / len(x):+.4f} (n={len(x)})" if x else "无样本"
    lines.append(f"  一致成交 pnl 均值: {_avg(agree_pnls)}")
    lines.append(f"  背离成交 pnl 均值: {_avg(conflict_pnls)}")
    lines.append(f"  '确认闸门本可避亏' 累计: {gate_helps} 笔")
    return "\n".join(lines)


def _rejoin_existing(date):
    """只刷新已有记录的 instruction/pnl 关联, 保留盘前快照(不可重采)。

    用于收盘后/次日早上回填 pnl_pct —— 不能重跑 _collect(会拿盘后数据覆盖盘前快照)。"""
    if not SHADOW_FILE.exists():
        return None
    rows = {}
    for line in SHADOW_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                r = json.loads(line)
                rows[r["date"]] = r
            except Exception:
                pass
    rec = rows.get(date)
    if not rec:
        return None
    verdict = rec.get("group_verdict", "NO_DATA")
    instr = _da_instructions(date, PEERS + PEERS_REF)
    joined = {}
    for sym, ins in instr.items():
        ag = _agree(ins["direction"], verdict)
        gate_help = bool(ag is False and ins.get("executed") and (ins.get("pnl_pct") or 0) < 0)
        joined[sym] = {**ins, "agree_with_peer": ag, "gate_would_help": gate_help}
    rec["instructions"] = joined
    rec["rejoined_at"] = datetime.now(ET).isoformat()
    return rec


def main(push=False, refresh_join=False):
    date = _today_et()
    if refresh_join:
        rec = _rejoin_existing(date)
        if rec is None:
            print(f"[skip] {date} 无既有盘前快照, 跳过 refresh-join (先跑一次采集)")
            return
    else:
        rec = _collect(date)
    rows = _upsert(rec)
    report = _report(rows)
    print(report)
    if push:
        try:
            from app.notifier.feishu import send_text
            send_text(report)
            print("\n[ok] 飞书已推送", file=sys.stderr)
        except Exception as e:
            print(f"\n[warn] 推送失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="盘前存储峰群影子 (只读)")
    ap.add_argument("--push", action="store_true", help="推送飞书")
    ap.add_argument("--refresh-join", action="store_true", dest="refresh_join",
                    help="只刷新指令/pnl关联(保留盘前快照), 收盘后/次日早用")
    a = ap.parse_args()
    main(push=a.push, refresh_join=a.refresh_join)
