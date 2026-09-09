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
     (9.5 修正 2026-08-30: 原门在 n≥5 ≈ 抛硬币, 与本仓 Rule 9 冲突 —
      现降级: n≥20 才出方向性结论, n<20 时③仅作立项参考不作证据读)
  全过 → 升级为正式提案另立 DEB 裁决; 任一不过 → 结案归档。

成本: ~¥0.08/标的 × 17 × 10 交易日 ≈ ¥13.6/窗口 (9.7 修正: 原判据窗口 8/31-9/14 = 10 交易日)。
回滚: 删 crontab 一行 (30 20 * * 1-5 ... da_glm_shadow.py) + 删本文件。
      (9.3 修正: 实挂是 30 20, 原文误写 15 20; 该 cron 行是本项唯一 kill switch)
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


def _alert_bridge_timeout(trade_date):
    """9.2 修复: '本日放弃'分支此前永不执行 (timeout 3000 先杀进程) —
    现 timeout 已抬到 9000, 本分支可达; 真到达时推钉钉, 防静默漏采。"""
    try:
        env_path = os.path.join(FTSEEK_ROOT, ".env.us")
        webhook = ""
        if os.path.exists(env_path):
            for line in open(env_path, encoding="utf-8"):
                if line.startswith("DINGTALK_WEBHOOK_URL_US="):
                    webhook = line.split("=", 1)[1].strip()
        if not webhook:
            return
        import urllib.request
        body = json.dumps({"msgtype": "text", "text": {"content": (
            f"[FTSeek-US告警] da_glm_shadow: bridge {trade_date} 未在 "
            f"{POLL_UNTIL_ET} ET 前就绪, 本日放弃 (静默漏采防护)")}}).encode()
        req = urllib.request.Request(
            webhook, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[da-glm-shadow] 告警推送失败(不阻断): {e!r}")


def _wait_for_bridge(trade_date):
    path = os.path.join(BRIDGE_DIR, f"opportunity_scan_{trade_date}.json")
    while True:
        if os.path.exists(path):
            return path
        if _now_et().strftime("%H:%M") >= POLL_UNTIL_ET:
            print(f"[da-glm-shadow] bridge 未在 {POLL_UNTIL_ET} ET 前就绪, 本日放弃")
            _alert_bridge_timeout(trade_date)
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
                # 材料漂移诊断: 影子采集比生产分析晚 ~1.5h, 盘前价可能已变。
                # [2026-09-01 同类问题修复·口径配对] 捕 effective_chg_pct (盘前真实
                # 涨跌, 与生产 checkpoint snapshot.change_pct 同 effective 口径 —
                # premarket 下 deep_analyzer 把 change_pct 重写为 effective_chg_pct)。
                # 旧 shadow_change_pct 从 last_price/prev_close 推导: 盘前
                # last_price=昨收 (data_collector.py LIF 盲区注释实锤), 推导恒≈0,
                # 与生产 effective 口径异基不可比 → is_tainted 误杀盘前异动>1pp 标的
                # (8/31 实测 EIX/NOW/BABA 3/7 误杀; 而两臂 prompt 基本面视图
                # _snapshot 实为逐字节一致 = 材料同构, 漂移≈0)。
                _snap = collected.get("snapshot") or {}
                row["shadow_price"] = _snap.get("last_price")
                row["shadow_effective_chg_pct"] = _snap.get("effective_chg_pct")
                data_text = llm_synth._format_data_for_prompt(collected)
                # [2026-09-09 F3] 与生产共用同一 prompt 构造函数 — 重排 flag 两臂同步
                #   翻转, 当日臂间比较保持单变量。glm 臂保持纯字符串格式 (不走数组
                #   缓存格式), 拼接后文本与生产模型所见逐字节一致。
                _u, _tail = llm_synth.build_user_prompt(data_text)
                user_prompt = (_tail + _u) if _tail else _u

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
                        "glm_tokens_in": (res.get("_usage") or {}).get("prompt_tokens"),
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
                # [2026-09-01 同类问题修复·过期免疫] 生产盘前涨跌随行捕获: 检查点
                #   _CKPT_KEEP_DAYS=3 自删, 9/15 评估时 8/31-9/10 已不存在, eval
                #   重读将静默失去 taint 对照 (同蒸馏影子 fix① 采集时捕获模式)。
                try:
                    _ck_p = os.path.join(FTSEEK_ROOT, "data/da_checkpoint",
                                         et_date, row["symbol"] + ".json")
                    if os.path.exists(_ck_p):
                        _ck = json.load(open(_ck_p, encoding="utf-8"))
                        row["prod_ckpt_direction"] = (_ck.get("_llm_output") or {}).get("direction")
                        row["prod_ckpt_model"] = _ck.get("_ckpt_model")
                        row["prod_snapshot_change_pct"] = (_ck.get("snapshot") or {}).get("change_pct")
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
