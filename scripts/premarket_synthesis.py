#!/usr/bin/env python3
"""盘前综合分析脚本 — 每个交易日盘前自动运行

流程:
  1. 运行 factor pipeline (已有)
  2. 运行 deep_research (已有, 但会跳过已有结果)
  3. 运行 portfolio_synth (新增: 多模型跨标的综合)
  4. 回写 battle_plan JSON
  5. 飞书推送摘要

用法:
  # 手动运行
  python3 scripts/premarket_synthesis.py

  # cron定时 (ET 08:30 = 北京 20:30, 周一-五)
  30 20 * * 1-5 cd /opt/ftseek/FTSeek-US && /opt/ftseek/FTSeek-US/venv/bin/python3 scripts/premarket_synthesis.py >> logs/premarket_synth.log 2>&1
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 确保项目根目录在 path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("premarket_synth")

PLAN_PATH = PROJECT_ROOT / "data" / "daily_battle_plan_us.json"


async def run_premarket_synthesis():
    """盘前综合分析主流程"""
    t0 = time.time()
    logger.info("=" * 60)
    logger.info("🚀 盘前综合分析启动")
    logger.info("=" * 60)

    # Step 1: 运行 factor pipeline (如果计划不存在或日期不对)
    from app.plan.pipeline import run_pipeline as run_factor_pipeline

    logger.info("[Step 1] 运行 Factor Pipeline...")
    plan = None
    try:
        plan = run_factor_pipeline()
        if plan and plan.get("targets"):
            logger.info(f"[Step 1] ✅ Factor Pipeline: {len(plan['targets'])} targets, regime={plan.get('market_regime')}")
        else:
            logger.warning("[Step 1] ⚠ Factor Pipeline 返回空 targets")
            return
    except Exception as e:
        logger.error(f"[Step 1] ❌ Factor Pipeline 失败: {e}")
        return

    # Step 2: deep_research (已有缓存检查，不会重复生成)
    from app.plan.deep_research_pipeline import run_deep_research_batch

    targets = plan.get("targets", [])

    # Step 1.5: 采集盘前市场数据 (Futu为主数据源)
    logger.info("[Step 1.5] 采集盘前市场数据...")
    premarket_data = None
    premarket_path = PLAN_PATH.parent / "premarket_enrichment.json"
    try:
        from app.data.premarket_enrichment import collect_premarket_data
        watchlist = [t.get("symbol", "") for t in targets if t.get("symbol")]
        premarket_data = collect_premarket_data(watchlist_symbols=watchlist)
        if premarket_data and not premarket_data.get("error"):
            premarket_path.write_text(json.dumps(premarket_data, ensure_ascii=False, indent=2))
            n_sectors = len(premarket_data.get("sector_rotation", {}).get("ranked", []))
            n_movers = len(premarket_data.get("premarket_movers", {}).get("gainers", [])) + len(premarket_data.get("premarket_movers", {}).get("losers", []))
            logger.info(f"[Step 1.5] ✅ 盘前数据: {n_sectors} sectors, {n_movers} movers, indices={list(premarket_data.get('market_indices',{}).keys())}")
        else:
            logger.warning(f"[Step 1.5] ⚠ 盘前数据采集失败: {premarket_data}")
            if premarket_path.exists():
                premarket_data = json.loads(premarket_path.read_text())
    except Exception as e:
        logger.warning(f"[Step 1.5] 盘前数据采集异常: {e}")
        if premarket_path.exists():
            try:
                premarket_data = json.loads(premarket_path.read_text())
            except Exception:
                pass

    logger.info(f"[Step 2] 运行 Deep Research (max 3 targets)...")
    try:
        dr_results = await run_deep_research_batch(
            targets=targets,
            premarket_data=premarket_data,
            max_targets=5,
        )
        success = sum(1 for r in dr_results if r.get("deep_research"))
        logger.info(f"[Step 2] ✅ Deep Research: {success}/{len(dr_results)} 成功")

        # Write deep_research results back into plan targets
        dr_map = {r["symbol"]: r.get("deep_research") for r in dr_results if r.get("symbol")}
        updated = 0
        for t in targets:
            sym = t.get("symbol", "")
            if sym in dr_map and dr_map[sym]:
                t["deep_research"] = dr_map[sym]
                updated += 1
        if updated:
            plan["targets"] = targets
            with open(PLAN_PATH, "w") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
            logger.info(f"[Step 2] ✅ Deep Research 回写 {updated}/{len(targets)} targets")
    except Exception as e:
        logger.error(f"[Step 2] ❌ Deep Research 失败: {e}")
        # 继续执行portfolio_synth (用已有数据)

    # Step 3: Portfolio Synthesis (新增)
    from app.plan.portfolio_synth import run_portfolio_synthesis

    # 重新加载plan (deep_research可能已回写)
    if PLAN_PATH.exists():
        try:
            with open(PLAN_PATH) as f:
                plan = json.load(f)
        except Exception:
            pass

    logger.info("[Step 3] 运行 Portfolio Synthesis (2模型并行)...")
    try:
        synth = await run_portfolio_synthesis(plan)
        if synth:
            logger.info(f"[Step 3] ✅ Portfolio Synthesis: agreement={synth.get('_meta', {}).get('agreement', '?')}")
            logger.info(f"  市况: {synth.get('market_assessment', '?')}")
            logger.info(f"  方向: {synth.get('recommended_direction', '?')}")
            for a in synth.get("action_list", []):
                logger.info(f"  #{a.get('rank','?')} {a.get('symbol','')} {a.get('action','')} {a.get('direction','')} conf={a.get('conviction','?')}% — {a.get('core_reason','')[:50]}")
            logger.info(f"  飞书摘要: {synth.get('feishu_brief', '')}")

            # 回写到plan
            plan["portfolio_synthesis"] = synth
            plan["_synthesis_generated_at"] = datetime.now(timezone.utc).isoformat()

            with open(PLAN_PATH, "w") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
            logger.info("[Step 3] ✅ 已回写到 daily_battle_plan_us.json")

            # Step 4: 飞书推送
            _send_feishu_notification(synth)

        else:
            logger.warning("[Step 3] ⚠ Portfolio Synthesis 失败")
    except Exception as e:
        logger.exception(f"[Step 3] ❌ Portfolio Synthesis 异常: {e}")

    elapsed = round(time.time() - t0, 1)
    logger.info(f"🏁 盘前综合分析完成 ({elapsed}s)")
    logger.info("=" * 60)


def _send_feishu_notification(synth: dict):
    """飞书推送盘前分析摘要"""
    try:
        from app.notifier.feishu import send_text as send_feishu_message
    except ImportError:
        logger.warning("[飞书] notifier 不可用，跳过推送")
        return

    brief = synth.get("feishu_brief", "")
    assessment = synth.get("market_assessment", "")
    direction = synth.get("recommended_direction", "")
    risk = synth.get("risk_level", "")
    actions = synth.get("action_list", [])

    # 构建飞书消息
    lines = ["📊 盘前综合分析\n"]
    lines.append(f"市况: {assessment}")
    lines.append(f"方向: {direction} | 风险: {risk}\n")
    lines.append("📋 操作名单:")
    for a in actions:
        rank = a.get("rank", "?")
        sym = a.get("symbol", "").replace("US.", "")
        action = a.get("action", "")
        dir_text = a.get("direction", "")
        conf = a.get("conviction", 0)
        reason = a.get("core_reason", "")[:40]
        emoji = {"BUY": "🟢", "SELL": "🔴", "CONDITIONAL": "🟡", "WATCH": "⚪"}.get(action, "⚪")
        lines.append(f"  {emoji} #{rank} {sym} {action} {dir_text} {conf}% — {reason}")

    lines.append(f"\n💡 {brief}")
    msg = "\n".join(lines)

    try:
        send_feishu_message(msg)
        logger.info("[飞书] ✅ 推送成功")
    except Exception as e:
        logger.warning(f"[飞书] 推送失败: {e}")


if __name__ == "__main__":
    asyncio.run(run_premarket_synthesis())
