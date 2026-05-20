"""
FTSeek-US 复盘编排器 (Review Orchestrator v2.0 — 混合架构)

融合: 日/周/月复盘 + EOD辩论 + 记忆注入 + 统一推送 + 深度上下文

架构升级 (2026-05-05):
  - 统一入口: ReviewOrchestrator 协调所有复盘周期
  - 辩论集成: 每个周期都接入 DebatePlanner (CLI Agent + MemoryInjector)
  - 记忆闭环: 复盘结果 → Lessons DB + us.json
  - 统一推送: 标准化报告格式 (Feishu/DingTalk)
  - ★★★ 混合架构 (2026-05-05):
    - Phase 1: RichContextBuilder — 深度上下文 (历史交易/教训/日志/记忆/决策漏斗)
    - Phase 2: MultiModelDebateRunner — 3 模型并行 HTTP 辩论 (保留现有)
    - Phase 3: ValidationAgent — 辩论结论的代码/日志/DB 证据验证

设计原则:
  - 模块化: Collector / Analyst / Renderer / Notifier 四层分离
  - 低耦合: 纯 dict 数据协议, 不直接依赖实现细节
  - 零破坏: 保留现有 DailyReview/WeeklyReview/EODAttribution
  - 降级链: 辩论失败 → 纯统计报告 → 兜底报告
  - 渐进叠加: Phase 1 为 in-process Python, Phase 3 可升级为 delegate_task

Karpathy BEFORE 检查:
  1. 数据采集: 复用现有 DB 查询和 Review 模块 + 新增深度上下文
  2. 辩论注入: DebatePlanner.enrich_with_memory() + RichContextBuilder 提供上下文
  3. 输出格式: JSON dict + 文本报告 (向后兼容)
"""
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek_us.review.orchestrator")

# P2 修复 (2026-05-03): 路径通过环境变量可配置
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = os.getenv("FTSEEK_DB_PATH", str(PROJECT_ROOT / "data" / "ftseek_us.db"))
DECISIONS_DB = os.getenv("FTSEEK_DECISIONS_DB_PATH", str(PROJECT_ROOT / "data" / "decisions.db"))
MEMORY_FILE = os.getenv("FTSEEK_MEMORY_FILE", str(Path.home() / ".hermes" / "ftseek" / "memory" / "us.json"))


# =====================================================================
#  数据采集层 (Collector)
# =====================================================================

class ReviewDataCollector:
    """
    统一数据采集 — 从所有数据源收集复盘所需数据
    
    低耦合: 纯读取, 零副作用, 输出标准 dict
    """
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
    
    def _query(self, sql: str, params: tuple = (), db_path: str = None) -> List[Dict]:
        target = db_path or self.db_path
        try:
            conn = sqlite3.connect(target)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[Collector] 查询失败 ({Path(target).name}): {e}")
            return []
    
    def collect_daily(self, date: str) -> Dict:
        """采集日级复盘数据 (P0 修复 2026-05-08: 添加 source_type 分类)"""
        trades = self._query(
            "SELECT * FROM trade_records WHERE trade_date = ? ORDER BY created_at",
            (date,)
        )
        closed = [t for t in trades if t.get('exit_price') is not None]
        
        # P0 修复 (2026-05-08): 按来源分类统计
        by_source = {}
        for t in closed:
            src = t.get('source_type', 'MAIN_PIPELINE')
            if src not in by_source:
                by_source[src] = {'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
            by_source[src]['trades'] += 1
            pnl = t.get('realized_pnl', 0) or 0
            by_source[src]['pnl'] += pnl
            if pnl > 0:
                by_source[src]['wins'] += 1
            elif pnl < 0:
                by_source[src]['losses'] += 1
        
        # 计算胜率
        for src in by_source:
            s = by_source[src]
            s['win_rate'] = s['wins'] / s['trades'] if s['trades'] > 0 else 0
            s['avg_pnl'] = s['pnl'] / s['trades'] if s['trades'] > 0 else 0
        
        # 资金状态 (表名: capital_state, 字段: last_update)
        capital = self._query(
            "SELECT * FROM capital_state ORDER BY last_update DESC LIMIT 1"
        )
        capital_state = capital[0] if capital else {}
        
        # 决策漏斗 (在 decisions.db 中)
        total_decisions = 0
        try:
            result = self._query(
                "SELECT COUNT(*) as total FROM decisions WHERE date(created_at) = ?",
                (date,), db_path=DECISIONS_DB
            )
            total_decisions = result[0]['total'] if result else 0
        except (sqlite3.Error, KeyError, IndexError):
            pass
        
        return {
            "report_type": "daily",
            "date": date,
            "trades": trades,
            "closed_trades": closed,
            "by_source": by_source,  # P0 修复: 来源分类统计
            "capital_state": capital_state,
            "total_decisions": total_decisions,
        }
    
    def collect_weekly(self, week_start: str, week_end: str) -> Dict:
        """采集周级复盘数据"""
        trades = self._query(
            "SELECT * FROM trade_records WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (week_start, week_end)
        )
        closed = [t for t in trades if t.get('exit_price') is not None]
        
        # 逐日分布
        daily_counts = self._query(
            "SELECT trade_date, COUNT(*) as cnt, SUM(realized_pnl) as pnl "
            "FROM trade_records WHERE trade_date BETWEEN ? AND ? "
            "GROUP BY trade_date ORDER BY trade_date",
            (week_start, week_end)
        )
        
        return {
            "report_type": "weekly",
            "week_start": week_start,
            "week_end": week_end,
            "trades": trades,
            "closed_trades": closed,
            "daily_distribution": daily_counts,
        }
    
    def collect_monthly(self, year: int, month: int) -> Dict:
        """采集月级复盘数据"""
        start = f"{year}-{month:02d}-01"
        # P0 修复 (2026-05-03): 统一使用下月1日作为 < 边界, 防止12月31日数据丢失
        if month == 12:
            end = f"{year+1}-01-01"
        else:
            end = f"{year}-{month+1:02d}-01"
        
        trades = self._query(
            "SELECT * FROM trade_records WHERE trade_date >= ? AND trade_date < ? ORDER BY trade_date",
            (start, end)
        )
        closed = [t for t in trades if t.get('exit_price') is not None]
        
        return {
            "report_type": "monthly",
            "year": year,
            "month": month,
            "trades": trades,
            "closed_trades": closed,
        }



# =====================================================================
#  深度上下文构建器 (RichContextBuilder) — Phase 1 of Hybrid Architecture
# =====================================================================

class RichContextBuilder:
    """
    构建辩论用的深度上下文 — 超越基础交易数据
    
    混合架构 Phase 1 (2026-05-05):
      - 历史交易模式 (30天趋势, 逐标的表现, 退出原因分布)
      - 活跃教训 (lessons DB 中高 impact_score 条目)
      - 近期异常 (日志 ERROR/WARNING 模式)
      - 记忆上下文 (us.json 中的 known_issues, strategy_adjustments)
      - 决策漏斗深度 (按方向/强度/共识分布)
      - 性能趋势 (逐日胜率/盈亏)
    
    这些上下文让多模型辩论能够:
      - 结合历史教训推理 (避免重复犯错)
      - 识别系统性模式 (而非孤立分析单笔交易)
      - 基于已知问题做假设验证
    """
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self._lock = threading.Lock()
    
    def _query(self, sql: str, params: tuple = (), db_path: str = None) -> List[Dict]:
        target = db_path or self.db_path
        try:
            conn = sqlite3.connect(target)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[RichContext] 查询失败 ({Path(target).name}): {e}")
            return []
    
    def build(self, date: str = None) -> Dict:
        """
        构建完整的深度上下文
        
        Args:
            date: 复盘日期 (默认今天)
        
        Returns:
            {
                "historical_patterns": {...},      # 30天趋势
                "active_lessons": [...],           # 高影响教训
                "recent_anomalies": [...],         # 近期异常
                "memory_context": {...},           # 记忆关键数据
                "decision_funnel": {...},          # 决策漏斗深度
                "performance_trend": [...],        # 逐日表现
                "capital_trajectory": [...],       # 资金轨迹
            }
        """
        target_date = date or datetime.now().strftime("%Y-%m-%d")
        
        return {
            "historical_patterns": self._build_historical_patterns(target_date),
            "active_lessons": self._build_active_lessons(),
            "recent_anomalies": self._build_recent_anomalies(),
            "memory_context": self._build_memory_context(),
            "decision_funnel": self._build_decision_funnel(target_date),
            "performance_trend": self._build_performance_trend(target_date),
            "capital_trajectory": self._build_capital_trajectory(target_date),
        }
    
    def _build_historical_patterns(self, date: str) -> Dict:
        """构建 30 天历史交易模式"""
        # 30 天内的所有已平仓交易
        trades = self._query(
            "SELECT symbol, direction, realized_pnl, pnl_pct, exit_reason, "
            "signal_strength, strategy_id, holding_hours "
            "FROM trade_records WHERE exit_reason IS NOT NULL "
            "AND trade_date >= date(?, '-30 days') AND trade_date <= ? "
            "ORDER BY trade_date DESC",
            (date, date)
        )
        
        if not trades:
            return {"total_trades": 0, "message": "无历史数据"}
        
        total = len(trades)
        wins = sum(1 for t in trades if t.get("realized_pnl", 0) > 0)
        total_pnl = sum(t.get("realized_pnl", 0) or 0 for t in trades)
        
        # 退出原因分布
        exit_reasons = {}
        for t in trades:
            reason = t.get("exit_reason") or "unknown"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        
        # 逐标的表现
        symbol_stats = {}
        for t in trades:
            sym = t.get("symbol", "unknown")
            if sym not in symbol_stats:
                symbol_stats[sym] = {"trades": 0, "wins": 0, "pnl": 0}
            symbol_stats[sym]["trades"] += 1
            if t.get("realized_pnl", 0) > 0:
                symbol_stats[sym]["wins"] += 1
            symbol_stats[sym]["pnl"] += t.get("realized_pnl", 0) or 0
        
        # 胜率最差的标的 (至少 3 笔交易)
        worst_symbols = sorted(
            [(k, v["pnl"], v["wins"]/v["trades"] if v["trades"] > 0 else 0, v["trades"])
             for k, v in symbol_stats.items() if v["trades"] >= 3],
            key=lambda x: x[1]
        )[:5]
        
        return {
            "period_days": 30,
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(wins / total, 3) if total > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "exit_reasons": dict(sorted(exit_reasons.items(), key=lambda x: -x[1])[:10]),
            "worst_symbols": [
                {"symbol": s[0], "pnl": round(s[1], 2), "win_rate": round(s[2], 2), "trades": s[3]}
                for s in worst_symbols
            ],
            "best_symbols": [
                {"symbol": s[0], "pnl": round(s[1], 2), "win_rate": round(s[2], 2), "trades": s[3]}
                for s in sorted(
                    [(k, v["pnl"], v["wins"]/v["trades"] if v["trades"] > 0 else 0, v["trades"])
                     for k, v in symbol_stats.items() if v["trades"] >= 3],
                    key=lambda x: -x[1]
                )[:5]
            ],
        }
    
    def _build_active_lessons(self) -> List[Dict]:
        """提取活跃的高影响教训"""
        lessons = self._query(
            "SELECT lesson_text, lesson_category, impact_score, confidence, "
            "symbols, expiry_date, validation_count "
            "FROM lessons WHERE status = 'active' "
            "AND (expiry_date = '' OR expiry_date >= date('now')) "
            "AND impact_score >= 0.5 "
            "ORDER BY impact_score DESC, confidence DESC "
            "LIMIT 15"
        )
        return lessons
    
    def _build_recent_anomalies(self) -> Dict:
        """分析近期日志异常模式"""
        log_path = PROJECT_ROOT / "logs" / "ftseek_us.log"
        anomalies = {"errors": [], "warnings": [], "patterns": []}
        
        try:
            if log_path.exists():
                # 读取最近 2000 行日志
                with open(log_path, "r") as f:
                    lines = f.readlines()[-2000:]
                
                today = datetime.now().strftime("%Y-%m-%d")
                
                error_patterns = {}
                warning_patterns = {}
                
                for line in lines:
                    if not line.startswith(today):
                        continue
                    if "ERROR" in line or "CRITICAL" in line:
                        # 提取关键部分 (去掉时间戳和模块名)
                        key = line.split("] ", 1)[-1].strip()[:120] if "] " in line else line.strip()[:120]
                        error_patterns[key] = error_patterns.get(key, 0) + 1
                    elif "WARNING" in line:
                        key = line.split("] ", 1)[-1].strip()[:120] if "] " in line else line.strip()[:120]
                        warning_patterns[key] = warning_patterns.get(key, 0) + 1
                
                anomalies["errors"] = [
                    {"pattern": k, "count": v}
                    for k, v in sorted(error_patterns.items(), key=lambda x: -x[1])[:10]
                ]
                anomalies["warnings"] = [
                    {"pattern": k, "count": v}
                    for k, v in sorted(warning_patterns.items(), key=lambda x: -x[1])[:10]
                ]
                anomalies["summary"] = {
                    "total_errors": sum(error_patterns.values()),
                    "total_warnings": sum(warning_patterns.values()),
                    "unique_errors": len(error_patterns),
                    "unique_warnings": len(warning_patterns),
                }
        except Exception as e:
            logger.warning(f"[RichContext] 日志分析失败: {e}")
            anomalies["error"] = str(e)
        
        return anomalies
    
    def _build_memory_context(self) -> Dict:
        """提取记忆文件中的关键上下文"""
        try:
            with open(MEMORY_FILE, "r") as f:
                mem = json.load(f)
            
            return {
                "known_issues": [
                    {"id": i.get("id", ""), "summary": (i.get("summary", ""))[:150]}
                    for i in mem.get("known_issues", [])[:5]
                ],
                "strategy_adjustments": mem.get("strategy_adjustments", [])[:5],
                "core_principles": mem.get("core_principles", [])[:5],
                "performance": mem.get("performance", {}),
                "capital_state": mem.get("capital_state", {}),
            }
        except Exception as e:
            logger.warning(f"[RichContext] 记忆加载失败: {e}")
            return {"error": str(e)}
    
    def _build_decision_funnel(self, date: str) -> Dict:
        """构建决策漏斗深度分析"""
        try:
            # 按方向+强度分布
            by_strength = self._query(
                "SELECT direction, final_strength, COUNT(*) as cnt "
                "FROM decisions WHERE date(created_at) = ? "
                "GROUP BY direction, final_strength ORDER BY cnt DESC",
                (date,), db_path=DECISIONS_DB
            )
            
            # 共识度分布
            by_consensus = self._query(
                "SELECT "
                "  CASE WHEN consensus_level >= 0.8 THEN 'high' "
                "       WHEN consensus_level >= 0.5 THEN 'medium' "
                "       ELSE 'low' END as consensus_bucket, "
                "  COUNT(*) as cnt "
                "FROM decisions WHERE date(created_at) = ? "
                "GROUP BY consensus_bucket",
                (date,), db_path=DECISIONS_DB
            )
            
            # 标准差分布 (分歧度)
            by_stddev = self._query(
                "SELECT "
                "  CASE WHEN std_dev = 0 THEN 'perfect' "
                "       WHEN std_dev <= 0.5 THEN 'low' "
                "       WHEN std_dev <= 1.0 THEN 'medium' "
                "       ELSE 'high' END as stddev_bucket, "
                "  COUNT(*) as cnt "
                "FROM decisions WHERE date(created_at) = ? "
                "GROUP BY stddev_bucket",
                (date,), db_path=DECISIONS_DB
            )
            
            total = self._query(
                "SELECT COUNT(*) as total FROM decisions WHERE date(created_at) = ?",
                (date,), db_path=DECISIONS_DB
            )
            
            return {
                "total_decisions": total[0]["total"] if total else 0,
                "by_direction_strength": by_strength,
                "by_consensus": by_consensus,
                "by_stddev": by_stddev,
            }
        except Exception as e:
            logger.warning(f"[RichContext] 决策漏斗分析失败: {e}")
            return {"error": str(e)}
    
    def _build_performance_trend(self, date: str) -> List[Dict]:
        """构建逐日性能趋势 (最近 14 天)"""
        daily = self._query(
            "SELECT trade_date, COUNT(*) as trades, "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "ROUND(SUM(realized_pnl), 2) as total_pnl, "
            "ROUND(AVG(pnl_pct), 2) as avg_pnl_pct "
            "FROM trade_records WHERE exit_reason IS NOT NULL "
            "AND trade_date >= date(?, '-14 days') AND trade_date <= ? "
            "GROUP BY trade_date ORDER BY trade_date",
            (date, date)
        )
        
        result = []
        for d in daily:
            trades = d["trades"]
            wins = d["wins"]
            result.append({
                "date": d["trade_date"],
                "trades": trades,
                "win_rate": round(wins / trades, 3) if trades > 0 else 0,
                "total_pnl": d["total_pnl"],
                "avg_pnl_pct": d["avg_pnl_pct"],
            })
        return result
    
    def _build_capital_trajectory(self, date: str) -> List[Dict]:
        """构建资金轨迹 (最近 14 次记录)"""
        capital = self._query(
            "SELECT current_equity, peak_equity, drawdown_pct, water_level, "
            "cash, market_value, last_update "
            "FROM capital_state ORDER BY last_update DESC LIMIT 14"
        )
        return list(reversed(capital))  # 旧→新


# =====================================================================
#  多模型辩论器 (Multi-Model Debate Runner)
# =====================================================================

class MultiModelDebateRunner:
    """
    结构化辩论引擎 — 3 轮辩论: 独立分析 → 交叉反驳 → 仲裁收敛
    
    架构升级 (2026-05-05 v2.0 — Level 1: Structured Debate):
      - Round 1: 3 模型独立分析 (原有)
      - Round 2: 交叉反驳 — 每个模型看到其他 2 个观点，必须回应/反驳/修正
      - Round 3: Arbiter — 综合收敛，提取共识/分歧/最终结论
      - 新增收敛度指标: 衡量辩论质量
    
    架构升级 (2026-05-05 v1.0):
      - 替代 claude -p subprocess (失败率 100%: error_max_turns)
      - 使用 agent_bridge._call_llm_direct() 并行 HTTP 调用
      - 3 模型并行: qwen3.6-plus (多头) / deepseek-v4-pro (空头) / MiniMax-M2.5 (审计)
      - 同一 dashscope API, 不同模型, 无需 subprocess
      - 实测: 20s 返回 3 模型结果 (vs subprocess 90s 超时)
    """
    
    # 模型角色配置
    MODEL_ROLES = [
        {"model": "qwen3.6-plus",    "role": "bull",     "label": "多头分析师"},
        {"model": "deepseek-v4-pro", "role": "bear",     "label": "空头审计师"},
        {"model": "MiniMax-M2.5",    "role": "auditor",  "label": "独立风险评估师"},
    ]
    
    # 角色名称映射 (用于 Round 2 prompt)
    ROLE_LABELS = {"bull": "多头分析师", "bear": "空头审计师", "auditor": "独立风险评估师"}
    
    def __init__(self, agent_bridge, max_rounds: int = 2):
        self.agent_bridge = agent_bridge
        self.max_rounds = max_rounds  # 最大辩论轮次 (1=仅独立分析, 2=交叉反驳)
        self._lock = threading.Lock()
    
    def run_debate(self, prompt: str, system_prompt: str, timeout: int = 120) -> Dict:
        """
        结构化辩论: Round 1 (独立) → Round 2 (交叉反驳) → Round 3 (Arbiter)
        
        Returns:
            {
                "mode": "debate" | "partial" | "fallback",
                "rounds": 2,                          # 实际辩论轮次
                "round1_results": {...},              # Round 1 原始结果
                "round2_results": {...},              # Round 2 交叉反驳结果
                "arbiter_conclusion": {...},          # Arbiter 综合结论
                "convergence_metrics": {              # 收敛度指标
                    "view_change_ratio": float,       # Round 2 vs Round 1 观点变化率
                    "consensus_count": int,           # 共识项数量
                    "divergence_count": int,          # 分歧项数量
                },
                "results": {...},                     # 最终结果 (Round 2 或 Round 1 fallback)
                "consensus": [...],
                "divergences": [...],
                "success_count": int,
                "total_models": int,
            }
        """
        # ── Round 1: 独立分析 ──
        round1_results, r1_success = self._run_round(
            prompt, system_prompt, timeout, round_num=1
        )
        
        if r1_success == 0:
            return self._fallback_result("所有模型 Round 1 失败")
        
        logger.info(f"[DebateRunner] Round 1 完成: {r1_success}/{len(self.MODEL_ROLES)} 成功")
        
        # ── Round 2: 交叉反驳 (可选，如果 max_rounds >= 2) ──
        round2_results = {}
        r2_success = 0
        arbiter_conclusion = {}
        
        if self.max_rounds >= 2 and r1_success >= 2:
            round2_results, r2_success = self._run_round2(
                prompt, system_prompt, round1_results, timeout
            )
            logger.info(f"[DebateRunner] Round 2 完成: {r2_success}/{len(self.MODEL_ROLES)} 成功")
            
            # ── Round 3: Arbiter 综合收敛 ──
            if r2_success >= 2:
                arbiter_conclusion = self._run_arbiter(
                    round1_results, round2_results, system_prompt, timeout
                )
                logger.info(f"[DebateRunner] Arbiter 完成: 收敛度 {arbiter_conclusion.get('convergence_score', 'N/A')}")
        
        # 确定最终结果 (优先 Round 2, fallback Round 1)
        final_results = round2_results if r2_success > 0 else round1_results
        final_success = r2_success if r2_success > 0 else r1_success
        
        # 确定模式
        if final_success == 0:
            mode = "fallback"
        elif final_success < len(self.MODEL_ROLES):
            mode = "partial"
        else:
            mode = "debate"
        
        # 分析共识与分歧
        consensus, divergences = self._analyze_convergence(final_results)
        
        # 计算收敛度指标
        convergence = self._compute_convergence_metrics(
            round1_results, final_results, consensus, divergences
        )
        
        return {
            "mode": mode,
            "rounds": 2 if r2_success > 0 else 1,
            "round1_results": round1_results,
            "round2_results": round2_results,
            "arbiter_conclusion": arbiter_conclusion,
            "convergence_metrics": convergence,
            "results": final_results,
            "consensus": consensus,
            "divergences": divergences,
            "success_count": final_success,
            "total_models": len(self.MODEL_ROLES),
        }
    
    def _run_round(self, prompt: str, system_prompt: str, timeout: int, round_num: int) -> tuple:
        """执行一轮并行模型调用"""
        results = {}
        success_count = 0
        
        def call_one(cfg):
            nonlocal success_count
            role_prompt = self._build_role_prompt(system_prompt, cfg["role"], cfg["label"])
            try:
                result = self.agent_bridge._call_llm_direct(
                    prompt=prompt,
                    system_prompt=role_prompt,
                    model=cfg["model"],
                    round_num=f"review_r{round_num}_{cfg['role']}",
                    timeout=timeout,
                )
                if result:
                    with self._lock:
                        results[cfg["role"]] = result
                        results[f"{cfg['role']}_model"] = cfg["model"]
                        success_count += 1
                    logger.info(f"[DebateRunner] R{round_num} {cfg['label']} ({cfg['model']}) 成功")
                else:
                    logger.warning(f"[DebateRunner] R{round_num} {cfg['label']} ({cfg['model']}) 返回空")
            except Exception as e:
                logger.warning(f"[DebateRunner] R{round_num} {cfg['label']} ({cfg['model']}) 失败: {e}")
        
        threads = []
        for cfg in self.MODEL_ROLES:
            t = threading.Thread(target=call_one, args=(cfg,), daemon=True, name=f"debate-r{round_num}-{cfg['role']}")
            t.start()
            threads.append(t)
        
        deadline = time.time() + timeout
        for t in threads:
            remaining = max(0, deadline - time.time())
            if remaining > 0:
                t.join(timeout=remaining)
        
        return results, success_count
    
    def _run_round2(self, original_prompt: str, system_prompt: str, round1_results: Dict, timeout: int) -> tuple:
        """
        Round 2: 交叉反驳
        每个模型看到其他 2 个的观点，必须:
        - 同意哪些点 (共识)
        - 反驳哪些点 (分歧 + 理由)
        - 修正自己的观点 (收敛)
        """
        results = {}
        success_count = 0
        
        def refute_one(cfg):
            nonlocal success_count
            role = cfg["role"]
            
            # 构建其他模型的观点摘要
            others = {}
            for other_cfg in self.MODEL_ROLES:
                if other_cfg["role"] != role:
                    other_result = round1_results.get(other_cfg["role"], {})
                    if isinstance(other_result, dict):
                        summary = {}
                        for key in ["insights", "root_causes", "suggestions", "fixes", "risks"]:
                            val = other_result.get(key)
                            if isinstance(val, list):
                                summary[key] = val[:3]  # 最多 3 条
                            elif isinstance(val, str):
                                summary[key] = [val[:200]]
                        others[self.ROLE_LABELS[other_cfg["role"]]] = summary
            
            # ★★★ Level 2: 构建交叉反驳 prompt — 强制证据驱动反驳
            refute_prompt = f"""{original_prompt}

## Round 2: 交叉反驳 (Level 2: 证据驱动)

其他模型的观点:
{json.dumps(others, ensure_ascii=False, indent=2)}

## 你的任务
1. **回应质疑**: 其他模型对你的观点有哪些质疑？请逐一回应 (必须有证据)
2. **反驳不同意见**: 你不同意其他模型的哪些判断？为什么？(提供数据/逻辑依据)
3. **修正观点**: 基于其他模型的反馈，你是否有需要修正或补充的观点？
4. **达成共识**: 你和哪些模型在哪些点上达成一致？
5. **保留分歧**: 哪些点上你仍然坚持不同意见？为什么？

## 输出格式 (严格 JSON)
{{
  "responses_to_others": [
    {{
      "to_model": "模型名称",
      "response": "回应内容",
      "evidence_type": "db_query|log_pattern|config_check|memory|trend",
      "evidence_hint": "如何验证",
      "confidence": 0.0-1.0
    }}
  ],
  "rebuttals": [
    {{
      "against": "反驳的观点",
      "reason": "反驳理由",
      "evidence_type": "db_query|log_pattern|config_check|memory|trend",
      "evidence_hint": "如何验证",
      "confidence": 0.0-1.0
    }}
  ],
  "revised_insights": [
    {{
      "claim": "修正后的洞察",
      "evidence_type": "db_query|log_pattern|config_check|memory|trend",
      "evidence_hint": "如何验证",
      "confidence": 0.0-1.0,
      "changed_from_round1": "与 Round 1 的变化"
    }}
  ],
  "consensus_points": ["达成共识的点 1"],
  "remaining_disagreements": ["保留分歧的点 1"],
  "final_confidence": 0.0-1.0,
  "key_changes_from_round1": ["变化 1", "变化 2"]
}}

⚠️ 禁止空谈: 每个回应/反驳必须有 evidence_type 和 evidence_hint
⚠️ 置信度标准: 0.9=有直接数据支撑, 0.7=有间接证据, 0.5=推理推测"""
            
            role_prompt = self._build_role_prompt(system_prompt, role, cfg["label"])
            try:
                result = self.agent_bridge._call_llm_direct(
                    prompt=refute_prompt,
                    system_prompt=role_prompt,
                    model=cfg["model"],
                    round_num=f"review_r2_{role}",
                    timeout=timeout,
                )
                if result:
                    with self._lock:
                        results[role] = result
                        results[f"{role}_model"] = cfg["model"]
                        success_count += 1
                    logger.info(f"[DebateRunner] R2 {cfg['label']} 交叉反驳成功")
            except Exception as e:
                logger.warning(f"[DebateRunner] R2 {cfg['label']} 交叉反驳失败: {e}")
        
        threads = []
        for cfg in self.MODEL_ROLES:
            t = threading.Thread(target=refute_one, args=(cfg,), daemon=True, name=f"debate-r2-{cfg['role']}")
            t.start()
            threads.append(t)
        
        deadline = time.time() + timeout
        for t in threads:
            remaining = max(0, deadline - time.time())
            if remaining > 0:
                t.join(timeout=remaining)
        
        return results, success_count
    
    def _run_arbiter(self, round1_results: Dict, round2_results: Dict, system_prompt: str, timeout: int) -> Dict:
        """
        Round 3: Arbiter 综合收敛
        读取 Round 1 + Round 2 的所有结果，综合给出最终结论
        """
        # 提取所有观点
        all_views = {}
        for round_data, round_label in [(round1_results, "Round 1"), (round2_results, "Round 2")]:
            for role in ["bull", "bear", "auditor"]:
                r = round_data.get(role, {})
                if isinstance(r, dict):
                    key = f"{self.ROLE_LABELS[role]} ({round_label})"
                    view = {}
                    for k in ["insights", "root_causes", "suggestions", "fixes", "risks",
                             "responses_to_others", "rebuttals", "revised_insights",
                             "consensus_points", "remaining_disagreements"]:
                        if k in r:
                            view[k] = r[k]
                    all_views[key] = view
        
        # ★★★ Level 2: Arbiter prompt — 评估证据质量
        arbiter_prompt = f"""你是 FTSeek-US 辩论仲裁者。请综合以下 3 个模型在 2 轮辩论中的观点，给出最终结论。

## 所有观点
{json.dumps(all_views, ensure_ascii=False, indent=2)[:3000]}

## 你的任务 (Level 2: 证据质量评估)
1. **共识提取**: 提取 2+ 模型都认同的观点 (必须评估证据质量)
2. **分歧分析**: 分析分歧点的根本原因 (数据差异？视角不同？假设不同？)
3. **证据评估**: 对每个观点的证据质量进行评估:
   - excellent: 有直接 DB/日志数据支撑
   - good: 有间接证据或强逻辑推理
   - weak: 仅有推理或猜测
4. **最终结论**: 综合给出 3-5 条最关键的结论 (按重要性排序)
5. **修复建议**: 给出 2-3 条具体的、可执行的修复建议
6. **置信度评估**: 综合模型置信度和证据质量给出最终置信度

## 输出格式 (严格 JSON)
{{
  "consensus": [
    {{
      "point": "共识观点",
      "supported_by": ["多头", "空头"],
      "evidence_quality": "excellent|good|weak",
      "evidence_summary": "证据摘要",
      "confidence": 0.0-1.0
    }}
  ],
  "divergences": [
    {{
      "point": "分歧观点",
      "bull_view": "...",
      "bear_view": "...",
      "root_cause": "分歧原因 (数据/视角/假设)",
      "resolution_hint": "如何验证谁对谁错"
    }}
  ],
  "final_conclusions": [
    {{
      "conclusion": "结论",
      "priority": "P0/P1/P2",
      "evidence_quality": "excellent|good|weak",
      "confidence": 0.0-1.0,
      "reasoning": "推理链"
    }}
  ],
  "actionable_fixes": [
    {{
      "fix": "修复建议",
      "target": "config|code|process|data",
      "effort": "low|medium|high",
      "expected_impact": "预期效果",
      "confidence": 0.0-1.0
    }}
  ],
  "convergence_score": 0.0-1.0,
  "debate_quality": "high|medium|low",
  "evidence_summary": {{
    "excellent_count": 0,
    "good_count": 0,
    "weak_count": 0,
    "overall_quality": "excellent|good|weak"
  }}
}}"""
        
        try:
            result = self.agent_bridge._call_llm_direct(
                prompt=arbiter_prompt,
                system_prompt="你是 FTSeek-US 辩论仲裁者。中立、客观、基于证据。输出严格 JSON。",
                model="qwen3.6-plus",
                round_num="review_arbiter",
                timeout=timeout,
            )
            if result:
                logger.info("[DebateRunner] Arbiter 综合收敛成功")
                return result
            else:
                logger.warning("[DebateRunner] Arbiter 返回空")
                return {}
        except Exception as e:
            logger.warning(f"[DebateRunner] Arbiter 失败: {e}")
            return {}
    
    def _fallback_result(self, reason: str) -> Dict:
        """辩论失败时的降级结果"""
        return {
            "mode": "fallback",
            "rounds": 0,
            "round1_results": {},
            "round2_results": {},
            "arbiter_conclusion": {},
            "convergence_metrics": {"view_change_ratio": 0, "consensus_count": 0, "divergence_count": 0},
            "results": {},
            "consensus": [],
            "divergences": [],
            "success_count": 0,
            "total_models": len(self.MODEL_ROLES),
            "error": reason,
        }
    
    def _build_role_prompt(self, base_system: str, role: str, label: str) -> str:
        """构建角色特定的 system prompt — Level 2: 强制证据驱动输出"""
        # ★★★ Level 2: 结构化证据输出要求
        evidence_format = """

## 证据驱动输出要求 (Level 2)
每个洞察/建议/风险必须附带证据:
{
  "insights": [
    {
      "claim": "观点描述",
      "evidence_type": "db_query|log_pattern|config_check|memory|trend",
      "evidence_hint": "如何验证 (SQL关键词/日志关键词/配置路径)",
      "confidence": 0.0-1.0,
      "alternative_explanation": "其他可能的解释 (可选)"
    }
  ],
  "root_causes": [...],
  "fixes": [
    {
      "claim": "修复建议",
      "target": "config|code|process|data",
      "effort": "low|medium|high",
      "confidence": 0.0-1.0
    }
  ],
  "risks": [...]
}

⚠️ 禁止空谈: 每个 claim 必须有 evidence_type 和 evidence_hint
⚠️ 置信度标准: 0.9=有直接数据支撑, 0.7=有间接证据, 0.5=推理推测, <0.5=猜测"""
        
        role_addons = {
            "bull": f"""**你的角色**: 多头分析师。识别交易中的积极信号和机会。
找出被低估的机会和有效的策略。{evidence_format}""",
            "bear": f"""**你的角色**: 空头审计师（红队）。从风险、挑战和系统漏洞角度审查。
找出被忽视的问题和潜在的系统性风险。{evidence_format}""",
            "auditor": f"""**你的角色**: 独立风险评估师。中立审查所有数据，评估策略有效性和系统性风险。
提供客观的第三方视角。{evidence_format}""",
        }
        return base_system + role_addons.get(role, "")
    
    def _analyze_convergence(self, results: Dict) -> tuple:
        """分析多模型结果的共识与分歧"""
        consensus = []
        divergences = []
        
        # 提取各模型的核心洞察
        insights = {}
        for role in ["bull", "bear", "auditor"]:
            r = results.get(role, {})
            if isinstance(r, dict):
                for key in ["insights", "root_causes", "suggestions", "fixes", "risks"]:
                    val = r.get(key)
                    if isinstance(val, list):
                        insights.setdefault(key, {})[role] = val
                    elif isinstance(val, str):
                        insights.setdefault(key, {})[role] = [val]
        
        # 简单共识分析: 如果 2+ 模型提到相似内容
        for key, role_data in insights.items():
            all_items = []
            for role, items in role_data.items():
                all_items.extend([(item, role) for item in items])
            
            # 统计出现次数 (基于关键词相似度)
            from collections import Counter
            keywords = []
            for item, _ in all_items:
                words = str(item).lower()
                keywords.append(words)
            
            # 简化: 如果某个关键词在 2+ 角色中出现
            role_sets = {role: set(str(x).lower() for x in items) for role, items in role_data.items()}
            all_keys = set()
            for s in role_sets.values():
                all_keys |= s
            
            for k in all_keys:
                roles_mentioned = [r for r, s in role_sets.items() if any(k in x or x in k for x in s)]
                if len(roles_mentioned) >= 2:
                    consensus.append({"topic": k[:100], "roles": roles_mentioned})
                elif len(role_sets) > 1:
                    divergences.append({"topic": k[:100], "only_in": roles_mentioned})
        
        return consensus, divergences
    
    def _compute_convergence_metrics(self, round1_results: Dict, final_results: Dict, consensus: list, divergences: list) -> Dict:
        """
        计算辩论收敛度指标
        
        衡量辩论质量:
        - view_change_ratio: Round 2 vs Round 1 观点变化率
        - consensus_count: 共识项数量
        - divergence_count: 分歧项数量
        - convergence_score: 综合收敛度 (0-1)
        """
        # 计算观点变化率
        r1_insights = set()
        r2_insights = set()
        
        for role in ["bull", "bear", "auditor"]:
            r1 = round1_results.get(role, {})
            r2 = final_results.get(role, {})
            
            for data, target in [(r1, r1_insights), (r2, r2_insights)]:
                if isinstance(data, dict):
                    for key in ["insights", "root_causes", "suggestions", "fixes", "risks",
                               "revised_insights", "consensus_points"]:
                        val = data.get(key)
                        if isinstance(val, list):
                            for item in val:
                                if isinstance(item, str):
                                    target.add(item[:100])  # 截断避免过长
            
        total_r1 = len(r1_insights) if r1_insights else 1
        changed = len(r2_insights.difference(r1_insights)) if r2_insights else 0
        view_change_ratio = min(changed / total_r1, 1.0)
        
        # 综合收敛度
        total_items = len(consensus) + len(divergences)
        consensus_ratio = len(consensus) / total_items if total_items > 0 else 0.5
        
        # 收敛度 = 共识占比 × 0.6 + 观点变化率 × 0.4 (变化率太高说明没收敛，太低说明没交锋)
        optimal_change = 0.3  # 理想变化率 30% (有交锋但不完全推翻)
        change_score = 1.0 - abs(view_change_ratio - optimal_change) / optimal_change
        change_score = max(0, min(1, change_score))
        
        convergence_score = round(consensus_ratio * 0.6 + change_score * 0.4, 2)
        
        return {
            "view_change_ratio": round(view_change_ratio, 2),
            "consensus_count": len(consensus),
            "divergence_count": len(divergences),
            "convergence_score": convergence_score,
            "debate_quality": "high" if convergence_score > 0.7 else "medium" if convergence_score > 0.4 else "low",
        }

# =====================================================================
#  辩论分析层 (Analyst)
# =====================================================================



# =====================================================================
#  辩论 Issue 提取器 (Debate Issue Extractor)
# =====================================================================

class DebateIssueExtractor:
    """
    将多模型辩论结果 (JSON) 转为 DetectedIssue 对象, 注入 review dict
    使 IssueDetector 能检测 LLM 提取的 Issue, ActionExecutor 能自动修复
    
    架构升级 (2026-05-05):
      - 桥接 LLM 辩论 (自由文本) → IssueDetector (结构化 Issue)
      - 解析 3 模型的 {root_causes, fixes, risks, strategy_adjustments}
      - 转为 DetectedIssue(category='llm_debate', severity='P0/P1/P2')
      - 注入 review['llm_issues'] 供 IssueDetector 检测
    """
    
    # LLM 建议到 Issue 类别的映射
    KEYWORD_CATEGORY_MAP = {
        # 信号/策略质量
        ("信号", "策略", "voting_llm", "胜率", "期望值", "模型", "入场", "阈值"): "signal_quality",
        # 风控
        ("止损", "回撤", "风控", "仓位", "资金", "破产"): "risk",
        # 执行
        ("滑点", "重复", "ghost", "幽灵", "持仓", "对账", "同步"): "execution",
        # 漏斗/过滤
        ("过滤", "漏斗", "波动率", "趋势", "ATR", "成交量", "VIX"): "funnel",
        # 数据完整性
        ("快照", "延迟", "数据", "API", "心跳", "Futu"): "data_integrity",
    }
    
    # LLM 建议到严重程度的映射
    SEVERITY_KEYWORDS = {
        "P0": ["暂停", "立即", "禁止", "停止", "critical", "fatal", "重复开平", "ghost", "幽灵持仓", "数据漂移"],
        "P1": ["收紧", "提高", "修复", "增加", "优化", "重要", "建议"],
        "P2": ["关注", "观察", "考虑", "建议关注"],
    }
    
    def __init__(self):
        self._lock = threading.Lock()
    
    def extract(self, debate_results: Dict, trade_data: Dict = None) -> List[Dict]:
        """
        从辩论结果提取结构化 Issue
        
        Args:
            debate_results: {"bull": {...}, "bear": {...}, "auditor": {...}}
            trade_data: 当日交易数据 (用于严重性判断)
        
        Returns:
            List[Dict] — 每条符合 DetectedIssue 协议
        """
        issues = []
        
        for role, result in debate_results.items():
            if role.endswith("_model") or not isinstance(result, dict):
                continue
            
            # ★★★ 新增: 提取 ChangeProposal 结构化提案
            proposals = result.get("proposals", [])
            if isinstance(proposals, list):
                for p in proposals:
                    if isinstance(p, dict):
                        issue = {
                            "issue_id": f"llm_proposal_{p.get('id', hash(str(p)) % 10000)}_{datetime.now().strftime('%Y%m%d')}",
                            "category": f"llm_debate_proposal",
                            "title": f"[{p.get('component', 'unknown')}] {p.get('change_type', 'change')}",
                            "description": p.get("rationale", ""),
                            "severity": self._assess_severity(p.get("rationale", ""), trade_data),
                            "evidence": {
                                "source_model": result.get("source_model", "unknown"), 
                                "role": role,
                                "proposal": p  # 完整提案供 ActionExecutor 使用
                            },
                            "suggested_action": json.dumps(p, ensure_ascii=False),
                            "auto_fix_available": p.get("change_type") == "config_tweak",
                            "detected_at": datetime.now().isoformat(),
                        }
                        issues.append(issue)
            
            # 提取各类建议/洞察
            for key in ["root_causes", "insights", "core_insights", "fixes", 
                        "suggestions", "strategy_adjustments", "risks", "risk_warnings"]:
                items = result.get(key, [])
                if isinstance(items, str):
                    items = [items]
                if not isinstance(items, list):
                    continue
                
                for item in items:
                    if not isinstance(item, str) or len(item) < 10:
                        continue
                    
                    category = self._classify(item)
                    severity = self._assess_severity(item, trade_data)
                    
                    issue = {
                        "issue_id": f"llm_{role}_{category}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hash(item) % 10000:04d}",
                        "category": f"llm_debate_{category}",
                        "title": item[:80],
                        "description": item,
                        "severity": severity,
                        "evidence": {"source_model": result.get("source_model", "unknown"), "role": role},
                        "suggested_action": item,
                        "auto_fix_available": severity in ("P0", "P1"),
                        "detected_at": datetime.now().isoformat(),
                    }
                    issues.append(issue)
        
        logger.info(f"[DebateExtractor] 提取 {len(issues)} 个 LLM Issue")
        return issues
    
    def _classify(self, text: str) -> str:
        """基于关键词分类 Issue 类别"""
        text_lower = text.lower()
        for keywords, category in self.KEYWORD_CATEGORY_MAP.items():
            if any(kw.lower() in text_lower for kw in keywords):
                return category
        return "general"
    
    def _assess_severity(self, text: str, trade_data: Dict = None) -> str:
        """评估 Issue 严重程度"""
        text_lower = text.lower()
        for severity, keywords in self.SEVERITY_KEYWORDS.items():
            if any(kw.lower() in text_lower for kw in keywords):
                return severity
        return "P2"


class ReviewAnalyst:
    """
    辩论分析 — 将采集的数据注入 DebatePlanner, 运行双 Agent 辩论
    
    混合架构升级 (2026-05-05):
      - 集成 RichContextBuilder 提供深度上下文 (历史/教训/异常/记忆)
      - 辩论 prompt 包含丰富的历史模式和已知问题
      - 低耦合: 仅依赖 DebatePlanner 和 MemoryInjector 的公共接口
    """
    
    def __init__(self, agent_bridge=None):
        self.agent_bridge = agent_bridge
        self._debate_planner = None
        self._rich_context = RichContextBuilder()
        self._lock = threading.Lock()
    
    @property
    def debate_planner(self):
        if self._debate_planner is None:
            from app.agents.debate.debate_planner import get_debate_planner
            self._debate_planner = get_debate_planner(agent_bridge=self.agent_bridge)
        return self._debate_planner
    
    def analyze(self, data: Dict) -> Dict:
        """
        运行辩论分析
        
        Args:
            data: Collector 输出的标准 dict
        
        Returns:
            {
                "debate_result": dict,  # 辩论收敛结果
                "mode": str,            # "debate" / "hermes_only" / "cli_only" / "fallback"
                "lessons_extracted": int,
            }
        """
        report_type = data.get("report_type", "daily")
        
        # 构建辩论上下文
        context = self._build_debate_context(data)
        
        # 通过 DebatePlanner 运行
        # 注意: 这里复用 DebatePlanner 的记忆注入和辩论流程
        # 但不走完整的 generate_premarket_plan (那是盘前的)
        enriched = self.debate_planner.enrich_with_memory(context)
        
        logger.info(
            f"[Analyst] {report_type} 辩论上下文构建完成 | "
            f"memory={len(enriched.get('memory_section', ''))} chars"
        )
        
        # 构建分析 prompt (根据报告类型)
        prompt = self._build_analysis_prompt(data, enriched)
        system_prompt = self._build_system_prompt(report_type)
        
        # 运行双 Agent 辩论
        result = self._run_debate(prompt, system_prompt, context)
        
        return result
    
    def _build_debate_context(self, data: Dict) -> Dict:
        """构建辩论上下文 — 混合架构: 基础数据 + 深度上下文"""
        trades = data.get("closed_trades", [])
        total_pnl = sum(t.get("realized_pnl", 0) or 0 for t in trades)
        wins = sum(1 for t in trades if (t.get("realized_pnl", 0) or 0) > 0)
        win_rate = wins / len(trades) if trades else 0
        
        capital = data.get("capital_state", {})
        date = data.get("date", data.get("week_end", ""))
        
        # ★★★ Phase 1: 构建深度上下文
        rich_ctx = self._rich_context.build(date) if date else {}
        
        return {
            "report_type": data.get("report_type", "daily"),
            "date": date,
            "trades_summary": {
                "total": len(trades),
                "wins": wins,
                "losses": len(trades) - wins,
                "win_rate": round(win_rate, 4),
                "total_pnl": round(total_pnl, 2),
            },
            "trades_detail": json.dumps(trades[:10], ensure_ascii=False, indent=2),
            "capital": {
                "equity": capital.get("current_equity", "N/A"),
                "drawdown": capital.get("drawdown_pct", "N/A"),
            },
            # ★★★ 混合架构: 注入深度上下文
            "rich_context": rich_ctx,
        }
    
    def _build_analysis_prompt(self, data: Dict, enriched: Dict) -> str:
        """构建分析 prompt — 混合架构: 注入深度上下文"""
        report_type = data.get("report_type", "daily")
        trades = data.get("closed_trades", [])
        rich_ctx = data.get("rich_context", {})
        
        lines = [
            f"你是 FTSeek-US {report_type}复盘分析师。请对以下数据进行深度分析。",
            "",
            enriched.get("memory_section", ""),
            "",
            # ★★★ Phase 1: 深度上下文注入
            "【历史交易模式 (30天)】",
            json.dumps(rich_ctx.get("historical_patterns", {}), ensure_ascii=False, indent=2),
            "",
            "【活跃教训 (高影响)】",
            json.dumps(rich_ctx.get("active_lessons", [])[:5], ensure_ascii=False, indent=2),
            "",
            "【记忆上下文】",
            json.dumps({
                "known_issues": rich_ctx.get("memory_context", {}).get("known_issues", [])[:3],
                "strategy_adjustments": rich_ctx.get("memory_context", {}).get("strategy_adjustments", [])[:3],
            }, ensure_ascii=False, indent=2),
            "",
            "【近期异常】",
            json.dumps(rich_ctx.get("recent_anomalies", {}).get("summary", {}), ensure_ascii=False, indent=2),
            "",
            "【性能趋势 (14天)】",
            json.dumps(rich_ctx.get("performance_trend", [])[-7:], ensure_ascii=False, indent=2),
            "",
            f"【今日交易数据】共 {len(trades)} 笔:",
            json.dumps([
                {
                    "symbol": t.get("symbol"),
                    "direction": t.get("direction"),
                    "pnl": t.get("realized_pnl"),
                    "pnl_pct": t.get("pnl_pct"),
                    "exit_reason": t.get("exit_reason"),
                }
                for t in trades[:10]
            ], ensure_ascii=False, indent=2),
            "",
            "【分析要求】",
            "1. 结合历史交易模式, 识别系统性问题 (不是单笔交易的表面原因)",
            "2. 对比活跃教训, 判断今日是否重复犯错",
            "3. 结合已知问题 (known_issues), 验证问题是否仍在恶化",
            "4. 根据性能趋势, 判断策略是否在退化",
            "5. 给出 2-3 条具体的策略调整建议 (必须有代码/配置变更)",
            "6. 指出需要重点关注的标的和风险点",
            "7. 输出严格 JSON: {\"insights\": [...], \"root_causes\": [...], \"fixes\": [...], \"warnings\": [...], \"next_focus\": [...]}",
        ]
        
        return "\n".join(lines)
    
    def _build_system_prompt(self, report_type: str) -> str:
        """构建系统 prompt — 增加 Karpathy 原则与 SSOT 强制约束"""
        role_map = {
            "daily": "盘后复盘分析师",
            "weekly": "周度复盘分析师",
            "monthly": "月度复盘分析师",
        }
        return (
            f"你是 FTSeek-US {role_map.get(report_type, '复盘分析师')}。核心目标：稳定赚钱。\n"
            "【强制规范】\n"
            "1. Karpathy 原则：严禁过度设计，只改必要参数，修改前必须验证影响面。\n"
            "2. 编码 SSOT：所有参数变更必须指向 `config/unified_config.yaml`，严禁在代码中硬编码。\n"
            "3. 结构化输出：必须在 `proposals` 字段输出 `ChangeProposal` JSON，格式见下方。\n"
            "4. 验证闭环：每个变更必须包含 `expected_impact` 和 `verification_method`。\n"
            "【ChangeProposal 格式】\n"
            "{\n"
            "  \"id\": \"PROP-{date}-{seq}\",\n"
            "  \"component\": \"组件名\",\n"
            "  \"change_type\": \"config_tweak | code_fix | strategy_change\",\n"
            "  \"config_path\": \"unified_config.yaml 中的 Key 路径\",\n"
            "  \"current_value\": \"当前值\",\n"
            "  \"target_value\": \"目标值\",\n"
            "  \"expected_impact\": \"预期效果指标\",\n"
            "  \"verification_method\": \"如何验证（对比 X 天数据）\",\n"
            "  \"rationale\": \"变更依据\"\n"
            "}\n"
            "输出必须包含：{\"insights\": [], \"root_causes\": [], \"proposals\": [ChangeProposal], \"warnings\": []}"
        )
    
    def _run_debate(self, prompt: str, system_prompt: str, context: Dict) -> Dict:
        """运行多模型辩论分析 (v3: 结构化辩论 — 独立分析 → 交叉反驳 → 仲裁收敛)"""
        result = {
            "debate_result": None,
            "mode": "fallback",
            "lessons_extracted": 0,
        }
        
        if not self.agent_bridge:
            logger.warning("[Analyst] agent_bridge 不可用, 跳过辩论")
            return result
        
        # ★★★ Level 1: 结构化辩论 (max_rounds=2 启用交叉反驳)
        runner = MultiModelDebateRunner(self.agent_bridge, max_rounds=2)
        debate_result = runner.run_debate(prompt, system_prompt, timeout=120)
        
        result["debate_result"] = debate_result["results"]
        result["mode"] = debate_result["mode"]
        result["success_count"] = debate_result["success_count"]
        result["total_models"] = debate_result["total_models"]
        result["consensus"] = debate_result["consensus"]
        result["divergences"] = debate_result["divergences"]
        # ★★★ 结构化辩论新增字段
        result["rounds"] = debate_result.get("rounds", 1)
        result["arbiter_conclusion"] = debate_result.get("arbiter_conclusion", {})
        result["convergence_metrics"] = debate_result.get("convergence_metrics", {})
        result["round1_results"] = debate_result.get("round1_results", {})
        result["round2_results"] = debate_result.get("round2_results", {})
        
        metrics = debate_result.get("convergence_metrics", {})
        logger.info(
            f"[Analyst] 结构化辩论完成: {debate_result['rounds']} 轮 | "
            f"mode={debate_result['mode']} | "
            f"收敛度={metrics.get('convergence_score', 'N/A')} | "
            f"质量={metrics.get('debate_quality', 'N/A')} | "
            f"({debate_result['success_count']}/{debate_result['total_models']} 成功)"
        )
        
        return result


# =====================================================================
#  报告渲染层 (Renderer)
# =====================================================================

class ReviewRenderer:
    """
    报告渲染 — 将辩论结果格式化为 Feishu/DingTalk 通知
    
    低耦合: 输入 dict, 输出文本
    """
    
    @staticmethod
    def render_daily(data: Dict, analysis: Dict) -> str:
        """渲染日报 — 支持结构化提案的人类可读展示"""
        trades = data.get("closed_trades", [])
        total_pnl = sum(t.get("realized_pnl", 0) or 0 for t in trades)
        wins = sum(1 for t in trades if (t.get("realized_pnl", 0) or 0) > 0)
        win_rate = wins / len(trades) if trades else 0
        
        lines = [
            f"📊 FTSeek-US 盘后复盘报告 ({data.get('date', '今日')})",
            f"{'='*40}",
            f"📈 交易数: {len(trades)} | 胜率: {win_rate:.0%} | 盈亏: ${total_pnl:.0f}",
            f"🤖 分析模式: {analysis.get('mode', 'N/A')}",
        ]
        
        # 辩论洞察 (支持从仲裁结论提取)
        debate = analysis.get("debate_result", {})
        arbiter = analysis.get("arbiter_conclusion", {})
        final_result = arbiter if arbiter else debate
        
        if isinstance(final_result, dict):
            insights = final_result.get("insights", debate.get("insights", []))
            if insights:
                lines.append("\n💡 核心洞察:")
                for i, insight in enumerate(insights[:3], 1):
                    lines.append(f"  {i}. {insight}")
            
            adjustments = final_result.get("adjustments", debate.get("adjustments", []))
            if adjustments:
                lines.append("\n🔧 策略调整:")
                for i, adj in enumerate(adjustments[:3], 1):
                    lines.append(f"  {i}. {adj}")
            
            # ★★★ 结构化变更提案 (人类可读展示)
            proposals = final_result.get("proposals", [])
            if proposals:
                lines.append("\n📝 优化提案:")
                for i, p in enumerate(proposals[:3], 1):
                    if isinstance(p, dict):
                        comp = p.get("component", "未知")
                        change = p.get("change_type", "调整")
                        impact = p.get("expected_impact", "")
                        lines.append(f"  {i}. [{comp}] {change}")
                        if impact:
                            lines.append(f"     → 预期: {impact}")
                    else:
                        lines.append(f"  {i}. {str(p)[:100]}")
            
            warnings = final_result.get("warnings", debate.get("warnings", []))
            if warnings:
                lines.append("\n⚠️  风险警告:")
                for i, w in enumerate(warnings[:3], 1):
                    lines.append(f"  {i}. {w}")
        
        # CLI 审计补充
        cli = analysis.get("cli_audit", {})
        if cli:
            cli_insights = cli.get("insights", [])
            if cli_insights:
                lines.append("\n🔍 空头审计补充:")
                for i, ci in enumerate(cli_insights[:2], 1):
                    lines.append(f"  {i}. {ci}")
        
        return "\n".join(lines)
    
    @staticmethod
    def render_weekly(data: Dict, analysis: Dict) -> str:
        """渲染周报"""
        trades = data.get("closed_trades", [])
        total_pnl = sum(t.get("realized_pnl", 0) or 0 for t in trades)
        wins = sum(1 for t in trades if (t.get("realized_pnl", 0) or 0) > 0)
        win_rate = wins / len(trades) if trades else 0  # P0: 防止 ZeroDivisionError
        
        lines = [
            f"📊 FTSeek-US 周报 ({data.get('week_start', '')} ~ {data.get('week_end', '')})",
            f"{'='*40}",
            f"📈 本周交易: {len(trades)} | 胜率: {win_rate:.0%} | 盈亏: ${total_pnl:.0f}",
            f"🤖 分析模式: {analysis.get('mode', 'N/A')}",
        ]
        
        debate = analysis.get("debate_result", {})
        if isinstance(debate, dict):
            insights = debate.get("insights", [])
            if insights:
                lines.append("\n💡 周度洞察:")
                for i, insight in enumerate(insights[:3], 1):
                    lines.append(f"  {i}. {insight}")
        
        return "\n".join(lines)
    
    @staticmethod
    def render_monthly(data: Dict, analysis: Dict) -> str:
        """渲染月报"""
        trades = data.get("closed_trades", [])
        total_pnl = sum(t.get("realized_pnl", 0) or 0 for t in trades)
        wins = sum(1 for t in trades if (t.get("realized_pnl", 0) or 0) > 0)
        win_rate = wins / len(trades) if trades else 0  # P0: 防止 ZeroDivisionError
        
        lines = [
            f"📊 FTSeek-US 月报 ({data.get('year', '')}年{data.get('month', '')}月)",
            f"{'='*40}",
            f"📈 本月交易: {len(trades)} | 胜率: {win_rate:.0%} | 盈亏: ${total_pnl:.0f}",
        ]
        
        return "\n".join(lines)


# =====================================================================
#  编排器 (Orchestrator)
# =====================================================================

class ReviewOrchestrator:
    """
    复盘编排器 — 统一入口, 协调所有复盘周期
    
    使用方式:
        orchestrator = ReviewOrchestrator(notifier, agent_bridge)
        
        # 日复盘
        orchestrator.run_daily("2026-05-03")
        
        # 周复盘
        orchestrator.run_weekly("2026-05-01", "2026-05-05")
        
        # 月复盘
        orchestrator.run_monthly(2026, 4)
    """
    
    def __init__(self, notifier=None, agent_bridge=None):
        self.notifier = notifier
        self.agent_bridge = agent_bridge
        self.collector = ReviewDataCollector()
        self.analyst = ReviewAnalyst(agent_bridge=agent_bridge)
        self.issue_extractor = DebateIssueExtractor()
        self.validator = ValidationAgent()  # ★★★ Phase 3
        self.renderer = ReviewRenderer()
        self._lock = threading.Lock()
    
    def run_daily(self, date: str = None) -> Dict:
        """运行日复盘 — 混合架构: Phase 1+2+3"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        logger.info(f"[Orchestrator] 开始日复盘: {date}")
        
        # 1. 数据采集 (基础)
        data = self.collector.collect_daily(date)
        logger.info(f"[Orchestrator] 采集完成: {len(data['closed_trades'])} 笔交易")
        
        # 2. 辩论分析 (Phase 1+2: RichContext + Multi-Model Debate)
        analysis = self.analyst.analyze(data)
        
        # ★★★ Phase 3: 验证辩论结论
        validation_result = None
        debate_results = analysis.get("debate_result", {})
        if isinstance(debate_results, dict) and debate_results:
            logger.info("[Orchestrator] 开始 Phase 3 验证辩论结论")
            validation_result = self.validator.validate_debate_results(debate_results, date)
            logger.info(
                f"[Orchestrator] 验证完成: {validation_result.get('validation_summary', 'N/A')}"
            )
        
        # 3. 渲染报告
        report_text = self.renderer.render_daily(data, analysis)
        
        # 4. 推送通知
        if self.notifier and report_text:
            try:
                self.notifier.send_text(report_text)
                logger.info("[Orchestrator] 日报已推送")
            except Exception as e:
                logger.error(f"[Orchestrator] 推送失败: {e}")
        
        # 5. 提取教训 (复用 EOD 的逻辑)
        lessons_count = self._extract_lessons(analysis, data)
        
        # 从辩论结果提取结构化 Issue
        llm_issues = []
        if isinstance(debate_results, dict):
            llm_issues = self.issue_extractor.extract(debate_results, data)
        
        return {
            "date": date,
            "type": "daily",
            "trades": len(data["closed_trades"]),
            "analysis_mode": analysis.get("mode"),
            "lessons_extracted": lessons_count,
            "report_text": report_text,
            "llm_issues": llm_issues,
            # ★★★ 混合架构: 验证结果
            "validation_result": validation_result,
        }
    
    def run_weekly(self, week_start: str, week_end: str) -> Dict:
        """运行周复盘"""
        logger.info(f"[Orchestrator] 开始周复盘: {week_start} ~ {week_end}")
        
        data = self.collector.collect_weekly(week_start, week_end)
        analysis = self.analyst.analyze(data)
        report_text = self.renderer.render_weekly(data, analysis)
        
        if self.notifier and report_text:
            try:
                self.notifier.send_text(report_text)
                logger.info("[Orchestrator] 周报已推送")
            except Exception as e:
                logger.error(f"[Orchestrator] 推送失败: {e}")
        
        return {
            "week_start": week_start,
            "week_end": week_end,
            "type": "weekly",
            "trades": len(data["closed_trades"]),
            "analysis_mode": analysis.get("mode"),
        }
    
    def run_monthly(self, year: int, month: int) -> Dict:
        """运行月复盘"""
        logger.info(f"[Orchestrator] 开始月复盘: {year}-{month}")
        
        data = self.collector.collect_monthly(year, month)
        analysis = self.analyst.analyze(data)
        report_text = self.renderer.render_monthly(data, analysis)
        
        if self.notifier and report_text:
            try:
                self.notifier.send_text(report_text)
                logger.info("[Orchestrator] 月报已推送")
            except Exception as e:
                logger.error(f"[Orchestrator] 推送失败: {e}")
        
        return {
            "year": year,
            "month": month,
            "type": "monthly",
            "trades": len(data["closed_trades"]),
        }
    
    def _extract_lessons(self, analysis: Dict, data: Dict) -> int:
        """从分析结果提取教训 → lessons DB"""
        debate = analysis.get("debate_result", {})
        if not isinstance(debate, dict):
            return 0
        
        lessons = []
        
        # 从 adjustments 提取
        for adj in debate.get("adjustments", [])[:3]:
            if adj and isinstance(adj, str) and len(adj) > 10:
                lessons.append({
                    "lesson_text": adj[:200],
                    "lesson_category": "trading",
                    "lesson_subcategory": "review_adjustment",
                    "impact_score": 0.7,
                    "confidence": 0.8,
                    "source_type": "review_orchestrator",
                    "expiry_date": (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
                })
        
        # 从 warnings 提取
        for w in debate.get("warnings", [])[:3]:
            if w and isinstance(w, str) and len(w) > 10:
                lessons.append({
                    "lesson_text": f"风险警告: {w[:150]}",
                    "lesson_category": "risk",
                    "lesson_subcategory": "review_warning",
                    "impact_score": 0.6,
                    "confidence": 0.7,
                    "source_type": "review_orchestrator",
                    "expiry_date": (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
                })
        
        if not lessons:
            return 0
        
        # 写入 DB
        try:
            conn = sqlite3.connect(self.collector.db_path)
            persisted = 0
            for l in lessons:
                existing = conn.execute(
                    "SELECT id FROM lessons WHERE lesson_text = ? AND source_type = ?",
                    (l["lesson_text"], l["source_type"])
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    "INSERT INTO lessons "
                    "(lesson_text, lesson_category, lesson_subcategory, impact_score, confidence, "
                    " symbols, source_type, expiry_date, evidence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        l["lesson_text"], l["lesson_category"], l["lesson_subcategory"],
                        l["impact_score"], l["confidence"],
                        json.dumps(l.get("symbols", [])), l["source_type"],
                        l["expiry_date"], "{}",
                    )
                )
                persisted += 1
            conn.commit()
            conn.close()
            logger.info(f"[Orchestrator] 教训提取: {len(lessons)} 候选, {persisted} 写入")
            return persisted
        except Exception as e:
            logger.warning(f"[Orchestrator] 教训写入失败: {e}")
            return 0


# =====================================================================
#  辩论结论验证器 (ValidationAgent) — Phase 3 of Hybrid Architecture
# =====================================================================

class ValidationAgent:
    """
    验证辩论结论的真实性 — 用代码/日志/DB 证据交叉验证
    
    Level 2 升级 (2026-05-05):
      - 支持结构化证据验证 (evidence_type + evidence_hint)
      - 自动执行证据提示中的验证操作
      - 区分: 模型自证证据 vs 独立验证证据
      - 评估证据质量: excellent/good/weak
    
    混合架构 Phase 3 (2026-05-05):
      - 读取辩论结果 (从 review response 或 debate file)
      - 对每个 claim 进行证据验证
      - 区分: 真实问题 (有证据) vs 幻觉 (无证据)
      - 评估修复建议的可行性
    """
    
    # 验证规则: 将辩论中的 claim 类型映射到验证方法
    VALIDATION_RULES = {
        "signal_quality": "_validate_signal_quality",
        "risk": "_validate_risk",
        "execution": "_validate_execution",
        "funnel": "_validate_funnel",
        "data_integrity": "_validate_data_integrity",
        "general": "_validate_general",
    }
    
    # 证据类型到验证方法的映射
    EVIDENCE_TYPE_HANDLERS = {
        "db_query": "_verify_db_evidence",
        "log_pattern": "_verify_log_evidence",
        "config_check": "_verify_config_evidence",
        "memory": "_verify_memory_evidence",
        "trend": "_verify_trend_evidence",
    }
    
    def __init__(self, db_path: str = None, decisions_db: str = None):
        self.db_path = db_path or DB_PATH
        self.decisions_db = decisions_db or DECISIONS_DB
        self._lock = threading.Lock()
    
    def _query(self, sql: str, params: tuple = (), db_path: str = None) -> List[Dict]:
        target = db_path or self.db_path
        try:
            conn = sqlite3.connect(target)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[ValidationAgent] 查询失败: {e}")
            return []
    
    def validate_debate_results(self, debate_results: Dict, date: str = None) -> Dict:
        """
        验证辩论结果中的每个 claim — Level 2: 支持结构化证据
        
        Args:
            debate_results: {
                "bull": {
                    "insights": [
                        {"claim": "...", "evidence_type": "db_query", "evidence_hint": "...", "confidence": 0.8},
                        "简单字符串 (兼容旧格式)",
                    ],
                    ...
                },
                ...
            }
            date: 复盘日期
        
        Returns:
            {
                "verified_issues": [...],     # 有证据支持的真实问题
                "unverified_claims": [...],   # 无证据的 claim (可能是幻觉)
                "evidence_quality_summary": { # Level 2: 证据质量统计
                    "excellent": 0,
                    "good": 0,
                    "weak": 0,
                },
                "confidence_score": float,    # 整体可信度 0-1
                "validation_summary": str,    # 人类可读的验证摘要
            }
        """
        verified = []
        unverified = []
        evidence_quality = {"excellent": 0, "good": 0, "weak": 0}
        
        for role in ["bull", "bear", "auditor"]:
            result = debate_results.get(role, {})
            if not isinstance(result, dict):
                continue
            
            # 验证 insights (兼容 Level 2 结构化格式和旧格式)
            for item in result.get("insights", []):
                v = self._validate_item(item, date, role)
                if v["verified"]:
                    verified.append(v)
                else:
                    unverified.append(v)
                # 统计证据质量
                eq = v.get("evidence_quality", "weak")
                if eq in evidence_quality:
                    evidence_quality[eq] += 1
            
            # 验证 root_causes
            for item in result.get("root_causes", []):
                v = self._validate_item(item, date, role, category="root_cause")
                if v["verified"]:
                    verified.append(v)
                else:
                    unverified.append(v)
                eq = v.get("evidence_quality", "weak")
                if eq in evidence_quality:
                    evidence_quality[eq] += 1
            
            # 验证 fixes/suggestions
            for item in result.get("fixes", result.get("suggestions", [])):
                v = self._validate_fix(item)
                if v.get("feasible"):
                    verified.append(v)
                else:
                    unverified.append(v)
        
        total = len(verified) + len(unverified)
        confidence = len(verified) / total if total > 0 else 0
        
        # Level 2: 证据质量评估
        total_evidence = sum(evidence_quality.values())
        if total_evidence > 0:
            quality_score = (evidence_quality["excellent"] * 1.0 + evidence_quality["good"] * 0.7 + evidence_quality["weak"] * 0.3) / total_evidence
        else:
            quality_score = 0
        
        overall_quality = "excellent" if quality_score > 0.8 else "good" if quality_score > 0.5 else "weak"
        
        return {
            "verified_issues": verified,
            "unverified_claims": unverified,
            "total_claims": total,
            "verified_count": len(verified),
            "evidence_quality_summary": evidence_quality,
            "evidence_quality_overall": overall_quality,
            "confidence_score": round(confidence, 2),
            "validation_summary": (
                f"验证完成: {len(verified)}/{total} 个 claim 有证据支持 "
                f"(可信度 {confidence:.0%}, 证据质量: {overall_quality})"
            ),
        }
    
    def _validate_item(self, item, date: str, role: str, category: str = "insight") -> Dict:
        """
        验证单个 item — Level 2: 兼容结构化证据和旧格式
        """
        # Level 2: 结构化证据格式
        if isinstance(item, dict):
            claim = item.get("claim", item.get("response", item.get("reason", "")))
            evidence_type = item.get("evidence_type", "")
            evidence_hint = item.get("evidence_hint", "")
            model_confidence = item.get("confidence", 0.5)
            
            if not claim or len(claim) < 5:
                return {"claim": str(item), "verified": False, "reason": "claim too short"}
            
            # 如果有证据提示, 尝试自动验证
            if evidence_type and evidence_hint:
                handler_name = self.EVIDENCE_TYPE_HANDLERS.get(evidence_type)
                if handler_name:
                    handler = getattr(self, handler_name, None)
                    if handler:
                        try:
                            evidence_result = handler(evidence_hint, claim, date)
                            if evidence_result.get("verified"):
                                return {
                                    "claim": claim,
                                    "category": category,
                                    "verified": True,
                                    "evidence_quality": "excellent" if evidence_result.get("direct") else "good",
                                    "evidence": evidence_result.get("evidence", evidence_hint),
                                    "model_confidence": model_confidence,
                                    "independent_verification": True,
                                    "severity": self._assess_severity_from_confidence(model_confidence),
                                }
                        except Exception as e:
                            logger.warning(f"[ValidationAgent] 证据验证失败: {e}")
            
            # 如果没有证据提示或验证失败, 使用旧方法验证
            cat = self._classify_claim(claim)
            validator_name = self.VALIDATION_RULES.get(cat, "_validate_general")
            validator = getattr(self, validator_name, self._validate_general)
            result = validator(claim, date, category)
            result["model_confidence"] = model_confidence
            result["evidence_quality"] = "good" if result.get("verified") else "weak"
            return result
        
        # 旧格式: 简单字符串
        if not isinstance(item, str) or len(item) < 10:
            return {"claim": str(item), "verified": False, "reason": "claim too short"}
        
        cat = self._classify_claim(item)
        validator_name = self.VALIDATION_RULES.get(cat, "_validate_general")
        validator = getattr(self, validator_name, self._validate_general)
        return validator(item, date, category)
    
    def _validate_claim(self, claim: str, date: str = None, category: str = "insight") -> Dict:
        """验证单个 claim 的真实性"""
        if not isinstance(claim, str) or len(claim) < 10:
            return {"claim": claim, "verified": False, "reason": "claim too short"}
        
        # 根据 claim 内容自动分类并选择验证方法
        cat = self._classify_claim(claim)
        validator_name = self.VALIDATION_RULES.get(cat, "_validate_general")
        validator = getattr(self, validator_name, self._validate_general)
        
        return validator(claim, date, category)
    
    def _classify_claim(self, claim: str) -> str:
        """自动分类 claim"""
        text = claim.lower()
        for keywords, category in DebateIssueExtractor.KEYWORD_CATEGORY_MAP.items():
            if any(kw.lower() in text for kw in keywords):
                return category
        return "general"
    
    def _validate_signal_quality(self, claim: str, date: str, category: str) -> Dict:
        """验证信号质量相关 claim — 检查 DB 中信号命中率"""
        if date:
            hits = self._query(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins "
                "FROM trade_records WHERE trade_date = ?",
                (date,)
            )
            if hits and hits[0]["total"] > 0:
                wr = hits[0]["wins"] / hits[0]["total"]
                return {
                    "claim": claim,
                    "category": "signal_quality",
                    "verified": True,
                    "evidence": f"当日 {hits[0]['total']} 笔交易, 胜率 {wr:.0%}",
                    "severity": "P0" if wr < 0.2 else "P1" if wr < 0.35 else "P2",
                }
        
        return {"claim": claim, "category": "signal_quality", "verified": False, "reason": "no trade data"}
    
    def _validate_risk(self, claim: str, date: str, category: str) -> Dict:
        """验证风控相关 claim — 检查资金状态和回撤"""
        capital = self._query(
            "SELECT current_equity, peak_equity, drawdown_pct, water_level "
            "FROM capital_state ORDER BY last_update DESC LIMIT 1"
        )
        if capital:
            c = capital[0]
            dd = c.get("drawdown_pct", 0)
            return {
                "claim": claim,
                "category": "risk",
                "verified": True,
                "evidence": f"equity={c.get('current_equity', 'N/A')}, drawdown={dd:.1%}, level={c.get('water_level', 'N/A')}",
                "severity": "P0" if dd < -0.1 else "P1" if dd < -0.05 else "P2",
            }
        
        return {"claim": claim, "category": "risk", "verified": False, "reason": "no capital data"}
    
    def _validate_execution(self, claim: str, date: str, category: str) -> Dict:
        """验证执行相关 claim — 检查日志中的执行异常"""
        log_path = PROJECT_ROOT / "logs" / "ftseek_us.log"
        evidence_found = False
        error_count = 0
        
        try:
            if log_path.exists() and date:
                with open(log_path, "r") as f:
                    lines = f.readlines()[-2000:]
                
                today = datetime.now().strftime("%Y-%m-%d")
                keywords = ["滑点", "重复", "ghost", "幽灵", "持仓", "对账", "同步",
                           "slippage", "duplicate", "ghost", "reconcil", "sync"]
                
                for line in lines:
                    if today in line and any(kw in line.lower() for kw in keywords):
                        evidence_found = True
                        error_count += 1
                        break
        except Exception:
            pass
        
        return {
            "claim": claim,
            "category": "execution",
            "verified": evidence_found,
            "evidence": f"日志中发现 {error_count} 条相关异常" if evidence_found else "日志中未发现相关异常",
            "severity": "P1" if evidence_found else "P2",
        }
    
    def _validate_funnel(self, claim: str, date: str, category: str) -> Dict:
        """验证漏斗相关 claim — 检查决策漏斗数据"""
        if date:
            decisions = self._query(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN direction != 'NEUTRAL' THEN 1 ELSE 0 END) as actionable "
                "FROM decisions WHERE date(created_at) = ?",
                (date,), db_path=self.decisions_db
            )
            if decisions and decisions[0]["total"] > 0:
                d = decisions[0]
                actionable_pct = d["actionable"] / d["total"]
                return {
                    "claim": claim,
                    "category": "funnel",
                    "verified": True,
                    "evidence": f"当日 {d['total']} 个决策, {d['actionable']} 个 actionable ({actionable_pct:.0%})",
                    "severity": "P1" if actionable_pct < 0.3 else "P2",
                }
        
        return {"claim": claim, "category": "funnel", "verified": False, "reason": "no decision data"}
    
    def _validate_data_integrity(self, claim: str, date: str, category: str) -> Dict:
        """验证数据完整性 claim — 检查心跳/连接状态"""
        log_path = PROJECT_ROOT / "logs" / "ftseek_us.log"
        evidence_found = False
        
        try:
            if log_path.exists():
                with open(log_path, "r") as f:
                    lines = f.readlines()[-500:]
                
                today = datetime.now().strftime("%Y-%m-%d")
                keywords = ["timeout", "connection", "refused", "failed", "error",
                           "断流", "降级", "不可用", "熔断"]
                
                for line in lines:
                    if today in line and any(kw in line.lower() for kw in keywords):
                        evidence_found = True
                        break
        except Exception:
            pass
        
        return {
            "claim": claim,
            "category": "data_integrity",
            "verified": evidence_found,
            "evidence": "日志中发现数据异常迹象" if evidence_found else "日志中未发现数据异常",
            "severity": "P1" if evidence_found else "P2",
        }
    
    def _validate_general(self, claim: str, date: str, category: str) -> Dict:
        """通用验证 — 检查交易数据是否支持 claim"""
        if date:
            trades = self._query(
                "SELECT COUNT(*) as total, SUM(realized_pnl) as pnl "
                "FROM trade_records WHERE trade_date = ?",
                (date,)
            )
            if trades and trades[0]["total"] > 0:
                return {
                    "claim": claim,
                    "category": category,
                    "verified": True,
                    "evidence": f"当日 {trades[0]['total']} 笔交易, 总盈亏 ${trades[0]['pnl']:.0f}",
                    "severity": "P1" if trades[0]["pnl"] < 0 else "P2",
                }
        
        return {"claim": claim, "category": category, "verified": False, "reason": "no supporting data"}
    
    def _validate_fix(self, fix_claim) -> Dict:
        """评估修复建议的可行性 — Level 2: 兼容结构化格式"""
        # Level 2: 结构化格式
        if isinstance(fix_claim, dict):
            claim = fix_claim.get("claim", fix_claim.get("fix", ""))
            target = fix_claim.get("target", "")
            effort = fix_claim.get("effort", "high")
            confidence = fix_claim.get("confidence", 0.5)
            
            has_code_ref = any(kw in claim.lower() for kw in [
                "config", "yaml", "参数", "threshold", "阈值", "修改", "调整",
                "engine", "pipeline", "代码", "module"
            ]) if claim else False
            
            return {
                "claim": claim,
                "category": "fix",
                "feasible": has_code_ref or target in ["config", "code"],
                "reason": "包含代码/配置变更" if has_code_ref else "缺乏具体实现细节",
                "estimated_effort": effort if effort in ["low", "medium", "high"] else ("low" if has_code_ref else "high"),
                "model_confidence": confidence,
            }
        
        # 旧格式: 简单字符串
        if not isinstance(fix_claim, str):
            return {"claim": str(fix_claim), "category": "fix", "feasible": False, "reason": "invalid format"}
        
        has_code_ref = any(kw in fix_claim.lower() for kw in [
            "config", "yaml", "参数", "threshold", "阈值", "修改", "调整",
            "engine", "pipeline", "代码", "module"
        ])
        
        return {
            "claim": fix_claim,
            "category": "fix",
            "feasible": has_code_ref,
            "reason": "包含代码/配置变更" if has_code_ref else "缺乏具体实现细节",
            "estimated_effort": "low" if has_code_ref else "high",
        }
    
    # =====================================================================
    # Level 2: 证据验证方法
    # =====================================================================
    
    def _verify_db_evidence(self, hint: str, claim: str, date: str) -> Dict:
        """验证 DB 类型证据 — 执行 SQL 查询"""
        # 从 hint 中提取关键词并构建简单查询
        # 支持的 hint 格式: "trade_records WHERE exit_reason IS NOT NULL", "胜率", "PRE_CLOSE_EXIT" 等
        try:
            if "trade_records" in hint.lower() or "trade" in hint.lower():
                # 通用交易查询
                if date:
                    result = self._query(
                        "SELECT COUNT(*) as total, "
                        "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins, "
                        "SUM(realized_pnl) as total_pnl "
                        "FROM trade_records WHERE trade_date = ?",
                        (date,)
                    )
                    if result and result[0]["total"] > 0:
                        return {
                            "verified": True,
                            "direct": True,
                            "evidence": f"{result[0]['total']}笔交易, 胜率{result[0]['wins']/result[0]['total']:.0%}, 盈亏${result[0]['total_pnl']:.0f}",
                        }
            elif "decision" in hint.lower():
                if date:
                    result = self._query(
                        "SELECT COUNT(*) as total FROM decisions WHERE date(created_at) = ?",
                        (date,), db_path=self.decisions_db
                    )
                    if result:
                        return {
                            "verified": True,
                            "direct": True,
                            "evidence": f"{result[0]['total']}个决策",
                        }
            
            # Fallback: 通用计数查询
            return {"verified": False, "direct": False, "evidence": "DB 查询未匹配"}
        except Exception as e:
            return {"verified": False, "direct": False, "evidence": f"DB 查询失败: {str(e)[:100]}"}
    
    def _verify_log_evidence(self, hint: str, claim: str, date: str) -> Dict:
        """验证日志类型证据 — 搜索日志模式"""
        log_path = PROJECT_ROOT / "logs" / "ftseek_us.log"
        try:
            if log_path.exists():
                with open(log_path, "r") as f:
                    lines = f.readlines()[-2000:]
                
                today = datetime.now().strftime("%Y-%m-%d")
                # 提取 hint 中的关键词
                keywords = [k for k in hint.split() if len(k) > 2]
                match_count = 0
                for line in lines:
                    if today in line and any(kw.lower() in line.lower() for kw in keywords):
                        match_count += 1
                
                if match_count > 0:
                    return {
                        "verified": True,
                        "direct": True,
                        "evidence": f"日志中发现 {match_count} 条匹配记录",
                    }
            
            return {"verified": False, "direct": False, "evidence": "日志中未发现匹配"}
        except Exception as e:
            return {"verified": False, "direct": False, "evidence": f"日志搜索失败: {str(e)[:100]}"}
    
    def _verify_config_evidence(self, hint: str, claim: str, date: str) -> Dict:
        """验证配置类型证据 — 检查 unified_config.yaml"""
        config_path = PROJECT_ROOT / "config" / "unified_config.yaml"
        try:
            if config_path.exists():
                with open(config_path, "r") as f:
                    config_content = f.read()
                
                # 提取 hint 中的关键词
                keywords = [k for k in hint.split() if len(k) > 2]
                found = any(kw.lower() in config_content.lower() for kw in keywords)
                
                if found:
                    return {
                        "verified": True,
                        "direct": True,
                        "evidence": f"配置中存在关键词: {', '.join(keywords[:3])}",
                    }
            
            return {"verified": False, "direct": False, "evidence": "配置中未找到关键词"}
        except Exception as e:
            return {"verified": False, "direct": False, "evidence": f"配置检查失败: {str(e)[:100]}"}
    
    def _verify_memory_evidence(self, hint: str, claim: str, date: str) -> Dict:
        """验证记忆类型证据 — 检查 us.json"""
        try:
            with open(MEMORY_FILE, "r") as f:
                mem = json.load(f)
            
            # 提取 hint 中的关键词
            keywords = [k for k in hint.split() if len(k) > 2]
            mem_text = json.dumps(mem, ensure_ascii=False).lower()
            found = any(kw.lower() in mem_text for kw in keywords)
            
            if found:
                return {
                    "verified": True,
                    "direct": True,
                    "evidence": f"记忆中存在相关记录",
                }
            
            return {"verified": False, "direct": False, "evidence": "记忆中未找到相关记录"}
        except Exception as e:
            return {"verified": False, "direct": False, "evidence": f"记忆检查失败: {str(e)[:100]}"}
    
    def _verify_trend_evidence(self, hint: str, claim: str, date: str) -> Dict:
        """验证趋势类型证据 — 检查近期性能趋势"""
        try:
            if date:
                # 查询最近 7 天趋势
                trend = self._query(
                    "SELECT trade_date, COUNT(*) as trades, "
                    "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins "
                    "FROM trade_records WHERE exit_reason IS NOT NULL "
                    "AND trade_date >= date(?, '-7 days') AND trade_date <= ? "
                    "GROUP BY trade_date ORDER BY trade_date",
                    (date, date)
                )
                if trend and len(trend) >= 3:
                    win_rates = [t["wins"]/t["trades"] if t["trades"] > 0 else 0 for t in trend]
                    return {
                        "verified": True,
                        "direct": True,
                        "evidence": f"7天趋势: {[f'{wr:.0%}' for wr in win_rates]}",
                    }
            
            return {"verified": False, "direct": False, "evidence": "趋势数据不足"}
        except Exception as e:
            return {"verified": False, "direct": False, "evidence": f"趋势验证失败: {str(e)[:100]}"}
    
    def _assess_severity_from_confidence(self, confidence: float) -> str:
        """根据模型置信度评估严重性"""
        if confidence >= 0.85:
            return "P0"
        elif confidence >= 0.7:
            return "P1"
        else:
            return "P2"


# =====================================================================
#  模块级单例
# =====================================================================

_orchestrator: Optional[ReviewOrchestrator] = None
_orchestrator_lock = threading.Lock()


def get_review_orchestrator(notifier=None, agent_bridge=None) -> ReviewOrchestrator:
    global _orchestrator
    # P0 修复 (2026-05-03): 加锁防止并发初始化竞态
    with _orchestrator_lock:
        if _orchestrator is None:
            _orchestrator = ReviewOrchestrator(notifier=notifier, agent_bridge=agent_bridge)
    return _orchestrator


def reset_review_orchestrator():
    global _orchestrator
    with _orchestrator_lock:
        _orchestrator = None
