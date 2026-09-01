#!/usr/bin/env python3
"""da_prompt_distill_shadow 窗口收口评估 (DEB-20260901c 阶段二, 9/16 一次性).

回答: "蒸馏版 system prompt (V2) 是否无损 (①②③) 且省 token ≥15% (④)?"
数据源: data/shadow/da_prompt_distill_shadow.jsonl (live 行, 窗口 9/1-9/15)
      + data/replay_bars_daily (T+1 收盘价)。

预注册判据 (DEB-20260901-da-prompt-distillation-shadow §四, 窗口 9/1-9/15
= 10 交易日, 不含 9/7 劳动节; 窗口起点 2026-09-01 红队复查勘误, 原误写 9/2):
  ① V2 JSON 有效率 ≥99% 且契约违约率 ≤V1
  ② 分布漂移: direction 各类比例与 HIGH 档比例 |V2−V1| ≤10pp
  ③ T+1 alpha 配对: V2 非劣于 V1 (配对差均值 ≥-0.10%)
  ④ token 降幅 (prompt_tokens) ≥15%
  全过 → PROMOTE_PROPOSAL (另立执行 DEB 换生产 SYSTEM_PROMPT+PROMPT_HASH 锚);
  仅④不过且③正向 → C4_FAIL_REFER_USER (DEB §四"仅凭③改善另议", 不自动结案);
  其余任一不过 → ARCHIVE; 无样本 → NO_DATA。

归因数据不依赖评估日重读检查点 (2026-09-01 红队复查修复: 生产检查点
_CKPT_KEEP_DAYS=3 自删, 9/16 评估时 9 月初检查点已不存在): V1 的
finish_reason/契约违约/entry_price/token 已在采集时随落行捕获;
缺字段行 (采集侧兜底) → 对应判据分母剔除并单列计数, 不静默吞。

判据③配对口径: 仅收 V1/V2 双臂均给出可执行方向 (BUY/SELL_SHORT 且
entry_price>0 — SKIP 无价剔除) 的候选; 方向性 T+1 收益用收盘价
BUY=(c1−c0)/c0, SELL_SHORT=(c0−c1)/c0; diff=V2−V1 (方向一致对 diff≡0)。
T+1 bar 缺失 (9/7 劳动节无 bar / 末采夜未就绪) → 剔除。
n_pairs<20 时按本仓 Rule 9 打 small_sample 旗标 (数值照算, 裁决参考读)。

只读, 幂等, 零副作用。挂载: 一次性 cron 9/16 06:35 CST (9/15 末夜采集收口后),
跑完自删本 crontab 行 (防年度复燃) + latest json 落盘 + 飞书推送。
回滚: 删本文件 + 删 crontab 行 (标记见 CRON_MARKER)。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW_PATH = ROOT / "data" / "shadow" / "da_prompt_distill_shadow.jsonl"
DAILY_DIR = ROOT / "data" / "replay_bars_daily"
OUT_JSON = ROOT / "data" / "shadow" / "da_prompt_distill_shadow_eval_latest.json"
CRON_MARKER = "DA-DISTILL-ONE-SHOT-ADJUDICATION"
WINDOW_START = "2026-09-01"
WINDOW_END = "2026-09-15"
MIN_JSON_RATE = 0.99          # ① V2 JSON 有效率
MAX_DRIFT = 0.10              # ② 分布漂移 (比例差, 绝对值)
NON_INFERIOR_FLOOR = -0.0010  # ③ 配对差均值下界 (-0.10%)
MIN_TOKEN_SAVE = 0.15         # ④ prompt_tokens 降幅
MIN_PAIRS_CONFIDENT = 20      # Rule 9: 小样本打旗标不硬裁
ACTIONS = ("BUY", "SELL_SHORT")
DIR_CATS = ("BUY", "SELL_SHORT", "SKIP")
_NORM = {"LONG": "BUY", "SHORT": "SELL_SHORT"}


def _norm_dir(v) -> str:
    d = str(v or "?").upper()
    return _NORM.get(d, d)


def load_rows(path: str = str(SHADOW_PATH)) -> list:
    """live 行 + 窗口内 (9/1-9/15, 9/7 当日行保留 — 其无 bar 由判据③自然剔除)."""
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
        except Exception as e:
            # R5 留痕 (除-pass 棘轮纪律): 坏行不入样本但必须可查
            print(f"[warn] distill-shadow 行解析失败 (剔除): {e}", file=sys.stderr)
            continue
        if r.get("run_mode") != "live":
            continue
        day = str(r.get("date", ""))[:10]
        if WINDOW_START <= day <= WINDOW_END:
            out.append(r)
    return out


def _json_valid(r: dict, arm: str) -> bool:
    return (not r.get(f"{arm}_error")
            and r.get(f"{arm}_finish_reason") == "stop"
            and _norm_dir(r.get(f"{arm}_direction")) in DIR_CATS)


def _vio_rate(rows: list, arm: str):
    """(违约行数, 有字段行数) — 字段缺失行不入分母 (采集兜底, 单列计数)."""
    has = [r for r in rows if isinstance(r.get(f"{arm}_contract_violations"), list)]
    bad = [r for r in has if len(r["v1_contract_violations" if arm == "v1" else "v2_contract_violations"]) > 0]
    return len(bad), len(has)


def t1_dir_return(date: str, symbol: str, direction: str):
    """方向性 T+1 收益: BUY=(c1−c0)/c0, SELL_SHORT=(c0−c1)/c0."""
    sym = str(symbol).replace("US.", "")
    f = DAILY_DIR / f"US_{sym}.json"
    if not f.exists():
        return None
    try:
        bars = json.load(open(f, encoding="utf-8"))  # [[date, o, h, l, close], ...]
    except Exception:
        return None
    idx = {b[0]: i for i, b in enumerate(bars)}
    i = idx.get(str(date)[:10])
    if i is None or i + 1 >= len(bars):
        return None
    c0, c1 = float(bars[i][4]), float(bars[i + 1][4])
    if c0 <= 0:
        return None
    return (c1 - c0) / c0 if direction == "BUY" else (c0 - c1) / c0


def _actionable(r: dict, arm: str) -> bool:
    if _norm_dir(r.get(f"{arm}_direction")) not in ACTIONS:
        return False
    price = r.get(f"{arm}_entry_price")
    try:
        return price is not None and float(price) > 0
    except (TypeError, ValueError):
        return False


def evaluate(rows: list) -> dict:
    n = len(rows)
    if not n:
        return {"n_live": 0, "verdict": "NO_DATA"}

    # ① JSON 有效 + 违约率
    v2_valid = sum(1 for r in rows if _json_valid(r, "v2"))
    v1_valid = sum(1 for r in rows if _json_valid(r, "v1"))
    json_rate_v2 = v2_valid / n
    bad1, has1 = _vio_rate(rows, "v1")
    bad2, has2 = _vio_rate(rows, "v2")
    vio_rate_v1 = bad1 / has1 if has1 else 0.0
    vio_rate_v2 = bad2 / has2 if has2 else 0.0
    c1 = json_rate_v2 >= MIN_JSON_RATE and vio_rate_v2 <= vio_rate_v1

    # ② 分布漂移 (direction 各类 + HIGH 档)
    def prop(rows_, key):
        return sum(1 for r in rows_ if key(r)) / len(rows_) if rows_ else 0.0
    drift_dir = max(abs(prop(rows, lambda r: _norm_dir(r.get("v2_direction")) == c)
                        - prop(rows, lambda r: _norm_dir(r.get("v1_direction")) == c))
                    for c in DIR_CATS)
    drift_high = abs(prop(rows, lambda r: r.get("v2_score_band") == "HIGH")
                     - prop(rows, lambda r: r.get("v1_score_band") == "HIGH"))
    c2 = max(drift_dir, drift_high) <= MAX_DRIFT

    # ③ T+1 配对非劣 (双臂可执行 + bar 就绪)
    diffs, n_skip_noattrib = [], 0
    for r in rows:
        if not (_actionable(r, "v1") and _actionable(r, "v2")):
            continue
        d1 = t1_dir_return(r.get("date", ""), r.get("symbol", ""), _norm_dir(r.get("v1_direction")))
        d2 = t1_dir_return(r.get("date", ""), r.get("symbol", ""), _norm_dir(r.get("v2_direction")))
        if d1 is None or d2 is None:
            continue
        diffs.append(d2 - d1)
    n_pairs = len(diffs)
    mean_diff = sum(diffs) / n_pairs if n_pairs else None
    worst = min(diffs) if diffs else None
    small_sample = n_pairs < MIN_PAIRS_CONFIDENT
    c3 = mean_diff is not None and mean_diff >= NON_INFERIOR_FLOOR

    # ④ token 降幅 (prompt_tokens, 聚合口径)
    tin1 = sum(r.get("v1_tokens_in") or 0 for r in rows if r.get("v1_tokens_in"))
    tin2 = sum(r.get("v2_tokens_in") or 0 for r in rows if r.get("v2_tokens_in"))
    save_in = 1.0 - tin2 / tin1 if tin1 else 0.0
    c4 = save_in >= MIN_TOKEN_SAVE

    if c1 and c2 and c3 and c4:
        verdict = "PROMOTE_PROPOSAL"
    elif c1 and c2 and c3 and not c4:
        verdict = "C4_FAIL_REFER_USER"  # DEB §四: ④不达标仅凭③改善另议, 不自动结案
    else:
        verdict = "ARCHIVE"
    return {
        "n_live": n, "n_v2_valid": v2_valid, "n_v1_valid": v1_valid,
        "json_rate_v2": round(json_rate_v2, 4),
        "vio_rate_v1": round(vio_rate_v1, 4), "vio_rate_v2": round(vio_rate_v2, 4),
        "n_vio_attrib_missing": (n - has1) + (n - has2),
        "drift_dir": round(drift_dir, 4), "drift_high": round(drift_high, 4),
        "n_pairs": n_pairs, "mean_diff": round(mean_diff, 6) if mean_diff is not None else None,
        "worst_pair_diff": round(worst, 6) if worst is not None else None,
        "small_sample_flag": small_sample,
        "save_prompt_tokens": round(save_in, 4), "v1_tokens_in": tin1, "v2_tokens_in": tin2,
        "c1": c1, "c2": c2, "c3": c3, "c4": c4, "verdict": verdict,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", default=str(SHADOW_PATH))
    args = ap.parse_args()
    rows = load_rows(args.jsonl)
    e = evaluate(rows)
    print("═══ da_prompt_distill_shadow 窗口收口评估 (9/1-9/15, 不含 9/7) ═══")
    if not rows:
        print("窗口内无 live 样本 → NO_DATA (检查采集 cron 40 21 * * 1-5 是否存活)")
    else:
        print(f"live 样本: n={e['n_live']}  V2 JSON 有效率={e['json_rate_v2']*100:.2f}% "
              f"(V1 对照有效率 {e['n_v1_valid']}/{e['n_live']})")
        print(f"违约率: V1={e['vio_rate_v1']*100:.2f}% V2={e['vio_rate_v2']*100:.2f}% "
              f"(归因字段缺失行={e['n_vio_attrib_missing']})")
        print(f"分布漂移: direction={e['drift_dir']*100:.2f}pp HIGH档={e['drift_high']*100:.2f}pp")
        ss = " [small_sample<%d 旗标, 参考读]" % MIN_PAIRS_CONFIDENT if e["small_sample_flag"] else ""
        print(f"T+1 配对: n={e['n_pairs']}{ss} 均值差={e['mean_diff']} 最差对={e['worst_pair_diff']}")
        print(f"prompt_tokens 降幅: {e['save_prompt_tokens']*100:.2f}% "
              f"(V1={e['v1_tokens_in']} V2={e['v2_tokens_in']})")
        print("判据① 有效≥%.0f%%且违约≤V1: %s" % (MIN_JSON_RATE * 100, "✓" if e["c1"] else "✗"))
        print("判据② 漂移≤%.0fpp: %s" % (MAX_DRIFT * 100, "✓" if e["c2"] else "✗"))
        print("判据③ 非劣(均值差≥%.2f%%): %s" % (NON_INFERIOR_FLOOR * 100, "✓" if e["c3"] else "✗"))
        print("判据④ token降幅≥%.0f%%: %s" % (MIN_TOKEN_SAVE * 100, "✓" if e["c4"] else "✗"))
    print(f"裁决: {e['verdict']}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(
        {**e, "window": f"{WINDOW_START}..{WINDOW_END}",
         "generated_at": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[distill-eval] 裁决快照已落盘 {OUT_JSON}")
    print(f"[distill-eval] {_self_remove_cron()}")

    try:
        from app.notifier.feishu import send_text
        send_text(
            "📋 DA 蒸馏影子窗口裁决 (9/1-9/15)\n"
            f"live n={e.get('n_live', 0)} V2有效率={e.get('json_rate_v2', 0)*100:.2f}% "
            f"漂移={e.get('drift_dir', 0)*100:.2f}pp\n"
            f"①{'✓' if e.get('c1') else '✗'} ②{'✓' if e.get('c2') else '✗'} "
            f"③{'✓' if e.get('c3') else '✗'} ④{'✓' if e.get('c4') else '✗'}\n"
            f"裁决: {e.get('verdict')}\n产物: {OUT_JSON}")
        print("[distill-eval] 飞书已推送")
    except Exception as ex:
        print(f"[distill-eval] 飞书推送失败: {ex}")
    return 0


def _self_remove_cron() -> str:
    """自删一次性 crontab 行 (成功/失败都删, 防固定日期行年度复燃;
    同 DA-GLM-ONE-SHOT-ADJUDICATION 模式)。"""
    import subprocess
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                             check=True).stdout
    except Exception as e:
        return f"crontab -l 失败 (跳过自删): {e}"
    kept = [l for l in cur.splitlines() if CRON_MARKER not in l]
    if len(kept) == len(cur.splitlines()):
        return "crontab 中未找到一次性行 (可能已删)"
    try:
        subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                       text=True, check=True)
        return "一次性 crontab 行已自删"
    except Exception as e:
        return f"crontab 自删失败 (需人工): {e}"


if __name__ == "__main__":
    raise SystemExit(main())
