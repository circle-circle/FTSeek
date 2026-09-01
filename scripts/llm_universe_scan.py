#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 事件推理驱动的选股宇宙扩张 (LLM Universe Scan)
==================================================
把 LLM 用在"发现哪些标的"这一步: 读今日新闻事件 + 市场佐证 → LLM 关联因果推理 →
浮现受影响标的(含宇宙外, 受益+受损方) → 代码出数值(entry/SL/TP) → 写 playbook_instructions →
与 DA 共用同一自动执行/触发/退出轨道 在模拟盘执行 (2026-07: 从 fast_lane 迁移, 统一执行层)。

设计 (见 plan: LLM 事件推理驱动的选股宇宙扩张):
  - 信息源 = 新闻事件为主 + 市场佐证(板块轮动/异动)
  - LLM 出判断(方向/因果链/thesis/置信度), 代码出数值(Track A)
  - 独立 strategy_id=universe_expansion, 与 playbook_sim 物理隔离
  - feature flag universe_expansion.entry_enabled 默认关 → 只 scan/dry-run 不下单

子命令:
  scan    组装 clue brief → LLM 推理 → 落影子日志 (含 causal_chain/thesis); 不写作战计划
  inject  读最近 scan → 代码算 entry/SL/TP → 写 playbook_instructions (走 DA 统一执行轨道, 仅 entry_enabled=true)
  score   对已 scan 未评分标的取现价 → 前瞻收益 + 命中率
  report  查看近 N 天 scan/score 效果 (归因源=LLM_EVENT_SCAN)

运行:
  PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py scan [--clue "手动线索文本"]
  PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py inject [--dry-run]
  PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py score
  PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py report --days 30
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SCAN_PATH = _ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"
# DEB-20260814 §十三 (2026-08-14, 用户裁): 午间影子扫描独立落盘 —
#   主链路 (inject/push/score/DA供料 _load_ue_short_candidates) 全部只读 _SCAN_PATH,
#   午间记录对它们不可见 = 零污染。判据: scripts/ue_midday_shadow_eval.py。
_MIDDAY_PATH = _ROOT / "data" / "shadow" / "llm_universe_scan_midday.jsonl"
_MECH_CAPTURE_PATH = _ROOT / "data" / "shadow" / "universe_expansion_shadow.jsonl"

# 归因分类 (与 config/signal_stage 对齐)
DISCOVERY_SOURCE = "LLM_EVENT_SCAN"       # → battle_plan target.discovery_source
SOURCE_TYPE = "UNIVERSE_EXPANSION"        # → trade_records.source_type (语义标签)
STRATEGY_ID = "universe_expansion"        # → trade_records.strategy_id (物理隔离)
INFO_SOURCE = "LLM_EVENT_REASONING"

HIT_PCT = 1.0  # 评分: 前瞻收益(按方向) ≥ 此值 → 命中

# [U8 2026-08-25 evidence grounding] 当日输入材料缓存 — _build_clue_brief 每次构建重置,
#   供 _ground_evidence_refs 核验模型引证真实性。模块级避免跨函数传参。
_LAST_BRIEF_MATERIALS: List[str] = []
PROMPT_VERSION = "v2.8"  # scan 落库归因戳; v2.8=E3-fix 输出模板 {字段...} 占位展开为显式字段列表(含 invalidation_triggers) — E3 8臂实弹: 只改措辞强度 0%→0%, 字段写进 JSON 模板即 83-100% 填充且全过白名单 (2026-08-31, DEB-20260830第5轮, 用户裁"按建议先执行"); v2.7=invalidation_triggers 强制≥1条→可空数组降级 (2026-08-30, DEB-20260829第7轮, 0/208填充实测); v2.6=SKIP skip_reason 字段 (2026-08-25, DEB-ue-audit U4, 拒绝可归因); v2.5=删死止损规则 (2026-08-16, DEB-fullchain #23) + 版本戳/归因修复 (2026-08-25, DEB-ue-audit U1); v2.4=结构约束前置+回调优先 (2026-08-09, DEB-20260808 §17.1); v2.3=板块级联 (07-28); v2.2=可证伪字段+证据清单 (07-25)

SYSTEM_PROMPT = (
    "你是资深美股宏观-产业链关联分析师。基于给定的今日新闻事件、板块轮动与异动线索, "
    "做关联因果推理, 找出直接与间接受影响的美股标的 —— 既要受益方(做多), 也要受损方(做空), "
    "尤其挖掘不在常规观察池里的标的(泛化)。\n"
    "对每个标的给出字段: symbol(美股代码, 不带交易所前缀), direction(LONG/SHORT/SKIP), "
    "causal_chain(事件→传导→该标的的因果链, 一句话), thesis(交易论点, ≤60字), "
    "confidence(0~1 小数), is_universe_external(bool, 是否常规池外), "
    "winner_loser(WINNER 或 LOSER), corroborated_by_market(bool, 市场异动是否已印证)。\n"
    "只输出可在美股实际交易的真实 symbol; 不确定的标的 direction 填 SKIP。"
    "不要输出任何价格数字(entry/止损/止盈由下游代码计算)。\n"
    "严格只输出 JSON, 格式: {\"surfaced\": [ {字段...}, ... ]}。"
)

# P0 (意图对齐 + 强制引证自评): 与 V1 同源, 仅用于 scan-eval A/B; 不覆盖 V1。
# v2.1 (2026-07-23 用户批准, DEB-20260723-da-ue-prompt-optimization): 开放 THESIS_ONLY 通道 —
#   V2 "绝不为凑数做板块联想"致 THESIS_ONLY n=0 (不可证伪) + 模拟期样本不足 (日仅 2-3 浮现).
#   改双通道: 主通道纪律不变, 板块联想/逻辑推演可经 THESIS_ONLY 通道浮现 (标签+夹逼照常),
#   质量分层交给 score tier 切片用数据判定, 不在浮现端压制. 加法式变更, 回滚=git revert.
SYSTEM_PROMPT_V2 = (
    "你是资深事件驱动型美股分析师。职责: 浮现受事件/市场/逻辑驱动的美股标的, 双通道运行: "
    "主通道=存在【明确、可核验的因果传导路径】的标的(HARD_EVENT/MARKET_CORROBORATED); "
    "THESIS_ONLY 通道=有交易价值的板块联想/逻辑推演类标的(模拟验证期开放, 供下游分层判分)。\n"
    "要求:\n"
    "1. evidence_ref(主通道强制): 主通道标的必须引用给定材料中的【具体证据原文】—— 某条新闻标题 / "
    "某个市场异动数据 / 某条人工线索。若无法从给定材料引出具体证据(仅凭行业常识或板块联想), evidence_ref 留空。\n"
    "2. self_tier(强制自评): HARD_EVENT=因果链第一环锚定 evidence_ref 里的具体事件(封锁/战争/财报/监管/事故); "
    "MARKET_CORROBORATED=无具体事件但 evidence_ref 是真实市场异动/板块轮动数据; "
    "THESIS_ONLY=无 evidence_ref, 仅逻辑或板块联想。\n"
    "3. 自律约束: evidence_ref 为空者, self_tier 必须=THESIS_ONLY 且 confidence≤0.60; "
    "THESIS_ONLY 标的的 thesis 必须以'板块联想:'或'逻辑推演:'开头, 明示推演性质。\n"
    "4. 充分浮现: 凡具备【可核验因果路径 + 真实可执行机会】的标的都要浮现(多空机会对等浮现, 质量门槛各自对称), 不设人为数量上限, "
    "也不为控量而漏报——质量由 self_tier/evidence_ref 标签与模拟交易结果在下游度量, 而非在浮现端压制。"
    "证据强弱如实反映在 confidence 与 self_tier(HARD_EVENT>MARKET_CORROBORATED>THESIS_ONLY)。\n"
    "5. 不输出任何价格数字(entry/止损/止盈由下游计算); 只输出可真实交易的美股 symbol(不带前缀); 不确定填 direction=SKIP。\n"
    # v2.2 (2026-07-25, P1 可证伪字段 + P2 证据清单): 预测与叙事分离
    "6. 可证伪性(强制): 每个非 SKIP 标的额外输出 — invalidation(一句话失效条件, ≤40字), "
    # [U10 2026-08-25 建议④] 结构化失效触发器 — 照抄 DA r15 schema (tripwire_checker 白名单),
    #   让盘中失效核销器真正可响 (自然语言首期命中率≈0)。与 DA 同契约, 同一消费方。
    # [DEB-20260829 第7轮 2026-08-30] 强制"≥1条"降级为"可空数组": 8/19-8/28 实测 0/208
    #   填充 — flash 档模型对结构化数组零依从, 恒空强制字段=白耗token+假承诺 (模型不交作业)。
    #   降级后模型交不交都合法, 填充率成为真实依从度测量; tripwire 消费端空数组本就不拦。
    "invalidation_triggers(本股、代码可判定的失效条件数组, 能判定则输出, 可为空数组 []; 每条 {metric, operator, value}: "
    "metric 限 收盘价/成交量/5日涨跌/盘前涨跌幅, operator 限 < 或 >, value 纯数字), "
    "horizon_hours(论点预期兑现时效, 整数小时, 日内动量 4-8), expected_move_pct(预期方向幅度%, 小数)。\n"
    "7. 证据清单(强制, 如实勾选不得美化): evidence_checklist 四项布尔 — "
    "catalyst_specific(存在具体催化事件非泛情绪), catalyst_within_24h(催化发生在24h内), "
    "price_volume_confirmed(给定材料中有量价异动印证), crowding_low(非已被充分炒作2日以上的拥挤交易)。\n"
    # v2.2 (2026-07-25, P5 SHORT 高门槛): LLM 多头叙事先验强, 提高 SHORT 输出成本=天然质量门
    #   (实证: 本源 SHORT 命中 25%, DA 分歧SHORT 0%)。不禁 SHORT, 只要求想清三件事。
    "8. SHORT 高门槛(强制): direction=SHORT 的标的必须额外输出 short_check — "
    "borrow_feasible(bool, 大盘股/流动性好可借券), downside_catalyst_expiry_hours(int, 下跌催化剂时效), "
    "squeeze_risk(1-5, 逼空风险自评)。无具体下跌催化剂(仅'估值高'/'涨多了')的 SHORT 不得浮现。\n"
    # v2.3 (2026-07-28, 板块级联): 07-27 芯片/存储大跌日系统满舵做多 (12/12 LONG, TSM BUY -95)
    #   — 轮动数据已标 Technology 最弱但 prompt 未要求用于级联/做空推理。
    "9. 板块级联(强制评估): 若事件涉及某板块龙头/产业链环节(如芯片/存储/能源/金融), "
    "必须评估同板块传导: 负面事件下同板块弱势标的的 SHORT 侧与受益替代品的 LONG 侧均需考虑, "
    "不得只输出事件主体本身; [市场佐证] 中的轮出板块/盘中脆弱板块可作级联 SHORT 的 "
    "evidence_ref (self_tier=MARKET_CORROBORATED); 与轮出板块同向的 LONG 需在 thesis 中自证"
    "为何逆板块风仍成立。级联 SHORT 同受第 8 条高门槛约束。\n"
    # v2.4 (2026-08-09, DEB-20260808-signal-source §17.1): 结构性约束前置 + 回调优先 —
    #   追涨日 LONG 实证净负; 让 LLM 不产低质的单.
    # v2.5 (2026-08-16, DEB-fullchain #23): 删除死止损宽度规则 — 原"止损须2-5%(>6%被准入硬拦)"
    #   中, 硬拦门 max_sl_pct=6.0 已于 8/16 禁用 (max_sl_pct=0), 且 SL 宽度由代码
    #   _compute_numbers 恒定算出 (LONG 5.91% / SHORT 6.09%), LLM 不控制止损价, 该规则
    #   LLM 永远无法满足 → 持续扭曲生成端 (E1 空头衰减诊断的混淆变量, DEB-short-funnel §8.2 N1).
    "10. 结构约束: 不接当日跌>5% 且无企稳证据的接飞刀 LONG; 入场区贴近支撑/VWAP 的回踩企稳优先于新高追涨延伸。\n"
    "11. 回调优先: 做多论点优先选'回调日企稳'标的; "
    "强势延伸日的 LONG 需在 thesis 自证为何不追高。\n"
    "每个标的字段: symbol, direction(LONG/SHORT/SKIP), causal_chain(事件→传导→标的, 一句话), "
    "evidence_ref(引用的具体证据原文, 无则空), self_tier(HARD_EVENT/MARKET_CORROBORATED/THESIS_ONLY), "
    "thesis(≤60字), confidence(0~1 小数), is_universe_external(bool), winner_loser(WINNER/LOSER), "
    "corroborated_by_market(bool), invalidation(str), horizon_hours(int), expected_move_pct(float), "
    "evidence_checklist({catalyst_specific,catalyst_within_24h,price_volume_confirmed,crowding_low} 均bool), "
    "short_check(仅SHORT: {borrow_feasible,downside_catalyst_expiry_hours,squeeze_risk}), "
    # [U4 2026-08-25 DEB-ue-audit] SKIP 必须给理由 — 可归因"为什么不浮现" (审计病灶: 拒绝不可归因)。
    "skip_reason(仅SKIP: 一句话, 如'已充分反映/无具体催化/流动性不足')。\n"
    # [E3-fix 2026-08-31, DEB-20260830第5轮·用户裁"按建议先执行"] 模板占位 {字段...} →
    #   显式字段列表。E3 8臂实弹 (data/shadow/e3_contract_bakeoff.jsonl): 措辞强度
    #   0%→0%, 字段写进 JSON 模板即 83-100% 填充 — 模型照 JSON 模板示例填空, 示例里
    #   没有的字段≈不存在 (DA 侧 45/45=100% 的同构真因)。字段集与 _normalize_surfaced
    #   解析白名单逐项对齐; V1 SYSTEM_PROMPT 不动 (scan-eval A/B 基线仪器)。
    "严格只输出 JSON, 格式: {\"surfaced\": [ {\"symbol\": \"AAPL\", \"direction\": \"LONG\", "
    "\"causal_chain\": \"事件→传导→标的(一句话)\", \"evidence_ref\": \"引用的具体证据原文, 无则留空字符串\", "
    "\"self_tier\": \"HARD_EVENT或MARKET_CORROBORATED或THESIS_ONLY\", \"thesis\": \"交易论点≤60字\", "
    "\"confidence\": 0.72, \"is_universe_external\": false, \"winner_loser\": \"WINNER\", "
    "\"corroborated_by_market\": true, \"invalidation\": \"一句话失效条件≤40字\", "
    "\"invalidation_triggers\": [{\"metric\": \"收盘价\", \"operator\": \"<\", \"value\": 185.5}], "
    "\"horizon_hours\": 6, \"expected_move_pct\": 2.5, "
    "\"evidence_checklist\": {\"catalyst_specific\": true, \"catalyst_within_24h\": true, "
    "\"price_volume_confirmed\": false, \"crowding_low\": true}, "
    "\"short_check\": {\"borrow_feasible\": true, \"downside_catalyst_expiry_hours\": 24, \"squeeze_risk\": 2}, "
    "\"skip_reason\": \"仅SKIP时填, 一句话\"}, ... ]}。"
    "每个标的必须列全上述全部字段: invalidation_triggers 每条限白名单 (metric∈收盘价/成交量/5日涨跌/盘前涨跌幅, "
    "operator∈<或>, value 纯数字), 能判定则填, 确实无可判定条件才可为空数组 []; "
    "short_check 仅 SHORT 标的填对象, 非 SHORT 填 null; skip_reason 仅 SKIP 标的填字符串, 非 SKIP 填 null。"
)

# [DEB-20260825-ue-audit U1] SYSTEM_PROMPT_V2 指纹 — 落库归因戳 (与 DA r13
#   _prompt_fingerprint 同模式)。病灶1版本戳失真: PROMPT_VERSION 常量可在 prompt
#   已改时忘记升 (8/16 v2.5 改动落库仍标 v2.4), hash 随内容自动变 → 新旧基线可切分。
PROMPT_HASH_V2 = hashlib.sha1(SYSTEM_PROMPT_V2.encode("utf-8")).hexdigest()[:12]


# ── 通用工具 ──────────────────────────────────────────────────
def _now_et() -> datetime:
    # [2026-08-26 UE审计P0·用户批准] 原硬编码 `utc - 4h` = 永久EDT假钟: cmd_confirm
    #   窗口守卫(:2098)在EST季节把 22:40CST 正确发车(真09:40 EST)读成10:40→拒跑,
    #   把 21:40CST 错误发车(真08:40盘前)读成09:40→放行落污染样本, 幂等守卫再锁死
    #   当日 → 整冬静默错样+缺样。夏令时正确时钟, 与 _is_us_premarket 同款;
    #   附带修正时间戳假标(+00:00实为ET读数) → skill侧 age 计算不再恒虚高4h。
    #   测试经 monkeypatch._now_et 注入, 不受实现切换影响。
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        pass
    try:
        import pytz
        return datetime.now(pytz.timezone("America/New_York"))
    except Exception:
        pass
    return datetime.now(timezone.utc) - timedelta(hours=4)


def _today() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _is_us_trading_day_today() -> bool:
    """美股交易日判断 (复用 holiday_calendar 权威能力: Futu缓存+硬编码降级+周末兑底).

    cron `30 20 * * 1-5` 仅排周末, 不排美股节假日 → 靠本守卫避免假日白跑 LLM/写陈旧指令.
    日历不可用时 fail-open 按交易日 (与 app/playbook _is_trading_day_today 一致, 宁可跑不可漏).
    """
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return get_holiday_calendar().is_trading_day("US", _now_et().date())
    except Exception:
        return True


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _cfg(dotted: str, default=None):
    try:
        from app.core.config_container import get_config
        v = get_config().get_dotted(dotted, default)
        return v if v is not None else default
    except Exception:
        return default


# 2026-08-22 (P2): _build_router 模块级缓存 — scan→inject→push 链 2-3 次重复构造
_ROUTER_CACHE = None

def _build_router():
    global _ROUTER_CACHE
    if _ROUTER_CACHE is not None:
        return _ROUTER_CACHE
    from app.data.market_data_router import MarketDataRouter
    _ROUTER_CACHE = MarketDataRouter(
        twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
        finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
        alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
    )
    return _ROUTER_CACHE


def _batch_quotes(router, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    full = [f"US.{s}" if not str(s).startswith("US.") else s for s in symbols]
    try:
        raw = router.get_batch_quotes(full) or {}
    except Exception as e:
        print(f"  [warn] get_batch_quotes 异常: {e}")
        return {}
    return {str(k).replace("US.", "").upper(): v for k, v in raw.items()}


def _quote_price(q: Dict[str, Any]) -> Optional[float]:
    if not q:
        return None
    return q.get("price") or q.get("last_price")


def _is_da_priority_skip(existing_decision_id: Any) -> bool:
    """P1-14 DA 优先判定: 已有行 decision_id 非空且非本源前缀 → UE 不得覆盖."""
    did = str(existing_decision_id or "").strip()
    return bool(did) and not did.startswith(f"{STRATEGY_ID}:")


def _streak_passes(s: Dict[str, Any], max_streak: int) -> bool:
    """[U7 2026-08-25 REVIEW-20260824 裁决GO] 续期硬闸谓词 (可单测)。

    streak>max_streak 拦; None/0→视作首日=1 (埋点失败保守按新浮现处理, 不误杀)。
    max_streak=99 默认=全放行 (缺键零行为, 防配置未读到静默启闸)。
    """
    _stk = s.get("emergence_streak_days") or 1
    return _stk <= max_streak


# ── [UE-B2 2026-08-29, DEB-20260829 第3轮批准] inject 拒绝落盘 ──
# 根因 (D3 病灶台账 U3): inject 各拒绝分支只留 stdout — "SHORT 27→9 衰减黑箱"
#   无法归因。每个被拒候选落一行结构化记录, 连续10交易日后首次成表分解衰减漏斗。
#   纯观测, 零行为影响。回滚: 配置设 universe_expansion.inject_reject_log: false。
_INJECT_REJECT_PATH = _ROOT / "data" / "shadow" / "llm_universe_inject.jsonl"


def _log_inject_reject(surfaced: List[Dict[str, Any]], min_conf: float,
                       positions: set, existing_pb: set, max_streak: int,
                       max_n: int) -> None:
    """对每个未进入 inject 的浮现候选记录唯一拒绝原因 (优先级序与候选筛选一致)。"""
    if not bool(_cfg("universe_expansion.inject_reject_log", True)):
        return
    try:
        # 候选筛选链 (与 cmd_inject 的 cand 列表推导同序): 方向 → 置信 → 持仓 → DA已写 → streak
        kept: List[Dict[str, Any]] = []
        rows: List[Dict[str, Any]] = []
        for s in surfaced:
            sym = s.get("symbol", "")
            conf = float(s.get("confidence", 0) or 0)
            stk = s.get("emergence_streak_days") or 1
            tier = s.get("tier", "")
            if s.get("direction") not in ("LONG", "SHORT"):
                reason = f"direction({s.get('direction','')})"
            elif conf < min_conf:
                reason = f"conf({conf:.2f}<{min_conf:g})"
            elif sym in positions:
                reason = "already_in_position"
            elif sym in existing_pb:
                reason = "da_or_other_already_written"
            elif stk > max_streak:
                reason = f"streak_gate({stk}>{max_streak})"
            else:
                kept.append(s)
                continue
            rows.append({"date": _today(), "symbol": sym, "direction": s.get("direction", ""),
                         "reject_reason": reason, "raw_conf": conf, "tier": tier,
                         "streak": stk, "stage": "filter"})
        # 配额截断: 置信排序后超出 max_n 的候选
        kept.sort(key=lambda s: s.get("confidence", 0), reverse=True)
        for s in kept[max_n:]:
            rows.append({"date": _today(), "symbol": s.get("symbol", ""),
                         "direction": s.get("direction", ""),
                         "reject_reason": "quota_cap(max_targets_per_day)",
                         "raw_conf": float(s.get("confidence", 0) or 0),
                         "tier": s.get("tier", ""),
                         "streak": s.get("emergence_streak_days") or 1, "stage": "filter"})
        if not rows:
            return
        _INJECT_REJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _INJECT_REJECT_PATH.open("a") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  [inject-reject-log] {len(rows)} 条拒绝记录 → {_INJECT_REJECT_PATH.name}")
    except Exception as e:
        print(f"  [warn] inject 拒绝落盘失败 (不阻塞): {e}")


def _log_inject_loop_reject(s: Dict[str, Any], reason: str) -> None:
    """[UE-B2] inject 指令构造循环内的拒绝分支 (无现价/entry_zone 无效) 落盘。"""
    if not bool(_cfg("universe_expansion.inject_reject_log", True)):
        return
    try:
        row = {"date": _today(), "symbol": s.get("symbol", ""),
               "direction": s.get("direction", ""), "reject_reason": reason,
               "raw_conf": float(s.get("confidence", 0) or 0), "tier": s.get("tier", ""),
               "streak": s.get("emergence_streak_days") or 1, "stage": "build"}
        _INJECT_REJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _INJECT_REJECT_PATH.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _log_inject_save_reject(it: Dict[str, Any], reason: str) -> None:
    """[UE-B2 2026-08-30 复核补漏] 入库循环 (stage=save) 的拒绝分支落盘:
    DA优先不覆盖 / 同标的同方向冷却跳过。第4轮首批只接了筛选链+构造循环,
    漏掉入库循环这两支 → "每笔reject可归因"判据不满足, 补上。
    tier/streak/raw_conf 从指令 raw_instruction.origin 回填 (构造期已埋)。"""
    if not bool(_cfg("universe_expansion.inject_reject_log", True)):
        return
    try:
        origin = ((it.get("raw_instruction") or {}).get("origin") or {})
        row = {"date": _today(), "symbol": str(it.get("symbol", "")).replace("US.", ""),
               "direction": it.get("direction", ""), "reject_reason": reason,
               "raw_conf": float(origin.get("raw_confidence") or it.get("confidence") or 0),
               "tier": origin.get("self_tier") or "",
               "streak": origin.get("emergence_streak_days") or 1, "stage": "save"}
        _INJECT_REJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _INJECT_REJECT_PATH.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── [DA-C1 2026-08-30, DEB-20260829 第4轮用户批准] 方向结构测量 ──
# 病灶 (D3): 单周 DA 输出 BUY:SELL_SHORT = 67:6, 账本方向倒挂。归因二分未决:
#   供给偏斜 (scanner 候选池本来就偏多) vs 判断偏斜 (供给对称但 DA 只挑多)。
# 预注册判据 (DEB §执行项): 攒 ≥10 交易日; 若供给对称而输出 BUY 占比仍 >75%
#   → 立案 prompt 级诊断; 否则归档为 regime 产物 (8月反弹期混杂)。
# 实现: 每日 inject 时落一行 — 候选池 (surfaced, 含双桶 tier 结构) 方向分布
#   + 当日 DA 输出 (decision_id 前缀 DA-*) 方向分布。纯观测零行为。
# 回滚: 配置 universe_expansion.direction_structure_log: false。
_DIRECTION_STRUCTURE_PATH = _ROOT / "data" / "shadow" / "ue_direction_structure.jsonl"


def _log_direction_structure(surfaced: List[Dict[str, Any]],
                             today_instructions: List[Dict[str, Any]],
                             min_conf: float, max_streak: int) -> None:
    """每日两行合一: 候选池方向结构 + DA 输出方向结构 (一行两节)。"""
    if not bool(_cfg("universe_expansion.direction_structure_log", True)):
        return
    try:
        pool_dist = {"LONG": 0, "SHORT": 0, "OTHER": 0}
        tier_dist = {"HARD_EVENT": 0, "MARKET_CORROBORATED": 0, "THESIS_ONLY": 0}
        pool_conf = {"LONG": [], "SHORT": []}
        for s in surfaced:
            d = s.get("direction", "")
            pool_dist[d if d in pool_dist else "OTHER"] += 1
            tier_dist[str(s.get("self_tier") or s.get("tier") or "UNKNOWN")] = \
                tier_dist.get(str(s.get("self_tier") or s.get("tier") or "UNKNOWN"), 0) + 1
            if d in pool_conf:
                pool_conf[d].append(float(s.get("confidence", 0) or 0))
        da_dist = {"BUY": 0, "SELL_SHORT": 0, "SKIP": 0, "OTHER": 0}
        for inst in today_instructions:
            did = str(inst.get("decision_id") or "")
            if not did.startswith("DA-"):
                continue  # 只统计 DA 输出; UE 自身写入同批落盘, 不互相污染
            d = str(inst.get("direction", "")).upper()
            da_dist[d if d in da_dist else "OTHER"] += 1
        _avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
        row = {
            "date": _today(), "stage": "direction_structure",
            "pool": {"n": len(surfaced), "dist": pool_dist, "tier_dist": tier_dist,
                     "avg_conf_long": _avg(pool_conf["LONG"]),
                     "avg_conf_short": _avg(pool_conf["SHORT"]),
                     "min_conf_gate": min_conf, "max_streak_gate": max_streak},
            "da_output": {"n": sum(da_dist.values()), "dist": da_dist},
        }
        _append_jsonl(_DIRECTION_STRUCTURE_PATH, row)
        print(f"  [direction-structure] 池 LONG/SHORT/OTHER="
              f"{pool_dist['LONG']}/{pool_dist['SHORT']}/{pool_dist['OTHER']} | "
              f"DA BUY/SHORT/SKIP={da_dist['BUY']}/{da_dist['SELL_SHORT']}/{da_dist['SKIP']}")
    except Exception as e:
        print(f"  [warn] 方向结构测量落盘失败 (不阻塞): {e}")


# ── [UE-C2 2026-08-30, DEB-20260829 第4轮用户批准] streak 闸反事实跟踪 ──
# 目的: 评估 streak 硬闸的机会成本 (8/28 BABA conf0.85 被拦但次日 +1.93% 命中案例)。
# 预注册判据 (DEB §执行项): 攒 n≥20 被拦信号; 反事实均值显著>0 → 提案放宽
#   max_emergence_streak 对 HARD_EVENT 的豁免。
# 收益口径: 不另拉行情 — cmd_score 的评分链不受 streak 闸影响 (tradeable 只滤
#   方向+现价), 被拦标的的 fwd_pct 照常产出; 裁决时按 (scan_date, symbol) join
#   score results 即得反事实收益, 与 D 系列判分完全同口径。
# 回滚: 配置 universe_expansion.streak_counterfactual_log: false。
_STREAK_CF_PATH = _ROOT / "data" / "shadow" / "ue_streak_counterfactual.jsonl"


def _blocked_by_streak(surfaced: List[Dict[str, Any]], min_conf: float,
                       positions: set, existing_pb: set,
                       max_streak: int) -> List[Dict[str, Any]]:
    """除 streak 外全部门都通过的候选 (与 _log_inject_reject 的 streak_gate 分支等价)。"""
    return [s for s in surfaced
            if s.get("direction") in ("LONG", "SHORT")
            and float(s.get("confidence", 0) or 0) >= min_conf
            and s.get("symbol") not in positions
            and s.get("symbol") not in existing_pb
            and not _streak_passes(s, max_streak)]


def _log_streak_counterfactual(surfaced: List[Dict[str, Any]], min_conf: float,
                               positions: set, existing_pb: set,
                               max_streak: int) -> None:
    if not bool(_cfg("universe_expansion.streak_counterfactual_log", True)):
        return
    try:
        blocked = _blocked_by_streak(surfaced, min_conf, positions, existing_pb, max_streak)
        if not blocked:
            return
        rows = [{"date": _today(), "symbol": s.get("symbol", ""),
                 "direction": s.get("direction", ""), "streak": s.get("emergence_streak_days") or 1,
                 "raw_conf": float(s.get("confidence", 0) or 0),
                 "tier": str(s.get("self_tier") or s.get("tier") or ""),
                 "scan_price": s.get("scan_price"),
                 "note": "join score results by (date,symbol) for counterfactual fwd_pct"}
                for s in blocked]
        _append_jsonl(_STREAK_CF_PATH, {"date": _today(), "n_blocked": len(rows), "rows": rows})
        print(f"  [streak-counterfactual] {len(rows)} 个被 streak 闸拦截信号已登记反事实跟踪")
    except Exception as e:
        print(f"  [warn] streak 反事实落盘失败 (不阻塞): {e}")


# ── [UE-A3 2026-08-29, DEB-20260829 第3轮批准] 8-K/13DG clue 噪声过滤 ──
# 病灶 (U1): 8/28 实测 15 条 8-K 中 7 条为商品 ETF 每日例行 7.01 Reg FD
#   (CPER/BNO/UNL/UGA/USL/UNG/USO — 无事件性) + 1 条权证 (OSRHW) → 真股权事件被稀释。
#   对照: RFC-061 已给盘前榜做了同等过滤, 8-K/13DG 无。
# 规则: ①商品信托名单 (每日例行文件) ②权证 (名称含 WARRANT/WTS — 名称权威,
#   不用裸 W 后缀猜测: 8/29 实弹发现 FWFW=Flywheel 真实公司被后缀规则误杀)
#   ③连字符权证 (IONQ-WT 式, 无歧义) ④ETF 例行 7.01 (items 仅 7.01 + 名称含 ETF 关键词)。
# 13DG 侧: 权证/优先股举牌无普通股事件意义, 同样剔除。
# 回滚: 配置 universe_expansion.clue_sec_noise_filter: false。
_COMMODITY_TRUST_TICKERS = frozenset(
    {"CPER", "BNO", "UNL", "UGA", "USL", "UNG", "USO"})  # United States Commodity Funds 族
_8K_ETF_NAME_KEYWORDS = ("ETF", "SPDR", "ISHARES", "ULTRA", "PROSHARES", "DIREXION",
                         "INVESCO", "VANGUARD", "SELECT SECTOR", "EXCHANGE TRADED")


def _sec_clue_noise_filter_enabled() -> bool:
    return bool(_cfg("universe_expansion.clue_sec_noise_filter", True))


def _is_warrant_ticker(ticker: str, company: str) -> bool:
    """权证判定 (保守: 宁可漏过权证, 不可误伤普通股 — 预注册判据"真事件不减少")。"""
    t = (ticker or "").upper().replace("US.", "")
    name = (company or "").upper()
    if "WARRANT" in name or "WTS" in name:
        return True
    if "-WT" in t or "-W" in t:  # 连字符权证 (IONQ-WT 式, 无歧义)
        return True
    return False


def _filter_8k_noise(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[UE-A3] 8-K 噪声过滤主入口: 静态规则 + 同批交叉判据。

    同批交叉判据 (8/29 实弹): 裸 W 后缀不可靠 (FWFW=Flywheel 真实公司被误杀),
    改判"同公司名在本批有无 W 后缀的基础 ticker" → 该 W 后缀者=权证
    (如 MOBIX LABS 同批既有 MOBX 又有 MOBXW → MOBXW 判定权证)。
    保守方向: 判据不满足则保留 (漏过权证 < 误伤普通股)。
    """
    kept_static = [r for r in rows
                   if not _is_8k_noise(r.get("ticker", ""), r.get("company", ""),
                                       [str(i).split()[0] for i in (r.get("items") or [])])]
    # 同批基础 ticker 公司名集合 (非 W 形态结尾)
    base_names = set()
    for r in kept_static:
        t = str(r.get("ticker", "")).upper().replace("US.", "")
        if not (t.endswith("W") and len(t) >= 4):
            base_names.add(str(r.get("company", "")).upper().strip())
    out = []
    for r in kept_static:
        t = str(r.get("ticker", "")).upper().replace("US.", "")
        cname = str(r.get("company", "")).upper().strip()
        if t.endswith("W") and len(t) >= 4 and cname in base_names:
            continue  # 同批有基础公司 → 权证
        out.append(r)
    return out


def _is_8k_noise(ticker: str, company: str, items: List[str]) -> bool:
    """True = 例行文件/权证噪声, 应剔除。"""
    if not _sec_clue_noise_filter_enabled():
        return False
    t = (ticker or "").upper().replace("US.", "")
    name = (company or "").upper()
    if t in _COMMODITY_TRUST_TICKERS:
        return True
    if _is_warrant_ticker(ticker, company):
        return True
    # ETF 例行 7.01 Reg FD: item 仅 7.01 且名称带 ETF 关键词
    if items == ["7.01"] and any(k in name for k in _8K_ETF_NAME_KEYWORDS):
        return True
    return False


def _is_13dg_noise(ticker: str, company: str) -> bool:
    """True = 权证/优先股举牌 (对普通股发现无事件意义), 应剔除。"""
    if not _sec_clue_noise_filter_enabled():
        return False
    name = (company or "").upper()
    if any(k in name for k in ("PREFERRED", "PFD", "DEPOSITORY")):
        return True
    return _is_warrant_ticker(ticker, company)


# ── [U11 2026-08-26] 开盘确认门 (两段式: 盘前假设→开盘+10min代码裁决) ──
# 设计: 21:10 CST scan 生成假设 (照旧, DA供料不断); 开盘+10min (EDT=21:40 / EST=22:40
#   CST, 双季 cron 双挂) 用真实盘中价做 price-in 裁决。用户 8/25 裁"信息是否已被价格
#   反映"判据第一次有真实盘中数据可执行 (盘前只能用盘前代理)。
#   调度防漂移 (2026-08-26 复查加固): 锚定 ET 的时点无法用单一 CST cron 表达 — 美国 DST
#   切换后 21:40 CST 会落 08:40 ET 窗外 → 静默拒跑断粮数月。故 EDT/EST 各挂一发
#   (恒差 1h > 窗宽 58min), 任一季节恰一发落在窗口内, 另一发被守卫拒绝; 配合
#   cmd_confirm 当日幂等守卫保证一天最多一条裁决样本。watchdog 挂 ue_confirm_gate.jsonl 兜底。
# 时点依据 (回测 17票×20交易日 7/28-8/22, 配对 n=194):
#   +10min vs +30min: 92%同向(等待无新信息, 等待成本+0.50%/笔, t=5.67, p<0.001);
#   翻转笔(8%)等到+30按新方向也是亏(-0.60%); 每候选日EV +10=+0.57% vs +30=+0.16%。
# Phase 1 = 影子: 只裁决留痕 (ue_confirm_gate.jsonl), 不拦既有 21:10 inject 链。
#   预注册判据: ≥10个交易日且被裁决n≥30后, 若 PRICE_IN 桶前瞻均值显著≤0 而
#   CONFIRM 桶>0 → 接线为 inject 前置闸 (inject 挪到确认门后)。
_CONFIRM_PATH = _ROOT / "data" / "shadow" / "ue_confirm_gate.jsonl"


def _price_in_verdict(direction: str, prev_close: Optional[float],
                      price: Optional[float], threshold: float) -> Tuple[str, Optional[float]]:
    """确认门裁决谓词 (纯函数, 可单测)。返回 (verdict, signed_pre_move)。

    signed_pre_move = 开盘后价格相对昨收的方向化变动:
      LONG: (price-prev)/prev ; SHORT: -(price-prev)/prev
    verdict:
      PRICE_IN  : signed ≥ threshold → 信息已同向大幅反映, 无增量 (拦)
      REVERSED  : signed ≤ -threshold → 开盘反向, 市场不认同方向 (留痕, Phase1不拦)
      CONFIRM   : 其余 → 假设未被充分反映, 保留
      NO_QUOTE  : 无价可裁 (缺昨收/现价)
    """
    if not prev_close or prev_close <= 0 or not price or price <= 0:
        return "NO_QUOTE", None
    signed = (price - prev_close) / prev_close
    if direction == "SHORT":
        signed = -signed
    if signed >= threshold:
        return "PRICE_IN", signed
    if signed <= -threshold:
        return "REVERSED", signed
    return "CONFIRM", signed


def _is_us_premarket() -> bool:
    """美股盘前时段判定 (04:00-09:30 ET). pytz 处理夏令时; 失败回落 _now_et 硬编码偏移."""
    try:
        import pytz
        now_et = datetime.now(pytz.timezone("America/New_York"))
        hm = now_et.hour * 100 + now_et.minute
        return 400 <= hm < 930
    except Exception:
        hm = _now_et().hour * 100 + _now_et().minute
        return 400 <= hm < 930


def _effective_quote_price(q: Dict[str, Any]) -> Optional[float]:
    """盘前感知取价 (2026-08-11, LIF/UE 同款盘前盲点修复).

    盘前时段 snapshot 的 last_price/price=昨收 (无当日成交), 真实可交易价在 pre_price。
    实证: 8/11 UE inject XOM/CVX/JD entry 逐一精确=昨收; JD 实际盘前已 -3.76%, 锚错 3.8%。
    与 DA skill v0.64 effective_price 同源同口径。
    [DEB-fullchain P1-11 2026-08-16] 盘前+锚开启但 pre_price 缺失/无效时不再回落昨收
      (回落源链 Finnhub/TwelveData 盘前 price 语义不明, 8/11 JD 锚错 3.8% 即该家族),
      返回 None → 调用方按"无现价"跳过 (scan_price=None 被可交易过滤排除, inject 显式
      [skip] 无现价), 宁可少一条指令不可锚错 SL/TP。非盘前时段/premarket_anchor=false
      行为不变。回滚: universe_expansion.premarket_anchor=false → 恒走旧口径。
    """
    if not q:
        return None
    if bool(_cfg("universe_expansion.premarket_anchor", True)):
        try:
            pre = float(q.get("pre_price") or 0)
        except (TypeError, ValueError):
            pre = 0.0
        if _is_us_premarket():
            if pre > 0:
                return pre
            return None
    return _quote_price(q)


# ── clue brief 组装 (信息获取) ────────────────────────────────
def _gather_news(hours: int) -> List[Dict[str, Any]]:
    """近 N 小时新闻 (RSSHub, llm_pipeline 同源); 失败返回 [].

    C1 (2026-07-27, 用户裁"提升标的/指令质量优先"): 并入 finnhub 市场新闻 (已付费闲置
    能力, 60/min 令牌桶+2h 缓存在位) — 同构 dict (source='finnhub_market') 进同一条链:
    brief 新闻段 + S0 渠道归因 + S1 切片全自动覆盖, 判分"持证上岗"。
    门控: universe_expansion.clue_finnhub_news (默认开); 回滚: 设 false。
    """
    items: List[Dict[str, Any]] = []
    try:
        from app.data.rsshub_us_news import RSSHubUSNewsFetcher
        items = RSSHubUSNewsFetcher().get_recent_news(hours=hours, limit=30) or []
    except Exception as e:
        print(f"  [warn] 新闻拉取失败 (降级): {e}")
    if bool(_cfg("universe_expansion.clue_finnhub_news", True)):
        try:
            from datetime import timezone as _tz
            from email.utils import format_datetime as _fmt_dt
            from app.data.finnhub_provider import FinnhubProvider
            from app.data.rsshub_us_news import RSSHubUSNewsFetcher as _RF
            _matcher = _RF.__new__(_RF)  # 只用 P4a 匹配器, 不碰 db
            fh = FinnhubProvider()
            cutoff_ts = datetime.now(_tz.utc).timestamp() - hours * 3600
            for a in (fh.get_market_news(limit=20) or []):
                ts = float(a.get("datetime") or 0)
                if ts and ts < cutoff_ts:
                    continue
                title = str(a.get("headline") or "").strip()
                if not title:
                    continue
                syms = set(f"US.{s}" for s in (a.get("relatedSymbols") or []) if s)
                syms |= _matcher._match_symbols_v2(f"{title} {a.get('summary') or ''}")
                # id 用 md5 非 hash(): hash() 带进程随机盐, 跨进程不稳定会污染 first_news_id 归因
                import hashlib as _hl
                items.append({
                    "id": f"fh_{_hl.md5(title.encode()).hexdigest()[:12]}",
                    "title": title,
                    "summary": str(a.get("summary") or "")[:300],
                    "source": "finnhub_market",
                    "source_name": f"Finnhub({a.get('source') or 'market'})",
                    "symbols": json.dumps(sorted(syms)),
                    "published_at": _fmt_dt(datetime.fromtimestamp(ts, _tz.utc)) if ts else "",
                })
        except Exception as e:
            print(f"  [warn] finnhub 新闻并入失败 (降级): {e}")
    # 合并后按发布时间降序 (brief 展示只取前 20, 不排序则 finnhub 条目永远被截断)
    def _pub_ts(n):
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(str(n.get("published_at") or "")).timestamp()
        except Exception:
            return 0.0
    items.sort(key=_pub_ts, reverse=True)
    return items


def _gather_market_corroboration() -> Dict[str, Any]:
    """市场佐证: 板块轮动/异动.

    2026-07-28 (板块级联批, 用户裁"要动"): 主源升级为 deep_sector_rotation.json —
    每日盘前生成 (07:42 ET) 含 sector_changes 数字, 但此前仓内零消费者 ("有人生成
    没人吃"): 07-27 它明确标注 Technology -1.44% 最弱, UE 却因只读周更机械影子
    capture 而对芯片满舵 BUY (TSM -95 实证)。周更 capture 降为 fallback。
    盘中脉搏 sector_pulse.json (若存在且新鲜) 一并注入。
    """
    out: Dict[str, Any] = {}
    # ── 主源: 每日盘前板块轮动 ──
    try:
        _rot_p = _ROOT / "data" / "deep_sector_rotation.json"
        if _rot_p.exists():
            _rot = json.loads(_rot_p.read_text())
            if str(_rot.get("date", "")) == _today():
                _chg = _rot.get("sector_changes") or {}
                out = {
                    "date": _rot.get("date"),
                    "strong_sectors": [f"{s}({_chg.get(s, '?')}%)" for s in (_rot.get("strongest") or [])[:3]],
                    "weak_sectors": [f"{s}({_chg.get(s, '?')}%)" for s in (_rot.get("weakest") or [])[:3]],
                    "movers": [],
                }
    except Exception as _re:
        print(f"  [warn] deep_sector_rotation 读取失败: {_re}")
    # ── 盘中脉搏补充 (sector_pulse.py 产出, 盘中 30min 更新) ──
    try:
        _pulse_p = _ROOT / "data" / "sector_pulse.json"
        if _pulse_p.exists():
            _pu = json.loads(_pulse_p.read_text())
            if str(_pu.get("date", "")) == _today() and _pu.get("ranked"):
                out.setdefault("date", _pu.get("date"))
                out["intraday_pulse"] = _pu["ranked"]
    except Exception:
        pass
    if out:
        return out
    # ── fallback: 周更机械影子 capture (原逻辑) ──
    recs = _read_jsonl(_MECH_CAPTURE_PATH)
    caps = [r for r in recs if r.get("type") == "capture"]
    if not caps:
        return {}
    last = caps[-1]
    # 防腐守卫 (2026-07-22): capture writer 曾停更, LLM 每日啃陈旧轮动数据浮现同批标的.
    #   若最新 capture 早于 4 个自然日, 视为失效 → 返回 {}, 让 LLM 落回实时新闻+硬事件推理.
    _cap_date = str(last.get("date", ""))
    try:
        _age_days = (datetime.strptime(_today(), "%Y-%m-%d")
                     - datetime.strptime(_cap_date, "%Y-%m-%d")).days
    except Exception:
        _age_days = 999
    if _age_days > 4:
        print(f"  [warn] 市场佐证 capture 已陈旧 (date={_cap_date or '?'}, {_age_days}d) → 跳过, "
              f"LLM 落回新闻+硬事件推理")
        return {}
    return {
        "date": last.get("date"),
        "strong_sectors": [f"{s['etf']}({s['name']}) {s['change_pct']:+}%"
                           for s in last.get("strong_sectors", [])],
        "weak_sectors": [f"{s['etf']}({s['name']}) {s['change_pct']:+}%"
                         for s in last.get("weak_sectors", [])],
        "movers": [f"{r['symbol']} {r['direction']} {r['change_pct']:+}%"
                   for r in last.get("surfaced", [])],
    }


def _gather_hard_events(news: List[Dict[str, Any]]) -> List[str]:
    """[ADR-017] 复用 CatalystClassifier(纯关键词, use_llm=False) 把已拉取新闻分类为
    结构化硬事件 → clue brief 的 [硬事件] 段, 供 V2 自评 HARD_EVENT。失败降级返回 []。"""
    if not news:
        return []
    try:
        from app.catalyst.event_classifier import CatalystClassifier
        from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency
    except Exception as e:
        print(f"  [warn] 催化分类器不可用 (降级, 无硬事件段): {e}")
        return []
    items = []
    for n in news:
        rel = n.get("symbols") or "[]"
        if isinstance(rel, str):
            try:
                rel = json.loads(rel)
            except Exception:
                rel = []
        if not isinstance(rel, list):
            rel = []
        try:
            items.append(UnifiedNewsItemV2(
                id=str(n.get("id", "")),
                title=str(n.get("title", "")),
                content=str(n.get("summary", "")),
                source=str(n.get("source", "rsshub")),
                source_name=str(n.get("source_name") or n.get("source", "rsshub")),
                timestamp=0.0,
                related_stocks=[str(s) for s in rel],
                urgency=NewsUrgency.NORMAL,
            ))
        except Exception:
            continue
    try:
        events = CatalystClassifier().classify(items, use_llm=False)
    except Exception as e:
        print(f"  [warn] 催化分类失败 (降级): {e}")
        return []
    _rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    events.sort(key=lambda ev: _rank.get(getattr(ev, "severity", "MEDIUM"), 3))
    out = []
    for ev in events[:15]:
        tks = ", ".join(str(t).replace("US.", "") for t in (ev.tickers or []))
        head = (ev.headline or "").strip()[:70]
        out.append(f"[{ev.severity}/{ev.event_type}] {head} → 关联: {tks or '(板块传导)'}")
    return out


def _gather_earnings_events() -> List[str]:
    """[ADR-018] 复用 EarningsCalendar (Alpha Vantage, 仅 1 次日历调用+内置缓存) 注入
    财报硬事件: 近期已发布(EARNINGS) + 临近(EARNINGS_UPCOMING 时点风险)。
    不用逐股 history 接口(5/min 限流不稳)。失败降级返回 []。"""
    try:
        from app.data.earnings_calendar import EarningsCalendar
    except Exception as e:
        print(f"  [warn] 财报日历不可用 (降级): {e}")
        return []
    out: List[str] = []
    try:
        ec = EarningsCalendar()
        for e in ec.get_recently_reported(days_back=2)[:8]:
            out.append(f"[HIGH/EARNINGS] {e.get('ticker','')} 财报已发布 {e.get('report_date','')}"
                       f"({e.get('time_of_day','')}) → 关注财报后动量")
        for u in ec.get_watchlist_earnings(days_ahead=3)[:8]:
            out.append(f"[MEDIUM/EARNINGS_UPCOMING] {u.get('ticker','')} 财报临近 {u.get('report_date','')}"
                       f"({u.get('days_until','?')}d) → 时点风险")
    except Exception as e:
        print(f"  [warn] 财报事件拉取失败 (降级): {e}")
    return out


# ── [UE-B1影子版 2026-08-29, DEB-20260829 第3轮重新设计] 全市场财报影子 ──
# 病灶 (U2): get_recently_reported/get_watchlist_earnings 内部均过滤到 watchlist,
#   宇宙外财报一条进不了发现层 (8/28 实测 clue "财报:0" 而同日全市场 8 家)。
# 第3轮查清: EarningsCalendar 无表级持久化 (ca1b4748 "147条入库"=进程内返回列表,
#   措辞误导), 消费方式就是进程内 get_earnings_calendar() 活体拉取 (富途主源)。
# 影子首版"只记不喂": 全市场当日+次日财报 → 排序键=与其他 clue 源交叉命中优先
#   (8-K/盘前榜/事件标的任一源出现者排前, 其余按报告时距升序) → 前8行落
#   data/shadow/ue_earnings_clue_shadow.jsonl, 不进 clue brief。
#   5交易日后看"影子行被其他源交叉印证比例"裁决是否转正 (分歧3排序键天然解)。
# 回滚: 配置 universe_expansion.clue_earnings_shadow: false。
_EARNINGS_SHADOW_PATH = _ROOT / "data" / "shadow" / "ue_earnings_clue_shadow.jsonl"


def _gather_earnings_shadow(corroboration_syms: set) -> None:
    """全市场财报日历 → 影子落盘 (不返回, 不喂模型)。失败静默降级。"""
    if not bool(_cfg("universe_expansion.clue_earnings_shadow", True)):
        return
    try:
        from app.data.earnings_calendar import EarningsCalendar
        ec = EarningsCalendar()
        cal = ec.get_earnings_calendar()  # 富途主源, 进程内缓存 TTL 1h
        if not cal:
            return
        today = _today()
        try:
            from datetime import datetime as _dt
            _t = _dt.strptime(today, "%Y-%m-%d")

            def _days_until(rd: str) -> int:
                try:
                    return (_dt.strptime(str(rd)[:10], "%Y-%m-%d") - _t).days
                except Exception:
                    return 999
            # 只取当日+次日 (时点风险窗口), 其余历史段不采
            cal = [r for r in cal if 0 <= _days_until(r.get("reportDate", "")) <= 1]
        except Exception:
            return
        rows = []
        for r in cal:
            sym = str(r.get("symbol", "")).upper().replace("US.", "")
            if not sym:
                continue
            rows.append({
                "date": today, "symbol": sym,
                "name": r.get("name", ""),
                "report_date": str(r.get("reportDate", ""))[:10],
                "days_until": _days_until(r.get("reportDate", "")),
                "cross_hit": sym in corroboration_syms,
                "fetched_at": _now_et().isoformat(),
            })
        # 排序: 交叉命中优先 → 报告时距升序; 取前8
        rows.sort(key=lambda x: (not x["cross_hit"], x["days_until"], x["symbol"]))
        rows = rows[:8]
        _EARNINGS_SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _EARNINGS_SHADOW_PATH.open("a") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  [earnings-shadow] {len(rows)} 行全市场财报影子 "
              f"(交叉命中{sum(1 for r in rows if r['cross_hit'])}) → {_EARNINGS_SHADOW_PATH.name}")
    except Exception as e:
        print(f"  [warn] 财报影子落盘失败 (降级): {e}")


def _gather_insider_clusters() -> List[str]:
    """[ADR-018] 读今日已持久化的内部人集群报告 (insider_daily_{date}.json, 无网络).
    集群买入=看多硬事件。无今日文件/无信号 → []。"""
    try:
        path = _ROOT / "data" / "reports" / f"insider_daily_{_today()}.json"
        if not path.exists():
            return []
        d = json.loads(path.read_text())
        by_sym = (d.get("stats") or {}).get("by_symbol") or {}
        out: List[str] = []
        for sym, cnt in by_sym.items():
            t = str(sym).replace("US.", "")
            out.append(f"[HIGH/INSIDER_CLUSTER] {t} 内部人集群买入信号 x{cnt} → 看多")
        return out
    except Exception as e:
        print(f"  [warn] 内部人集群读取失败 (降级): {e}")
        return []


def _gather_event_symbols_file() -> List[str]:
    """[P2' 2026-07-21] 读 gen_event_symbols.py 的当日产出 (data/event_symbols_{ET}.json).

    给 V2 提供"预消化的事件驱动标的+原因"作为佐证原料 — MARKET_CORROBORATED 需要
    可引用证据, 事件清单正是证据源; 不动 prompt 克制标准. 文件不存在/为空 → []。
    同一天 DA --symbols 与本段共用同一文件, 双源原料同源。"""
    try:
        path = _ROOT / "data" / f"event_symbols_{_today()}.json"
        if not path.exists():
            return []
        d = json.loads(path.read_text())
        out: List[str] = []
        for e in (d.get("event_symbols") or [])[:10]:
            t = str(e.get("symbol", "")).replace("US.", "")
            reasons = "; ".join(str(r)[:60] for r in (e.get("reasons") or [])[:2])
            out.append(f"- {t} (score={e.get('score', 0)}): {reasons}")
        return out
    except Exception as e:
        print(f"  [warn] 事件标的文件读取失败 (降级): {e}")
        return []


def _gather_peer_surge() -> List[str]:
    """[P3 2026-07-22] 盘前峰群异动段 — 读 premarket_peer_shadow.jsonl 当日记录 (record-only 影子),
    把板块级盘前异动作为 clue 供 V2 引用 (evidence_ref 可用 peer_surge).
    定位: 输入端扩容, 非放行信号 — "峰群直接跟随"已被 7 天回测否决 (-8.48%, 唯一 CONFIRM 日 -1.32%),
    此段仅把高波动板块票送入分析池, gate 照常把关. 文件缺失/当日无记录 → 跳过 (降级无碍).
    回滚: 配置 universe_expansion.clue_peer_surge: false."""
    try:
        path = Path("data/shadow/premarket_peer_shadow.jsonl")
        if not path.exists():
            return []
        today = _today()
        rec: Optional[dict] = None
        for line in path.read_text().strip().splitlines():
            try:
                d = json.loads(line)
                if d.get("date") == today and d.get("peers_snapshot"):
                    rec = d  # 当日最后一条 (refresh-join 复写保留快照)
            except Exception:
                continue
        if not rec:
            return []
        snap = rec.get("peers_snapshot") or {}

        def _fmt(members: List[str]) -> str:
            cells = []
            for sym in members:
                v = snap.get(sym)
                if isinstance(v, dict) and v.get("pre_change") is not None:
                    cells.append(f"{sym}{float(v['pre_change']):+.1f}%(量比{float(v.get('volume_ratio', 0)):.2f})")
            return " ".join(cells)

        # 2026-07-22: 多板块组 (groups + groups_members 自描述成分); 旧记录无 groups → 回落顶层存储组 (向后兼容)
        groups = rec.get("groups") or {}
        if groups:
            members_map = rec.get("groups_members") or {}
            out = []
            for name, gd in groups.items():
                line = _fmt([str(s).upper() for s in (members_map.get(name) or [])])
                if line:
                    out.append(f"{name}峰群 verdict={(gd or {}).get('verdict', '?')}: {line}")
            return out
        cells: List[str] = []
        for sym, v in snap.items():
            if isinstance(v, dict) and v.get("pre_change") is not None:
                cells.append(f"{sym}{float(v['pre_change']):+.1f}%(量比{float(v.get('volume_ratio', 0)):.2f})")
        if not cells:
            return []
        return [f"存储峰群 verdict={rec.get('group_verdict', '?')}: " + " ".join(cells)]
    except Exception as e:
        print(f"  [warn] 峰群影子读取失败 (降级): {e}")
        return []


def _gather_premarket_rank_clues() -> List[str]:
    """[2026-08-29 盘前榜过滤规则, 用户裁"按建议执行"] 富途全市场盘前榜质量门候选 → 线索。

    数据链: premarket_enrichment.ensure_premarket_rank_candidates() 幂等保证当日文件
      (data/premarket_rank_candidates_{ET}.json) 存在 — 盘前榜原始 50 只/向已过
      质量门 (盘前成交额≥$5M / 价格≥$5 / 市值≥$2B / ETF剔除 / |涨幅|>25% 打标不入围),
      每向 ≤5 候选。本函数只负责把过滤结果渲染成 clue 行。
    定位: 输入端扩容 (同 peer_surge), 非放行信号 — 候选进分析池, gate 照常把关。
      涨幅带影子打标: EXTREME 候选不入围只展示, 预注册判据攒样 (>25% vs 带内配对) 待裁决。
    文件缺失/当日无候选 → 触发一次采集兜底 (盘前 21:10 CST 首跑即产文件);
      采集失败降级 []。回滚: 配置 universe_expansion.clue_premarket_rank: false。"""
    try:
        from app.data.premarket_enrichment import ensure_premarket_rank_candidates
        path = ensure_premarket_rank_candidates(_today())
        if not path or not path.exists():
            return []
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("date") != _today():
            return []  # 陈旧文件 (跨日残留) 不消费
        out: List[str] = []

        def _fmt_side(side: Dict, label: str) -> None:
            for c in (side.get("candidates") or []):
                tags = ",".join(c.get("tags") or [])
                tag_s = f" [{tags}]" if tags else ""
                mcap_b = round(float(c.get("market_cap_usd", 0)) / 1e9, 1)
                out.append(f"- {c['symbol']} 盘前{label}#{c.get('rank', '?')} "
                           f"{c['change_pct']:+.1f}% 盘前成交${float(c.get('turnover_usd', 0)) / 1e6:.1f}M "
                           f"市值${mcap_b}B{tag_s}")
            for c in (side.get("extreme") or []):
                # 极端带: 展示但不入围 (影子攒样), 显式告知模型别当候选
                out.append(f"- {c['symbol']} 盘前{label}极端涨幅带({c['change_pct']:+.1f}%) "
                           f"[仅记录不入围: 追高=买顶风险, 预注册攒样中]")

        _fmt_side(d.get("gainers") or {}, "涨")
        _fmt_side(d.get("losers") or {}, "跌")
        return out
    except Exception as e:
        print(f"  [warn] 盘前榜线索读取失败 (降级): {e}")
        return []


def _latest_insider_report_age() -> Optional[int]:
    """最近一份 insider_daily 报告距今天数 (0=今日); 无任何报告返回 None."""
    try:
        files = sorted((_ROOT / "data" / "reports").glob("insider_daily_*.json"))
        if not files:
            return None
        d = files[-1].stem.replace("insider_daily_", "")
        return (datetime.strptime(_today(), "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days
    except Exception:
        return None


def _build_feedback_table(days: int = 7, max_rows: int = 10) -> List[str]:
    """v2.2 (P3): 本源近 N 日浮现→fwd 结果小表 (供 prompt 负反馈注入)。

    口径: playbook_instructions 中 source=universe_expansion 的方向指令,
    fwd = fwd_return_intraday 优先 (t1 兜底), 按方向调整 (SHORT 取负)。失败返回 []。
    """
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{_ROOT / 'data' / 'ftseek_us.db'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, symbol, direction, fwd_return_intraday, fwd_return_t1, raw_instruction_json "
            "FROM playbook_instructions WHERE date >= date(?, ?) "
            "AND direction IN ('BUY','SELL_SHORT') AND raw_instruction_json LIKE '%universe_expansion%' "
            "ORDER BY date DESC, symbol", (_today(), f"-{days} day")).fetchall()
        conn.close()
        out = []
        for r in rows[:max_rows]:
            f = r["fwd_return_intraday"] if r["fwd_return_intraday"] is not None else r["fwd_return_t1"]
            if f is None:
                res = "待回填"
            else:
                adj = float(f) * (1 if r["direction"] == "BUY" else -1) * 100
                res = f"{adj:+.1f}%" + (" ✓" if adj > 0 else " ✗")
            _dir = "LONG" if r["direction"] == "BUY" else "SHORT"
            out.append(f"- {r['date']} {r['symbol'].replace('US.', '')} {_dir}: {res}")
        return out
    except Exception as e:
        print(f"  [warn] 负反馈表构建失败 (降级无注入): {e}")
        return []


def _build_clue_brief(manual_clue: str = "") -> str:
    hours = int(_cfg("universe_expansion.news_lookback_hours", 24))
    parts = [f"日期: {_today()} (ET)"]
    health: List[str] = []  # [新鲜度 2026-07-22] 各输入源健康摘要 (日志可观测, 区分"无事件"与"链路断")
    # [U8 2026-08-25 evidence grounding] 材料行缓存 — 供 _ground_evidence_refs 做
    #   "引证是否真实存在于今日输入"的代码核验。每次构建重置。
    global _LAST_BRIEF_MATERIALS
    _LAST_BRIEF_MATERIALS = []

    if manual_clue:
        parts.append(f"\n[人工线索]\n{manual_clue.strip()}")

    news = _gather_news(hours)

    # [ADR-017/018] 结构化硬事件段 (config 驱动, 传导优先级最高, 供 V2 自评 HARD_EVENT)
    hard: List[str] = []
    _corro_syms: set = set()  # [UE-B1影子] 财报影子交叉印证符号集 (8-K/13DG ticker)
    if bool(_cfg("universe_expansion.clue_hard_events", True)):
        _he = _gather_hard_events(news)            # 新闻分类 (关键词, 零成本)
        hard += _he
        health.append(f"硬事件:{len(_he)}")
    if bool(_cfg("universe_expansion.clue_earnings_events", True)):
        _ee = _gather_earnings_events()            # 财报日历 (Alpha Vantage)
        hard += _ee
        health.append(f"财报:{len(_ee)}")
    if bool(_cfg("universe_expansion.clue_insider_clusters", True)):
        _ic = _gather_insider_clusters()           # 内部人集群 (已持久化文件)
        hard += _ic
        _age = _latest_insider_report_age()
        health.append(f"内部人:{len(_ic)}" + ("(报告缺失)" if _age is None
                      else (f"(断更{_age}d)" if _age > 0 else "")))
    # S3 (2026-07-27, DEB-20260727): EDGAR 8-K 直连 — 分钟级官方一手源, CIK→ticker 精确,
    #   item code 带方向先验标注 (2.02业绩/4.02重述强利空等)。config 门控, 失败降级空。
    if bool(_cfg("universe_expansion.clue_8k_events", True)):
        try:
            from app.data.sec_8k_provider import fetch_recent_8k
            _8k = fetch_recent_8k(lookback_hours=int(_cfg(
                "universe_expansion.news_lookback_hours", 24)))
            # [UE-A3 2026-08-29] ETF/权证/商品信托例行文件剔除 (先过滤再取15, 不足不补凑)
            _8k = _filter_8k_noise(_8k)
            _8k_lines = [f"8-K {r['ticker']}: {'; '.join(r['item_labels'])} "
                         f"(filed {r['updated'][:16]})" for r in _8k[:15]]
            hard += _8k_lines
            health.append(f"8-K:{len(_8k_lines)}")
            _corro_syms.update(str(r.get("ticker", "")).upper().replace("US.", "")
                               for r in _8k)  # [UE-B1影子] 交叉印证源①
        except Exception as _8k_e:
            print(f"  [warn] 8-K 直连失败 (降级): {_8k_e}")
            health.append("8-K:err")
    # C2 (2026-07-27, 用户裁"提升标的/指令质量优先"): SC 13D/G 举牌 — 常先于新闻的
    #   强 HARD_EVENT (13D=主动举牌看多先验, 13G=被动持仓弱信号)。门控+降级同 8-K。
    if bool(_cfg("universe_expansion.clue_13dg_events", True)):
        try:
            from app.data.sec_8k_provider import fetch_recent_13dg
            _dg = fetch_recent_13dg(lookback_hours=96)
            # [UE-A3 2026-08-29] 权证/优先股举牌剔除 (对普通股发现无事件意义)
            _dg = [r for r in _dg
                   if not _is_13dg_noise(r.get("ticker", ""), r.get("company", ""))]
            _dg_lines = [f"举牌 {r['ticker']}: {r['form']}"
                         f"{' (主动举牌, 看多先验强)' if r['form'].startswith('SC 13D') else ' (被动持仓)'} "
                         f"(filed {r['updated'][:16]})" for r in _dg[:10]]
            hard += _dg_lines
            health.append(f"13DG:{len(_dg_lines)}")
            _corro_syms.update(str(r.get("ticker", "")).upper().replace("US.", "")
                               for r in _dg)  # [UE-B1影子] 交叉印证源②
        except Exception as _dg_e:
            print(f"  [warn] 13D/G 直连失败 (降级): {_dg_e}")
            health.append("13DG:err")
    if hard:
        parts.append("\n[硬事件 (已分类, 传导优先级最高)]")
        parts.extend(hard)

    # [P2' 2026-07-21] 事件驱动标的清单 (gen_event_symbols 当日产出, 与 DA --symbols 同源).
    #   给 V2 可引用的佐证原料; 文件缺失=当日未生成 → 跳过, 行为与之前一致. 回滚: 配置设 false.
    if bool(_cfg("universe_expansion.clue_event_symbols", True)):
        es = _gather_event_symbols_file()
        health.append(f"事件标的:{len(es)}")
        if es:
            parts.append("\n[今日事件驱动标的 (事件清单, 可作 evidence_ref 引用)]")
            parts.extend(es)

    # [P3 2026-07-22] 盘前板块峰群 (影子测量转 clue 输入; 仅热度参考, 跟随策略已回测否决).
    if bool(_cfg("universe_expansion.clue_peer_surge", True)):
        ps = _gather_peer_surge()
        health.append(f"峰群:{len(ps)}组")
        if ps:
            parts.append("\n[盘前板块峰群 (影子实测, 热度参考非跟随信号)]")
            parts.extend(ps)

    # [2026-08-29 盘前榜过滤规则] 富途全市场盘前榜质量门候选 (成交额/价格/市值/ETF门后每向≤5)。
    #   定位同峰群: 输入端扩容非放行信号; 极端涨幅带已剔除只展示。回滚: 配置设 false。
    if bool(_cfg("universe_expansion.clue_premarket_rank", True)):
        pr = _gather_premarket_rank_clues()
        health.append(f"盘前榜:{len(pr)}条")
        if pr:
            parts.append("\n[盘前全市场榜质量门候选 (已过成交额≥$5M/价格≥$5/市值≥$2B门, 候选非信号)]")
            parts.extend(pr)

    mkt = _gather_market_corroboration()
    health.append(f"佐证:{mkt.get('date') or '无/陈旧'}")
    if mkt:
        parts.append("\n[市场佐证 (板块轮动/异动)]")
        if mkt.get("strong_sectors"):
            parts.append("轮入板块: " + ", ".join(mkt["strong_sectors"]))
        if mkt.get("weak_sectors"):
            parts.append("轮出板块: " + ", ".join(mkt["weak_sectors"]))
        if mkt.get("movers"):
            parts.append("宇宙外异动: " + ", ".join(mkt["movers"][:12]))
        if mkt.get("intraday_pulse"):
            parts.append("盘中板块脉搏(实时): " + ", ".join(str(x) for x in mkt["intraday_pulse"][:11]))

    if news:
        # [2026-07-22] symbols 字段为 JSON 字符串, 空列表 "[]" 真值恒真曾拼出 "[[]]" 噪声 → 解析后非空才展示
        _n_sym = 0
        parts.append(f"\n[近{hours}h 新闻头条 (共{len(news)}条, 取前20; 时效越新传导价值越高)]")
        for n in news[:20]:
            title = (n.get("title") or "").strip()
            syms = n.get("symbols") or ""
            if isinstance(syms, str):
                try:
                    syms = json.loads(syms)
                except Exception:
                    syms = [syms] if syms else []
            syms = [str(s).replace("US.", "") for s in syms if s]
            if syms:
                _n_sym += 1
            # v2.2 (2026-07-25, P4 信息饮食): 每条带时效小时数 — LLM 无时钟,
            #   不标时效则 23h 前旧闻与 1h 前突发同权重 (重复推荐的助燃剂之一)
            _age_tag = ""
            try:
                _pub = str(n.get("published_at") or "")
                if _pub:
                    # RSSHub 返 RFC2822 ('Fri, 24 Jul 2026 16:21:16 -0400'); ISO 兜底
                    try:
                        from email.utils import parsedate_to_datetime
                        _pub_dt = parsedate_to_datetime(_pub)
                    except Exception:
                        _pub_dt = datetime.fromisoformat(_pub.replace("Z", "+00:00"))
                    _age_h = (datetime.now(_pub_dt.tzinfo) - _pub_dt).total_seconds() / 3600
                    if 0 <= _age_h < 200:
                        _age_tag = f" ({_age_h:.0f}h前)"
            except Exception:
                pass
            if title:
                parts.append(f"- {title}{_age_tag}" + (f"  [{', '.join(syms)}]" if syms else ""))
        health.append(f"新闻:{len(news)}条(带标的{_n_sym})")
    else:
        parts.append("\n[新闻] 无近期新闻 (依赖市场佐证 + 人工线索推理)")
        health.append("新闻:0⚠️")

    # v2.2 (2026-07-25, P3 负反馈注入): 本源近7日浮现→结果小表 — 模型每天看同样新闻
    #   自然得同样结论, 它不知道昨天推错了 (ORCL SHORT×5 根因)。比代码硬冷却聪明:
    #   保留"事件持续发酵"的合理重复, 砍"无新信息惯性重复"。回滚: 配置 clue_feedback=false。
    if bool(_cfg("universe_expansion.clue_feedback", True)):
        fb = _build_feedback_table()
        health.append(f"负反馈:{len(fb)}条")
        if fb:
            parts.append("\n[本源近7日浮现回顾 (方向调整后前瞻收益, 负=推错)]")
            parts.extend(fb)
            parts.append("规则: 对上表中连续推错(连续负收益)的 同标的+同方向, 除非今日材料中出现"
                         "【新增量证据】(新事件/新数据), 否则不得再次浮现; 若确有新证据, evidence_ref 必须引用新证据。")

    # [UE-B1影子 2026-08-29] 全市场财报影子落盘 (只记不喂) — 交叉印证源③事件标的文件 ④盘前榜
    try:
        _es_path = _ROOT / "data" / f"event_symbols_{_today()}.json"
        if _es_path.exists():
            _esd = json.loads(_es_path.read_text())
            _corro_syms.update(str(e.get("symbol", "")).upper().replace("US.", "")
                               for e in (_esd.get("event_symbols") or []))
        _pm_path = _ROOT / "data" / f"premarket_rank_candidates_{_today()}.json"
        if _pm_path.exists():
            _pmd = json.loads(_pm_path.read_text())
            for _side in ("gainers", "losers"):
                for _c in ((_pmd.get(_side) or {}).get("candidates") or []):
                    _corro_syms.add(str(_c.get("symbol", "")).upper().replace("US.", ""))
    except Exception:
        pass
    _gather_earnings_shadow(_corro_syms)

    print(f"  [输入源健康] " + " | ".join(health))
    # [U9 2026-08-25 建议②] 扫描时注入当日 regime 摘要 — 与注入端同源 (MarketRegimeGate),
    #   让模型带着市场状态做因果推理 (此前注入端才算, 模型扫描时看不见)。
    #   失败降级空, 不阻断。回滚: universe_expansion.scan_regime_brief=false。
    if bool(_cfg("universe_expansion.scan_regime_brief", True)):
        try:
            from app.playbook.market_regime import MarketRegimeGate as _MRG_scan
            _rc_scan = _MRG_scan().assess()
            _spy_trend = "above_ma20" if _rc_scan.spy_ma20 >= _rc_scan.spy_ma50 else "below_ma20"
            _regime_line = (f"regime={_rc_scan.regime.value} · VIX={_rc_scan.vix} · "
                            f"SPY {_spy_trend} (MA20 {_rc_scan.spy_ma20:.1f} / MA50 {_rc_scan.spy_ma50:.1f})")
            parts.append("\n[当日市场环境 (与执行端同源口径, 因果推理须考虑)]")
            parts.append(_regime_line)
        except Exception as _rg_scan_e:
            print(f"  [warn] regime 摘要注入失败 (降级, 不阻断): {_rg_scan_e}")
    # [U8 2026-08-25 evidence grounding] 收集全部材料行 — 供引证真实性核验。
    #   拆行保留 (人工线索段是头+内容单元素); 排除节标题与日期行。
    for _p in parts:
        for _line in str(_p).split("\n"):
            _line = _line.strip()
            if _line and not _line.startswith("[") and not _line.startswith("日期"):
                _LAST_BRIEF_MATERIALS.append(_line)
    return "\n".join(parts)


# ── scan: LLM 推理浮现标的 ─────────────────────────────────────
def _normalize_surfaced(raw: Any, with_tier: bool = False) -> List[Dict[str, Any]]:
    """归一化 LLM 浮现标的。with_tier=True 时额外解析 evidence_ref/self_tier,
    并做 Track A 夹逼: evidence_ref 为空 → 强制 self_tier=THESIS_ONLY 且 confidence≤0.60
    (标签归一化, 非过滤门; V1 路径不传 with_tier, 行为不变)。"""
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("symbol", "")).replace("US.", "").upper().strip()
        direction = str(it.get("direction", "SKIP")).upper().strip()
        if not sym or direction not in ("LONG", "SHORT", "SKIP"):
            continue
        try:
            conf = float(it.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        # [DEB-fullchain #24 2026-08-16] schema 契约夹逼 [0,1]: LLM 偶发输出 1.2/7 之类,
        #   未夹逼时 cap50 仍放行 100、7 被当 700% 截成 50, 语义双错。夹逼后 cap 语义稳定。
        conf = min(max(conf, 0.0), 1.0)
        wl = str(it.get("winner_loser", "")).upper().strip()
        item = {
            "symbol": sym,
            "direction": direction,
            "causal_chain": str(it.get("causal_chain", ""))[:200],
            "thesis": str(it.get("thesis", ""))[:120],
            "confidence": round(conf, 3),
            "is_universe_external": bool(it.get("is_universe_external", False)),
            "winner_loser": wl if wl in ("WINNER", "LOSER") else "",
            "corroborated_by_market": bool(it.get("corroborated_by_market", False)),
            # [U4 2026-08-25] SKIP 理由透传 — 拒绝可归因 (审计病灶: 拒绝不可归因)
            "skip_reason": str(it.get("skip_reason", ""))[:120].strip() if direction == "SKIP" else "",
        }
        if with_tier:
            ev = str(it.get("evidence_ref", ""))[:200].strip()
            tier = str(it.get("self_tier", "")).upper().strip()
            _tier_fallback = False  # 2026-08-22 (UE P1): 非法 tier 静默替换留痕, 攒样数据可区分
            if tier not in ("HARD_EVENT", "MARKET_CORROBORATED", "THESIS_ONLY"):
                tier = "THESIS_ONLY"
                _tier_fallback = True
            # [U8 2026-08-25 evidence grounding] 引证真实性核验 — 防伪造引证骗过夹逼。
            #   enforce=false(默认, 观测): 只留痕 evidence_grounded, 不改判定;
            #   enforce=true: 未过核验的引证视同无引证 → 落 Track A 夹逼。
            #   判据: 攒样 5 交易日, 人工抽检匹配误杀率 <10% → 裁开强制。回滚=删键。
            _ev_grounded = True
            if ev and bool(_cfg("universe_expansion.evidence_grounding", True)):
                try:
                    _ev_grounded = _evidence_grounded(ev, _LAST_BRIEF_MATERIALS)
                except Exception:
                    _ev_grounded = True  # 核验器自身故障 → 不误杀
                if not _ev_grounded and bool(_cfg("universe_expansion.evidence_grounding_enforce", False)):
                    ev = ""  # 视同无引证 → 下方 Track A 夹逼接管
            raw_conf = conf
            clamped = False
            if not ev:  # Track A 夹逼: 无证据 → 强制降级 (兜底 LLM 不守自律约束)
                tier = "THESIS_ONLY"
                if conf > 0.60:
                    clamped = True
                conf = min(conf, 0.60)
            item["evidence_ref"] = ev
            item["self_tier"] = tier
            # [U8] 引证真实性留痕 — 供攒样判误杀率; 未过核验且未开强制时不改变判定
            item["evidence_grounded"] = _ev_grounded
            item["raw_confidence"] = round(raw_conf, 3)
            item["clamped"] = clamped
            if _tier_fallback:
                item["_tier_fallback"] = True  # LLM 输出非法 tier, 被代码兜底替换
            item["confidence"] = round(conf, 3)
            # v2.2 (2026-07-25, P1 可证伪字段): 纯落库供判分, 不进任何执行逻辑
            item["invalidation"] = str(it.get("invalidation", ""))[:120]
            # [U10 2026-08-25 建议④] 结构化失效触发器解析 — 白名单与
            #   tripwire_checker._get_structured_triggers 同契约 (同一消费方):
            #   metric∈{收盘价,成交量,5日涨跌,盘前涨跌幅} & operator∈{<,>} & value可float。
            #   非法条目静默丢弃 (宁漏语义, 与消费端一致)。
            _it_metrics = ("收盘价", "成交量", "5日涨跌", "盘前涨跌幅")
            _it_raw = it.get("invalidation_triggers") or []
            if not isinstance(_it_raw, list):
                _it_raw = []
            _it_ok = []
            for _t in _it_raw[:4]:  # 上限4条, 防异常输出膨胀
                if not isinstance(_t, dict):
                    continue
                _tm = str(_t.get("metric") or "").strip()
                _to = str(_t.get("operator") or "").strip()
                if _tm not in _it_metrics or _to not in ("<", ">"):
                    continue
                try:
                    _tv = float(_t.get("value"))
                except (TypeError, ValueError):
                    continue
                _it_ok.append({"metric": _tm, "operator": _to, "value": _tv})
            item["invalidation_triggers"] = _it_ok
            try:
                item["horizon_hours"] = int(it.get("horizon_hours", 0) or 0)
            except (TypeError, ValueError):
                item["horizon_hours"] = 0
            try:
                item["expected_move_pct"] = round(float(it.get("expected_move_pct", 0) or 0), 2)
            except (TypeError, ValueError):
                item["expected_move_pct"] = 0.0
            # v2.2 (P2 rubric化): 校准从模型移到代码 — 模型只枚举证据, 分数代码算。
            #   rubric_score = 四项 checklist 命中数 (0-4), 与 conf 并列落库供相关性对比 (预注册 Q2)。
            _cl = it.get("evidence_checklist") or {}
            if not isinstance(_cl, dict):
                _cl = {}
            _keys = ("catalyst_specific", "catalyst_within_24h", "price_volume_confirmed", "crowding_low")
            item["evidence_checklist"] = {k: bool(_cl.get(k, False)) for k in _keys}
            item["rubric_score"] = sum(1 for k in _keys if _cl.get(k))
            # v2.2 (P5 SHORT 高门槛): short_check 解析落库 (纯归因, 不拦不改流程)
            if direction == "SHORT":
                _sc = it.get("short_check") or {}
                if not isinstance(_sc, dict):
                    _sc = {}
                try:
                    _sqz = int(_sc.get("squeeze_risk", 0) or 0)
                except (TypeError, ValueError):
                    _sqz = 0
                try:
                    _dce = int(_sc.get("downside_catalyst_expiry_hours", 0) or 0)
                except (TypeError, ValueError):
                    _dce = 0
                item["short_check"] = {
                    "borrow_feasible": bool(_sc.get("borrow_feasible", False)),
                    "downside_catalyst_expiry_hours": _dce,
                    "squeeze_risk": _sqz,
                    "provided": bool(_sc),   # 模型是否交作业 (未交=旧习惯残留, 切片可判)
                }
        out.append(item)
    return out


def _norm_text(s: str) -> str:
    """[U8] 归一化文本用于匹配: 小写, 只留字母数字与汉字."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(s).lower())


def _evidence_grounded(ev: str, materials: List[str], k: int = 6, ratio: float = 0.5) -> bool:
    """[U8 2026-08-25] 引证真实性核验 (纯代码): 模型引用的证据片段是否出自今日输入材料。

    方法: 双方归一化后取 k 字符滑窗, 证据窗口命中材料库的比例 ≥ ratio → 判为真实引证。
    短引证 (<k) 退化为整串包含匹配。防"编一句像材料的话"骗过 Track A 夹逼。
    """
    ev_n = _norm_text(ev)
    if not ev_n or not materials:
        return False
    if len(ev_n) < k:
        return any(ev_n in _norm_text(m) for m in materials)
    grams = set()
    for m in materials:
        mn = _norm_text(m)
        grams.update(mn[i:i + k] for i in range(max(len(mn) - k + 1, 0)))
    ev_grams = [ev_n[i:i + k] for i in range(len(ev_n) - k + 1)]
    if not ev_grams:
        return False
    hit = sum(1 for g in ev_grams if g in grams)
    return hit / len(ev_grams) >= ratio


def _validate_and_repair_surfaced(surfaced: List[Dict[str, Any]]) -> Dict[str, Any]:
    """[U4 2026-08-25, DEB-ue-audit r7] per-item 输出契约校验 + 单次白名单修复。

    契约 (SYSTEM_PROMPT_V2 已要求的字段):
      - SHORT   : short_check 必填且 downside_catalyst_expiry_hours > 0
      - SKIP    : skip_reason 非空
      - 非SKIP  : invalidation 非空
    违约 → 单次 LLM repair 调用, 白名单仅接受 short_check / skip_reason / invalidation;
    direction / confidence / thesis 一律拒收 (r13 零方向变更的结构性保障照抄)。
    开关: universe_expansion.schema_enforce (config) — False/缺省 = 只记录违约与修复
    (观测模式, 不丢标的); True = 修复后仍违约的标的丢弃。
    """
    stats: Dict[str, Any] = {"violations": [], "repaired": 0, "still_bad": 0,
                             "dropped": 0, "direction_patch_rejected": 0,
                             "enforce": bool(_cfg("universe_expansion.schema_enforce", False))}

    def _missing(s: Dict[str, Any]) -> List[str]:
        m = []
        d = s.get("direction")
        if d == "SHORT":
            sc = s.get("short_check") or {}
            if not sc.get("provided"):
                m.append("short_check")
            elif int(sc.get("downside_catalyst_expiry_hours") or 0) <= 0:
                m.append("short_check.downside_catalyst_expiry_hours")
        elif d == "SKIP":
            if not str(s.get("skip_reason") or "").strip():
                m.append("skip_reason")
        if d in ("LONG", "SHORT"):
            if not str(s.get("invalidation") or "").strip():
                m.append("invalidation")
        return m

    bad = [(i, s, _missing(s)) for i, s in enumerate(surfaced)]
    bad = [(i, s, m) for i, s, m in bad if m]
    stats["violations"] = [{"symbol": s.get("symbol"), "direction": s.get("direction"),
                            "missing": m} for _, s, m in bad]
    if not bad:
        stats["clean"] = True
        return stats

    from app.llm.model_call import call_llm
    # 单次 repair 调用: 只问违约项, 只收白名单字段
    try:
        _rep_prompt = ("以下浮现标的缺少必填字段。只补齐方括号内列出的缺失字段, 逐条输出该标的的"
                       "完整补齐值。禁止改动任何其他字段。严格 JSON: "
                       "{\"repairs\": [{\"symbol\": str, \"invalidation\": str(缺失时), "
                       "\"skip_reason\": str(缺失时), \"short_check\": {\"borrow_feasible\": bool, "
                       "\"downside_catalyst_expiry_hours\": int, \"squeeze_risk\": int}(缺失时)}]}\n\n"
                       + json.dumps([{"symbol": s.get("symbol"), "direction": s.get("direction"),
                                      "thesis": s.get("thesis"), "causal_chain": s.get("causal_chain"),
                                      "missing": m} for _, s, m in bad], ensure_ascii=False))
        _rep = call_llm(feature="universe_expansion_reasoning", prompt=_rep_prompt,
                        json_mode=True, max_tokens=8000)
        # [DEB-20260829 第10轮 9.4 修复] 1200→8000: glm-5.2 实测输出 5448-6070 tok,
        # 1200 必截断且本调用无 schema_required → 降档自救链不可达 (has_schema=False)。
        # 抬到与主调用点同限, 截断时 finish_reason=length 可观测。
        if _rep and _rep.success:
            _rp = _rep.response if isinstance(_rep.response, dict) else json.loads(_rep.response)
            _rmap = {str(r.get("symbol", "")).replace("US.", "").upper(): r
                     for r in (_rp.get("repairs") or []) if isinstance(r, dict)}
            for i, s, m in bad:
                r = _rmap.get(str(s.get("symbol", "")).upper())
                if not r:
                    continue
                fixed = []
                # 白名单逐字段回填 — direction/confidence/thesis 永不接受补丁
                if "invalidation" in m:
                    v = str(r.get("invalidation") or "").strip()
                    if v:
                        s["invalidation"] = v[:120]
                        fixed.append("invalidation")
                if "skip_reason" in m:
                    v = str(r.get("skip_reason") or "").strip()
                    if v:
                        s["skip_reason"] = v[:120]
                        fixed.append("skip_reason")
                if m and m[0].startswith("short_check"):
                    rsc = r.get("short_check") or {}
                    if isinstance(rsc, dict) and rsc:
                        try:
                            _dce = int(rsc.get("downside_catalyst_expiry_hours", 0) or 0)
                        except (TypeError, ValueError):
                            _dce = 0
                        try:
                            _sqz = int(rsc.get("squeeze_risk", 0) or 0)
                        except (TypeError, ValueError):
                            _sqz = 0
                        s["short_check"] = {
                            "borrow_feasible": bool(rsc.get("borrow_feasible", False)),
                            "downside_catalyst_expiry_hours": _dce,
                            "squeeze_risk": _sqz,
                            "provided": True,
                            "repaired": True,
                        }
                        fixed.append("short_check")
                # repair 若附带 direction 改动 → 拒收 (结构保障留痕)
                if "direction" in r and str(r.get("direction", "")).upper() != s.get("direction"):
                    stats["direction_patch_rejected"] += 1
                if fixed:
                    stats["repaired"] += 1
                    s["_contract_repaired"] = fixed
        else:
            print(f"  [contract] repair 调用失败 (不阻断): {getattr(_rep, 'error', '?') if _rep else 'no_result'}")
    except Exception as _ce:
        print(f"  [contract] repair 异常 (不阻断): {_ce}")

    # 复检: 修复后仍违约的计数; 强制模式才丢弃
    drop_idx = set()
    for i, s, _m in bad:
        if _missing(s):
            stats["still_bad"] += 1
            if stats["enforce"]:
                drop_idx.add(i)
                stats["dropped"] += 1
    if drop_idx:
        surfaced[:] = [s for i, s in enumerate(surfaced) if i not in drop_idx]
    return stats


def _annotate_news_channel(surfaced: List[Dict[str, Any]], news: List[Dict[str, Any]]) -> None:
    """S0 (2026-07-27): 机械归因浮现标的 → 首报新闻渠道 (record-only, 不影响筛选/排序).

    匹配口径: 新闻 symbols 标注含该标的 (P4a 匹配器产出), 或裸 ticker 词界出现在标题
    (大小写敏感, 与 P4a 同规则)。多条命中取 published 最早者 = 首报。
    落 s["info_channel"] (如 us_bloomberg / none_matched) + s["first_news_id"]。
    """
    import re as _re
    from email.utils import parsedate_to_datetime as _p2d
    if not surfaced:
        return
    prepared = []
    for n in news or []:
        syms = n.get("symbols") or "[]"
        if isinstance(syms, str):
            try:
                syms = json.loads(syms)
            except Exception:
                syms = []
        try:
            pub = _p2d(str(n.get("published_at") or ""))
        except Exception:
            pub = None
        prepared.append((set(str(x).replace("US.", "") for x in (syms or [])),
                         str(n.get("title") or ""), str(n.get("source") or "unknown"),
                         str(n.get("id") or ""), pub))
    # 歧义 ticker (F/T/V/MA...) 标题兜底误归因风险 (独立字母命中任意标题) — 与 P4a 同规:
    #   只信 symbols 标注 (P4a 产出已带歧义守卫), 不做标题裸词匹配。
    try:
        from app.data.rsshub_us_news import RSSHubUSNewsFetcher as _NF
        _ambiguous = _NF.AMBIGUOUS_TICKERS
    except Exception:
        _ambiguous = frozenset()
    for s in surfaced:
        sym = str(s.get("symbol", "")).replace("US.", "").upper()
        if not sym:
            continue
        hits = []
        pat = None if sym in _ambiguous else _re.compile(r"\b" + _re.escape(sym) + r"\b")
        for syms, title, src, nid, pub in prepared:
            if sym in syms or (pat is not None and pat.search(title)):
                hits.append((pub, src, nid))
        if hits:
            # 最早 published 优先; None 沉底 (统一 timestamp 化, 防 None/naive-aware 比较炸)
            hits.sort(key=lambda h: (h[0] is None, h[0].timestamp() if h[0] else 0))
            s["info_channel"] = hits[0][1]
            s["first_news_id"] = hits[0][2]
        else:
            s["info_channel"] = "none_matched"
            s["first_news_id"] = None


def _annotate_emergence_streak(surfaced: List[Dict[str, Any]]) -> None:
    """[shadow 2026-07-22] 浮现连续天数埋点 (record-only, 不影响任何筛选/排序).

    背景: 7/9-7/21 实证重开≥3次标的 +$202 vs 一次性标的 -$331 (含幸存者偏差),
    且 UE 存在同 thesis 惯性续期 (ORCL 连续 6 日空, 第 6 天 -$100 回吐)。
    本埋点给每个浮现标的标注 emergence_streak_days, 两周后判分
    "首日浮现 vs 续期第 N 天"绩效差。

    口径: 以 scan jsonl 历史中的 distinct scan 日期序列为日历 (scan 仅交易日跑,
    天然跳过周末/节假日), 同 symbol+direction 在紧邻的前序 scan 日持续出现则续期。
    首日浮现 streak=1; 中断后重新浮现从 1 重计。今日多次 scan 不重复累加。
    """
    try:
        history = [r for r in _read_jsonl(_SCAN_PATH)
                   if r.get("type") == "scan" and r.get("date") and r.get("date") < _today()]
        # 每个历史 scan 日 → 当日浮现过的 (symbol, direction) 集合 (同日多次 scan 合并)
        by_date: Dict[str, set] = {}
        for r in history:
            keys = by_date.setdefault(r["date"], set())
            for s in r.get("surfaced", []):
                if s.get("direction") in ("LONG", "SHORT"):
                    keys.add((s.get("symbol"), s.get("direction")))
        prior_dates = sorted(by_date.keys(), reverse=True)  # 紧邻在前
        for s in surfaced:
            streak = 1
            if s.get("direction") in ("LONG", "SHORT"):
                key = (s["symbol"], s["direction"])
                for d in prior_dates:
                    if key in by_date[d]:
                        streak += 1
                    else:
                        break
            s["emergence_streak_days"] = streak
    except Exception as e:  # 埋点失败不阻断 scan 主流程
        print(f"  [warn] emergence_streak 埋点失败 (继续): {e}")
        for s in surfaced:
            s.setdefault("emergence_streak_days", None)


def cmd_scan(manual_clue: str = "", midday: bool = False) -> int:
    if not _is_us_trading_day_today():
        print(f"[scan] {_today()} 非美股交易日 (周末/节假日), 跳过扫描")
        return 0
    # DEB-20260814 §十三: 午间影子扫描 (ET 12:30) — 只 scan 不 inject, 独立 jsonl。
    #   填补 09:10→次日早间的事件真空带。回滚: universe_expansion.midday_shadow_enabled=false
    #   或删 crontab 行。判据预注册于 DEB §13.3。
    if midday and not bool(_cfg("universe_expansion.midday_shadow_enabled", True)):
        print("[scan-midday] midday_shadow_enabled=false, 跳过")
        return 0
    brief = _build_clue_brief(manual_clue)
    print("=" * 64)
    print("clue brief (喂给 LLM):")
    print(brief[:1500])
    print("=" * 64)

    try:
        from app.llm.model_call import call_llm
    except Exception as e:
        print(f"[scan] 无法导入 call_llm: {e}")
        return 1

    # [ADR-016] live scan prompt 版本 config 驱动 (v1|v2); 默认 v1 安全回退,
    # 设 universe_expansion.prompt_version=v2 → 事件驱动+强制引证+Track A 夹逼 (更克制/更稳)。
    _pv = str(_cfg("universe_expansion.prompt_version", "v1")).strip().lower()
    _use_v2 = _pv in ("v2", "v2.0")
    _system = SYSTEM_PROMPT_V2 if _use_v2 else SYSTEM_PROMPT
    print(f"[scan] prompt_version={'v2' if _use_v2 else 'v1'} (config: universe_expansion.prompt_version)")
    res = call_llm(
        "universe_expansion_reasoning",
        brief,
        system=_system,
        temperature=0.2,
        # [DEB-20260829 第8轮 2026-08-30] 4000→8000: 主模型切 glm-5 配套 (glm 实测输出
        #   5448-6070 tok, 4000 下 finish_reason=length 截断已实弹验证).
        # [2026-09-01] 8000→16000: 当晚 glm-5.2 与 deepseek 双臂均 out=8000 恰触顶
        #   finish_reason=length → schema_failed → all_models_failed, scan exit 1 断
        #   && 链, inject/push 未跑 (llm_audit 实证; 输出需求内容驱动波动大 3008→>8000).
        #   16000 DashScope probe 实收; 满载 ~210-230s 仍在实体 timeout=300 内;
        #   截断降档底线 16000*0.5=8000 恰为旧上限.
        max_tokens=16000,
        schema_required={"surfaced": list},
        json_mode=True,
    )
    if not res or not res.success:
        err = getattr(res, "error", "unknown") if res else "no_result"
        print(f"[scan] LLM 调用失败 (不落库, 不下单): {err}")
        return 1

    parsed = res.response if isinstance(res.response, dict) else {}
    surfaced = _normalize_surfaced(parsed.get("surfaced"), with_tier=_use_v2)
    if not surfaced:
        print("[scan] LLM 未浮现有效标的 (schema 通过但 surfaced 空)")
        return 0

    # [U4 2026-08-25, DEB-ue-audit r7] per-item 输出契约校验 + 白名单修复。
    #   观测模式 (schema_enforce 缺省 False): 只记录违约与修复, 不丢标的, 零方向变更。
    #   判据: short_check provided 率与 skip_reason/invalidation 非空率升至 100% (repair 后),
    #   repair 未改变任何 direction (direction_patch_rejected 恒 0), 攒样 5 交易日后裁是否开强制。
    try:
        _contract_stats = _validate_and_repair_surfaced(surfaced)
        _nv = len(_contract_stats.get("violations", []))
        print(f"  [contract] 违约 {_nv} / 修复 {_contract_stats.get('repaired')} / "
              f"仍违约 {_contract_stats.get('still_bad')} / 方向补丁拒收 "
              f"{_contract_stats.get('direction_patch_rejected')}"
              + (f" / 丢弃 {_contract_stats.get('dropped')} (强制模式)" if _contract_stats.get("enforce") else " (观测模式)"))
    except Exception as _u4_e:
        print(f"  [warn] 契约校验异常 (不阻断): {_u4_e}")
        _contract_stats = {"error": str(_u4_e)}

    # 为可评分, 抓 scan 时参考价
    # 2026-08-11: effective 口径 — 盘前时段用 pre_price (last_price=昨收会污染 fwd 基线)
    router = _build_router()
    tradeable = [s for s in surfaced if s["direction"] in ("LONG", "SHORT")]
    q = _batch_quotes(router, [s["symbol"] for s in tradeable])
    for s in surfaced:
        px = _effective_quote_price(q.get(s["symbol"], {}))
        s["scan_price"] = round(px, 4) if px else None

    # [shadow] 浮现连续天数标注 (必须在 _append_jsonl 前: 只读历史日, 不含今日)
    _annotate_emergence_streak(surfaced)

    # S0 (2026-07-27, DEB-20260727 §六.3): 真实新闻渠道归因 — origin.info_source 此前
    #   恒为常量 LLM_EVENT_REASONING (管线标签), 渠道判分零区分度 ("常量退化"第三例)。
    #   机械归因: 标的出现在哪条新闻 (symbols 标注或标题词界) → 最早发布者=首报渠道。
    #   无匹配 → none_matched (市场佐证/纯推理浮现)。S1 切片两周后判 Q1。
    _annotate_news_channel(surfaced, _gather_news(48))

    # [U3 2026-08-25 结案删除, DEB-ue-audit r7] self-critique 影子调用已删除 (省 1 次 LLM/日)。
    #   预注册判据 (审计 §判据表): T+1 倒挂持续 — DOWN/DROP 命中 59.2% > KEEP 42.9% (n=103≥30);
    #   实际执行账本 DROP 桶 +$85 / DOWNGRADE 桶 −$286 (verdict 与盈亏无指导性);
    #   T+0/T+1 口径互斥且置换检验均不显著 (p=0.82/0.12)。
    #   同步动作: skill DA feed DROP filter 移除 + G1-3 门退役 (双端一致)。
    #   恢复: git show 2fd79ab3:scripts/llm_universe_scan.py (v2.2 块)。

    # R5 (2026-08-02): UE 候选级快照接线 — 此前 signal_snapshots 仅 DA (UE 覆盖 0%).
    #   全量 tradeable (含 critique DROP / 未过注入闸) 落快照, "质量分→结局"攒样不需成交;
    #   fwd 回填链 run_signal_ic_backfill 按 trade_date 自动接. decision_id 与注入指令同键
    #   (universe_expansion:{date}:{symbol}), UPSERT 幂等. 失败静默不阻断扫描.
    #   §十三: 午间模式跳过 — decision_id 键与早间 scan/inject 相同, UPSERT 会覆盖
    #   早间快照 (scan_price/factors 被午间值污染)。午间样本由独立 jsonl + 评估脚本自评分。
    if not midday:
        try:
            from app.analysis.signal_ic_tracker import record_signal_snapshot as _rec_snap
            _snap_n = 0
            for s in tradeable:
                try:
                    _rec_snap(
                        decision_id=f"{STRATEGY_ID}:{_today()}:{s['symbol']}",
                        symbol=f"US.{s['symbol']}",
                        direction=s["direction"],
                        strength=int(round(float(s.get("confidence") or 0) * 100)),
                        confidence=float(s.get("confidence") or 0),
                        strategy_id=STRATEGY_ID,
                        trade_date=_today(),
                        factors={
                            "rubric_score": s.get("rubric_score"),
                            "evidence_checklist": s.get("evidence_checklist"),
                            "self_tier": s.get("self_tier"),
                            "verdict": (s.get("critique") or {}).get("verdict"),
                            "info_channel": s.get("info_channel"),
                            "scan_price": s.get("scan_price"),
                        },
                    )
                    _snap_n += 1
                except Exception as _snap_e:
                    # [审计 2026-08-30] 原 except:pass 静默吞 — 快照缺失对
                    # signal_ic_tracker 是该 decision_id 的盲区, 不可静默。
                    print(f"  [warn] UE 快照单标的失败 {s.get('symbol')}: {_snap_e}")
            print(f"  [R5] UE 候选快照 {_snap_n}/{len(tradeable)} → signal_snapshots")
        except Exception as _snap_e:
            print(f"  [warn] UE 候选快照失败 (不影响扫描): {_snap_e}")

    rec = {
        "type": "scan",
        "date": _today(),
        "scanned_at": _now_et().isoformat(),
        "scan_window": "midday" if midday else "premarket",  # §十三: 评估脚本按窗口切片
        "source_type": SOURCE_TYPE,
        "strategy_id": STRATEGY_ID,
        "discovery_source": DISCOVERY_SOURCE,
        "info_source": INFO_SOURCE,
        "model_used": getattr(res, "model_used", None),
        "prompt_version": PROMPT_VERSION if _use_v2 else "v1",
        # [U1] 内容指纹 — 版本戳失真防护 (旧记录无此字段, 按日期切分)
        "prompt_hash": PROMPT_HASH_V2 if _use_v2 else None,
        # [U4] 契约校验统计 — 攒样判据数据源 (违约率/修复率/方向补丁拒收)
        "contract_stats": _contract_stats,
        "surfaced": surfaced,
        "note": ("record-only midday shadow; 不进 inject/DA供料/push, 判据见 DEB §13.3"
                 if midday else "record-only; 不驱动 live, inject 另需 entry_enabled=true"),
    }
    _append_jsonl(_MIDDAY_PATH if midday else _SCAN_PATH, rec)

    print(f"[scan] {_today()} 模型={getattr(res,'model_used','?')} 浮现 {len(surfaced)} 标的:")
    # [2026-07-22] tier 分布计数: 解除数量压制后监控 THESIS_ONLY 是否洪泛 (配合输入源健康行)
    _tc = {"HARD_EVENT": 0, "MARKET_CORROBORATED": 0, "THESIS_ONLY": 0}
    for s in surfaced:
        _t = str(s.get("self_tier", "")).upper()
        if _t in _tc:
            _tc[_t] += 1
    print(f"  [tier分布] HARD {_tc['HARD_EVENT']} / CORR {_tc['MARKET_CORROBORATED']} / "
          f"THESIS {_tc['THESIS_ONLY']}" + (" ⚠️THESIS占多数" if _tc['THESIS_ONLY'] > len(surfaced) / 2 else ""))
    for s in surfaced:
        tag = "🌍外" if s["is_universe_external"] else "  "
        _stk = s.get("emergence_streak_days")
        _stk_tag = f" 续期D{_stk}" if _stk and _stk > 1 else ""
        print(f"  {s['direction']:5} {s['symbol']:6} conf={s['confidence']:.2f} {tag} "
              f"{s['winner_loser']:6} | {s['causal_chain'][:70]}{_stk_tag}")
    return 0


# ── inject: 代码出数值 → 写作战计划 (flag 闸门) ─────────────────
def _compute_numbers(entry_price: float, direction: str) -> Dict[str, Any]:
    """代码出数值 (Track A): entry_zone via entry_band, SL/TP 锚定 entry_zone 边界外.

    SL/TP 从 entry_zone 的 low/high 外扩 (而非 entry_price 中价), 保证:
      - LONG : SL 严格低于 zone low, TP 严格高于 zone high
      - SHORT: SL 严格高于 zone high, TP 严格低于 zone low
    避免 SL 落在入场区内 (KNIFE_FLOOR_PCT 与 default_stop_pct 同为 3% 时会重合).
    """
    from app.playbook.entry_band import compute_entry_band
    is_short = (direction == "SHORT")
    low, high = compute_entry_band(entry_price, is_short)
    if not (low and high):
        return {"entry_zone": [], "stop_loss": 0.0, "take_profit": 0.0}
    stop_pct = float(_cfg("universe_expansion.default_stop_pct", 3.0)) / 100.0
    tp_pct = float(_cfg("universe_expansion.default_tp_pct", 6.0)) / 100.0
    if is_short:
        stop_loss = high * (1 + stop_pct)   # SL 高于 zone high
        take_profit = low * (1 - tp_pct)    # TP 低于 zone low
    else:
        stop_loss = low * (1 - stop_pct)    # SL 低于 zone low
        take_profit = high * (1 + tp_pct)   # TP 高于 zone high
    return {
        "entry_zone": [low, high],
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
    }


def _position_symbols() -> set:
    """现有持仓标的 (从 daily_active_pool 的 POSITION 来源)."""
    try:
        from app.utils.daily_active_pool import DailyActivePool, SOURCE_POSITION
        p = DailyActivePool()
        p.load()
        return {e.code.replace("US.", "").upper()
                for e in p.get_entries() if e.source == SOURCE_POSITION}
    except Exception as e:
        # [2026-08-26 UE审计随批] 静默返回空集=持仓排重静默失效 (可能对已持仓标的
        #   重复注入); 执行层有方向感知冲突检查兜底但不应无声。禁静默与冷却门同纪律。
        print(f"  [warn] 持仓读取失败, 持仓排重失效 (继续): {e}")
        return set()


def cmd_inject(force_dry: bool = False) -> int:
    if not _is_us_trading_day_today():
        print(f"[inject] {_today()} 非美股交易日 (周末/节假日), 跳过注入")
        return 0
    enabled = bool(_cfg("universe_expansion.entry_enabled", False))
    dry = force_dry or not enabled
    min_conf = float(_cfg("universe_expansion.min_confidence", 0.5))
    max_n = int(_cfg("universe_expansion.max_targets_per_day", 30))
    # [DEB-20260830第13轮·审计默认值对齐] 回退默认值对齐 yaml 真值 (0.70→0.5, 5→30):
    # 活性值经实弹探针确认=0.5/30 (yaml SSOT), 本改动零活性行为变更,
    # 仅消除 config 容器降级路径下阈值静默漂移 (浮现标的被比预期多丢/少放)。

    scans = [r for r in _read_jsonl(_SCAN_PATH) if r.get("type") == "scan"]
    today_scans = [r for r in scans if r.get("date") == _today()]
    if not today_scans:
        print(f"[inject] 今日({_today()})无 scan 结果, 先跑 scan")
        return 0
    surfaced = today_scans[-1].get("surfaced", [])
    _scan_model = today_scans[-1].get("model_used")  # 2026-08-03 任务#3: 记录级模型归因 → 指令列

    today = _today()
    positions = _position_symbols()

    # 排重: 今日已在 playbook_instructions 的标的 (DA 优先, universe_expansion 让路).
    #   save_instruction 按 (date,symbol) UPSERT — 同标的会覆盖 DA, 故此处排除.
    # [ADR-019] 只对"实际占位"指令让路: 跳过 direction=SKIP 的被拒候选行.
    #   根因: get_instructions_by_date 不过滤 SKIP, 而 SKIP 是 DB 最大类 (163 vs BUY 112),
    #   若一律纳入排重 → DA/投票拒绝(SKIP)的标的会连带封杀 universe 浮现同一标的.
    existing_pb = set()
    _today_pb: List[Dict[str, Any]] = []  # [DA-C1] 当日全量指令行 (含SKIP), 供方向结构测量
    try:
        from app.playbook.db import PlaybookDB
        _today_pb = PlaybookDB().get_instructions_by_date(today) or []
        for _inst in _today_pb:
            if str(_inst.get("direction", "")).upper() == "SKIP":
                continue
            _s = str(_inst.get("symbol", "")).replace("US.", "").upper()
            if _s:
                existing_pb.add(_s)
    except Exception as e:
        print(f"  [warn] 读取 playbook_instructions 排重失败 (继续): {e}")

    # 候选筛选: 可交易方向 + 置信度达标 + 非持仓 + 非DA已写
    # [U7 2026-08-25, DEB-ue-audit r5 / REVIEW-20260824 裁决GO] streak 硬闸:
    #   续期≥max 的浮现不再注入 (8/12-8/24 OOS: streak≥3 桶方向性前瞻均值 −0.28%≤0,
    #   预注册判据机械执行). 默认99=缺键零行为(防配置未读到静默启闸), 显式配2才起闸.
    #   回滚: yaml max_emergence_streak 改 99 或删键。
    _max_streak = int(_cfg("universe_expansion.max_emergence_streak", 99) or 99)
    cand = [s for s in surfaced
            if s["direction"] in ("LONG", "SHORT")
            and s["confidence"] >= min_conf
            and s["symbol"] not in positions
            and s["symbol"] not in existing_pb
            and _streak_passes(s, _max_streak)]
    cand.sort(key=lambda s: s["confidence"], reverse=True)
    cand = cand[:max_n]

    # [UE-B2 2026-08-29] 拒绝归因落盘 — 筛选链每个落选候选一行结构化记录 (纯观测)。
    _log_inject_reject(surfaced, min_conf, positions, existing_pb, _max_streak, max_n)
    # [DA-C1 / UE-C2 2026-08-30] 方向结构 + streak 反事实测量 (纯观测零行为)。
    _log_direction_structure(surfaced, _today_pb, min_conf, _max_streak)
    _log_streak_counterfactual(surfaced, min_conf, positions, existing_pb, _max_streak)

    if not cand:
        print(f"[inject] 无合格候选 (min_conf={min_conf}, 已排除持仓/DA已写)")
        return 0

    router = _build_router()
    q = _batch_quotes(router, [s["symbol"] for s in cand])
    # 2026-08-03 修复 (regime 精准性验证): 直写指令缺 market_regime/vix/spy_trend 三字段
    #   → 7/9-7/23 指令级 regime 全 NULL, 归因/复盘无法按 regime 切片 (与 DA bridge 同口径).
    #   复用 MarketRegimeGate (与 run_playbook_generate / DA bridge 同源); assess() 内部
    #   已有数据源降级链 (VIX uncertain → 保守降级), 不会抛异常, best-effort 不阻塞注入.
    _regime_fields = {}
    try:
        from app.playbook.market_regime import MarketRegimeGate as _MRG_ue
        _rc_ue = _MRG_ue().assess()
        _regime_fields = {
            "market_regime": _rc_ue.regime.value,
            "vix_at_generation": _rc_ue.vix,
            "spy_trend_at_generation": "above_ma20" if _rc_ue.spy_ma20 >= _rc_ue.spy_ma50 else "below_ma20",
        }
    except Exception as _rg_ue_e:
        print(f"  [warn] regime 补齐失败(非致命): {_rg_ue_e}")
    instructions = []
    for s in cand:
        # 2026-08-11: effective 口径 — 盘前时段用 pre_price 锚定 entry/SL/TP/band。
        #   修复前 last_price=昨收 → 锚错 (8/11 JD 实盘锚 33.47 vs 真实盘前 32.21, 错 3.8%)。
        px = _effective_quote_price(q.get(s["symbol"], {})) or s.get("scan_price")
        if not px:
            print(f"  [skip] {s['symbol']} 无现价")
            _log_inject_loop_reject(s, "no_quote(无现价)")
            continue
        # 2026-08-13: 价格地板 — OTC仙股源头拦截 (AETN $0.13案例: Futu无OTC行情,
        #   执行端永远无法re-price=死指令+轮询噪音). 与DA扫描器$5闸+admission.min_entry_price同源.
        #   落SKIP行保留可归因 (与admission REJECT同轨, 不丢样本). 回滚: min_price_floor=0.
        _floor = float(_cfg("universe_expansion.min_price_floor", 0) or 0)
        if _floor > 0 and float(px) < _floor:
            instructions.append({
                "date": today,
                "symbol": f"US.{s['symbol']}",
                "direction": "SKIP",
                "skip_reason": f"price_floor({float(px):.2f}<{_floor:g}, OTC/仙股无富途行情)",
                "entry_price": round(float(px), 4),
                "confidence": min(int(round(float(s["confidence"]) * 100)), 50),
                "thesis": s.get("thesis", ""),
                "decision_id": f"{STRATEGY_ID}:{today}:{s['symbol']}",
                "model_used": _scan_model,
            })
            print(f"  [skip] {s['symbol']} 价格地板 ({float(px):.2f} < {_floor:g}) → SKIP行")
            continue
        nums = _compute_numbers(float(px), s["direction"])
        if not nums["entry_zone"]:
            print(f"  [skip] {s['symbol']} entry_zone 无效")
            _log_inject_loop_reject(s, "entry_zone_invalid")
            continue
        # entry_zone 即 compute_entry_band 输出 (执行端同源, band 一致)
        entry_low, entry_high = nums["entry_zone"][0], nums["entry_zone"][1]
        # 方向映射到 playbook 词汇 (以 DA 实际写入值为准: pipeline.py L736)
        pb_direction = "BUY" if s["direction"] == "LONG" else "SELL_SHORT"
        # 2026-07-16 P1 修复: universe_expansion confidence 上限 50.
        #   根因: LLM 直接输出 confidence=0.85-0.92, 高于 DA 2模型一致的 0.70.
        #   但 universe_expansion 的分析深度 (1句thesis, 无conditions, 无invalidation)
        #   远不如 DA (200字thesis + 3 conditions + invalidation).
        #   高 confidence → auto_execute 排序优先 → 抢占 DA 信号的预算分配.
        #   修复: 上限 50 (低于 DA 一致70, 高于 DA 分歧35), 反映"有方向但无深度分析".
        #   [2026-07-25 勘误留记] 曾试 cap→30 降 UE 排序优先级, 后发现排序主键是
        #   pipeline.py::_src_priority (ADR-020 用户指定 UE 最优先), conf 仅同源内次键 —
        #   改 cap 对跨源排序无效且污染 scorecard conf 分桶, 已回滚。资源错配议题见 handoff。
        _raw_conf = int(round(float(s["confidence"]) * 100))  # schema INTEGER 0-100
        _UE_CONF_CAP = 50
        _capped_conf = min(_raw_conf, _UE_CONF_CAP)
        instructions.append({
            "date": today,
            "symbol": f"US.{s['symbol']}",
            "direction": pb_direction,
            "entry_price": round(float(px), 4),  # 触发基准 (_check_intraday_trigger 用)
            "entry_low": entry_low,
            "entry_high": entry_high,
            # anchor_dev_pct (DEB-feedback-loop F1, 2026-07-31): UE 锚时滞 — inject 现价
            #   相对 scan 浮现价的带符号漂移 (%). entry==px 构造上 dev≈0, 有意义的同尺锚
            #   是 scan→inject 漂移; 与 DA (LLM entry vs 分析时刻市价) 一把尺裁决入场区.
            "anchor_dev_pct": (
                round((float(px) - float(s["scan_price"])) / float(s["scan_price"]) * 100, 3)
                if (s.get("scan_price") and px) else None
            ),
            "stop_loss": nums["stop_loss"],
            "take_profit": nums["take_profit"],
            "confidence": _capped_conf,
            "thesis": s.get("thesis", ""),
            # [DEB-20260825-ue-audit U2] invalidation 顶层透传 — 此前只进 origin blob
            #   (invalidation_ue), save_instruction 只认顶层键 (db.py) → 列恒空,
            #   tripwire_checker 文本轨对 UE 指令 100% 双盲。接上后 UE 即刻进入
            #   盘中失效核销 (宁漏不误杀: checker 自带解析守卫, 非数字子句不命中)。
            #   诚实预期: 现有 invalidation 多为自然语言 (非数字), 首期命中率≈0,
            #   价值=审计覆盖; 30 天判据不达标再提案结构化 (见 deb §预注册)。
            "invalidation": s.get("invalidation", "") or "",
            "sector": s.get("sector", ""),
            "horizon": "short",
            # 2026-08-03: regime 三字段 (与 DA bridge 同口径, 修复 7/9-7/23 直写缺失)
            "market_regime": _regime_fields.get("market_regime"),
            "vix_at_generation": _regime_fields.get("vix_at_generation"),
            "spy_trend_at_generation": _regime_fields.get("spy_trend_at_generation"),
            # 归因 (轻量, 复用现有字段, 无需 schema 迁移):
            "decision_id": f"{STRATEGY_ID}:{today}:{s['symbol']}",
            "model_used": _scan_model,  # 2026-08-03 任务#3: UE 指令模型归因 (scan 记录级)
            "raw_instruction": {
                "source": STRATEGY_ID,  # universe_expansion — 报表侧切分 DA vs 本源
                "prompt_version": PROMPT_VERSION,
                # [U1] 内容指纹随指令落库 (raw_instruction_json 内)
                "prompt_hash": PROMPT_HASH_V2,
                # [U10 2026-08-25 建议④] 结构化失效触发器 → tripwire_checker 结构化轨
                #   (与 DA r15 同契约同消费方; 消费端 _get_structured_triggers 再验一次白名单)
                "invalidation_triggers": s.get("invalidation_triggers") or [],
                "origin": {
                    "info_source": INFO_SOURCE,
                    # S0 (2026-07-27): 真实首报渠道 + 新闻 id (机械归因, 见 _annotate_news_channel)
                    "info_channel": s.get("info_channel") or None,
                    "first_news_id": s.get("first_news_id") or None,
                    "causal_chain": s.get("causal_chain", ""),
                    "winner_loser": s.get("winner_loser", ""),
                    "corroborated_by_market": s.get("corroborated_by_market"),
                    # [shadow] 浮现连续天数透传 → 交易级判分可直接 join (首日=1)
                    "emergence_streak_days": s.get("emergence_streak_days"),
                    # [2026-07-22] 解除数量压制后按证据层级分层判分的前提: tier/证据透传落库
                    "self_tier": s.get("self_tier") or None,
                    "evidence_ref": (s.get("evidence_ref") or "")[:300] or None,
                    # [2026-07-25 信息源复盘] cap 前原始 conf 落库 — 否则 07-16 cap=50 把 LLM
                    #   置信分层全抹掉 (库内 UE conf 恒=50, 归因无信息量)。排序仍用 capped。
                    "raw_confidence": _raw_conf,
                    # [v2.2 2026-07-25, P1/P2] 可证伪字段 + rubric 落库 (纯归因, 不进执行)
                    "invalidation_ue": s.get("invalidation") or None,
                    "horizon_hours": s.get("horizon_hours") or None,
                    "expected_move_pct": s.get("expected_move_pct") or None,
                    "rubric_score": s.get("rubric_score"),
                    "evidence_checklist": s.get("evidence_checklist") or None,
                    # [v2.2 2026-07-25, P5/A2] SHORT 三问 + self-critique shadow 落库 (纯归因)
                    "short_check": s.get("short_check") or None,
                    "critique": s.get("critique") or None,
                },
            },
        })

    print(f"[inject] entry_enabled={enabled} → {'DRY-RUN (不写库)' if dry else '写入 playbook_instructions'}")
    for it in instructions:
        # 2026-08-24: 防御性 .get — 价格地板 SKIP 行无 entry_low/SL/TP 键,
        #   裸键 KeyError 在打印循环炸掉整个 inject (连正常指令都没写库).
        #   与 DA pipeline Stage4 同款失效类. 8/24 实弹: USBC $0.41 SKIP 行引爆.
        if it.get("direction") == "SKIP":
            print(f"  {'SKIP':10} {it['symbol']:8} entry={it['entry_price']} "
                  f"skip={it.get('skip_reason','')} conf={it['confidence']} src={STRATEGY_ID}")
        else:
            print(f"  {it['direction']:10} {it['symbol']:8} entry={it['entry_price']} "
                  f"zone=[{it['entry_low']},{it['entry_high']}] SL={it['stop_loss']} TP={it['take_profit']} "
                  f"conf={it['confidence']} src={STRATEGY_ID}")

    if dry:
        if not enabled:
            print("  [安全阀] universe_expansion.entry_enabled=false → 未写库, 未下单。")
        return 0

    if not instructions:
        return 0

    # 写入 playbook_instructions — 与 DA 共用同一执行/触发/退出轨道.
    #   auto_execute 会对未触发指令做 _check_intraday_trigger (symbol-agnostic 取价),
    #   宇宙外标的无需订阅/轮询注册即可被执行.
    from app.playbook.db import PlaybookDB
    db = PlaybookDB()
    db.ensure_tables()
    saved = 0
    # S0 影子开关 (DEB-20260805-supply 终裁②, 2026-08-06 用户裁"同意"): 只读一次.
    try:
        from app.core.config_container import get_config as _gc_sb
        _sb_on = bool(_gc_sb().get_dotted("universe_expansion.short_boost_shadow", False))
    except Exception:
        _sb_on = False
    for it in instructions:
        try:
            # [DEB-fullchain P1-14 2026-08-16] DA 优先防覆盖: 旧版 save_instruction 的
            #   UPSERT 按 (date,symbol) 无条件整行覆盖 — UE cron 复跑会把 DA 同日更深
            #   指令 (200字thesis+conditions+invalidation) 覆盖成 UE 浅指令 (1句thesis)。
            #   写前查已有行: decision_id 非空且非本源前缀 (DA-/DEC-) → 跳过不覆盖。
            #   UE 覆盖 UE 复跑仍允许 (自更新); 空 decision_id 旧行维持原覆盖行为。
            #   RFC-066 (2026-08-25): 多行台账 — 需扫描该 (date, symbol) 的**全部轮次行**,
            #   任一行是 DA/DEC 优先级即跳过 (旧版只看单行)。
            _all_rows = [r for r in db.get_instructions_by_date(it.get("date"))
                         if r.get("symbol") == it.get("symbol")]
            _da_hit = next((r for r in _all_rows
                            if _is_da_priority_skip(r.get("decision_id"))), None)
            if _da_hit:
                print(f"  [skip] {it.get('symbol')} 今日已有非UE指令 "
                      f"(decision_id={_da_hit.get('decision_id')}) → DA优先不覆盖")
                _log_inject_save_reject(
                    it, f"da_priority_skip({_da_hit.get('decision_id', '')})")
                continue
            # ── Phase 0 (2026-08-23): 同标的同方向 3 交易日冷却 ──
            # LLM 对同一新闻可连续多天产出同标的同方向指令 (ORCL 连续 6 天空, 累计回吐
            # -$100)。前笔亏了它不知道 (绩效反馈已加入 prompt, 但冷却作硬兜底)。
            # 查 trade_records 近 N 天同标的同方向的已执行/已平仓记录, 有则跳过。
            # 回滚: universe_expansion.same_symbol_cooldown_days 改 0。
            try:
                from app.core.config_container import get_config as _gc_cool
                _cool_days = int(_gc_cool().get_dotted(
                    "universe_expansion.same_symbol_cooldown_days", 3))
            except Exception:
                _cool_days = 3
            if _cool_days > 0:
                try:
                    import sqlite3 as _cdb
                    from app.core.db_utils import get_main_db as _gmdb
                    from app.utils.direction_utils import to_trade_direction
                    from datetime import datetime as _cdt, timedelta as _ctd
                    # [Hermes 复核 2026-08-23] 词表修正: 指令侧词汇是 BUY/SELL_SHORT
                    # (pb_direction, :1235), trade_records.direction 只存 LONG/SHORT —
                    # 原查询用指令词汇恒 0 命中, 冷却门是死门 (DB 实弹:
                    # direction='BUY' 全表 0 行)。
                    # [Qoder 源头修复 2026-08-23] 映射改走 app/utils/direction_utils
                    # 单一真源 (该 dict 此前在 5+ 站点就地重写, 缺真源正是本 bug 的根);
                    # 方向不可识别时必须告警 —— 静默 no-op 就是死门本身。
                    _cool_dir = to_trade_direction(it.get("direction"))
                    _cutoff_d = (_cdt.now() - _ctd(days=_cool_days)).strftime("%Y-%m-%d")
                    if not _cool_dir:
                        print(f"  [warn] cooldown 未生效: {it.get('symbol')} 方向 "
                              f"{it.get('direction')!r} 无法映射到成交词表")
                    else:
                        _cc = _cdb.connect(str(_gmdb()), timeout=3)
                        _cr = _cc.execute(
                            "SELECT 1 FROM trade_records WHERE symbol=? AND direction=? "
                            "AND trade_date>=? AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                            "LIMIT 1",
                            (it.get("symbol"), _cool_dir, _cutoff_d)
                        ).fetchone()
                        _cc.close()
                        if _cr:
                            print(f"  [cooldown] {it.get('symbol')} {it.get('direction')} "
                                  f"近{_cool_days}日已有同方向执行记录 → 冷却跳过")
                            _log_inject_save_reject(
                                it, f"cooldown({_cool_days}d同方向已执行)")
                            continue
                except Exception as _cool_err:
                    # 冷却查询失败不阻断主流程, 但禁静默 (死门教训: 静默吞 = 门死了没人知道)
                    print(f"  [warn] cooldown 查询失败 ({it.get('symbol')}): {_cool_err}")
            db.save_instruction(it)
            saved += 1
            # S0 影子: UE×SHORT 加码 record-only — 登记"若 ×1.25 加码"假设队列成员,
            #   T+1 与未加码同池对照; 主门 n=60 单侧 α=0.025 δ=2pp, 敏感性 Bonferroni n=85,
            #   毕业前不动真仓 (停止规则见 DEB §四.3). 回滚: 开关置 false.
            if _sb_on and it.get("direction") == "SELL_SHORT":
                try:
                    import json as _sbj
                    from pathlib import Path as _sbp
                    _sb_row = {
                        "date": it["date"], "symbol": it["symbol"],
                        "direction": it["direction"],
                        "decision_id": it.get("decision_id"),
                        "entry_price": it.get("entry_price"),
                        "stop_loss": it.get("stop_loss"),
                        "take_profit": it.get("take_profit"),
                        "confidence": it.get("confidence"),
                        "hypo_boost": 1.25,
                        "rule": "ue_short_boost_s0_shadow",
                    }
                    with _sbp("data/ue_short_boost_shadow.jsonl").open("a", encoding="utf-8") as _sbf:
                        _sbf.write(_sbj.dumps(_sb_row, ensure_ascii=False) + "\n")
                    print(f"  [S0影子] {it['symbol']} SELL_SHORT 加码影子已登记 (×1.25 假设)")
                except Exception as _sbe:
                    print(f"  [warn] S0 影子落盘失败(非致命): {_sbe}")
        except Exception as e:
            print(f"  [warn] {it['symbol']} 写库失败: {e}")

    print(f"[inject] 已写入 {saved} 条 playbook_instructions (source={STRATEGY_ID}, 走 DA 统一执行轨道)")
    return 0


# ── score / report ───────────────────────────────────────────
def cmd_score() -> int:
    records = _read_jsonl(_SCAN_PATH)
    scans = [r for r in records if r.get("type") == "scan"]
    scored = {r.get("scan_date") for r in records if r.get("type") == "score"}
    todo = [c for c in scans if c.get("date") not in scored and c.get("surfaced")]
    if not todo:
        print("[score] 无待评分 scan")
        return 0
    router = _build_router()
    for c in todo:
        tradeable = [s for s in c["surfaced"] if s["direction"] in ("LONG", "SHORT") and s.get("scan_price")]
        q = _batch_quotes(router, [s["symbol"] for s in tradeable])
        results, fwd_list, hits = [], [], 0
        for s in tradeable:
            cur = _quote_price(q.get(s["symbol"], {}))
            base = s.get("scan_price")
            if not cur or not base or base <= 0:
                continue
            raw = (cur - base) / base * 100
            fwd = round(raw if s["direction"] == "LONG" else -raw, 2)
            hit = fwd >= HIT_PCT
            hits += 1 if hit else 0
            fwd_list.append(fwd)
            results.append({"symbol": s["symbol"], "direction": s["direction"],
                            "scan_price": base, "scored_price": round(cur, 4),
                            "fwd_pct": fwd, "hit": hit, "confidence": s["confidence"],
                            # [P0.1-b 2026-07-23] 分层标签随行落库: tier 排序验证 + streak 判分的原料
                            "self_tier": s.get("self_tier"),
                            "emergence_streak_days": s.get("emergence_streak_days"),
                            "evidence_ref": (s.get("evidence_ref") or "")[:80] or None,
                            "strategy_id": STRATEGY_ID})
        n = len(results)
        summary = {"n": n, "hit_rate": round(hits / n, 3) if n else None,
                   "avg_fwd_pct": round(sum(fwd_list) / len(fwd_list), 2) if fwd_list else None}
        _append_jsonl(_SCAN_PATH, {"type": "score", "scan_date": c.get("date"),
                                   "scored_at": _now_et().isoformat(),
                                   "results": results, "summary": summary})
        print(f"[score] scan={c.get('date')} 评分{n} 命中率={summary['hit_rate']} "
              f"平均前瞻={summary['avg_fwd_pct']}%")
    return 0


def cmd_report(days: int = 30) -> int:
    records = _read_jsonl(_SCAN_PATH)
    if not records:
        print(f"影子文件为空: {_SCAN_PATH}")
        return 0
    cutoff = (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    scans = [r for r in records if r.get("type") == "scan" and r.get("date", "") >= cutoff]
    scores = [r for r in records if r.get("type") == "score" and r.get("scan_date", "") >= cutoff]
    total = sum(len(c.get("surfaced", [])) for c in scans)
    uniq = {s["symbol"] for c in scans for s in c.get("surfaced", [])}
    fwd = [x["fwd_pct"] for r in scores for x in r.get("results", [])]
    hit = [x["hit"] for r in scores for x in r.get("results", [])]

    print("=" * 64)
    print(f"📡 LLM 事件推理·选股宇宙扩张 效果 (近 {days} 天)   {_SCAN_PATH.name}")
    print(f"归因: source_type={SOURCE_TYPE} | strategy_id={STRATEGY_ID} | discovery_source={DISCOVERY_SOURCE}")
    print("=" * 64)
    print(f"scan 次数       : {len(scans)}")
    print(f"浮现标的         : {total} 人次 / {len(uniq)} 只去重")
    if hit:
        print(f"命中率(前瞻≥{HIT_PCT}%): {round(sum(hit)/len(hit),3)} ({sum(hit)}/{len(hit)})")
        print(f"平均前瞻收益     : {round(sum(fwd)/len(fwd),2)}%")
    else:
        print("已评分           : 0 (待 score)")

    # [P0.1-b 2026-07-23] 分层切片: self_tier 排序验证 (P1.3 判据) + streak 判分
    _rows = [x for r in scores for x in r.get("results", [])]

    def _slice_print(title: str, key_fn, order=None):
        buckets: Dict[str, List[float]] = {}
        hits_m: Dict[str, int] = {}
        for x in _rows:
            k = key_fn(x)
            if k is None:
                continue
            buckets.setdefault(k, []).append(x["fwd_pct"])
            hits_m[k] = hits_m.get(k, 0) + (1 if x.get("hit") else 0)
        if not buckets:
            print(f"{title}: 无带标签评分数据 (score 需含分层字段)")
            return
        print(title)
        keys = order or sorted(buckets)
        for k in keys:
            if k not in buckets:
                continue
            v = buckets[k]
            print(f"    {k:24s} n={len(v):3d} 命中率={round(hits_m[k]/len(v),3):.3f}"
                  f" 平均前瞻={sum(v)/len(v):+.2f}%")

    _slice_print("按 self_tier 切片 (预期 HARD_EVENT > MARKET_CORROBORATED > THESIS_ONLY):",
                 lambda x: x.get("self_tier"),
                 order=["HARD_EVENT", "MARKET_CORROBORATED", "THESIS_ONLY"])
    _slice_print("按浮现连续天数切片 (07-22 埋点判分):",
                 lambda x: ("首日" if (x.get("emergence_streak_days") or 0) <= 1
                            else ("2日续期" if x["emergence_streak_days"] == 2 else "≥3日续期"))
                 if x.get("emergence_streak_days") is not None else None,
                 order=["首日", "2日续期", "≥3日续期"])
    print("-" * 64)
    sc_by_date = {s["scan_date"]: s for s in scores}
    for c in sorted(scans, key=lambda x: x.get("date", "")):
        d = c.get("date")
        surf = c.get("surfaced", [])
        line = f"{d}  模型={c.get('model_used','?')}  浮现{len(surf)}只"
        sc = sc_by_date.get(d)
        if sc and sc.get("summary", {}).get("n"):
            sm = sc["summary"]
            line += f"  → 命中率{sm['hit_rate']} 平均{sm['avg_fwd_pct']:+}%"
        print(line)
        for s in surf[:8]:
            tag = ""
            if sc:
                m = next((x for x in sc.get("results", []) if x["symbol"] == s["symbol"]), None)
                if m:
                    tag = f"  → 前瞻{m['fwd_pct']:+.2f}% {'✅' if m['hit'] else '❌'}"
            ext = "🌍" if s.get("is_universe_external") else " "
            print(f"    {s['direction']:5} {s['symbol']:6} {ext} conf={s['confidence']:.2f}"
                  f"{tag}  | {s['causal_chain'][:50]}")
    print("=" * 64)
    return 0


# ── scan-eval: V1/V2 prompt A/B 对比 (shadow, 不落主scan/不下单) ──
_PROMPT_EVAL_PATH = _ROOT / "data" / "shadow" / "prompt_eval.jsonl"


def cmd_scan_eval(manual_clue: str = "") -> int:
    """同一 clue brief 分别喂 V1(现状)/V2(P0 意图对齐+引证自评), 并排对比。
    纯 shadow: 不落主 scan、不 inject、不下单。唯一变量=SYSTEM_PROMPT。"""
    brief = _build_clue_brief(manual_clue)
    print("=" * 72)
    print(f"scan-eval clue brief ({'有人工线索' if manual_clue else '无clue,自动新闻'}):")
    print(brief[:1200])
    print("=" * 72)

    try:
        from app.llm.model_call import call_llm
    except Exception as e:
        print(f"[scan-eval] 无法导入 call_llm: {e}")
        return 1

    def _run(system: str, tag: str, with_tier: bool):
        res = call_llm("universe_expansion_reasoning", brief, system=system,
                       temperature=0.2, max_tokens=16000,
                       schema_required={"surfaced": list}, json_mode=True)
        # [DEB-20260829 第10轮 9.4 修复] 2500→8000: glm-5.2 实测输出 5448-6070 tok。
        # 原 2500 下自救降档到 max(1024, 2500*0.5)=1250 只会更糟 (A/B 仪器在 glm 下实质死亡);
        # [2026-09-01] 8000→16000: 与主调用点同晚双臂触顶事故同步 (同 brief 同输出形态
        # 共死亡模式), 降档底线 16000*0.5=8000=旧上限。
        if not res or not res.success:
            print(f"[{tag}] LLM 调用失败: {getattr(res, 'error', '?') if res else 'no_result'}")
            return [], None
        parsed = res.response if isinstance(res.response, dict) else {}
        return _normalize_surfaced(parsed.get("surfaced"), with_tier=with_tier), getattr(res, "model_used", None)

    print("[V1] 现状 prompt 推理中 ...")
    v1, m1 = _run(SYSTEM_PROMPT, "V1", False)
    print("[V2] P0 意图对齐 prompt 推理中 ...")
    v2, m2 = _run(SYSTEM_PROMPT_V2, "V2", True)

    # 抓 scan 参考价 (供远端 T+N 前瞻评分, Layer 0 补缺)
    try:
        router = _build_router()
        _all = list({s["symbol"] for s in (v1 + v2) if s["direction"] in ("LONG", "SHORT")})
        _q = _batch_quotes(router, _all)
        for s in (v1 + v2):
            _px = _quote_price(_q.get(s["symbol"], {}))
            s["scan_price"] = round(_px, 4) if _px else None
    except Exception as _pe:
        print(f"  [warn] 抓参考价失败 (不影响近端评估): {_pe}")

    v1_by = {s["symbol"]: s for s in v1}
    v2_by = {s["symbol"]: s for s in v2}
    syms = sorted(set(v1_by) | set(v2_by))

    print("\n" + "=" * 72)
    print(f"V1/V2 并排对比  (V1浮现{len(v1)} vs V2浮现{len(v2)})")
    print("-" * 72)
    for sym in syms:
        a = v1_by.get(sym)
        b = v2_by.get(sym)
        a_str = f"{a['direction']:5} conf={a['confidence']:.2f}" if a else "  —(未浮现)"
        if b:
            ev = b.get("evidence_ref", "")
            b_str = (f"{b['direction']:5} conf={b['confidence']:.2f} {b.get('self_tier','?'):<18} "
                     f"ev={('有:'+ev[:24]) if ev else '空->自降'}")
        else:
            b_str = "  —(未浮现/弃选)"
        print(f"  {sym:6}  V1[{a_str}]  ||  V2[{b_str}]")
    print("-" * 72)

    _append_jsonl(_PROMPT_EVAL_PATH, {
        "type": "prompt_eval", "date": _today(), "evaluated_at": _now_et().isoformat(),
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": PROMPT_HASH_V2,  # [U1]
        "clue": manual_clue, "v1": {"model": m1, "surfaced": v1},
        "v2": {"model": m2, "surfaced": v2},
    })
    print(f"[scan-eval] 已落对比文件: {_PROMPT_EVAL_PATH.name} (record-only, 未下单)")
    return 0


# ── eval-report: 近端评估 (浮现质量, 当天可得) ─────────
def cmd_eval_report(days: int = 14) -> int:
    """Layer 1 近端评估: 聚合 prompt_eval.jsonl 的浮现质量指标
    (克制比/引证率/tier分布/夹逼率/稳定性), 不依赖市场。"""
    import statistics
    from collections import defaultdict

    recs = [r for r in _read_jsonl(_PROMPT_EVAL_PATH) if r.get("type") == "prompt_eval"]
    cutoff = (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    recs = [r for r in recs if r.get("date", "") >= cutoff]
    if not recs:
        print(f"[eval-report] 近{days}天无 prompt_eval 数据 (先跑 scan-eval)")
        return 0

    def _trade(surf):
        return [s for s in surf if s.get("direction") in ("LONG", "SHORT")]

    restraint = []
    ev_total, ev_cited = 0, 0
    tier_cnt = {"HARD_EVENT": 0, "MARKET_CORROBORATED": 0, "THESIS_ONLY": 0}
    clamp_total, clamp_hit = 0, 0
    groups = defaultdict(list)

    for r in recs:
        v1 = _trade(r.get("v1", {}).get("surfaced", []))
        v2 = _trade(r.get("v2", {}).get("surfaced", []))
        if len(v1) > 0:
            restraint.append(len(v2) / len(v1))
        for s in v2:
            ev_total += 1
            if s.get("evidence_ref"):
                ev_cited += 1
            tier_cnt[s.get("self_tier", "THESIS_ONLY")] = tier_cnt.get(s.get("self_tier", "THESIS_ONLY"), 0) + 1
            if "clamped" in s:
                clamp_total += 1
                clamp_hit += 1 if s.get("clamped") else 0
        key = (r.get("date"), r.get("clue", ""))
        groups[key].append((frozenset(s["symbol"] for s in v1),
                            frozenset(s["symbol"] for s in v2)))

    def _avg_jaccard(sets):
        pairs = []
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                a, b = sets[i], sets[j]
                u = len(a | b)
                pairs.append((len(a & b) / u) if u else 1.0)
        return statistics.mean(pairs) if pairs else None

    v1_stab, v2_stab = [], []
    for _key, runs in groups.items():
        if len(runs) >= 2:
            a = _avg_jaccard([x[0] for x in runs])
            b = _avg_jaccard([x[1] for x in runs])
            if a is not None:
                v1_stab.append(a)
            if b is not None:
                v2_stab.append(b)

    print("=" * 66)
    print(f"📊 scan-eval 近端评估 (近{days}天, {len(recs)}次A/B)   prompt_eval.jsonl")
    print("=" * 66)
    if restraint:
        print(f"克制比 V2/V1       : {statistics.mean(restraint):.2f}  (均值; <1=V2更克制)")
    if ev_total:
        print(f"V2 引证率          : {ev_cited/ev_total:.0%}  ({ev_cited}/{ev_total})")
    print(f"V2 tier 分布       : HARD {tier_cnt['HARD_EVENT']} / MARKET {tier_cnt['MARKET_CORROBORATED']} / THESIS {tier_cnt['THESIS_ONLY']}")
    if clamp_total:
        print(f"夹逼率(无据却高置信) : {clamp_hit/clamp_total:.0%}  ({clamp_hit}/{clamp_total}; 越低=LLM越守自律)")
    if v2_stab:
        print(f"稳定性(Jaccard)    : V2={statistics.mean(v2_stab):.2f}  vs  V1={statistics.mean(v1_stab):.2f}  (越高越稳; {len(v2_stab)}组多跑)")
    else:
        print("稳定性            : (需同日同场景多次跑才有; 每周3连跑后可见)")
    print("=" * 66)
    return 0


def cmd_push(dry: bool = False, date: Optional[str] = None) -> int:
    """读今日最新一轮 scan → 钉钉推一条整体汇总 (镜像 da_bridge_dingtalk_push 模式)。

    新鲜度守卫: 只推今日 ET 的 scan 记录; 无今日记录 → 跳过不推 (绝不推陈旧)。
    失败安全: 钉钉异常吞掉, 不影响主链。entry_enabled 状态一并汇报 (record-only / 已注入)。
    """
    target = date or _today()
    recs = [r for r in _read_jsonl(_SCAN_PATH)
            if r.get("type") == "scan" and r.get("date") == target]
    if not recs:
        print(f"[push] {target} 无 scan 记录 (今日未扫描/非交易日), 跳过不推")
        return 0
    rec = recs[-1]  # 今日最后一轮
    surfaced = rec.get("surfaced", []) or []
    tradeable = [s for s in surfaced if s.get("direction") in ("LONG", "SHORT")]
    entry_enabled = bool(_cfg("universe_expansion.entry_enabled", False))

    n_long = sum(1 for s in tradeable if s.get("direction") == "LONG")
    n_short = sum(1 for s in tradeable if s.get("direction") == "SHORT")
    n_ext = sum(1 for s in tradeable if s.get("is_universe_external"))
    n_skip = sum(1 for s in surfaced if s.get("direction") == "SKIP")
    tier_cnt: Dict[str, int] = {}
    for s in tradeable:
        t = s.get("self_tier") or "-"
        tier_cnt[t] = tier_cnt.get(t, 0) + 1

    mode = "已注入DB→执行轨道" if entry_enabled else "record-only(未下单)"
    # [DEB-20260825-ue-audit 复核新增] push 文案说真话 — 病灶: mode 只看 entry_enabled
    #   开关, 不看实际落库数。8/25 实弹: 8只浮现, 7只无现价+1只DA排重 → 实际写0条,
    #   但推送仍打"已注入DB→执行轨道" = 对外宣称与实际相反。改查当日真实落库行数。
    _ue_injected_n = 0
    if entry_enabled:
        try:
            from app.playbook.db import PlaybookDB
            _conn_pb = PlaybookDB()._conn()
            _row_n = _conn_pb.execute(
                "SELECT COUNT(*) FROM playbook_instructions WHERE date=? "
                "AND decision_id LIKE ?", (target, f"{STRATEGY_ID}:%")).fetchone()
            _ue_injected_n = int(_row_n[0] or 0) if _row_n else 0
            mode = f"已注入DB→执行轨道 (实际{_ue_injected_n}条)" if _ue_injected_n else \
                f"注入0条 (候选{len(tradeable)}只被现价缺失/排重/冷却等过滤, 见universe_scan.log)"
        except Exception as _pe2:
            print(f"  [warn] 落库数查询失败: {_pe2}")
            mode = f"注入数未知 (entry_enabled=true 但DB查询失败)"
    lines = [
        f"🌌 Universe选股扫描完成 ({target})",
        f"模型={rec.get('model_used','?')} prompt={rec.get('prompt_version','?')} · {mode}",
        f"可交易: {len(tradeable)}只 (多{n_long}/空{n_short}) · 池外{n_ext} · SKIP{n_skip}",
    ]
    if tier_cnt:
        lines.append("自评tier: " + " ".join(f"{k}:{v}" for k, v in tier_cnt.items()))
    lines.append("")
    for s in sorted(tradeable, key=lambda x: x.get("confidence", 0) or 0, reverse=True):
        sym = s.get("symbol", "")
        d = s.get("direction", "")
        wl = s.get("winner_loser", "")
        ext = "🌍外 " if s.get("is_universe_external") else ""
        tier = s.get("self_tier", "")
        # 2026-08-21 (A方案 UE版): conf 是单模型自评, 非共识 (UE 无双模型设计).
        #   真正的置信度锚点是 self_tier 证据层级, 不是 conf 数值 — 已有 Track A 夹逼
        #   (无证据→THESIS_ONLY且≤0.60) + 入库 cap=50 两道闸. 移除 conf 伪概率数字,
        #   改显示 tier 语义标签, 避免误读成"高置信".
        tier_sem = {"HARD_EVENT": "📌硬事件", "MARKET_CORROBORATED": "📊板块证", "THESIS_ONLY": "💭纯推断"}.get(tier, "❓无层级")
        px = s.get("scan_price")
        px_str = f" 参考${px:.2f}" if px else ""
        chain = (s.get("causal_chain", "") or "")[:60]
        thesis = (s.get("thesis", "") or "")[:50]
        lines.append(f"{d} {sym} {ext}{wl} {tier_sem}{px_str}")
        if chain:
            lines.append(f"  链: {chain}")
        if thesis:
            lines.append(f"  论: {thesis}")
    lines.append("")
    lines.append("数值(entry/SL/TP)由 inject 代码计算; 实际执行价=re-price 实时bid/ask")
    msg = "\n".join(lines)
    print(msg)
    if dry:
        print("\n[dry-run] 未推送")
        return 0
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        DingTalkNotifier().send_text(msg)
        print("\n[ok] 钉钉已推送")
    except Exception as e:
        print(f"\n[warn] 钉钉推送失败(不影响链路): {e}")
    return 0


# ── [U11] confirm: 开盘+10min 确认门 (Phase 1 影子裁决) ───────────
def cmd_confirm(force: bool = False, dry_run: bool = False) -> int:
    """两段式第二段: 读今日盘前 scan 假设 → 真实盘中价 price-in 裁决 → 落影子。

    Phase 1 = 影子: 只裁决留痕 (_CONFIRM_PATH), 不拦既有 21:10 inject 链。
    时点语义: 09:32-10:30 ET 窗口外拒绝运行 (开盘+10min 语义漂移防护), --force 可覆盖。
    幂等: 当日已有裁决记录则跳过 (--force 也不绕过, 防双季 cron/重跑重复落样)。
    预注册判据 (Phase 2 接线条件): ≥10交易日且被裁决 n≥30 后, 若 PRICE_IN 桶
      前瞻均值≤0 且 CONFIRM 桶>0 → 接线为 inject 前置闸。
    [2026-08-26 UE审计留记] inject_eligible 是近似切片: 未含冷却/价格地板/无现价
      过滤 (那些在 inject 写库循环内才判) → eligible=True 人群 ⊇ 实际注入人群。
      Phase 2 接线时应改为对当日实际注入人群裁决, 避免门槛标定在超集上。
    """
    if not _is_us_trading_day_today():
        print(f"[confirm] {_today()} 非美股交易日, 跳过")
        return 0
    # 时点守卫: 确认门的语义锚在开盘+10min, 窗口外运行=语义漂移 (数据污染裁决样本)
    _hm = _now_et().hour * 100 + _now_et().minute
    if not (932 <= _hm <= 1030) and not force:
        print(f"[confirm] 当前 ET 时刻 {_hm} 在确认窗口(0932-1030)外, 拒绝运行 (--force 覆盖)")
        return 0
    # 当日幂等守卫 (2026-08-26 复查加固): 双季 cron (EDT 21:40 / EST 22:40 CST) 恒差
    #   1h > 窗宽 58min, 常规下任一季节恰一发落窗内 — 但若未来 cron/窗口被改动致两发
    #   同窗, 或手工重跑, 此处保证一天最多落一条裁决样本 (重复落样污染预注册 n)。
    #   --force 不绕过本守卫 (样本纯度优先); 测试重写当日样本: 删 jsonl 对应行再跑。
    for _prev in _read_jsonl(_CONFIRM_PATH):
        if _prev.get("type") == "confirm" and _prev.get("date") == _today():
            print(f"[confirm] 今日({_today()})已有裁决记录, 幂等跳过 "
                  f"(重写请删 {_CONFIRM_PATH.name} 对应行)")
            return 0

    threshold = float(_cfg("universe_expansion.confirm_gate.price_in_threshold", 0.02) or 0.02)
    scans = [r for r in _read_jsonl(_SCAN_PATH) if r.get("type") == "scan"]
    today_scans = [r for r in scans if r.get("date") == _today()
                   and r.get("scan_window", "premarket") == "premarket"]
    if not today_scans:
        print(f"[confirm] 今日({_today()})无盘前 scan 结果")
        return 0
    surfaced = today_scans[-1].get("surfaced", [])
    tradeable = [s for s in surfaced
                 if s.get("direction") in ("LONG", "SHORT") and s.get("scan_price")]
    if not tradeable:
        print("[confirm] 无可裁决候选 (无 tradeable 浮现)")
        return 0

    # inject 同口径资格判定 (供后续按 inject 人群切片裁决)
    min_conf = float(_cfg("universe_expansion.min_confidence", 0.70))
    _max_streak = int(_cfg("universe_expansion.max_emergence_streak", 99) or 99)
    positions = _position_symbols()
    existing_pb = set()
    try:
        from app.playbook.db import PlaybookDB
        for _inst in PlaybookDB().get_instructions_by_date(_today()):
            if str(_inst.get("direction", "")).upper() == "SKIP":
                continue
            _s = str(_inst.get("symbol", "")).replace("US.", "").upper()
            if _s:
                existing_pb.add(_s)
    except Exception as e:
        print(f"  [warn] 读取 playbook_instructions 排重失败 (继续): {e}")

    router = _build_router()
    q = _batch_quotes(router, [s["symbol"] for s in tradeable])

    verdicts = []
    counts = {"PRICE_IN": 0, "REVERSED": 0, "CONFIRM": 0, "NO_QUOTE": 0}
    for s in tradeable:
        quote = q.get(s["symbol"], {})
        prev_close = float(quote.get("prev_close") or 0) or None
        price = _quote_price(quote)
        verdict, signed = _price_in_verdict(s["direction"], prev_close, price, threshold)
        counts[verdict] += 1
        _eligible = (s.get("confidence", 0) >= min_conf
                     and s["symbol"] not in positions
                     and s["symbol"] not in existing_pb
                     and _streak_passes(s, _max_streak))
        verdicts.append({
            "symbol": s["symbol"],
            "direction": s["direction"],
            "confidence": s.get("confidence"),
            "emergence_streak_days": s.get("emergence_streak_days") or 1,
            "inject_eligible": bool(_eligible),
            "prev_close": prev_close,
            "price": price,
            "signed_pre_move": round(signed, 6) if signed is not None else None,
            "verdict": verdict,
        })
        print(f"  {verdict:9} {s['symbol']:6} {s['direction']:5} "
              f"signed={signed:+.2%}" if signed is not None else
              f"  {verdict:9} {s['symbol']:6} {s['direction']:5} (无价)")

    rec = {
        "type": "confirm",
        "date": _today(),
        "confirmed_at": _now_et().isoformat(),
        "scan_window": "post_open_confirm",
        "phase": "shadow",
        "threshold": threshold,
        "strategy_id": STRATEGY_ID,
        "verdicts": verdicts,
        "counts": counts,
        "note": ("Phase1 影子裁决: 不拦 21:10 inject 链。判据: PRICE_IN桶前瞻≤0 且 "
                 "CONFIRM桶>0 (≥10交易日且n≥30) → Phase2 接线为 inject 前置闸。"),
    }
    if not dry_run:
        _append_jsonl(_CONFIRM_PATH, rec)
    _tail = " [dry-run 不落盘]" if dry_run else ""
    print(f"[confirm] {_today()} 裁决 {len(verdicts)} 候选 → "
          f"PRICE_IN {counts['PRICE_IN']} / REVERSED {counts['REVERSED']} / "
          f"CONFIRM {counts['CONFIRM']} / NO_QUOTE {counts['NO_QUOTE']} "
          f"(阈值 ±{threshold:.1%}){_tail}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 事件推理驱动的选股宇宙扩张")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="LLM 推理浮现标的 (落影子, 不下单)")
    p_scan.add_argument("--clue", default="", help="人工事件/线索文本 (dry-run 验证推理)")
    p_scan.add_argument("--midday", action="store_true",
                        help="午间影子模式: 独立jsonl, 跳过R5快照, 不进inject/DA供料 (DEB §13)")
    p_inj = sub.add_parser("inject", help="代码出数值→写作战计划 (仅 entry_enabled=true)")
    p_inj.add_argument("--dry-run", action="store_true", help="强制只打印不写盘")
    sub.add_parser("score", help="对已 scan 未评分标的算前瞻收益")
    p_rep = sub.add_parser("report", help="查看近 N 天效果")
    p_rep.add_argument("--days", type=int, default=30)
    p_eval = sub.add_parser("scan-eval", help="V1/V2 prompt A/B 对比 (shadow, 不落主scan/不下单)")
    p_eval.add_argument("--clue", default="", help="人工线索 (复现强事件场景做对比)")
    p_evr = sub.add_parser("eval-report", help="近端评估: 聚合浮现质量指标 (克制比/引证率/稳定性)")
    p_evr.add_argument("--days", type=int, default=14)
    p_push = sub.add_parser("push", help="读今日最新一轮 scan → 钉钉整体汇总 (新鲜度守卫)")
    p_push.add_argument("--dry", action="store_true", help="只打印不推送")
    p_push.add_argument("--date", help="scan 日期 YYYY-MM-DD (默认今日 ET)")
    p_cfm = sub.add_parser("confirm", help="开盘+10min 确认门: 真实盘中价 price-in 裁决 (U11)")
    p_cfm.add_argument("--force", action="store_true",
                       help="忽略 0932-1030 ET 时点窗口守卫 (测试用)")
    p_cfm.add_argument("--dry-run", action="store_true",
                       help="只打印裁决不落影子样本 (防测试污染攒样)")
    args = ap.parse_args()

    try:
        if args.cmd == "scan":
            return cmd_scan(manual_clue=args.clue, midday=args.midday)
        if args.cmd == "inject":
            return cmd_inject(force_dry=args.dry_run)
        if args.cmd == "score":
            return cmd_score()
        if args.cmd == "report":
            return cmd_report(days=args.days)
        if args.cmd == "scan-eval":
            return cmd_scan_eval(manual_clue=args.clue)
        if args.cmd == "eval-report":
            return cmd_eval_report(days=args.days)
        if args.cmd == "push":
            return cmd_push(dry=args.dry, date=args.date)
        if args.cmd == "confirm":
            return cmd_confirm(force=args.force, dry_run=args.dry_run)
    except Exception as e:
        import traceback
        print(f"⚠️ 执行异常 ({args.cmd}): {e}")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    _ret = main()
    # 2026-07-16: 强制干净退出。futu OpenQuoteContext 的非 daemon 看门狗线程会吊住
    # 进程 (曾观测 scan 写完记录后挂起 10min+), 导致 cron `scan && inject && push`
    # 卡在第一段不推进。所有实质工作 (jsonl 落盘 / DB 提交 / 钉钉发送) 均在 main()
    # return 前同步完成, 残留仅为 futu 线程 → flush 后直接终止, 不等待残留线程。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(_ret if isinstance(_ret, int) else 0)
