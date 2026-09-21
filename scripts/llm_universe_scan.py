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
import math
import os
import re
import sys
import time
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

# ── [P2.1 2026-09-12, proud-slope-falcon] 材料索引: gather 时登记结构化事实 ──
# 病灶 (原实现 = "事后按行前缀猜材料", 已实证为断头传感器):
#   _build_clue_brief 尾部曾把 parts 按 "\n" 拆行, 再用
#   `not line.startswith("[")` 排除节标题 —— 但该谓词同时**误杀了以 `[` 开头的事实行**:
#     `[CRITICAL/REGULATION] …`  (_gather_hard_events 分类新闻)
#     `[HIGH/EARNINGS] …` / `[MEDIUM/EARNINGS_UPCOMING] …`  (_gather_earnings_events)
#     `[HIGH/INSIDER_CLUSTER] …`  (_gather_insider_clusters)
#   这四类正是 SYSTEM_PROMPT_V2 要求模型为 `self_tier=HARD_EVENT` 去锚定引用的材料。
#   后果 (只读实测 data/shadow/llm_universe_scan*.jsonl, 347 行带 evidence_grounded):
#     grounded=False 共 32 行 (9.2%), 其中 **31 行 (96.9%) 是 HARD_EVENT 逐字引用上述四类行**
#     (例: `[HIGH/INSIDER_CLUSTER] BABA 内部人集群买入信号 x1 → 看多` = gather 的 f-string 原文),
#     仅 1 行是 MARKET_CORROBORATED ⇒ 观测到的"误杀"几乎全是本缺陷, 不是模型编造。
#   影响面: `evidence_grounding_enforce=false`(当前值) ⇒ **暂无行为变更**, 但攒样判据
#     ("5 交易日人工抽检误杀率 <10% → 裁开强制") 正建立在被污染的分母上; 若据 9.2% 开强制,
#     31 条真实 HARD_EVENT 引证会被清成空串 → Track A 夹逼 → tier 降 THESIS_ONLY + conf≤0.60。
# 修法: 材料索引在 **gather 现场**登记 (source/symbols/published_at/event_key 当时都还在手上),
#   render 消费同一份对象; `_LAST_BRIEF_MATERIALS` 由索引派生, **不再有任何行前缀猜测**。
#   节标题/日期行本来就不是事实 ⇒ 不登记 (by construction, 而非靠谓词过滤)。
# 本子批**不改渲染文本** (text 逐字沿用旧 f-string) ⇒ brief 正文零漂移, 无需升 PROMPT_VERSION。
# 回滚: 还原本段 + _build_clue_brief 尾部派生行 (见 DEB-20260910 §17)。
_BRIEF_EVIDENCE: List[Dict[str, Any]] = []
EVIDENCE_SCHEMA = ("evidence_id", "source", "published_at", "symbols",
                   "event_key", "text")


# [P2.1 修正 2026-09-13] 只有"长得像 ticker"的 token 才准进材料索引的 symbols 槽。
#   形状族与 candidate_eligibility._SYMBOL / _SYMBOL_UNSUPPORTED_SHAPE 同源 (字母数字 +
#   单个 `.`/`-` 分隔的权证·优先股后缀), 但这里是**登记前的卫生门, 不是资格判定** ——
#   资格判定的唯一真源仍是 candidate_eligibility.evaluate_candidate。
# 为什么需要它 (实测缺陷, 2026-09-13 只读复现): market_corroboration 的"宇宙外异动"行
#   曾写 `symbols=[x.split(":")[0].split("(")[0] for x in movers]` —— 那是按**臆想的**
#   渲染格式去反解 ticker, 而生产实际格式是 f"{symbol} {direction} {pct:+}%"
#   (例 "ESTC UP +19.3%", 既无冒号也无括号) ⇒ 整串被当 symbol 登记进索引。后果两层:
#     ① 初筛把 "ESTC+19.3%" 判 invalid_symbol → brief 里凭空多一行
#        "仅背景(context_only, 不得当直接交易候选): ESTC UP +19.3%" = **对真标的的假指控**,
#        正是 P2 验收②"背景事件和榜外发现未被错误清空"要防的事;
#     ② 真 ESTC 从未被初筛过 (它压根没进具名集合), P2 验收①的"低价/低波不占预算"在
#        异动源上等于没生效。
#   根因与 P2.1 要修的是同一族: **事后从渲染串里猜结构化字段**。故正解是把 symbols 取回
#   gather 现场 (_gather_market_corroboration 现返回 mover_symbols), 本门只作兜底 ——
#   拦住任何未来又冒出来的"拿字符串当 symbols"的调用点, 不让它污染索引与初筛。
_TICKERISH = re.compile(r"[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z0-9]{1,4})?\Z")


def _ticker_symbols(values) -> List[str]:
    """挑出 ticker 形状的 token (去 `US.` 前缀/大写/去重/保序), 非 ticker 一律丢弃。"""
    out: List[str] = []
    for raw in values or ():
        tok = str(raw or "").strip().upper().replace("US.", "")
        if tok and _TICKERISH.fullmatch(tok) and tok not in out:
            out.append(tok)
    return out


def _fact(source: str, text: str, *, symbols=(), published_at=None,
          event_key=None) -> Dict[str, Any]:
    """[P2.1] 一条结构化事实 — gather 产出、render 消费、索引登记**同一个对象**。

    `text` 必须是最终送进 brief 的那一行 (逐字), 否则索引与正文不对应,
    引证核验就又回到"猜"。symbols 统一去 `US.` 前缀并大写 (与既有 _corro_syms 同口径),
    并过 `_ticker_symbols` 形状兜底 (见其注释: 拦住"拿渲染串当 symbols"的源头污染)。
    """
    return {
        "source": str(source),
        "text": str(text),
        "symbols": sorted(set(_ticker_symbols(symbols))),
        "published_at": published_at,
        "event_key": event_key,
    }


def _take_facts(facts, *, source: str = "unknown") -> List[str]:
    """登记 facts 进材料索引, 返回可渲染文本行 (顺序与内容逐字不变)。

    兼容传入裸 str (未改造的调用点/降级路径) —— 一律按事实登记, 不丢材料。
    """
    lines: List[str] = []
    for item in (facts or []):
        if isinstance(item, str):
            item = _fact(source, item)
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        entry = dict(item)
        entry["text"] = text
        entry["source"] = str(entry.get("source") or source)
        entry["evidence_id"] = f"E{len(_BRIEF_EVIDENCE) + 1:03d}"
        _BRIEF_EVIDENCE.append(entry)
        lines.append(text)
    return lines


def brief_evidence_index() -> List[Dict[str, Any]]:
    """[P2.1] 最近一次 _build_clue_brief 的材料索引 (只读副本, 供落盘/核验/测试)。"""
    return [{k: e.get(k) for k in EVIDENCE_SCHEMA} for e in _BRIEF_EVIDENCE]


PROMPT_VERSION = "v2.13"  # scan 落库归因戳; v2.13=市场佐证段**时点诚实化** (2026-09-14, DEB-20260914-premarket-data-validity-audit §一 P1-3, 用户裁"诚实化+补时点标签"): [市场佐证] 三条源 (DA 日更盘前轮动 / 机械 capture 回退源 / 当日盘中脉搏) 的渲染行补齐**数据时点口径** —— 修前模型看到的是**无日期**的 "轮入板块: XLK(Technology) +1.32%", 无从判断是今日还是上一交易日, 而实测该回退源的数字**本就是上一交易日收盘**(capture 的 date 是采集时刻 ET 日期, cron ET 00:30) 且 `published_at` 也照抄该采集日 ⇒ 时点错一个交易日; 同批修①生产两窗 (ET 07:40 da_feed / 09:10) 结构上永不成立却挂着"盘中板块脉搏(实时)"标签的**失实声明**(实测唯一命中者是 ET 12:30 的 `--midday` **只读影子** ⇒ 该输入从未进过任何生产指令), ②`_age_days` 负数(未来日期)被放行的守卫缺口, ③脉搏分支 `except: pass` 静默吞错 → 显式留痕。**刻意不放宽守卫**: 盘前仍不注入上一交易日脉搏 — DEB-20260729 §十 作者按其价值判断"建议不修"(pulse 真归宿是盘中板块 veto, 至今仍在参数冻结后排队), 本批只消除"宣称有而实际没有"的静默, 不改变该价值判断; 材料一条不增不外扩、全程 fail-open; ⚠ 与 v2.11/v2.12 同族盲区**第三次出现**: 本变更改的是 brief(user)侧渲染 ⇒ `PROMPT_HASH_V2` 只哈希 SYSTEM_PROMPT_V2 故**哈希不变**(仍 70e4680597af), 按 prompt_hash 切分的口径**分不开 v2.12 前后**; 分段必须改用本批同步升版的 `BRIEF_TEMPLATE_VERSION=ue_brief_v4` + `brief_template_hash`/`brief_content_hash` (三指纹基建见 P2.5, 已于 2026-09-13 起落进 scan 与 thinking-eval 两条记录) — 即"缺口已兑现": 用户裁要分段时历史轮次**有键可切**; ⚠ 另: thinking-eval 预注册判据 (ledger cron 1e96c03c945e, 到期 2026-09-22) 的切分键是 `prompt_hash`, 本批**不动判据**(改判据＝改尺子须用户裁, DEB-20260910 §17.6 已在待裁项), 但本批使该序列新增一个 brief 分段点(v3→v4), 故 9/22 读数须按 `brief_template_version` 分段后再判"有效轮次"; v2.12=P2.2 输入具名标的可交易性初筛段 (2026-09-12, DEB-20260910 §17 / proud-slope-falcon, 用户裁"按设计计划执行落地"): clue brief 末尾按 **P1 同一资格口径**追加"可作直接交易候选 / 仅背景(context_only) / 未定(缺数据)"三态清单 —— 属 brief(user)侧**输入变更**, 故升版; 材料一条不删、全程 fail-open、扫描期口径刻意不含执行时微结构门(报价新鲜度/点差/借券, 见 _scan_phase_policy); ⚠ 与 v2.11 同族盲区**再次出现**: PROMPT_HASH_V2(:195) 只哈希 SYSTEM_PROMPT_V2 ⇒ 本变更不改哈希(仍 70e4680597af), 按 prompt_hash 切分的口径**分不开 v2.11/v2.12**; 分段请改用本批同时落库的 brief_template_version(=ue_brief_v3) / brief_template_hash / brief_content_hash —— P2.5 已把该基建补上, 所以这次是"缺口已兑现"而非"又添一个盲区"(盲区记录见下方 v2.11 段与 tests/test_ue_contract_validator.py::test_brief_only_bump_leaves_hash_identical_to_v210); 同批 P2.4 引证三态 / P2.6 空转跳批均为**代码侧附加留痕与预算守卫**, 不改模型输入, 不单独占版号; v2.11=负反馈表口径两处修 (2026-09-11, DEB-20260910 §11.1/§11.6-item6/§11.8, 用户裁"核查确认无误按建议执行"+"升 v2.11 记 U1, 缺口另立项"): ①U1 反号修(本 U1 = DEB-20260910 编号, 非下方 v2.9 段旧 U1) — 删读侧二次签名(写侧 ic_tracker 已按方向签名), 修前自 2026-07-25 起 UE SHORT 有 fwd 共 66 条、66/66 标签反转, 其中 41 条做对的空头被标"推错"喂进抑制规则; ②U1b 零收益三态 — adj==0 由归入 ✗ 改判 ＝, 表头与抑制规则同步定义三态并显式排除 ＝ 计入「连续推错」, 判定用精确零不用显示舍入(否则 08-11 US.CVX 真值 +0.0249% 会被误判中性); ⚠️归因盲区就地声明: 本版两处均在 brief/user 侧, 而 PROMPT_HASH_V2(:126) 只哈希 SYSTEM_PROMPT_V2 ⇒ 升版号不改变哈希, brief 侧指纹基建缺口按用户裁另立项(§11.6-item6), 不得据 v2.11 认定 brief 变更已被指纹覆盖; v2.10=失效条件逐metric补单位, 成交量改20日完整日线量比 (2026-09-08); v2.9=kimi收敛稿全文重写55→26行 (2026-09-05, PROPOSAL-20260905 U4, 用户裁"如无问题可以实施"): ①字段表补invalidation_triggers进JSON示例(v2.8漏,E3规律"示例没有≈不存在",实测填充率仅36%); ②LONG示例short_check/skip_reason改null+新增完整SHORT示例(v2.8示例自违null规则=教错误行为); ③删"充分浮现不设数量上限"(放量偏置,质量分层由tier闸接管); ④删板块级联/结构约束/回调优先三散文(不可测不依从,U1渲染侧+U3 tier闸已对冲); ⑤夹逼写成可预期事实("代码将核验/封顶")替代"自律约束"; v2.8=E3-fix 输出模板 {字段...} 占位展开为显式字段列表(含 invalidation_triggers) — E3 8臂实弹: 只改措辞强度 0%→0%, 字段写进 JSON 模板即 83-100% 填充且全过白名单 (2026-08-31, DEB-20260830第5轮, 用户裁"按建议先执行"); v2.7=invalidation_triggers 强制≥1条→可空数组降级 (2026-08-30, DEB-20260829第7轮, 0/208填充实测); v2.6=SKIP skip_reason 字段 (2026-08-25, DEB-ue-audit U4, 拒绝可归因); v2.5=删死止损规则 (2026-08-16, DEB-fullchain #23) + 版本戳/归因修复 (2026-08-25, DEB-ue-audit U1); v2.4=结构约束前置+回调优先 (2026-08-09, DEB-20260808 §17.1); v2.3=板块级联 (07-28); v2.2=可证伪字段+证据清单 (07-25)

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
# v2.10 在 v2.9 收敛稿上补齐 invalidation_triggers 量纲并与 parser 白名单对齐。
# v2.9 (2026-09-05, PROPOSAL-20260905 U4, 用户裁"如无问题可以实施"): kimi 收敛稿全文重写
#   (55→26行 −53%)。修复点见 PROMPT_VERSION 注释。V1 SYSTEM_PROMPT 不动
#   (scan-eval A/B 基线仪器); v2.8 旧文 git 历史可取。
#   预注册声明 (P3): 浮现量下降是预期效果 (监控 HARD_EVENT 桶量不塌缩);
#   SHORT 示例可能轻微抬 SHORT 产出率 (第9条门槛保留); invalidation_triggers 填充率
#   基线 36% → 目标升。同窗影子 ≥15 交易日 (与 U1 渲染侧同窗, UE 侧一次一变量)。
SYSTEM_PROMPT_V2 = (
    "你是资深事件驱动型美股分析师。职责: 从给定材料浮现受事件/市场驱动的可交易美股标的, 多空对等, 质量分层由下游代码执行。\n"
    "输出契约(逐字段, 缺字段=违约):\n"
    "1. symbol: 美股代码不带前缀, 只输出真实可交易标的; direction: LONG/SHORT/SKIP, 不确定=SKIP。\n"
    "2. evidence_ref: 引用给定材料中的具体证据原文(新闻标题/异动数据/人工线索); 仅凭行业常识或板块联想则留空字符串。"
    "代码将核验引证真实性, 空引证将被强制降级。\n"
    "3. self_tier: HARD_EVENT=因果链锚定evidence_ref里的具体事件(封锁/战争/财报/监管/事故); "
    "MARKET_CORROBORATED=无具体事件但evidence_ref是真实市场异动/板块轮动数据; THESIS_ONLY=evidence_ref为空仅逻辑联想。\n"
    "4. causal_chain: 事件→传导→标的一句话; thesis: 交易论点≤60字(THESIS_ONLY必须以'板块联想:'或'逻辑推演:'开头)。\n"
    "5. confidence: 0~1小数, 如实反映证据强度(HARD_EVENT>MARKET_CORROBORATED>THESIS_ONLY; 空引证者将被代码封顶0.60)。\n"
    "6. invalidation: 一句话失效条件≤40字(非SKIP必填); invalidation_triggers: 代码可判定失效条件数组, "
    "每条{metric,operator,value}, metric限收盘价/量比/5日涨跌/盘前涨跌幅, operator限<或>, value纯数字。"
    "收盘价单位=USD; 量比=当日累计成交量÷最近20个完整交易日平均成交量(无量纲, 如2.0); 两个涨跌幅指标单位=百分比百分点(如-5); 无可判定条件填[]。\n"
    "7. evidence_checklist: {catalyst_specific,catalyst_within_24h,price_volume_confirmed,crowding_low}均bool, 如实勾选不得美化。\n"
    "8. horizon_hours: 预期兑现时效整数小时(日内动量4-8); expected_move_pct: 预期方向幅度%小数; "
    "is_universe_external: bool; winner_loser: WINNER/LOSER; corroborated_by_market: bool。\n"
    "9. 仅SHORT填short_check对象{borrow_feasible(bool),downside_catalyst_expiry_hours(int),squeeze_risk(1-5)}, 非SHORT填null; "
    "无具体下跌催化剂(仅'估值高'/'涨多了')的SHORT不得浮现。仅SKIP填skip_reason一句话, 非SKIP填null。\n"
    "不输出任何价格数字(entry/止损/止盈由下游代码计算)。严格只输出JSON, 字段与顺序与示例完全一致:\n"
    "{\"surfaced\": [\n"
    " {\"symbol\": \"AAPL\", \"direction\": \"LONG\", \"evidence_ref\": \"材料中具体证据原文\", \"self_tier\": \"HARD_EVENT\", "
    "\"causal_chain\": \"事件→传导→标的\", \"thesis\": \"交易论点≤60字\", \"confidence\": 0.72, "
    "\"invalidation\": \"一句话失效条件\", \"invalidation_triggers\": [{\"metric\": \"收盘价\", \"operator\": \"<\", \"value\": 185.5}, {\"metric\": \"量比\", \"operator\": \"<\", \"value\": 0.8}], "
    "\"evidence_checklist\": {\"catalyst_specific\": true, \"catalyst_within_24h\": true, \"price_volume_confirmed\": true, \"crowding_low\": true}, "
    "\"horizon_hours\": 6, \"expected_move_pct\": 2.5, \"is_universe_external\": false, \"winner_loser\": \"WINNER\", "
    "\"corroborated_by_market\": true, \"short_check\": null, \"skip_reason\": null},\n"
    " {\"symbol\": \"XYZ\", \"direction\": \"SHORT\", \"evidence_ref\": \"材料中具体证据原文\", \"self_tier\": \"MARKET_CORROBORATED\", "
    "\"causal_chain\": \"事件→传导→标的\", \"thesis\": \"交易论点≤60字\", \"confidence\": 0.65, "
    "\"invalidation\": \"一句话失效条件\", \"invalidation_triggers\": [], "
    "\"evidence_checklist\": {\"catalyst_specific\": true, \"catalyst_within_24h\": true, \"price_volume_confirmed\": true, \"crowding_low\": false}, "
    "\"horizon_hours\": 8, \"expected_move_pct\": 3.0, \"is_universe_external\": true, \"winner_loser\": \"LOSER\", "
    "\"corroborated_by_market\": true, \"short_check\": {\"borrow_feasible\": true, \"downside_catalyst_expiry_hours\": 24, \"squeeze_risk\": 2}, \"skip_reason\": null}\n"
    "]}"
)

# [DEB-20260825-ue-audit U1] SYSTEM_PROMPT_V2 指纹 — 落库归因戳 (与 DA r13
#   _prompt_fingerprint 同模式)。病灶1版本戳失真: PROMPT_VERSION 常量可在 prompt
#   已改时忘记升 (8/16 v2.5 改动落库仍标 v2.4), hash 随内容自动变 → 新旧基线可切分。
PROMPT_HASH_V2 = hashlib.sha1(SYSTEM_PROMPT_V2.encode("utf-8")).hexdigest()[:12]

# ── [P2.5 2026-09-12, proud-slope-falcon] brief 侧指纹: 补 §11.6-item6 基建缺口 ──
# 病灶 (DEB-20260910 §11.6-item6 已量出, 当时按用户裁"另立项"未动):
#   `PROMPT_HASH_V2` = sha1(SYSTEM_PROMPT_V2), **只覆盖系统 prompt**。而负反馈表、
#   抑制规则散文、输入初筛段、以及全部 clue 材料都在 **user 内容 (brief)** 里
#   ⇒ 任何 brief 侧改动对落库的 prompt_version/prompt_hash **完全不可见**,
#   "同版本内样本可混算"这条护栏在 UE 侧对 brief 改动不设防。
#   后果已发生过一次: v2.11 改的正是反馈表渲染, 而 PROMPT_HASH_V2 一字未变。
#   对照: 仓外 DA 侧 `_prompt_fingerprint(SYSTEM_PROMPT, USER_PROMPT_TEMPLATE)`
#   同时哈希 user 模板 —— UE 侧此前没有对等物, 本段补上。
# 三个指纹各司其职 (不可互相替代):
#   brief_template_version  手工维护的**分段键** — 试验按它切窗 (计划 P0.5:
#                           "输入分布变化后, 受影响试验按新版本分段");
#   brief_template_hash     自动算的**结构**指纹 — 只哈希非事实的脚手架 (节标题 +
#                           指令散文), 数字掩码后跨日稳定 ⇒ 改了段结构/措辞就变,
#                           当天新闻变了不变。它的作用是**验证 template_version 没忘升**
#                           (与 PROMPT_HASH_V2 对 SYSTEM_PROMPT_V2 同一防护意图);
#   brief_content_hash      当次渲染全文指纹 — 逐run 唯一, 供精确复现与审计,
#                           **不可**当分段键 (每天都变)。
# 不改动 PROMPT_HASH_V2 的定义与取值: 它是既有 thinking-eval A/B 预注册判据的切分键,
#   改它等于中途换尺子。brief 侧变更改用 brief_template_hash 切分, 两者并列落库。
BRIEF_TEMPLATE_VERSION = "ue_brief_v4"   # v4 = 市场佐证段时点口径 (2026-09-14, PROMPT_VERSION v2.13)
                                         #  v3 = P2.1 材料索引 + P2.2 输入初筛段
# [P3.1 2026-09-13, DEB-20260910 §18.2] 生成值留档口径版本。
#   planned_* 记的是"生成时刻的数值与口径", 而 default_stop_pct/default_tp_pct/
#   KNIFE_FLOOR_PCT/CHASE_ZONE_PCT 任一改动都会让同一份 planned_* 的含义漂移
#   (配置 3%/6% 落到价格上其实是 5.91%/11.30%, 见 §18.2 表)。改这些常量 ⇒ 必须升本版本,
#   否则跨期比较 planned vs 重定价 会拿两把不同的尺子。
# [P3.3-3.5 2026-09-13] v1→v2: blob 从"只有生成值三段"扩成"生成值 + 估算股数/费用覆盖/
#   可执行性"。升版是读账需要 —— v1 行没有 size/cost/exec 三段, 跨期聚合时若不按版本
#   切分, 会把"当时刻意没算"(v1) 与"算了但缺数"(v2 的 not_computed_*) 混成一类。
#   sizing/cost 口径自身的版本另记 planned_executability.PLANNED_EXEC_BASIS_VERSION
#   (只改借券基准或上限归因时升那个, 不必升 blob 版本)。
PLANNED_PREVIEW_VERSION = "ue_planned_v2"
_LAST_BRIEF_FINGERPRINTS: Dict[str, Any] = {}


def _brief_fingerprints(parts, evidence, brief_text: str) -> Dict[str, Any]:
    r"""算 brief 三指纹 + 材料构成统计 (纯函数, 不做 IO)。

    脚手架 = parts 里**不是**已登记事实文本的条目 (节标题、指令散文、日期行)。
    数字掩码 (`\d+` → `#`) 让 `日期: 2026-09-12 (ET)`、`[近24h 新闻头条 (共37条…)]`
    这类含当日值的脚手架跨日稳定 —— 否则 template_hash 每天都变, 无法当结构指纹。

    ⚠ 诚实边界 (实测确认, 不是理论担忧): template_hash 只在**同一组段落被渲染**时跨日
      稳定。多个段本来就是条件段 —— 负反馈表空则不出、regime 拉取失败则不出、本批新增
      的输入初筛段在无具名标的时不出 ⇒ 段落集合变化会让 hash 移动而代码并未改。
      方向是**安全侧**: 它只会多报"hash 变了"(假阳, 人工比对 brief_sections 即可排除),
      不会漏报真实的模板改动 —— 而漏报才是"忘了升 template_version"的危险方向。
      `brief_sections` 就是为这次人工排除而落的段落清单 (只进 _LAST_BRIEF_FINGERPRINTS
      供诊断/测试, **不落库** —— 不新增无消费方的库字段, AGENT.md P7 军规)。
      试验分段键仍以手工维护的 brief_template_version 为准。
    """
    fact_texts = {str(e.get("text") or "") for e in (evidence or [])}
    scaffolding = [str(x) for x in (parts or []) if str(x).strip() not in fact_texts]
    masked = "\n".join(re.sub(r"\d+", "#", line) for line in scaffolding)
    # 段落清单 = 以 "\n[" 开头的 parts 条目 (节标题统一格式)。事实行虽也可能以 "["
    #   开头 (如 `[HIGH/EARNINGS] …`), 但它们是独立列表项、无前导换行 ⇒ 不会被误收。
    sections = [str(x).lstrip("\n") for x in (parts or []) if str(x).startswith("\n[")]
    stats: Dict[str, int] = {}
    for entry in (evidence or []):
        key = str(entry.get("source") or "unknown")
        stats[key] = stats.get(key, 0) + 1
    return {
        "brief_template_version": BRIEF_TEMPLATE_VERSION,
        "brief_template_hash": hashlib.sha1(masked.encode("utf-8")).hexdigest()[:12],
        "brief_content_hash": hashlib.sha1(
            str(brief_text or "").encode("utf-8")).hexdigest()[:16],
        "brief_scaffolding_lines": len(scaffolding),
        "brief_material_count": len(evidence or []),
        "brief_material_stats": dict(sorted(stats.items())),
        "brief_section_count": len(sections),
        "brief_sections": sections,
    }


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
# [P1.5 2026-09-12, proud-slope-falcon] inject 额度进程间锁载体 (flock)。
#   覆盖"读取当日额度 → 资格筛选 → 写后确认"整段, 防 cron 与手动重跑并发超额;
#   锁键语义 = 自动账户模式/来源/ET 日期 (本文件即 UE 自动源, 每日一条链)。
#   已在 tests/conftest.py _PRODUCTION_DATA_SEAMS 登记 → 单测自动落沙箱。
_INJECT_LOCK_PATH = _ROOT / "data" / "shadow" / "ue_inject_quota.lock"


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


def _log_inject_batch_decision(scan_rec: Optional[Dict[str, Any]], reason: str,
                               today: str, now_et: datetime) -> None:
    """[P1.5 2026-09-12] inject 批次级消费留痕: 消费了哪一批 / 为何不消费。

    与 _log_inject_reject (候选级) 分层。本行回答"今天的注入依据是哪一批材料",
    使"静默回用过期/异窗 (da_feed/midday) 候选"在审计上不可能隐形。
    复用 inject jsonl 载体, 不新建文件/库。
    """
    try:
        rec = scan_rec or {}
        row = {"date": today, "type": "batch_decision", "stage": "inject_batch",
               "decided_at": now_et.isoformat(), "reason": reason,
               "batch_id": rec.get("batch_id"),
               "scan_window": rec.get("scan_window", "premarket"),
               "batch_started_at": rec.get("started_at"),
               "batch_scanned_at": rec.get("scanned_at"),
               "surfaced_n": len(rec.get("surfaced") or []),
               "empty_batch": bool(rec.get("empty_batch")),
               "model_used": rec.get("model_used"),
               "policy_version": rec.get("policy_version")}
        _INJECT_REJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _INJECT_REJECT_PATH.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        # 留痕失败必须可见 (R5/AGENT.md "静默吞 = 门死了没人知道"):
        #   本行是"今天注入依据哪一批材料"的唯一载体, 静默 pass 会让批次纪律
        #   在审计上隐形 (也曾吞掉 tests/conftest.py 的生产 data/ 写入硬闸)。
        print(f"  [warn] inject 批次决策留痕落盘失败 (不阻塞注入): {e}")


def _eligibility_skip_reason(reason_codes: List[str], screening: Dict[str, Any],
                             policy: Dict[str, Any]) -> str:
    """资格拒绝 → 可归因 skip_reason (自带数值; 与旧 price_floor/low_atr 同轨)。"""
    codes = [str(c) for c in (reason_codes or [])]
    px = screening.get("price")
    ratio = screening.get("screening_atr_ratio")
    if "price_floor" in codes and px:
        return (f"price_floor({float(px):.2f}<{float(policy.get('min_price') or 0):g}, "
                f"OTC/仙股无富途行情)")
    if "low_atr" in codes and ratio is not None:
        return (f"low_atr({float(ratio):.4f}<{float(policy.get('min_atr_ratio') or 0):g}, "
                f"低波票过不了执行门)")
    return f"candidate_ineligible({','.join(codes) or 'unknown'})"


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


# ── [U3 PROPOSAL-20260905 P4, 2026-09-05 用户裁"如无问题可以实施"] tier 闸 (影子-only) ──
# 病灶 (B1+CF1/CF2 实证): self_tier 有真实区分度但完全未消费 — HARD_EVENT +$101.54/17笔
#   vs MARKET_CORROBORATED −$379.10/12笔 (MC=出血桶); THESIS_ONLY 现状漏洞:
#   夹逼上限 0.60 > min_conf 0.5 必漏 (库内已有 4 条指令 3 笔成交)。
# 规则 (影子判定, 不拦任何指令):
#   HARD_EVENT            → 直进 (仍过既有 conf/streak/持仓/价格地板/低ATR 闸)
#   MARKET_CORROBORATED   → 需补强: symbol ∈ 当日交叉命中集 (ue_corro_syms_{ET}.json,
#     U1③ scan 侧落盘) 或 raw_conf≥0.70 且 rubric_score≥3; 否则 would_block=True
#   THESIS_ONLY           → would_block=True
# 预注册判据: 攒样 ≥15 交易日且被影子化桶 fwd 回填 n≥30。
#   毕业: 被拦桶方向性前瞻均值 ≤0 且放行桶均笔 >0 → 用户裁决开 enforce (tier_gate_enabled)。
#   撤闸认错: 被拦桶前瞻 >+0.3% 且 n≥30 → 撤。
# 诚实声明: MC 桶 n=12, 均笔 −31.59 可能由少数大亏主导 (未做 permutation/聚类)
#   → 闸设计为可撤而非永久封禁。影子期 tier_gate_enabled 禁改 true。
# 回滚: 配置 universe_expansion.tier_gate_shadow: false。
_TIER_GATE_SHADOW_PATH = _ROOT / "data" / "shadow" / "ue_tier_gate_shadow.jsonl"


def _load_corro_syms(today: str) -> set:
    """读当日交叉命中集 (U1③ scan 侧落盘)。文件缺失/陈旧=空集 (交叉命中判 False)。"""
    try:
        _p = _ROOT / "data" / f"ue_corro_syms_{today}.json"
        if _p.exists():
            _d = json.loads(_p.read_text(encoding="utf-8"))
            if _d.get("date") == today:
                return {str(x).upper() for x in (_d.get("symbols") or [])}
    except Exception as _e:
        # 降级空集须响亮 (except-pass 棘轮 + 禁静默纪律); 文件缺失属正常分支不进此
        print(f"  [warn] 交叉命中集读取失败 (降级空集, tier 闸交叉命中臂失效): {_e}")
    return set()


def _tier_gate_verdict(s: Dict[str, Any], corro_syms: set) -> Dict[str, Any]:
    """tier 闸影子判定 — 返回判定行 (纯观测, 永不改变指令流)。"""
    tier = str(s.get("self_tier") or s.get("tier") or "THESIS_ONLY").upper()
    if tier not in ("HARD_EVENT", "MARKET_CORROBORATED", "THESIS_ONLY"):
        tier = "THESIS_ONLY"
    # 量纲: 此层 (scan jsonl / normalize) raw_confidence 是 0-1 小数, 阈值 0.70 同尺;
    #   指令 origin blob 的同名键是 0-100 整数, 不可跨载体直接比较。
    raw_conf = float(s.get("raw_confidence", s.get("confidence", 0)) or 0)
    try:
        rubric = int(s.get("rubric_score", 0) or 0)
    except (TypeError, ValueError):
        rubric = 0
    sym = str(s.get("symbol", "")).upper()
    in_cross = bool(sym) and sym in corro_syms
    if tier == "HARD_EVENT":
        would_block, rule = False, "hard_event_pass"
    elif tier == "MARKET_CORROBORATED":
        reinforced = in_cross or (raw_conf >= 0.70 and rubric >= 3)
        would_block = not reinforced
        rule = ("mc_cross_hit" if in_cross
                else ("mc_conf_rubric" if reinforced else "mc_unreinforced"))
    else:
        would_block, rule = True, "thesis_only_shadow"
    return {"symbol": sym, "direction": s.get("direction", ""),
            "tier": tier, "raw_conf": round(raw_conf, 3), "rubric_score": rubric,
            "in_cross_hit": in_cross, "would_block": would_block, "rule": rule}


def _log_tier_gate_shadow(surfaced: List[Dict[str, Any]], cand_syms: set) -> None:
    """对全部 LONG/SHORT 浮现标的落 tier 闸影子判定。passed_chain=是否过了既有
    筛选链 (进 cand) — 毕业判据 join fwd 时以 passed_chain=True 子集度量
    "若开闸, 今日实际被拦的指令" (被既有链先拦下的标的不受 tier 闸影响)。"""
    if not bool(_cfg("universe_expansion.tier_gate_shadow", True)):
        return
    try:
        corro = _load_corro_syms(_today())
        rows = []
        for s in surfaced:
            if s.get("direction") not in ("LONG", "SHORT"):
                continue
            v = _tier_gate_verdict(s, corro)
            v["passed_chain"] = v["symbol"] in cand_syms
            rows.append(v)
        if not rows:
            return
        n_block = sum(1 for r in rows if r["would_block"])
        _append_jsonl(_TIER_GATE_SHADOW_PATH,
                      {"date": _today(), "n": len(rows), "n_would_block": n_block,
                       "corro_set_size": len(corro), "rows": rows})
        print(f"  [tier-gate-shadow] {n_block}/{len(rows)} 影子拦截 "
              f"→ {_TIER_GATE_SHADOW_PATH.name}")
    except Exception as e:
        print(f"  [warn] tier 闸影子落盘失败 (不阻塞): {e}")


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
        # P2 (2026-09-17, 用户裁「接收建议」): RSSHub 降级告警 — 静默降级=病D,
        #   无人知晓信息层断腿. 告警失败只打印, 不阻断主链 (best-effort).
        try:
            from app.observability.feishu_alert import FeishuAlerter
            alerter = FeishuAlerter(
                app_id=os.getenv("FEISHU_APP_ID", ""),
                app_secret=os.getenv("FEISHU_APP_SECRET", "")
            )
            alerter.send_warn(
                title="UE 新闻源降级告警",
                content=f"RSSHub 拉取失败, 降级到 Finnhub 单源。\n错误: {str(e)[:200]}\n时间: {_today()}",
                receive_id=os.getenv("FEISHU_CHAT_ID", "")
            )
        except Exception as _ae:
            print(f"  [warn] 降级告警发送失败 (不阻断): {_ae}")
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


def _prev_trading_day_str(dstr: str) -> str:
    """`dstr`(YYYY-MM-DD) 之前的**上一个美股交易日**; 日历不可用时原样返回 (fail-open)。

    2026-09-14 (DEB-20260914-premarket-data-validity-audit §一 P1-3) 为什么需要它:
    回退源 `universe_expansion_shadow.jsonl` 的 `date` 是**写入时刻的 ET 日期**
    (writer `scripts/universe_expansion_shadow.py` `date = _today_et_str()`, cron 是
    ET 00:30), 而那一刻取到的行情是**上一交易日收盘** ⇒ 记录 date=2026-09-14 装的是
    09-11(周五)的板块涨跌。消费端此前把该 date 当数据时点透传 (published_at),
    **时点错一个交易日** —— 与本批其它条目同族("时点不清的值被当成实时值用")。
    """
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        _d = datetime.strptime(dstr, "%Y-%m-%d").date()
        _p = get_holiday_calendar().prev_trading_day("US", _d)
        if _p is not None:
            return _p.isoformat()
    except Exception as _e:
        print(f"  [warn] 上一交易日推算失败 ({_e}) → 时点口径退回原 date")
    return dstr


def _gather_market_corroboration() -> Dict[str, Any]:
    """市场佐证: 板块轮动/异动. **每条材料都必须自带数据时点口径**。

    2026-07-28 (板块级联批, 用户裁"要动"): 主源升级为 deep_sector_rotation.json —
    DA 每日**盘前 (ET 08:00, cron 20:00 CST 的 DA 链)** 生成, 含 sector_changes 数字,
    但此前仓内零消费者 ("有人生成没人吃"): 07-27 它明确标注 Technology -1.44% 最弱,
    UE 却因只读机械影子 capture 而对芯片满舵 BUY (TSM -95 实证)。capture 降为 fallback。

    ⚠ 2026-09-14 时点诚实化批 (DEB-20260914 §一 P1-3, 用户裁"诚实化+补时点标签") 修正了
    本函数此前**三处失实/无时点**的声明 (全部实测取证, 非读注释推断):
      ① 原写"每日盘前生成 (07:42 ET)" —— 实为 DA 在 **ET 08:00** 写 ⇒ UE 的 **da_feed
         早跑 (ET 07:40)** 结构上**永远早于生产者 20 分钟**, 该窗次次落到 capture 回退;
      ② 原写"周更机械影子 capture" —— 该 capture (**ET 00:30** 日更) 的 `date` 是写入
         时刻的 ET 日期, 而当时取到的行情是**上一交易日收盘** ⇒ 数字的真实数据日是
         `date` 的前一交易日 (由 `_prev_trading_day_str` 还原, 见其 docstring);
      ③ 原写"盘中脉搏 (若存在且新鲜) 一并注入" + 渲染标签"盘中板块脉搏(实时)"——
         **生产两窗 (ET 07:40 / 09:10) 结构上永不成立** (脉搏由 sector_pulse.py 在
         ET 10:00-15:30 写), 唯一能命中"当日"的是 **ET 12:30 的 `--midday` 只读影子**
         ⇒ 该输入从未进过任何生产指令。此处**只修"声明失实"与"时点不清", 不放宽守卫**:
         盘前不注入上一交易日脉搏 —— DEB-20260729 §十 作者按其价值判断建议"不修"
         (pulse 的真归宿是盘中板块 veto, 至今仍挂在参数冻结后排队), 本批保留该**价值
         判断**, 但"宣称有而实际没有"的静默必须消除 (丢弃时显式留痕)。
    返回 dict 的 `date` = **数据实际所属交易日** (不是采集时刻), `caliber`/`caliber_pulse`
    为可读口径串, 由调用方渲染进行首 (改渲染 ⇒ 已升 PROMPT_VERSION v2.13 + ue_brief_v4)。
    """
    out: Dict[str, Any] = {}
    # ── 主源: DA 日更盘前板块轮动 (守卫: 必须是**今日**盘前那一版) ──
    try:
        _rot_p = _ROOT / "data" / "deep_sector_rotation.json"
        if _rot_p.exists():
            _rot = json.loads(_rot_p.read_text())
            if str(_rot.get("date", "")) == _today():
                _chg = _rot.get("sector_changes") or {}
                _gen = str(_rot.get("generated_at", "") or "")
                out = {
                    "date": _rot.get("date"),
                    "caliber": (f"DA 日更盘前轮动 {_rot.get('date')} 盘前生成"
                                + (f" (generated_at {_gen})" if _gen else "")),
                    "strong_sectors": [f"{s}({_chg.get(s, '?')}%)" for s in (_rot.get("strongest") or [])[:3]],
                    "weak_sectors": [f"{s}({_chg.get(s, '?')}%)" for s in (_rot.get("weakest") or [])[:3]],
                    "movers": [],
                    "mover_symbols": [],
                }
            else:
                # 不许静默替换: 回退源与主源的**口径不同** (见 docstring ①②), 替换必须可见。
                print(f"  [info] 主源 deep_sector_rotation date={_rot.get('date') or '缺失'} "
                      f"!= 今日 {_today()} → 落 capture 回退 (da_feed 07:40 窗结构上早于 "
                      f"DA 08:00 生产, 见 DEB-20260914 §一 P1-3)")
    except Exception as _re:
        print(f"  [warn] deep_sector_rotation 读取失败: {_re}")
    # ── 盘中脉搏补充 (sector_pulse.py 产出, 盘中 ET 10:00-15:30 每 30min 覆写) ──
    try:
        _pulse_p = _ROOT / "data" / "sector_pulse.json"
        if _pulse_p.exists():
            _pu = json.loads(_pulse_p.read_text())
            _pu_date = str(_pu.get("date", "") or "")
            if _pu_date == _today() and _pu.get("ranked"):
                out.setdefault("date", _pu_date)
                out["caliber_pulse"] = f"当日盘中脉搏 (asof {_pu.get('asof_et', '?')} ET)"
                out["intraday_pulse"] = _pu["ranked"]
            else:
                # 显式留痕 (原为 `except Exception: pass` 式的静默): 盘前窗日复一日走到这里,
                #   不打印就无人知道这条输入从未生效过。
                print(f"  [info] 盘中脉搏未注入: date={_pu_date or '缺失'} != 今日 {_today()} "
                      f"(脉搏在 ET 10:00-15:30 写, UE 生产窗 07:40/09:10 结构上不可得; "
                      f"仅 midday 只读影子可见 — DEB-20260914 §一 P1-3)")
    except Exception as _pe:
        print(f"  [warn] 盘中脉搏读取失败 (降级, 无脉搏行): {_pe}")
    if out:
        return out
    # ── fallback: 机械影子 capture (原逻辑) ──
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
    # [2026-09-14 P1-3] 未来日期守卫: `_age_days` 为**负数**时原逻辑直接放行
    #   (守卫只写 `> 4`) ⇒ writer 时钟可疑时, 未来日期的数据会被当成"最新"消费。
    #   与上面陈旧守卫同一意图的对称面: 宁可落回新闻+硬事件, 不消费无法定位时点的数据。
    if _age_days < 0:
        print(f"  [warn] 市场佐证 capture 日期在未来 (date={_cap_date}, {_age_days}d) → 跳过, "
              f"writer 时钟可疑; LLM 落回新闻+硬事件推理")
        return {}
    # [2026-09-14 P1-3] 时点还原: capture 的 `date` 是**写入时刻的 ET 日期** (cron ET 00:30),
    #   而那一刻的行情是**上一交易日收盘** ⇒ 数据真实所属交易日 = date 的前一交易日。
    #   旧代码把这个采集日当数据时点透传给 published_at (实测差一个交易日) 且渲染进 brief
    #   的文本里**没有任何时点** —— 模型无从判断是今日还是上一交易日。
    _cap_session = _prev_trading_day_str(_cap_date)
    return {
        "date": _cap_session,
        "caliber": (f"机械 capture 回退源 (采集于 {_cap_date} ET 盘前) — "
                    f"**数据为上一交易日 {_cap_session} 收盘涨跌**, 非今日盘中"),
        "strong_sectors": [f"{s['etf']}({s['name']}) {s['change_pct']:+}%"
                           for s in last.get("strong_sectors", [])],
        "weak_sectors": [f"{s['etf']}({s['name']}) {s['change_pct']:+}%"
                         for s in last.get("weak_sectors", [])],
        "movers": [f"{r['symbol']} {r['direction']} {r['change_pct']:+}%"
                   for r in last.get("surfaced", [])],
        # [P2.1 修正 2026-09-13] 结构化 ticker 与渲染串**同源同处**产出 — 消费方不再需要
        #   从 "ESTC UP +19.3%" 这种成品串里反解 symbol (反解口径曾与真实格式不符, 见
        #   _ticker_symbols 注释)。渲染串本身一字未改 ⇒ brief 正文零漂移。
        "mover_symbols": [str(r.get("symbol") or "") for r in last.get("surfaced", [])],
    }


def _gather_hard_events(news: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[ADR-017] 复用 CatalystClassifier(纯关键词, use_llm=False) 把已拉取新闻分类为
    结构化硬事件 → clue brief 的 [硬事件] 段, 供 V2 自评 HARD_EVENT。失败降级返回 []。

    [P2.1] 返回结构化事实 (`_fact`) 而非裸行 —— 渲染文本逐字不变, 但 severity/event_type
    与关联 ticker 同时进材料索引, 引证核验不再靠行前缀猜来源。"""
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
        out.append(_fact(
            "hard_event_classified",
            f"[{ev.severity}/{ev.event_type}] {head} → 关联: {tks or '(板块传导)'}",
            symbols=[str(t) for t in (ev.tickers or [])],
            event_key=f"{ev.severity}/{ev.event_type}"))
    return out


def _gather_earnings_events() -> List[Dict[str, Any]]:
    """[ADR-018] 复用 EarningsCalendar (Alpha Vantage, 仅 1 次日历调用+内置缓存) 注入
    财报硬事件: 近期已发布(EARNINGS) + 临近(EARNINGS_UPCOMING 时点风险)。
    不用逐股 history 接口(5/min 限流不稳)。失败降级返回 []。

    [P2.1] 返回结构化事实; `report_date` 落 `published_at` (财报日是时效锚, 此前渲染后即丢)。"""
    try:
        from app.data.earnings_calendar import EarningsCalendar
    except Exception as e:
        print(f"  [warn] 财报日历不可用 (降级): {e}")
        return []
    out: List[Dict[str, Any]] = []
    try:
        ec = EarningsCalendar()
        for e in ec.get_recently_reported(days_back=2)[:8]:
            out.append(_fact(
                "earnings_calendar",
                f"[HIGH/EARNINGS] {e.get('ticker','')} 财报已发布 {e.get('report_date','')}"
                f"({e.get('time_of_day','')}) → 关注财报后动量",
                symbols=[e.get("ticker", "")], published_at=e.get("report_date"),
                event_key="EARNINGS"))
        for u in ec.get_watchlist_earnings(days_ahead=3)[:8]:
            out.append(_fact(
                "earnings_calendar",
                f"[MEDIUM/EARNINGS_UPCOMING] {u.get('ticker','')} 财报临近 {u.get('report_date','')}"
                f"({u.get('days_until','?')}d) → 时点风险",
                symbols=[u.get("ticker", "")], published_at=u.get("report_date"),
                event_key="EARNINGS_UPCOMING"))
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


def _gather_insider_clusters() -> List[Dict[str, Any]]:
    """[ADR-018] 读今日已持久化的内部人集群报告 (insider_daily_{date}.json, 无网络).
    集群买入=看多硬事件。无今日文件/无信号 → []。

    [P2.1] 返回结构化事实 (本段是 31/32 误杀样本的来源, 见 _BRIEF_EVIDENCE 注释)。"""
    try:
        path = _ROOT / "data" / "reports" / f"insider_daily_{_today()}.json"
        if not path.exists():
            return []
        d = json.loads(path.read_text())
        by_sym = (d.get("stats") or {}).get("by_symbol") or {}
        out: List[Dict[str, Any]] = []
        for sym, cnt in by_sym.items():
            t = str(sym).replace("US.", "")
            out.append(_fact("insider_cluster",
                             f"[HIGH/INSIDER_CLUSTER] {t} 内部人集群买入信号 x{cnt} → 看多",
                             symbols=[t], published_at=_today(),
                             event_key="INSIDER_CLUSTER"))
        return out
    except Exception as e:
        print(f"  [warn] 内部人集群读取失败 (降级): {e}")
        return []


def _gather_event_symbols_file() -> List[Dict[str, Any]]:
    """[P2' 2026-07-21] 读 gen_event_symbols.py 的当日产出 (data/event_symbols_{ET}.json).

    给 V2 提供"预消化的事件驱动标的+原因"作为佐证原料 — MARKET_CORROBORATED 需要
    可引用证据, 事件清单正是证据源; 不动 prompt 克制标准. 文件不存在/为空 → []。
    同一天 DA --symbols 与本段共用同一文件, 双源原料同源。

    [P2.1] 返回结构化事实。"""
    try:
        path = _ROOT / "data" / f"event_symbols_{_today()}.json"
        if not path.exists():
            return []
        d = json.loads(path.read_text())
        out: List[Dict[str, Any]] = []
        for e in (d.get("event_symbols") or [])[:10]:
            t = str(e.get("symbol", "")).replace("US.", "")
            reasons = "; ".join(str(r)[:60] for r in (e.get("reasons") or [])[:2])
            out.append(_fact("event_symbols",
                             f"- {t} (score={e.get('score', 0)}): {reasons}",
                             symbols=[t], published_at=_today(),
                             event_key="event_symbol"))
        return out
    except Exception as e:
        print(f"  [warn] 事件标的文件读取失败 (降级): {e}")
        return []


def _gather_macro_brief() -> List[Dict[str, Any]]:
    """[RFC-076] 读当日宏观披露 brief (data/ue_macro_brief_{ET}.json)。

    生产方: scripts/ue_macro_brief_collect.py (cron 21:05 CST, FRED 新披露派生)。
    无文件/无新披露 → [] (静默降级, 与既有线索源同构)。

    [P2.1] 返回结构化事实; 宏观行**无具名标的** (`symbols=[]`) — 这正是 P2.6
    "无合格具名候选但有宏观材料时仍允许一次 UE 扫描" 的判据来源。"""
    try:
        path = _ROOT / "data" / f"ue_macro_brief_{_today()}.json"
        if not path.exists():
            return []
        d = json.loads(path.read_text())
        lines = d.get("brief_lines") or []
        return [_fact("macro_brief", str(x)[:300], published_at=_today(),
                      event_key="macro") for x in lines][:8]
    except Exception as e:
        print(f"  [warn] 宏观brief读取失败 (降级): {e}")
        return []


def _gather_peer_surge() -> List[Dict[str, Any]]:
    """[P3 2026-07-22] 盘前峰群异动段 — 读 premarket_peer_shadow.jsonl 当日记录 (record-only 影子),
    把板块级盘前异动作为 clue 供 V2 引用 (evidence_ref 可用 peer_surge).
    定位: 输入端扩容, 非放行信号 — "峰群直接跟随"已被 7 天回测否决 (-8.48%, 唯一 CONFIRM 日 -1.32%),
    此段仅把高波动板块票送入分析池, gate 照常把关. 文件缺失/当日无记录 → 跳过 (降级无碍).
    回滚: 配置 universe_expansion.clue_peer_surge: false.

    [P2.1] 返回结构化事实; 峰群成分股落 `symbols` (此前只在渲染串里, 无法按标的核验引证)."""
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

        def _fmt(members: List[str]) -> tuple:
            cells = []
            used: List[str] = []
            for sym in members:
                v = snap.get(sym)
                if isinstance(v, dict) and v.get("pre_change") is not None:
                    cells.append(f"{sym}{float(v['pre_change']):+.1f}%(量比{float(v.get('volume_ratio', 0)):.2f})")
                    used.append(str(sym))
            return " ".join(cells), used

        # 2026-07-22: 多板块组 (groups + groups_members 自描述成分); 旧记录无 groups → 回落顶层存储组 (向后兼容)
        groups = rec.get("groups") or {}
        if groups:
            members_map = rec.get("groups_members") or {}
            out = []
            for name, gd in groups.items():
                line, members = _fmt([str(s).upper() for s in (members_map.get(name) or [])])
                if line:
                    out.append(_fact(
                        "peer_surge",
                        f"{name}峰群 verdict={(gd or {}).get('verdict', '?')}: {line}",
                        symbols=members, published_at=today,
                        event_key=f"peer_surge/{name}"))
            return out
        cells: List[str] = []
        used_syms: List[str] = []
        for sym, v in snap.items():
            if isinstance(v, dict) and v.get("pre_change") is not None:
                cells.append(f"{sym}{float(v['pre_change']):+.1f}%(量比{float(v.get('volume_ratio', 0)):.2f})")
                used_syms.append(str(sym))
        if not cells:
            return []
        return [_fact("peer_surge",
                      f"存储峰群 verdict={rec.get('group_verdict', '?')}: " + " ".join(cells),
                      symbols=used_syms, published_at=today,
                      event_key="peer_surge/storage")]
    except Exception as e:
        print(f"  [warn] 峰群影子读取失败 (降级): {e}")
        return []


def _gather_premarket_rank_clues() -> List[Dict[str, Any]]:
    """[2026-08-29 盘前榜过滤规则, 用户裁"按建议执行"] 富途全市场盘前榜质量门候选 → 线索。

    数据链: premarket_enrichment.ensure_premarket_rank_candidates() 幂等保证当日文件
      (data/premarket_rank_candidates_{ET}.json) 存在 — 盘前榜原始 50 只/向已过
      质量门 (盘前成交额≥$5M / 价格≥$5 / 市值≥$2B / ETF剔除 / |涨幅|>25% 打标不入围),
      每向 ≤5 候选。本函数只负责把过滤结果渲染成 clue 行。
    定位: 输入端扩容 (同 peer_surge), 非放行信号 — 候选进分析池, gate 照常把关。
      涨幅带影子打标: EXTREME 候选数据保留在 candidates json 攒样 (预注册判据照跑),
      默认不渲染进 prompt (U1①, clue_render_extreme_band: true 可回滚恢复展示)。
    文件缺失/当日无候选 → 触发一次采集兜底 (盘前 21:10 CST 首跑即产文件);
      采集失败降级 []。回滚: 配置 universe_expansion.clue_premarket_rank: false。

    [P2.1] 返回结构化事实; 候选 symbol 落 `symbols`, 极端带行另记 event_key 以便审计区分。"""
    try:
        from app.data.premarket_enrichment import ensure_premarket_rank_candidates
        path = ensure_premarket_rank_candidates(_today())
        if not path or not path.exists():
            return []
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("date") != _today():
            return []  # 陈旧文件 (跨日残留) 不消费
        out: List[Dict[str, Any]] = []

        def _fmt_side(side: Dict, label: str) -> List[Dict[str, Any]]:
            lines: List[Dict[str, Any]] = []
            for c in (side.get("candidates") or []):
                tags = ",".join(c.get("tags") or [])
                tag_s = f" [{tags}]" if tags else ""
                mcap_b = round(float(c.get("market_cap_usd", 0)) / 1e9, 1)
                lines.append(_fact(
                    "premarket_rank",
                    f"- {c['symbol']} 盘前{label}#{c.get('rank', '?')} "
                    f"{c['change_pct']:+.1f}% 盘前成交${float(c.get('turnover_usd', 0)) / 1e6:.1f}M "
                    f"市值${mcap_b}B{tag_s}",
                    symbols=[c.get("symbol", "")], published_at=_today(),
                    event_key=f"premarket_rank/{label}"))
            # [U1① PROPOSAL-20260905, 2026-09-05] 极端带行默认不渲染 — "展示但不入围"
            #   是自相矛盾的输入 (模型仍会当软候选锚定, 追高语境污染源)。攒样不丢:
            #   extreme 数据保留在 candidates json (采集端), 预注册判据照跑。
            #   回滚: universe_expansion.clue_render_extreme_band: true。
            if bool(_cfg("universe_expansion.clue_render_extreme_band", False)):
                for c in (side.get("extreme") or []):
                    lines.append(_fact(
                        "premarket_rank",
                        f"- {c['symbol']} 盘前{label}极端涨幅带({c['change_pct']:+.1f}%) "
                        f"[仅记录不入围: 追高=买顶风险, 预注册攒样中]",
                        symbols=[c.get("symbol", "")], published_at=_today(),
                        event_key=f"premarket_rank_extreme/{label}"))
            return lines

        _g = _fmt_side(d.get("gainers") or {}, "涨")
        _l = _fmt_side(d.get("losers") or {}, "跌")
        # [U1② PROPOSAL-20260905] 多空对称截断 — gainer 行数 ≤ loser 行数
        #   (losers=0 → gainers 全不渲染; 纯涨幅日=追榜日, 预注册声明此特性)。
        #   依据: 涨幅榜单边注入把扫描语境推向 LONG 偏向。回滚: clue_side_symmetry: false。
        if bool(_cfg("universe_expansion.clue_side_symmetry", True)):
            _g = _g[:len(_l)]
        out.extend(_g)
        out.extend(_l)
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


def _build_feedback_table(days: int = 7, max_rows: int = 10) -> List[Dict[str, Any]]:
    """v2.2 (P3): 本源近 N 日浮现→fwd 结果小表 (供 prompt 负反馈注入)。

    口径: playbook_instructions 中 source=universe_expansion 的方向指令,
    fwd = fwd_return_intraday 优先 (t1 兜底)。失败返回 []。

    ⚠️ 契约 (U1 2026-09-11): fwd_return_* 各列**已经是方向签名后**的指令收益 —
      写侧 app/playbook/ic_tracker.py 统一乘 sign (BUY=+1 / SELL_SHORT=-1),
      故正数=该方向指令赚钱、负数=亏钱, 与标的自身涨跌无关。读侧**不得**再乘方向,
      否则 SHORT 行被二次取负 = 标签整体反转 (实测自 2026-07-25 起 UE SHORT 有 fwd 共
      66 条、66/66 反转, 其中 41 条做对的空头被标"推错", 直接喂给 prompt 里"连续推错的
      同标的+同方向不得再次浮现"那条抑制规则 ⇒ 系统性地驱逐正确空头、强化错误空头)。
      本函数只做 fraction→percent 换算。

    [P2.1] 返回结构化事实 —— 抑制规则要求"若确有新证据, evidence_ref 必须引用新证据",
      即本表**是**模型会引用的材料, 必须进索引 (此前它以 `- ` 开头侥幸未被前缀谓词误杀)。
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
        for r in rows:
            f = r["fwd_return_intraday"] if r["fwd_return_intraday"] is not None else r["fwd_return_t1"]
            # [U5 2026-09-05] 滤"待回填"行: 无结果行对 prompt 是纯占位噪音
            #   (负反馈注入的意义=真实盈亏方向, 待回填行没有方向信息)。
            if f is None:
                continue
            adj = float(f) * 100  # 已含方向符号 (见上方契约), 只做 fraction→percent
            # [U1b 零收益三态 2026-09-11] 修前 adj==0 落 else 被判 ✗, 而下游表头把"推错"
            #   定义为负收益、抑制规则写"连续推错(连续负收益)…不得再次浮现" ⇒ 一次持平被当
            #   一次推错喂进抑制规则 (自我矛盾, 非策略选择)。
            #   判定用**精确** adj==0, 不得放宽为显示舍入 round(adj,1)==0: 后者会把真实非零的
            #   小收益误判为中性 (实测 2026-08-11 US.CVX 真值 +0.0249%, 显示 "+0.0%")。
            res = f"{adj:+.1f}%" + (" ✓" if adj > 0 else " ✗" if adj < 0 else " ＝")
            _dir = "LONG" if r["direction"] == "BUY" else "SHORT"
            out.append(_fact("feedback_table",
                             f"- {r['date']} {r['symbol'].replace('US.', '')} {_dir}: {res}",
                             symbols=[r["symbol"]], published_at=r["date"],
                             event_key=f"feedback/{_dir}"))
            if len(out) >= max_rows:
                break
        return out
    except Exception as e:
        print(f"  [warn] 负反馈表构建失败 (降级无注入): {e}")
        return []


# ── [P2.2 2026-09-12, proud-slope-falcon] 输入具名标的的可交易性初筛 ──────────
# 计划 P2.2: "对输入中明确的 ticker 批量取轻量快照, 先做 P1 初筛。不可交易公司的
#   事件可保留为 context_only, 用于产业链推导, 但不能直接当合格交易对象。"
# 病灶: 修前 clue brief 把 8-K / 财报 / 内部人 / 盘前榜 / 峰群里出现的 ticker 原样送进
#   模型, 模型无从知道其中哪些**根本不能交易** (仙股价 / 低波 / 权证优先股), 于是把
#   分析预算花在注定被 inject 侧资格门拒掉的标的上 (白烧 LLM), 或把不可交易公司的
#   事件当成直接交易机会。
# 设计取舍 (三条, 每条都对应计划里的一句硬约束):
#   ① **附加段, 不改既有行** — 渲染成 brief 末尾一个独立三态清单, 原有材料行逐字不动
#      (P2 验收②"背景事件和榜外发现未被错误清空"; 也让本改动的正文漂移面可被零漂移测试钉住)。
#   ② **不是白名单** — 只标注"输入里已具名的标的", 模型仍可提出材料外的新 ticker,
#      那些在 inject 侧走同一资格门 (P1.5)。计划 P2.3 明写"不把输入集合当封闭股票白名单"。
#   ③ **扫描期口径 ≠ 执行期口径** — 见 _scan_phase_policy 的理由。
# 全程 fail-open: 任何异常 ⇒ 返回 status=unavailable 且**不追加本段**, 材料零改动。
#   "筛不了"绝不能变成"清空材料"。
# 回滚: universe_expansion.input_prescreen=false。
_LAST_INPUT_PRESCREEN: Dict[str, Any] = {}
INPUT_PRESCREEN_SOURCE = "ue_input_prescreen"
# [P2.2 2026-09-12] 这些源里的 symbols **不计入**待初筛的具名集合 (理由见
#   _build_clue_brief 内注释): feedback_table = 近7日浮现**回顾**, 配套 prompt 规则是
#   "连续推错的同标的同方向不得再浮现" ⇒ 反候选清单, 不该占今日初筛名额。
_PRESCREEN_EXCLUDED_SOURCES = frozenset({"feedback_table"})


def _scan_phase_policy(base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """[P2.2] 扫描期资格口径 = P1 执行口径**减去**执行时微结构门。

    为什么不能照搬 `load_candidate_policy()`: 它带 `max_quote_age_sec=90`(报价新鲜度)、
    `quote_gate_enabled`(点差/bid-ask)、`borrow_gate_enabled`(借券) —— 那三样是**下单前**
    的实时行情门。UE scan 在 ET 09:10 盘前跑, 距入场窗约 2 小时, 用 90 秒新鲜度去筛会把
    输入标的几乎全判 `stale_quote` ⇒ 初筛退化成"全清", 正是 P2 验收②要防的事。
    计划 P1.2 也明写"执行前仍复核有效性, 不能拿盘前快照当实时行情" ⇒ 微结构门留在
    inject/执行侧 (P1.5 已接), 扫描期只判**结构性可交易性**:
      标的形状 (权证/优先股) / 价格地板 / 低 ATR (低波)。

    `max_quote_age_sec` 设成一个不可能达到的大值而不是删键 —— `evaluate_candidate`
    读 `policy["max_quote_age_sec"]`, 删键会退回默认 90 (更严), 与意图相反。

    ⚠ 该值必须是**有限** float, 不能用 `float("inf")`: `screen_candidates` 会把 policy
      整体喂给 `_fingerprint()` → `json.dumps(..., allow_nan=False)` 计算 `input_hash`,
      `Infinity` 不是合法 JSON ⇒ 抛 ValueError ⇒ 被本函数的 fail-open 兜住后
      **每次初筛都静默退化成 status=unavailable** (材料不标注, 缺陷隐形)。
      实测教训, 由 test_scan_phase_policy_is_json_fingerprintable 钉住。
    """
    from app.playbook.candidate_eligibility import load_candidate_policy

    policy = dict(base if base is not None else load_candidate_policy())
    policy["quote_gate_enabled"] = False
    policy["borrow_gate_enabled"] = False
    # 1e12 秒 ≈ 3.2 万年: 有限 (可 JSON 指纹化) 且任何真实报价龄都不可能超过。
    policy["max_quote_age_sec"] = 1e12
    policy["phase"] = "ue_input_prescreen"
    return policy


# ── [P2.2-fix 2026-09-15, DEB-20260910 §20 后续] 扫描期报价时戳**键名归一** ──────────
# 病灶 (2026-09-14 两轮首跑实测): 30/30 受检标的 `missing_fields` 恒含 `source_asof`、
#   `quote_age_sec=None` ⇒ `evaluate_candidate` 的 PASS 要求"零 missing" ⇒ **`tradeable` 恒空**,
#   P2 验收①的正向半边从未生效 (负向 `low_atr`/`price_floor` 正常, 零误杀)。
# 根因不是缺数据, 是**键名错配**: 供应商报价字典带的是 `timestamp` (unix; = 我方取数/入 push
#   缓存的时刻, 见 `app/data/futu_lv3_provider.py:1098` 与 `:1056`), 而
#   `app/playbook/candidate_eligibility.py:_source_time` 只认 `source_asof`/`update_time`。
# ⚠ 为什么**只在扫描期**归一、不去改 SSOT 的 `_source_time`: inject 侧 (:3289) 按
#   `status != "REJECT"` **真过滤**, 且 inject 用 `load_candidate_policy()` 的
#   `max_quote_age_sec=90` ⇒ 全局补时戳会**新激活 `stale_quote` 拒绝并真丢候选** =
#   交易路径行为变更, 按 AGENT.md C4「行为变更一律影子先行」须另立影子批 (已登记待裁)。
#   扫描期口径本就**不含**新鲜度门 (`_scan_phase_policy` 把 `max_quote_age_sec` 设成 1e12),
#   所以这里补时戳只让"缺数"不再冒充"未知", **不产生任何新拒绝、不改任何门**。
# 语义诚实: `timestamp` 是"我方观测到该报价的时刻", **不是**交易所撮合时点 ⇒ 归一后在
#   summary 里写 `source_asof_basis` 标明口径, 供审计区分 (不当成行情时点用)。
# fail-open: 预取异常 ⇒ 返回 None, `screen_candidates` 自己取价 (行为逐字退回修前, 不炸本段)。
def _prescreen_quotes_with_asof(router, symbols, now=None):
    try:
        from app.playbook.candidate_eligibility import _SYMBOL, canonical_symbol
        canon = [canonical_symbol(s) for s in (symbols or []) if str(s).strip()]
        # 与 screen_candidates 自取价时的过滤逐字同口径 (candidate_eligibility:186
        #   `_SYMBOL.fullmatch`) —— 材料里混进的非标的形状 token 不得进 router,
        #   否则一个坏代码可能拖垮整批取价 (修前它们本就被挡在取价之外)。
        canon = [s for s in canon if _SYMBOL.fullmatch(s)]
        raw = router.get_batch_quotes(canon) if router and canon else {}
    except Exception as exc:
        print(f"  [warn] 初筛报价预取失败 (退回 screen_candidates 自取价): {exc}")
        return None, 0
    out: Dict[str, Any] = {}
    normalized = 0
    for key, quote in (raw or {}).items():
        if not isinstance(quote, dict):
            continue
        row = dict(quote)
        if not (row.get("source_asof") or row.get("update_time")):
            stamp = row.get("timestamp")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 0:
                row["source_asof"] = float(stamp)
                normalized += 1
        out[key] = row
    print(f"  [prescreen] 报价命中 {len(out)}/{len(canon)}, 时戳键归一 {normalized} 条")
    return out, normalized


def _prescreen_input_symbols(symbols, *, router=None, now=None, policy=None,
                             max_symbols=None,
                             ordering: str = "material_index_order") -> Dict[str, Any]:
    """对材料索引里**已具名**的 ticker 做批量轻量初筛, 返回三态清单 (纯读, 不落库)。

    三态语义 (与 P1 资格判定同口径, 不新造判据):
      `tradeable`    PASS    —— 结构性可交易, 可作直接交易候选;
      `context_only` REJECT  —— 明确不可交易 (形状/价格地板/低ATR), 事件只作背景与
                               产业链推导, **不得**当直接交易候选 (附 reasons 可归因);
      `unknown`      UNKNOWN —— 缺价/缺ATR/缺行情时戳 ⇒ 不猜, 按未定处理,
                               执行侧仍受原风险门约束 (缺数 ≠ 拒绝, 计划 P1.3)。
    """
    from app.playbook.candidate_eligibility import make_batch_id, screen_candidates

    ordered = list(dict.fromkeys(str(s).strip().upper().replace("US.", "")
                                 for s in (symbols or []) if str(s).strip()))
    if max_symbols is None:
        max_symbols = int(_cfg("universe_expansion.input_prescreen_max_symbols", 30))
    summary: Dict[str, Any] = {
        "status": "ok", "named_in_materials": len(ordered),
        "checked": 0, "overflow_skipped": ordered[max_symbols:],
        "tradeable": [], "context_only": [], "unknown": [],
        "policy_version": None, "phase": "ue_input_prescreen",
        "ordering": ordering,
    }
    ordered = ordered[:max_symbols]
    if not ordered:
        summary["status"] = "no_named_symbol"
        return summary
    try:
        policy = policy if policy is not None else _scan_phase_policy()
        summary["policy_version"] = policy.get("version")
        router = router if router is not None else _build_router()
        # [P2.2-fix 2026-09-15] 扫描期报价时戳键归一 (理由/边界见函数头注释)。
        #   返回 None 时传 quotes=None ⇒ screen_candidates 自取价, 与修前逐字同行为。
        _quotes, _n_asof = _prescreen_quotes_with_asof(router, ordered, now)
        # [P2.2-fix③ 2026-09-15] `now` 必须**在取价之后**定型。供应商 `timestamp` 写的是
        #   取价完成时刻 (`futu_lv3_provider` 各处 `time.time()`), 若 `now` 早于它,
        #   `_source_time` 算出的 `quote_age` 为负 ⇒ 超过 5s 容差即被
        #   `evaluate_candidate` 判 `future_quote` **REJECT** —— 比修前的 UNKNOWN 更糟:
        #   "缺数"被升级成"明确不可交易"写进 brief, 反向误导模型。批量取 30 只一旦走
        #   降级源 (AlphaVantage 免费档每只 sleep 12s) 远超 5s。
        #   调用方显式传 now (测试/回放) 时保持原值, 不擅改观测时点。
        now = now or _now_et()
        batch_id = make_batch_id(INPUT_PRESCREEN_SOURCE, now)
        if _quotes is not None:
            summary["quotes_prefetched"] = len(_quotes)
            summary["source_asof_normalized"] = _n_asof
            if _n_asof:
                summary["source_asof_basis"] = "provider_observation_timestamp"
        rows = screen_candidates([{"symbol": s} for s in ordered], router,
                                 source=INPUT_PRESCREEN_SOURCE, batch_id=batch_id,
                                 now=now, policy=policy, quotes=_quotes)
    except Exception as exc:
        print(f"  [warn] 输入初筛失败 (fail-open, 材料零改动): {exc}")
        summary.update(status="unavailable", error=type(exc).__name__)
        return summary
    for row in rows:
        scr = row.get("candidate_screening") or {}
        bare = str(scr.get("symbol") or "").replace("US.", "")
        status = str(scr.get("status") or "UNKNOWN").upper()
        entry = {"symbol": bare, "status": status,
                 "reasons": list(scr.get("reasons") or []),
                 "missing_fields": list(scr.get("missing_fields") or []),
                 "price": scr.get("price"),
                 "screening_atr_ratio": scr.get("screening_atr_ratio"),
                 "atr_reason": scr.get("atr_reason")}
        summary["checked"] += 1
        if status == "PASS":
            summary["tradeable"].append(bare)
        elif status == "REJECT":
            summary["context_only"].append(entry)
        else:
            summary["unknown"].append(entry)
    return summary


# ── [P2.6 2026-09-12, proud-slope-falcon] 输入可用性: 空转材料不花 LLM 预算 ──
SCAN_SKIP_NO_INPUT = "no_actionable_input"


def _scan_input_is_actionable(index, prescreen=None, fingerprints=None) -> Dict[str, Any]:
    """[P2.6] 纯函数: 这次输入值不值得花一次 LLM 调用 (不做 IO, 不落库)。

    计划 P2.6 原文: "无合格具名候选且无有效宏观/传导材料时跳过 LLM 并写空批次;
    有发现价值的背景材料则仍允许一次 UE 扫描。" 两句相撞时**一律偏向不跳** ——
    P2 验收②("背景事件和榜外发现未被错误清空")比省一次调用重要: 漏跳只浪费预算,
    错跳是静默丢掉当天全部 UE 发现, 且账上看起来像"当日无机会"。故判据刻意窄到
    只剩"材料索引里一条事实都没有"这一退化态:
      · 具名标的哪怕**全被判 context_only 也不跳** (P2.2: 不可交易公司的事件保留作
        产业链推导, 属计划原文里"有发现价值的背景材料");
      · 任何一条无 symbols 的事实 (宏观/板块/regime) 都算传导材料 ⇒ 不跳;
      · 初筛降级 (status=unavailable) 一律不跳 —— 缺数 ≠ 没有机会 (计划 P1.3);
      · **brief 管线未被观测到**一律不跳 (见下 `fingerprints`);
      · 人工线索 (`manual_clue`) 在 _build_clue_brief 里就登记成一条无 symbols 的事实
        ⇒ 天然落进 macro 桶, 人明确要求扫的时候绝不会被判空转跳过。

    `fingerprints` = `_LAST_BRIEF_FINGERPRINTS`。它只由 `_build_clue_brief` 在**成功构建
    的最后一行**赋值, 且函数入口先清空 ⇒ 非空 = "本次 brief 确实走完了 gather 管线,
    材料索引可信"。为空 = brief 来自别处 (测试桩整体替换 _build_clue_brief / 被 patch /
    构建中途抛错) ⇒ 此时索引里的"空"不能读成"今天真没材料", 必须不跳。
    实测教训 (2026-09-12 离线全链): tests/playbook/offline_chain_driver.py 把
    `_build_clue_brief` 换成返回冻结串的桩, 索引自然为空, 守卫据此跳过了 LLM
    ⇒ 整条 scan→inject→execute 链断在第一节 (18 例 setup ERROR)。缺这道观测前提,
    "索引空"与"没跑 gather"在代码里是同一个形态。

    真正会被跳过的只有"全部输入源同时空转": 此时旧行为是拿一份几乎空白的 brief 去问
    模型, 模型只能凭参数记忆编标的 (幻觉), 且照样烧满 max_tokens=16000。
    """
    facts = [e for e in (index or []) if str(e.get("text") or "").strip()]
    named = [e for e in facts if (e.get("symbols") or [])]
    macro = [e for e in facts if not (e.get("symbols") or [])]
    ps_status = str((prescreen or {}).get("status") or "")
    degraded = ps_status not in ("", "ok", "no_named_symbol")
    observed = bool(fingerprints)
    has_material = bool(named or macro)
    if not observed:
        reason = "brief_pipeline_not_observed"
    elif named and macro:
        reason = "has_named_and_macro_material"
    elif named:
        reason = "has_named_material"
    elif macro:
        reason = "has_macro_material"
    elif degraded:
        reason = "input_degraded_fail_open"
    else:
        reason = "empty_material_index"
    return {
        "actionable": bool(has_material or degraded or not observed),
        "reason": reason,
        "brief_observed": observed,
        "material_count": len(facts),
        "named_material_count": len(named),
        "macro_material_count": len(macro),
        "prescreen_status": ps_status or None,
    }


def _render_input_prescreen(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把初筛结果渲染成 brief 附加段的事实行 (空结果 ⇒ 不追加任何行)。"""
    if not summary or summary.get("status") not in ("ok", "no_named_symbol"):
        return []
    if not (summary.get("tradeable") or summary.get("context_only")
            or summary.get("unknown")):
        return []
    lines: List[Dict[str, Any]] = []
    tradeable = list(summary.get("tradeable") or [])
    if tradeable:
        lines.append(_fact("input_prescreen",
                           "可作直接交易候选: " + ", ".join(tradeable),
                           symbols=tradeable, published_at=_today(),
                           event_key="prescreen_tradeable"))
    for entry in (summary.get("context_only") or []):
        why = ",".join(entry.get("reasons") or []) or "ineligible"
        lines.append(_fact(
            "input_prescreen",
            f"仅背景(context_only, 不得当直接交易候选): {entry['symbol']} — {why}",
            symbols=[entry.get("symbol")], published_at=_today(),
            event_key="prescreen_context_only"))
    for entry in (summary.get("unknown") or []):
        why = ",".join(entry.get("missing_fields") or []) or "unknown"
        lines.append(_fact(
            "input_prescreen",
            f"未定(缺数据, 不猜; 执行侧仍受原风险门约束): {entry['symbol']} — 缺 {why}",
            symbols=[entry.get("symbol")], published_at=_today(),
            event_key="prescreen_unknown"))
    if summary.get("overflow_skipped"):
        lines.append(_fact(
            "input_prescreen",
            f"初筛名额上限外未判定 (按未定处理, 不因此排除): "
            f"{', '.join(summary['overflow_skipped'][:20])}",
            symbols=summary["overflow_skipped"], published_at=_today(),
            event_key="prescreen_overflow"))
    return lines


def _build_clue_brief(manual_clue: str = "") -> str:
    # [U8 2026-08-25 / P2.1 / P2.5 2026-09-12] 材料索引与指纹 — 供 _evidence_grounded /
    #   _classify_evidence_citation 做"引证是否真实存在于今日输入"的代码核验。
    #   索引在 **gather 现场**逐条登记 (见 _take_facts); _LAST_BRIEF_MATERIALS 在函数
    #   末尾由索引派生, 不再有行前缀猜测。
    # ⚠ 状态失效必须排在**任何可能抛的语句之前** (原先排在 `_cfg(...)` 之后): 构建在
    #   第一行就抛时, 上一次的 _BRIEF_EVIDENCE/_LAST_BRIEF_FINGERPRINTS 会原地留存,
    #   等于宣称"这次输入和上次一样" —— P2.6 的空转守卫正是读这份索引判"今天有没有材料",
    #   留着旧值会让它按上次的材料决定是否调 LLM。
    global _LAST_BRIEF_MATERIALS, _BRIEF_EVIDENCE, _LAST_BRIEF_FINGERPRINTS
    _LAST_BRIEF_MATERIALS = []
    _BRIEF_EVIDENCE = []
    _LAST_BRIEF_FINGERPRINTS = {}
    hours = int(_cfg("universe_expansion.news_lookback_hours", 24))
    parts = [f"日期: {_today()} (ET)"]
    health: List[str] = []  # [新鲜度 2026-07-22] 各输入源健康摘要 (日志可观测, 区分"无事件"与"链路断")

    if manual_clue:
        parts.append(f"\n[人工线索]\n{manual_clue.strip()}")
        _take_facts([_fact("manual_clue", manual_clue.strip(), event_key="manual")],
                    source="manual_clue")

    news = _gather_news(hours)

    # [ADR-017/018] 结构化硬事件段 (config 驱动, 传导优先级最高, 供 V2 自评 HARD_EVENT)
    hard: List[Dict[str, Any]] = []
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
            _8k_lines = [_fact("sec_8k",
                               f"8-K {r['ticker']}: {'; '.join(r['item_labels'])} "
                               f"(filed {r['updated'][:16]})",
                               symbols=[r.get("ticker", "")],
                               published_at=str(r.get("updated") or "")[:16] or None,
                               event_key="sec_8k") for r in _8k[:15]]
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
            _dg_lines = [_fact("sec_13dg",
                               f"举牌 {r['ticker']}: {r['form']}"
                               f"{' (主动举牌, 看多先验强)' if r['form'].startswith('SC 13D') else ' (被动持仓)'} "
                               f"(filed {r['updated'][:16]})",
                               symbols=[r.get("ticker", "")],
                               published_at=str(r.get("updated") or "")[:16] or None,
                               event_key=str(r.get("form") or "sec_13dg"))
                         for r in _dg[:10]]
            hard += _dg_lines
            health.append(f"13DG:{len(_dg_lines)}")
            _corro_syms.update(str(r.get("ticker", "")).upper().replace("US.", "")
                               for r in _dg)  # [UE-B1影子] 交叉印证源②
        except Exception as _dg_e:
            print(f"  [warn] 13D/G 直连失败 (降级): {_dg_e}")
            health.append("13DG:err")
    # [RFC-076 2026-09-04] 宏观披露段: FRED 新披露 (非农/CPI/失业率当日发布) →
    #   宏观→板块传导线索。生产方: scripts/ue_macro_brief_collect.py (cron 21:05)。
    #   文件缺失/无新披露 → 空段, 行为与之前一致。回滚: 配置设 false 或删 cron 行。
    if bool(_cfg("universe_expansion.clue_macro_brief", True)):
        _mb = _gather_macro_brief()
        hard += _mb
        health.append(f"宏观:{len(_mb)}")
    if hard:
        parts.append("\n[硬事件 (已分类, 传导优先级最高)]")
        parts.extend(_take_facts(hard, source="hard_event"))

    # [P2' 2026-07-21] 事件驱动标的清单 (gen_event_symbols 当日产出, 与 DA --symbols 同源).
    #   给 V2 可引用的佐证原料; 文件缺失=当日未生成 → 跳过, 行为与之前一致. 回滚: 配置设 false.
    if bool(_cfg("universe_expansion.clue_event_symbols", True)):
        es = _gather_event_symbols_file()
        health.append(f"事件标的:{len(es)}")
        if es:
            parts.append("\n[今日事件驱动标的 (事件清单, 可作 evidence_ref 引用)]")
            parts.extend(_take_facts(es, source="event_symbols"))

    # [P3 2026-07-22] 盘前板块峰群 (影子测量转 clue 输入; 仅热度参考, 跟随策略已回测否决).
    if bool(_cfg("universe_expansion.clue_peer_surge", True)):
        ps = _gather_peer_surge()
        health.append(f"峰群:{len(ps)}组")
        if ps:
            parts.append("\n[盘前板块峰群 (影子实测, 热度参考非跟随信号)]")
            parts.extend(_take_facts(ps, source="peer_surge"))

    # [2026-08-29 盘前榜过滤规则] 富途全市场盘前榜质量门候选 (成交额/价格/市值/ETF门后每向≤5)。
    #   定位同峰群: 输入端扩容非放行信号; 极端涨幅带已剔除只展示。回滚: 配置设 false。
    if bool(_cfg("universe_expansion.clue_premarket_rank", True)):
        pr = _gather_premarket_rank_clues()
        health.append(f"盘前榜:{len(pr)}条")
        if pr:
            parts.append("\n[盘前全市场榜质量门候选 (已过成交额≥$5M/价格≥$5/市值≥$2B门, 候选非信号)]")
            parts.extend(_take_facts(pr, source="premarket_rank"))

    mkt = _gather_market_corroboration()
    health.append(f"佐证:{mkt.get('date') or '无/陈旧'}")
    if mkt:
        parts.append("\n[市场佐证 (板块轮动/异动)]")
        # [2026-09-14 P1-3] 时点口径**必须出现在模型看得到的正文里**: 上述三条源的采集
        #   时刻/数据所属交易日各不相同 (DA 日更盘前轮动 / capture 的上一交易日收盘 /
        #   当日盘中脉搏), 旧渲染只给光秃秃的板块涨跌行 ⇒ 模型无从判断时点。
        _cal = str(mkt.get("caliber") or "").strip()
        _cal_p = str(mkt.get("caliber_pulse") or "").strip()
        _cal_pre = f" [{_cal}]" if _cal else ""
        _mkt_facts: List[Dict[str, Any]] = []
        if mkt.get("strong_sectors"):
            _mkt_facts.append(_fact("market_corroboration",
                                    f"轮入板块{_cal_pre}: " + ", ".join(mkt["strong_sectors"]),
                                    published_at=mkt.get("date"),
                                    event_key="sector_rotation_in"))
        if mkt.get("weak_sectors"):
            _mkt_facts.append(_fact("market_corroboration",
                                    f"轮出板块{_cal_pre}: " + ", ".join(mkt["weak_sectors"]),
                                    published_at=mkt.get("date"),
                                    event_key="sector_rotation_out"))
        if mkt.get("movers"):
            _mkt_facts.append(_fact("market_corroboration",
                                    f"宇宙外异动{_cal_pre}: " + ", ".join(mkt["movers"][:12]),
                                    # symbols 取 gather 现场的结构化字段 (与渲染串同处产出),
                                    #   不再从成品串反解 —— 见 _ticker_symbols 注释的实测缺陷。
                                    symbols=(mkt.get("mover_symbols") or [])[:12],
                                    published_at=mkt.get("date"),
                                    event_key="out_of_universe_movers"))
        if mkt.get("intraday_pulse"):
            _mkt_facts.append(_fact("market_corroboration",
                                    f"盘中板块脉搏 [{_cal_p or '当日盘中'}] (实时): "
                                    + ", ".join(str(x) for x in mkt["intraday_pulse"][:11]),
                                    published_at=mkt.get("date"),
                                    event_key="intraday_sector_pulse"))
        parts.extend(_take_facts(_mkt_facts, source="market_corroboration"))

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
                # [P2.1] 事实登记与渲染同源: published_at/symbols 在此刻还在手上,
                #   渲染完就只剩字符串, 事后无从复原 (原实现正是这样丢的)。
                parts.extend(_take_facts(
                    [_fact("news_headline",
                           f"- {title}{_age_tag}"
                           + (f"  [{', '.join(syms)}]" if syms else ""),
                           symbols=syms,
                           published_at=str(n.get("published_at") or "") or None,
                           event_key="news")], source="news_headline"))
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
            parts.append("\n[本源近7日浮现回顾 (方向调整后前瞻收益: ✓=推对盈利 / ✗=推错亏损 / "
                         "＝=零收益持平, ＝不计入推错)]")
            parts.extend(_take_facts(fb, source="feedback_table"))
            parts.append("规则: 对上表中连续推错(连续负收益)的 同标的+同方向, 除非今日材料中出现"
                         "【新增量证据】(新事件/新数据), 否则不得再次浮现; 若确有新证据, evidence_ref 必须引用新证据。"
                         "表中 ＝ (零收益) 不是推错, 不得计入「连续推错」或据以触发本规则。")

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
    # [U1③ PROPOSAL-20260905, 2026-09-05] 交叉命中集落盘 — U3 tier 闸影子判定的
    #   "市场佐证交叉命中"消费源: scan 写 (此处), inject 读。失败降级不阻断
    #   (影子闸读不到文件 = 交叉命中判 False, 与文件缺失语义一致)。
    try:
        (_ROOT / "data" / f"ue_corro_syms_{_today()}.json").write_text(
            json.dumps({"date": _today(), "symbols": sorted(_corro_syms)},
                       ensure_ascii=False), encoding="utf-8")
    except Exception as _corro_e:
        print(f"  [warn] 交叉命中集落盘失败 (降级, 不阻断): {_corro_e}")

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
            parts.extend(_take_facts(
                [_fact("market_regime", _regime_line, published_at=_today(),
                       event_key="regime")], source="market_regime"))
        except Exception as _rg_scan_e:
            print(f"  [warn] regime 摘要注入失败 (降级, 不阻断): {_rg_scan_e}")

    # ── [P2.2] 输入具名标的可交易性初筛 (附加段; 材料一条不删, fail-open) ──
    #   具名集合取自**材料索引**而非再解析正文 —— 索引在 gather 现场登记了 symbols,
    #   这里直接消费, 不需要从渲染串里把 ticker 抠回来 (那又是一次"事后猜")。
    #   放在最后: 此时全部 gather 已完成, 具名集合才是完整的。
    global _LAST_INPUT_PRESCREEN
    _LAST_INPUT_PRESCREEN = {}
    if bool(_cfg("universe_expansion.input_prescreen", True)):
        #   具名集合的取法有两处刻意选择 (都是"别把最该判的挤掉"):
        #   ① **排除 feedback_table** — 那是"近7日浮现回顾", 配套 prompt 规则本就是
        #      "连续推错的同标的同方向不得再浮现"。它是**反候选**清单, 拿它占初筛名额
        #      等于让历史标的挤掉今日事件标的。
        #   ② **保材料索引顺序**而非字典序 — 索引顺序 = gather 顺序 = 传导优先级顺序
        #      (硬事件→财报→内部人→…→回顾→regime)。名额上限截断时字典序会把 Z 开头的
        #      今日热点整批丢进 overflow, 保序丢的才是优先级尾巴。与计划 P1.4"避免先丢掉
        #      最强空头"同一原则: 先定序, 后截断。
        #   ③ [P2.2-fix 2026-09-15] **名额排序改按"被几条材料具名"降序** (同数保首现顺序)。
        #      病灶 (2026-09-14 两轮首跑实测): 只保材料索引顺序截断 30 个后, 模型 surfaced
        #      的标的与受检集合重叠仅 **4/10** 与 **1/6** —— 未受检的全落在 overflow_skipped
        #      (含当日真成交的 AAOI/CRWD) ⇒ 初筛看的和模型选的几乎是两批票, 验收①落空。
        #      判据: 被多条**独立**材料交叉具名 = 更可能被提名, 优先占名额; 同提及数按首现顺序
        #      (= gather 传导优先级) 稳定排序 ⇒ 不丢②的优先级语义, 只把多源热点提进名额内。
        #      刻意**不动** max_symbols=30 (一次一个变量; 覆盖面够不够等实跑数据再裁)。
        _mentions: Dict[str, int] = {}
        _named: List[str] = []
        for _ev in _BRIEF_EVIDENCE:
            if str(_ev.get("source") or "") in _PRESCREEN_EXCLUDED_SOURCES:
                continue
            for _sym in (_ev.get("symbols") or []):
                _key = str(_sym).strip().upper().replace("US.", "")
                if not _key:
                    continue
                if _key not in _mentions:
                    _named.append(_key)
                _mentions[_key] = _mentions.get(_key, 0) + 1
        _first_seen = {_sym: _i for _i, _sym in enumerate(_named)}
        _named.sort(key=lambda _s: (-_mentions[_s], _first_seen[_s]))
        _ps = _prescreen_input_symbols(
            _named, ordering="mention_count_desc_then_first_seen")
        _ps_lines = _render_input_prescreen(_ps)
        if _ps_lines:
            parts.append("\n[输入具名标的可交易性初筛 (与注入侧同一资格口径; "
                         "本段只标注输入里已具名的标的, 不是可选股票白名单 — "
                         "你仍可提出材料外的新标的, 它们会在注入前接受同一资格判定)]")
            parts.extend(_take_facts(_ps_lines, source="input_prescreen"))
        _LAST_INPUT_PRESCREEN = _ps
        health.append(f"输入初筛:{_ps.get('checked', 0)}判/"
                      f"{len(_ps.get('tradeable') or [])}可交易/"
                      f"{len(_ps.get('context_only') or [])}仅背景/"
                      f"{len(_ps.get('unknown') or [])}未定"
                      + ("" if _ps.get("status") == "ok" else f"({_ps.get('status')})"))
    # [P2.1 2026-09-12] 材料行由**索引派生** — 索引在 gather 现场逐条登记, 与正文同源。
    #   原实现在此处把 parts 按 "\n" 拆行再用 `not startswith("[")` 排除节标题, 该谓词
    #   同时误杀了 `[CRITICAL/…]`/`[HIGH/EARNINGS]`/`[HIGH/INSIDER_CLUSTER]` 等**事实行**
    #   (实测 32 条 grounded=False 中 31 条即此因, 见 _BRIEF_EVIDENCE 注释)。
    #   现节标题/日期行本就不登记为事实 ⇒ 无需任何前缀猜测。
    _LAST_BRIEF_MATERIALS = [e["text"] for e in _BRIEF_EVIDENCE]
    _brief_text = "\n".join(parts)
    # [P2.5] 三指纹与材料构成随构建落定 — cmd_scan 直接取, 不再事后重算 (事后重算
    #   就等于又给"猜"留了一个入口)。
    _LAST_BRIEF_FINGERPRINTS = _brief_fingerprints(parts, _BRIEF_EVIDENCE, _brief_text)
    return _brief_text


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
            # [P2.4] 留原始引证 — 下方 enforce=true 会把 ev 清空, 三态分类必须用清空前的值,
            #   否则"未过核验"会被误记成"模型没给引证"(no_ref), 两种缺陷在账上混成一类。
            _ev_raw = ev
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
            # ── [P2.4 2026-09-12] 引证三态 (纯附加留痕, 不改上面任何判定) ──
            #   消费方登记见 _classify_evidence_citation 注释 (AGENT.md P7 军规)。
            #   fail-safe: 分类器自身异常**不落 not_found** —— 那是对模型"编造引证"的
            #   指控, 传感器坏了不能赖模型; 改落 transmission_assumed (保守: 不宣称直接
            #   关联) 并把 matched_source 标成 classifier_error 供归因。与 _ev_grounded
            #   的"核验器故障 → 不误杀"同一处置哲学。
            try:
                _cit = _classify_evidence_citation(
                    _ev_raw, sym, brief_evidence_index())
            except Exception:
                _cit = {"state": ("no_ref" if not _ev_raw else "transmission_assumed"),
                        "evidence_id": None, "matched_symbols": [],
                        "matched_source": "classifier_error" if _ev_raw else None}
            item["evidence_citation_state"] = _cit.get("state")
            item["evidence_id"] = _cit.get("evidence_id")
            item["evidence_matched_symbols"] = list(_cit.get("matched_symbols") or [])
            item["evidence_matched_source"] = _cit.get("matched_source")
            item["raw_confidence"] = round(raw_conf, 3)
            item["clamped"] = clamped
            if _tier_fallback:
                item["_tier_fallback"] = True  # LLM 输出非法 tier, 被代码兜底替换
            item["confidence"] = round(conf, 3)
            # v2.2 (2026-07-25, P1 可证伪字段): 纯落库供判分, 不进任何执行逻辑
            item["invalidation"] = str(it.get("invalidation", ""))[:120]
            # [U10 2026-08-25 建议④] 结构化失效触发器解析 — 白名单与
            #   tripwire_checker._get_structured_triggers 同契约 (同一消费方):
            #   metric∈{收盘价,量比,5日涨跌,盘前涨跌幅} & operator∈{<,>} & value可float。
            #   非法条目静默丢弃 (宁漏语义, 与消费端一致)。
            _it_metrics = ("收盘价", "量比", "5日涨跌", "盘前涨跌幅")
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


# ── [P2.4 2026-09-12, proud-slope-falcon] 引证三态: "材料里有这句" ≠ "因果已核实" ──
# 病灶: `evidence_grounded` 是**一个 bool**, 只回答"这段引证出自今日材料吗"。它答不了
#   计划 P2.4 要的那一问: 命中的那条材料**自己就带这个标的**吗 (直接关联实体), 还是
#   模型从宏观/板块/同业材料**推**到这只票 (间接传导假设)。两者在账上同一个 True ⇒
#   "引证已核验"被下游读成"因果已核验", 而传导假设恰恰是最需要人工抽检的一类。
#   错实体 (拿 A 公司的事件给 B 票背书) 也一并落进 True 侧, 在既有传感器上完全不可见。
# 修法: 材料索引 (P2.1) 每条事实都带 symbols ⇒ 把同一套滑窗匹配从"过没过阈"改成
#   "过阈的是哪一条", 再按那条事实的 symbols 是否含本标的分态。
# **纯附加留痕** (刻意的边界, 计划 P2.4 明写"本轮不借此打开现有 tier/grounding/confirm
#   强制门"): 不改 `_evidence_grounded` 的算法与取值, 不改 Track A 夹逼, 不改 confidence,
#   不动 `evidence_grounding_enforce`。三态只落库供人工抽检与 P4 readout 分层。
# 消费方登记 (AGENT.md P7): ①`evidence_grounding_enforce` 开启裁决的人工抽检分层
#   (预注册判据"5 交易日抽检误杀率 <10%"此前只有 bool, 无法区分误杀与真传导);
#   ②P4 `scripts/unit_cell_daily_readout.py` 全漏斗的 LLM 段分层;
#   ③`scripts/shadow_field_cardinality.py` ue_scan.evidence_citation_state 基数自检。
CITATION_STATES = ("no_ref", "not_found", "entity_linked", "transmission_assumed")


def _match_evidence_fact(ev: str, index, k: int = 6, ratio: float = 0.5):
    """返回与引证最匹配的**那一条事实** (dict) 或 None — 与 `_evidence_grounded` 同参同算法。

    差别只在返回"哪一条"而不是"过没过阈": 分实体/传导必须知道命中的事实带哪些 symbols。
    同分取索引里更靠前的一条 (gather 顺序即传导优先级顺序) ⇒ 结果可复现。
    """
    ev_n = _norm_text(ev)
    if not ev_n or not index:
        return None
    best, best_score = None, 0.0
    for entry in index:
        mn = _norm_text(str(entry.get("text") or ""))
        if not mn:
            continue
        if len(ev_n) < k or len(mn) < k:
            score = 1.0 if (ev_n in mn or mn in ev_n) else 0.0
        else:
            grams = {mn[i:i + k] for i in range(len(mn) - k + 1)}
            ev_grams = [ev_n[i:i + k] for i in range(len(ev_n) - k + 1)]
            score = sum(1 for g in ev_grams if g in grams) / len(ev_grams)
        if score > best_score:
            best, best_score = entry, score
    return best if best_score >= ratio else None


def _classify_evidence_citation(ev: str, symbol: str, index) -> Dict[str, Any]:
    """[P2.4] 引证三态判定 (纯函数, 不做 IO, 不改任何既有判定)。

    状态枚举 (量纲=枚举, 取值域固定为 CITATION_STATES):
      `no_ref`               模型没给引证 —— 已被 Track A 夹逼接管, 此处只归类不改判;
      `not_found`            给了引证但材料索引里定位不到 = 编造/未知编号 ⇒ **不得**记作
                             已核验证据 (计划 P2.4 原文);
      `entity_linked`        命中的事实**自己就带这个标的** = 直接关联实体;
      `transmission_assumed` 引证真实存在, 但那条事实的 symbols 里没有本标的 = 间接传导
                             **假设** (宏观/板块/同业推导, 或错实体)。编号存在 ≠ 因果已核实。

    `evidence_matched_symbols` 是为"错实体 vs 真传导"的人工分辨而落的: 二者同落
      transmission_assumed, 但命中事实带 symbols (→ 可能是错实体/同业传导) 与不带
      symbols (→ 纯宏观传导) 在抽检时处置不同。`evidence_id=None` 且状态为
      transmission_assumed 表示**跨行引证**: 整段话由多条材料拼成, 池化匹配过阈但
      定位不到单条事实 ⇒ 保守不宣称直接关联。
    """
    sym = str(symbol or "").strip().upper().replace("US.", "")
    ev = str(ev or "").strip()
    out: Dict[str, Any] = {"state": "no_ref", "evidence_id": None,
                           "matched_symbols": [], "matched_source": None}
    if not ev:
        return out
    entries = [e for e in (index or []) if str(e.get("text") or "").strip()]
    if not entries:
        out["state"] = "not_found"
        return out
    hit = _match_evidence_fact(ev, entries)
    if hit is None:
        # 单条定位失败, 但池化匹配 (既有传感器口径) 可能仍过阈 = 跨行拼引。
        #   此时**不**回退成 not_found: 那会与 evidence_grounded=True 自相矛盾;
        #   也不宣称 entity_linked: 定位不到事实就无从判断实体关联 ⇒ 保守落传导假设。
        pooled = False
        try:
            pooled = _evidence_grounded(ev, [str(e.get("text") or "") for e in entries])
        except Exception:
            pooled = False
        out["state"] = "transmission_assumed" if pooled else "not_found"
        return out
    out["evidence_id"] = hit.get("evidence_id")
    out["matched_source"] = hit.get("source")
    matched = [str(x).strip().upper().replace("US.", "")
               for x in (hit.get("symbols") or []) if str(x).strip()]
    out["matched_symbols"] = matched
    out["state"] = "entity_linked" if sym and sym in matched else "transmission_assumed"
    return out


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


def _annotate_prescreen_eligibility(surfaced: List[Dict[str, Any]],
                                    ps_in: Optional[Dict[str, Any]] = None) -> None:
    """[2026-09-18 登记项 P3] 给每个浮现标的标注**输入侧资格口径** (纯标注, 零行为变更)。

    背景 (2026-09-18 UE 复核实锤): brief 的「[输入具名标的可交易性初筛]」只覆盖材料里
    **已具名**的标的 (`named_in_materials`), 而 brief 同时明说"你仍可提出材料外的新标的,
    它们会在注入前接受同一资格判定"; 材料外标的在**输入侧零资格信息** ⇒ 模型可把它当
    "可交易"浮现, 直到 21:10 inject 的 ATR/价格地板预检才被拒。9/18 实况: ZIM 由航运业
    传导浮现 (材料未具名, `evidence_citation_state=transmission_assumed`), 播报成"可交易"
    却在低ATR闸被拒 (0.0262<0.03) —— 拒收本身是设计内且可归因, 缺的是**读数能提前看见它**。

    本函数对未覆盖的浮现标的补跑**同一** `_prescreen_input_symbols` 三态, 结果写回
    surfaced 记录 (prescreen_scope / prescreen_status / prescreen_reasons), 供推送逐条
    标记与扫描期计数使用。**不参与 inject 判定** (inject 侧本就有同款预检, 双写会造成
    两份判据漂移); fail-open: 任何异常 → 不标注、不崩溃 (缺数 ≠ 拒绝, 计划 P1.3)。
    """
    try:
        ps_in = ps_in if ps_in is not None else (_LAST_INPUT_PRESCREEN or {})
        # 覆盖集合: 输入初筛三态里出现过的标的 (tradeable 是裸符号串, 另两态是 entry dict)
        covered: Dict[str, Dict[str, Any]] = {}
        for _sym in (ps_in.get("tradeable") or []):
            covered[str(_sym).strip().upper()] = {"status": "PASS", "reasons": []}
        for _key, _status in (("context_only", "REJECT"), ("unknown", "UNKNOWN")):
            for _e in (ps_in.get(_key) or []):
                if isinstance(_e, dict):
                    covered[str(_e.get("symbol") or "").strip().upper()] = {
                        "status": _status, "reasons": [r for r in (_e.get("reasons") or []) if r]}

        uncovered = [str(s.get("symbol") or "").strip().upper() for s in surfaced
                     if str(s.get("symbol") or "").strip()
                     and str(s.get("symbol") or "").strip().upper() not in covered]
        extra: Dict[str, Dict[str, Any]] = {}
        if uncovered:
            _ps2 = _prescreen_input_symbols(uncovered, ordering="surfaced_order")
            for _sym in (_ps2.get("tradeable") or []):
                extra[str(_sym).strip().upper()] = {"status": "PASS", "reasons": []}
            for _key, _status in (("context_only", "REJECT"), ("unknown", "UNKNOWN")):
                for _e in (_ps2.get(_key) or []):
                    if isinstance(_e, dict):
                        extra[str(_e.get("symbol") or "").strip().upper()] = {
                            "status": _status, "reasons": [r for r in (_e.get("reasons") or []) if r]}

        n_ext_reject = 0
        for s in surfaced:
            sym = str(s.get("symbol") or "").strip().upper()
            if sym in covered:
                s["prescreen_scope"] = "in_materials"
                s["prescreen_status"] = covered[sym]["status"]
                s["prescreen_reasons"] = covered[sym]["reasons"]
            elif sym in extra:
                s["prescreen_scope"] = "material_external"
                s["prescreen_status"] = extra[sym]["status"]
                s["prescreen_reasons"] = extra[sym]["reasons"]
                if extra[sym]["status"] == "REJECT":
                    n_ext_reject += 1
            else:
                s["prescreen_scope"] = "material_external"
                s["prescreen_status"] = "UNKNOWN"
                s["prescreen_reasons"] = []
        if uncovered:
            print(f"  [prescreen-annotate] 材料外浮现 {len(uncovered)} 只 → 补跑同一资格口径: "
                  + " / ".join(f"{k}={v['status']}" for k, v in extra.items())
                  + (f" (结构不可交易 {n_ext_reject} 只)" if n_ext_reject else ""))
        return
    except Exception as e:  # 标注失败不阻断 scan 主流程
        print(f"  [warn] 资格口径标注失败 (fail-open, 零行为变更): {e}")
        return


def _scan_fail_alert(reason: str, midday: bool = False) -> None:
    """[DEB-20260829 第12轮·用户裁"同意"] scan 失败飞书告警 — cron && 链断的可见性补丁.

    9/1 事故: 双臂 8000 tok 截断 → scan exit 1 → && 链静默断, inject/push 未跑,
    直到人工次日发现。失败即推飞书 (与 DEB-20260901 判读报告同通道)。
    best-effort: 告警自身失败只打印, 绝不影响主链返回码。"""
    try:
        from app.notifier.feishu import send_text
        _chain = ("午间影子 scan (独立运行, 无下游 && 链)" if midday
                  else "晚间 scan — cron && 链已断: inject/push 未跑, 当晚无 UE 指令")
        send_text(f"⚠️ UE scan 失败 [{_today()}]: {reason}\n{_chain}\n排查: logs/universe_scan.log")
    except Exception as e:
        print(f"  [warn] scan 失败告警发送失败 (不阻断): {e}")


def cmd_scan(manual_clue: str = "", midday: bool = False, da_feed: bool = False) -> int:
    if not _is_us_trading_day_today():
        print(f"[scan] {_today()} 非美股交易日 (周末/节假日), 跳过扫描")
        return 0
    from app.playbook.candidate_eligibility import make_batch_id, POLICY_VERSION
    window = "midday" if midday else ("da_feed" if da_feed else "premarket")
    now = _now_et()
    batch = {"batch_id": make_batch_id("UE", now), "date": _today(),
             "scan_window": window, "started_at": now.isoformat(),
             "policy_version": POLICY_VERSION}
    path = _MIDDAY_PATH if midday else _SCAN_PATH
    _append_jsonl(path, {**batch, "type": "batch_start", "batch_status": "pending"})
    result = {}
    try:
        rc = _cmd_scan_impl(manual_clue, midday, da_feed, batch, result)
    except Exception as exc:
        _append_jsonl(path, {**batch, "type": "batch_end", "batch_status": "failed",
                            "error": str(exc), "finished_at": _now_et().isoformat()})
        _scan_fail_alert(f"执行异常: {exc}", midday=midday)
        return 1
    if rc != 0:
        # 失败批次必须落盘并与"无机会"区分 (latest_premarket 据此拒绝消费)。
        _append_jsonl(path, {**batch, "type": "batch_end", "batch_status": "failed",
                            "finished_at": _now_et().isoformat()})
        return rc
    if not result.get("scan_written"):
        # rc=0 但 impl 未落 scan 记录 (如 midday 影子关闭) → 显式落一条非 scan 终态,
        #   使本次尝试作废早先候选, 而不是静默让 inject 回用旧批次。
        _append_jsonl(path, {**batch, "type": "batch_end",
                            "batch_status": "complete_no_scan",
                            "finished_at": _now_et().isoformat()})
    return rc


def _cmd_scan_impl(manual_clue, midday, da_feed, batch, result) -> int:
    # [P1.5] 窗口标签单一真源: 记录里的 scan_window 与包装器选路径必须同源。
    _window = "midday" if midday else ("da_feed" if da_feed else "premarket")
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
        _scan_fail_alert(f"无法导入 call_llm: {e}", midday=midday)
        return 1

    # [ADR-016] live scan prompt 版本 config 驱动 (v1|v2); 默认 v1 安全回退,
    # 设 universe_expansion.prompt_version=v2 → 事件驱动+强制引证+Track A 夹逼 (更克制/更稳)。
    _pv = str(_cfg("universe_expansion.prompt_version", "v1")).strip().lower()
    _use_v2 = _pv in ("v2", "v2.0")
    _system = SYSTEM_PROMPT_V2 if _use_v2 else SYSTEM_PROMPT
    print(f"[scan] prompt_version={'v2' if _use_v2 else 'v1'} (config: universe_expansion.prompt_version)")

    # ── [P2.6] 材料索引空转 ⇒ 不调 LLM, 直接落**完成态空批** ──
    #   与另两种终态互斥可区分: ①本态 llm_skipped=true (根本没问模型);
    #   ②empty_batch=true 且 llm_skipped 缺省 (问了, schema 通过但 surfaced 空 = 当日无机会);
    #   ③batch_end/batch_status=failed (调用失败/异常)。inject 见 ①② 均正常收工不回用旧候选。
    _actionable = _scan_input_is_actionable(brief_evidence_index(),
                                            _LAST_INPUT_PRESCREEN,
                                            _LAST_BRIEF_FINGERPRINTS)
    if not _actionable["actionable"] and bool(
            _cfg("universe_expansion.scan_skip_on_empty_input", True)):
        print(f"[scan] 输入无可用材料 ({_actionable['reason']}: 具名 "
              f"{_actionable['named_material_count']} / 宏观 "
              f"{_actionable['macro_material_count']}) → 跳过 LLM, 落空批次 "
              f"(完成态, 非失败; 不拿近空白 brief 诱发幻觉)")
        _append_jsonl(_MIDDAY_PATH if midday else _SCAN_PATH, {
            "type": "scan",
            "date": _today(),
            "scanned_at": _now_et().isoformat(),
            "scan_window": _window,
            "batch_id": batch.get("batch_id"),
            "batch_status": "complete",
            "started_at": batch.get("started_at"),
            "policy_version": batch.get("policy_version"),
            "source_type": SOURCE_TYPE,
            "strategy_id": STRATEGY_ID,
            "discovery_source": DISCOVERY_SOURCE,
            "info_source": INFO_SOURCE,
            "model_used": None,
            "prompt_version": PROMPT_VERSION if _use_v2 else "v1",
            "prompt_hash": PROMPT_HASH_V2 if _use_v2 else None,
            "brief_template_version": BRIEF_TEMPLATE_VERSION,
            "brief_template_hash": _LAST_BRIEF_FINGERPRINTS.get("brief_template_hash"),
            "brief_content_hash": _LAST_BRIEF_FINGERPRINTS.get("brief_content_hash"),
            "brief_material_count": _actionable["material_count"],
            "brief_material_stats": _LAST_BRIEF_FINGERPRINTS.get("brief_material_stats"),
            "input_prescreen": dict(_LAST_INPUT_PRESCREEN or {}),
            "input_actionability": _actionable,
            "surfaced": [],
            "empty_batch": True,
            "llm_skipped": True,
            "skip_reason": SCAN_SKIP_NO_INPUT,
            "note": ("P2.6: 材料索引空转 ⇒ 未调 LLM (省预算 + 防幻觉)。与"
                     "'问了模型但 surfaced 空'及'batch_end/failed'三者互斥可区分; "
                     "回滚=universe_expansion.scan_skip_on_empty_input 置 false"),
        })
        result["scan_written"] = True
        return 0

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
        # 2026-09-17 (用户裁「按建议执行」②): 16000→24000 — glm-5.3 强制思考链烧满
        #   16k 预算致 schema 失败 4/4=100% (llm_audit 11686/11719/11721/11751),
        #   官方 Max Output 131072 有充足余量. 单轮成本上限 24k×0.004169≈$0.10 可控.
        max_tokens=24000,
        schema_required={"surfaced": list},
        json_mode=True,
    )
    if not res or not res.success:
        err = getattr(res, "error", "unknown") if res else "no_result"
        print(f"[scan] LLM 调用失败 (不落库, 不下单): {err}")
        _scan_fail_alert(f"LLM 调用失败: {err}", midday=midday)
        return 1

    parsed = res.response if isinstance(res.response, dict) else {}
    surfaced = _normalize_surfaced(parsed.get("surfaced"), with_tier=_use_v2)
    if not surfaced:
        # [P1.5 2026-09-12] 空扫描必须落批次 — 修前直接 return 0 不写记录:
        #   ①inject 取"当日最后一条 scan" → 静默回用早先(可能已过期)候选;
        #   ②"当日无机会"与"批次失败"在账上无法区分。
        #   空批次 = 正常结束 (batch_status=complete, surfaced=[], empty_batch=true),
        #   与失败批次 (batch_end/failed) 分开; inject 见空批次即正常收工, 不回用旧候选。
        print("[scan] LLM 未浮现有效标的 (schema 通过但 surfaced 空) → 落空批次")
        _append_jsonl(_MIDDAY_PATH if midday else _SCAN_PATH, {
            "type": "scan",
            "date": _today(),
            "scanned_at": _now_et().isoformat(),
            "scan_window": _window,
            "batch_id": batch.get("batch_id"),
            "batch_status": "complete",
            "started_at": batch.get("started_at"),
            "policy_version": batch.get("policy_version"),
            "source_type": SOURCE_TYPE,
            "strategy_id": STRATEGY_ID,
            "discovery_source": DISCOVERY_SOURCE,
            "info_source": INFO_SOURCE,
            "model_used": getattr(res, "model_used", None),
            "prompt_version": PROMPT_VERSION if _use_v2 else "v1",
            "prompt_hash": PROMPT_HASH_V2 if _use_v2 else None,
            "surfaced": [],
            "empty_batch": True,
            "note": "LLM schema 通过但 surfaced 空 — 当日无机会 (非失败批次)",
        })
        result["scan_written"] = True
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

    # [2026-09-18 登记项 P3] 输入侧资格口径标注 (纯标注): 材料外浮现标的补跑同一
    #   `_prescreen_input_symbols` 三态 → 推送能提前逐条标"⛔资格预筛未过"。
    #   零行为变更 (不改 direction/inject 判定), fail-open。
    _annotate_prescreen_eligibility(surfaced)

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
    #   da_feed 早跑 (2026-09-09) 同理跳过: 它的唯一职责是给 DA 供新鲜 SHORT 候选,
    #   IC 快照的唯一写手仍是 21:10 的 premarket 跑 — 否则 19:40 的薄盘前数据会先占位,
    #   若 21:10 跑失败, 被污染的快照就成了当日 canonical。
    if not midday and not da_feed:
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
        # §十三: 评估脚本按窗口切片; da_feed (2026-09-09) = 19:40 CST 早跑, 只供 DA,
        #   不进 inject/push/confirm/score (四者均按 premarket 或 today[-1] 取行)。
        "scan_window": _window,
        # [P1.5 2026-09-12] 批次身份/时效/策略版本随完成记录落盘 —
        #   inject 只消费最新有效 premarket 批次, 时效走 candidate_scan_max_age_hours。
        "batch_id": batch.get("batch_id"),
        "batch_status": "complete",
        "started_at": batch.get("started_at"),
        "policy_version": batch.get("policy_version"),
        "source_type": SOURCE_TYPE,
        "strategy_id": STRATEGY_ID,
        "discovery_source": DISCOVERY_SOURCE,
        "info_source": INFO_SOURCE,
        "model_used": getattr(res, "model_used", None),
        "prompt_version": PROMPT_VERSION if _use_v2 else "v1",
        # [U1] 内容指纹 — 版本戳失真防护 (旧记录无此字段, 按日期切分)
        "prompt_hash": PROMPT_HASH_V2 if _use_v2 else None,
        # [P2.5 2026-09-12] brief 侧指纹与材料构成 — 补 §11.6-item6 缺口:
        #   prompt_hash 只覆盖 SYSTEM_PROMPT_V2, brief 改动此前对归因戳完全不可见。
        #   brief_template_version 是试验分段键; brief_template_hash 防"忘了升版号";
        #   brief_content_hash 逐run 唯一供复现; brief_material_stats 是输入构成的
        #   按源计数 (P4 readout 与 W2 基数自检的消费面)。
        "brief_template_version": BRIEF_TEMPLATE_VERSION,
        "brief_template_hash": _LAST_BRIEF_FINGERPRINTS.get("brief_template_hash"),
        "brief_content_hash": _LAST_BRIEF_FINGERPRINTS.get("brief_content_hash"),
        "brief_material_count": _LAST_BRIEF_FINGERPRINTS.get("brief_material_count"),
        "brief_material_stats": _LAST_BRIEF_FINGERPRINTS.get("brief_material_stats"),
        # [P2.2 2026-09-12] 输入具名标的初筛摘要 — 三态清单 + 口径版本。
        #   落库目的: ①P4 全漏斗第一段 ("输入里有多少具名标的、多少本就不可交易")
        #   ②验证"低价/低波不再占用 UE 输入预算"这条验收项 ③context_only 标的若仍被
        #   模型当直接候选浮现, 可据此归因是模型不遵还是标注没送到。
        "input_prescreen": dict(_LAST_INPUT_PRESCREEN or {}),
        # [U4] 契约校验统计 — 攒样判据数据源 (违约率/修复率/方向补丁拒收)
        "contract_stats": _contract_stats,
        # [PROPOSAL-20260906 2026-09-06] RAW 口径契约统计 (normalize/repair 之前) —
        #   P3 判据"invalidation_triggers 填充率基线 36%"是 RAW 口径; contract_stats
        #   是 repair 后口径, 直接对比会高估依从 → 假毕业。同函数即 thinking-eval
        #   的 _v29_contract_stats, v1 臂不落 (字段集不适用)。
        "contract_stats_raw": (_v29_contract_stats(parsed.get("surfaced"))
                               if _use_v2 else None),
        "surfaced": surfaced,
        "note": ("record-only midday shadow; 不进 inject/DA供料/push, 判据见 DEB §13.3"
                 if midday else
                 ("DA 供料早跑 (19:40 CST/07:40 ET); 写主 jsonl 供 DA _load_ue_short_candidates "
                  "取 scans[-1]; 不进 inject/push/confirm/score; 回滚=删 crontab 行或 "
                  "universe_expansion.da_candidate_feed=false"
                  if da_feed else
                  "record-only; 不驱动 live, inject 另需 entry_enabled=true")),
    }
    _append_jsonl(_MIDDAY_PATH if midday else _SCAN_PATH, rec)
    result["scan_written"] = True

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
    # ── [P3.1 2026-09-13] 非有限入场价显式拒绝 (计划 P3「非有限数…拒绝」) ──
    #   实测缺陷 (本批测试自己抓到的, 修前): compute_entry_band 只挡 `e <= 0`, 而
    #   NaN 与 ±inf 都**通不过**那个比较 (NaN<=0 为 False, inf>0) ⇒ 返回 (nan, nan)
    #   / (inf, inf)。于是 entry_zone 是**非空**列表 ⇒ 调用方 `if not nums["entry_zone"]`
    #   守卫放行 ⇒ 一条 stop_loss=NaN 的指令会被写库。共享基建侧同样不拦 (实测
    #   compute_entry_band(nan)/(inf) 原样返回), 但那是执行路径共用的, 本批不代改
    #   (同 §17.3-⑥ 纪律: 共享基建要有自己的批次与验证窗), 已在 DEB §19 列待裁。
    #   UE 侧就地拒绝还有第二个理由: P3.1 新增了 planned_preview **JSON blob**, 而
    #   json.dumps(nan) 会吐出 `NaN` —— Python 能吃、严格 JSON 解析器不能
    #   ⇒ 不拦住就会把无效 JSON 写进 raw_instruction_json。
    try:
        _ep = float(entry_price)
    except (TypeError, ValueError):
        _ep = float("nan")
    if not math.isfinite(_ep) or _ep <= 0:
        return {"entry_zone": [], "stop_loss": 0.0, "take_profit": 0.0,
                "planned_preview": {
                    "state": "not_generated_non_finite_price"
                    if not math.isfinite(_ep) else "not_generated_invalid_band",
                    "version": PLANNED_PREVIEW_VERSION,
                }}
    entry_price = _ep
    low, high = compute_entry_band(entry_price, is_short)
    if not (low and high):
        return {"entry_zone": [], "stop_loss": 0.0, "take_profit": 0.0,
                "planned_preview": {"state": "not_generated_invalid_band",
                                    "version": PLANNED_PREVIEW_VERSION}}
    stop_pct = float(_cfg("universe_expansion.default_stop_pct", 3.0)) / 100.0
    tp_pct = float(_cfg("universe_expansion.default_tp_pct", 6.0)) / 100.0
    if is_short:
        stop_loss = high * (1 + stop_pct)   # SL 高于 zone high
        take_profit = low * (1 - tp_pct)    # TP 低于 zone low
    else:
        stop_loss = low * (1 - stop_pct)    # SL 低于 zone low
        take_profit = high * (1 + tp_pct)   # TP 高于 zone high
    # ── [P3.1 2026-09-13, DEB-20260910 §18.2/§18.6] 生成值预览 (纯派生, 零 IO) ──
    #   同时记"配置口径"与"从 entry 量出的实测口径": SL/TP 锚在 entry_band 边界之外,
    #   所以 default_stop_pct=3.0 / default_tp_pct=6.0 落到价格上是 BUY −5.91%/+11.30%、
    #   SHORT +6.09%/−10.70% (§18.2 表, 已与 8 行未触发 UE 生产行逐位对齐)。只记配置值
    #   会让读账的人以为生成止损是 3%; 只记实测值则常量一改就无法复算 ⇒ 两个都记。
    _sl_dist = abs(entry_price - stop_loss)
    _tp_dist = abs(take_profit - entry_price)
    _preview = {
        "state": "generated",
        "version": PLANNED_PREVIEW_VERSION,
        # ⚠ 与 DA 侧 planned_* 同名不同源: DA 的 planned_* 是**模型声明**的数值,
        #   UE 的是**代码出的**数值 (Track A)。跨源比 "planned vs 重定价" 测的不是
        #   同一件事, 必须带本键一起读。
        "value_origin": "code_track_a",
        "direction": "SELL_SHORT" if is_short else "BUY",
        "entry": round(float(entry_price), 4),
        "entry_zone": [low, high],
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
        # 计划 P3 要求的"风险距离" = |entry − SL| (价格单位, 未乘股数 ⇒ 不是风险金额)
        "risk_distance": round(_sl_dist, 4),
        "sl_pct_cfg": round(stop_pct * 100.0, 4),
        "tp_pct_cfg": round(tp_pct * 100.0, 4),
        "sl_pct_from_entry": (round(_sl_dist / float(entry_price) * 100.0, 4)
                              if entry_price else None),
        "tp_pct_from_entry": (round(_tp_dist / float(entry_price) * 100.0, 4)
                              if entry_price else None),
        "rr": round(_tp_dist / _sl_dist, 4) if _sl_dist > 0 else None,
        "generated_at": _now_et().isoformat(),
    }
    return {
        "entry_zone": [low, high],
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
        "planned_preview": _preview,
    }


def _planned_exec_block(preview: Dict[str, Any], sizing_ctx: Dict[str, Any],
                        borrow_rates: Dict[str, Any], surfaced: Dict[str, Any],
                        screening: Dict[str, Any], px: float, nums: Dict[str, Any],
                        pb_direction: str, confidence: int, today: str,
                        regime: Optional[str]) -> Dict[str, Any]:
    """[P3.3-3.5 2026-09-13] 估算股数 / 费用覆盖 / 可执行性 → 合入 planned_preview。

    record-only: 产物只进 raw_instruction_json 的嵌套键, **不进**指令顶层、不进任何门/
    排序/定价/仲裁判据 (计划 P3「预估股数不写成真实仓位」)。执行侧真门仍是
    admission.apply() (enforce=true) 与 pipeline 的 sizing/reprice; 本块只是把
    "生成时刻按同一口径会算出什么" 留在账上, 供 P3 验收② 与 P4 全漏斗对账。

    权益/借券费率**每批读一次** (调用方传入 sizing_ctx / borrow_rates), 逐候选只跑纯函数
    —— 计划明禁"逐候选调用会读取资本状态的完整 compute_instruction_values()"。
    任何异常都退化成显式 exec_state, 不让预览失败拖垮注入。
    """
    try:
        from app.playbook.planned_executability import build_planned_executability
        extra = build_planned_executability(
            preview=preview, sizing_ctx=sizing_ctx,
            symbol=f"US.{surfaced['symbol']}", direction=pb_direction,
            entry=px, stop_loss=nums.get("stop_loss"), take_profit=nums.get("take_profit"),
            confidence=confidence, horizon_hours=surfaced.get("horizon_hours"),
            trade_date=today, screening=screening, borrow_rates=borrow_rates, regime=regime,
        )
    except Exception as _pe_e:
        return {"exec_state": "not_generated_error",
                "exec_error": f"{type(_pe_e).__name__}: {_pe_e}"[:200]}
    return {**preview, **extra}


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


    # ── [P1.5 2026-09-12, DEB-20260910 / proud-slope-falcon] 批次消费纪律 ──
    #   修前: 取当日最后一条 type=="scan" — ①da_feed (07:40 ET) 与 premarket
    #   (09:10 ET) 同写主 jsonl, "最后一条"可能误消费 da_feed 批次; ②空扫描不落
    #   批次 → 静默回用早先(可能过期)候选; ③无时效校验。
    #   修后: 只允许最新有效 premarket 批次; 更晚开始的空/失败批次使更早的成功批次
    #   失效 (不回用); 时效走 playbook.candidate_scan_max_age_hours (与 DA feed 同一
    #   显式键)。这是批次时效, 不是订单 TTL — 原盘中入场截止规则不变。
    from app.playbook.candidate_eligibility import load_candidate_policy, require_auto_sim
    from app.playbook.ue_batch import injection_lock, latest_premarket

    today = _today()
    now_et = _now_et()
    try:
        policy = load_candidate_policy()
    except Exception as _pol_e:
        print(f"[inject] 候选资格配置读取失败 → 停止注入 (不猜阈值): {_pol_e}")
        return 1
    scan_rec, batch_reason = latest_premarket(
        _read_jsonl(_SCAN_PATH), today, now_et, policy["max_scan_age_hours"])
    _log_inject_batch_decision(scan_rec, batch_reason, today, now_et)
    if scan_rec is None:
        print(f"[inject] 无有效 premarket 批次 ({batch_reason}) → 不注入; "
              f"不回用 da_feed/midday/过期候选")
        return 0
    batch_id = str(scan_rec.get("batch_id") or f"UE:legacy:{today}")
    surfaced = scan_rec.get("surfaced") or []
    if not surfaced:
        # 全空 = 正常结束 (无机会), 与失败批次分开; 空批次已由 cmd_scan 落盘。
        print(f"[inject] 最新 premarket 批次 {batch_id} 浮现 0 条 (全空正常结束, "
              f"非失败) → 无候选可注入")
        return 0
    # 新策略注入的身份前置条件 (P0.2): 自动 broker 与自动账本必须同为 SIM。
    #   矛盾/未知 → 停止本批, 不自动改 .env.us/账户/模式。dry-run 只读不在此拦。
    if not dry:
        try:
            require_auto_sim()
        except Exception as _sim_e:
            print(f"[inject] 启动模式非 SIM 或矛盾 → 停止注入 (不写库): {_sim_e}")
            return 1
    # 读取额度 → 筛选 → 写后确认全程持进程间锁: cron 与手动重跑并发不会双花名额。
    with injection_lock(_INJECT_LOCK_PATH):
        return _cmd_inject_impl(
            enabled=enabled, dry=dry, min_conf=min_conf, max_n=max_n, today=today,
            now_et=now_et, scan_model=scan_rec.get("model_used"), batch_id=batch_id,
            surfaced=surfaced, policy=policy)


def _cmd_inject_impl(*, enabled: bool, dry: bool, min_conf: float, max_n: int,
                     today: str, now_et: datetime, scan_model: Optional[str],
                     batch_id: str, surfaced: List[Dict[str, Any]],
                     policy: Dict[str, Any]) -> int:
    """inject 主体 — 在 cmd_inject 的批次校验与进程间额度锁之内运行。

    P1.5 顺序契约: 原有方向/confidence/持仓/DA/streak 过滤 (阈值与排序含义不变)
    → 报价 + 筛选口径 ATR 资格判定 → 在合格集合内按当日剩余额度取 K (淘汰由剩余
    合格队列补位) → 写入 → 写后确认。分析名额不是成交配额, 允许零推荐。
    """
    from app.playbook.candidate_eligibility import (
        positive_number, record_candidate_batch, screen_candidates, select_candidates,
        selection_summary)
    from app.playbook.ue_batch import confirm_saved, effective_ue_symbols

    positions = _position_symbols()
    _scan_model = scan_model  # 保留原局部名: 指令行 model_used 归因逐字不变
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
    # [P1.5] 不再在此截断: 名额改在资格判定之后消费 ("先过滤后预算"), 被淘汰者由
    #   剩余合格队列补位。原 cand[:max_n] 先截断 → 价格/ATR 淘汰后不补位 = 名额空转。

    # [UE-B2 2026-08-29] 拒绝归因落盘 — 筛选链每个落选候选一行结构化记录 (纯观测)。
    _log_inject_reject(surfaced, min_conf, positions, existing_pb, _max_streak, max_n)
    # [DA-C1 / UE-C2 2026-08-30] 方向结构 + streak 反事实测量 (纯观测零行为)。
    _log_direction_structure(surfaced, _today_pb, min_conf, _max_streak)
    _log_streak_counterfactual(surfaced, min_conf, positions, existing_pb, _max_streak)
    # [U3 2026-09-05, PROPOSAL-20260905 P4] tier 闸影子判定 (record-only, 永不拦指令)。
    # tier_gate_enabled 是毕业后才许开的 enforce 开关 (判据见 yaml); 影子期若被误开,
    # enforce 代码尚不存在 → 静默=true 即死键幻觉, 此处响亮告警并坚持影子。
    if bool(_cfg("universe_expansion.tier_gate_enabled", False)):
        print("  [warn] tier_gate_enabled=true 但 tier 闸 enforce 尚未实现 (影子期) — "
              "按影子继续; 毕业判据满足并经用户裁决前请保持 false")
    _log_tier_gate_shadow(surfaced, {str(s.get("symbol", "")).upper() for s in cand})

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
    # ── [P3.3-3.5 2026-09-13, DEB-20260910 §20] 估算股数/费用覆盖/可执行性的**批级**上下文 ──
    #   权益每批读一次 (只读 URI 取 capital_state 最新行, 不实例化 CapitalSafetySystem —
    #   后者 __init__ 会 _ensure_db_table() 开写连接, cron fork 的 UE 进程不该引入);
    #   借券费率每批读一次 (data/shadow/borrow_rate_series.jsonl, cron 快照)。
    #   两者失败都只降级预览, 不阻塞注入 (record-only)。
    _sizing_ctx: Dict[str, Any] = {"state": "unavailable", "notes": ["context_not_read"]}
    _borrow_rates: Dict[str, Any] = {}
    try:
        from app.playbook.planned_executability import load_borrow_rates, read_sizing_context
        _sizing_ctx = read_sizing_context()
        _borrow_rates = load_borrow_rates()
        print(f"  [planned-exec] 权益=${_sizing_ctx.get('equity'):,.0f} "
              f"(源={_sizing_ctx.get('equity_origin')}, 龄={_sizing_ctx.get('equity_age_hours')}h, "
              f"水位={_sizing_ctx.get('water_level')}) | 借券费率覆盖 {len(_borrow_rates)} 标的 "
              f"| 上限={float(_sizing_ctx.get('max_position_pct') or 0)*100:g}% "
              f"⇒ 帽内名义≈${float(_sizing_ctx.get('equity') or 0)*float(_sizing_ctx.get('max_position_pct') or 0):,.0f}")
    except Exception as _pe_ctx_e:
        print(f"  [warn] 生成期预览上下文读取失败 (record-only, 不阻塞注入): {_pe_ctx_e}")
    # [P1 2026-09-02 低ATR源头预检] ATR provider — 与执行门/盘前定桶
    #   (pipeline.py _tag_volatility_buckets) 同一 provider 同一数据源 (日线 ATR,
    #   盘前稳定不依赖 OpenD)。
    # [U2 根因修 2026-09-11] 开关/阈值改走执行门同一 accessor get_playbook_config()
    #   (app/playbook/config.py, 与 pipeline.py:5317-5318 同源)。修前读扁平键
    #   _cfg("playbook.low_atr_gate_*"), 而真 yaml 路径是
    #   playbook.execution_parameters.low_atr_gate_* (unified_config.yaml:1185-1186) —
    #   get_dotted 严格逐层找不到扁平键 (config_container.py:401-418) → 自 09-02
    #   上线以来预检恒读默认 False, 从未生效; 执行门经 PlaybookConfig 扁平合并
    #   (config.py:84-90) 读到 True/0.03 — 同名门一侧活一侧死。统一 accessor 后
    #   "单旋钮同开同关"是结构保证, 不再依赖两处字符串手工同步。
    #   行为变更记录 (契约): 本次接线修复使预检首次真实生效, inject 候选分布收窄 —
    #   只读影子复算 (DB ro, 08-27~09-10 UE 指令行 n=55, atr_pct 覆盖率 100%):
    #   56.4% atr_pct<0.03 (BUY 62.5% / SHORT 51.6%); 同批本就被执行门拦截
    #   (7/24 以来 5,240 条 low_atr_block 遥测) → 交易行为理论零变更, 省死指令开销。
    #   prompt 未动 → prompt_version/prompt_hash 指纹不变。反分叉钉:
    #   tests/test_ue_low_atr_precheck.py TestNoForkNail (活 yaml 双 accessor 同值,
    #   mock 不可满足) + TestSourceContract 禁扁平键回归。
    #   accessor 异常 / provider 构建失败 → 预检整体 fail-open (执行门兜底), 显式留痕。
    _ue_atr_provider = None
    _ue_atr_gate_enabled = False
    _ue_atr_gate_min = 0.025  # 回退默认与执行门 pipeline.py:5318 对齐; 活性值 yaml SSOT
    try:
        from app.playbook.config import get_playbook_config as _get_pb_cfg_ue
        _pb_cfg_ue = _get_pb_cfg_ue()
        _ue_atr_gate_enabled = bool(_pb_cfg_ue.get("low_atr_gate_enabled", False))
        _ue_atr_gate_min = float(_pb_cfg_ue.get("low_atr_gate_min_atr_pct", 0.025))
    except Exception as _pb_cfg_ue_e:
        print(f"  [warn] 执行门配置读取失败, 低ATR预检 fail-open (执行门兜底): {_pb_cfg_ue_e}")
    if _ue_atr_gate_enabled:
        try:
            from app.core.di_container import get_di_container
            _ue_atr_provider = get_di_container().atr_provider()
        except Exception as _atr_prov_e:
            print(f"  [warn] ATR provider 构建失败, 低ATR预检 fail-open: {_atr_prov_e}")

    # ── [P1.1/P1.5 2026-09-12] 统一可交易性初筛 → 之后才取 K ──
    #   筛选 ATR 改走 ATRProvider.get_screening_atr (原始已完成日线 TR14 rolling-mean),
    #   不再是止损口径 get_atr_pct (那条链可能含 VIX 缩放 / 三层底限 / 分钟 ATR)。
    #   ⚠️ 显式策略行为变更 (P1.2): ATR 来源与口径改变 → 拒绝集合与旧预检不完全相同;
    #   阈值仍是执行门同一旋钮 low_atr_gate_* (上方 accessor), 未新增永久开关。
    #   价格地板 = max(admission.min_entry_price, universe_expansion.min_price_floor),
    #   两个既有地板都不放宽 (今日同为 5.0)。
    _ue_floor = float(_cfg("universe_expansion.min_price_floor", 0) or 0)
    policy["low_atr_enabled"] = _ue_atr_gate_enabled
    policy["min_atr_ratio"] = _ue_atr_gate_min
    if _ue_floor > 0:
        policy["min_price"] = max(float(policy.get("min_price") or 0.0), _ue_floor)
    policy["min_price_sources"] = ("max(playbook.admission.min_entry_price,"
                                   " universe_expansion.min_price_floor)")
    if _ue_atr_gate_enabled and _ue_atr_provider is None:
        # 与旧预检同纪律: provider 构建失败 → 低ATR预检整体 fail-open (执行门兜底)。
        print("  [warn] ATR provider 不可用 → 低ATR预检 fail-open (执行门兜底)")
        policy["low_atr_enabled"] = False
    _screen_rows = [
        {"symbol": s["symbol"], "direction": s["direction"],
         # 资格判定与指令锚价用同一个数 (UE 盘前锚定口径), 避免筛选价/锚价分叉。
         "effective_price": _effective_quote_price(q.get(s["symbol"], {})),
         "scan_price": s.get("scan_price"), "confidence": s.get("confidence"),
         "surfaced": s}
        for s in cand
    ]
    try:
        _screened = screen_candidates(
            _screen_rows, router, source="UE", batch_id=batch_id, now=now_et,
            quotes=q, policy=policy, atr_provider=_ue_atr_provider)
    except Exception as _scr_e:
        # 初筛整体异常 → fail-open (执行门兜底), 但必须留痕, 不静默当"全部合格"。
        print(f"  [warn] 候选资格判定异常 fail-open (执行门兜底): {_scr_e}")
        _screened = [dict(r, candidate_screening={
            "status": "UNKNOWN", "reasons": [], "missing_fields": [],
            "reason": f"screening_error:{type(_scr_e).__name__}"}) for r in _screen_rows]
    for _row in _screened:
        if (_row.get("candidate_screening") or {}).get("atr_reason") == "atr_unavailable":
            print(f"  [warn] {_row['surfaced']['symbol']} 低ATR预检异常 fail-open: atr_unavailable")
    _summary = selection_summary(_screened)
    print(f"[inject] 资格初筛: 候选 {_summary['total']} → REJECT "
          f"{_summary['rejected_ineligible']} reasons={_summary['reason_codes']}")

    # 资格拒绝 → SKIP 行 (可归因, 不占当日额度; 与旧 price_floor/low_atr 同轨)。
    instructions = []
    for _row in _screened:
        _sc = _row.get("candidate_screening") or {}
        if _sc.get("status") != "REJECT":
            continue
        _s = _row["surfaced"]
        _reasons = [str(c) for c in (_sc.get("reasons") or [])]
        _skip_reason = _eligibility_skip_reason(_reasons, _sc, policy)
        _px = _sc.get("price") or _s.get("scan_price")
        if not _px:
            # 无有效参考价 → 不能生成可执行指令, 也不落 entry_price 缺失的伪 SKIP 行。
            print(f"  [skip] {_s['symbol']} 资格拒绝且无有效参考价 ({_skip_reason})")
            _log_inject_loop_reject(_s, f"ineligible_no_price({_skip_reason})")
            continue
        instructions.append({
            "date": today,
            "symbol": f"US.{_s['symbol']}",
            "direction": "SKIP",
            "skip_reason": _skip_reason,
            "entry_price": round(float(_px), 4),
            "confidence": int(round(float(_s["confidence"]) * 100)),
            "thesis": _s.get("thesis", ""),
            "decision_id": f"{STRATEGY_ID}:{today}:{_s['symbol']}",
            "model_used": scan_model,
            # [P3.1] SKIP 行从不调用 _compute_numbers (无 SL/TP 可留档) ⇒ 显式写状态。
            #   否则日后读账无法区分"UE 行没有 planned_* 因为它是 SKIP 行"(正常) 与
            #   "没有 planned_* 因为代码漏写"(缺陷) —— 与 §17.3-② "索引空 vs 没跑
            #   gather" 同型的歧义, 一律用显式状态消除, 不靠"键不存在"表态。
            "raw_instruction": {
                "source": STRATEGY_ID,
                "candidate_screening": _sc,
                "planned_preview": {"state": "not_generated_ineligible",
                                    "version": PLANNED_PREVIEW_VERSION},
            },
        })
        if "low_atr" in _reasons and _sc.get("screening_atr_ratio") is not None:
            print(f"  [skip] {_s['symbol']} 低ATR预检 "
                  f"({float(_sc['screening_atr_ratio']):.4f} "
                  f"< {policy['min_atr_ratio']:g}) → SKIP行")
        else:
            print(f"  [skip] {_s['symbol']} 资格初检 ({_skip_reason}) → SKIP行")
        _log_inject_loop_reject(_s, _skip_reason)

    # 合格集合 (PASS + UNKNOWN) 按源排序进入取 K 与补位。UNKNOWN = 缺数, 不等于拒绝:
    #   研究/注入可继续, 执行阶段仍由原低ATR风险门用实时行情复核 (盘前快照不当实时价)。
    _eligible = [r for r in _screened
                 if (r.get("candidate_screening") or {}).get("status") != "REJECT"]
    for _row in _eligible:
        s = _row["surfaced"]
        _sc = _row.get("candidate_screening") or {}
        # 2026-08-11: effective 口径 — 盘前时段用 pre_price 锚定 entry/SL/TP/band。
        #   修复前 last_price=昨收 → 锚错 (8/11 JD 实盘锚 33.47 vs 真实盘前 32.21, 错 3.8%)。
        px = _effective_quote_price(q.get(s["symbol"], {})) or s.get("scan_price")
        if not px:
            print(f"  [skip] {s['symbol']} 无现价")
            _log_inject_loop_reject(s, "no_quote(无现价)")
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
        # [U2 PROPOSAL-20260905 P2, 2026-09-05 用户裁"如无问题可以实施"] 删除 confidence cap=50.
        #   原动机 (压 auto_execute 排序) 已死: 排序主键是 _src_priority (ADR-020),
        #   conf 仅同源内次键 (2026-07-25 勘误) — cap 只剩副作用: 库内 UE conf 恒=50,
        #   置信分层归零 (0/29 条有可分桶信息), conf 校准/tier 闸阈值全部无从谈起。
        #   护栏: UE conf 此后只用于归因分桶与影子判分, 禁止新增任何线性 conf 门
        #   (阈值裁决走 U3 tier 闸规则, 不是 conf 数字)。
        _raw_conf = int(round(float(s["confidence"]) * 100))  # schema INTEGER 0-100
        # [P3.3-3.5] 估算股数 + 费用覆盖 + 可执行性 (record-only, 逐候选只跑纯函数)
        _preview_full = _planned_exec_block(
            nums["planned_preview"], _sizing_ctx, _borrow_rates, s, _sc,
            float(px), nums, pb_direction, _raw_conf, today,
            _regime_fields.get("market_regime"))
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
            "confidence": _raw_conf,
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
                # [P1.5] 候选筛选快照随指令落库: ①写后确认按 candidate_id 回读核对
                #   (save_instruction 返回 ID ≠ 入围成功); ②执行侧低ATR门读同义字段;
                #   ③漏斗审计 candidate_funnel.jsonl 与指令行可 join。
                "candidate_screening": _sc,
                # [P0-A 2026-09-04, REVIEW-20260904 用户裁"按建议执行"] chase_cap 禁追线:
                #   UE 指令此前无此字段 → pipeline chase_cap 遥测/enforce 对 UE 双盲
                #   (9/4 USAR 盘前追价 +2.16% 入场, 8 分钟 SL_INITIAL -$158 实锤)。
                #   取值=入场带顺势沿: LONG 取 entry_high / SHORT 取 entry_low
                #   (compute_entry_band 的 CHASE_ZONE_PCT=5% 沿, 即系统自声明的
                #   "顺势最大追入幅度" — 语义=越过入场带=追出被分析的区间)。
                #   与 DA 的 LLM 声明式禁追线同顶层键位、同消费端
                #   (parse_raw_semantics / pipeline chase_cap 块), 无需下游改动。
                #   诚实边界: 带内追价 (如 USAR +2.16%) 不违例 — 该档病灶由
                #   月报 D2/D3 裁决项覆盖, 本字段管的是"追出带外"的极端段。
                "chase_cap_price": entry_high if pb_direction == "BUY" else entry_low,
                "prompt_version": PROMPT_VERSION,
                # [U1] 内容指纹随指令落库 (raw_instruction_json 内)
                "prompt_hash": PROMPT_HASH_V2,
                # ── [P3.1 2026-09-13, DEB-20260910 §18.2/§18.6] UE 生成值留档 ──
                #   实测: 触发时 pipeline.py:4242 用重定价值整体覆盖 entry_price/
                #   stop_loss/take_profit 三列 (DA 侧 307 行可比中 300 行三字段全变),
                #   而 UE 行 raw_instruction_json 的 planned_* 覆盖率是 **0/183**
                #   (DA 侧 457/470=97.2%) ⇒ 重定价后库里既无生成值也无"从哪到哪"
                #   (§18.3: reprice_* 三键被 db._TRACKING_FIELDS 静默丢弃), P3 验收②
                #   "生成预览与最终委托可追踪" 对 UE **不可测**。
                #   前三个键**与 DA 侧同名同层** ⇒ 同一条 json_extract('$.planned_entry')
                #   可跨源查询 (P4 全漏斗需要); UE 特有的口径元数据收进 planned_preview
                #   一个嵌套键, 不铺开成一堆平级键。
                #   纯附加、零行为变更: raw_instruction 只被 parse_raw_semantics (按
                #   .get() 取 entry_archetype/confirmation/chase_cap_price) 与报表读,
                #   admission.apply() 保留既有键 (test_dict_raw_instruction_keys_are_kept),
                #   不进任何门/排序/定价判据。
                #   **未记 position_size_hint**: UE 生成时不持股数估算, 而算它需要资本
                #   状态 (计划 P3 明禁"逐候选调用会读取资本状态的完整
                #   compute_instruction_values()"); 执行侧真实股数由 P3.2 的
                #   metadata_json.reprice_audit + sizing_delta.jsonl 影子承担。留待
                #   "估算股数/费用覆盖/可执行性" 那一批一起做, 不在本批造半截数字。
                "planned_entry": round(float(px), 4),
                "planned_stop_loss": nums["stop_loss"],
                "planned_take_profit": nums["take_profit"],
                "planned_preview": _preview_full,
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
                    # [2026-07-25 信息源复盘] cap 前原始 conf 落库 (0-100 整数)。
                    #   2026-09-05 U2 删 cap 后顶层 confidence 与本键同值 (历史行
                    #   顶层=cap50 截断值, 本键才是原值 — 跨史 join 须用本键)。
                    #   量纲注意: scan jsonl 同名键是 0-1 小数 (normalize 层), 不同尺。
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

    # ── [P1.5] 当日额度: 唯一有效 UE 入围累计 (重跑不重占名额; SKIP/仲裁拒绝不算成功) ──
    from app.playbook.db import PlaybookDB
    db = PlaybookDB()
    db.ensure_tables()
    _consumed = effective_ue_symbols(db.get_instructions_by_date(today) or [], STRATEGY_ID)
    _quota = max(0, max_n - len(_consumed))
    # [P1.5 补 2026-09-12] 写库名额只发给"能产出可执行指令"的候选。
    #   资格层: 无有效价格 = 缺数 → UNKNOWN (研究可继续, 不是拒绝)。
    #   预算层: 名额是**成交预算**, 发给产不出指令的候选 = 空耗一名, 后面合格候选
    #   补不上位 (离线全链实测 2026-09-12: 无报价候选占 rank=1, 名额 3 只写出 2 条)。
    #   分层记录不混成一个 SKIP: 资格 status=UNKNOWN + selection_status=deferred_no_price。
    def _defer_no_executable_price(row):
        _s = row.get("surfaced") or {}
        _px = (_effective_quote_price(q.get(_s.get("symbol"), {}))
               or _s.get("scan_price"))
        return None if positive_number(_px) is not None else "deferred_no_price"

    _selected = select_candidates(_screened, _quota, allow_unknown=True,
                                  already_selected=_consumed,
                                  defer_reason=_defer_no_executable_price)
    # 全漏斗审计 (批次/候选级): 资格 → 预算; LLM 否决与写库仲裁在各自阶段分开记录。
    record_candidate_batch(_screened, batch_id=batch_id, source="UE",
                           phase="inject_dry" if dry else "inject")
    _sel = selection_summary(_screened)
    print(f"[inject] 当日额度 max={max_n} | 已入围唯一标的={len(_consumed)} | "
          f"本轮名额={_quota} | 选取={len(_selected)} (budget_cut={_sel['budget_cut']}, "
          f"duplicate={_sel['duplicate']}, "
          f"deferred_no_price={_sel.get('deferred_no_price', 0)})")
    _writable = {str(r["surfaced"]["symbol"]) for r in _selected}
    _quota_written = 0
    _unknown_writes = 0
    saved = 0
    if dry:
        if not enabled:
            print("  [安全阀] universe_expansion.entry_enabled=false → 未写库, 未下单。")
        return 0

    if not instructions:
        return 0

    # 写入 playbook_instructions — 与 DA 共用同一执行/触发/退出轨道.
    #   auto_execute 会对未触发指令做 _check_intraday_trigger (symbol-agnostic 取价),
    #   宇宙外标的无需订阅/轮询注册即可被执行.
    # S0 影子开关 (DEB-20260805-supply 终裁②, 2026-08-06 用户裁"同意"): 只读一次.
    try:
        from app.core.config_container import get_config as _gc_sb
        _sb_on = bool(_gc_sb().get_dotted("universe_expansion.short_boost_shadow", False))
    except Exception:
        _sb_on = False
    for it in instructions:
        _is_skip = it.get("direction") == "SKIP"
        _cand_sym = str(it.get("symbol", "")).replace("US.", "")
        if not _is_skip:
            # 名额闸: 只写本轮选取的候选; 名额用尽即停 (被淘汰者不写入)。
            if _unknown_writes:
                print(f"  [quota] {it['symbol']} 前一笔写入结果不明 → 停止补位 "
                      f"(先核对, 不盲目重写)")
                _log_inject_save_reject(it, "halted_after_unknown_write")
                continue
            if _cand_sym not in _writable or _quota_written >= _quota:
                print(f"  [quota] {it['symbol']} 超出当日 UE 名额 "
                      f"({_quota_written}/{_quota}) → 不写入")
                _log_inject_save_reject(it, f"quota_exhausted({_quota_written}/{_quota})")
                continue
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
                        # [2026-09-18 登记项②] 措辞/级别修正: SKIP 行**本就不该**登记冷却
                        #   (它不是成交方向), 每天打 [warn] 会被当异常扫 (9/18 实测当日唯一
                        #   告警即此, 属良性)。真映射失败 (非 SKIP 方向却映射不出) 仍必须
                        #   可见 ⇒ 保留 [warn] 分支。
                        if str(it.get("direction") or "").upper() == "SKIP":
                            print(f"  [cooldown] {it.get('symbol')} 方向 SKIP → 不登记冷却 "
                                  f"(SKIP 行无需冷却; 非缺陷)")
                        else:
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
            _rid = db.save_instruction(it)
            # [P1.5] 写后确认: save_instruction 返回 ID **不代表入围成功** — 覆盖仲裁
            #   拒绝时同样返回现存行 ID (db.py:393-401)。按 candidate_id/decision_id/
            #   symbol/direction/source/skip_reason 回读核对, 确定拒绝才补下一名。
            _state = confirm_saved(db, it, _rid)
            if _state == "saved":
                saved += 1
                if not _is_skip:
                    _quota_written += 1
            elif _state == "rejected":
                print(f"  [reject] {it['symbol']} 写库仲裁拒绝 (id={_rid}) "
                      f"→ 不占额度, 后续合格候选补位")
                _log_inject_save_reject(it, f"arbitration_rejected(id={_rid})")
                continue
            else:
                _log_inject_save_reject(it, f"write_state_unknown(id={_rid})")
                if _is_skip:
                    print(f"  [warn] {it['symbol']} SKIP 行写库结果不明 (id={_rid})")
                else:
                    _unknown_writes += 1
                    print(f"  [warn] {it['symbol']} 写库结果不明 (id={_rid}) "
                          f"→ 不计额度, 停止补位待核对")
                    continue
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

    print(f"[inject] 已写入 {saved} 条 playbook_instructions (占额度 "
          f"{_quota_written}/{_quota}, 结果不明 {_unknown_writes}; "
          f"source={STRATEGY_ID}, 走 DA 统一执行轨道)")
    return 0


# ── score / report ───────────────────────────────────────────
def cmd_score() -> int:
    records = _read_jsonl(_SCAN_PATH)
    # da_feed 早跑 (2026-09-09) 不入评分: 它只为 DA 供候选, 盘前数据薄, 评它会把同一
    #   交易日的前瞻收益计两次 (原 todo 按 date 判 scored, 同日两行会双双入选)。
    scans = [r for r in records if r.get("type") == "scan"
             and r.get("scan_window", "premarket") == "premarket"]
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
    scans = [r for r in records if r.get("type") == "scan" and r.get("date", "") >= cutoff
             and r.get("scan_window", "premarket") == "premarket"]
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
    if not _is_us_trading_day_today():
        print("[scan-eval] 非美股交易日，跳过评估")
        return 0
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
                       temperature=0.2, max_tokens=24000,
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
        # [P2.5 2026-09-13] 同 thinking_eval: prompt_hash 只覆盖 SYSTEM_PROMPT_V2,
        #   brief 侧变更对它不可见 ⇒ 并列落 brief 分段键, 使 v1/v2 对比序列可按输入版本切分。
        "brief_template_version": BRIEF_TEMPLATE_VERSION,
        "brief_template_hash": _LAST_BRIEF_FINGERPRINTS.get("brief_template_hash"),
        "brief_content_hash": _LAST_BRIEF_FINGERPRINTS.get("brief_content_hash"),
        "clue": manual_clue, "v1": {"model": m1, "surfaced": v1},
        "v2": {"model": m2, "surfaced": v2},
    })
    print(f"[scan-eval] 已落对比文件: {_PROMPT_EVAL_PATH.name} (record-only, 未下单)")
    return 0


# ── thinking-eval: glm-5.2 enable_thinking on/off 影子 A/B (record-only) ──
# 2026-09-05 (用户令"要"跑探针+影子A/B): thinking 现被 model_call.py DashScope
#   setdefault(enable_thinking=False) 全局关闭 (2026-07-06 P0 针对 800-tok 投票小调用,
#   UE 16000-tok 大调用系误伤)。路由级 extra_body.enable_thinking 灰度基建已在
#   (2026-07-25 E3 用户裁决)。本命令 = 开生产灰度前的影子实证仪器:
#   同一 clue brief + 同一 v2.9+ prompt, 唯一变量 = enable_thinking。
#   纯 shadow: 不落主 scan / 不 inject / 不下单, 独立 jsonl 不污染 prompt_eval 序列。
#   预注册判读: ①schema 通过率不降 ②时延增量 (生产满载 210-230s/timeout=300 的
#   安全余量) ③token 增量 (thinking 消耗 reasoning token, 挤占 16000 预算的风险)
#   ④v2.9+ 契约填充率 (基线 36% 同窗) ⑤tier 分布不塌缩。
_THINKING_EVAL_PATH = _ROOT / "data" / "shadow" / "thinking_eval.jsonl"


def _v29_contract_stats(raw_items: Any) -> Dict[str, Any]:
    """v2.9+ 契约填充率统计 — RAW response 口径 (normalize/repair 之前),
    衡量模型本体依从度, 与生产修复链无关。null-by-design 字段按方向分别判定合规。"""
    items = [it for it in (raw_items or []) if isinstance(it, dict)]
    n = len(items)
    st = {
        "n": n, "n_long": 0, "n_short": 0, "n_skip": 0,
        "tiers": {}, "evidence_ref_nonempty": 0,
        "invalidation_filled_non_skip": 0, "non_skip": 0,
        "triggers_present": 0, "triggers_nonempty": 0,
        "checklist_ok": 0, "horizon_present": 0, "move_present": 0,
        "short_check_compliant": 0, "skip_reason_compliant": 0,
        "wl_valid": 0, "conf_valid": 0, "_conf_sum": 0.0,
    }
    for it in items:
        d = str(it.get("direction", "")).upper().strip()
        if d == "LONG":
            st["n_long"] += 1
        elif d == "SHORT":
            st["n_short"] += 1
        elif d == "SKIP":
            st["n_skip"] += 1
        tier = str(it.get("self_tier", "")).upper().strip()
        st["tiers"][tier or "<missing>"] = st["tiers"].get(tier or "<missing>", 0) + 1
        if str(it.get("evidence_ref") or "").strip():
            st["evidence_ref_nonempty"] += 1
        if d != "SKIP":
            st["non_skip"] += 1
            if str(it.get("invalidation") or "").strip():
                st["invalidation_filled_non_skip"] += 1
        tr = it.get("invalidation_triggers")
        if isinstance(tr, list):
            st["triggers_present"] += 1
            if tr:
                st["triggers_nonempty"] += 1
        cl = it.get("evidence_checklist")
        if isinstance(cl, dict) and all(
            isinstance(cl.get(k), bool)
            for k in ("catalyst_specific", "catalyst_within_24h",
                      "price_volume_confirmed", "crowding_low")
        ):
            st["checklist_ok"] += 1
        if isinstance(it.get("horizon_hours"), (int, float)):
            st["horizon_present"] += 1
        if isinstance(it.get("expected_move_pct"), (int, float)):
            st["move_present"] += 1
        sc = it.get("short_check")
        if d == "SHORT":
            if isinstance(sc, dict) and all(
                k in sc for k in ("borrow_feasible",
                                  "downside_catalyst_expiry_hours", "squeeze_risk")
            ):
                st["short_check_compliant"] += 1
        elif sc is None:
            st["short_check_compliant"] += 1
        sr = it.get("skip_reason")
        if d == "SKIP":
            if str(sr or "").strip():
                st["skip_reason_compliant"] += 1
        elif sr is None or not str(sr).strip():
            st["skip_reason_compliant"] += 1
        if str(it.get("winner_loser", "")).upper().strip() in ("WINNER", "LOSER"):
            st["wl_valid"] += 1
        try:
            c = float(it.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            c = 0.0
        if 0.0 < c <= 1.0:
            st["conf_valid"] += 1
        st["_conf_sum"] += c
    st["conf_mean"] = round(st.pop("_conf_sum") / n, 3) if n else 0.0
    return st


def cmd_thinking_eval() -> int:
    """同一 clue brief + 同一 v2.9+ prompt, enable_thinking off/on 双臂并排。
    纯 shadow: 不落主 scan、不 inject、不下单。唯一变量=enable_thinking。"""
    # 2026-09-09 F2: 影子实验流量打标 — 网关审计 is_test=1, 不污染生产存活检测/
    #   日报网关口径 (成本由日报影子段从 thinking_eval.jsonl 独立归集)。
    os.environ["FTSEEK_SHADOW_RUN"] = "1"
    if not _is_us_trading_day_today():
        print("[thinking-eval] 非美股交易日，跳过评估")
        return 0
    brief = _build_clue_brief("")
    print("=" * 72)
    print(f"thinking-eval clue brief ({len(brief)} chars):")
    print(brief[:1200])
    print("=" * 72)

    try:
        from app.llm.model_call import call_llm
    except Exception as e:
        print(f"[thinking-eval] 无法导入 call_llm: {e}")
        return 1

    def _run(tag: str, thinking_on: bool):
        t0 = time.time()
        res = call_llm(
            "universe_expansion_reasoning", brief,
            system=SYSTEM_PROMPT_V2, temperature=0.2, max_tokens=24000,
            schema_required={"surfaced": list}, json_mode=True,
            extra_body={"enable_thinking": True} if thinking_on else None,
        )
        dt = time.time() - t0
        if not res or not res.success:
            print(f"[{tag}] LLM 调用失败: {getattr(res, 'error', '?') if res else 'no_result'}")
            return {"tag": tag, "ok": False, "latency_s": round(dt, 1),
                    "error": getattr(res, "error", "no_result") if res else "no_result"}
        parsed = res.response if isinstance(res.response, dict) else {}
        raw_items = parsed.get("surfaced")
        norm = _normalize_surfaced(raw_items, with_tier=True)
        return {
            "tag": tag, "ok": True, "latency_s": round(dt, 1),
            "model": getattr(res, "model_used", ""),
            "finish": getattr(res, "finish_reason", ""),
            "tokens_in": getattr(res, "tokens_in", 0),
            "tokens_out": getattr(res, "tokens_out", 0),
            "stats": _v29_contract_stats(raw_items),
            "surfaced_brief": [
                {"symbol": s["symbol"], "direction": s["direction"],
                 "confidence": s["confidence"], "self_tier": s.get("self_tier", "")}
                for s in norm
            ],
        }

    print("[OFF] thinking=False (生产现状) 推理中 ...", flush=True)
    off = _run("OFF", False)
    print(f"  -> {'OK' if off['ok'] else 'FAIL'} {off['latency_s']}s"
          + (f" out={off.get('tokens_out')} n={off['stats']['n']}" if off["ok"] else ""),
          flush=True)
    print("[ON]  thinking=True 推理中 ...", flush=True)
    on = _run("ON", True)
    print(f"  -> {'OK' if on['ok'] else 'FAIL'} {on['latency_s']}s"
          + (f" out={on.get('tokens_out')} n={on['stats']['n']}" if on["ok"] else ""),
          flush=True)

    print("\n" + "=" * 72)
    print("thinking OFF/ON 并排对比")
    print("-" * 72)
    hdr = ("指标", "OFF(现状)", "ON")
    rows = [("schema 通过", "✓" if off["ok"] else "✗ " + off.get("error", ""),
             "✓" if on["ok"] else "✗ " + on.get("error", ""))]
    if off["ok"] and on["ok"]:
        def _pct(a: Dict[str, Any], k: str, base: str = "n") -> str:
            den = a["stats"].get(base, 0)
            return f"{a['stats'].get(k, 0)}/{den}" if den else "-"
        rows += [
            ("时延 s", off["latency_s"], on["latency_s"]),
            ("tokens_out (含 reasoning)", off["tokens_out"], on["tokens_out"]),
            ("finish_reason", off["finish"], on["finish"]),
            ("浮现数 L/S/K",
             f"{off['stats']['n_long']}/{off['stats']['n_short']}/{off['stats']['n_skip']}",
             f"{on['stats']['n_long']}/{on['stats']['n_short']}/{on['stats']['n_skip']}"),
            ("tier 分布", json.dumps(off["stats"]["tiers"], ensure_ascii=False),
             json.dumps(on["stats"]["tiers"], ensure_ascii=False)),
            ("invalidation 填充(非SKIP)", _pct(off, "invalidation_filled_non_skip", "non_skip"),
             _pct(on, "invalidation_filled_non_skip", "non_skip")),
            ("triggers 非空", _pct(off, "triggers_nonempty"), _pct(on, "triggers_nonempty")),
            ("checklist 合规", _pct(off, "checklist_ok"), _pct(on, "checklist_ok")),
            ("short_check 合规", _pct(off, "short_check_compliant"),
             _pct(on, "short_check_compliant")),
            ("skip_reason 合规", _pct(off, "skip_reason_compliant"),
             _pct(on, "skip_reason_compliant")),
            ("引证非空", _pct(off, "evidence_ref_nonempty"), _pct(on, "evidence_ref_nonempty")),
            ("conf 均值", off["stats"]["conf_mean"], on["stats"]["conf_mean"]),
        ]
    for r in rows:
        print(f"  {r[0]:<22} {str(r[1]):<28} {r[2]}")
    if off["ok"] and on["ok"]:
        print("-" * 72)
        _o = {b["symbol"]: b for b in off["surfaced_brief"]}
        _n = {b["symbol"]: b for b in on["surfaced_brief"]}
        for sym in sorted(set(_o) | set(_n)):
            a, b = _o.get(sym), _n.get(sym)
            _a = f"{a['direction']:5} {a['confidence']:.2f} {a['self_tier']}" if a else "—"
            _b = f"{b['direction']:5} {b['confidence']:.2f} {b['self_tier']}" if b else "—"
            print(f"  {sym:6} OFF[{_a}]  ON[{_b}]")
    print("=" * 72)

    _append_jsonl(_THINKING_EVAL_PATH, {
        "type": "thinking_eval", "date": _today(), "evaluated_at": _now_et().isoformat(),
        "prompt_version": PROMPT_VERSION, "prompt_hash": PROMPT_HASH_V2,
        # ── [P2.5 2026-09-13] brief 侧分段键也落进本影子序列 ──
        #   为什么必须落: `docs/CONTROL-LOOP-LEDGER.md` 里 thinking-eval 的预注册判据是
        #   "有效轮次 ≥10 且**跨 prompt_hash 不得混合**"(cron 1e96c03c945e, 到期
        #   2026-09-22)。而 PROMPT_HASH_V2 = sha1(SYSTEM_PROMPT_V2), P2.2 改的是 **brief
        #   (user 侧)** ⇒ 哈希不变 ⇒ 该判据**机械上不会归零**, v2.12 前后的轮次会被静默
        #   混进同一序列去宣布毕业。这正是计划 P0.5 明令禁止的 ("输入分布变化后, 受影响
        #   试验按新版本分段, 不能混入旧窗口宣布毕业"), 也是 §11.6-item6 量出过的同一盲区
        #   (v2.10→v2.11 已实际发生过一次)。
        #   本改动只**采集分段键**, 不改判据本身 —— 判据的切分键要不要从 prompt_hash 扩到
        #   brief_template_version 属"改判据", 须用户裁 (DEB-20260910 §17.6 已列为待裁项)。
        #   不落这两个键的后果是: 即便用户裁了要分段, 历史轮次也**无从分段**(数据没采)。
        "brief_template_version": BRIEF_TEMPLATE_VERSION,
        "brief_template_hash": _LAST_BRIEF_FINGERPRINTS.get("brief_template_hash"),
        "brief_content_hash": _LAST_BRIEF_FINGERPRINTS.get("brief_content_hash"),
        "brief_material_count": _LAST_BRIEF_FINGERPRINTS.get("brief_material_count"),
        "off": off, "on": on,
    })
    print(f"[thinking-eval] 已落对比文件: {_THINKING_EVAL_PATH.name} (record-only, 未下单)")
    return 0 if (off["ok"] or on["ok"]) else 1


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


# [2026-09-18] 推送执行闸分解 (纯读当日 inject 拒绝归因 jsonl; 不重算、不落盘、不改行为)
_REJECT_LABELS = {
    "streak_gate": "streak闸",
    "low_atr": "低ATR闸",
    "price_floor": "低价闸",
    "no_quote": "无现价",
    "conf": "低置信",
    "already_in_position": "已持仓",
    "da_or_other_already_written": "已写库",
    "quota_cap": "超额",
    "cooldown": "冷却",
}


def _gated_breakdown(target: str) -> str:
    """当日被执行闸拦下的候选按原因分桶 → 推送附行; 空串 = 无被拦记录 (不占行)。

    direction(SKIP) 桶略去 — 上一条"扫描期可交易"行里的 SKIP{n} 已表达同一批。
    只读落盘产物 (inject 侧 _log_inject_reject / build 阶段同文件), 不引入新判据。
    """
    try:
        buckets: Dict[str, int] = {}
        for r in _read_jsonl(_INJECT_REJECT_PATH):
            if r.get("date") != target or r.get("stage") not in ("filter", "build", "save"):
                continue
            key = str(r.get("reject_reason") or "").split("(")[0].strip()
            if not key or key == "direction":
                continue
            label = _REJECT_LABELS.get(key, key)
            buckets[label] = buckets.get(label, 0) + 1
        if not buckets:
            return ""
        detail = " / ".join(f"{k}{v}" for k, v in sorted(buckets.items(), key=lambda x: (-x[1], x[0])))
        return f"执行闸: 拦{sum(buckets.values())}只 ({detail})"
    except Exception as e:
        print(f"  [warn] 执行闸分解读数失败: {e}")
        return ""


def _prescreen_marker(s: Dict[str, Any]) -> str:
    """[2026-09-18 登记项 P3] 逐条标记"输入侧资格预筛未过" (仅 REJECT; 其余返回空串)。

    为什么需要: 材料外浮现的标的在推送里此前与已具名且 PASS 的候选**长得一模一样**,
    读者会当成可交易候选 — 实况是它到 21:10 inject 才被 ATR/价格地板拒 (9/18 ZIM)。
    这里只**展示**扫描期已算好的三态 (prescreen_status), 不新造判据。
    """
    if str(s.get("prescreen_status") or "") != "REJECT":
        return ""
    reasons = [str(r) for r in (s.get("prescreen_reasons") or []) if r]
    return f" ⛔资格预筛未过({','.join(reasons) or 'REJECT'})"


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
    # [2026-09-18 登记项 P3] 扫描期内即可判定为"结构不可交易"(资格口径 REJECT)
    #   的 tradeable 只数 — 多为材料外浮现者 (已具名者通常在 brief 的 context_only 段
    #   就被模型避开, 但不保证)。纯计数, 不参与任何筛选; 缺标注 → 0 (fail-open)。
    n_ext_reject = sum(1 for s in tradeable if str(s.get("prescreen_status") or "") == "REJECT")
    tier_cnt: Dict[str, int] = {}
    for s in tradeable:
        t = s.get("self_tier") or "-"
        tier_cnt[t] = tier_cnt.get(t, 0) + 1

    mode = "已注入DB→执行轨道" if entry_enabled else "record-only(未下单)"
    # [DEB-20260825-ue-audit 复核新增] push 文案说真话 — 病灶: mode 只看 entry_enabled
    #   开关, 不看实际落库数。8/25 实弹: 8只浮现, 7只无现价+1只DA排重 → 实际写0条,
    #   但推送仍打"已注入DB→执行轨道" = 对外宣称与实际相反。改查当日真实落库行数。
    # [2026-09-18 推送读数对齐真实链路] 计数收窄 + 补执行闸分解行 (两处失真实测):
    #   ①原计数只按 decision_id 前缀, **把 SKIP 行算成"已注入"** ⇒ 9/18 实况: 当日该前缀
    #     仅 1 行 = US.ZIM 低ATR 的 SKIP 行, 推送打"已注入DB→执行轨道 (实际1条)", 而真落库
    #     可交易指令 = **0 条** (两条 LONG 分别被 streak 闸 / 低ATR 闸拦)。现加执行侧同款
    #     白名单 direction IN ('BUY','SELL_SHORT') (见 app/pipeline/llm_pipeline.py:833)。
    #     ⚠ 只按方向收窄, **不再过滤 skip_reason**: 9/14 实测 BUY/SELL_SHORT 行上的非空
    #     skip_reason 是**注入后**状态 (no_trigger/chase_cap/thesis_drift/TRIPWIRE), 它们是
    #     真注入过的指令; 拒收只发生在 direction='SKIP' 行 (加 skip_reason 过滤会把 9/14 的
    #     3 条误报成 1 条)。
    #   ②"可交易: N只"取 scan surfaced 的 LONG/SHORT = **扫描期口径**, 在 streak/ATR 闸之前;
    #     9/18 两只全被下游拦却仍显示"可交易2只" ⇒ 标签改"扫描期可交易" + 补一行执行闸结果
    #     (_gated_breakdown 只读当日 inject 拒绝归因 jsonl, 纯观测不重算)。
    #   回滚: 恢复原 SQL 计数与 "可交易:" 标签即可 — 本函数只读 DB + 推送文案, 零交易行为。
    _ue_injected_n = 0
    if entry_enabled:
        try:
            from app.playbook.db import PlaybookDB
            _conn_pb = PlaybookDB()._conn()
            _row_n = _conn_pb.execute(
                "SELECT COUNT(*) FROM playbook_instructions WHERE date=? "
                "AND decision_id LIKE ? "
                "AND direction IN ('BUY','SELL_SHORT')",
                (target, f"{STRATEGY_ID}:%")).fetchone()
            _ue_injected_n = int(_row_n[0] or 0) if _row_n else 0
            mode = f"已注入DB→执行轨道 (实际{_ue_injected_n}条)" if _ue_injected_n else \
                f"注入0条 (候选{len(tradeable)}只未过执行闸)"
        except Exception as _pe2:
            print(f"  [warn] 落库数查询失败: {_pe2}")
            mode = f"注入数未知 (entry_enabled=true 但DB查询失败)"
    _gate_line = _gated_breakdown(target)
    lines = [
        f"🌌 Universe选股扫描完成 ({target})",
        f"模型={rec.get('model_used','?')} prompt={rec.get('prompt_version','?')} · {mode}",
        f"扫描期可交易: {len(tradeable)}只 (多{n_long}/空{n_short}) · 池外{n_ext} · SKIP{n_skip}"
        + (f" · 结构不可交易{n_ext_reject}" if n_ext_reject else ""),
    ]
    if _gate_line:
        lines.append(_gate_line)
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
        lines.append(f"{d} {sym} {ext}{wl} {tier_sem}{px_str}{_prescreen_marker(s)}")
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
        from app.push import push_text_safe
        from app.push.message import Level as _Lv, Category as _Ct
        # [RFC-085·续2 2026-09-21] sync=True 必须: 本脚本 __main__ 末尾 os._exit()
        #   (futu SDK 非 daemon 线程) 会在 PushHub 的异步 send worker 投递前杀掉进程
        #   ⇒ 消息静默丢失, 而日志照打"[ok] 已推送"= 假绿。实弹复现: 走同一路径
        #   10 次, 本地假 webhook 实收 7~8 次。返回值 = **真实**送达结果。
        _ok = push_text_safe(msg, level=_Lv.HIGH, category=_Ct.SYSTEM, sync=True)
        if _ok:
            print("\n[ok] 已推送 (PushHub, 同步送达确认)")
        else:
            print("\n[warn] PushHub 全通道失败(未送达) — 见 data/push_failures.jsonl")
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
    p_scan.add_argument("--da-feed", action="store_true",
                        help="DA 供料早跑 (19:40 CST): 写主jsonl(scan_window=da_feed)供 DA 取 scans[-1]; "
                             "跳过R5快照; 不进 inject/push/confirm/score — 与 --midday 互斥")
    p_inj = sub.add_parser("inject", help="代码出数值→写作战计划 (仅 entry_enabled=true)")
    p_inj.add_argument("--dry-run", action="store_true", help="强制只打印不写盘")
    sub.add_parser("score", help="对已 scan 未评分标的算前瞻收益")
    p_rep = sub.add_parser("report", help="查看近 N 天效果")
    p_rep.add_argument("--days", type=int, default=30)
    p_eval = sub.add_parser("scan-eval", help="V1/V2 prompt A/B 对比 (shadow, 不落主scan/不下单)")
    p_eval.add_argument("--clue", default="", help="人工线索 (复现强事件场景做对比)")
    sub.add_parser("thinking-eval", help="glm-5.2 enable_thinking on/off A/B (shadow, 不落主scan/不下单)")
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
            return cmd_scan(manual_clue=args.clue, midday=args.midday,
                            da_feed=getattr(args, "da_feed", False))
        if args.cmd == "inject":
            return cmd_inject(force_dry=args.dry_run)
        if args.cmd == "score":
            return cmd_score()
        if args.cmd == "report":
            return cmd_report(days=args.days)
        if args.cmd == "scan-eval":
            return cmd_scan_eval(manual_clue=args.clue)
        if args.cmd == "thinking-eval":
            return cmd_thinking_eval()
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
        # [DEB-20260829 第12轮] scan 的意外异常同样断 && 链 — 与 LLM 失败分支同告警
        if args.cmd == "scan":
            _scan_fail_alert(f"执行异常: {e}", midday=bool(getattr(args, "midday", False)))
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
