"""
RecommendationSynthesizer — 盘前推荐合成器

输入:
  - battle_plan        : premarket_planner 产出的作战计划
  - voting_results     : voting_coordinator 产出的多模型投票结果
  - debate_summary     : debate_engine 产出的辩论结论
  - market_data        : (可选) 实时行情数据

输出:
  - 标准化推荐 JSON (推荐列表 + 行业配置 + 高层摘要 + 操作清单)

设计原则:
  1. 纯计算逻辑 - 不调用任何外部 API
  2. 容错优先  - 任何输入字段缺失时使用合理默认值
  3. SSOT      - 行业 (sector) 信息从 unified_config.yaml watchlist 读取
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    # 项目内统一配置访问 (SSOT)
    from app.core.config_container import get_config  # type: ignore
except Exception:  # pragma: no cover - 配置容器缺失时的降级
    get_config = None  # type: ignore

logger = logging.getLogger(__name__)


# ── 分级阈值 ────────────────────────────────────────────────
HIGH_CONFIDENCE = 0.70
MEDIUM_CONFIDENCE = 0.50
# Task #46 (2026-05-28) 配置 SSOT: 上面保留为 get_config() 未就绪时的 fallback,
# 实际置信度阈值请调用 _resolve_confidence_thresholds() 从 YAML 读取
_DEFAULT_HIGH_CONFIDENCE = HIGH_CONFIDENCE
_DEFAULT_MEDIUM_CONFIDENCE = MEDIUM_CONFIDENCE


def _resolve_confidence_thresholds() -> Tuple[float, float]:
    """lazy 从 SSOT (recommendations.confidence_thresholds) 读取 high/medium 阈值"""
    if get_config is None:
        return _DEFAULT_HIGH_CONFIDENCE, _DEFAULT_MEDIUM_CONFIDENCE
    try:
        from app.core.config_keys import ConfigKeys
        cfg = get_config()
        if cfg is None:
            return _DEFAULT_HIGH_CONFIDENCE, _DEFAULT_MEDIUM_CONFIDENCE
        high = float(cfg.get_dotted(
            ConfigKeys.CONFIDENCE_HIGH.value, _DEFAULT_HIGH_CONFIDENCE
        ))
        medium = float(cfg.get_dotted(
            ConfigKeys.CONFIDENCE_MEDIUM.value, _DEFAULT_MEDIUM_CONFIDENCE
        ))
        return high, medium
    except Exception as e:  # pragma: no cover
        logger.debug(f"[Synthesizer] 置信度阈值读取失败, 使用默认值: {e}")
        return _DEFAULT_HIGH_CONFIDENCE, _DEFAULT_MEDIUM_CONFIDENCE

# ── 中文翻译词典 (英文关键词 → 中文) ────────────────────────
EN_TO_ZH_KEYWORDS: Dict[str, str] = {
    "earnings beat": "财报超预期",
    "earnings miss": "财报不及预期",
    "ai demand": "AI 需求强劲",
    "ai chip": "AI 芯片",
    "sector rotation": "板块轮动",
    "gap fill": "缺口回补",
    "breakout": "突破形态",
    "breakdown": "跌破支撑",
    "trend": "趋势延续",
    "momentum": "动量加速",
    "oversold": "超卖反弹",
    "overbought": "超买回落",
    "support": "关键支撑",
    "resistance": "关键阻力",
    "institutional buying": "机构买入",
    "institutional selling": "机构卖出",
    "fed pause": "美联储暂停加息",
    "rate cut": "降息预期",
    "rate hike": "加息预期",
    "vix spike": "VIX 飙升",
    "risk on": "风险偏好上升",
    "risk off": "避险情绪升温",
    "news positive": "利好消息",
    "news negative": "利空消息",
    "guidance raise": "上调指引",
    "guidance cut": "下调指引",
    "regime fit": "市况契合",
    "news sentiment": "新闻情绪",
}


class RecommendationSynthesizer:
    """盘前推荐合成器 - 融合多源信号生成用户友好的股票推荐"""

    def __init__(self, output_dir: Optional[str] = None):
        """
        Args:
            output_dir: 推荐 JSON 输出目录 (默认 data/recommendations/)
        """
        self._sector_map: Optional[Dict[str, str]] = None  # 懒加载
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            # 项目根: app/recommendations/synthesizer.py → 上溯 3 层
            project_root = Path(__file__).resolve().parent.parent.parent
            self.output_dir = project_root / "data" / "recommendations"
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # pragma: no cover
            logger.warning(f"[Synthesizer] 创建输出目录失败: {e}")

    # ─────────────────────────────────────────────────────
    # 主入口
    # ─────────────────────────────────────────────────────
    def synthesize(
        self,
        battle_plan: Optional[Dict[str, Any]] = None,
        voting_results: Optional[Dict[str, Any]] = None,
        debate_summary: Optional[Dict[str, Any]] = None,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        合成推荐 JSON

        Returns:
            标准化推荐字典 (含 date / market_regime / summary / recommendations
            / sector_allocation / action_items / generated_at)
        """
        battle_plan = battle_plan or {}
        voting_results = voting_results or {}
        debate_summary = debate_summary or {}
        market_data = market_data or {}

        targets: List[Dict[str, Any]] = battle_plan.get("targets", []) or []
        restricted: List[str] = battle_plan.get("restricted", []) or []
        market_regime = self._normalize_regime(battle_plan.get("market_regime"))

        # 1. 逐 symbol 合成单条推荐
        recommendations: List[Dict[str, Any]] = []
        for tgt in targets:
            try:
                rec = self._synthesize_single(
                    target=tgt,
                    voting_results=voting_results,
                    debate_summary=debate_summary,
                    market_data=market_data,
                )
                if rec:
                    recommendations.append(rec)
            except Exception as e:
                sym = tgt.get("symbol", "?")
                logger.warning(f"[Synthesizer] 合成 {sym} 失败: {e}")

        # 2. 排序: confidence ↓, risk_reward ↓
        recommendations.sort(
            key=lambda r: (r.get("confidence_score", 0.0), r.get("risk_reward", 0.0)),
            reverse=True,
        )
        for idx, rec in enumerate(recommendations, start=1):
            rec["rank"] = idx

        # 3. 行业聚合 + 高层摘要 + 操作清单
        sector_alloc = self._aggregate_sectors(recommendations)
        summary = self._build_summary(recommendations, sector_alloc)
        action_items = self._build_action_items(restricted, battle_plan)

        # 4. 顶层结构
        now = datetime.now().astimezone()
        result: Dict[str, Any] = {
            "date": now.strftime("%Y-%m-%d"),
            "market_regime": market_regime,
            "summary": summary,
            "recommendations": recommendations,
            "sector_allocation": sector_alloc,
            "action_items": action_items,
            "generated_at": now.isoformat(timespec="seconds"),
        }

        # 5. 持久化
        self._persist(result)
        return result

    # ─────────────────────────────────────────────────────
    # 单条推荐合成
    # ─────────────────────────────────────────────────────
    def _synthesize_single(
        self,
        target: Dict[str, Any],
        voting_results: Dict[str, Any],
        debate_summary: Dict[str, Any],
        market_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        symbol_raw = target.get("symbol") or target.get("code") or ""
        symbol = self._strip_market_prefix(symbol_raw)
        if not symbol:
            return None

        direction = (target.get("direction") or "LONG").upper()
        action = "BUY" if direction == "LONG" else "SELL"

        # ── 投票 / 辩论分项数据 ──
        vote = self._lookup(voting_results, symbol, symbol_raw) or {}
        debate = self._lookup(debate_summary, symbol, symbol_raw) or {}

        # ── 置信度融合 ──
        conf_vote = self._safe_float(vote.get("confidence"))
        conf_debate = self._safe_float(
            debate.get("confidence", debate.get("consensus"))
        )
        confidence = self._blend_confidence(conf_vote, conf_debate)
        confidence_level = self._grade(confidence)

        # ── 入场区 / 止盈止损 / Risk-Reward ──
        entry_low, entry_high = self._parse_entry_zone(target.get("entry_zone"))
        take_profit = self._safe_float(target.get("take_profit"))
        stop_loss = self._safe_float(target.get("stop_loss"))
        rr = self._compute_risk_reward(entry_low, entry_high, take_profit, stop_loss, direction)

        # ── 中文解读 ──
        zh_thesis = self._build_zh_thesis(target, vote, debate)

        # ── 风险因素 ──
        risk_factors = self._collect_risk_factors(vote, debate, target)

        # ── 行业 ──
        sector = self._resolve_sector(symbol, target)

        rec: Dict[str, Any] = {
            "rank": 0,  # 后续排序时回填
            "symbol": symbol,
            "action": action,
            "confidence_level": confidence_level,
            "confidence_score": round(confidence, 3),
            "zh_thesis": zh_thesis,
            "entry_zone": self._format_entry_zone(entry_low, entry_high),
            "target": self._format_price(take_profit),
            "stop_loss": self._format_price(stop_loss),
            "risk_reward": round(rr, 2) if rr is not None else None,
            "catalyst": target.get("catalyst") or "",
            "risk_factors": risk_factors,
            "sector": sector,
        }
        # 附加行情快照 (若有)
        md = self._lookup(market_data, symbol, symbol_raw) or {}
        if md:
            price = self._safe_float(md.get("price") or md.get("current_price"))
            change_pct = self._safe_float(md.get("change_pct"))
            if price is not None:
                rec["current_price"] = round(price, 2)
            if change_pct is not None:
                rec["change_pct"] = round(change_pct, 4)
        return rec

    # ─────────────────────────────────────────────────────
    # 置信度 / 分级
    # ─────────────────────────────────────────────────────
    @staticmethod
    def _blend_confidence(vote_conf: Optional[float], debate_conf: Optional[float]) -> float:
        """置信度融合 (任一缺失则用另一方 100% 权重，全部缺失返回 0.5)"""
        if vote_conf is not None and debate_conf is not None:
            return float(vote_conf) * 0.5 + float(debate_conf) * 0.5
        if vote_conf is not None:
            return float(vote_conf)
        if debate_conf is not None:
            return float(debate_conf)
        return 0.5

    @staticmethod
    def _grade(confidence: float) -> str:
        high, medium = _resolve_confidence_thresholds()
        if confidence >= high:
            return "HIGH"
        if confidence >= medium:
            return "MEDIUM"
        return "LOW"

    # ─────────────────────────────────────────────────────
    # 入场区 / RR
    # ─────────────────────────────────────────────────────
    def _parse_entry_zone(
        self, raw: Any
    ) -> Tuple[Optional[float], Optional[float]]:
        if raw is None:
            return None, None
        # list / tuple
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            lo = self._safe_float(raw[0])
            hi = self._safe_float(raw[1])
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            return lo, hi
        # dict
        if isinstance(raw, dict):
            lo = self._safe_float(raw.get("low") or raw.get("min"))
            hi = self._safe_float(raw.get("high") or raw.get("max"))
            return lo, hi
        # 字符串 "145.5-147.0"
        if isinstance(raw, str) and "-" in raw:
            parts = [p.strip() for p in raw.split("-", 1)]
            lo = self._safe_float(parts[0])
            hi = self._safe_float(parts[1])
            return lo, hi
        return None, None

    @staticmethod
    def _format_entry_zone(lo: Optional[float], hi: Optional[float]) -> str:
        if lo is None and hi is None:
            return ""
        if lo is None:
            return f"{hi:.2f}"
        if hi is None:
            return f"{lo:.2f}"
        return f"{lo:.2f}-{hi:.2f}"

    @staticmethod
    def _format_price(p: Optional[float]) -> str:
        if p is None:
            return ""
        return f"{p:.2f}"

    @staticmethod
    def _compute_risk_reward(
        entry_low: Optional[float],
        entry_high: Optional[float],
        take_profit: Optional[float],
        stop_loss: Optional[float],
        direction: str,
    ) -> Optional[float]:
        if take_profit is None or stop_loss is None:
            return None
        # 入场中位
        if entry_low is not None and entry_high is not None:
            entry_mid = (entry_low + entry_high) / 2.0
        elif entry_low is not None:
            entry_mid = entry_low
        elif entry_high is not None:
            entry_mid = entry_high
        else:
            return None
        try:
            if direction == "SHORT":
                reward = entry_mid - take_profit
                risk = stop_loss - entry_mid
            else:
                reward = take_profit - entry_mid
                risk = entry_mid - stop_loss
            if risk <= 0:
                return None
            return reward / risk
        except Exception:
            return None

    # ─────────────────────────────────────────────────────
    # 中文解读
    # ─────────────────────────────────────────────────────
    def _build_zh_thesis(
        self,
        target: Dict[str, Any],
        vote: Dict[str, Any],
        debate: Dict[str, Any],
    ) -> str:
        parts: List[str] = []

        # 1. 优先 voting.key_reasons (中文最先)
        for r in (vote.get("key_reasons") or [])[:2]:
            if isinstance(r, str) and r.strip():
                parts.append(self._maybe_translate(r.strip()))

        # 2. battle_plan target.entry_logic / logic / rationale
        plan_logic = (
            target.get("rationale")
            or target.get("entry_logic")
            or target.get("logic")
            or target.get("thesis")
            or ""
        )
        if plan_logic:
            parts.append(self._maybe_translate(str(plan_logic).strip()))

        # 3. debate.core_thesis
        core = debate.get("core_thesis") or debate.get("thesis") or ""
        if core:
            parts.append(self._maybe_translate(str(core).strip()))

        # 去重 & 拼接 & 截断
        seen, dedup = set(), []
        for p in parts:
            p = p.strip("。.;； ")
            if not p or p in seen:
                continue
            seen.add(p)
            dedup.append(p)
        if not dedup:
            dedup.append("综合多源信号给出建议")
        text = "+".join(dedup)
        if len(text) > 100:
            text = text[:97] + "..."
        if not text.endswith("。"):
            text += "。"
        return text

    @staticmethod
    def _maybe_translate(s: str) -> str:
        """若包含明显中文则保留，否则把英文关键词词典翻译合并"""
        if any("\u4e00" <= ch <= "\u9fff" for ch in s):
            return s
        lower = s.lower()
        hits: List[str] = []
        for en, zh in EN_TO_ZH_KEYWORDS.items():
            if en in lower and zh not in hits:
                hits.append(zh)
        if hits:
            return "、".join(hits[:3])
        # 兜底: 取前 24 字符英文原文
        return s if len(s) <= 24 else s[:24] + "..."

    # ─────────────────────────────────────────────────────
    # 风险因素
    # ─────────────────────────────────────────────────────
    def _collect_risk_factors(
        self,
        vote: Dict[str, Any],
        debate: Dict[str, Any],
        target: Dict[str, Any],
    ) -> List[str]:
        out: List[str] = []
        for src in (
            vote.get("risk_factors"),
            debate.get("risk_factors"),
            target.get("risk_warnings"),
        ):
            if not src:
                continue
            if isinstance(src, str):
                out.append(self._maybe_translate(src))
            elif isinstance(src, (list, tuple)):
                for item in src:
                    if isinstance(item, str) and item.strip():
                        out.append(self._maybe_translate(item.strip()))
        # 去重保序，截断 5 条
        seen, dedup = set(), []
        for r in out:
            if r in seen:
                continue
            seen.add(r)
            dedup.append(r)
            if len(dedup) >= 5:
                break
        return dedup

    # ─────────────────────────────────────────────────────
    # 行业聚合 / 摘要 / 操作清单
    # ─────────────────────────────────────────────────────
    def _resolve_sector(self, symbol: str, target: Dict[str, Any]) -> str:
        # 1. target 上自带
        sec = target.get("sector")
        if sec:
            return str(sec)
        # 2. unified_config.yaml watchlist
        sm = self._load_sector_map()
        return sm.get(symbol.upper(), "Unknown")

    def _load_sector_map(self) -> Dict[str, str]:
        if self._sector_map is not None:
            return self._sector_map
        sm: Dict[str, str] = {}
        if get_config is not None:
            try:
                cfg = get_config()
                wl = cfg.get_dotted("watchlist.us_stocks", []) or []
                for item in wl:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("code") or item.get("symbol") or "")
                    sym = self._strip_market_prefix(code)
                    sec = item.get("sector")
                    if sym and sec:
                        sm[sym.upper()] = str(sec)
            except Exception as e:
                logger.debug(f"[Synthesizer] sector map 加载失败: {e}")
        self._sector_map = sm
        return sm

    @staticmethod
    def _aggregate_sectors(recs: List[Dict[str, Any]]) -> Dict[str, float]:
        if not recs:
            return {}
        total = len(recs)
        counter: Dict[str, int] = {}
        for r in recs:
            sec = r.get("sector") or "Unknown"
            counter[sec] = counter.get(sec, 0) + 1
        # 占比, 保留 2 位
        result = {sec: round(cnt / total, 4) for sec, cnt in counter.items()}
        # 按占比降序
        return dict(sorted(result.items(), key=lambda kv: kv[1], reverse=True))

    @staticmethod
    def _build_summary(
        recs: List[Dict[str, Any]],
        sector_alloc: Dict[str, float],
    ) -> str:
        if not recs:
            return "今日无推荐标的，建议观望。"
        buy_count = sum(1 for r in recs if r.get("action") == "BUY")
        sell_count = len(recs) - buy_count
        msg = f"今日推荐 {buy_count} 只看涨、{sell_count} 只看跌。"
        if sector_alloc:
            top_sec, top_pct = next(iter(sector_alloc.items()))
            msg += f"{top_sec} 占比 {int(round(top_pct * 100))}%。"
        return msg

    def _build_action_items(
        self,
        restricted: List[str],
        battle_plan: Dict[str, Any],
    ) -> List[str]:
        items: List[str] = []
        for sym in restricted or []:
            clean = self._strip_market_prefix(str(sym))
            reason = self._lookup_restrict_reason(battle_plan, sym, clean)
            if reason:
                items.append(f"{clean}: {reason}")
            else:
                items.append(f"{clean}: 今日回避")
        # position_actions 中的 EXIT/REDUCE 同步进操作清单
        for pa in battle_plan.get("position_actions", []) or []:
            if not isinstance(pa, dict):
                continue
            sym = self._strip_market_prefix(str(pa.get("symbol", "")))
            act = (pa.get("action") or "").upper()
            reason = pa.get("reason") or ""
            if sym and act in {"EXIT", "REDUCE", "TRIM"}:
                items.append(f"{sym}: {act} - {reason}".rstrip(" -"))
        return items

    @staticmethod
    def _lookup_restrict_reason(
        battle_plan: Dict[str, Any],
        sym_raw: str,
        sym_clean: str,
    ) -> Optional[str]:
        # 部分 battle_plan 形态: restricted 是 list; 也可能 restricted_with_reason 是 dict
        rwr = battle_plan.get("restricted_reasons") or battle_plan.get(
            "restricted_with_reason"
        )
        if isinstance(rwr, dict):
            return rwr.get(sym_raw) or rwr.get(sym_clean) or rwr.get(sym_clean.upper())
        return None

    # ─────────────────────────────────────────────────────
    # 持久化
    # ─────────────────────────────────────────────────────
    def _persist(self, payload: Dict[str, Any]) -> None:
        try:
            date_str = payload.get("date") or datetime.now().strftime("%Y-%m-%d")
            file_path = self.output_dir / f"daily_{date_str}.json"
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(
                f"[Synthesizer] 推荐已持久化: {file_path} "
                f"(共 {len(payload.get('recommendations', []))} 条)"
            )
        except Exception as e:
            logger.warning(f"[Synthesizer] 持久化失败: {e}")

    # ─────────────────────────────────────────────────────
    # 工具方法
    # ─────────────────────────────────────────────────────
    @staticmethod
    def _safe_float(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            f = float(v)
            if f != f:  # NaN
                return None
            return f
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _strip_market_prefix(code: str) -> str:
        """剥离 US./HK. 等市场前缀，统一返回纯 symbol (大写)"""
        if not code:
            return ""
        c = str(code).strip()
        for prefix in ("US.", "HK.", "us.", "hk."):
            if c.startswith(prefix):
                c = c[len(prefix):]
                break
        return c.upper()

    def _lookup(
        self,
        bag: Dict[str, Any],
        symbol_clean: str,
        symbol_raw: str,
    ) -> Optional[Dict[str, Any]]:
        """以多种 key 形态从 bag 中查找单 symbol 数据"""
        if not isinstance(bag, dict) or not bag:
            return None
        # 直接命中
        for key in (symbol_clean, symbol_raw, symbol_clean.upper(), symbol_clean.lower()):
            if key and key in bag:
                v = bag.get(key)
                if isinstance(v, dict):
                    return v
        # 嵌套 per_symbol / results / by_symbol
        for nest_key in ("per_symbol", "results", "by_symbol", "symbols"):
            nest = bag.get(nest_key)
            if isinstance(nest, dict):
                for key in (symbol_clean, symbol_raw, symbol_clean.upper()):
                    if key and key in nest and isinstance(nest[key], dict):
                        return nest[key]
        return None

    @staticmethod
    def _normalize_regime(raw: Any) -> str:
        if not raw:
            return "NEUTRAL"
        s = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
        if s in {"RISK_ON", "BULLISH", "BULL"}:
            return "BULLISH"
        if s in {"RISK_OFF", "BEARISH", "BEAR"}:
            return "BEARISH"
        if s in {"NEUTRAL", "RANGE", "SIDEWAYS"}:
            return "NEUTRAL"
        return s
