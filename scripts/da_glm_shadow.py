#!/usr/bin/env python3
"""DA glm-5.2 影子攒样 (2026-08-30 用户裁"1要")

目的: 攒样检验 8/30 DA 多模型对比中 glm-5.2 的唯一显著分歧 — SHORT 产出意愿。
  生产 4/4 里唯一给 SELL_SHORT 的是 glm; 生产候选池方向 3 BUY+1 SKIP 结构。

架构 (纯旁路, 零生产触碰):
  读生产 bridge (data/opportunity_scan_{ET日期}.json) 候选池
  → 每标的独立 collect_all (生产同款采集器, 输入同构)
  → glm-5.2 分析师 Pass1 (生产同款 SYSTEM_PROMPT/USER_PROMPT_TEMPLATE, temp=0.3)
  → append data/shadow/da_glm_shadow.jsonl (run_mode 区分 live/test)
  零落库 / 零 bridge 写入 / 零指令触碰 / 不动 da_checkpoint (生产续跑缓存)。

预注册判据 (10 交易日窗口 8/31-9/14, 攒满即裁):
  ① JSON 成功率 ≥90% (glm 在 DA 格式下的可靠性底线)
  ② glm SELL_SHORT 提案 ≥5 笔 (供给意愿真实性)
  ③ SHORT 提案的 T+1 方向性收益均值 >0 (提案有效性)
  全过 → 升级为正式提案另立 DEB 裁决; 任一不过 → 结案归档。

成本: ~¥0.08/标的 × 17 × 21 交易日 ≈ ¥24/窗口。
回滚: 删 crontab 一行 (15 20 * * 1-5 ... da_glm_shadow.py) + 删本文件。
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

FTSEEK_ROOT = "/opt/ftseek/FTSeek-US"
SKILL_SCRIPTS = "/root/.hermes/skills/deep-stock-analysis/scripts"
SHADOW_PATH = os.path.join(FTSEEK_ROOT, "data/shadow/da_glm_shadow.jsonl")
BRIDGE_DIR = os.path.join(FTSEEK_ROOT, "data")
MODEL = "glm-5.2"
MAX_TOKENS = 8000   # 放开生产 3000 上限防截断混淆; finish_reason 照记可分辨
POLL_UNTIL_ET = "10:30"  # bridge 未就绪则每 10min 重试到此时刻 (生产 07:00 ET 发车, ~07:30 就绪)


def _now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=4)


def _read_existing_dates(path):
    dates = set()
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                d = json.loads(line)
                if d.get("run_mode") == "live":
                    dates.add(d.get("date"))
            except Exception:
                continue
    return dates


def _wait_for_bridge(trade_date):
    path = os.path.join(BRIDGE_DIR, f"opportunity_scan_{trade_date}.json")
    while True:
        if os.path.exists(path):
            return path
        if _now_et().strftime("%H:%M") >= POLL_UNTIL_ET:
            print(f"[da-glm-shadow] bridge 未在 {POLL_UNTIL_ET} ET 前就绪, 本日放弃")
            return None
        print(f"[da-glm-shadow] bridge 未就绪, 10min 后重试 ...")
        time.sleep(600)


def main():
    symbols_limit = None
    run_mode = "live"
    date_override = ""
    for a in sys.argv[1:]:
        if a.startswith("--symbols="):
            symbols_limit = a.split("=", 1)[1].upper().replace("US.", "").split(",")
        if a.startswith("--date="):
            date_override = a.split("=", 1)[1]
        if a == "--test":
            run_mode = "test"

    # 模块加载顺序 (与生产一致): skill scripts 目录必须在 sys.path 最前 —
    # 其内部 `import config` 解析到 skill 的 config.py (PYTHON/DASHSCOPE_KEY/LLM_MODEL),
    # 若 FTSeek 根目录在前会被 config/ 包截胡 (AttributeError: no PYTHON)。
    sys.path.insert(0, FTSEEK_ROOT)
    sys.path.insert(0, SKILL_SCRIPTS)
    import config as da_config
    import data_collector
    import llm_synth

    if not da_config.DASHSCOPE_KEY:
        print("[da-glm-shadow] DASHSCOPE_API_KEY 缺失, 退出")
        return 1

    et_date = date_override or _now_et().strftime("%Y-%m-%d")
    if run_mode == "live" and et_date in _read_existing_dates(SHADOW_PATH):
        print(f"[da-glm-shadow] {et_date} 已有 live 记录, 幂等跳过")
        return 0

    bridge_path = _wait_for_bridge(et_date) if run_mode == "live" else \
        os.path.join(BRIDGE_DIR, f"opportunity_scan_{et_date}.json")
    if not bridge_path or not os.path.exists(bridge_path):
        print(f"[da-glm-shadow] 无 bridge ({et_date}), 退出")
        return 0

    bridge = json.load(open(bridge_path, encoding="utf-8"))
    prod_model = bridge.get("model_used")
    cands = bridge.get("candidates") or []
    if symbols_limit:
        cands = [c for c in cands
                 if str(c.get("symbol", "")).replace("US.", "").upper() in symbols_limit]
    print(f"[da-glm-shadow] {et_date} 候选 {len(cands)} 只 (生产模型={prod_model}) mode={run_mode}")

    os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
    n_ok = n_fail = 0
    with open(SHADOW_PATH, "a", encoding="utf-8") as fout:
        for c in cands:
            sym_raw = str(c.get("symbol", ""))
            sym = sym_raw.replace("US.", "").upper()
            t0 = time.time()
            row = {
                "type": "da_glm_shadow", "run_mode": run_mode, "date": et_date,
                "symbol": sym_raw if sym_raw.startswith("US.") else f"US.{sym}",
                "scanned_at": _now_et().isoformat(),
                "prod_model": prod_model,
                "prod_direction": c.get("direction"),
                "prod_score": c.get("score"),
                "glm_model": MODEL,
            }
            try:
                collected = data_collector.collect_all(
                    f"US.{sym}", kline_days=60, anomaly_days=7)
                q = collected.get("_quality") or {}
                row["input_quality"] = f'{q.get("ok")}/{q.get("total")}'
                # 材料漂移诊断: 影子采集比生产分析晚 ~1h, 盘前价格可能已变 —
                # 记录影子侧快照价, 判据结算时与生产 checkpoint._snapshot 对比,
                # 漂移超阈的样本标记 tainted (材料不同构, 分歧不可归因于模型)
                _snap = collected.get("snapshot") or {}
                row["shadow_price"] = _snap.get("last_price")
                # collect_all snapshot 无 change_pct 键, 从 prev_close 推导
                try:
                    _lp = float(_snap.get("last_price") or 0)
                    _pc = float(_snap.get("prev_close") or 0)
                    if _lp > 0 and _pc > 0:
                        row["shadow_change_pct"] = round((_lp - _pc) / _pc * 100, 2)
                except Exception:
                    pass
                data_text = llm_synth._format_data_for_prompt(collected)
                user_prompt = llm_synth.USER_PROMPT_TEMPLATE.format(data=data_text)

                t1 = time.time()
                res = llm_synth._call_llm(
                    llm_synth.SYSTEM_PROMPT, user_prompt,
                    temperature=0.3, max_tokens=MAX_TOKENS, model=MODEL)
                row["latency_s"] = round(time.time() - t1, 1)

                if not isinstance(res, dict) or "_error" in res:
                    row["glm_error"] = str(res)[:200]
                    row["glm_finish_reason"] = (res or {}).get("_finish_reason")
                    n_fail += 1
                else:
                    ep = res.get("execution_plan") or {}
                    trig = ep.get("invalidation_triggers") if isinstance(ep, dict) else None
                    row.update({
                        "glm_direction": res.get("direction"),
                        "glm_rating": res.get("rating"),
                        "glm_score_band": str(res.get("score_band") or "").upper(),
                        "glm_finish_reason": res.get("_finish_reason"),
                        "glm_tokens_out": (res.get("_usage") or {}).get("completion_tokens"),
                        "glm_trig_n": len(trig) if isinstance(trig, list) else 0,
                        "glm_raw": res,
                    })
                    # 止损宽度 (生产硬规 2-5%)
                    try:
                        entry = float(ep.get("entry_price"))
                        sl = float(ep.get("stop_loss_price"))
                        if entry > 0 and sl > 0:
                            row["glm_sl_pct"] = round(abs(sl - entry) / entry * 100, 2)
                    except Exception:
                        pass
                    n_ok += 1

                # 分歧分类 (核心跟踪量) — 标签归一化: 生产用 LONG/SHORT/SKIP,
                # glm 输出 BUY/SELL_SHORT/SKIP (DA 契约), 不归一会把对齐误判成分歧
                _norm = {"BUY": "LONG", "SELL_SHORT": "SHORT"}
                pd = _norm.get(str(c.get("direction")), str(c.get("direction")))
                gd = _norm.get(str(row.get("glm_direction")), str(row.get("glm_direction")))
                row["divergence"] = (
                    "glm_SHORT_prod_not_SHORT" if gd == "SHORT" and pd != "SHORT"
                    else "agree" if pd == gd else f"{gd}_vs_{pd}")
                # 生产 DA 实际输出方向 (checkpoint _llm_output.direction, BUY/SELL_SHORT 标签)
                # — 与 bridge 的 LONG/SHORT 是两套标签体系, 结算时双基准核对
                try:
                    _ck_p = os.path.join(FTSEEK_ROOT, "data/da_checkpoint",
                                         et_date, row["symbol"] + ".json")
                    if os.path.exists(_ck_p):
                        _ck = json.load(open(_ck_p, encoding="utf-8"))
                        row["prod_ckpt_direction"] = (_ck.get("_llm_output") or {}).get("direction")
                        row["prod_ckpt_model"] = _ck.get("_ckpt_model")
                except Exception:
                    pass
            except Exception as e:
                row["glm_error"] = f"exception: {e!r}"[:300]
                n_fail += 1
            row["elapsed_s"] = round(time.time() - t0, 1)
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")
            fout.flush()
            print(f"  {sym}: prod={c.get('direction')} glm={row.get('glm_direction')} "
                  f"div={row.get('divergence')} {row.get('elapsed_s')}s")

    print(f"[da-glm-shadow] 完成: 成功 {n_ok} / 失败 {n_fail} → {SHADOW_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
