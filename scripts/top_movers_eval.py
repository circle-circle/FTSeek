#!/usr/bin/env python3
"""富途全市场盘中异动榜 (通道 E) — 判据评估 (2026-09-21 RFC-092 清账批补建)

判据原文 (futu_channel_top_movers 注册表条目, 2026-08-29 预注册):
  "异动榜Top100入选 → 后续30min/日内窗口超额收益配对基线, IC 显著才评估接候选池发现通道;
   评估脚本待建 (先攒样)。"

为什么不用日K/1m bar 配对: 实测 replay_bars 对 (mover, day) 的覆盖率仅 **2%**
(87/5372) — 该目录只对"系统看过/交易过"的标的落盘, 而异动榜是全市场(~1800 标的)。
改用**自足口径**: 每轮快照自带 change_pct(相对昨收) ⇒ 同一 (ET session, symbol) 的
前后两次观测可自算区间收益, 零 bar 依赖。

口径:
  · 入场 = 该标的**当日首次出现在榜**的观测 (发现通道的可交易日点);
  · 结局 = 同日**末次观测**的 change_pct (收盘前最后一轮快照, 非精确收盘价);
  · 前向收益 = (1+c_exit/100)/(1+c_entry/100) − 1;
  · 信号 = 入场时的榜内排名 (rank, 1=涨幅最高) 与该轮 change_pct;
  · 超额基线 = **榜内对照** (rank 十分位对比): 榜首组 vs 榜尾组;
  · 统计 = 逐日 IC (Spearman) → 日序列 t (宪章第 9 律: pooled 不单独采信) + MDE(80%) + 符号检验;
  · 30min 窗口 = 入场后首个 ≥30 分钟的观测。

⚠️ 已知选择偏差 (必须随读数一起引用): 只有仍在榜的标的才被再次观测到 ⇒ 出榜样本被截断;
   横截面"榜首是否续涨"的读数因此偏乐观, 方向性结论只在 |t|≥2 且 MDE 覆盖时采信。

纯读, 只写自己的 latest.json。无推送/无 DB 写/无 cron 改动。
用法: python scripts/top_movers_eval.py [--out ...]
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
_log = logging.getLogger(__name__)
SRC = REPO / "data" / "futu_top_movers_shadow.jsonl"
OUT = REPO / "data" / "shadow" / "top_movers_eval_latest.json"
MIN_DAYS = 10      # 按日聚类要的会话数下限


def et_session(ts: str) -> Optional[str]:
    """CST 'YYYY-MM-DDTHH:MM:SS' → ET 会话日 (ET = CST − 12h, 夏令时 EDT)。

    优先用项目时区工具, 缺失时退回显式 −12h (并在此注明: 冬令时 EST 需 −13h,
    本影子观测窗为 8/29-9/19 全在 EDT 内)。
    """
    try:
        from app.utils.timezone import to_et  # type: ignore
        dt = datetime.datetime.fromisoformat(ts)
        return to_et(dt).strftime("%Y-%m-%d")
    except Exception:
        try:
            dt = datetime.datetime.fromisoformat(ts) - datetime.timedelta(hours=12)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 4:
        return None

    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return (num / (dx * dy)) if dx > 0 and dy > 0 else None


def _t_mde(vals: List[float]) -> Dict:
    n = len(vals)
    if n < 2:
        return {"n_days": n, "mean": None, "t": None, "mde_80pct": None, "distinguishable": False}
    m = sum(vals) / n
    var = sum((x - m) ** 2 for x in vals) / (n - 1)
    se = math.sqrt(var / n)
    return {"n_days": n, "mean": round(m, 4), "t": round(m / se, 3) if se > 0 else None,
            "mde_80pct": round(2.8 * se, 4),
            "distinguishable": bool(se > 0 and abs(m) > 2.8 * se)}


def load_panel(src: Path) -> Dict[str, Dict[str, List[Tuple[str, float, int]]]]:
    """{et_day: {symbol: [(ts, change_pct, rank_in_list)]}} — rank 以 change_pct 降序。"""
    panel: Dict[str, Dict[str, List[Tuple[str, float, int]]]] = defaultdict(lambda: defaultdict(list))
    if not src.exists():
        return panel
    with src.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                _log.warning("jsonl parse failed", exc_info=True)
                continue
            day = et_session(str(r.get("ts", "")))
            if not day:
                continue
            mv = r.get("movers") or []
            ranked = sorted(mv, key=lambda m: -(m.get("change_pct") or 0.0))
            for i, m in enumerate(ranked):
                sym, cp = m.get("symbol"), m.get("change_pct")
                if not sym or cp is None:
                    continue
                panel[day][sym].append((str(r.get("ts")), float(cp), i + 1))
    return panel


def evaluate(min_days: int = MIN_DAYS) -> Dict:
    panel = load_panel(SRC)
    day_ics: List[float] = []
    pairs: List[Dict] = []
    decile: Dict[str, List[float]] = defaultdict(list)
    for day, syms in sorted(panel.items()):
        xs, ys = [], []
        for sym, obs in syms.items():
            obs.sort()
            entry_ts, entry_cp, entry_rank = obs[0]
            exit_cp = obs[-1][1]
            if len(obs) < 2:
                continue
            fwd = (1 + exit_cp / 100.0) / (1 + entry_cp / 100.0) - 1
            xs.append(entry_cp)
            ys.append(fwd)
            pairs.append({"day": day, "symbol": sym, "entry_rank": entry_rank,
                          "entry_cp": round(entry_cp, 3), "fwd_pct": round(fwd * 100, 4),
                          "n_obs": len(obs)})
            decile["top10" if entry_rank <= 10 else ("bottom10" if entry_rank > 90 else "mid")].append(fwd)
            # 30min 窗口
            t0 = datetime.datetime.fromisoformat(entry_ts)
            for ts, cp, _rk in obs[1:]:
                if (datetime.datetime.fromisoformat(ts) - t0).total_seconds() >= 1800:
                    pairs[-1]["fwd30_pct"] = round(((1 + cp / 100.0) / (1 + entry_cp / 100.0) - 1) * 100, 4)
                    break
        ic = _spearman(xs, ys)
        if ic is not None:
            day_ics.append(ic)

    # 稳健性: 剔除"change_pct 全程未更新"(数据冻结)的配对 — 防 IC 由仪器假象驱动
    frozen = 0
    clean_ics: List[float] = []
    for day, syms in sorted(panel.items()):
        xs2, ys2 = [], []
        for sym, obs in syms.items():
            obs.sort()
            if len(obs) < 2:
                continue
            if len({round(o[1], 4) for o in obs}) < 2:
                frozen += 1
                continue
            xs2.append(obs[0][1])
            ys2.append((1 + obs[-1][1] / 100.0) / (1 + obs[0][1] / 100.0) - 1)
        ic2 = _spearman(xs2, ys2)
        if ic2 is not None:
            clean_ics.append(ic2)

    ic_stats = _t_mde(day_ics)
    sig = (ic_stats.get("t") is not None and abs(ic_stats["t"]) >= 2
           and ic_stats.get("distinguishable"))
    verdict = ("INSUFFICIENT_DAYS" if ic_stats["n_days"] < min_days else
               ("IC_SIGNIFICANT_GRADUATE_CANDIDATE" if sig else "NO_IC"))
    return {
        "n_obs_pairs": len(pairs),
        "n_days": len(panel),
        "n_days_with_ic": ic_stats["n_days"],
        "daily_ic": ic_stats,
        "ic_significant": sig,
        "robust_ex_frozen": {"n_frozen_pairs": frozen, "daily_ic": _t_mde(clean_ics),
                             "note": "剔除 change_pct 全程未更新的配对后重算 (防仪器假象)"},
        "decile_fwd_mean": {k: (round(sum(v) / len(v) * 100, 4) if v else None)
                            for k, v in decile.items()},
        "fwd30_mean_pct": (round(sum(p["fwd30_pct"] for p in pairs if "fwd30_pct" in p)
                                  / max(1, sum(1 for p in pairs if "fwd30_pct" in p)), 4)
                           if any("fwd30_pct" in p for p in pairs) else None),
        "verdict": verdict,
        "caliber": ("入场=当日首次入榜观测; 结局=同日末次观测 change_pct (非精确收盘); "
                    "前向收益=(1+c_exit/100)/(1+c_entry/100)-1; 信号=入场 change_pct; "
                    "IC=逐日 Spearman → 日序列 t + MDE(80%)"),
        "known_bias": "只有仍在榜的标的被再次观测 ⇒ 出榜样本截断, 榜首续涨读数偏乐观",
        "sample": pairs[:8],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="异动榜通道 E 判据评估 (record-only)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    res = evaluate()
    res["generated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    res["n"] = res["n_obs_pairs"]  # 注册表计数锚 (与判据同轴: 可配对观测数)
    print("═══ 异动榜通道 E 判据评估 (IC 显著才评估接候选池; n=可配对观测) ═══")
    print("会话 %d 天, 可配对(入榜→同日末次观测) %d 对 | 逐日 IC 有效 %d 天"
          % (res["n_days"], res["n_obs_pairs"], res["n_days_with_ic"]))
    ic = res["daily_ic"]
    print("逐日 IC: 均值 %s | t=%s | MDE(80%%)=%s ⇒ %s"
          % (ic["mean"], ic["t"], ic["mde_80pct"],
             "可分辨" if ic["distinguishable"] else "与0不可分辨"))
    print("榜内十分位前向收益: %s" % res["decile_fwd_mean"])
    print("30min 窗口均值: %s%%" % res["fwd30_mean_pct"])
    print("裁决: %s" % res["verdict"])
    # [只印不判] IC 方向读数 (2026-09-26 RFC-109④): 上方 verdict 的显著性判据是
    # abs(t)>=2 ⇒ **负 IC (榜首续跌=反向信号) 同样印 GRADUATE_CANDIDATE**, 照抄裁决行
    # 会把"该反向用"读成"可接候选池发现通道"(同向)。判决字段一字不动 (改判据=重新裁决),
    # 只并列印方向与十分位, 由裁决人自行判读。
    _icm = (res.get("daily_ic") or {}).get("mean")
    _dec = res.get("decile_fwd_mean") or {}
    if _icm is not None:
        _dirn = "榜首续跌 (负IC ⇒ 反向信号, 接候选池=接反)" if _icm < 0 \
            else "榜首续涨 (正IC ⇒ 同向)"
        print("[只印不判] IC 方向: mean=%s ⇒ %s | 十分位前向 top10=%s mid=%s bottom10=%s"
              % (_icm, _dirn, _dec.get("top10"), _dec.get("mid"), _dec.get("bottom10")))
    print("偏差注记: %s" % res["known_bias"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→ %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())