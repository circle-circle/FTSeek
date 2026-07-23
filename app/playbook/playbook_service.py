"""
Playbook Service — 编排执行、反馈收集、查询

核心职责:
  1. 执行 Playbook 指令 (通过 ManualOrderService 下单)
  2. 注册 ManualHoldRegistry (防止 auto-pipeline 干扰)
  3. 收集用户反馈 (跳过原因、参数修改)
  4. 更新 playbook_instructions DB 执行状态
  5. 提供 Dashboard 查询接口

Usage:
  from app.playbook.playbook_service import get_playbook_service

  svc = get_playbook_service()
  instructions = svc.get_today_instructions()
  result = svc.execute_instruction(instruction_id)
  svc.skip_instruction(instruction_id, reason="风险太大")
"""
import logging
import json
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, date

from app.playbook.db import PlaybookDB
from app.utils.timezone import now_et

logger = logging.getLogger(__name__)

# Singleton
_service_instance = None

# pipeline_view TTL 缓存 (秒). 盘中数据每 5min 才变, 30s 缓存挡住高频轮询打 DB.
_PV_TTL = 30


def _parse_json_field(val: Any, default: Any) -> Any:
    """安全解析 JSON 字段 (conditions_json / data_quality_json 可能是 str 或已解析)."""
    if val is None:
        return default
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (ValueError, TypeError):
        return default


def _extract_atr_from_instruction(inst: Dict[str, Any]) -> float:
    """从 playbook 指令提取 ATR 数值, 写入 trade_records.metadata_json.atr (诊断用).

    2026-06-24 修复: playbook_instructions 表无 atr_source 顶层列 (schema 见 db.py),
    atr_source 嵌在 raw_instruction_json blob 里 (instruction_generator 产出). 此前
    inst.get("atr_source","") 永远返回 "" → "=" not in "" → metadata.atr 恒为 0,
    导致事后复盘"SL 距离是否合理"无法诊断. 注意: 退出逻辑本身不读此值 (退出用实时
    market_data.atr), 故此前 bug 仅影响可观测性, 不影响执行.

    2026-07-16 修复: DA bridge 直写 instruction 时无 atr_source 字段 → metadata.atr=0.
    修复: atr_source 缺失时, 从 entry_price 和 stop_loss 反算隐含 ATR% (SL 距离 / vol_bucket 倍数).
    虽然不是真实 ATR, 但反映了 vol_bucket 级别, 供事后诊断 SL 合理性.

    atr_source 格式 (来自 instruction_generator):
      "technical.atr_14=8.35 (quality=good)"
      "daily_kline.atr_14=8.35 (quality=good)"
      "FALLBACK: price×2%=8.35 (ATR missing)"
    """
    # 1. 顶层 atr_source (前向兼容: 若未来加列可直接命中)
    src = inst.get("atr_source") or ""
    if "=" not in src:
        # 2. 嵌在 raw_instruction_json 里 — 解析出来
        raw = inst.get("raw_instruction_json")
        if raw:
            try:
                src = (json.loads(raw) if isinstance(raw, str) else raw).get("atr_source") or ""
            except (ValueError, TypeError):
                src = ""
    if src and "=" in src:
        try:
            raw_val = src.split("=", 1)[1].split(" ")[0]
            return float(raw_val.rstrip("%"))
        except (ValueError, IndexError):
            pass
    
    # 3. 2026-07-16: atr_source 缺失 → 从 SL 距离反算隐含 ATR%
    #   vol_bucket: sl_pct = max(sl_floor, min(sl_ceil, atr_pct * 100 * sl_multiplier))
    #   简化: 用 SL 距离 / entry_price 作为 ATR% 的近似 (诊断用, 非精确值)
    entry = float(inst.get("entry_price", 0) or 0)
    sl = float(inst.get("stop_loss", 0) or 0)
    if entry > 0 and sl > 0:
        sl_pct = abs(entry - sl) / entry * 100
        # 反推 ATR: medium bucket sl_multiplier=1.5 → atr_pct ≈ sl_pct / 1.5
        # 但无法确定 bucket, 用 sl_pct 本身作为 atr% 近似 (保守上界)
        return round(entry * sl_pct / 100, 2)  # 返回隐含 ATR 绝对值
    return 0.0



def get_playbook_service():
    """获取 PlaybookService 单例"""
    global _service_instance
    if _service_instance is None:
        _service_instance = PlaybookService()
    return _service_instance


class PlaybookService:
    """Playbook 业务服务层"""

    def __init__(self, playbook_db: PlaybookDB = None):
        self.db = playbook_db or PlaybookDB()
        self.db.ensure_tables()
        # pipeline_view TTL 缓存 (按 trade_date 键控)
        self._pv_cache: Optional[Dict[str, Any]] = None
        self._pv_ts: float = 0.0
        self._pv_key: Optional[str] = None

    # ========== 查询接口 ==========

    def get_today_instructions(self, trade_date: str = None) -> List[Dict]:
        """获取今日交易指令"""
        if trade_date is None:
            trade_date = self._et_today()
        instructions = self.db.get_instructions_by_date(trade_date)
        # 附加用户反馈
        for inst in instructions:
            feedbacks = self.db.get_user_feedback(inst["id"])
            inst["user_feedback"] = feedbacks
        return instructions

    def get_instruction_detail(self, instruction_id: int) -> Optional[Dict]:
        """获取指令详情 (含反馈)"""
        # 通过 recent instructions 查找
        instructions = self.db.get_recent_instructions(limit=200)
        for inst in instructions:
            if inst["id"] == instruction_id:
                inst["user_feedback"] = self.db.get_user_feedback(inst["id"])
                return inst
        return None

    def get_playbook_summary(self, days: int = 30) -> Dict[str, Any]:
        """获取 Playbook 汇总统计"""
        stats = self.db.get_summary_stats(days)
        regime_log = self.db.get_regime_log(days=5)
        today_instructions = self.get_today_instructions()

        # 按方向分类
        buy_count = sum(1 for i in today_instructions if i.get("direction") == "BUY")
        short_count = sum(1 for i in today_instructions if i.get("direction") == "SELL_SHORT")
        skip_count = sum(1 for i in today_instructions if i.get("direction") == "SKIP")
        executed_count = sum(1 for i in today_instructions if i.get("executed"))

        return {
            **stats,
            "today_date": self._et_today(),
            "today_instructions_count": len(today_instructions),
            "today_buy_count": buy_count,
            "today_short_count": short_count,
            "today_skip_count": skip_count,
            "today_executed_count": executed_count,
            "recent_regime": regime_log[0] if regime_log else None,
            "active_patterns": self._get_active_patterns_summary(),
        }

    def get_history(self, days: int = 30) -> List[Dict]:
        """获取历史指令"""
        return self.db.get_recent_instructions(limit=days * 10)

    def get_factor_stats(self) -> Dict[str, List[Dict]]:
        """获取因子统计"""
        return {
            "single": self.db.get_factor_stats(level="single"),
            "pair": self.db.get_factor_stats(level="pair"),
        }

    def get_patterns(self, status: str = None) -> List[Dict]:
        """获取 patterns"""
        return self.db.get_patterns(status=status)

    # ========== Pipeline 可视化 ==========

    def get_pipeline_view(self, trade_date: str = None) -> Dict[str, Any]:
        """聚合管道各阶段数据, 供 Dashboard 可视化 (带 30s TTL 缓存).

        数据流: 候选池(factor_screen) → 投票(signal_snapshots) → 指令(playbook_instructions)
        附: 因子IC(factor_ic_latest) + 方向审计(playbook_direction_audit.jsonl)

        Returns:
            {date, regime, funnel, factor_ic, items: [...], served_at}
        """
        # 缓存键: 输入日期; None → ET 今日. (回退到最近交易日在 _build 内发生, TTL 内稳定)
        cache_key = trade_date or self._et_today()
        if (self._pv_cache is not None and self._pv_key == cache_key
                and (time.time() - self._pv_ts) < _PV_TTL):
            return self._pv_cache

        view = self._build_pipeline_view(trade_date)
        view["served_at"] = datetime.now().isoformat()
        self._pv_cache = view
        self._pv_ts = time.time()
        self._pv_key = cache_key
        return view

    def _build_pipeline_view(self, trade_date: str = None) -> Dict[str, Any]:
        """实际组装 pipeline_view (无缓存). 由 get_pipeline_view 包装调用."""
        if trade_date is None:
            trade_date = self._et_today()

        # ① playbook_instructions — 最终指令
        instructions = self.db.get_instructions_by_date(trade_date)

        # 2026-07-04: 默认日期无指令时(假日/盘前), 自动回退到最近有数据的交易日
        if not instructions:
            recent = self.db.get_recent_instructions(limit=1)
            if recent:
                trade_date = recent[0].get("date", trade_date)
                instructions = self.db.get_instructions_by_date(trade_date)

        # ② signal_snapshots — 投票结果 (按 symbol 索引)
        vote_map = self._get_vote_map(trade_date)

        # ③ factor_screen_latest.json — 候选池 + 因子评分
        #   candidates[] 仅 top_n 截断 (默认8), 非真实打分池; 漏斗"候选池"须用 scored_count
        #   (实际打分标的数, 如43), 否则会出现 候选池(8) < 投票(37) 的逻辑倒挂.
        screen_data = self._load_json_file("data/factor_screen_latest.json") or {}
        screen_map = {}
        if screen_data.get("candidates"):
            for c in screen_data["candidates"]:
                sym = c.get("symbol", "")
                if sym:
                    screen_map[sym] = c
        screen_scored = int(screen_data.get("scored_count") or screen_data.get("pool_size") or len(screen_map))
        screen_date = str(screen_data.get("timestamp", ""))[:10]

        # ④ factor_ic_latest.json — 因子 IC
        factor_ic = self._load_json_file("data/factor_ic_latest.json") or {}

        # ⑤ direction_audit.jsonl — LLM vs 技术方向
        audit_map = self._get_audit_map(trade_date)

        # ⑥ signal_context — 信息层 (新闻/内部人/财报/基本面/主题雷达/LLM reasoning)
        context_map = {}
        try:
            contexts = self.db.get_signal_contexts_by_date(trade_date)
            context_map = {c.get("symbol", ""): c for c in contexts if c}
        except Exception as e:
            logger.debug(f"[playbook] signal_context 查询失败: {e}")

        # 组装 items
        items = []
        for inst in instructions:
            sym = inst.get("symbol", "")
            vote = vote_map.get(sym, {})
            screen = screen_map.get(sym, {})
            audit = audit_map.get(sym, {})

            # 解析 conditions
            conditions = inst.get("conditions_json")
            if isinstance(conditions, str):
                import json as _json
                try:
                    conditions = _json.loads(conditions)
                except Exception:
                    conditions = []
            if not isinstance(conditions, list):
                conditions = conditions or []

            # 解析 data_quality
            dq = inst.get("data_quality_json")
            if isinstance(dq, str):
                import json as _json
                try:
                    dq = _json.loads(dq)
                except Exception:
                    dq = {}
            if not isinstance(dq, dict):
                dq = dq or {}

            # 解析 raw_instruction_json
            raw = inst.get("raw_instruction_json")
            if isinstance(raw, str):
                import json as _json
                try:
                    raw = _json.loads(raw)
                except Exception:
                    raw = {}
            if not isinstance(raw, dict):
                raw = raw or {}

            # 该标的触发的因子 IC
            trigger_factors = conditions or (vote.get("factors_json") or [])
            if isinstance(trigger_factors, str):
                import json as _json
                try:
                    trigger_factors = _json.loads(trigger_factors)
                except Exception:
                    trigger_factors = []
            if not isinstance(trigger_factors, list):
                # dict / 其他类型 → 前端 factors.map 会崩; 统一降级为 list
                trigger_factors = list(trigger_factors.keys()) if isinstance(trigger_factors, dict) else []
            factor_ic_for_symbol = {}
            for f in trigger_factors:
                # factors 可能是 "theme_momentum:+0.20" 格式, 取冒号前
                f_key = f.split(":")[0] if isinstance(f, str) else str(f)
                if f_key in factor_ic:
                    factor_ic_for_symbol[f_key] = factor_ic[f_key]

            # 来源归因: DA (da_bridge) vs 宇宙扩张 (universe_expansion) vs 其他.
            #   优先取 raw_instruction_json.source (UE inject 显式写入), 回退按 decision_id 前缀判定.
            _did = str(inst.get("decision_id", "") or "")
            _src = str(raw.get("source") or "").strip()
            if not _src:
                if _did.startswith("DA-"):
                    _src = "da_bridge"
                elif _did.startswith("universe_expansion"):
                    _src = "universe_expansion"
                elif _did:
                    _src = "internal"
                else:
                    _src = "unknown"

            items.append({
                "symbol": sym,
                "source": _src,
                "direction": inst.get("direction", ""),
                "confidence": inst.get("confidence", 0),
                "sector": inst.get("sector", ""),
                "thesis": inst.get("thesis", ""),
                "entry_price": inst.get("entry_price", 0),
                "stop_loss": inst.get("stop_loss", 0),
                "take_profit": inst.get("take_profit", 0),
                "position_size": inst.get("position_size", 0),
                "risk_reward": inst.get("risk_reward", 0),
                "conditions": conditions,
                "data_quality": dq,
                "ok_dimensions": inst.get("ok_dimensions", 0),
                "market_regime": inst.get("market_regime", ""),
                "vix_at_generation": inst.get("vix_at_generation", 0),
                "skip_reason": inst.get("skip_reason", ""),
                "executed": bool(inst.get("executed", 0)),
                "outcome": inst.get("outcome", ""),
                "atr_source": raw.get("atr_source", ""),
                "vote": {
                    "direction": vote.get("direction", ""),
                    "strength": vote.get("strength", 0),
                    "confidence": vote.get("confidence", 0),
                    "consensus_level": vote.get("consensus_level", 0),
                    "decision_id": vote.get("decision_id", ""),
                    "factors": trigger_factors,
                } if vote else None,
                "audit": {
                    "llm_direction": audit.get("llm_direction", ""),
                    "technical_direction": audit.get("technical_direction", ""),
                    "llm_strength": audit.get("llm_strength", 0),
                    "llm_confidence": audit.get("llm_confidence", 0),
                    "llm_std": audit.get("llm_std", 0),
                } if audit else None,
                "factor_scores": screen.get("factors", {}),
                "composite_score": screen.get("composite_score", 0),
                "composite_score_long": screen.get("composite_score_long", 0),
                "composite_score_short": screen.get("composite_score_short", 0),
                "factor_ic": factor_ic_for_symbol,
                # 信息层上下文 (brief缺失时用fallback)
                "signal_context": self._get_signal_context_or_fallback(context_map.get(sym, {}), inst),
            })

        # 漏斗统计
        vote_directions = [v.get("direction", "") for v in vote_map.values()]
        inst_directions = [i.get("direction", "") for i in instructions]
        funnel = {
            "candidates": screen_scored,
            "candidates_scored": screen_scored,
            "screen_date": screen_date,
            "screen_stale": bool(screen_date and screen_date != trade_date),
            "voted": len(vote_map),
            "vote_long": sum(1 for d in vote_directions if d == "LONG"),
            "vote_short": sum(1 for d in vote_directions if d == "SHORT"),
            "vote_neutral": sum(1 for d in vote_directions if d == "NEUTRAL"),
            "buy": sum(1 for d in inst_directions if d == "BUY"),
            "sell_short": sum(1 for d in inst_directions if d == "SELL_SHORT"),
            "skip": sum(1 for d in inst_directions if d == "SKIP"),
        }

        # regime
        regime_log = self.db.get_regime_log(days=1)
        regime = regime_log[0] if regime_log else {}
        if not regime:
            # fallback: 从 instructions 取
            for inst in instructions:
                if inst.get("market_regime"):
                    regime = {
                        "regime": inst.get("market_regime", ""),
                        "vix": inst.get("vix_at_generation", 0),
                    }
                    break

        return {
            "date": trade_date,
            "regime": regime,
            "funnel": funnel,
            "factor_ic": factor_ic,
            "items": items,
            # Deepulse 主题雷达 (全局, 非per-symbol)
            "theme_radar": self._load_theme_radar(),
        }

    def _get_signal_context_or_fallback(self, ctx_row: Dict, inst: Dict) -> Optional[Dict]:
        """signal_context有brief就用, 否则从instruction构造fallback brief."""
        ctx = self._format_signal_context(ctx_row)
        if ctx and ctx.get("brief"):
            return ctx
        # brief缺失 → fallback
        fallback = self._build_fallback_brief(inst, inst.get("symbol", ""))
        if fallback:
            # 合并: 保留ctx中已有的信息层字段 + 注入fallback brief
            if ctx:
                ctx["brief"] = fallback["brief"]
                return ctx
            return fallback
        return ctx

    @staticmethod
    def _build_fallback_brief(inst: Dict, symbol: str) -> Optional[Dict]:
        """signal_context 缺失时, 从 instruction 自身字段构造 fallback brief.

        不含LLM分析, 只展示已有的entry/SL/TP/conditions/data_quality.
        让前端卡片在任何情况下都能展示策略参数.
        """
        if not inst:
            return None
        entry = inst.get("entry_price") or 0
        sl = inst.get("stop_loss") or 0
        tp = inst.get("take_profit") or 0
        direction = inst.get("direction", "")
        thesis = inst.get("thesis", "")

        # 关键位: entry/SL/TP → 支撑/阻力
        key_levels = {}
        if direction == "BUY":
            if sl > 0:
                key_levels["support"] = [round(sl, 2)]
            if tp > 0:
                key_levels["resistance"] = [round(tp, 2)]
        elif direction == "SELL_SHORT":
            if tp > 0:
                key_levels["support"] = [round(tp, 2)]
            if sl > 0:
                key_levels["resistance"] = [round(sl, 2)]

        # 入场策略: 从direction + conditions推导
        conditions = inst.get("conditions") or []
        cond_str = ", ".join(conditions[:3]) if conditions else ""
        if direction == "BUY":
            entry_strategy = f"BUY @{entry:.2f}" if entry > 0 else "BUY"
            if cond_str:
                entry_strategy += f" ({cond_str})"
        elif direction == "SELL_SHORT":
            entry_strategy = f"SHORT @{entry:.2f}" if entry > 0 else "SHORT"
            if cond_str:
                entry_strategy += f" ({cond_str})"
        else:
            entry_strategy = None

        # 数据完整度: fallback时标注缺失
        dq_list = []
        if not inst.get("thesis"):
            dq_list.append("thesis:missing")
        # signal_context的6维度数据在fallback时不可用
        dq_list.append("LLM brief:待7/7盘前生成")

        brief = {
            "conclusion": thesis[:80] if thesis else f"{direction} @{entry:.2f}" if entry > 0 else direction,
            "reasons": conditions[:3] if conditions else [],
            "risks": [],
            "entry_strategy": entry_strategy,
            "key_support": round(sl, 2) if sl > 0 else None,
            "key_resistance": round(tp, 2) if tp > 0 else None,
            "stop_trigger": f"SL @{sl:.2f}" if sl > 0 else None,
            "key_levels": key_levels,
            "data_quality": dq_list,
        }
        return {"brief": brief}

    @staticmethod
    def _format_signal_context(ctx: Dict) -> Optional[Dict]:
        """格式化 signal_context DB 行 → 前端友好的 dict."""
        if not ctx:
            return None
        import json as _json

        def _parse(val):
            if val is None:
                return None
            if isinstance(val, (list, dict)):
                return val
            try:
                return _json.loads(val)
            except Exception:
                return None

        # 2026-07-04: 策略简报字段 (从raw_data_json提取, 不加DB新列)
        _raw_parsed = _parse(ctx.get("raw_data_json"))
        _raw_data = _raw_parsed if isinstance(_raw_parsed, dict) else {}
        # 2026-07-04: 数据完整度 (从各维度quality字段提取)
        _dq_summary = []
        for _dim, _key in [("fundamental", "fundamental"), ("technical", "technical"),
                           ("insider", "insider"), ("news", "news"),
                           ("earnings", "earnings"), ("sentiment", "sentiment")]:
            _dim_data = _raw_data.get(_dim, {})
            if isinstance(_dim_data, dict):
                _q = _dim_data.get("quality", "missing")
                _src = _dim_data.get("source", "")
                _dq_summary.append(f"{_dim}:{_q}" + (f"/{_src}" if _src else ""))
        _brief = None
        if _raw_data.get("brief_conclusion") or _raw_data.get("key_levels"):
            _brief = {
                "conclusion": _raw_data.get("brief_conclusion"),
                "reasons": _raw_data.get("brief_reasons") or [],
                "risks": _raw_data.get("brief_risks") or [],
                "entry_strategy": _raw_data.get("entry_strategy"),
                "key_support": _raw_data.get("key_support"),
                "key_resistance": _raw_data.get("key_resistance"),
                "stop_trigger": _raw_data.get("stop_trigger"),
                "key_levels": _raw_data.get("key_levels") or {},
                "data_quality": _dq_summary,
            }

        return {
            "news_headlines": _parse(ctx.get("news_headlines_json")) or [],
            "news_sentiment": ctx.get("news_sentiment"),
            "insider": {
                "signal": ctx.get("insider_signal"),
                "cluster_score": ctx.get("insider_cluster_score"),
                "net_shares": ctx.get("insider_net_shares"),
                "cluster_buyers": _parse(ctx.get("insider_cluster_buyers_json")) or [],
            } if ctx.get("insider_signal") else None,
            "earnings": {
                "next_date": ctx.get("earnings_next_date"),
                "days_until": ctx.get("earnings_days_until"),
                "beat_rate": ctx.get("earnings_beat_rate"),
            } if ctx.get("earnings_next_date") else None,
            "fundamental": {
                "pe": ctx.get("pe"),
                "market_cap": ctx.get("market_cap"),
                "industry": ctx.get("industry"),
                "revenue_growth_yoy": ctx.get("revenue_growth_yoy"),
                "roe": ctx.get("roe"),
                "debt_to_equity": ctx.get("debt_to_equity"),
                "high_52w": ctx.get("high_52w"),
                "low_52w": ctx.get("low_52w"),
            } if ctx.get("pe") else None,
            "theme": {
                "momentum": ctx.get("theme_momentum"),
                "wind": ctx.get("theme_wind"),
                "narratives": _parse(ctx.get("theme_narratives_json")) or [],
            } if ctx.get("theme_momentum") is not None else None,
            "llm_reasoning": _parse(ctx.get("llm_reasoning_json")) or [],
            "brief": _brief,
        }

    @staticmethod
    def _load_theme_radar() -> Optional[Dict]:
        """加载 Deepulse 主题雷达 (theme_context.json)."""
        data = PlaybookService._load_json_file("data/theme_context.json")
        if not data:
            return None
        domains = data.get("domains") or {}
        result = {}
        for name, d in domains.items():
            result[name] = {
                "momentum": d.get("theme_momentum"),
                "sentiment": d.get("sentiment_direction"),
                "breadth": d.get("breadth"),
                "narratives": d.get("key_narratives", [])[:3],
                "beneficiaries": d.get("beneficiaries", [])[:5],
                "detractors": d.get("detractors", [])[:5],
                "confidence": d.get("confidence"),
            }
        return {"date": data.get("date"), "domains": result}

    @staticmethod
    def _load_json_file(rel_path: str) -> Optional[Dict]:
        """安全加载 JSON 文件 (相对于项目根目录)."""
        try:
            from pathlib import Path
            root = Path(__file__).resolve().parent.parent.parent
            with open(root / rel_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"[playbook] 加载 {rel_path} 失败: {e}")
            return None

    @staticmethod
    def _get_vote_map(trade_date: str) -> Dict[str, Dict]:
        """从 signal_snapshots.db 读取某日投票结果, 按 symbol 索引."""
        try:
            from app.analysis.signal_ic_tracker import _get_db_path as _get_snap_path
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(str(_get_snap_path()))
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                "SELECT decision_id, symbol, direction, strength, confidence, "
                "consensus_level, strategy_id, factors_json "
                "FROM signal_snapshots WHERE date(trade_date) = ? "
                "ORDER BY strength DESC",
                (trade_date,),
            ).fetchall()
            conn.close()
            result = {}
            for r in rows:
                d = dict(r)
                sym = d.get("symbol", "")
                # 去重: 同一 symbol 取 strength 最高的 (ORDER BY 已保证)
                if sym and sym not in result:
                    result[sym] = d
            return result
        except Exception as e:
            logger.warning(f"[playbook] signal_snapshots 查询失败: {e}")
            return {}

    @staticmethod
    def _get_audit_map(trade_date: str) -> Dict[str, Dict]:
        """从 playbook_direction_audit.jsonl 读取方向审计, 按 symbol 索引."""
        try:
            from pathlib import Path
            root = Path(__file__).resolve().parent.parent.parent
            log_path = root / "data" / "shadow" / "playbook_direction_audit.jsonl"
            if not log_path.exists():
                return {}
            result = {}
            with open(log_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        ts = rec.get("timestamp", "")
                        if trade_date in ts:
                            sym = rec.get("symbol", "")
                            if sym:
                                result[sym] = rec
                    except (json.JSONDecodeError, TypeError):
                        continue
            return result
        except Exception as e:
            logger.debug(f"[playbook] 方向审计加载失败: {e}")
            return {}

    # ========== 执行接口 ==========

    def execute_instruction(
        self,
        instruction_id: int,
        acknowledge_risk: bool = False,
        custom_params: Dict = None,
    ) -> Dict[str, Any]:
        """
        执行一条 Playbook 指令

        流程:
        1. 查找指令
        2. 校验 (方向、价格合理性)
        3. 通过 ManualOrderService 下单
        4. 注册 ManualHoldRegistry
        5. 更新 DB (executed=1, execution_price, execution_time)

        Args:
            instruction_id: 指令 ID
            acknowledge_risk: 用户确认风险
            custom_params: 用户自定义参数 (如修改止损)

        Returns:
            {"success": bool, "message": str, "order_result": dict}
        """
        # 1. 查找指令
        inst = self._find_instruction_by_id(instruction_id)
        if not inst:
            return {"success": False, "message": "指令不存在"}

        if inst.get("executed"):
            return {"success": False, "message": "指令已执行"}

        if inst.get("direction") == "SKIP":
            return {"success": False, "message": "SKIP 指令不可执行"}

        # 2026-06-25: 账户级熔断 guard (OKR)
        # 若当日亏损 ≥2% 停止新仓, ≥3% 强制全平.
        try:
            from app.risk.account_circuit_breaker import check_circuit_breaker
            from app.risk.capital_safety import get_capital_safety_system
            _cs = get_capital_safety_system()
            _equity = float(getattr(_cs._state, 'current_equity', 0) or 0)
            if _equity > 0:
                _cb = check_circuit_breaker(_equity)
                if _cb.action in ("HALT", "LIQUIDATE"):
                    _msg = f"[OKR熔断] {_cb.reason}, 暂停 Playbook 执行"
                    logger.warning(_msg)
                    return {"success": False, "message": _msg}
        except Exception as _cb_err:
            # DEB-20260721 R4 GATE-1: 纳入 risk.acb_fail_mode 统一 fail 语义 (C-7 的第三份拷贝).
            #   当前 mode=closed → 检查失明即拒绝开仓 (行为变更, 与批次② C-7 裁决同向:
            #   失明的熔断器=没有熔断器). 回滚: acb_fail_mode 改回 open.
            try:
                from app.core.config_container import get_config as _get_cfg_okr
                _okr_fail_mode = _get_cfg_okr().get_dotted("risk.acb_fail_mode", "open")
            except Exception:
                _okr_fail_mode = "open"
            if _okr_fail_mode == "closed":
                logger.error(f"[playbook] OKR熔断检查异常 (fail-close 拒绝开仓): {_cb_err}")
                return {"success": False, "message": f"OKR熔断检查异常 (fail-close): {_cb_err}"}
            logger.warning("[playbook] 熔断检查异常 (放行): %s", _cb_err)

        # 切实盘前风控接线: 绝对熔断硬门 (连亏5笔/日亏$2K/时亏$1K).
        #   此前 can_open_new_position 从未在 Playbook 路径调用 → ACB 形同虚设.
        #   A1+ (2026-07-19): fail 模式由配置 risk.acb_fail_mode 驱动 (open|closed, 默认 open).
        #   live 切换时翻 closed, 符合"可回滚、可配置化"铁律.
        try:
            from app.risk.absolute_circuit_breaker import get_absolute_circuit_breaker
            _allowed, _acb_reason = get_absolute_circuit_breaker().can_open_new_position("playbook")
            if not _allowed:
                logger.warning(f"[绝对熔断] {_acb_reason}, 拒绝开仓")
                return {"success": False, "message": f"[绝对熔断] {_acb_reason}"}
        except Exception as _acb_err:
            # A1+: 配置驱动 fail 模式 (open=放行, closed=拒绝)
            try:
                from app.core.config_container import get_config as _get_cfg_acb
                _acb_fail_mode = _get_cfg_acb().get_dotted("risk.acb_fail_mode", "open")
            except Exception:
                _acb_fail_mode = "open"
            if _acb_fail_mode == "closed":
                logger.error(f"[playbook] 绝对熔断检查异常 (fail-close 拒绝开仓): {_acb_err}")
                return {"success": False, "message": f"绝对熔断检查异常 (fail-close): {_acb_err}"}
            logger.warning("[playbook] 绝对熔断检查异常 (放行): %s", _acb_err)

        # 读取下单模式: 统一走 broker 真实下单 (futu_simulate / live), 禁止纯虚拟 shadow.
        # simulate (shadow) 模式已废弃, 如果配置误设为 simulate, 强制降级为 futu_simulate.
        try:
            from app.core.config_container import get_config
            _exec_mode = get_config().get_dotted("playbook.execute_mode", "futu_simulate")
        except Exception:
            _exec_mode = "futu_simulate"

        symbol = inst["symbol"]
        direction = inst["direction"]

        if _exec_mode == "simulate":
            logger.warning(f"[playbook] execute_mode=simulate 已废弃, {symbol} 强制降级为 futu_simulate (真实模拟下单)")
            _exec_mode = "futu_simulate"

        entry_price = custom_params.get("entry_price") if custom_params else inst.get("entry_price", 0)
        stop_loss = custom_params.get("stop_loss") if custom_params else inst.get("stop_loss", 0)
        take_profit = custom_params.get("take_profit") if custom_params else inst.get("take_profit", 0)
        position_size = custom_params.get("position_size") if custom_params else inst.get("position_size", 0)
        if not position_size or position_size <= 0:
            position_size = 1

        # 方向映射
        mos_direction = "LONG" if direction == "BUY" else "SHORT"

        # 关联投票决策 ID (用于 IC 追踪)
        # 优先用 playbook_instructions 表中已存储的 decision_id (由 VotingCoordinator 写入)
        # fallback: 查 signal_snapshots (兼容旧数据)
        _matched_decision_id = inst.get("decision_id", "") or ""
        if not _matched_decision_id:
            try:
                from app.analysis.signal_ic_tracker import _get_db_path as _get_snap_path
                import sqlite3 as _sqlite3
                _snap_conn = _sqlite3.connect(str(_get_snap_path()))
                _snap_conn.row_factory = _sqlite3.Row
                _trade_date = inst.get("date", "")
                _row = _snap_conn.execute(
                    "SELECT decision_id FROM signal_snapshots WHERE symbol = ? AND date(trade_date) = ? ORDER BY strength DESC LIMIT 1",
                    (symbol, _trade_date),
                ).fetchone()
                if _row:
                    _matched_decision_id = _row["decision_id"]
                    logger.info(f"[playbook] matched decision_id={_matched_decision_id} for {symbol} (from signal_snapshots fallback)")
                _snap_conn.close()
            except Exception as _e:
                logger.debug(f"[playbook] decision_id lookup failed: {_e}")

        # ── broker 路径 (futu_simulate / live 统一): target_env 由 AUTO_TRADE_ACCOUNT 决定 ──
        # 2026-06-24 修复: 之前 futu_simulate/live 各写死 target_env, 与 switch_mode (写 AUTO_TRADE_ACCOUNT)
        # 不通 → 系统页切实盘时 playbook 仍在 sim. 现统一: target_env = AUTO_TRADE_ACCOUNT (sim|real).
        import os as _os
        _auto_account = _os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()

        if _auto_account == "real":
            # 实盘: graduation gate 前置, 未毕业 fail-close 降级 sim
            try:
                from app.playbook.pipeline import _check_graduation_gate
                _gate = _check_graduation_gate()
            except Exception as _ge:
                logger.error(f"[playbook] graduation gate 异常 (fail-close → 降级 sim): {_ge}")
                return self._execute_via_dual_broker(
                    inst, instruction_id, symbol, direction, entry_price, stop_loss,
                    take_profit, position_size, mos_direction, _matched_decision_id,
                    target_env="sim",
                )
            if not _gate.get("graduated"):
                logger.warning(f"[playbook] AUTO_TRADE_ACCOUNT=real 但未毕业, 降级 sim: {_gate.get('reason')}")
                return self._execute_via_dual_broker(
                    inst, instruction_id, symbol, direction, entry_price, stop_loss,
                    take_profit, position_size, mos_direction, _matched_decision_id,
                    target_env="sim",
                )
            return self._execute_via_dual_broker(
                inst, instruction_id, symbol, direction, entry_price, stop_loss,
                take_profit, position_size, mos_direction, _matched_decision_id,
                target_env="real",
            )

        # AUTO_TRADE_ACCOUNT=sim → 模拟盘真实下单 (futu_simulate, OKR 验证)
        return self._execute_via_dual_broker(
            inst, instruction_id, symbol, direction, entry_price, stop_loss,
            take_profit, position_size, mos_direction, _matched_decision_id,
            target_env="sim",
        )

    def _execute_via_dual_broker(
        self, inst, instruction_id, symbol, direction, entry_price, stop_loss,
        take_profit, position_size, mos_direction, matched_decision_id, target_env,
    ):
        """sim/live 共用执行路径 (Phase 2 无缝统一, 2026-06-24).

        无缝命门: futu_simulate (target_env=sim) 与 live (target_env=real) 走完全相同的代码,
        只差 target_env. 策略逻辑/仓位/记录全一致, sim→live 零行为分叉.

        - target_env=sim  → source_type=PLAYBOOK_SIM,  mode=futu_simulate, trade_mode=simulate
        - target_env=real → source_type=PLAYBOOK_LIVE, mode=live,          trade_mode=live
        """
        import uuid as _uuid
        import json as _json
        from app.core.models import Order, Direction, OrderType
        from app.execution.dual_broker import get_dual_broker

        _is_live = (target_env == "real")

        # 2026-06-28 fail-closed guard: 任何路径请求 live 都必须先通过毕业门禁.
        # 如果未毕业(包括异常), 强制降级 sim, 避免误下实盘单.
        if _is_live:
            try:
                from app.playbook.pipeline import _check_graduation_gate
                _gate = _check_graduation_gate()
                if not _gate.get("graduated"):
                    logger.warning(f"[playbook] target_env=real 但未毕业, 强制降级 sim: {_gate.get('reason')}")
                    target_env = "sim"
                    _is_live = False
            except Exception as _ge:
                logger.error(f"[playbook] 毕业门禁异常, 强制降级 sim: {_ge}")
                target_env = "sim"
                _is_live = False

        # LIVE_TRIAL 试运行 (Phase 2): target_env=real 且 live_trial 开启时仓位×0.5,
        # 对冲 sim→live 不可见的滑点 gap. 验证 sim PnL≈live PnL (10-20笔) 后用户关 live_trial 放全量.
        if _is_live:
            try:
                from app.core.config_container import get_config as _get_cfg
                _live_trial = _get_cfg().get_dotted("playbook.live_trial", True)
            except Exception:
                _live_trial = True
            if _live_trial and position_size and position_size > 1:
                position_size = max(1, int(position_size * 0.5))
                logger.info(f"[playbook] LIVE_TRIAL 试运行: {symbol} 仓位×0.5 → {position_size}")
        _source_type = "PLAYBOOK_LIVE" if _is_live else "PLAYBOOK_SIM"
        _mode_label = "live" if _is_live else "futu_simulate"
        _trade_mode = "live" if _is_live else "simulate"
        _owner = "playbook_live" if _is_live else "playbook_sim"

        # Phase 3 (2026-06-24) 源头拦截: 下单前查 ManualHoldRegistry.
        # 若该 symbol 被手动持有 (实盘), playbook 跳过 — 避免同 symbol 在 open_positions
        # (symbol PK) 上 manual(real, owner=manual) 与 sim(owner=auto) 互相覆盖导致仓位孤儿化.
        # 这是 flat-switch 下最安全的同账户隔离方式, 无需深改 open_positions 主键.
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
            if manual_hold_registry.is_auto_blocked(symbol):
                logger.info(f"[playbook] {symbol} 被手动持有(同账户), 跳过 {_mode_label} 执行 (避免同 symbol 冲突)")
                return {
                    "success": False,
                    "message": f"{symbol} 被手动持有, 跳过 (避免与实盘手动仓位冲突)",
                    "skipped_manual_held": True,
                }
        except Exception as _mhe:
            logger.warning(f"[playbook] ManualHoldRegistry 检查异常 (放行): {_mhe}")

        # Phase 3 (2026-06-24, decision "sim 也守 PDT"): 下单前查 PDT 权限.
        # sim/live 统一查 (is_pdt_account: equity≥25k 豁免, <25k 守 ≤3/5日).
        # 确保 sim 验证的是 PDT 合规策略, 避免 sim 达标 live(<25k) 跑不了的假达标.
        # A1+ (2026-07-19): fail 模式同 ACB, 由 risk.acb_fail_mode 统一驱动.
        try:
            from app.risk.pdt_guard import get_pdt_guard
            _pdt = get_pdt_guard()
            _allowed, _pdt_reason, _pdt_level = _pdt.check_trade_permission()
            if not _allowed:
                logger.warning(f"[playbook] {symbol} {_mode_label} 被 PDT 拦截: {_pdt_reason}")
                return {
                    "success": False,
                    "message": f"{symbol} 被 PDT 拦截: {_pdt_reason}",
                    "skipped_pdt": True,
                }
        except Exception as _pe:
            # A1+: 配置驱动 fail 模式 (open=放行, closed=拒绝)
            try:
                from app.core.config_container import get_config as _get_cfg_pdt
                _pdt_fail_mode = _get_cfg_pdt().get_dotted("risk.acb_fail_mode", "open")
            except Exception:
                _pdt_fail_mode = "open"
            if _pdt_fail_mode == "closed":
                logger.error(f"[playbook] PDT 检查异常 (fail-close 拒绝开仓): {_pe}")
                return {"success": False, "message": f"PDT 检查异常 (fail-close): {_pe}"}
            logger.warning(f"[playbook] PDT 检查异常 (放行): {_pe}")

        # 滑点数据闭环 (P0): 下单前抓 arrival price (信号参考价), 事后与真实成交价对比.
        #   口径: bid/ask 中间价优先, 兜底 last price. 取价失败不阻塞下单 (fail-open, 记 0).
        _arrival_price = 0.0
        _arr_ctx = None
        try:
            from app.data.market_data_router import MarketDataRouter as _MDR
            _arr_ctx = _MDR().get_quote(symbol)
            if _arr_ctx:
                _a_bid = float(_arr_ctx.get("lv3_bid") or _arr_ctx.get("bid") or 0)
                _a_ask = float(_arr_ctx.get("lv3_ask") or _arr_ctx.get("ask") or 0)
                if _a_bid > 0 and _a_ask > 0:
                    _arrival_price = round((_a_bid + _a_ask) / 2.0, 4)
                else:
                    _arrival_price = float(_arr_ctx.get("price", 0) or 0)
        except Exception as _arr_e:
            logger.debug(f"[playbook] {symbol} arrival price 抓取失败 (不阻塞下单): {_arr_e}")

        # 下单前报价健康门 (Tier B 2026-07-19): 复用 check_quote_health 拦截过宽 spread / 过期报价.
        #   复用上面已抓的 _arr_ctx (不额外调 API). 取价失败/无报价 → fail-open (下游另有兜底).
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_gate
            _gate_cfg = _get_pb_cfg_gate()
            if _gate_cfg.get("pretrade_quote_gate_enabled", True) and _arr_ctx:
                from app.risk.data_health_guard import check_quote_health
                _g_bid = float(_arr_ctx.get("lv3_bid") or _arr_ctx.get("bid") or 0)
                _g_ask = float(_arr_ctx.get("lv3_ask") or _arr_ctx.get("ask") or 0)
                # 仅在 bid/ask 齐备时硬门 (缺失=薄盘/盘前, 不过度拦截, 交由下游兜底).
                if _g_bid > 0 and _g_ask > 0:
                    _gate_quote = {
                        "bid": _g_bid,
                        "ask": _g_ask,
                        "price": float(_arr_ctx.get("price", 0) or _arr_ctx.get("last", 0) or 0),
                        "timestamp": _arr_ctx.get("timestamp"),
                    }
                    _q_ok, _q_reason = check_quote_health(
                        _gate_quote,
                        max_age_seconds=float(_gate_cfg.get("pretrade_max_quote_age_sec", 60.0)),
                        max_spread_pct=float(_gate_cfg.get("pretrade_max_spread_pct", 0.05)),
                    )
                    if not _q_ok:
                        logger.warning(f"[playbook] {_mode_label} {symbol} 报价健康门拒绝下单: {_q_reason}")
                        return {"success": False, "message": f"报价健康门拒绝: {_q_reason}"}
        except Exception as _gate_e:
            # DEB-20260721 R4: debug→warning — fail-open 失明必须可观测可计数 (grep "报价健康门异常").
            logger.warning(f"[playbook] {symbol} 报价健康门异常 (fail-open 放行): {_gate_e}")

        # B4 数据新鲜度门 (2026-07-20): 降级源 (finnhub 15min延迟等) / 过期报价 → 禁开新仓.
        #   补 health 门盲区: 降级源恰好无 bid/ask → 整体绕过上面的 health 门.
        #   仅拦开仓 (本路径只做开仓, 平仓走 monitor_playbook_sim_exits 不经此处).
        #   _arr_ctx 为 None (取价失败) 维持 fail-open, 与 health 门语义一致.
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_fresh
            _fresh_cfg = _get_pb_cfg_fresh()
            if _fresh_cfg.get("entry_freshness_gate_enabled", True) and _arr_ctx:
                from app.risk.data_health_guard import check_quote_freshness
                _f_ok, _f_reason = check_quote_freshness(
                    _arr_ctx,
                    max_age_seconds=float(_fresh_cfg.get("entry_max_quote_age_sec", 90.0)),
                )
                if not _f_ok:
                    logger.warning(f"[playbook] {_mode_label} {symbol} 数据新鲜度门拒绝开仓: {_f_reason}")
                    return {"success": False, "message": f"数据新鲜度门拒绝: {_f_reason}"}
        except Exception as _fresh_e:
            # DEB-20260721 R4: debug→warning — fail-open 失明必须可观测可计数 (grep "新鲜度门异常").
            logger.warning(f"[playbook] {symbol} 数据新鲜度门异常 (fail-open 放行): {_fresh_e}")

        _order = Order(
            symbol=symbol,
            direction=Direction.LONG if direction == "BUY" else Direction.SHORT,
            order_type=OrderType.MARKET,
            quantity=int(position_size) if position_size else 1,
            price=0.0,  # 市价单
        )
        _order.metadata = {"playbook_instruction_id": instruction_id, "source": f"playbook_{_mode_label}"}
        _order.client_order_id = f"pb_{instruction_id}_{target_env}"

        # 幂等抢占 (P0 2026-07-19): 下单前原子置位 executed, 防并发/重启重试双重下单.
        #   抢占失败 → 已有其他调用在处理该指令, 直接放弃 (不重复下单).
        if not self.db.try_claim_execution(instruction_id):
            logger.warning(f"[playbook] {_mode_label} {symbol} 指令 {instruction_id} 已被抢占, 跳过 (防重复下单)")
            return {"success": False, "message": "指令已被执行或正在执行 (幂等抢占)"}

        # 下单 (sim 或 real, 同一方法)
        try:
            _dual = get_dual_broker()
            _result = _dual.submit_order(_order, target_env=target_env)
        except Exception as _e:
            logger.error(f"[playbook] {_mode_label} 下单异常 {symbol}: {_e}")
            self.db.release_execution_claim(instruction_id)
            return {"success": False, "message": f"{_mode_label} 下单异常: {_e}"}

        if not _result.get("success"):
            logger.error(f"[playbook] {_mode_label} 下单失败 {symbol}: {_result.get('error')}")
            self.db.release_execution_claim(instruction_id)
            return {"success": False, "message": f"{_mode_label} 下单失败: {_result.get('error')}"}

        _futu_id = _result.get("futu_order_id", "")
        # 2026-06-27 辩论DEB-20260627: _fill=0时不fallback到entry_price
        # ponytail: 1次查询, 失败则warning+拒绝(不sleep阻塞批量路径)
        _fill = _result.get("filled_price", 0) or 0
        _entry_price_source = "broker_deal" if _fill > 0 else "unknown"
        if not _fill or _fill <= 0:
            _fill = self._query_filled_price_for_order(_futu_id, target_env)
            if _fill > 0:
                _entry_price_source = "broker_deal_retry"
        if not _fill or _fill <= 0:
            # 2026-07-19 (DEB-round3 GHOST-RC1 根因修复): quote 兜底前先确认订单真成交.
            #   根因: 旧逻辑 fill=0 时无条件假设"已成交"用报价记账, 实际订单可能 FAILED/
            #   CANCELLED/TIMEOUT → 虚假仓位 → 退出 broker_no_position → BROKER_GHOST_CLOSE
            #   (7/6 六连实证: position_sync 证实 broker 无持仓且无今日成交记录).
            #   真相源与 close 路径一致: order_list_query.order_status (2026-07-07 修复确立).
            #   查询异常 → 降级维持旧兜底 (宁可记不错杀, 与 2026-06-29 辩论一致).
            # 2026-07-21 (GHOST-RC1 时序修复): 确认窗 5s→30s (对齐平仓侧轮询先例).
            #   根因: SUBMITTED 是等待态而非失败态; 实证 EOG(7/20)/TTE/UBER(7/21) 均在
            #   拒记后 9~29s 内成交 → 5s 窗误判 → 真仓无账走降级补录 (假归因 50/0.5).
            #   仅 fill=0 的模糊单进等待, 正常成交不受影响. 回滚: 配置改回 5.
            try:
                from app.playbook.config import get_playbook_config as _gpc_cw
                _confirm_wait = float(_gpc_cw().get("entry_confirm_max_wait_s", 30.0))
                _confirmed, _ostatus = self._confirm_order_filled(
                    _futu_id, target_env, max_wait_s=_confirm_wait)
            except Exception as _cf_err:
                logger.warning(f"[playbook] {_mode_label} {symbol} 订单状态确认异常 (降级放行): {_cf_err}")
                _confirmed, _ostatus = True, "query_error"
            if not _confirmed:
                logger.error(
                    f"[playbook] {_mode_label} {symbol} 订单 {_futu_id} 未成交 (status={_ostatus}), "
                    f"拒绝记账 — 消除 GHOST-RC1 虚假仓位"
                )
                self.db.release_execution_claim(instruction_id)
                return {"success": False, "message": f"{_mode_label} {symbol} 订单未成交 (status={_ostatus})"}
            # 2026-06-29: 最后兜底 — broker确认FILLED_ALL但avg_price=0时, 用实时报价记账
            # 根因: sim账户偶发 avg_price=0 但订单已成交 → 拒绝记账 → 孤儿仓 → position_sync用8%覆盖SL
            # 2026-07-21 更正: "avg_price=0"真根因是 dual_broker fallback 字段名笔误 (当日已修复为
            #   dealt_avg_price, sim 真实成交均价可正常获取); 本 bid/ask 兜底保留作极端后备, 触发应罕见.
            # 2026-07-16 P2 修复: 用 bid/ask 替代 last price (BUY→ask, SHORT→bid),
            #   更接近实际成交价. last price 可能是上一笔成交, 偏离当前 bid-ask.
            try:
                from app.data.market_data_router import MarketDataRouter
                _dr = MarketDataRouter()
                _ctx = _dr.get_quote(symbol)
                if _ctx and _ctx.get("price", 0) > 0:
                    _cur = float(_ctx["price"])
                    _bid = float(_ctx.get("lv3_bid") or _ctx.get("bid") or 0)
                    _ask = float(_ctx.get("lv3_ask") or _ctx.get("ask") or 0)
                    # BUY 成交在 ask 侧, SHORT 成交在 bid 侧
                    if mos_direction == "SHORT" and _bid > 0:
                        _fill = _bid
                    elif mos_direction != "SHORT" and _ask > 0:
                        _fill = _ask
                    else:
                        _fill = _cur
                    _entry_price_source = "quote_fallback"
                    logger.warning(
                        f"[playbook] {_mode_label} {symbol} 成交价=0, 用bid/ask${_fill:.2f}兜底记账 "
                        f"(订单 {_futu_id} 已确认成交) src=quote_fallback dir={mos_direction}"
                    )
            except Exception:
                pass
        if not _fill or _fill <= 0:
            logger.error(f"[playbook] {_mode_label} {symbol} 订单 {_futu_id} 无法获取成交价, 拒绝记账")
            return {"success": False, "message": f"{_mode_label} {symbol} 成交价查询失败, 可能未成交"}
        # 部分成交处理 (P0 2026-07-19): 用真实成交量记账, 消除幽灵仓.
        #   broker 返回 filled_qty=0 时 (SIM avg_price=0 / 查询未命中) 回退下单量.
        _req_qty = int(position_size) if position_size else 1
        _filled_qty = float(_result.get("filled_qty", 0) or 0)
        if 0 < _filled_qty < _req_qty:
            _effective_qty = int(_filled_qty)
            logger.warning(
                f"[playbook] {_mode_label} {symbol} 部分成交: 下单 {_req_qty} 实际 {_effective_qty}, "
                f"按实际成交量记账 (futu_id={_futu_id})"
            )
        else:
            _effective_qty = _req_qty
        _now_iso = now_et().isoformat()
        _order_id = f"{_mode_label}_{_uuid.uuid4().hex[:8]}"

        # 滑点数据闭环 (P0): 用 arrival price 与真实成交价算滑点 bps (正=不利).
        #   BUY 成交价 > 参考价 → 不利; SHORT 成交价 < 参考价 → 不利.
        _slippage_bps = None
        if _arrival_price and _arrival_price > 0 and _fill and _fill > 0:
            _raw_bps = (float(_fill) - float(_arrival_price)) / float(_arrival_price) * 10000.0
            _slippage_bps = round(-_raw_bps if mos_direction == "SHORT" else _raw_bps, 2)
            if abs(_slippage_bps) > 50.0:
                logger.warning(
                    f"[playbook] {_mode_label} {symbol} 滑点 {_slippage_bps:+.1f}bps 超阈值 50bps "
                    f"(参考${_arrival_price:.2f} → 成交${_fill:.2f} dir={mos_direction})"
                )
        _exec_meta = {
            "arrival_price": float(_arrival_price) if _arrival_price else None,
            "fill_price": float(_fill),
            "slippage_bps": _slippage_bps,
            "entry_price_source": _entry_price_source,
        }

        # 2026-07-22 只读探针: 开盘 gap 采集 (不拦截, 纯记录). 复用上面已抓的 _arr_ctx, 零额外 API.
        #   动机: 无法凭感觉给 gap 拦截阈值 — 先积累 gap↔outcome 数据 (join 本单最终 pnl/mfe/mae),
        #   2周后用相关性裁决"高开追单是否真更易亏 / 拐点在几%", 再决定是否加门. fail-open 记 None.
        _gap_meta = None
        try:
            if _arr_ctx:
                _prev_close = float(_arr_ctx.get("prev_close", 0) or 0)
                _today_open = float(_arr_ctx.get("open", 0) or 0)
                if _prev_close > 0 and _today_open > 0:
                    _gap_pct = (_today_open - _prev_close) / _prev_close * 100.0
                    _is_short = "SHORT" in str(mos_direction).upper() or "SELL" in str(mos_direction).upper()
                    _aligned_gap = -_gap_pct if _is_short else _gap_pct
                    _gap_meta = {
                        "prev_close": round(_prev_close, 4),
                        "today_open": round(_today_open, 4),
                        "fill_price": float(_fill),
                        "gap_pct": round(_gap_pct, 3),
                        "aligned_gap_pct": round(_aligned_gap, 3),
                    }
        except Exception:
            _gap_meta = None

        # 写 trade_records (真实成交价, source_type 按 target_env 区分)
        try:
            from app.storage.trade_store import TradeStore
            from app.core.paths import get_trade_records_db
            _ts = TradeStore(str(get_trade_records_db()))
            _trace_id = f"playbook-{_mode_label}-{symbol}-{_now_iso[:10]}"

            # 2026-07-11: 入场市场上下文补全 (entry_vix/entry_session 此前 PLAYBOOK 全空,
            #   供 config_version×regime×session 三维分层复盘). fail-closed: 取不到落 0/"".
            # 2026-07-16 P1 修复: market_regime 从 MarketRegimeGate 实时获取, 不依赖 instruction.
            try:
                from app.utils.timezone import get_current_session as _get_sess
                _entry_session = _get_sess() or ""
            except Exception:
                _entry_session = ""
            # 2026-07-16: regime 从 MarketRegimeGate 实时判定, instruction 中无此字段.
            # 2026-07-17: entry_vix 复用 regime 评估的 vix — assess() 内含 VIX 多级重试
            #   (Router→AV→重试→兜底 20.0) 并落在 RegimeContext.vix. 此前 entry_vix 单独调
            #   get_vix_or_none() 无缓存无重试, 盘前/限流恒 None→0.0 (填充率仅 10%).
            _entry_vix = 0.0
            _live_regime = inst.get("market_regime", "")
            _rc = None
            try:
                from app.playbook.market_regime import MarketRegimeGate
                _rc = MarketRegimeGate().assess()
                if not _live_regime:
                    _live_regime = str(_rc.regime.value if hasattr(_rc.regime, 'value') else _rc.regime)
                _entry_vix = float(getattr(_rc, "vix", 0.0) or 0.0)
            except Exception:
                if not _live_regime:
                    _live_regime = "UNKNOWN"
            # 兜底: regime 评估失败时单独取一次 VIX (仍可能取不到 → 0.0)
            if _entry_vix <= 0:
                try:
                    from app.data.market_data_router import get_market_data_router as _get_mdr
                    _entry_vix = _get_mdr().get_vix_or_none() or 0.0
                except Exception:
                    _entry_vix = 0.0

            # 2026-07-03 fix: 从 voting_predictions 表查投票数据, 写入 trade_records
            # 根因: save_trade 未传 voting_result/model_scores/consensus_level/final_strength
            _voting_data = None
            if matched_decision_id:
                _vp_conn = None
                try:
                    import sqlite3 as _sqlite3
                    _vp_db = str(get_trade_records_db())
                    _vp_conn = _sqlite3.connect(_vp_db)
                    _vp_conn.row_factory = _sqlite3.Row
                    _vp_row = _vp_conn.execute(
                        "SELECT model_votes, predicted_strength, predicted_confidence "
                        "FROM voting_predictions WHERE decision_id = ? LIMIT 1",
                        (matched_decision_id,)
                    ).fetchone()
                    if _vp_row:
                        import json as _vp_json
                        _model_votes = _vp_json.loads(_vp_row["model_votes"]) if _vp_row["model_votes"] else {}
                        _voting_data = {
                            "voting_result": _model_votes,
                            "model_scores": list(_model_votes.values()) if _model_votes else [],
                            "consensus_level": float(_vp_row["predicted_confidence"] or 0),
                            "final_strength": int(_vp_row["predicted_strength"] or 0),
                        }
                except Exception as _ve:
                    logger.debug(f"[playbook] voting_data 查询失败 {symbol}: {_ve}")
                finally:
                    if _vp_conn is not None:
                        _vp_conn.close()

            _ts.save_trade({
                "trace_id": _trace_id,
                "trade_date": _now_iso[:10],
                "symbol": symbol,
                "direction": mos_direction,
                "entry_time": _now_iso,
                "exit_time": "",
                "entry_price": float(_fill),
                "exit_price": 0,
                "quantity": _effective_qty,
                "realized_pnl": 0,
                "pnl_pct": 0,
                "exit_reason": "",
                "holding_hours": 0,
                # 2026-07-11 量纲统一: signal_strength 0-100, signal_confidence 0-1 (对齐其他信号源)
                # 2026-07-16 P2: signal_strength 不是独立质量分, 是 confidence 的镜像值.
                #   下游分析(分层/归因/IC)不应当质量分使用 — 用 source_type 区分信号源质量:
                #   PLAYBOOK_SIM(DA)=35/70, universe_expansion=50(capped), MAIN_PIPELINE=旧pipeline.
                "signal_strength": int(inst.get("confidence", 0)) if inst.get("confidence") else 0,
                "signal_confidence": float(inst.get("confidence", 0)) / 100.0,
                "futu_order_id": _futu_id or _order_id,
                "source_type": _source_type,
                "entry_reason": f"playbook_{_mode_label}",
                "owner_strategy": _owner,
                "market_regime": _live_regime,
                "entry_vix": _entry_vix,
                "entry_session": _entry_session,
                "mode": _mode_label,
                "trade_mode": _trade_mode,
                "signal_id": _trace_id,
                "decision_id": matched_decision_id,
                # 2026-07-09: 因子快照持久化 — 从conditions提取因子标记到f1-f5独立列
                # 2026-07-16 P1 修复: 扩展关键字匹配 — DA 的 conditions 用 "insider_net_sell"/
                #   "pe_above_industry"/"below_ma20"/"news_positive"/"earnings_beat_streak" 等,
                #   原映射关键字不匹配 → f1-f5 全 0. 修复: 补充 DA conditions 的关键字.
                "f1_trend": 1.0 if any("ma" in c or "macd" in c or "trend" in c or "above_ma" in c or "below_ma" in c for c in _parse_json_field(inst.get("conditions_json"), [])) else 0.0,
                "f2_momentum": 1.0 if any("momentum" in c or "rsi" in c or "overbought" in c or "oversold" in c or "breakout" in c or "earnings_beat" in c for c in _parse_json_field(inst.get("conditions_json"), [])) else 0.0,
                "f3_volume": 1.0 if any("volume" in c or "vol" in c for c in _parse_json_field(inst.get("conditions_json"), [])) else 0.0,
                "f4_orderflow": 1.0 if any("insider" in c or "msp" in c or "order_flow" in c or "net_sell" in c or "net_buy" in c for c in _parse_json_field(inst.get("conditions_json"), [])) else 0.0,
                "f5_lifecycle": 1.0 if any("lifecycle" in c or "breakout" in c or "support" in c or "resistance" in c or "pe_above" in c or "pe_below" in c or "news_positive" in c or "news_negative" in c for c in _parse_json_field(inst.get("conditions_json"), [])) else 0.0,
                "factor_detail": inst.get("conditions_json", ""),
                "metadata_json": _json.dumps({
                    "playbook_instruction_id": instruction_id,
                    "stop_loss": float(stop_loss) if stop_loss else 0,
                    "take_profit": float(take_profit) if take_profit else 0,
                    "atr": _extract_atr_from_instruction(inst),
                    "thesis": inst.get("thesis", "")[:200],
                    "invalidation": inst.get("invalidation", "") or "",  # 2026-07-11 Stage2a: 供退出侧影子评估
                    "peak_price": float(_fill),
                    "trough_price": float(_fill),  # 2026-07-11: 谷值追踪初始化 (与 peak 对称)
                    "entry_price_source": _entry_price_source,  # 2026-07-03: 追踪 entry_price 来源
                    # 2026-06-26: Horizon 决策写入 metadata
                    "horizon": inst.get("horizon", "short"),
                    "horizon_confidence": inst.get("horizon_confidence", 0.0),
                    "horizon_reasoning": inst.get("horizon_reasoning", {}),
                    # P1 (2026-06-24): 因子归因 — Playbook 路径此前不写 factor 数据 (trade_records.factor_detail 恒 NULL)
                    # 写入触发条件 + 数据质量, 供 OKR 按 source/factor 拆分 (与 MAIN_PIPELINE factor_detail 同用途)
                    "conditions": _parse_json_field(inst.get("conditions_json"), []),
                    "data_quality": _parse_json_field(inst.get("data_quality_json"), {}),
                    # 滑点数据闭环 (P0): arrival price / 真实成交价 / 滑点 bps
                    "execution": _exec_meta,
                    # 2026-07-22 只读探针: 开盘 gap (record-only, 供 gap↔outcome 裁决)
                    "entry_gap": _gap_meta,
                }),
                "signal_details": _json.dumps({
                    "source": f"playbook_{_mode_label}",
                    "entry_price": float(_fill),
                    "stop_loss": float(stop_loss) if stop_loss else 0,
                    "take_profit": float(take_profit) if take_profit else 0,
                    "futu_order_id": _futu_id,
                }),
            }, voting_data=_voting_data)
            logger.info(f"[playbook] {_mode_label} trade_record 已写入: {symbol} @ {_fill} futu_id={_futu_id}")

            # 2026-07-02: open_positions SSOT — playbook下单后立即写open_positions
            # 根因: playbook只写trade_records不写open_positions → position_sync
            # 无法区分系统仓和人工仓 → 人工仓被误标auto → 被自动平仓
            # 修复: 下单后立即UPSERT open_positions, owner='auto', source_type标记来源
            try:
                _ts.upsert_position_simple(
                    symbol=symbol,
                    direction=mos_direction,
                    size=_effective_qty,
                    entry_price=float(_fill),
                    owner_strategy=_owner,
                    owner='auto',
                    account_type=_trade_mode,
                    source_type=_source_type,
                    futu_order_id=_futu_id or _order_id,
                    stop_loss=float(stop_loss) if stop_loss else 0,
                    sync_status='synced',
                )
                logger.info(f"[playbook] {_mode_label} open_position 已写入: {symbol}")
            except Exception as _ope:
                logger.warning(f"[playbook] open_position 写入失败 (不影响主流程, position_sync会补): {_ope}")
        except Exception as _tre:
            # P0 (2026-06-24): broker 已下单但 trade_record 写失败 → 反向平仓回滚, 防孤儿仓
            # (否则 monitor 查不到该仓位, 永不被自动平仓). 首跑 sim 最高危断点.
            logger.error(f"[playbook] {_mode_label} trade_record 写入失败, 回滚平仓 {symbol}: {_tre}")
            try:
                _dual_rb = get_dual_broker()
                _rb = _dual_rb.close_position(symbol, _effective_qty, mos_direction, target_env=target_env)
                if _rb.get("success"):
                    logger.warning(
                        f"[playbook] {_mode_label} 回滚平仓成功 {symbol} @ {_rb.get('filled_price')} "
                        f"(孤儿仓已防)"
                    )
                else:
                    logger.error(
                        f"[playbook] {_mode_label} 回滚平仓失败 {symbol}: {_rb.get('error')} "
                        f"— ⚠️ 需手动处理 sim 账户孤儿仓"
                    )
            except Exception as _rbe:
                logger.error(
                    f"[playbook] {_mode_label} 回滚平仓异常 {symbol}: {_rbe} "
                    f"— ⚠️ 需手动处理 sim 账户孤儿仓"
                )
            return {"success": False, "message": f"{_mode_label} trade_record 写入失败已回滚平仓: {_tre}"}

        # 注册 ManualHoldRegistry (防 auto pipeline 干扰)
        # 2026-07-03 fix: account 应与 target_env 一致 (sim→sim, real→real)
        # 之前默认 account="real" → sim 仓位被注册为 real → Reconciler 误判为 MANUAL
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
            manual_hold_registry.register(
                symbol=symbol, direction=mos_direction, quantity=_effective_qty,
                reason=f"Playbook {_mode_label}: {inst.get('thesis', '')[:60]}",
                account=("real" if _trade_mode == "live" else "sim"),
            )
        except Exception as _e:
            logger.warning(f"ManualHoldRegistry register failed: {_e}")

        # 更新 DB — execution_price 用真实成交价
        self.db.update_instruction_tracking(instruction_id, {
            "executed": 1,
            "execution_price": float(_fill),
            "execution_time": _now_iso,
            "outcome": "holding",
            "decision_id": matched_decision_id,
        })
        self.db.save_user_feedback({"instruction_id": instruction_id, "action": "executed"})

        _env_desc = "实盘真实成交" if _is_live else "模拟盘真实成交"
        logger.info(f"Playbook {_mode_label} executed: {symbol} {direction} @ ${_fill} ({_env_desc}, futu_id={_futu_id})")

        # live 兜底: 挂 broker STOP 单 (服务断连时仍止损; sim 跳过由规则引擎兜底)
        if _is_live and stop_loss and float(stop_loss) > 0:
            try:
                _dual.place_protective_stop(
                    symbol, _effective_qty,
                    mos_direction, float(stop_loss), target_env="real",
                )
            except Exception as _se:
                logger.warning(f"[playbook] live STOP 兜底失败 {symbol}: {_se}")

        # 2026-07-16: playbook 成交逐笔飞书推送 (复用 PushHub trade_entry)。
        #   来源精准标注 — sim→playbook_sim / live→playbook_live。自带 try/except, 绝不影响下单。
        #   2026-07-17: 回填 SL/TP/来源 → 富卡 (钉钉 markdown / 飞书多行)。
        #   2026-07-23: 画像显性化 — 计划价+偏离、DA/UE 细分来源、tier/streak/horizon/conf
        #   (DEB-20260723 配套: 通知端把当日埋点可视化, 组装失败降级原版式)。
        try:
            from app.push import get_push_hub, trade_entry as _pb_entry
            _pb_strategy = "playbook_live" if _is_live else "playbook_sim"
            try:
                _pb_sl = float(stop_loss) if stop_loss and float(stop_loss) > 0 else None
            except Exception:
                _pb_sl = None
            try:
                _pb_tp = [float(take_profit)] if take_profit and float(take_profit) > 0 else None
            except Exception:
                _pb_tp = None
            _pb_extra = {}
            try:
                _raw = inst.get("raw_instruction_json")
                _raw = json.loads(_raw) if isinstance(_raw, str) and _raw else (_raw or {})
                _origin = _raw.get("origin") or {}
                _src_raw = str(_raw.get("source") or "")
                _src_detail = {"deep_analysis": "DA", "universe_expansion": "UE"}.get(_src_raw)
                _planned = _raw.get("planned_entry") or None
                _pb_extra = {
                    "planned_entry": float(_planned) if _planned else None,
                    "horizon": inst.get("horizon"),
                    "src_detail": _src_detail,
                    "self_tier": _origin.get("self_tier"),
                    "emergence_streak": _origin.get("emergence_streak_days"),
                }
                _conf = inst.get("confidence")
                if _conf is not None:
                    _pb_extra["conf_note"] = f"conf {int(_conf)}"
                if _planned and float(_planned) > 0 and float(_fill) > 0:
                    _pb_extra["entry_dev_pct"] = round(
                        (float(_fill) - float(_planned)) / float(_planned) * 100, 1)
            except Exception:
                _pb_extra = {}
            get_push_hub().push(_pb_entry(
                symbol=symbol,
                direction=mos_direction,
                qty=_effective_qty,
                price=float(_fill),
                strategy=_pb_strategy,
                trace_id=matched_decision_id or None,
                sl=_pb_sl,
                tp=_pb_tp,
                source_type=_pb_strategy,
                extra=_pb_extra,
            ))
        except Exception as _pb_push_e:
            logger.warning(f"[playbook推送] {symbol} 成交推送失败 (不影响下单): {_pb_push_e}")

        return {
            "success": True,
            "message": f"{_mode_label} 执行 {symbol} {direction} @ ${_fill} ({_env_desc})",
            "execution_price": float(_fill),
            "mode": _mode_label,
            "futu_order_id": _futu_id,
        }

    # ========== 跳过/反馈接口 ==========

    def _query_filled_price_for_order(self, futu_order_id: str, target_env: str) -> float:
        """查询订单成交均价 (带target_env路由).

        2026-07-03: 委托给 dual_broker._query_filled_price (已改用 history_deal_list_query).
        之前这里有自己的 order_list_query 副本, SIM 环境系统性返回0.
        """
        try:
            from app.execution.dual_broker import get_dual_broker
            dual = get_dual_broker()
            ctx, acc_id, trd_env, lock = dual._ctx_and_acc(target_env)
            return dual._query_filled_price(ctx, futu_order_id, trd_env, lock)
        except Exception as e:
            logger.warning(f"[playbook] 查成交价失败 {futu_order_id}: {e}")
        return 0.0

    def _confirm_order_filled(self, futu_order_id: str, target_env: str,
                              max_wait_s: float = 5.0) -> tuple:
        """确认订单是否真实成交 → (confirmed: bool, status: str).

        2026-07-19 (DEB-round3 GHOST-RC1): 入场侧成交确认, 与 dual_broker.close_position
        的平仓侧验证共用同一真相源 — order_list_query.order_status (FILLED_ALL).
        短轮询 ≤5s (入场路径不阻塞批量执行, 平仓侧 30s 轮询的收紧版).

        判定:
          FILLED_ALL / FILLED_PART / dealt_qty>0 → (True, status)
          FAILED / CANCELLED / DELETED / REJECTED / TIMEOUT → (False, status) 不可恢复
          超时仍非 FILLED → (False, status) (宁可拒记不记假仓)
        """
        from futu import RET_OK
        from app.execution.dual_broker import get_dual_broker
        dual = get_dual_broker()
        ctx, acc_id, trd_env, lock = dual._ctx_and_acc(target_env)
        _deadline = time.time() + max_wait_s
        _last_status = "UNKNOWN"
        while time.time() < _deadline:
            try:
                with lock:
                    ret_od, od_data = ctx.order_list_query(
                        order_id=str(futu_order_id), trd_env=trd_env)
                if ret_od == RET_OK and od_data is not None and not od_data.empty:
                    _row = od_data.iloc[0]
                    _status = str(_row.get("order_status", ""))
                    _last_status = _status
                    _dealt = float(_row.get("dealt_qty", 0) or 0)
                    if "FILLED_ALL" in _status or "FILLED_PART" in _status or _dealt > 0:
                        return True, _status
                    if any(_k in _status for _k in
                           ("FAILED", "CANCELLED", "DELETED", "REJECTED", "TIMEOUT")):
                        return False, _status
            except Exception as _od_err:
                logger.debug(f"[playbook] order_list_query 确认异常 {futu_order_id}: {_od_err}")
            time.sleep(0.5)
        return False, _last_status

    def skip_instruction(
        self,
        instruction_id: int,
        reason: str = "",
        custom_reason: str = "",
    ) -> Dict[str, Any]:
        """
        跳过一条指令并记录原因

        Args:
            instruction_id: 指令 ID
            reason: 预设原因 (risk_too_high / unfamiliar_sector / similar_position / insufficient_capital)
            custom_reason: 自定义原因文本

        Returns:
            {"success": bool, "message": str}
        """
        inst = self._find_instruction_by_id(instruction_id)
        if not inst:
            return {"success": False, "message": "指令不存在"}

        skip_reason = custom_reason or reason or "用户主动跳过"

        # 更新 DB
        self.db.update_instruction_tracking(instruction_id, {
            "outcome": "skipped",
        })

        # 保存反馈
        self.db.save_user_feedback({
            "instruction_id": instruction_id,
            "action": "skipped",
            "skip_reason": skip_reason,
        })

        logger.info(f"Playbook skipped: {inst['symbol']} reason={skip_reason}")
        return {"success": True, "message": f"已跳过 {inst['symbol']}"}

    def modify_instruction(
        self,
        instruction_id: int,
        modified_field: str,
        modified_value: str,
    ) -> Dict[str, Any]:
        """
        记录用户修改了指令的某个参数

        Args:
            instruction_id: 指令 ID
            modified_field: 修改的字段 (stop_loss / take_profit / position_size)
            modified_value: 修改后的值

        Returns:
            {"success": bool, "message": str}
        """
        inst = self._find_instruction_by_id(instruction_id)
        if not inst:
            return {"success": False, "message": "指令不存在"}

        original_value = str(inst.get(modified_field, ""))

        self.db.save_user_feedback({
            "instruction_id": instruction_id,
            "action": "modified",
            "modified_field": modified_field,
            "original_value": original_value,
            "modified_value": modified_value,
        })

        logger.info(f"Playbook modified: {inst['symbol']} {modified_field}={modified_value}")
        return {"success": True, "message": f"已修改 {modified_field}"}

    # ========== 内部方法 ==========

    def _find_instruction_by_id(self, instruction_id: int) -> Optional[Dict]:
        """在最近的指令中查找"""
        instructions = self.db.get_recent_instructions(limit=200)
        for inst in instructions:
            if inst["id"] == instruction_id:
                return inst
        return None

    def _get_active_patterns_summary(self) -> List[Dict]:
        """获取活跃 patterns 摘要"""
        try:
            patterns = self.db.get_patterns(status="active")
            return [{
                "pattern_key": p["pattern_key"],
                "conditions": p.get("conditions_json", []),
                "win_rate": p.get("win_rate", 0),
                "total_trades": p.get("total_trades", 0),
            } for p in patterns[:5]]
        except Exception:
            return []

    @staticmethod
    def _et_today() -> str:
        """获取 ET 时区的今日日期"""
        try:
            from app.web_dashboard.data_service import et_today
            return et_today()
        except ImportError:
            return date.today().isoformat()
