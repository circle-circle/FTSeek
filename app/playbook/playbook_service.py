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
import threading
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, date

from app.core import paths as core_paths
from app.playbook.db import PlaybookDB
from app.playbook.live_arm_gate import count_open_live_positions, evaluate_live_arm_gate
from app.utils.timezone import now_et

logger = logging.getLogger(__name__)

# Singleton
_service_instance = None

# 2026-08-21 (盲区A): 重复开仓守卫审计去重 — 每 (instruction_id, symbol) 首次拦截才写审计行,
#   监控循环每轮重试不重复记账. 进程重启清空无害 (审计行非关键路径).
_DUP_GUARD_AUDITED: set = set()

# pipeline_view TTL 缓存 (秒). 盘中数据每 5min 才变, 30s 缓存挡住高频轮询打 DB.
_PV_TTL = 30

# ── 本模块私有 MarketDataRouter 单例 (2026-09-03, DEB-20260903-intraday-chain-audit §五 D3(b)) ──
# 病灶: 下单路径两处**每单** new MarketDataRouter() → 每单一条 OpenD 连接 + 一个 30s 健康守护
#   线程; 守护线程 while True 无停止位且强引用 self → 实例与其 socket 永不回收 (实测 21:32 重启
#   后 50min 内新建 17 次, 进程 32 socket / 73 线程, 且 09-02 全日 26 次)。
# 本修法只把 "N 个实例" 收敛成 "1 个实例", 构造参数与原先逐字相同 (MarketDataRouter() 全默认)
#   → 取价仍走**新建实例的快照路径** (get_market_snapshot, 带真 bid/ask), 报价口径零变更。
# 为什么不用 market_data_router.get_market_data_router() 那个进程级共享单例: 共享实例是 scheduler
#   注册的主 router, 监控池标的都在其 _subscribed_symbols 内 → get_quote 会走订阅/push 路径
#   (futu_lv3_provider.py:824-851 get_stock_quote 无盘口, bid=ask=last 兜底; push 路径同)。
#   换过去等于同时改 arrival price (滑点尺子) + P1 深跳空降仓探针 + 报价健康门严格度 → 行为变更,
#   须按 C4 影子先行 + 宪章二.2 新旧基线切分另案处理 (DEB §五 (a))。本私有单例从不订阅任何标的,
#   故 _subscribed_symbols 恒空 → 永远走快照路径, 与今日逐单新建同源同口径。
# 为什么每次取价前清该 symbol 的报价缓存: 长生命周期实例会带 provider 侧缓存 (Futu 5s /
#   Finnhub·TwelveData 300s), 而"每单新建"永远空缓存。不清就会在同一次下单内 (arrival price →
#   成交价 quote 兜底, 确认窗最快 <5s 返回) 复用旧价, 给记账/滑点尺子引入无法归因的新鲜度差。
#   清掉后与新建实例逐字等价 (总是新取一次)。缓存清空失败不影响取价 (fail-open)。
_private_router = None
_private_router_lock = threading.Lock()
# provider 侧报价缓存键 (futu_lv3_provider.py:815 / finnhub_provider.py:215 / twelvedata_provider.py:271)
_PRIVATE_ROUTER_QUOTE_CACHE_KEYS = ("quote:{sym}", "fh_quote_{sym}", "quote_{sym}")


def _get_private_router():
    """返回本模块私有 MarketDataRouter 单例 (懒创建, 双重检查锁)。"""
    global _private_router
    if _private_router is None:
        with _private_router_lock:
            if _private_router is None:
                from app.data.market_data_router import MarketDataRouter
                _private_router = MarketDataRouter()
    return _private_router


def _private_router_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """经私有单例取一次报价, 语义等同"每单新建 router" (不吃 provider 报价缓存)。"""
    router = _get_private_router()
    if router is None:
        return None
    for _prov_attr in ("futu", "fh", "td"):
        _prov = getattr(router, _prov_attr, None)
        _cache = getattr(_prov, "_cache", None)
        if not isinstance(_cache, dict):
            continue
        _cache_time = getattr(_prov, "_cache_time", None)
        for _key_tpl in _PRIVATE_ROUTER_QUOTE_CACHE_KEYS:
            _key = _key_tpl.format(sym=symbol)
            _cache.pop(_key, None)
            if isinstance(_cache_time, dict):
                _cache_time.pop(_key, None)
    return router.get_quote(symbol)


def _gap_dive_should_halve(direction: str, prev_close: float, today_open: float,
                           position_size: int, *, halve_pct: float = -3.0) -> bool:
    """深跳空逆势 BUY 降仓判定 (2026-08-20, DEB §44/§47 P1, 用户裁决).

    证据: gap≤-3% 逆势 BUY 全历史 n=14 净 -772 (胜率 21%), 8/18 MU/FIX/CAT 三笔
    隔夜 SL_INITIAL -818; gap-through 会跳穿止损, 紧 SL 救不了, 仅仓位端可缓解.
    顺势 SHORT 同条件 n=5 净 +11.84 → 只拦 BUY. 纯函数便于测试; 任一输入缺失
    → False (fail-open 不降仓, 与 entry_gap 探针同纪律). 回滚: YAML 置 false.
    """
    try:
        if direction != "BUY":
            return False
        if not position_size or position_size <= 1:
            return False
        if not prev_close or prev_close <= 0 or not today_open or today_open <= 0:
            return False
        gap_pct = (today_open - prev_close) / prev_close * 100.0
        return gap_pct <= halve_pct
    except Exception:
        return False


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
    """从 playbook 指令提取 ATR 绝对价 (兼容包装, 见 _extract_atr_with_basis)。

    2026-09-02 (DEB-20260831 §8.7 atr_basis 打标): 拆出 with_basis 变体返回
    (值, 来源), 本函数保持纯 float 契约 (tests/test_atr_source_unit_extraction.py)。
    """
    return _extract_atr_with_basis(inst)[0]


def _extract_atr_with_basis(inst: Dict[str, Any]) -> tuple:
    """从 playbook 指令提取 ATR 数值, 写入 trade_records.metadata_json.atr (诊断用).

    2026-06-24 修复: playbook_instructions 表无 atr_source 顶层列 (schema 见 db.py),
    atr_source 嵌在 raw_instruction_json blob 里 (instruction_generator 产出). 此前
    inst.get("atr_source","") 永远返回 "" → "=" not in "" → metadata.atr 恒为 0,
    导致事后复盘"SL 距离是否合理"无法诊断.

    ⚠ 本值自 e32d376f (2026-09-01 ATR 比例紧 SL) 起**承重退出行为**: pipeline 规则0.5
    读 metadata.atr 为**绝对价** (atr% = atr/entry×100). 原注释"退出逻辑本身不读此值"
    已失效, 入库必须统一绝对价口径.

    2026-09-02 单位修复 (DEB-20260831 §8.8): instruction_generator 新格式是百分比
    ("VOL_medium: atr_pct=3.13% sl=4.50%"), 旧格式是绝对价 ("technical.atr_14=8.35").
    此前解析器不分单位 → 百分比直落绝对值槽, LLY(entry≈1178) 被规则0.5 算成
    atr%=0.27% → tight 落 1% 地板, V 型日误杀行为复活. 现按 "atr_pct=" 键名识别
    百分比并乘入场价转绝对值.

    2026-07-16 修复: DA bridge 直写 instruction 时无 atr_source 字段 → metadata.atr=0.
    修复: atr_source 缺失时, 从 entry_price 和 stop_loss 反算隐含 ATR% (SL 距离 / vol_bucket 倍数).
    虽然不是真实 ATR, 但反映了 vol_bucket 级别, 供事后诊断 SL 合理性.

    atr_source 格式 (来自 instruction_generator):
      "technical.atr_14=8.35 (quality=good)"          ← 绝对价 (旧)
      "daily_kline.atr_14=8.35 (quality=good)"        ← 绝对价 (旧)
      "FALLBACK: price×2%=8.35 (ATR missing)"         ← 绝对价 (旧)
      "VOL_medium: atr_pct=3.13% sl=4.50% rr=1.80"    ← 百分比 (2026-09-01 起, 可能追加 vix_widen= 后缀)
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
            _val = float(raw_val.rstrip("%"))
            # 2026-09-02 单位修复 (docstring 详见上): "atr_pct=" 键名 = 百分比格式,
            #   乘入场价转绝对价入库; 旧格式键名 (atr_14= / price×2%=) 本就是绝对价.
            if "atr_pct=" in src:
                _entry = float(inst.get("entry_price", 0) or 0)
                if _entry > 0:
                    return round(_entry * _val / 100.0, 2), "real"
            else:
                return _val, "real"
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
        return round(entry * sl_pct / 100, 2), "proxy"  # 返回隐含 ATR 绝对值
    return 0.0, "missing"



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
        # 2026-08-03 (改进批 任务#4): factor_screen 自 2026-07-10 无生产者 (死文件).
        #   陈旧文件的 per-symbol 因子分/composite 照常展示会被误读为当日评分 →
        #   陈旧时清空 screen_map (item 侧 factor_scores/composite 归零), 但保留
        #   screen_scored/screen_date/screen_stale 供漏斗层如实呈现"筛选非当日".
        # DEB-20260903 #88: 与 L486 screen_stale 语义对齐 — 原仅在 screen_date 非空
        #   且不匹配时清空; 当 screen_date 为空(无 timestamp)时 screen_stale=True 但
        #   screen_map 未清空, 陈旧因子分仍被当当日评分展示. 改为同一判定口径.
        if (not screen_date) or screen_date != trade_date:
            screen_map = {}

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
            # 2026-08-03 (任务#4): 文件缺失(已归档死文件)也视为 stale — 否则漏斗
            #   会把"无筛选数据"显示为"筛选正常", 比陈旧更误导.
            "screen_stale": (not screen_date) or screen_date != trade_date,
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
                # 2026-08-26: trade_date 实测为纯 'YYYY-MM-DD' 文本 (signal_snapshots 全表
                # 4118 行无时间成分), date() 包裹使 idx_snap_date 失效; 改等值比较, 等价且走索引。
                "FROM signal_snapshots WHERE trade_date = ? "
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
            # DEB-20260903 #89: 读侧与写侧同源 — 原硬编码 root/data/shadow 绝对路径,
            #   写侧(pipeline._log_playbook_direction_audit)走
            #   playbook.technical_arbitration.shadow_log_path 配置, 两侧可漂移失联.
            from app.core.config_container import get_config as _gc_audit
            log_path = Path(str(_gc_audit().get_dotted(
                "playbook.technical_arbitration.shadow_log_path",
                "data/shadow/playbook_direction_audit.jsonl")))
            if not log_path.exists():
                return {}
            result = {}
            bad_lines = 0
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
                        bad_lines += 1
                        continue
            if bad_lines:
                # R5 纪律留痕: 解析失败行不静默 (DEB-20260901 Phase C 棘轮修复)
                logger.warning("[playbook] 方向审计 jsonl 解析失败 %d 行已跳过: %s",
                               bad_lines, log_path)
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
        target_env: Optional[str] = None,
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

        # 风控分级复权缩放 (2026-09-05, 用户裁"按照建议执行"): 两个半仓来源取 min —
        #   ① OKR 账户熔断的 REDUCE_50 档 (此前**无任何消费方**, 见下方接线);
        #   ② 绝对熔断连亏冷静期到期后的"观察期"(AbsoluteCircuitBreaker.get_position_scale)。
        #   只缩**新开仓名义**, 不触碰已持仓的 SL/TP/退出 (退出快环永不受熔断影响)。
        _risk_scale = 1.0

        # 2026-06-25: 账户级熔断 guard (OKR)
        # 若当日亏损 ≥2% 停止新仓, ≥3% LIQUIDATE 档 (强制全平从未接线, 实际与
        # 停新仓同效 — 见 account_circuit_breaker.py 头注 2026-08-28 纠偏).
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
                elif _cb.action == "REDUCE_50":
                    # 2026-09-05 接线: account_circuit_breaker 定义了 NORMAL/REDUCE_50/HALT/
                    #   LIQUIDATE 四档, 而本处此前只拦 HALT/LIQUIDATE → 日亏 -1%~-2% 区间
                    #   **什么都不做**, 从"正常"直接跳"停新仓", 中间那级是纸面的 (全仓
                    #   grep REDUCE_50 唯一产出点即 account_circuit_breaker.py:123, 零消费方)。
                    #   接上它=给熔断一个"观察档": 不停新开 (符合数据采集目标), 只把名义减半。
                    #   实证零成本: 现行制度 48 个交易日最差日 -$793.60 = 权益的 -0.80%,
                    #   从未进入 -1% 档 → 本分支历史触发 0 次。
                    _risk_scale = min(_risk_scale, 0.5)
                    logger.warning(
                        f"[OKR熔断·REDUCE_50] {_cb.reason} → 本笔名义×0.5 "
                        f"(分级复权, 不停新开仓)")
            else:
                # DEB-20260823 X2: equity≤0(启动首查前/断供窗口)时熔断失明——原实现静默
                # 跳过检查, 与下方异常路径 acb_fail_mode=closed 哲学矛盾. 改为拒单,
                # update_account 刷新出真实权益后自动恢复.
                _msg = f"[OKR熔断·fail-close] 账户权益不可用({_equity}), 熔断无法评估, 拒绝开仓"
                logger.error(_msg)
                return {"success": False, "message": _msg}
        except Exception as _cb_err:
            # DEB-20260721 R4 GATE-1: 纳入 risk.acb_fail_mode 统一 fail 语义 (C-7 的第三份拷贝).
            #   当前 mode=closed → 检查失明即拒绝开仓 (行为变更, 与批次② C-7 裁决同向:
            #   失明的熔断器=没有熔断器). 回滚: acb_fail_mode 改回 open.
            try:
                from app.core.config_container import get_config as _get_cfg_okr
                _okr_fail_mode = _get_cfg_okr().get_dotted("risk.acb_fail_mode", "closed")
            except Exception:
                _okr_fail_mode = "closed"
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
            _acb = get_absolute_circuit_breaker()
            _allowed, _acb_reason = _acb.can_open_new_position("playbook")
            if not _allowed:
                logger.warning(f"[绝对熔断] {_acb_reason}, 拒绝开仓")
                return {"success": False, "message": f"[绝对熔断] {_acb_reason}"}
            # 连亏冷静期到期 → 观察期半仓 (不是直接全放行)。实测现行制度 48 个交易日中
            #   11 天触发连亏暂停, 其中 4 天当日再无盈利平仓 → 旧语义一路锁到收盘,
            #   半天成交样本丢失; 新语义 30min 冷静 + 3 笔半仓观察, 观察期内亏损即回冷静期。
            _acb_scale = float(_acb.get_position_scale("playbook") or 1.0)
            if _acb_scale < 1.0:
                _risk_scale = min(_risk_scale, _acb_scale)
                logger.warning(
                    f"[绝对熔断·观察期] {inst.get('symbol')} 连亏冷静期已过, 本笔名义×{_acb_scale} "
                    f"(观察期内出现亏损即回到冷静期)")
        except Exception as _acb_err:
            # A1+: 配置驱动 fail 模式 (open=放行, closed=拒绝)
            try:
                from app.core.config_container import get_config as _get_cfg_acb
                _acb_fail_mode = _get_cfg_acb().get_dotted("risk.acb_fail_mode", "closed")
            except Exception:
                _acb_fail_mode = "closed"
            if _acb_fail_mode == "closed":
                logger.error(f"[playbook] 绝对熔断检查异常 (fail-close 拒绝开仓): {_acb_err}")
                return {"success": False, "message": f"绝对熔断检查异常 (fail-close): {_acb_err}"}
            logger.warning("[playbook] 绝对熔断检查异常 (放行): %s", _acb_err)

        # ── R-5 (2026-08-23 DEB-20260823): 资本安全水位硬门 ──
        # can_open_position() 此前全仓唯一消费者是 web_dashboard/order/validator.py:162,
        # Playbook 路径从未调用 → RED(-10~-20%)/BLACKOUT(>-35%)/系统锁定/状态保存失败
        # 全部拦不住。sizing 侧 pos_mult=0 又被 instruction_generator max(1,size) 兜成 1 股,
        # 故回撤 -20% 仍能开仓。此门是唯一能真正拒单的位置。
        try:
            from app.core.config_container import get_config as _get_cfg_ws
            _ws_gate_on = bool(_get_cfg_ws().get_dotted("risk.capital_safety.playbook_hard_gate", True))
        except Exception:
            _ws_gate_on = True
        if _ws_gate_on:
            try:
                from app.risk.capital_safety import get_capital_safety_system as _get_cs_gate
                _cs_ok, _cs_msg = _get_cs_gate().can_open_position()
                if not _cs_ok:
                    logger.warning(f"🛑 [资本安全水位] {inst.get('symbol','')} 拒绝开仓: {_cs_msg}")
                    return {"success": False, "message": f"[资本安全水位] {_cs_msg}"}
            except Exception as _cs_err:
                # fail-close: 水位查不到 = 不知道自己在哪个回撤档 → 不开新仓
                logger.error(f"🛑 [资本安全水位·fail-close] 检查异常, 拒绝开仓: {_cs_err}")
                return {"success": False, "message": f"资本安全水位检查异常 (fail-close): {_cs_err}"}

        # ── R-1 (2026-08-23 DEB-20260823): VIX 门 ──
        # order_executor._submit_order_inner 有「VIX 新鲜度 fail-close + VIX>28 硬拒」两道门,
        # 但 Playbook 不经 order_executor (dual_broker 直连 ctx.place_order), 全链路 grep
        # app/playbook/ 零 VIX 拦截 — entry_vix 仅被记账不被消费。此处补齐, 语义照抄主线。
        try:
            from app.core.config_container import get_config as _get_cfg_vix
            _vix_gate_on = bool(_get_cfg_vix().get_dotted("risk.playbook_vix_gate_enabled", True))
        except Exception:
            _vix_gate_on = True
        if _vix_gate_on:
            _vix_val = None
            try:
                # 2026-09-14 (盘前数据有效性审计 P0-1): 紧随其后的 `if _vix_val is None`
                #   分支自上线起**不可达** — 上游 market_state._fetch_vix_data 末行恒
                #   `return 20.0`, 故注释宣称的"新鲜度 fail-close"当时是纸面的 (与
                #   order_executor 同款同因)。上游已改为如实返回 None, 本分支自此真实生效,
                #   且上游现于盘前/盘中优先取 Futu VIXY 实时代理 (不再读 T-1 FRED 日频)。
                from app.analysis.market_state import get_checker as _get_vix_checker
                _vix_val = _get_vix_checker().get_vix_cached()
            except Exception as _vix_err:
                logger.error(f"🔒 [VIX·fail-close] VIX 获取异常, 拒绝开仓: {_vix_err}")
                return {"success": False, "message": f"VIX 获取异常 (fail-close): {_vix_err}"}
            if _vix_val is None:
                logger.warning("🔒 [VIX·fail-close] VIX 数据不可用, 拒绝开仓 (宁可错过不可放错)")
                return {"success": False, "message": "VIX 数据不可用 (fail-close)"}
            try:
                from app.risk.vix_guard import VIXGuard as _VIXGuard
                _vix_blocked, _vix_reason = _VIXGuard().should_block(float(_vix_val))
            except Exception as _vg_err:
                # VIXGuard 内部逻辑异常不升级为拒单 (与主线 :3232 同口径);
                # 新鲜度门已在上方 fail-close 兜住"取不到数"这一真风险。
                logger.warning(f"⚠️ [VIX_GUARD_ERR] VIXGuard 异常, 不拦截: {_vg_err}")
                _vix_blocked, _vix_reason = False, ""
            if _vix_blocked:
                logger.warning(f"🚫 [VIX_HARD_BLOCK] {inst.get('symbol','')} {_vix_reason} (VIX={_vix_val})")
                return {"success": False, "message": f"[VIX硬拒] {_vix_reason}"}

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

        # DEB-20260903 #90: 逐键回退而非整体切换 — 原实现 custom_params 为非空 dict 但缺键时,
        # .get() 返回 None 覆盖指令原始值 (entry/SL/TP 丢失, position_size 被兑底成 1 股)
        # DEB-20260904 #179: 键存在但值为 None 时 .get 仍返回 None 覆盖原值 — 改为值非 None 才覆盖
        _cp = custom_params or {}
        entry_price = _cp["entry_price"] if _cp.get("entry_price") is not None else inst.get("entry_price", 0)
        stop_loss = _cp["stop_loss"] if _cp.get("stop_loss") is not None else inst.get("stop_loss", 0)
        take_profit = _cp["take_profit"] if _cp.get("take_profit") is not None else inst.get("take_profit", 0)
        position_size = _cp["position_size"] if _cp.get("position_size") is not None else inst.get("position_size", 0)
        if not position_size or position_size <= 0:
            position_size = 1

        # 议题二C (2026-08-01, 用户授权"授权你执行"): BUY 半码 — 已执行 BUY 实证负 edge
        #   (-0.3~-0.5%/笔, n=134-157, p≈0.04; 对照 SHORT +1.5%/笔 p<0.01).
        #   截断取整偏保守 (减风险方向). 一行回滚: buy_position_factor 改回 1.0.
        if direction == "BUY":
            try:
                from app.core.config_container import get_config as _get_cfg_pf
                _buy_pf = float(_get_cfg_pf().get_dotted("playbook.direction_gate.buy_position_factor", 1.0) or 1.0)
            except Exception:
                _buy_pf = 1.0
            if 0 < _buy_pf < 1 and position_size > 1:
                _ps_before = position_size
                position_size = max(1, int(position_size * _buy_pf))
                logger.info(f"[playbook] {symbol} BUY 仓位因子 (议题二C): {_ps_before} → {position_size} (factor={_buy_pf})")

        # 准入门二次复核 (2026-08-06, 用户指示): 执行时 position_size 已确定 (F1 SSOT/factor),
        #   弥补 save_instruction 入口 position_size=None → notional=0 → 费用类判据空转的问题.
        #   生产 YAML 当前 enforce=true, REJECT 会拦截 (记 skip_reason 不落 executed);
        #   代码 fallback enforce=false 时仅审计。异常 fail-open 放行 (宁漏不误杀).
        try:
            from app.playbook.admission import evaluate as _adm_eval
            _adm_result = _adm_eval({
                "symbol": symbol, "direction": direction,
                "entry_price": entry_price, "take_profit": take_profit,
                # [DEB-fullchain #26 2026-08-16] 补 stop_loss 键: admission max_sl_pct 判据
                #   读 inst["stop_loss"], 此前缺键 → SL 宽度判据执行侧恒空转 (入口拦/执行放
                #   双标)。当前 max_sl_pct=0 无行为差, 该门重启前置条件已满足。
                "stop_loss": stop_loss,
                "position_size": position_size,
                "confidence": inst.get("confidence", 0) or 0,
                "date": inst.get("date", ""),
            })
            _adm_verdict = _adm_result.get("verdict", "PASS")
            _adm_reasons = _adm_result.get("reasons", []) or []
            _adm_enforce = bool(_adm_result.get("enforce", False))
            if _adm_verdict == "REJECT":
                _adm_metrics = _adm_result.get("metrics", {}) or {}
                logger.warning(
                    f"[admission] {symbol} {direction} 执行时复核 REJECT "
                    f"reasons={_adm_reasons} notional={_adm_metrics.get('notional')} "
                    f"enforce={_adm_enforce}"
                )
                if _adm_enforce:
                    _adm_why = "; ".join(_adm_reasons)
                    try:
                        self.db.update_instruction_tracking(
                            instruction_id, {"skip_reason": f"admission: {_adm_why}"}
                        )
                    except Exception:
                        pass
                    return {"success": False, "message": f"admission 拦截: {_adm_why}",
                            "admission_blocked": True}
        except Exception as _adm_exec_err:
            logger.warning(f"[admission] {symbol} 执行时复核异常 (fail-open 放行): {_adm_exec_err}")

        # 应用风控分级复权 (放在准入门二次复核**之后**: 与 LIVE_TRIAL/gap_dive 两处半仓
        #   先例同序, 且不会因随后缩小名义而新触发 admission 的 NOTIONAL_TOO_SMALL。
        #   生产 min_notional_usd=2500; 当前顺序按缩放前名义复核, 避免风险降仓二次拒绝)。
        #   已知代价 (与两处先例同源): admission 的 fee_bps 判据评的是缩放前的名义,
        #   若将来启用 min_profit_fee_ratio 须把缩放移到复核之前, 否则判据与实际下单不符。
        if _risk_scale < 1.0 and position_size and position_size > 1:
            _ps_before_risk = int(position_size)
            position_size = max(1, int(position_size * _risk_scale))
            logger.warning(
                f"[playbook] {symbol} 风控分级复权: 仓位 {_ps_before_risk} → {position_size} "
                f"(scale={_risk_scale}) — 半仓观察, 非停新开")

        # 方向映射
        # DEB-20260904 #180: 白名单校验 — 旧逻辑 direction==BUY→LONG 其余一律 SHORT,
        # 任何意外值 (typo/空串/SELL) 被静默当 SHORT 下单
        _dir_upper = str(direction or "").strip().upper()
        if _dir_upper == "BUY":
            mos_direction = "LONG"
        elif _dir_upper in ("SELL_SHORT", "SHORT", "SELL"):
            mos_direction = "SHORT"
        else:
            _bad_dir_reason = f"direction 非法: {direction!r} (仅允许 BUY/SELL_SHORT)"
            logger.error(f"[playbook] {symbol} {_bad_dir_reason}, 拒单")
            try:
                self.db.update_instruction_tracking(instruction_id, {"skip_reason": _bad_dir_reason})
            except Exception:
                pass
            return {"success": False, "message": _bad_dir_reason, "skipped_bad_direction": True}

        # 议题二C (2026-08-01, 用户授权): BUY 日执行配额 — 负 edge 方向限流
        #   (基线 3.65 笔/日 → 配额 2 笔/日). 超出配额的 BUY 不置 executed,
        #   记 skip_reason=buy_quota, 指令保留落库继续攒未执行 open 基样本.
        #   一行回滚: buy_daily_exec_quota 改回 999.
        if direction == "BUY":
            try:
                from app.core.config_container import get_config as _get_cfg_dg
                _dg_quota = int(float(_get_cfg_dg().get_dotted("playbook.direction_gate.buy_daily_exec_quota", 999) or 999))
            except Exception:
                _dg_quota = 999
            if _dg_quota < 999:
                try:
                    _dg_date = inst.get("date") or now_et().date().isoformat()
                    _dg_done = self.db.count_executed_by_direction(_dg_date, "BUY")
                    if _dg_done >= _dg_quota:
                        _dg_reason = f"buy_quota: 今日BUY已执行{_dg_done}笔 ≥ 配额{_dg_quota} (议题二C 负edge限流)"
                        logger.info(f"[playbook] {symbol} BUY 日配额拦截: {_dg_reason}")
                        self.db.update_instruction_tracking(instruction_id, {"skip_reason": _dg_reason})
                        return {"success": False, "message": _dg_reason, "skipped_buy_quota": True}
                except Exception as _dg_e:
                    logger.warning(f"[playbook] {symbol} BUY 配额检查异常 (fail-open 放行): {_dg_e}")

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
                    # 2026-08-26: 同 _get_vote_map — trade_date 实测纯 'YYYY-MM-DD',
                    # date() 包裹废掉 idx_snap_date + idx_snap_symbol 选择度; 改等值走索引。
                    "SELECT decision_id FROM signal_snapshots WHERE symbol = ? AND trade_date = ? ORDER BY strength DESC LIMIT 1",
                    (symbol, _trade_date),
                ).fetchone()
                if _row:
                    _matched_decision_id = _row["decision_id"]
                    logger.info(f"[playbook] matched decision_id={_matched_decision_id} for {symbol} (from signal_snapshots fallback)")
                _snap_conn.close()
            except Exception as _e:
                logger.debug(f"[playbook] decision_id lookup failed: {_e}")

        # [DEB-20260901 选项1②] archetype 路由提取 (pipeline 经 custom_params 携带;
        #   默认关闭/未携带 = None, 下单走市价现状, 零行为变更)。
        _route_exec = custom_params.get("archetype_route") if isinstance(custom_params, dict) else None
        if not (isinstance(_route_exec, dict) and _route_exec.get("action") == "limit"):
            _route_exec = None

        # ── broker 路径: 显式 target_env 是唯一交易臂入口 ──
        # Path A (AUTO_TRADE_ACCOUNT=real 全局翻转/降级) 已于 RFC-131 收口退役:
        # 未显式传 target_env 的调用只能执行 SIM；请求 real 一律 HALT，不再静默降级。
        import os as _os
        _explicit_target = str(target_env or "").strip().lower()
        if _explicit_target in {"sim", "real"}:
            return self._execute_via_dual_broker(
                inst, instruction_id, symbol, direction, entry_price, stop_loss,
                take_profit, position_size, mos_direction, _matched_decision_id,
                target_env=_explicit_target, archetype_route=_route_exec,
            )

        _auto_account = _os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()

        if _auto_account == "real":
            logger.critical(
                "[playbook] AUTO_TRADE_ACCOUNT=real without explicit target_env=real — "
                "Path A retired; HALT_NEW_LIVE. Use playbook.live_manual_arm."
            )
            return {
                "success": False,
                "message": "[LIVE_PATH_A_RETIRED] explicit target_env=real and live_manual_arm are required",
            }

        # AUTO_TRADE_ACCOUNT=sim → 模拟盘真实下单 (futu_simulate, OKR 验证)
        return self._execute_via_dual_broker(
            inst, instruction_id, symbol, direction, entry_price, stop_loss,
            take_profit, position_size, mos_direction, _matched_decision_id,
            target_env="sim", archetype_route=_route_exec,
        )

    # ── R-1 (2026-08-23 DEB-20260823): per-symbol 下单互斥 ──
    # duplicate_position_guard 读 open_positions (:1056), 而该表在下单成功之后才写 (:1556)
    # → TOCTOU 窗口。不同 instruction_id 的同标的指令从 LLMStrategy 线程与 Dashboard 线程
    # 并发进入即可双开 (代码注释自述 7/2 GOOG SHORT×2 实锤)。try_claim_execution 只对同一
    # instruction_id 原子, 挡不住这个。此处把「查重→下单→写库」整段圈成 per-symbol 临界区。
    _SYMBOL_ORDER_LOCKS: Dict[str, threading.Lock] = {}
    _SYMBOL_ORDER_LOCKS_GUARD = threading.Lock()

    @classmethod
    def _get_symbol_order_lock(cls, symbol: str) -> threading.Lock:
        with cls._SYMBOL_ORDER_LOCKS_GUARD:
            _lk = cls._SYMBOL_ORDER_LOCKS.get(symbol)
            if _lk is None:
                _lk = threading.Lock()
                cls._SYMBOL_ORDER_LOCKS[symbol] = _lk
            return _lk

    def _execute_via_dual_broker(
        self, inst, instruction_id, symbol, direction, entry_price, stop_loss,
        take_profit, position_size, mos_direction, matched_decision_id, target_env,
        archetype_route=None,
    ):
        """per-symbol 互斥包装层 (R-1). 真实逻辑在 _execute_via_dual_broker_inner.

        非阻塞获取: 抢不到锁 = 同标的另有下单在途 → 直接拒绝, 指令保持 pending 下轮再试。
        刻意不阻塞等待, 因为持锁方可能正在等 broker 成交查询 (秒级), 阻塞会拖住整个
        auto_execute 循环。
        """
        _lock = self._get_symbol_order_lock(symbol)
        if not _lock.acquire(blocking=False):
            logger.warning(
                f"🔒 [SymbolLock] {symbol} 同标的下单在途, 本次拒绝 (instruction_id={instruction_id})"
            )
            return {"success": False, "message": f"[SymbolLock] {symbol} 同标的下单在途, 稍后重试"}
        try:
            return self._execute_via_dual_broker_inner(
                inst, instruction_id, symbol, direction, entry_price, stop_loss,
                take_profit, position_size, mos_direction, matched_decision_id, target_env,
                archetype_route=archetype_route,
            )
        finally:
            _lock.release()

    def _execute_via_dual_broker_inner(
        self, inst, instruction_id, symbol, direction, entry_price, stop_loss,
        take_profit, position_size, mos_direction, matched_decision_id, target_env,
        archetype_route=None,
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

        if _is_live:
            _arm_cfg = {}
            try:
                from app.core.config_container import get_config as _get_cfg_arm
                _arm_cfg = _get_cfg_arm().get_dotted("playbook.live_manual_arm", None) or {}
            except Exception as _ae:
                logger.error(f"[playbook] LIVE 臂开关读取异常 (fail-closed 拒绝): {_ae}")
                _arm_cfg = {}

            import os as _os_live
            try:
                from app.core.scheduler_triggers import get_market_phase
                _market_phase = get_market_phase().value
            except Exception as _phase_error:
                logger.error(f"[playbook] LIVE market phase read failed: {_phase_error}")
                _market_phase = ""

            _gate = evaluate_live_arm_gate(
                _arm_cfg,
                allow_real_trading=_os_live.getenv("ALLOW_REAL_TRADING", "false").strip().lower() == "true",
                real_account_id=_os_live.getenv("REAL_ACCOUNT_ID", ""),
                lane_db_name=getattr(self.db, "db_name", ""),
                instruction_direction=str(direction or ""),
                market_phase=_market_phase,
                open_live_positions=count_open_live_positions(
                    core_paths.get_trade_records_db_for_arm("real")
                ),
            )
            if not _gate.get("allowed"):
                _reason_text = "; ".join(
                    reason.get("message", "unknown") for reason in _gate.get("reasons", [])
                )
                logger.critical(
                    f"🚫 [playbook] LIVE arm gate rejected {symbol}: {_reason_text} — HALT_NEW_LIVE"
                )
                return {
                    "success": False,
                    "message": f"[LIVE_ARM_GATE] {symbol} {_reason_text}",
                    "reasons": _gate.get("reasons", []),
                }

            _probe = _arm_cfg.get("probe") if isinstance(_arm_cfg.get("probe"), dict) else {}
            _max_qty = 1
            if not position_size:
                position_size = 1
            elif int(position_size) > _max_qty:
                logger.info(
                    f"[playbook] LIVE engineering probe clamps {symbol} qty "
                    f"{int(position_size)} → {_max_qty}"
                )
                position_size = 1

            try:
                from app.push import get_push_hub
                from app.push.message import Level as _PushLevel
                get_push_hub().push_text(
                    f"🟡 [LIVE C-lite] {symbol} LONG 1股探针准备提交 "
                    f"(auth={_probe.get('authorization_id')})",
                    level=_PushLevel.HIGH,
                )
            except Exception as _probe_push_error:
                logger.warning(f"[playbook] LIVE probe push failed: {_probe_push_error}")

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

        # P0 (2026-08-28 全链路复查): 单标的当日亏损限额 — 死亡螺旋防线接进实盘执行链.
        #   根因: 该防线 ($500/标的/日) 此前只写在 order_executor._check_symbol_daily_loss,
        #   位于 2026-07-02 起冻结的 MAIN_PIPELINE 开仓路径 (本函数经 dual_broker 直连下单,
        #   不经 order_executor) → 实盘链路上没有任何单标的日亏损门, MU 当日连亏 5 笔仍可
        #   续开 MU。口径与阈值共用 app/risk/symbol_day_loss (出场日记账 + 净额 + 已了结)。
        #   fail 语义沿用主线同名门 (fail-open + WARNING, 盲区不拦但留痕)。
        #   回滚: unified_config 的 playbook.execution_parameters 增
        #   symbol_daily_loss_gate_enabled: false + 重启 (缺键=默认开; 声明 true 会撞
        #   pre-commit 治理门"启用 disabled 策略需 ADR", 故此处只留说明不落键)。
        if direction in ("BUY", "SELL_SHORT"):
            try:
                from app.playbook.config import get_playbook_config as _get_pb_cfg_dl
                _dl_gate_on = bool(_get_pb_cfg_dl().get("symbol_daily_loss_gate_enabled", True))
            except Exception:
                _dl_gate_on = True
            if _dl_gate_on:
                try:
                    from app.risk.symbol_day_loss import is_symbol_day_loss_exceeded
                    _blocked, _day_net = is_symbol_day_loss_exceeded(symbol)
                    if _blocked:
                        logger.warning(
                            f"🚫 [playbook·单标的日亏损] {symbol} 当日已了结净额 "
                            f"${_day_net:.2f} 超限, 跳过 {_mode_label} 开仓"
                        )
                        return {
                            "success": False,
                            "message": f"{symbol} 当日累计亏损 ${_day_net:.2f} 超限额, 当日禁入",
                            "skipped_symbol_daily_loss": True,
                        }
                except Exception as _dle:
                    logger.warning(f"[playbook] 单标的日亏损门查询失败 (放行): {_dle}")

        # 2026-08-21 (盲区A): 重复开仓守卫 — 下单前查 open_positions 该 symbol 是否已有
        #   playbook 持仓 (size>0). W_WEEK 水下仓持满5交易日后, DA 每日重新提名同标的会
        #   二次开仓 (7/2 GOOG SHORT×2 实锤重叠). 单点收口: 本函数是全部 writer 的唯一
        #   执行路径. 回滚: duplicate_position_guard_enabled=false + 重启.
        # 2026-08-21 (Qoder §56.2-3 建议③): 拦截不落 skip_reason — 被拦指令保持 pending
        #   是 live-eval 正确语义 (持仓平掉后当日指令应可再入); 每指令首次拦截补一条
        #   去重审计行 (exec_gate_audit), 解决"每轮重复查询无痕迹"问题.
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_dup
            if bool(_get_pb_cfg_dup().get("duplicate_position_guard_enabled", False)):
                import sqlite3 as _dup_sql
                _dup_conn = _dup_sql.connect(
                    str(core_paths.get_trade_records_db_for_arm(target_env)), timeout=3)
                try:
                    _dup_conn.row_factory = _dup_sql.Row
                    _dup_row = _dup_conn.execute(
                        "SELECT size, owner_strategy FROM open_positions WHERE symbol=? AND size>0 LIMIT 1",
                        (symbol,),
                    ).fetchone()
                finally:
                    _dup_conn.close()
                if _dup_row is not None:
                    try:
                        _dup_audit_key = (instruction_id, symbol)
                        if _dup_audit_key not in _DUP_GUARD_AUDITED:
                            _DUP_GUARD_AUDITED.add(_dup_audit_key)
                            from app.playbook.pipeline import _emit_exec_gate_audit
                            _emit_exec_gate_audit(
                                {"id": instruction_id, "symbol": symbol,
                                 "direction": direction, "date": None},
                                action="blocked_duplicate_position",
                                entry=entry_price, size=position_size, notional=None,
                                extra={"held_size": _dup_row["size"],
                                       "held_owner": _dup_row["owner_strategy"]},
                            )
                    except Exception:
                        pass  # 审计行失败不阻断拦截返回
                    logger.info(
                        f"[playbook] {symbol} {_mode_label} 重复开仓拦截: 已有持仓 "
                        f"size={_dup_row['size']} owner={_dup_row['owner_strategy']} → 跳过"
                    )
                    return {
                        "success": False,
                        "message": f"{symbol} 已有持仓(size={_dup_row['size']}), 重复开仓拦截",
                        "skipped_duplicate_position": True,
                    }
        except Exception as _dup_err:
            logger.warning(f"[playbook] 重复开仓守卫检查异常 (放行): {_dup_err}")

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
                _pdt_fail_mode = _get_cfg_pdt().get_dotted("risk.acb_fail_mode", "closed")
            except Exception:
                _pdt_fail_mode = "closed"
            if _pdt_fail_mode == "closed":
                logger.error(f"[playbook] PDT 检查异常 (fail-close 拒绝开仓): {_pe}")
                return {"success": False, "message": f"PDT 检查异常 (fail-close): {_pe}"}
            logger.warning(f"[playbook] PDT 检查异常 (放行): {_pe}")

        # 滑点数据闭环 (P0): 下单前抓 arrival price (信号参考价), 事后与真实成交价对比.
        #   口径: bid/ask 中间价优先, 兜底 price；失败记 0，随后由目标环境决定是否放行。
        _arrival_price = 0.0
        _arr_ctx = None
        try:
            # D3(b): 私有单例取价 (原为每单 new MarketDataRouter() → OpenD 连接 + 守护线程泄漏)
            _arr_ctx = _private_router_quote(symbol)
            if _arr_ctx:
                _a_bid = float(_arr_ctx.get("lv3_bid") or _arr_ctx.get("bid") or 0)
                _a_ask = float(_arr_ctx.get("lv3_ask") or _arr_ctx.get("ask") or 0)
                if _a_bid > 0 and _a_ask > 0:
                    _arrival_price = round((_a_bid + _a_ask) / 2.0, 4)
                else:
                    _arrival_price = float(_arr_ctx.get("price", 0) or 0)
        except Exception as _arr_e:
            logger.debug(
                f"[playbook] {symbol} arrival price 抓取失败: {_arr_e}",
                exc_info=True,
            )

        # C2: 实盘新开仓必须至少有一个有效价格；此基础门不可关闭。
        _live_quote_price = _arrival_price
        if _is_live and _arr_ctx and _live_quote_price <= 0:
            try:
                _live_quote_price = float(_arr_ctx.get("last", 0) or 0)
            except (TypeError, ValueError):
                _live_quote_price = 0.0
        if _is_live and (_arr_ctx is None or _live_quote_price <= 0):
            logger.error(
                f"[playbook] {_mode_label} {symbol} 实盘无可用报价, C2 fail-close 拒绝开仓 "
                f"(instruction_id={instruction_id})"
            )
            return {"success": False,
                    "message": f"{_mode_label} {symbol} 实盘无可用报价 (C2 fail-close)",
                    "error_code": "LIVE_ENTRY_NO_QUOTE",
                    "c2_no_quote": True}

        # P1 深跳空逆势 BUY 降仓 (2026-08-20, DEB §44/§47, 用户裁决).
        #   _execute_via_dual_broker 是 DA直写/reentry/投票 全部 writer 的唯一执行路径
        #   → 单点覆盖 (guardrail 只在投票路径的教训, 见 §45.2). 复用已抓 _arr_ctx 零额外 API.
        #   异常/缺价 → fail-open 不降仓 (与 entry_gap 探针同纪律). 回滚: YAML gap_dive_halve_enabled=false+重启.
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_gap
            _gap_cfg = _get_pb_cfg_gap()
            if _gap_cfg.get("gap_dive_halve_enabled", False) and _arr_ctx and position_size:
                _gd_prev = float(_arr_ctx.get("prev_close", 0) or 0)
                _gd_open = float(_arr_ctx.get("open", 0) or 0)
                _gd_thr = float(_gap_cfg.get("gap_dive_halve_pct", -3.0))
                if _gap_dive_should_halve(direction, _gd_prev, _gd_open, position_size, halve_pct=_gd_thr):
                    _gd_orig = int(position_size)
                    position_size = max(1, int(position_size * 0.5))
                    logger.info(
                        f"[playbook] P1深跳空降仓: {_mode_label} {symbol} BUY gap≤{_gd_thr}% "
                        f"仓位 {_gd_orig}→{position_size} (DEB §47)"
                    )
        except Exception as _gd_e:
            logger.warning(f"[playbook] P1 gap 降仓判定异常 (fail-open 放行): {_gd_e}")

        # 下单前报价健康门 (Tier B 2026-07-19): 复用 check_quote_health 拦截过宽 spread / 过期报价.
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_gate
            _gate_cfg = _get_pb_cfg_gate()
            if _gate_cfg.get("pretrade_quote_gate_enabled", True):
                _g_bid = float((_arr_ctx or {}).get("lv3_bid") or (_arr_ctx or {}).get("bid") or 0)
                _g_ask = float((_arr_ctx or {}).get("lv3_ask") or (_arr_ctx or {}).get("ask") or 0)
                if _is_live and (_g_bid <= 0 or _g_ask <= 0):
                    logger.error(
                        f"[playbook] {_mode_label} {symbol} 实盘 bid/ask 缺失, C2 fail-close 拒绝开仓"
                    )
                    return {"success": False,
                            "message": f"{_mode_label} {symbol} 实盘 bid/ask 缺失 (C2 fail-close)",
                            "c2_quote_health": True}
                if _arr_ctx and _g_bid > 0 and _g_ask > 0:
                    from app.risk.data_health_guard import check_quote_health
                    _gate_quote = {
                        "bid": _g_bid,
                        "ask": _g_ask,
                        "price": float(_arr_ctx.get("price", 0) or _arr_ctx.get("last", 0) or 0),
                        "timestamp": _arr_ctx.get("timestamp"),
                    }
                    # DEB-ruler §7.5-A (2026-08-04): push 报价路径无真盘口, 兜底 bid=ask=last
                    # (futu_lv3_provider.py:426-431/791-795) → 真盘口恒有 ask>bid, 故
                    # bid==ask 即"数据退化"信号: spread 维度不可检, 跳过该维度只留 staleness.
                    _book_degenerate = abs(_g_bid - _g_ask) < 1e-9
                    _q_ok, _q_reason = check_quote_health(
                        _gate_quote,
                        max_age_seconds=float(_gate_cfg.get("pretrade_max_quote_age_sec", 60.0)),
                        max_spread_pct=(1.0 if _book_degenerate
                                        else float(_gate_cfg.get("pretrade_max_spread_pct", 0.05))),
                    )
                    if not _q_ok:
                        logger.warning(f"[playbook] {_mode_label} {symbol} 报价健康门拒绝下单: {_q_reason}")
                        return {"success": False, "message": f"报价健康门拒绝: {_q_reason}"}
        except Exception as _gate_e:
            if _is_live:
                logger.error(
                    f"[playbook] {_mode_label} {symbol} 报价健康门异常, 实盘 C2 fail-close 拒绝开仓: {_gate_e}",
                    exc_info=True,
                )
                return {"success": False,
                        "message": f"{_mode_label} {symbol} 实盘报价健康门异常 (C2 fail-close)",
                        "c2_gate_error": True}
            logger.warning(
                f"[playbook] {symbol} 报价健康门异常 (fail-open 放行): {_gate_e}",
                exc_info=True,
            )

        # B4 数据新鲜度门 (2026-07-20): 降级源 / 过期报价 → 禁开新仓。
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
            if _is_live:
                logger.error(
                    f"[playbook] {_mode_label} {symbol} 数据新鲜度门异常, 实盘 C2 fail-close 拒绝开仓: {_fresh_e}",
                    exc_info=True,
                )
                return {"success": False,
                        "message": f"{_mode_label} {symbol} 实盘数据新鲜度门异常 (C2 fail-close)",
                        "c2_gate_error": True}
            logger.warning(
                f"[playbook] {symbol} 数据新鲜度门异常 (fail-open 放行): {_fresh_e}",
                exc_info=True,
            )

        # [DEB-20260901 选项1② · ADR-035] archetype 路由: execute_instruction 从
        #   custom_params 提取后经参数透传 (本作用域无 custom_params)。默认关闭/
        #   未携带 = None → 市价现状, 零行为变更。
        _route = archetype_route if (isinstance(archetype_route, dict)
                                     and archetype_route.get("action") == "limit") else None
        if _route:
            _order = Order(
                symbol=symbol,
                direction=Direction.LONG if direction == "BUY" else Direction.SHORT,
                order_type=OrderType.LIMIT,
                quantity=int(position_size) if position_size else 1,
                price=float(_route["limit_price"]),  # 确认位限价
            )
        else:
            _order = Order(
                symbol=symbol,
                direction=Direction.LONG if direction == "BUY" else Direction.SHORT,
                order_type=OrderType.MARKET,
                quantity=int(position_size) if position_size else 1,
                price=0.0,  # 市价单
            )
        _order.metadata = {"playbook_instruction_id": instruction_id, "source": f"playbook_{_mode_label}"}

        # B1-lite: claim 原子进入 UNKNOWN 并返回 attempt；所有后续状态写均 CAS。
        _attempt = self.db.try_claim_execution(instruction_id)
        if _attempt is None:
            logger.warning(f"[playbook] {_mode_label} {symbol} 指令 {instruction_id} 已被抢占, 跳过 (防重复下单)")
            return {"success": False, "message": "指令已被执行或正在执行 (幂等抢占)"}
        _order.client_order_id = f"pb_{instruction_id}_{target_env}_{_attempt}"

        try:
            _dual = get_dual_broker()
            _result = _dual.submit_order(_order, target_env=target_env)
        except Exception as _e:
            logger.error(f"[playbook] {_mode_label} 下单异常且受理结果不确定 {symbol}: {_e}")
            self.db.mark_execution_unknown(
                instruction_id, _attempt, broker_env=target_env, broker_order_status="SUBMIT_EXCEPTION")
            return {"success": False, "message": f"{_mode_label} 下单异常(状态不确定，已锁定): {_e}"}

        _submit_state = str(_result.get("submit_state") or
                            ("ACCEPTED" if _result.get("success") else "UNKNOWN"))
        _futu_id = str(_result.get("futu_order_id") or "")
        if _submit_state == "DEFINITIVE_REJECT":
            self.db.unlock_execution_if_zero_fill_terminal(
                instruction_id, _attempt, "DEFINITIVE_REJECT", dealt_qty=0)
            logger.error(f"[playbook] {_mode_label} 下单明确拒绝 {symbol}: {_result.get('error')}")
            return {"success": False, "message": f"{_mode_label} 下单失败: {_result.get('error')}"}
        if _submit_state != "ACCEPTED" or not _futu_id:
            self.db.mark_execution_unknown(
                instruction_id, _attempt, broker_order_id=_futu_id, broker_env=target_env,
                broker_order_status=_submit_state or "UNKNOWN")
            logger.error(f"[playbook] {_mode_label} 下单结果不确定 {symbol}: {_result.get('error')}")
            return {"success": False, "message": f"{_mode_label} 下单结果不确定，指令保持锁定"}

        # 一拿到 order id 就先持久化，成交查询/进程崩溃均不能丢失 broker 身份。
        _broker_status = str(_result.get("broker_order_status") or "ACCEPTED")
        if not self.db.mark_execution_submitted(
                instruction_id, _attempt, _futu_id, target_env, _broker_status):
            logger.critical(f"[playbook] {symbol} order id 持久化 CAS 失败: {_futu_id} attempt={_attempt}")
            return {"success": False, "message": "订单已受理但状态持久化冲突，指令保持锁定"}
        # [DEB-20260901 选项1② · ADR-035] 路由限价单: TTL 内等待成交, 到期撤单放弃
        #   (绝不市价追)。成交真相源与市价路径确认窗同源 (order_list_query.dealt_*)。
        #   proceed=True → _fill>0, 下方兜底链自然空转; 终态失败 → 记 skip 返回。
        # 2026-06-27 辩论DEB-20260627: _fill=0时不fallback到entry_price
        # ponytail: 1次查询, 失败则warning+拒绝(不sleep阻塞批量路径)
        if _route:
            _rt = self._handle_routed_fill_wait(
                _futu_id, target_env, _route, instruction_id, symbol,
                int(position_size) if position_size else 1, _mode_label, direction,
                execution_attempt=_attempt)
            if not _rt.get("proceed"):
                return _rt["result"]
            _fill = _rt["fill_price"]
            _confirm_dqty = _rt["dealt_qty"]
            _entry_price_source = _rt["price_source"]
        else:
            _fill = _result.get("filled_price", 0) or 0
            _confirm_dqty = 0.0  # 2026-07-27: 确认窗带回的真成交量 (未走确认分支时为 0)
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
                _confirmed, _ostatus, _confirm_davg, _confirm_dqty = self._confirm_order_filled(
                    _futu_id, target_env, max_wait_s=_confirm_wait)
            except Exception as _cf_err:
                logger.warning(f"[playbook] {_mode_label} {symbol} 订单状态确认异常 (保持锁定): {_cf_err}")
                _confirmed, _ostatus, _confirm_davg, _confirm_dqty = False, "QUERY_ERROR", 0.0, 0.0
            if not _confirmed:
                _unlocked = self.db.unlock_execution_if_zero_fill_terminal(
                    instruction_id, _attempt, _ostatus, dealt_qty=_confirm_dqty)
                if not _unlocked:
                    self.db.mark_execution_unknown(
                        instruction_id, _attempt, broker_order_id=_futu_id, broker_env=target_env,
                        broker_order_status=_ostatus or "CONFIRM_TIMEOUT")
                logger.error(
                    f"[playbook] {_mode_label} {symbol} 订单 {_futu_id} 未确认成交 "
                    f"(status={_ostatus}, retryable={_unlocked})"
                )
                return {"success": False, "message":
                        f"{_mode_label} {symbol} 订单未确认成交 (status={_ostatus})"}
            # 2026-07-27 (DEB-entry-fill 复核翻案): 确认循环已带回 dealt_avg_price 真成交均价 —
            #   优先用它记账 (broker_deal_confirm), quote 兜底降为真正的极端后备。
            #   旧链路: 下单瞬间取价必为 0 (sim 异步成交) → 确认后直接跳 quote_fallback 记 ask
            #   假价 (近一周 broker_deal 占比≈0, SOLS +$13 记成 -$0.96 实证)。回滚: 删本块。
            if _confirm_davg and _confirm_davg > 0:
                _fill = _confirm_davg
                _entry_price_source = "broker_deal_confirm"
                logger.info(
                    f"[playbook] {_mode_label} {symbol} 确认窗内取得真成交均价 ${_fill:.4f} "
                    f"(src=broker_deal_confirm, 订单 {_futu_id})"
                )
            # 2026-06-29: 最后兜底 — broker确认FILLED_ALL但avg_price=0时, 用实时报价记账
            # 根因: sim账户偶发 avg_price=0 但订单已成交 → 拒绝记账 → 孤儿仓 → position_sync用8%覆盖SL
            # 2026-07-21 更正: "avg_price=0"真根因是 dual_broker fallback 字段名笔误 (当日已修复为
            #   dealt_avg_price, sim 真实成交均价可正常获取); 本 bid/ask 兜底保留作极端后备, 触发应罕见.
            # 2026-07-16 P2 修复: 用 bid/ask 替代 last price (BUY→ask, SHORT→bid),
            #   更接近实际成交价. last price 可能是上一笔成交, 偏离当前 bid-ask.
            # 2026-07-27: 加 _fill<=0 守卫 — 旧版无条件执行会把上面刚取得的真价覆盖回 ask 假价。
            if not _fill or _fill <= 0:
                try:
                    # D3(b): 同 arrival price 走私有单例, 不再每单新建 router
                    _ctx = _private_router_quote(symbol)
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
            logger.error(f"[playbook] {_mode_label} {symbol} 订单 {_futu_id} 已可能成交但缺成交价, 保持锁定待补账")
            self.db.mark_execution_filled_unbooked(
                instruction_id, _attempt, broker_order_status="FILLED_NO_PRICE")
            return {"success": False, "message": f"{_mode_label} {symbol} 成交价缺失，已锁定待对账"}

        _observed_qty = float(_result.get("filled_qty", 0) or 0) or float(_confirm_dqty or 0)
        _requested_qty = float(int(position_size) if position_size else 1)
        _fill_status = "FILLED_PART" if 0 < _observed_qty < _requested_qty else "FILLED"
        if not self.db.mark_execution_filled_unbooked(instruction_id, _attempt, _fill_status):
            logger.critical(f"[playbook] {symbol} 成交状态 CAS 失败 attempt={_attempt}, 严禁继续簿记/重下")
            return {"success": False, "message": "成交状态持久化冲突，已停止本次簿记"}
        # 部分成交处理 (P0 2026-07-19): 用真实成交量记账, 消除幽灵仓.
        #   broker 返回 filled_qty=0 时 (SIM avg_price=0 / 查询未命中) 回退下单量.
        # 2026-07-27 (同款时序修复): _result.filled_qty 也是下单瞬间取的 (sim 异步必 0) —
        #   优先用确认窗带回的 dealt_qty 真值, 部分成交不再被回退成全量记账。
        _req_qty = int(position_size) if position_size else 1
        _filled_qty = float(_result.get("filled_qty", 0) or 0)
        if _filled_qty <= 0 and _confirm_dqty and _confirm_dqty > 0:
            _filled_qty = float(_confirm_dqty)
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
        _trade_db_path = None
        try:
            _trade_db_path = str(core_paths.get_trade_records_db_for_arm(target_env).resolve())
            from app.storage.trade_store import TradeStore
            _ts = TradeStore(_trade_db_path)
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

            # F2 (DEB-20260804, 2026-08-04 用户裁"先实施执行方案"): 触发时真数值因子快照落库
            #   此前 Router 每周期算 RSI/MACD/ATR/VWAP偏离等数值但只进 DEBUG 日志, 从不落库 →
            #   真数值 IC 无从计算。只加字段不改行为; fail-open; 回滚=删除本块
            _ts_ctx_snap = {}
            try:
                from app.data.market_data_router import get_market_data_router as _get_mdr_f2
                _ctx = _get_mdr_f2().get_full_market_context(symbol) or {}
                for _k in ("rsi", "macd", "atr", "atr_percent", "vwap", "vwap_intraday",
                           "volume_ratio", "order_flow_imbalance", "price"):
                    if _ctx.get(_k) is not None:
                        _ts_ctx_snap[_k] = round(float(_ctx[_k]), 6)
            except Exception:
                _ts_ctx_snap = {}

            # 2026-07-03 fix: 从 voting_predictions 表查投票数据, 写入 trade_records
            # 根因: save_trade 未传 voting_result/model_scores/consensus_level/final_strength
            _voting_data = None
            if matched_decision_id:
                _vp_conn = None
                try:
                    import sqlite3 as _sqlite3
                    _vp_db = str(core_paths.get_trade_records_db_for_arm(target_env))
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
                # F2 (DEB-20260804): 结构化因子快照 — conditions + ts_ctx 数值 (无下游消费旧格式)
                "factor_detail": _json.dumps({
                    "conditions": _parse_json_field(inst.get("conditions_json"), []),
                    "ts_ctx": _ts_ctx_snap,
                }, ensure_ascii=False),
                "metadata_json": _json.dumps({
                    "playbook_instruction_id": instruction_id,
                    "stop_loss": float(stop_loss) if stop_loss else 0,
                    "take_profit": float(take_profit) if take_profit else 0,
                    "atr": (_atr_pair := _extract_atr_with_basis(inst))[0],
                    "atr_basis": _atr_pair[1],  # real|proxy|missing — KR2 切片口径 (DEB-20260831 §8.7)
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
                    # 2026-07-28 (首夜复盘 #2): 归因块 — metadata 内自证信号源/分桶,
                    #   此前只能跨表 join 指令表才能切分 UE/DA/topup (复盘时连自己都归错过)。
                    #   decision_id 列已有真值, 这里冗余一份使单表切片完备。
                    "attribution": {
                        "signal_source": _parse_json_field(inst.get("raw_instruction_json"), {}).get("source") or "",
                        "timing": _parse_json_field(inst.get("raw_instruction_json"), {}).get("timing") or "",
                        "vol_bucket": inst.get("vol_bucket") or "",
                        "atr_pct": inst.get("atr_pct"),
                        "decision_id": matched_decision_id,
                    },
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
                # R-12b (2026-09-06 裁决): entry_time 与上方 trade_records 同源 (_now_iso,
                # 成交后富化前取值) — 此前本调用自取 now_et(), 富化 I/O 慢路径实测漂 2.94s.
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
                    entry_time=_now_iso,
                )
                logger.info(f"[playbook] {_mode_label} open_position 已写入: {symbol}")
            except Exception as _ope:
                logger.warning(f"[playbook] open_position 写入失败 (不影响主流程, position_sync会补): {_ope}")
        except Exception as _tre:
            logger.error(f"[playbook] {_mode_label} trade_record 写入失败, 补偿退出 {symbol}: {_tre}")
            try:
                if not _trade_db_path:
                    raise ValueError("compensation source ledger identity unavailable")
                _entry_identity = _futu_id
                _rb_owner = {
                    "kind": "compensation", "db_path": _trade_db_path,
                    "close_key": f"entry:{_entry_identity}",
                    "instruction_id": instruction_id, "execution_attempt": _attempt,
                    "entry_order_id": _entry_identity, "entry_time": _now_iso,
                    "entry_price": float(_fill), "quantity": _effective_qty,
                    "symbol": symbol, "direction": mos_direction, "source_type": _source_type,
                }
                _rb_decision = {
                    "reason": "ENTRY_BOOKKEEPING_COMPENSATION", "reference_price": float(_fill),
                    "metadata": {"playbook_instruction_id": instruction_id}, "holding_hours": 0.0,
                }
                _dual_rb = get_dual_broker()
                _rb = _dual_rb.close_position(
                    symbol, _effective_qty, mos_direction, target_env=target_env,
                    exit_owner=_rb_owner, exit_decision=_rb_decision)
                _rb_filled = bool(_rb.get("order_filled"))
                _rb_dealt = float(_rb.get("dealt_qty") or 0.0)
                if _rb.get("success") and _rb_filled and _rb_dealt == float(_effective_qty):
                    from app.playbook.pipeline import _book_playbook_exit_result
                    _book_playbook_exit_result(_dual_rb, _rb)
                    logger.warning(
                        f"[playbook] {_mode_label} 补偿退出已全量成交 {symbol} @ {_rb.get('filled_price')} "
                        f"(dealt={_rb_dealt}/{_effective_qty}, intent={_rb.get('exit_intent_id')})"
                    )
                else:
                    logger.critical(
                        f"[playbook] {_mode_label} 补偿退出待确认 {symbol}: "
                        f"success={_rb.get('success')} order_filled={_rb_filled} "
                        f"dealt={_rb_dealt}/{_effective_qty} src={_rb.get('price_source')} "
                        f"err={_rb.get('error')} intent={_rb.get('exit_intent_id')} "
                        f"— 未决意图由退出监控接续；无意图或证据冲突须人工对账"
                    )
            except Exception as _rbe:
                logger.exception(
                    f"[playbook] {_mode_label} 补偿退出或落账异常 {symbol}: {_rbe} "
                    f"— 保留入场锁及已有退出意图，需对账"
                )
            # B1-lite: broker 曾有成交且本地簿记失败，无论补偿平仓结果如何均不可重下。
            self.db.mark_execution_filled_unbooked(
                instruction_id, _attempt, broker_order_status="BOOKKEEPING_FAILED")
            return {"success": False, "message": f"{_mode_label} trade_record 写入失败，已锁定待对账: {_tre}"}

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
        # G-05 (2026-09-05 审计): 成交后簿记必须包 try — 此前两行裸奔, SQLite 锁忙
        #   (database is locked) 时异常冒出 execute_instruction, 被调用方 (pipeline 自动执行
        #   循环) 的 except 记为 _audit_action="error" + skipped_count+=1 → broker 侧仓位已
        #   真实成交而漏斗遥测报"跳过", 且 executed_count/_max_deploy 均不累加 → 同轮后续
        #   指令的购买力/日名义额度被高估 (已部署敞口口径失真)。
        #   处置原则: 成交既已发生 → 一律按 success 返回 (返回失败会诱发重下=双重持仓);
        #   幂等锁**不得**释放 (释放=指令可被再次捞起→重复下单), 缺口以 critical 告警 +
        #   bookkeeping_failed 标记上交, 由 position_sync 补 open_positions。
        #   回滚: 去掉 try 包裹即恢复旧行为 (不建议, 旧行为会冒泡)。
        _bk_failed = None
        try:
            self.db.update_instruction_tracking(instruction_id, {"decision_id": matched_decision_id})
            self.db.save_user_feedback({"instruction_id": instruction_id, "action": "executed"})
            if not self.db.mark_execution_booked(
                    instruction_id, _attempt, execution_price=float(_fill),
                    execution_time=_now_iso, outcome="holding"):
                raise RuntimeError(f"BOOKED CAS rejected for attempt={_attempt}")
        except Exception as _bk_err:
            _bk_failed = str(_bk_err)
            self.db.mark_execution_filled_unbooked(
                instruction_id, _attempt, broker_order_status="BOOKKEEPING_FAILED")
            logger.critical(
                f"🚨 [playbook] {_mode_label} 成交簿记失败 {symbol} (仓位已成交, 严禁重下): "
                f"instruction={instruction_id} fill={_fill} qty={_effective_qty} "
                f"futu_id={_futu_id} err={_bk_err} — 需 position_sync/人工补账"
            )

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
            # 2026-08-14 止损可见性修复: 展示 β收紧后的有效止损 (与退出逻辑同源公式,
            #   config.tighten_sl_by_beta SSOT)。UCTT 案: 推送-4.83% 实际生效-2.42%。
            #   基于真实成交价 _fill 计算 (退出链同锚点); 失败保持 None 不影响原推送。
            _pb_eff_sl = None
            if _pb_sl is not None:
                try:
                    from app.playbook.config import tighten_sl_by_beta as _tsbb
                    _eff = _tsbb(mos_direction, float(_fill), _pb_sl)
                    if _eff and abs(_eff - _pb_sl) > 0.004:  # 有收紧才展示 (β<1且命中)
                        _pb_eff_sl = round(_eff, 2)
                except Exception:
                    _pb_eff_sl = None
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
                # D5 (2026-07-24 夜, DEB §十-b): topup 来源徽标含盘前/盘中窗口, 防与 DA/UE 人工混淆
                if _src_raw == "topup_pool":
                    _src_detail = "补量·盘中" if (_raw.get("timing") == "intraday") else "补量·盘前"
                else:
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
                # 2026-08-14 止损可见性: β收紧后有效止损注入 extra (渲染端展示)
                if _pb_eff_sl is not None:
                    _pb_extra["eff_sl"] = _pb_eff_sl
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
            # G-05: 簿记失败但成交为真 — 调用方须按"已部署敞口"计入, 不可当 skip 处理
            "bookkeeping_failed": _bk_failed,
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

    def _append_routed_shadow(self, row: Dict[str, Any]) -> None:
        """[DEB-20260901 选项1②] 路由结果遥测落 pullback_limit_shadow.jsonl。

        与 Phase B 影子同文件同 schema 版本 (type 区分), 次日重放脚本按 type 取行,
        新增 type 不影响 trigger 行消费。写失败仅 warning (遥测不阻塞执行)。
        """
        try:
            if "TEST" in str(row.get("symbol", "")).upper():
                return  # 测试标的守卫 (遥测污染案军规)
            from pathlib import Path as _Prt
            import json as _json_rt
            from datetime import datetime as _dt_rt
            from app.utils.timezone import now_et as _now_et_rt
            _prt_path = _Prt(__file__).resolve().parents[2] / "data" / "shadow" / "pullback_limit_shadow.jsonl"
            _prt_path.parent.mkdir(parents=True, exist_ok=True)
            row = {"ts": _dt_rt.now(_now_et_rt()).isoformat(timespec="seconds"),
                   "schema": "v1", **row}
            with open(_prt_path, "a", encoding="utf-8") as _f_rt:
                _f_rt.write(_json_rt.dumps(row, ensure_ascii=False) + "\n")
        except Exception as _rt_err:
            logger.warning(f"[archetype路由] 影子遥测落盘失败: {_rt_err}")

    def _handle_routed_fill_wait(self, futu_order_id: str, target_env: str,
                                  route: Dict, instruction_id: int, symbol: str,
                                  req_qty: int, mode_label: str,
                                  direction: str = "BUY",
                                  execution_attempt: int = None) -> Dict[str, Any]:
        """[DEB-20260901 选项1② · ADR-035] 路由限价单 TTL 等待 → 成交或撤单放弃。

        语义 (绝不降级市价, 消除"低吸变追高"的机制性根因):
          - TTL 内 FILLED_ALL      → proceed, 按 dealt_avg_price 真价记账。
          - TTL 到期部分成交       → 撤余单, 按已成交量记账 (与市价路径部分成交同口径)。
          - TTL 到期零成交         → 撤单 + 竞态复查 → skip_reason=archetype_ttl_expired 终态。
          - 等待中 FAILED/CANCELLED/REJECTED → skip_reason=archetype_order_dead 终态。
        真相源 = order_list_query.order_status/dealt_qty (与 _confirm_order_filled、
        close_position 平仓侧同源)。阻塞上限 = route.ttl_seconds (默认 300s,
        与 auto_execute 5 分钟节拍同量级; 退出监控有独立快循环不受牵连)。
        """
        from futu import RET_OK
        from app.execution.dual_broker import get_dual_broker
        _dual = get_dual_broker()
        ctx, acc_id, trd_env, lock = _dual._ctx_and_acc(target_env)
        _ttl = float(route.get("ttl_seconds", 300.0))
        _limit_px = float(route.get("limit_price", 0))
        _arch = route.get("archetype", "")
        _deadline = time.time() + _ttl
        _last_status = "UNKNOWN"
        if execution_attempt is None:
            try:
                execution_attempt = int(
                    (self._find_instruction_by_id(instruction_id) or {}).get("execution_attempt") or 0)
            except Exception:
                execution_attempt = 0

        def _skip_terminal(reason: str, message: str, extra: Dict = None,
                           dealt_qty: float = 0.0) -> Dict[str, Any]:
            """仅明确零成交终态可 CAS 解锁并落终态 skip。"""
            _canonical = next((s for s in (
                "CANCELLED_PART", "CANCELLED_ALL", "CANCELLED", "REJECTED", "FAILED",
                "DELETED", "DISABLED", "TIMEOUT") if s in str(_last_status).upper()), "")
            try:
                _unlocked = self.db.unlock_execution_if_zero_fill_terminal(
                    instruction_id, execution_attempt, _canonical, dealt_qty=dealt_qty)
            except Exception as _rel_err:
                _unlocked = False
                logger.warning(f"[archetype路由] {symbol} 安全解锁失败: {_rel_err}")
            if _unlocked:
                try:
                    self.db.update_instruction_tracking(instruction_id, {"skip_reason": reason})
                except Exception as _sk_err:
                    logger.warning(f"[archetype路由] {symbol} skip_reason 落库失败: {_sk_err}")
            else:
                self.db.mark_execution_unknown(
                    instruction_id, execution_attempt, broker_order_id=str(futu_order_id),
                    broker_env=target_env, broker_order_status=_last_status or "UNKNOWN")
                reason = "archetype_order_unconfirmed"
                message = f"{mode_label} {symbol} 订单终态/零成交未确认，保持锁定"
            _row = {"date": (inst_date or ""), "type": reason, "inst_id": instruction_id,
                    "symbol": symbol, "direction": ("SHORT" if str(direction).upper() in ("SHORT", "SELL_SHORT")
                                                     else "LONG"),
                    "entry_archetype": _arch, "limit_price": _limit_px,
                    "ttl_seconds": _ttl, "futu_order_id": str(futu_order_id),
                    "order_status": _last_status}
            if extra:
                _row.update(extra)
            self._append_routed_shadow(_row)
            logger.info(f"[archetype路由] {symbol} {_arch} 限价@{_limit_px} 终态={reason} "
                        f"(status={_last_status}, ttl={_ttl:.0f}s, futu={futu_order_id})")
            _res = {"success": False, "message": message}
            if reason == "archetype_ttl_expired":
                _res["ttl_expired"] = True
            return {"proceed": False, "result": _res}

        # 指令日期 (遥测用; 取不到留空)
        inst_date = ""
        try:
            _inst_row = self._find_instruction_by_id(instruction_id)
            inst_date = str((_inst_row or {}).get("date", "") or "")
        except Exception:
            pass

        def _query_once():
            """→ (status, dealt_qty, dealt_avg_price); 查询异常返回 (None, 0, 0)。"""
            try:
                with lock:
                    ret_od, od_data = ctx.order_list_query(
                        order_id=str(futu_order_id), trd_env=trd_env)
                if ret_od == RET_OK and od_data is not None and not od_data.empty:
                    _r = od_data.iloc[0]
                    _dq = float(_r.get("dealt_qty", 0) or 0)
                    try:
                        _da = float(_r.get("dealt_avg_price", 0) or 0)
                    except Exception:
                        _da = 0.0
                    return str(_r.get("order_status", "")), _dq, _da
            except Exception as _q_err:
                logger.debug(f"[archetype路由] {symbol} 订单查询异常: {_q_err}")
            return None, 0.0, 0.0

        # ── TTL 窗口轮询 (2s 间隔, 对 broker API 温和) ──
        while time.time() < _deadline:
            _status, _dealt, _davg = _query_once()
            if _status is not None:
                _last_status = _status
                if "FILLED_ALL" in _status:
                    _fill_px = _davg if _davg > 0 else _limit_px
                    logger.info(f"[archetype路由] {symbol} {_arch} 限价@{_limit_px} 全部成交 "
                                f"@{_fill_px} qty={_dealt} (futu={futu_order_id})")
                    self._append_routed_shadow(
                        {"date": inst_date, "type": "routed_fill", "inst_id": instruction_id,
                         "symbol": symbol, "entry_archetype": _arch, "limit_price": _limit_px,
                         "fill_price": _fill_px, "filled_qty": _dealt, "ttl_seconds": _ttl,
                         "wait_seconds": round(_ttl - max(0.0, _deadline - time.time()), 1),
                         "futu_order_id": str(futu_order_id)})
                    return {"proceed": True, "fill_price": _fill_px,
                            "dealt_qty": _dealt, "price_source": "routed_limit_deal"}
                if any(_k in _status for _k in
                       ("FAILED", "CANCELLED", "DELETED", "REJECTED", "TIMEOUT")):
                    # DEB-20260903 #91: 终态前检查部分成交 — CANCELLED_PART 等终态可能
                    # 已有 dealt_qty>0, 直接 skip 会丢弃已成交部分 → broker 有真实持仓
                    # 但无 trade_records 行 (孤儿仓, 无 SL 守护)
                    if _dealt > 0:
                        _fill_px = _davg if _davg > 0 else _limit_px
                        logger.warning(
                            f"[archetype路由] {symbol} 终态 {_status} 但已部分成交 "
                            f"{_dealt}/{req_qty} @{_fill_px} — 按已成交量记账")
                        self._append_routed_shadow(
                            {"date": inst_date, "type": "routed_fill", "inst_id": instruction_id,
                             "symbol": symbol, "entry_archetype": _arch, "limit_price": _limit_px,
                             "fill_price": _fill_px, "filled_qty": _dealt, "ttl_seconds": _ttl,
                             "wait_seconds": round(_ttl - max(0.0, _deadline - time.time()), 1),
                             "partial": True, "terminal_status": _status,
                             "futu_order_id": str(futu_order_id)})
                        return {"proceed": True, "fill_price": _fill_px,
                                "dealt_qty": _dealt, "price_source": "routed_limit_deal"}
                    return _skip_terminal(
                        "archetype_order_dead",
                        f"{mode_label} {symbol} 路由限价单被拒/已撤 (status={_status}), 放弃不追单")
            time.sleep(2.0)

        # ── TTL 到期: 撤单 + 竞态复查 ──
        _status, _dealt, _davg = _query_once()
        if _status is not None:
            _last_status = _status
        if _dealt >= req_qty or "FILLED_ALL" in (_last_status or ""):
            # 到期瞬间恰好全成 — 无需撤单
            _fill_px = _davg if _davg > 0 else _limit_px
            self._append_routed_shadow(
                {"date": inst_date, "type": "routed_fill", "inst_id": instruction_id,
                 "symbol": symbol, "entry_archetype": _arch, "limit_price": _limit_px,
                 "fill_price": _fill_px, "filled_qty": _dealt, "ttl_seconds": _ttl,
                 "wait_seconds": _ttl, "futu_order_id": str(futu_order_id)})
            return {"proceed": True, "fill_price": _fill_px,
                    "dealt_qty": _dealt, "price_source": "routed_limit_deal"}

        _cancel_res = _dual.cancel_order(str(futu_order_id), target_env)
        _cancel_ok = bool(_cancel_res.get("success"))
        if not _cancel_ok:
            logger.warning(f"[archetype路由] {symbol} 撤单失败: {_cancel_res.get('error')}")
        time.sleep(2.0)  # 等撤单传播, 随后竞态复查

        # 竞态复查: 撤单请求与成交之间可能交叉 — 复查到成交则按成交记账
        _status2, _dealt2, _davg2 = _query_once()
        if _status2 is not None:
            _last_status = _status2

        # DEB-20260904 #181a: 撤单未确认时若订单仍在途 (非终态), 不得 skip_terminal —
        # 订单可能随后成交形成孤儿仓. 终止集合与 #184 同口径: 只认终态.
        _terminal_frag = ("FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DELETED", "DISABLED", "REJECTED")
        _still_live = (not _cancel_ok) and _status2 and not any(_f in _status2 for _f in _terminal_frag)
        if _still_live:
            logger.error(
                f"[archetype路由] {symbol} 撤单失败且订单仍在途 (status={_status2}), "
                f"不置 terminal-skip — 保留待下轮复查, 防孤儿仓"
            )
            return {"proceed": False, "result": {
                "success": False, "cancel_unconfirmed": True,
                "message": f"撤单未确认 status={_status2} (订单仍在途, 未置 terminal-skip)"}}

        _fin_dealt = max(_dealt, _dealt2)
        if _fin_dealt > 0:
            # DEB-20260904 #181b: 双均价均为 0 时用 _limit_px 兜底入账 —
            # 旧逻辑 (要求 _davg2>0 or _davg>0) 会把真实成交掉进 skip_terminal 成孤儿仓
            _fill_px = _davg2 if _davg2 > 0 else (_davg if _davg > 0 else _limit_px)
            logger.info(f"[archetype路由] {symbol} TTL 到期部分成交 {_fin_dealt}/{req_qty}, "
                        f"撤余单后按已成交量记账 @{_fill_px}")
            self._append_routed_shadow(
                {"date": inst_date, "type": "routed_fill", "inst_id": instruction_id,
                 "symbol": symbol, "entry_archetype": _arch, "limit_price": _limit_px,
                 "fill_price": _fill_px, "filled_qty": _fin_dealt, "ttl_seconds": _ttl,
                 "wait_seconds": _ttl, "partial": True,
                 "futu_order_id": str(futu_order_id)})
            return {"proceed": True, "fill_price": _fill_px,
                    "dealt_qty": _fin_dealt, "price_source": "routed_limit_deal"}

        return _skip_terminal(
            "archetype_ttl_expired",
            f"{mode_label} {symbol} 路由限价@{_limit_px} {_ttl:.0f}s 未成交, 已撤单放弃 (绝不市价追)")

    def _confirm_order_filled(self, futu_order_id: str, target_env: str,
                              max_wait_s: float = 5.0) -> tuple:
        """确认订单是否真实成交 → (confirmed, status, dealt_avg_price, dealt_qty).
    
        2026-07-19 (DEB-round3 GHOST-RC1): 入场侧成交确认, 与 dual_broker.close_position
        的平仓侧验证共用同一真相源 — order_list_query.order_status (FILLED_ALL)。
        2026-07-27 (DEB-entry-fill 复核翻案): 确认循环拿到的行里本就有 dealt_avg_price
        真成交均价, 旧版只返 bool 把价丢了 → 调用方被迫走 quote_fallback 记 ask 假价
        (近一周 broker_deal 占比≈ 0, 多头入场价系统性记高+保本锚错, SOLS 实证杀单)。
        本修复就地带回真价: 零新挂点/零额外 API。
    
        判定:
          FILLED_ALL / FILLED_PART / dealt_qty>0 → (True, status, dealt_avg_price, dealt_qty)
          FAILED / CANCELLED / DELETED / REJECTED / TIMEOUT → (False, status, 0.0, 0.0) 不可恢复
          超时仍非 FILLED → (False, status, 0.0, 0.0) (宁可拒记不记假仓)
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
                        try:
                            _davg = float(_row.get("dealt_avg_price", 0) or 0)
                        except Exception:
                            _davg = 0.0
                        return True, _status, _davg, _dealt
                    if any(_k in _status for _k in
                           ("FAILED", "CANCELLED", "DELETED", "REJECTED", "TIMEOUT")):
                        return False, _status, 0.0, 0.0
            except Exception as _od_err:
                logger.debug(f"[playbook] order_list_query 确认异常 {futu_order_id}: {_od_err}")
            time.sleep(0.5)
        return False, _last_status, 0.0, 0.0

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

        # DEB-20260903 #92: 已执行指令不可被改写为 skipped —
        # 对齐 execute_instruction (:747) 守卫, 防止 executed=1 ∧ outcome=skipped 矛盾态
        if inst.get("executed"):
            return {"success": False, "message": "指令已执行, 不可跳过"}

        skip_reason = custom_reason or reason or "用户主动跳过"

        # 更新 DB
        # DEB-20260903 #92: skip_reason 同步写回指令跟踪表
        # (原仅写 feedback 表, Dashboard/执行路径读 skip_reason 时口径不一致)
        self.db.update_instruction_tracking(instruction_id, {
            "outcome": "skipped",
            "skip_reason": skip_reason,
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
        except Exception:
            # DEB-20260903 #93: 兜底同样按 ET 取日 (原 date.today() 用服务器本地时区,
            #   跨时区部署与上游 et_today() 口径不一致); except 由 ImportError 扩到
            #   Exception (et_today 运行期异常原会逃逸). now_et 模块级已导入.
            try:
                return now_et().date().isoformat()
            except Exception:
                return date.today().isoformat()
