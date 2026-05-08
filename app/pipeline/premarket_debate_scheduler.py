"""
盘前辩论调度器 — APScheduler 定时触发 PremarkDebateAgent

时间: 08:20 HKT (盘前 Pipeline 08:30 之前 10 分钟)
输出: data/premarket_debate.json (供 Pipeline/BattlePlanGenerator 使用)

2026-05-07 v2: 去除 USOvernightMonitor 跨市场耦合,
改用富途实时指标 + LLM 跨市场推理框架
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, List

logger = logging.getLogger("ftseek.pipeline.premarket_debate_scheduler")

# 辩论结果输出路径
DEBATE_OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'premarket_debate.json'
)


class PremarketDebateScheduler:
    """
    盘前辩论调度器

    由 APScheduler cron 触发 (08:20 HKT, Mon-Fri)，
    执行多模型辩论并保存结果供 Pipeline 使用。
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._last_result: Optional[Dict] = None
        self._last_run_at: Optional[datetime] = None

        logger.info(
            f"[PremarketDebateScheduler] 初始化: "
            f"enabled={enabled}, output={DEBATE_OUTPUT_PATH}"
        )

    def run(self, date: str = None) -> Dict[str, Any]:
        """
        同步入口 (供 APScheduler 调用)

        Args:
            date: 日期 (YYYY-MM-DD), 默认今天

        Returns:
            辩论结果字典
        """
        if not self.enabled:
            logger.info("[PremarketDebateScheduler] 未启用，跳过")
            return {"status": "skipped", "reason": "disabled"}

        from app.harness.premarket_debate import PremarkDebateAgent
        from config.config_center import get_config_center

        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        logger.info(f"[PremarketDebateScheduler] 开始盘前辩论: {date}")

        try:
            # 1. 收集候选池 (从 watchlist / 动态池)
            candidate_pool = self._get_candidate_pool()

            # 防御: 空候选池保护
            if not candidate_pool:
                logger.warning("[PremarketDebateScheduler] 候选池为空，跳过辩论 (避免LLM幻觉)")
                result = {
                    "mode": "skipped",
                    "success_count": 0,
                    "total_models": 0,
                    "execution_time": 0,
                    "arbiter_conclusion": {
                        "consensus_ranking": [],
                        "market_sentiment": "NEUTRAL",
                        "risk_level": "UNKNOWN",
                        "auditor_score": 50,
                        "action_recommendation": "NEUTRAL",
                        "suggested_position_pct": 0.0,
                        "avoid_codes": [],
                        "bull_summary": "候选池为空，请先配置港股 watchlist 或动态池",
                        "bear_summary": "无标的可分析",
                        "auditor_summary": "候选池配置缺失",
                    },
                }
                self._last_result = result
                self._last_run_at = datetime.now()
                self._save_result(result)
                self._send_notification(result, date)
                return result

            # 2. 收集市场上下文
            market_context = self._get_market_context(date)

            # 3. 执行辩论
            agent = PremarkDebateAgent()
            input_data = {
                "candidate_pool": candidate_pool,
                "market_context": market_context,
                "date": date,
            }

            # 在事件循环中执行 (异步)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(
                            asyncio.run, agent.execute(input_data)
                        )
                        result = future.result(timeout=150)
                else:
                    result = loop.run_until_complete(agent.execute(input_data))
            except RuntimeError:
                # 无事件循环，创建新的
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(agent.execute(input_data))

            self._last_result = result
            self._last_run_at = datetime.now()

            # 4. 保存结果到文件
            self._save_result(result)

            # 5. 推送通知
            self._send_notification(result, date)

            logger.info(
                f"[PremarketDebateScheduler] 辩论完成: "
                f"mode={result.get('mode')}, "
                f"success={result.get('success_count')}/{result.get('total_models')}"
            )

            return result

        except Exception as e:
            logger.error(f"[PremarketDebateScheduler] 辩论失败: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    def get_last_result(self) -> Optional[Dict]:
        """获取上次辩论结果"""
        if self._last_result:
            return self._last_result

        # fallback: 从文件读取
        try:
            if os.path.exists(DEBATE_OUTPUT_PATH):
                with open(DEBATE_OUTPUT_PATH, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    #  数据收集
    # ------------------------------------------------------------------

    def _get_candidate_pool(self) -> list:
        """从统一配置获取候选股票池"""
        try:
            from config.config_center import get_config_center
            cc = get_config_center()

            # 从 watchlist 获取
            watchlist = cc.get('watchlist', {}) or {}
            codes = []

            # 港股 watchlist (修复: 使用 hk_stocks 而非 hk)
            hk_list = watchlist.get('hk_stocks', watchlist.get('hk', []))
            for item in hk_list[:15]:  # 最多 15 只
                if isinstance(item, str):
                    code = item if item.startswith("HK.") else f"HK.{item}"
                    codes.append({"code": code, "source": "watchlist"})
                elif isinstance(item, dict):
                    code = item.get('code', '')
                    if code:
                        if not code.startswith("HK."):
                            code = f"HK.{code}"
                        codes.append({
                            "code": code,
                            "name": item.get('name', ''),
                            "source": "watchlist",
                        })

            # 从动态池补充 (优先于持仓)
            try:
                from app.utils.daily_active_pool import get_daily_active_pool
                pool = get_daily_active_pool(market_prefix="HK.")
                for entry in pool.get_entries()[:10]:
                    code = entry.code
                    if code and not any(c.get('code') == code for c in codes):
                        # 防御: 只接受 HK. 前缀
                        if code.startswith("HK."):
                            codes.append({
                                "code": code,
                                "source": entry.source,
                            })
            except Exception as e:
                logger.debug(f"[PremarketDebateScheduler] 动态池获取失败: {e}")

            # 从持仓补充
            try:
                from app.trader.position_manager import PositionManager
                pm = PositionManager.get_instance()
                positions = pm.get_all_positions()
                for pos in positions:
                    code = getattr(pos, 'code', '')
                    if code and not any(c.get('code') == code for c in codes):
                        # 防御: 只接受 HK. 前缀
                        if code.startswith("HK."):
                            codes.append({
                                "code": code,
                                "source": "position",
                            })
            except Exception:
                pass

            # 防御: 过滤非港股标的
            codes = [c for c in codes if c.get('code', '').startswith('HK.')]

            logger.info(f"[PremarketDebateScheduler] 候选池: {len(codes)} 只")
            return codes

        except Exception as e:
            logger.warning(f"[PremarketDebateScheduler] 候选池获取失败: {e}")
            return []

    def _get_market_context(self, date: str) -> dict:
        """收集市场上下文 — 基于富途实时指标 (v2: 去跨市场耦合)"""
        context = {"date": date}

        # ── 1. 港股指数/指标 (通过富途 OpenD) ──
        futu_context = self._fetch_futu_premarket_indicators()
        if futu_context:
            context["futu_indicators"] = futu_context

        # ── 2. 候选标的快照 (用于辩论参考) ──
        pool_snapshots = self._fetch_candidate_snapshots()
        if pool_snapshots:
            context["candidate_snapshots"] = pool_snapshots

        return context

    def _fetch_futu_premarket_indicators(self) -> Optional[Dict]:
        """通过富途 OpenD 获取盘前市场指标"""
        try:
            from futu import OpenQuoteContext, RET_OK
            from app import config

            ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            indicators = {}

            # 恒生指数/国指/科技指数
            indices = {
                "HK.800000": "恒生指数",
                "HK.800700": "国企指数",
            }
            ret, data = ctx.get_market_snapshot(list(indices.keys()))
            if ret == RET_OK and data is not None and not data.empty:
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    name = indices.get(code, code)
                    last = float(row.get("last_price", 0) or 0)
                    change = float(row.get("change_rate", 0) or 0) / 100.0
                    indicators[name] = {
                        "code": code,
                        "last_price": last,
                        "change_pct": round(change, 4),
                    }

            # A 股指数 (上证/沪深300/创业板 — 前一日收盘)
            ashare_indices = {
                "SH.000001": "上证指数",
                "SH.000300": "沪深300",
                "SZ.399006": "创业板指",
            }
            ret, data = ctx.get_market_snapshot(list(ashare_indices.keys()))
            if ret == RET_OK and data is not None and not data.empty:
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    name = ashare_indices.get(code, code)
                    last = float(row.get("last_price", 0) or 0)
                    change = float(row.get("change_rate", 0) or 0) / 100.0
                    indicators[name] = {
                        "code": code,
                        "last_price": last,
                        "change_pct": round(change, 4),
                    }

            # 汇率 (USD/HKD)
            ret, data = ctx.get_market_snapshot(["HK.USDHKD"])
            if ret == RET_OK and data is not None and not data.empty:
                row = data.iloc[0]
                indicators["USD_HKD"] = float(row.get("last_price", 0) or 0)

            ctx.close()
            logger.info(f"[Premarket] 富途指标获取成功: {len(indicators)} 项")
            return indicators

        except Exception as e:
            logger.warning(f"[Premarket] 富途指标获取失败: {e}")
            return None

    def _fetch_candidate_snapshots(self) -> Optional[List[Dict]]:
        """获取候选标的实时快照 (用于辩论参考)"""
        try:
            from futu import OpenQuoteContext, RET_OK
            from app import config
            from config.config_center import get_config_center

            cc = get_config_center()
            watchlist = cc.get('watchlist', {}) or {}
            hk_list = watchlist.get('hk_stocks', watchlist.get('hk', []))

            if not hk_list:
                return None

            codes = []
            for item in hk_list[:10]:
                if isinstance(item, str):
                    code = item if item.startswith("HK.") else f"HK.{item}"
                    codes.append(code)
                elif isinstance(item, dict):
                    code = item.get('code', '')
                    if code and not code.startswith("HK."):
                        code = f"HK.{code}"
                    if code:
                        codes.append(code)

            if not codes:
                return None

            ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            ret, data = ctx.get_market_snapshot(codes)
            snapshots = []

            if ret == RET_OK and data is not None and not data.empty:
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    last = float(row.get("last_price", 0) or 0)
                    change = float(row.get("change_rate", 0) or 0) / 100.0
                    vol_ratio = float(row.get("volume_ratio", 0) or 0)
                    turnover = float(row.get("turnover", 0) or 0)
                    amplitude = float(row.get("amplitude", 0) or 0) / 100.0

                    snapshots.append({
                        "code": code,
                        "last_price": last,
                        "change_pct": round(change, 4),
                        "volume_ratio": round(vol_ratio, 2),
                        "turnover": round(turnover, 4),
                        "amplitude_pct": round(amplitude, 4),
                    })

            ctx.close()
            logger.info(f"[Premarket] 候选标的快照: {len(snapshots)} 只")
            return snapshots

        except Exception as e:
            logger.warning(f"[Premarket] 候选快照获取失败: {e}")
            return None

    # ------------------------------------------------------------------
    #  持久化
    # ------------------------------------------------------------------

    def _save_result(self, result: Dict):
        """保存辩论结果到 JSON 文件"""
        try:
            os.makedirs(os.path.dirname(DEBATE_OUTPUT_PATH), exist_ok=True)

            output = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "timestamp": datetime.now().isoformat(),
                "mode": result.get("mode"),
                "success_count": result.get("success_count"),
                "total_models": result.get("total_models"),
                "execution_time": result.get("execution_time"),
                "arbiter_conclusion": result.get("arbiter_conclusion", {}),
                "models_used": {
                    role: result["results"].get(f"{role}_model", "N/A")
                    for role in ["bull", "bear", "auditor"]
                },
            }

            with open(DEBATE_OUTPUT_PATH, 'w', encoding='utf-8') as f:
                json.dump(output, f, ensure_ascii=False, indent=2)

            logger.info(f"[PremarketDebateScheduler] 辩论结果已保存: {DEBATE_OUTPUT_PATH}")

        except Exception as e:
            logger.error(f"[PremarketDebateScheduler] 保存失败: {e}")

    # ------------------------------------------------------------------
    #  通知
    # ------------------------------------------------------------------

    def _send_notification(self, result: Dict, date: str):
        """推送辩论摘要到钉钉"""
        try:
            arbiter = result.get('arbiter_conclusion', {})
            consensus = arbiter.get('consensus_ranking', [])

            lines = [
                f"🗣️ **盘前辩论报告** ({date})",
                "",
                f"模式: {result.get('mode', '?')}",
                f"成功: {result.get('success_count', 0)}/{result.get('total_models', 0)}",
                f"耗时: {result.get('execution_time', 0):.0f}s",
                "",
                f"市场情绪: {arbiter.get('market_sentiment', 'N/A')}",
                f"风险等级: {arbiter.get('risk_level', 'N/A')}",
                f"审计评分: {arbiter.get('auditor_score', 'N/A')}/100",
                f"操作建议: {arbiter.get('action_recommendation', 'N/A')}",
                f"建议仓位: {arbiter.get('suggested_position_pct', 0):.0%}",
                "",
            ]

            if consensus:
                lines.append("**标的共识排序:**")
                for i, item in enumerate(consensus[:5], 1):
                    flag = "✅" if item['net_confidence'] > 0 else "⚠️"
                    lines.append(
                        f"  {i}. {flag} {item['code']}: "
                        f"净信心={item['net_confidence']:+.2f}"
                    )
                lines.append("")

            # 多空摘要
            bull_summary = arbiter.get('bull_summary', '')
            bear_summary = arbiter.get('bear_summary', '')
            if bull_summary:
                lines.append(f"🐂 多头: {bull_summary}")
            if bear_summary:
                lines.append(f"🐻 空头: {bear_summary}")

            # 推送 (通过 notifier)
            try:
                from app.notifier import get_dingtalk_notifier
                notifier = get_dingtalk_notifier()
                if notifier:
                    notifier.send_text("\n".join(lines))
            except Exception as e:
                logger.debug(f"[PremarketDebateScheduler] 推送失败: {e}")

        except Exception as e:
            logger.warning(f"[PremarketDebateScheduler] 通知构建失败: {e}")
