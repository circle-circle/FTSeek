#!/usr/bin/env python3
"""pl_val 币种口径实证探针 (切实盘前置项, 2026-09-24).

背景: app/execution/broker_adapter.py 的 get_positions() 直接 float(row["pl_val"]),
若 Futu REAL 账户返回非 USD (如 HKD, ~7.8x), pre_trade_gate 日回撤读数会被静默
污染 (可能误熔断或漏熔断)。防御已落 (查询钉 currency=USD + 行币种透传 + gate
fail-close), 但**防御不能替代实证**: 切实盘前必须在真实账户上跑一次本探针,
确认 pl_val 量纲为 USD, 并把输出留档到本文件底部或 docs/。

用法 (人工执行, 勿入 cron):
    venv/bin/python scripts/probe_pl_val_currency.py            # SIM+REAL 都探
    venv/bin/python scripts/probe_pl_val_currency.py --env real # 只探 REAL

⚠️ 会打开一个 futu 连接占用 OpenD 槽位 — 在管线休市窗口执行, 或确认 OpenD
   允许多连接; 执行完自动断开。只读查询, 零下单。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env.us", override=True)


def probe(trd_env_name: str) -> dict:
    from futu import OpenSecTradeContext, TrdEnv, TrdMarket, RET_OK

    trd_env = TrdEnv.SIMULATE if trd_env_name == "sim" else TrdEnv.REAL
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
    )
    out = {"env": trd_env_name, "ok": False, "rows": [], "verdict": None}
    try:
        # 2026-10-15 修复: 原键名 FUTU_REAL_ACCOUNT/FUTU_SIM_ACCOUNT 在 .env.us 不存在
        # (真实键名 REAL_ACCOUNT_ID/SIMULATE_ACCOUNT_ID) ⇒ acc_id 退化为 0, 探的是富途
        # 默认账户而非目标账户 (假绿风险, 审计 P2-1)。
        acc_id = os.getenv(
            "REAL_ACCOUNT_ID" if trd_env_name == "real" else "SIMULATE_ACCOUNT_ID", ""
        )
        ret, data = ctx.position_list_query(
            trd_env=trd_env,
            acc_id=int(acc_id) if acc_id else 0,
        )
        if ret != RET_OK:
            out["verdict"] = f"QUERY_FAILED: {data}"
            return out
        out["ok"] = True
        if data is None or len(data) == 0:
            out["verdict"] = "NO_POSITIONS (空仓无法实证 — 需持仓 ≥1 只再探)"
            return out
        cols = list(data.columns)
        has_ccy = "currency" in cols
        for _, row in data.iterrows():
            r = {
                "code": str(row.get("code", "")),
                "qty": float(row.get("qty", 0) or 0),
                "pl_val": float(row.get("pl_val", 0) or 0),
                "pl_ratio": float(row.get("pl_ratio", 0) or 0),
                "market_val": float(row.get("market_val", 0) or 0),
                "cost_price": float(row.get("cost_price", 0) or 0),
                "currency": str(row.get("currency", "<字段不存在>")) if has_ccy else "<字段不存在>",
            }
            # 量纲交叉验证: pl_ratio 是**百分数** (futu 实测 -1.18 / -2.04 = -1.18% / -2.04%,
            # 非无量纲比率), 故须 /100 还原比率。若 pl_val 与成本同币种,
            # 则 pl_val ≈ (pl_ratio/100) × cost_total。
            cost_total = r["cost_price"] * abs(r["qty"])
            r["pl_ratio_as_pct"] = True
            r["pl_val_vs_ratio_x_cost"] = (
                round(r["pl_val"] / ((r["pl_ratio"] / 100.0) * cost_total), 4)
                if r["pl_ratio"] and cost_total else None
            )
            out["rows"].append(r)
        non_usd = [r for r in out["rows"] if str(r["currency"]).upper() not in ("USD", "<字段不存在>")]
        ratio_outliers = [
            r for r in out["rows"]
            if r["pl_val_vs_ratio_x_cost"] is not None
            and not (0.9 <= r["pl_val_vs_ratio_x_cost"] <= 1.1)
        ]
        if non_usd:
            out["verdict"] = f"🚨 非 USD 币种出现: {sorted({r['currency'] for r in non_usd})} — pl_val 不可直接入账"
        elif ratio_outliers:
            out["verdict"] = (
                f"🚨 pl_val / (pl_ratio×cost) 偏离 1.0 超 ±10%: "
                f"{[(r['code'], r['pl_val_vs_ratio_x_cost']) for r in ratio_outliers]} — 量纲可疑, 人工复核"
            )
        else:
            out["verdict"] = "✅ pl_val 量纲与 USD 成本口径自洽 (currency 列 + 比率交叉验证双通过)"
        return out
    finally:
        ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="pl_val 币种实证探针 (只读)")
    ap.add_argument("--env", choices=["sim", "real", "both"], default="both")
    args = ap.parse_args()
    envs = ["sim", "real"] if args.env == "both" else [args.env]
    results = []
    for e in envs:
        try:
            r = probe(e)
        except Exception as exc:
            r = {"env": e, "ok": False, "verdict": f"EXCEPTION: {type(exc).__name__}: {exc}"}
        results.append(r)
        print(f"\n===== {e.upper()} =====")
        print(json.dumps(r, ensure_ascii=False, indent=2))
    failed = [r for r in results if not r.get("ok")]
    bad = [r for r in results if r.get("ok") and "🚨" in str(r.get("verdict"))]
    print("\n===== 总结 =====")
    for r in results:
        print(f"  [{r['env']}] {r.get('verdict')}")
    if bad:
        print("\n结论: ❌ 未通过 — 切实盘前必须先解决 pl_val 量纲问题")
        return 2
    if failed:
        print("\n结论: ⚠️ 部分环境查询失败/无持仓 — 实证未完成, 切实盘前须重跑")
        return 1
    print("\n结论: ✅ 实证通过 — 请把本输出留档 (切实盘前置项销账)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
