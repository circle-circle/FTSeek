#!/usr/bin/env python3
"""da_glm_shadow 影子评估 (2026-08-30 补注册, DEB-20260829 第9轮 9.1/9.6 修复).

回答: "glm-5.2 在 DA 材料上的产出是否值得升级为正式提案?"
数据源: data/shadow/da_glm_shadow.jsonl (live 行) + data/replay_bars_daily (T+1 价).

预注册判据 (注册表 da_glm_shadow, 窗口 8/31-9/14 = 10 交易日):
  ① JSON 成功率 ≥90% (无 glm_error 且解析出 direction)
  ② glm SELL_SHORT 提案 ≥5 笔 (供给意愿真实性)
  ③ SHORT 提案 T+1 方向性收益均值 >0 — 9.5 修正: n≥20 才出方向性结论,
     n<20 时仅作立项参考不作证据读 (原 n≥5 ≈ 抛硬币, 与本仓 Rule 9 冲突)
  ①②全过 → PROMOTE_PROPOSAL (另立 DEB 裁决); 任一不过 → ARCHIVE。
  ③ 不作为毕业门票, 但 n≥20 时输出方向性参考值。

9.6: 材料漂移过滤 — 影子采集比生产晚 ~1h, 盘前价可能已变;
  |shadow_change_pct - 生产ckpt snapshot.change_pct| > TAINT_PCT (1.0pp)
  → tainted=1, 从 ②③ 分歧归因中剔除单列 (分歧不可归因于模型)。

只读, 幂等, 零副作用。挂载: 周检/窗口到期 (9/14) 机械裁决。
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW_PATH = ROOT / "data" / "shadow" / "da_glm_shadow.jsonl"
DAILY_DIR = ROOT / "data" / "replay_bars_daily"
MIN_N_JSON = 0.90        # ① JSON 成功率
MIN_N_SHORT = 5          # ② SHORT 提案数
MIN_N_DIR = 20           # ③ 方向性结论最小样本 (9.5 修正)
TAINT_PCT = 1.0          # 9.6 材料漂移阈值 (pp)


def load_rows(path: str = str(SHADOW_PATH), live_only: bool = True) -> list:
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if live_only and r.get("run_mode") != "live":
            continue
        out.append(r)
    return out


def _ckpt_snapshot(date: str, symbol: str) -> dict:
    """生产 checkpoint 的盘前快照 (真实键名 'snapshot' — 第9轮文档误写 '_snapshot')."""
    p = ROOT / "data" / "da_checkpoint" / date / f"{symbol}.json"
    if not p.exists():
        return {}
    try:
        return (json.load(open(p, encoding="utf-8")).get("snapshot")) or {}
    except Exception:
        return {}


def is_tainted(row: dict, snap: dict = None) -> bool:
    """9.6: 影子侧盘前涨跌与生产快照偏差超阈 → 材料不同构, 分歧不可归因于模型."""
    if snap is None:
        snap = _ckpt_snapshot(row.get("date", ""), row.get("symbol", ""))
    mine = row.get("shadow_change_pct")
    prod = snap.get("change_pct") if isinstance(snap, dict) else None
    if mine is None or prod is None:
        return False  # 缺一不判 (宁漏勿错杀, 与全仓保守口径一致)
    return abs(float(mine) - float(prod)) > TAINT_PCT


def t1_return_short(row: dict) -> float | None:
    """SHORT 提案的 T+1 方向性收益: (close_t0 - close_t1) / close_t0.
    盘后复盘就绪后才有次日收盘 → 窗口内最后一日通常无值 (返回 None)."""
    sym = str(row.get("symbol", "")).replace("US.", "")
    f = DAILY_DIR / f"US_{sym}.json"
    if not f.exists():
        return None
    try:
        bars = json.load(open(f, encoding="utf-8"))  # [[date, o, h, l, close], ...]
    except Exception:
        return None
    idx = {b[0]: i for i, b in enumerate(bars)}
    i = idx.get(str(row.get("date", ""))[:10])
    if i is None or i + 1 >= len(bars):
        return None
    c0, c1 = float(bars[i][4]), float(bars[i + 1][4])
    if c0 <= 0:
        return None
    return (c0 - c1) / c0  # SHORT: 跌为正收益


def evaluate(rows: list) -> dict:
    n = len(rows)
    ok = [r for r in rows if not r.get("glm_error") and r.get("glm_direction")]
    json_rate = len(ok) / n if n else 0.0
    tainted = [r for r in ok if is_tainted(r)]
    clean = [r for r in ok if r not in tainted]
    shorts = [r for r in clean if str(r.get("glm_direction")) in ("SELL_SHORT", "SHORT")]
    t1 = [x for x in (t1_return_short(r) for r in shorts) if x is not None]
    c1 = json_rate >= MIN_N_JSON
    c2 = len(shorts) >= MIN_N_SHORT
    dir_mean = sum(t1) / len(t1) if t1 else None
    c3_actionable = len(t1) >= MIN_N_DIR and dir_mean is not None and dir_mean > 0
    c3_reference = len(t1) > 0 and dir_mean is not None
    verdict = "PROMOTE_PROPOSAL" if (c1 and c2) else ("ARCHIVE" if n else "NO_DATA")
    return {
        "n_live": n, "n_ok": len(ok), "json_rate": round(json_rate, 4),
        "n_tainted": len(tainted), "n_short": len(shorts),
        "n_t1": len(t1), "dir_mean": round(dir_mean, 6) if dir_mean is not None else None,
        "c1_json": c1, "c2_short": c2,
        "c3_direction": c3_actionable, "c3_reference_only": c3_reference and not c3_actionable,
        "verdict": verdict,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", default=str(SHADOW_PATH))
    args = ap.parse_args()
    rows = load_rows(args.jsonl)
    e = evaluate(rows)
    print("═══ da_glm_shadow 影子评估 (窗口 8/31-9/14) ═══")
    print(f"live 样本: n={e['n_live']}  解析成功: {e['n_ok']} ({e['json_rate']*100:.1f}%)")
    print(f"tainted (材料漂移>{TAINT_PCT}pp): {e['n_tainted']}  SHORT提案(去污后): {e['n_short']}")
    print(f"T+1 可结算: {e['n_t1']}  方向均值: {e['dir_mean']}")
    print("判据① 成功率≥%.0f%%: %s" % (MIN_N_JSON * 100, "✓" if e["c1_json"] else "✗"))
    print("判据② SHORT提案≥%d: %s" % (MIN_N_SHORT, "✓" if e["c2_short"] else f"✗({e['n_short']})"))
    if e["c3_reference_only"]:
        print(f"判据③ 方向性: n={e['n_t1']}<{MIN_N_DIR} 仅参考 (均值={e['dir_mean']:+.4%}), 不作证据读")
    else:
        print("判据③ 方向性均值>0: %s" % ("✓" if e["c3_direction"] else "✗"))
    print(f"裁决: {e['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
