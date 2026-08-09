"""
FTSeek-US 与 Hermes Agent 轻量桥接模块

核心设计:
1. 通过文件系统与本地 Hermes Agent 通信
2. 美股记忆独立 (us.json)，避免与港股经验混用
3. 聚焦"稳定赚钱"核心目标
4. 独立降级: Hermes 不可用时自动 fallback，不影响交易

架构:
  盘前 (08:00 ET) → 写入请求 → Hermes 处理 → 读取计划
  盘后 (16:30 ET) → 写入交易记录 → Hermes 处理 → 读取报告 + 更新记忆
  ↓
  记忆分离: us.json (美股)
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftseek_us.llm.agent_bridge")

# 文件系统协议目录
HERMES_HOME = os.path.expanduser("~/.hermes")
FTSEEK_DIR = os.path.join(HERMES_HOME, "ftseek")
REQUESTS_DIR = os.path.join(FTSEEK_DIR, "requests")
RESPONSES_DIR = os.path.join(FTSEEK_DIR, "responses")
MEMORY_DIR = os.path.join(FTSEEK_DIR, "memory")

# 确保目录存在
for d in [REQUESTS_DIR, RESPONSES_DIR, MEMORY_DIR]:
    os.makedirs(d, exist_ok=True)

# ── P3 G1: SQLite-backed request queue (survives restarts) ──

QUEUE_DB_PATH = os.path.join(FTSEEK_DIR, "hermes_queue.db")


class AgentRequestQueue:
    """SQLite-backed durable request queue for Hermes Agent communication.

    Survives process restarts. Tracks status, retries, and processing time.
    """

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    def __init__(self, db_path: str = QUEUE_DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS agent_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT NOT NULL UNIQUE,
                        request_type TEXT NOT NULL,
                        market TEXT NOT NULL DEFAULT 'us',
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        retry_count INTEGER DEFAULT 0,
                        max_retries INTEGER DEFAULT 3,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        error_message TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ar_status ON agent_requests(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ar_type ON agent_requests(request_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ar_created ON agent_requests(created_at)")
                conn.commit()
                logger.info("[AgentQueue] SQLite 请求队列表初始化完成")
            except Exception as e:
                conn.rollback()
                logger.error(f"[AgentQueue] 初始化失败: {e}")
                raise
            finally:
                conn.close()

    def enqueue(self, request_id: str, request_type: str, payload: Dict,
                market: str = "us", max_retries: int = 3) -> bool:
        """Enqueue a request for durable async processing."""
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO agent_requests
                    (request_id, request_type, market, payload, status,
                     max_retries, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    request_id, request_type, market, json.dumps(payload, ensure_ascii=False),
                    self.STATUS_PENDING, max_retries, now, now,
                ))
                conn.commit()
                logger.info(f"[AgentQueue] 入队: {request_id} ({request_type})")
                return True
            except Exception as e:
                conn.rollback()
                logger.error(f"[AgentQueue] 入队失败: {e}")
                return False
            finally:
                conn.close()

    def poll_pending(self, request_type: str = None, limit: int = 5) -> List[Dict]:
        """Get pending requests ready for processing."""
        conn = self._conn()
        try:
            query = "SELECT * FROM agent_requests WHERE status = ?"
            params = [self.STATUS_PENDING]
            if request_type:
                query += " AND request_type = ?"
                params.append(request_type)
            query += " ORDER BY created_at ASC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_processing(self, request_id: str) -> bool:
        """Mark a request as being processed."""
        now = datetime.now().isoformat()
        return self._update_status(request_id, self.STATUS_PROCESSING, updated_at=now)

    def mark_done(self, request_id: str) -> bool:
        """Mark a request as completed."""
        now = datetime.now().isoformat()
        return self._update_status(request_id, self.STATUS_DONE,
                                   completed_at=now, updated_at=now)

    def mark_failed(self, request_id: str, error: str = "") -> bool:
        """Mark a request as failed, incrementing retry count."""
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT retry_count, max_retries FROM agent_requests WHERE request_id = ?",
                    (request_id,)
                ).fetchone()
                if not row:
                    return False

                new_count = row["retry_count"] + 1
                if new_count >= row["max_retries"]:
                    conn.execute("""
                        UPDATE agent_requests SET status=?, retry_count=?, error_message=?,
                        updated_at=?, completed_at=?
                        WHERE request_id=?
                    """, (self.STATUS_FAILED, new_count, error, now, now, request_id))
                else:
                    conn.execute("""
                        UPDATE agent_requests SET status=?, retry_count=?, error_message=?,
                        updated_at=?
                        WHERE request_id=?
                    """, (self.STATUS_PENDING, new_count, error, now, request_id))
                conn.commit()
                logger.info(f"[AgentQueue] 标记失败: {request_id} retry={new_count}/{row['max_retries']}")
                return True
            except Exception as e:
                conn.rollback()
                logger.error(f"[AgentQueue] 标记失败出错: {e}")
                return False
            finally:
                conn.close()

    def _update_status(self, request_id: str, status: str, **extra) -> bool:
        with self._lock:
            conn = self._conn()
            try:
                sets = ["status = ?", "updated_at = ?"]
                params = [status, extra.get("updated_at", datetime.now().isoformat())]
                if "completed_at" in extra:
                    sets.append("completed_at = ?")
                    params.append(extra["completed_at"])
                params.append(request_id)
                conn.execute(
                    f"UPDATE agent_requests SET {', '.join(sets)} WHERE request_id = ?",
                    params
                )
                conn.commit()
                return True
            except Exception as e:
                conn.rollback()
                logger.error(f"[AgentQueue] 更新状态失败: {e}")
                return False
            finally:
                conn.close()

    def get_stats(self) -> Dict:
        """Get queue statistics."""
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT status, COUNT(*) as cnt FROM agent_requests GROUP BY status
            """).fetchall()
            stats = {r["status"]: r["cnt"] for r in rows}
            total = sum(stats.values())
            return {"total": total, "by_status": stats}
        finally:
            conn.close()

    def purge_completed(self, days: int = 7):
        """Remove completed/failed requests older than N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM agent_requests WHERE status IN (?, ?) AND completed_at < ?",
                    (self.STATUS_DONE, self.STATUS_FAILED, cutoff)
                )
                conn.commit()
                logger.info(f"[AgentQueue] 清理 {days} 天前已完成请求")
            finally:
                conn.close()


# 模块级队列单例
_request_queue: Optional[AgentRequestQueue] = None


def get_request_queue() -> AgentRequestQueue:
    global _request_queue
    if _request_queue is None:
        _request_queue = AgentRequestQueue()
    return _request_queue


class FTSeekUSAgentBridge:
    """
    FTSeek-US 与 Hermes Agent 的轻量桥接

    设计原则:
    - 稳定赚钱 > 激进收益
    - 数据驱动 > 主观判断
    - 持续改进 > 一次性优化
    - 记忆独立: us.json (美股专用)
    - 降级优先 > 阻塞等待

    v3.0 (2026-05-06): 新增 HermesClient 支持 — Python 3.8 ↔ Worker (Python 3.11)
    """

    def __init__(self):
        self.market = "us"
        self._memory_file = os.path.join(MEMORY_DIR, "us.json")
        self._memory = self._load_memory()
        self._hermes_available = True
        self._client_available = False  # HermesClient 可用标记
        self._client = None
        
    def _load_memory(self) -> Dict:
        """加载美股交易记忆"""
        if os.path.exists(self._memory_file):
            try:
                with open(self._memory_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[Agent-US] 加载记忆失败: {e}")
        return self._default_memory()
    
    def _default_memory(self) -> Dict:
        """美股默认记忆"""
        return {
            "market": "美股",
            "core_principle": "稳定赚钱 > 激进收益",
            "market_specific_rules": [
                "1. 永远先保本，再考虑盈利",
                "2. 单日亏损超 3% 立即停手",
                "3. 连续 3 笔亏损，强制休息",
                "4. 计划外的交易需要 2 倍确认",
                "5. 盈利时要拿得住，亏损时要跑得快",
                "6. 不要试图抓住每一波行情",
                "7. 美股受宏观数据 (CPI/非农/FOMC) 影响大",
                "8. 注意盘前/盘后交易量和价差",
                "9. PDT 规则限制 (账户<$25k 时 5 天 3 次)",
                "10. 资金安全第一，收益率第二"
            ],
            "historical_lessons": [],
            "tagged_lessons": [],
            "strategy_adjustments": [],
            "symbol_lessons": {},
            "performance_tracking": {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "max_drawdown": 0.0,
                "last_30_days_pnl": 0.0
            },
            "last_updated": datetime.now().isoformat()
        }
    
    def _save_memory(self):
        """保存记忆到本地文件"""
        self._memory["last_updated"] = datetime.now().isoformat()
        try:
            with open(self._memory_file, 'w', encoding='utf-8') as f:
                json.dump(self._memory, f, ensure_ascii=False, indent=2)
            logger.info("[Agent-US] 交易记忆已保存")
        except Exception as e:
            logger.error(f"[Agent-US] 保存记忆失败: {e}")
    
    def generate_battle_plan(self, context: Dict = None) -> Dict:
        """
        盘前生成作战计划 (增强版)

        context 包含完整数据源:
        - yesterday_review: 昨日复盘结论
        - overnight_news: 隔夜新闻摘要
        - market_context: 宏观环境 (VIX/SPY/QQQ/DXY)
        - earnings_info: 财报日历
        - watchlist_info: 股池信息
        - positions: 当前持仓
        - capital_state: 资金状态
        - memory: 历史记忆
        - session_exit_quality: P2 G3 — 昨日各时段退出准确率
        """
        logger.info("[Agent-US] 开始生成盘前作战计划 (增强版)...")

        memory_context = self._format_memory_for_prompt()
        request_id = f"premarket_us_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        ctx = context or {}

        # P2 G3: 注入昨日退出质量数据, 供 LLM 调整今日入场策略
        session_quality_context = ctx.get("session_exit_quality", "")
        if not session_quality_context:
            try:
                from app.storage.exit_quality_tracker import get_exit_quality_tracker
                tracker = get_exit_quality_tracker()
                q_report = tracker.get_session_quality_report(days=5)  # 近5天
                if q_report:
                    lines = ["【近5日各时段退出准确率】"]
                    for sess, stats in sorted(q_report.items()):
                        lines.append(
                            f"  {sess}: {stats['accuracy']*100:.0f}% "
                            f"(good={stats['good']} bad={stats['bad']} "
                            f"neutral={stats['neutral']} total={stats['total']})"
                        )
                    # 标记问题时段
                    problem_sessions = [
                        s for s, st in q_report.items()
                        if st["accuracy"] < 0.45 and st["total"] >= 5
                    ]
                    if problem_sessions:
                        lines.append(f"⚠️ 问题时段 (准确率<45%): {', '.join(problem_sessions)}")
                        lines.append("→ 这些时段应避免开新仓, 或收紧入场标准")
                    session_quality_context = "\n".join(lines)
            except Exception as e:
                logger.debug(f"[Agent-US] 退出质量数据加载失败 (不影响计划生成): {e}")
                session_quality_context = ""

        # P2.2 (RFC-060): 注入历史教训 (复盘→次日计划闭环)
        # generate_battle_plan_injection() 此前零调用 — 接通让昨日亏钱教训影响今日策略
        lessons_context = ctx.get("historical_lessons", "")
        if not lessons_context:
            try:
                from app.review.lesson_injector import LessonInjector
                lessons_context = LessonInjector().generate_battle_plan_injection()
            except Exception as e:
                logger.debug(f"[Agent-US] 历史教训加载失败 (不影响计划生成): {e}")
                lessons_context = ""

        request = {
            "id": request_id,
            "type": "generate_battle_plan",
            "market": "us",
            "timestamp": datetime.now().isoformat(),
            "context": {
                "memory": memory_context,
                "yesterday_review": ctx.get("yesterday_review", ""),
                "overnight_news": ctx.get("overnight_news", ""),
                "market_context": ctx.get("market_context", {}),
                "earnings_info": ctx.get("earnings_info", ""),
                "watchlist_info": ctx.get("watchlist_info", []),
                "positions": ctx.get("positions", []),
                "capital_state": ctx.get("capital_state", {}),
                "sector_analysis": ctx.get("sector_analysis", {}),
                "decision_funnel": ctx.get("decision_funnel", {}),
                "rejection_analysis": ctx.get("rejection_analysis", {}),
                "session_exit_quality": session_quality_context,  # P2 G3
                "lessons": lessons_context,  # P2.2 历史教训 (复盘→计划闭环)
            },
            "prompt": f"""你是 FTSeek-US 美股盘前策略师。核心目标：**稳定赚钱**。

【历史记忆与教训】(美股)
{memory_context}

【历史教训 (复盘提炼 — 来自亏损经验, 务必参考)】
{lessons_context}

【昨日复盘结论】
{ctx.get('yesterday_review', '无昨日复盘数据')}

【各时段退出质量 (近5日) — 决定今日入场策略】
{session_quality_context}

【隔夜重要新闻】
{ctx.get('overnight_news', '无隔夜新闻')}

【宏观环境】
VIX: {ctx.get('market_context', {}).get('vix', 'N/A')}
SPY 盘前: {ctx.get('market_context', {}).get('spy', 'N/A')}
QQQ 盘前: {ctx.get('market_context', {}).get('qqq', 'N/A')}
DXY: {ctx.get('market_context', {}).get('dxy', 'N/A')}
市场阶段: {ctx.get('market_context', {}).get('regime', 'NEUTRAL')}

【财报日历】
{ctx.get('earnings_info', '本周无重要财报')}

【股池信息】
{json.dumps(ctx.get('watchlist_info', [])[:10], ensure_ascii=False, indent=2)}

【当前持仓】
{json.dumps(ctx.get('positions', [])[:5], ensure_ascii=False, indent=2)}

【资金状态】
权益: {ctx.get('capital_state', {}).get('current_equity', 'N/A')}
回撤: {ctx.get('capital_state', {}).get('drawdown_pct', 'N/A')}
水位: {ctx.get('capital_state', {}).get('water_level', 'N/A')}

【昨日决策漏斗】
{json.dumps(ctx.get('decision_funnel', {}), ensure_ascii=False, indent=2)}

【拒绝分析】
{json.dumps(ctx.get('rejection_analysis', {}), ensure_ascii=False, indent=2)}

═══════════════════════════════════════════
【发散思维引导 — 必须做跨维度推理】
═══════════════════════════════════════════

不是简单罗列数据，而是建立**因果传导链**。以下是推理框架（不限于此）:

1. **新闻 → 行业 → 个股**:
   例: "伊朗停火 → 原油跌 → XLE 能源股承压 → 做空 XOM/CVX"
   例: "FDA 批准新药 → 医药股大涨 → 做多相关标的"
   例: "CPI 超预期 → 降息预期推迟 → 科技股承压 → QQQ SHORT"

2. **跨市场联动**:
   例: "恒生科技指数昨晚 +3% → 中概 ADR 映射 → BABA/JD 可能高开 → 观察盘前量比"
   例: "日经 -2% → 亚太风险情绪差 → SPY 可能低开 → 关注 VIX 是否突破 20"
   例: "美元指数突破 105 → 新兴市场承压 → EEM 做空机会"

3. **财报窗口策略**:
   - 财报前 1-3 天: 可能有 run-up，但必须提前退出，不可持仓赌财报
   - 财报当日: 加入 restricted 列表，IV Crush + 滑点风险极高
   - 财报后首日: 波动率放大 50-100%，止损需要放宽

4. **持仓诊断**:
   - 检查每笔持仓的 SL/TP 距离，是否需要提前调整
   - 如果持仓股有财报/新闻催化，需要特别评估是否继续持有
   - 如果资金回撤接近 3%，考虑减仓保本

5. **股池动态调整**:
   - 基于新闻/财报/技术面，临时加入高确定性标的
   - 将数据不可用/高波动/财报期的标的加入 restricted

═══════════════════════════════════════════
【任务要求】
═══════════════════════════════════════════

1. 先用 2-3 句话做**市场阶段判断**，说明推理逻辑（不是只给结论）
2. 识别今日重点标的 (最多 5 只)，每只必须给出:
   - 推荐方向 (LONG/SHORT)
   - 入场区间、止损、止盈
   - **推理链**: 为什么选这个标的？(新闻催化/财报/技术面/跨市场映射)
3. 标注限制标的 (财报期/高波动/数据不可用/需要回避)
4. 对**现有持仓**逐一给出操作建议 (持有/调整 SL/提前退出/加仓)
5. 给出策略调整建议 (基于历史教训 + 当日市场环境)
6. 列出今日风险点 (宏观/个股/系统)

═══════════════════════════════════════════
【结构性约束 — 下游硬规, 违反即废单】(2026-08-09, DEB-20260808 §17.1)
═══════════════════════════════════════════
1. **止损距离 2-5%**: stop_loss 距 entry_zone 超出 6% 的指令被准入层硬拦 (白产);
   小于 2% 落入噪声扫损区 — 给出 2-5% 的止损才有效
2. **不接飞刀**: 当日跌 >5% 且无企稳证据的 LONG 一律不推荐 (刀具下坠不伸手)
3. **回踩优先于追涨**: 入场区间贴近支撑/VWAP 的回调企稳标的优先;
   新高延伸追涨标的降级 (实证 8月: 回调日 LONG 净正 / 追涨日 LONG 净负)

【输出格式】严格 JSON
{{
  "date": "YYYY-MM-DD",
  "market_regime": "RISK_ON/RISK_OFF/ROTATION/NEUTRAL",
  "regime_reasoning": "判断市场阶段的推理逻辑 (2-3 句话)",
  "targets": [
    {{
      "symbol": "US.NVDA",
      "direction": "LONG",
      "entry_zone": [142.0, 144.5],
      "stop_loss": 139.0,
      "take_profit": 152.0,
      "urgency": "HIGH/MEDIUM/LOW",
      "rationale": "推荐理由 (必须包含推理链，如 'Gap up on earnings + VWAP hold')"
    }}
  ],
  "restricted": ["US.TSLA"],
  "position_actions": [
    {{
      "symbol": "US.QQQ",
      "action": "HOLD/ADJUST_SL/EXIT/ADD",
      "reason": "操作理由"
    }}
  ],
  "strategy_adjustments": ["基于历史教训和当日环境的具体调整"],
  "risk_warnings": ["今日风险点"],
  "notes": "其他注意事项"
}}
"""
        }
        
        request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
        with open(request_file, 'w', encoding='utf-8') as f:
            json.dump(request, f, ensure_ascii=False, indent=2)
        logger.info(f"[Agent-US] 盘前计划请求已写入: {request_file}")

        # P3 G1: Durable enqueue for crash recovery
        try:
            get_request_queue().enqueue(request_id, "generate_battle_plan", request)
        except Exception as e:
            logger.debug(f"[Agent-US] 队列写入跳过: {e}")

        return request

    def generate_eod_review(self, trades: List[Dict] = None,
                            signals: List[Dict] = None,
                            rejections: Dict = None,
                            capital: Dict = None,
                            battle_plan: Dict = None) -> Dict:
        """
        盘后全链路归因分析
        
        利用长上下文分析今日交易、信号、拒绝、资金轨迹
        """
        logger.info("[Agent-US] 开始生成盘后归因报告...")
        
        # B1 修复 (2026-05-13): _et_date 在 prompt 模板中使用但未定义
        from datetime import timezone as _tz, timedelta as _td
        _et_date = datetime.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
        
        request_id = f"eod_us_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        memory_context = self._format_memory_for_prompt()
        
        request = {
            "id": request_id,
            "type": "generate_eod_review",
            "market": "us",
            "timestamp": datetime.now().isoformat(),
            "context": {
                "trades": trades or [],
                "signals": signals or {},
                "rejections": rejections or {},
                "capital": capital or {},
                "battle_plan": battle_plan or {},
                "memory": memory_context
            },
            "prompt": f"""你是 FTSeek-US 美股盘后复盘师。核心目标：**稳定赚钱**。

【今日作战计划】
{json.dumps(battle_plan, indent=2, ensure_ascii=False) if battle_plan else "无计划"}

【实际交易】
{json.dumps(trades, indent=2, ensure_ascii=False) if trades else "无交易"}

【信号漏斗】
{json.dumps(signals, indent=2, ensure_ascii=False) if signals else "无数据"}

【拒绝归因 TOP】
{json.dumps(rejections, indent=2, ensure_ascii=False) if rejections else "无数据"}

【资金轨迹】
{json.dumps(capital, indent=2, ensure_ascii=False) if capital else "无数据"}

【历史记忆】
{memory_context}

【任务要求】
1. 对比计划与实际执行的偏差
2. 分析每笔交易的盈亏归因
3. 识别系统性问题（信号质量/执行延迟/风控过严）
4. 提取 1-2 条新的交易教训
5. 提出明日改进建议

【输出格式】
严格 JSON:
{{
  "date": "{_et_date}",
  "summary": {{
    "total_trades": 0,
    "win_rate": 0.0,
    "total_pnl": 0.0,
    "plan_execution_rate": 0.0
  }},
  "plan_vs_actual": {{
    "planned": [],
    "executed": [],
    "missed": [],
    "extra": []
  }},
  "rejection_analysis": {{
    "top_reason": "",
    "count": 0,
    "is_systemic": false
  }},
  "lessons": [""],
  "symbol_lessons": {{"US.XXX": ""}},
  "tomorrow_suggestions": [""],
  "confidence": 0.0
}}
"""
        }
        
        request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
        with open(request_file, 'w', encoding='utf-8') as f:
            json.dump(request, f, ensure_ascii=False, indent=2)
        logger.info(f"[Agent-US] EOD 请求已写入: {request_file}")

        # P3 G1: Durable enqueue for crash recovery
        try:
            get_request_queue().enqueue(request_id, "generate_eod_review", request)
        except Exception as e:
            logger.debug(f"[Agent-US] 队列写入跳过: {e}")

        return request

    def wait_for_response(self, request_id: str, timeout: int = 300) -> Dict:
        """等待 Hermes Agent 响应"""
        if not self._hermes_available:
            logger.warning("[Agent-US] Hermes 不可用，使用降级响应")
            return self._fallback_response(request_id)
        response_file = os.path.join(RESPONSES_DIR, f"{request_id}.json")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if os.path.exists(response_file):
                try:
                    with open(response_file, 'r', encoding='utf-8') as f:
                        raw_text = f.read()
                    
                    # P0 修复 (2026-04-26): 清理 LLM 常见的 Markdown 标记，防止 JSON 解析失败导致死循环等待
                    cleaned_text = raw_text.strip()
                    if cleaned_text.startswith('```json'):
                        cleaned_text = cleaned_text[7:]
                    if cleaned_text.startswith('```'):
                        cleaned_text = cleaned_text[3:]
                    if cleaned_text.endswith('```'):
                        cleaned_text = cleaned_text[:-3]
                    
                    response = json.loads(cleaned_text)
                    logger.info(f"[Agent-US] 收到响应: {request_id}")
                    self._hermes_available = True

                    request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
                    if os.path.exists(request_file):
                        os.remove(request_file)

                    result = response.get("result", {})

                    # 更新记忆
                    if "memory_updates" in result:
                        self._update_memory(result["memory_updates"])

                    # P3 G1: mark queue item done
                    try:
                        get_request_queue().mark_done(request_id)
                    except Exception:
                        pass

                    return result
                except Exception as e:
                    logger.warning(f"[Agent-US] 读取响应失败: {e}")
            time.sleep(2)

        logger.warning(f"[Agent-US] 响应超时: {request_id}")
        self._hermes_available = False
        # P3 G1: mark queue item failed on timeout
        try:
            get_request_queue().mark_failed(request_id, "timeout")
        except Exception:
            pass
        return self._fallback_response(request_id)
    
    def _fallback_response(self, request_id: str) -> Dict:
        """降级响应"""
        if "premarket" in request_id:
            return {
                "status": "fallback",
                "message": "Hermes 不可用，使用降级模式",
                "plan": {
                    "date": datetime.now().strftime('%Y-%m-%d'),
                    "market_regime": "NEUTRAL",
                    "targets": [],
                    "restricted": [],
                    "risk_warnings": ["Agent 不可用，建议保守交易"],
                    "max_positions": 2,
                    "notes": "使用硬编码风控，等待 Agent 恢复"
                }
            }
        else:
            return {
                "status": "fallback",
                "message": "Hermes 不可用，跳过 AI 归因",
                "summary": {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0},
                "memory_updates": {}
            }
    
    def _update_memory(self, updates: Dict):
        """更新交易记忆"""
        if "historical_lessons" in updates:
            for lesson in updates["historical_lessons"]:
                if lesson not in self._memory["historical_lessons"]:
                    self._memory["historical_lessons"].append(lesson)
                    logger.info(f"[Agent-US] 新增教训: {lesson}")
        
        if "symbol_lessons" in updates:
            for symbol, lesson in updates["symbol_lessons"].items():
                self._memory["symbol_lessons"][symbol] = lesson
                logger.info(f"[Agent-US] 更新标的教训: {symbol}")
        
        if "strategy_adjustments" in updates:
            for adj in updates["strategy_adjustments"]:
                self._memory["strategy_adjustments"].append({
                    "date": datetime.now().isoformat(),
                    "adjustment": adj
                })
        
        if "performance" in updates:
            self._memory["performance_tracking"].update(updates["performance"])
        
        self._save_memory()
    
    def _format_memory_for_prompt(self) -> str:
        """将记忆格式化为 prompt 上下文"""
        lines = [
            f"市场: 美股",
            f"核心原则: {', '.join(self._memory.get('core_principles', [])[:3])}",
            "",
            "美股交易法则:",
        ]
        for rule in self._memory.get("market_specific_rules", []):
            lines.append(f"  {rule}")
        
        perf = self._memory.get("performance_tracking", {})
        lines.append(f"")
        lines.append(f"美股性能: 交易{perf.get('total_trades', 0)}笔, 胜率{perf.get('win_rate', 0)*100:.0f}%")
        
        lessons = self._memory.get("historical_lessons", [])
        if lessons:
            lines.append(f"\n历史教训 ({len(lessons)} 条):")
            for l in lessons[-7:]:  # 最多保留 7 条
                lines.append(f"  - {l}")
        
        adjustments = self._memory.get("strategy_adjustments", [])
        if adjustments:
            lines.append(f"\n策略调整 (最近 {len(adjustments)} 条):")
            for a in adjustments[-3:]:  # 最多 3 条
                if isinstance(a, dict):
                    lines.append(f"  - {a.get('date', '')}: {a.get('adjustment', '')}")
                else:
                    lines.append(f"  - {a}")
        
        symbol_lessons = self._memory.get("symbol_lessons", {})
        if symbol_lessons:
            lines.append(f"\n标的特殊规律:")
            for sym, lesson in list(symbol_lessons.items())[:5]:
                lines.append(f"  {sym}: {lesson}")
        
        return "\n".join(lines)

    # ── P3 G14: Structured memory with tags, vector-ready format, similarity search ──

    def add_tagged_lesson(self, lesson: str, tags: List[str] = None,
                          session: str = "", symbol: str = "",
                          outcome: str = "", quality: float = 0.0):
        """Store a lesson with structured tags for future retrieval.

        Tags enable filtering by session, symbol, outcome type, and quality tier.
        """
        tags = tags or []
        if session:
            tags.append(f"session:{session}")
        if symbol:
            tags.append(f"symbol:{symbol}")
        if outcome:
            tags.append(f"outcome:{outcome}")

        entry = {
            "lesson": lesson,
            "tags": tags,
            "session": session,
            "symbol": symbol,
            "outcome": outcome,
            "quality": quality,
            "timestamp": datetime.now().isoformat(),
            "embedding_text": f"{' '.join(tags)}: {lesson}",
        }

        if "tagged_lessons" not in self._memory:
            self._memory["tagged_lessons"] = []
        self._memory["tagged_lessons"].append(entry)

        # Also keep in legacy list for backward compat
        if lesson not in self._memory["historical_lessons"]:
            self._memory["historical_lessons"].append(lesson)

        self._save_memory()
        logger.info(f"[Agent-US] 标签记忆已存储: tags={tags} lesson={lesson[:80]}")

    def search_lessons(self, query: str, top_k: int = 5,
                       filter_tags: List[str] = None) -> List[Dict]:
        """Keyword-based similarity search over tagged lessons.

        Splits query into tokens and scores each lesson by overlapping tokens.
        Vector-ready: each entry has an `embedding_text` field for future
        embedding-based semantic search.

        Args:
            query: Search query string
            top_k: Number of results to return
            filter_tags: Only return lessons matching ALL these tags

        Returns:
            List of matching entries, sorted by relevance score descending
        """
        tagged = self._memory.get("tagged_lessons", [])
        if not tagged:
            return []

        query_tokens = set(query.lower().split())

        scored = []
        for entry in tagged:
            # Tag filter
            if filter_tags:
                entry_tags = set(entry.get("tags", []))
                if not set(filter_tags).issubset(entry_tags):
                    continue

            # Simple token-overlap scoring
            text = (entry.get("embedding_text", "") + " " + entry.get("lesson", "")).lower()
            text_tokens = set(text.split())
            if not query_tokens:
                score = 0.0
            else:
                overlap = query_tokens & text_tokens
                score = len(overlap) / len(query_tokens)

            if score > 0:
                scored.append({**entry, "relevance": round(score, 3)})

        scored.sort(key=lambda x: x["relevance"], reverse=True)
        return scored[:top_k]

    def get_memory_chunks(self) -> List[Dict]:
        """Export memory as embeddable text chunks for vectorization.

        Each chunk contains:
        - text: ready-to-embed text
        - metadata: tags, session, symbol, timestamp
        - source: where this memory came from

        This is the feed for future vector DB indexing (ChromaDB/Milvus/Pinecone).
        """
        chunks = []

        # Tagged lessons
        for entry in self._memory.get("tagged_lessons", []):
            chunks.append({
                "text": entry.get("embedding_text", entry.get("lesson", "")),
                "metadata": {
                    "tags": entry.get("tags", []),
                    "session": entry.get("session", ""),
                    "symbol": entry.get("symbol", ""),
                    "timestamp": entry.get("timestamp", ""),
                },
                "source": "tagged_lesson",
            })

        # Symbol-specific lessons
        for sym, lesson in self._memory.get("symbol_lessons", {}).items():
            chunks.append({
                "text": f"symbol:{sym} lesson: {lesson}",
                "metadata": {"tags": [f"symbol:{sym}", "type:symbol_lesson"], "symbol": sym},
                "source": "symbol_lesson",
            })

        # Strategy adjustments
        for adj in self._memory.get("strategy_adjustments", []):
            if isinstance(adj, dict):
                text = adj.get("adjustment", "")
                date = adj.get("date", "")
            else:
                text = str(adj)
                date = ""
            chunks.append({
                "text": text,
                "metadata": {"tags": ["type:strategy_adjustment"], "date": date},
                "source": "strategy_adjustment",
            })

        # Core rules
        for rule in self._memory.get("market_specific_rules", []):
            chunks.append({
                "text": rule,
                "metadata": {"tags": ["type:market_rule"]},
                "source": "market_rule",
            })

        logger.info(f"[Agent-US] 导出 {len(chunks)} 个记忆块 (vector-ready)")
        return chunks

    def get_memory(self) -> Dict:
        """获取当前记忆"""
        return self._memory.copy()
    
    def _check_plan_response(self, request_id: str = None, poll_seconds: int = 600) -> Optional[Dict]:
        """
        检查是否有盘前计划的 Agent 响应
        
        Args:
            request_id: 精确匹配请求 ID (推荐，避免读到过期响应)
            poll_seconds: 轮询等待秒数 (默认 600 = 10 分钟)
                         盘前 08:30-09:25 共 55 分钟窗口，10 分钟占 18%，完全可接受。
                         Cron 每 5 分钟轮询 + LLM 处理 60-180s，最坏情况 ~8 分钟。
        """
        import glob, time
        
        deadline = time.time() + poll_seconds if poll_seconds > 0 else 0
        
        while True:
            # P0 修复 (2026-04-26): 精确匹配 request_id，不再用 glob 防止读到过期响应
            if request_id:
                exact_file = os.path.join(RESPONSES_DIR, f"{request_id}.json")
                response_files = [exact_file] if os.path.exists(exact_file) else []
            else:
                response_files = glob.glob(os.path.join(RESPONSES_DIR, "premarket_us_*.json"))
                if not response_files:
                    response_files = glob.glob(os.path.join(RESPONSES_DIR, "premarket_us_test_*.json"))
            
            if response_files:
                response_files.sort(reverse=True)
                latest = response_files[0]
                
                try:
                    with open(latest, 'r', encoding='utf-8') as f:
                        response = json.load(f)
                    result = response.get("result", {})
                    plan = result.get("plan", {})
                    
                    # 更新记忆
                    if "memory_updates" in result:
                        self._update_memory(result["memory_updates"])
                    
                    # 清理已处理的响应文件
                    os.remove(latest)
                    # 也清理对应的请求文件
                    request_id = response.get("request_id", "")
                    request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
                    if os.path.exists(request_file):
                        os.remove(request_file)
                    
                    logger.info(f"[Agent-US] 盘前计划响应已读取并清理 | poll={poll_seconds}s")
                    return plan
                except Exception as e:
                    logger.warning(f"[Agent-US] 读取盘前计划响应失败: {e}")
                    return None
            
            # 如果不需要轮询，直接返回
            if poll_seconds <= 0:
                return None
            
            # 检查超时
            if time.time() >= deadline:
                logger.warning(f"[Agent-US] 盘前计划响应超时 ( waited {poll_seconds}s)")
                return None
            
            time.sleep(5)
    
    def call_llm_direct(self, prompt: str, system_prompt: str = None,
                        round_num=None, timeout: int = 120,
                        model: str = None) -> Optional[Dict]:
        """
        公共 LLM 调用接口 (P1 修复: 替代私有 _call_llm_direct)
        
        稳定 API，供 ReviewOrchestrator 等外部模块使用
        """
        return self._call_llm_direct(
            prompt=prompt,
            system_prompt=system_prompt,
            round_num=round_num,
            timeout=timeout,
            model=model,
        )
    
    def _call_llm_direct(self, prompt: str, system_prompt: str = None,
                          round_num=None, timeout: int = 120,
                          model: str = None) -> Optional[Dict]:
        """
        v2.0升级: 直接调用 LLM — 2026-05-21 迁移到 UnifiedLLMService

        旧: 手动重试 + 模型降级 + 指数退避 (130+ 行)
        新: YAML llm.features.agent_bridge 路由 (primary: deepseek-pro, fallback: qwen-plus)
            model_call 已内置: 重试 + 降级 + 审计 + cache

        Args:
            prompt: 用户 prompt
            system_prompt: 系统 prompt (可选)
            round_num: 辩论轮次标识
            timeout: 超时秒数
            model: 模型名 (忽略, 由 YAML 路由)

        Returns:
            解析后的 JSON dict, 失败返回 None
        """
        from app.llm.unified_service import UnifiedLLMService
        res = UnifiedLLMService.call(
            "agent_bridge", prompt,
            system=system_prompt or "",
            temperature=0.3,
            max_tokens=16000,  # 2026-06-14: 4096→16000, 充分输出
            json_mode=True,
            timeout=timeout,
        )
        if not res or not res.success:
            logger.warning(f"[AgentBridge·LLM] 调用失败: {res.error if res else 'unknown'}")
            return None

        # JSON 解析 (model_call json_mode 已强制格式, 但仍需 parse)
        text = res.response or ""
        if isinstance(text, dict):
            return text  # 已被 schema 解析
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"[AgentBridge·LLM] JSON 解析失败: {e}, raw={text[:200]!r}")
            # P1 修复 (2026-05-22): 重试一次，限制 max_tokens 减少截断风险
            # 根因: LLM 响应过长 (1668-2664 chars) 被 provider 截断，JSON 不完整
            try:
                logger.info("[AgentBridge·LLM] 重试: 限制 max_tokens=2048 获取更短响应")
                res2 = UnifiedLLMService.call(
                    "agent_bridge", prompt + "\n\n[IMPORTANT] Keep your response concise. Limit each string value to 200 chars max.",
                    system=(system_prompt or "") + "\n[IMPORTANT] Return SHORT responses. Each string value ≤ 200 chars.",
                    temperature=0.3,
                    max_tokens=2048,
                    json_mode=True,
                    timeout=timeout,
                )
                if res2 and res2.success:
                    text2 = res2.response or ""
                    if isinstance(text2, dict):
                        return text2
                    text2 = text2.strip()
                    if text2.startswith("```json"):
                        text2 = text2[7:]
                    if text2.startswith("```"):
                        text2 = text2[3:]
                    if text2.endswith("```"):
                        text2 = text2[:-3]
                    text2 = text2.strip()
                    return json.loads(text2)
            except Exception as e2:
                logger.warning(f"[AgentBridge·LLM] 重试也失败: {e2}")
            return None

    # ── v3.0 HermesClient 集成 (Python 3.11 Worker) ──

    def _init_client(self) -> bool:
        """延迟初始化 HermesClient (仅成功一次)"""
        if self._client_available and self._client is not None:
            return True
        try:
            from app.llm.hermes_client import HermesClient
            self._client = HermesClient()
            if self._client.ping():
                self._client_available = True
                logger.info("[AgentBridge] HermesClient 已连接 (Python 3.11 Worker)")
                return True
            logger.warning("[AgentBridge] HermesClient ping 失败")
            return False
        except Exception as e:
            logger.warning("[AgentBridge] HermesClient 初始化失败: %s", e)
            self._client_available = False
            return False

    def call_agent_with_tools(self, prompt: str, system_prompt: str = None,
                              toolsets: List[str] = None,
                              timeout: int = 120) -> Optional[Dict]:
        """
        v3.0: 通过 Hermes Agent 调用 LLM (带工具)

        相比 _call_llm_direct 的优势:
        - Agent 可以自主使用 web_search/terminal/file 工具
        - 支持多轮工具调用迭代
        - 内置上下文管理

        降级: Worker 不可用时回退到 _call_llm_direct
        """
        if self._init_client():
            try:
                result = self._client.chat_with_tools(
                    prompt=prompt,
                    system=system_prompt or "",
                    toolsets=toolsets or ["web", "file"],
                    max_iterations=10,
                    timeout=timeout,
                )
                if result and result.get("final_response"):
                    return {
                        "response": result["final_response"],
                        "tokens": result.get("total_tokens", 0),
                        "mode": "hermes_agent",
                    }
            except Exception as e:
                logger.warning("[AgentBridge] call_agent_with_tools 异常: %s", e)

        # 降级到 Bailian 直连
        logger.info("[AgentBridge] 降级到 Bailian 直连 (无工具)")
        fallback = self._call_llm_direct(prompt=prompt, system_prompt=system_prompt, timeout=timeout)
        if fallback:
            fallback["mode"] = "fallback_bailian"
            fallback["response"] = fallback.get("result", "") if isinstance(fallback, dict) else str(fallback)
        return fallback

    def call_agent_debate(self, topic: str, debate_prompt: str = "",
                          roles: List[Dict] = None,
                          timeout: int = 300) -> Optional[Dict]:
        """
        v3.0: 多角色辩论 + 终审

        并行运行多个分析师角色，最后法官综合。
        """
        if self._init_client():
            try:
                result = self._client.debate(
                    topic=topic,
                    debate_prompt=debate_prompt or topic,
                    roles=roles,
                    timeout=timeout,
                )
                if result:
                    return {
                        "verdict": result.get("final_response", ""),
                        "analysts": result.get("analysts", []),
                        "tokens": result.get("total_tokens", 0),
                        "mode": "hermes_debate",
                    }
            except Exception as e:
                logger.warning("[AgentBridge] call_agent_debate 异常: %s", e)

        # 降级: 单模型调用
        logger.info("[AgentBridge] 降级为单模型分析")
        fallback = self._call_llm_direct(
            prompt=debate_prompt or topic,
            system_prompt="你是美股分析师，给出你的独立判断。",
            timeout=timeout,
        )
        if fallback:
            return {
                "verdict": fallback.get("response", str(fallback)),
                "analysts": [],
                "tokens": 0,
                "mode": "fallback_single",
            }
        return None

    # ── 原有 _call_llm_direct (Bailian 直连, 降级保留) ──

    def _check_eod_response(self, request_id: str = None, poll_seconds: int = 600) -> Optional[Dict]:
        """
        检查是否有盘后归因的 Agent 响应
        
        Args:
            request_id: 精确匹配请求 ID (推荐，避免读到过期响应)
            poll_seconds: 轮询等待秒数 (默认 600 = 10 分钟)
                         盘后无时间压力，10 分钟绰绰有余。
        """
        import glob, time
        
        deadline = time.time() + poll_seconds if poll_seconds > 0 else 0
        
        while True:
            # P0 修复 (2026-04-26): 精确匹配 request_id
            if request_id:
                exact_file = os.path.join(RESPONSES_DIR, f"{request_id}.json")
                response_files = [exact_file] if os.path.exists(exact_file) else []
            else:
                response_files = glob.glob(os.path.join(RESPONSES_DIR, "eod_us_*.json"))
            if response_files:
                response_files.sort(reverse=True)
                latest = response_files[0]
                
                try:
                    with open(latest, 'r', encoding='utf-8') as f:
                        response = json.load(f)
                    result = response.get("result", {})
                    review = result.get("review", {})
                    
                    # 更新记忆
                    if "memory_updates" in result:
                        self._update_memory(result["memory_updates"])
                    
                    # 清理
                    os.remove(latest)
                    request_id = response.get("request_id", "")
                    request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
                    if os.path.exists(request_file):
                        os.remove(request_file)
                    
                    logger.info(f"[Agent-US] EOD 归因响应已读取并清理 | poll={poll_seconds}s")
                    return review
                except Exception as e:
                    logger.warning(f"[Agent-US] 读取 EOD 响应失败: {e}")
                    return None
            
            if poll_seconds <= 0:
                return None
            
            if time.time() >= deadline:
                logger.warning(f"[Agent-US] EOD 响应超时 (waited {poll_seconds}s)")
                return None
            
            time.sleep(5)
    
    def update_performance(self, total_trades: int, win_rate: float,
                           avg_pnl: float, max_drawdown: float):
        """更新性能追踪"""
        self._memory["performance_tracking"].update({
            "total_trades": total_trades,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "max_drawdown": max_drawdown
        })
        self._save_memory()


# 全局单例
_agent_bridge: Optional[FTSeekUSAgentBridge] = None


def get_agent_bridge() -> FTSeekUSAgentBridge:
    """获取全局 Agent 桥接实例"""
    global _agent_bridge
    if _agent_bridge is None:
        _agent_bridge = FTSeekUSAgentBridge()
    return _agent_bridge
