#!/usr/bin/env python3
"""
LLM Synthesizer — 把采集数据变为KO示例级分析
================================================
输入: data_collector 采集的8维数据
输出: 结构化分析JSON (结论/理由/交易计划/风险)

直接调 DashScope qwen3.7-max, enable_thinking=false, json_mode=true。
复用 FTSeek deep_target_analysis.py 的 prompt 设计理念。
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import time
import hashlib
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

try:  # v0.87: 异动 payload 解析/渲染单一真源 (中立模块, 与 deep_analyzer 共用)
    from anomaly_parse import render_anomaly_section
except Exception:  # pragma: no cover — 模块缺失时不渲染该段 (缺数据不编数据)
    def render_anomaly_section(collected):  # type: ignore
        return ""

try:  # 2026-10-23: prev_close 日K核对 (独立纯函数, 不改 data_collector WIP)
    from prev_close_reconcile import reconcile_prev_close
except Exception:  # pragma: no cover
    reconcile_prev_close = None  # type: ignore

try:  # 2026-10-23: 挑战者规范状态 (PARTIAL 不再被 agreed=true 升级)
    from debate_state import classify_debate
except Exception:  # pragma: no cover
    classify_debate = None  # type: ignore

DASHSCOPE_KEY = config.DASHSCOPE_KEY
DASHSCOPE_URL = config.DASHSCOPE_URL
MODEL = config.LLM_MODEL

# v0.39: 第二个模型用于辩论挑战
# 2026-08-13 统一配置获取: 从 config.py 读取 (环境变量 CHALLENGER_MODEL 可覆盖), 不再硬编码
CHALLENGER_MODEL = config.CHALLENGER_MODEL
# 2026-09-18 (用户裁「做」): 挑战者三级链的独立档模型 (见 _run_challenger; 家族约束见 config.py)
CHALLENGER_FALLBACK_MODEL = config.CHALLENGER_FALLBACK_MODEL
CHALLENGER_FALLBACK_MODEL_2 = config.CHALLENGER_FALLBACK_MODEL_2


def _call_llm(system: str, user: str, temperature: float = config.LLM_TEMPERATURE, max_tokens: int = 3000, model: str = None,
              cache_system: bool = False, extra_body: Optional[dict] = None,
              timeout: int = 60, user_tail: str = None) -> Optional[dict]:
    """调 DashScope, 返回 JSON dict. model参数可指定不同模型.

    [v0.70 审计修复 2026-08-26] 瞬态故障单次重试 — 原单发: 19:00CST 生产窗口内
    一次网络抖动/网关502 = 该候选当日直接损失 (_error→SKIP)。仅重试传输层故障
    (超时/连接中断/HTTP 429·5xx), 间隔8s; 模型输出 JSON 解析失败不重试
    (同输入重发大概率同样失败, 白烧 token)。

    [2026-09-01 PE 审阅] cache_system=True: system 消息改 content 数组 +
    cache_control=ephemeral, 命中 DashScope 显式前缀缓存 (网关侧同格式已实弹验证:
    创建 3719tok → 第2发 cached=3719, 99.4%)。仅 DA 分析师用 (SYSTEM_PROMPT 静态,
    同轮 7± 候选共享前缀); 不改 prompt 内容, PROMPT_HASH 不变。开关:
    config.DA_PROMPT_CACHE (回滚 = DA_PROMPT_CACHE=0)。降级链: 缓存格式下全败 →
    纯字符串 system 再试一次 (排除格式兼容嫌疑), 最坏 3 次尝试。

    [2026-09-01 DEB-20260901b] extra_body: 模型特定参数透传 (合并进 payload,
    可覆盖默认 — 如 {"enable_thinking": True, "thinking_budget": 2048});
    None (默认) = 与历史行为逐字节一致。

    [2026-09-09 F3] user_tail: user 消息稳定前缀 (契约段渲染文本), 与 cache_system
    联动 — 缓存格式下 user 内容拆为 [cache_control(user_tail), user] 两块,
    在 user 消息内建第二个缓存断点, 使跨候选逐字节相同的契约段 (~6 成 user token)
    命中前缀缓存; 模型按块序读到的文本 == user_tail + user, 与单块逐字节一致。
    降级路径 (use_cache=False) 自动退化为拼接纯文本。不传 = 原行为。
    """
    import time as _time

    def _one_attempt(use_cache: bool):
        if use_cache:
            _sys_msg = {"role": "system", "content": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]}
        else:
            _sys_msg = {"role": "system", "content": system}
        if use_cache and user_tail is not None:
            _user_content = [
                {"type": "text", "text": user_tail, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": user},
            ]
        elif user_tail is not None:
            _user_content = user_tail + user  # 降级/未开缓存: 单块全文, 语义等价
        else:
            _user_content = user
        payload = {
            "model": model or MODEL,
            "messages": [
                _sys_msg,
                {"role": "user", "content": _user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        if extra_body:
            payload.update(extra_body)  # 调用方显式覆盖 (如思考开关)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DASHSCOPE_KEY}",
        }
        req = urllib.request.Request(
            DASHSCOPE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        data = None
        try:
            # [2026-09-01 复查 P0] timeout 参数化: 思考模式实测 81.3s/候选 (5276 out tok,
            #   探针实录) 越旧 60s 硬墙 — 调用方按需抬高; 默认 60 = 关思考路径逐字节原行为。
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _choice = (data.get("choices") or [{}])[0]
            text = _choice["message"]["content"]
            out = json.loads(text)
            # [DEB-20260723 H4] 记录 API 信封回显模型名 → 上游模型漂移从不可观测变为可观测
            #   (N7: DashScope 当前仅回显别名, 但别名变更/未来快照号即信号)
            if isinstance(out, dict):
                out.setdefault("_model_reported", data.get("model"))
                # [DEB-20260825 第13轮 P1] 截断与成本此前**双盲**:
                #   ①max_tokens 截断 → JSON 解析失败 → 落 _error 静默 fallback, 无法区分
                #     "模型不听话"与"字数不够"(前者改 prompt, 后者加 max_tokens, 处置相反);
                #   ②daily_llm_token_report.by_feature 无 DA 条目 (8/19-8/24 实测) = DA token 成本不可核,
                #     任何"省 token"主张都无法验证。
                out.setdefault("_finish_reason", _choice.get("finish_reason"))
                out.setdefault("_usage", data.get("usage"))
            return out, True, None  # (result, succeeded, err)
        except Exception as e:
            # 失败路径同样带回信封元信息 — 截断诊断的关键判别式是 finish_reason == "length"
            _meta = {}
            try:
                if isinstance(data, dict):
                    _meta["_finish_reason"] = (data.get("choices") or [{}])[0].get("finish_reason")
                    _meta["_usage"] = data.get("usage")
            except Exception:
                pass
            # [2026-09-18 可见性补] JSON 解析失败/截断时, 把正文尾巴一起带回 —
            #   此前只回 str(e), 无法区分"模型回了一堆解释文字"(改 prompt) 与
            #   "被 max_tokens 截断"(抬预算)。上限 200 字符, 不膨胀 checkpoint。
            try:
                if isinstance(text, str) and text:
                    _meta["_raw_len"] = len(text)
                    _meta["_raw_tail"] = text[-200:]
            except Exception:
                pass
            return {"_error": str(e), **_meta}, False, e

    result, ok, err = _one_attempt(cache_system)
    if ok:
        return result

    # 重试判定: 响应已到达但内容不可用 (JSONDecodeError/KeyError=模型输出问题)
    # 不重试 — 同输入重发大概率同样失败; 其余异常 (超时/连接中断/HTTP 429·5xx等
    # 传输层故障) 重试一次。注意 json.JSONDecodeError 是 ValueError 子类。
    if isinstance(err, (json.JSONDecodeError, KeyError)):
        return result

    _time.sleep(8)
    _retry_result, _retry_ok, _ = _one_attempt(cache_system)
    if _retry_ok:
        print(f"[llm_synth] 瞬态故障重试成功 (首次: {type(err).__name__}: {err})",
              file=sys.stderr)
        return _retry_result
    # [2026-09-01 PE 审阅] 缓存格式降级: 开了 cache_system 仍全败 → 纯字符串 system
    #   再试一次, 把"缓存数组格式不兼容"与"真故障"分开, 格式问题不伤候选。
    if cache_system:
        _deg_result, _deg_ok, _ = _one_attempt(False)
        if _deg_ok:
            print("[llm_synth] 缓存格式降级成功 (cache_system 疑似不兼容, 已回退纯字符串 system)",
                  file=sys.stderr)
            return _deg_result
    print(f"[llm_synth] 重试仍失败 (首次: {type(err).__name__}: {err})",
          file=sys.stderr)
    return _retry_result


# [DEB-20260907 修订考古 — 只留代码注释, 不进 prompt 文本; 修订历史复述进 prompt 会重新注入锚点]
# 本轮改动 (四路多agent交叉审阅后用户裁"直接修复"):
#   1. KO 真实示例 → 合成占位示例 ACME (消除内容锚, 保留形态锚+反例)
#   2. "多空各至少2条" → 合计2-6/方向侧≥2/反方允许0条 (治凑数; 校验器已同步)
#   3. A-F 混合强度清单 → 三条条件式机检条款 (A/B 激励由 score_band 判据①吸收)
#   4. 删除虚假止损拦截规则 (阈值=0 不拦, 真实机制=桶边界钳制) + 方向性硬门移出
#      (飞刀/追涨/破位由规则层单一实现: FTSeek pipeline.py B_long_knife / chase_cap 门)
#   5. 删除"命中率约50%"全局心理锚 (均匀降档压力源, 与 7.0 塌缩同族机制)
#   6. USER 模板删 confidence 五档自评 (实证零门禁消费: quality_score 判定链不消费,
#      tier 只用 score+辩论一致度; signal_gate.py:94 仅剩 record-only 落库),
#      新增 evidence_gaps[] 承接不确定性出口 (护栏: SKIP 率升幅 ≤10pp)
#   7. 删除"(未验证)/估算"例外 (与反虚构硬规矛盾)
#   8. 证伪官: 删 my_score−1 扣分制 (Goodhart 压报诱导); 允许 SKIP (弃权中性, 代码
#      本有承接分支); 新增 falsification_target/flip_condition (AGREE 必填, 治仪式化)
#   回滚: cp scripts/llm_synth.py.bak-deb20260907 scripts/llm_synth.py (pipeline.py 同理)
SYSTEM_PROMPT = """你是一位顶级美股投资研究分析师。以下用合成示例股 ACME(虚构公司, 数字为占位符)
展示论据质量水准——禁止复用示例中的任何具体数字/事件到你的真实输出; 你的每个数字必须来自
本次输入数据, 复用示例内容会被幻觉核验判为来源不明。

【论据质量示例(合成占位, 只示范形态)】
示例bull_case论据(注意形态: 每条公司独有、可溯源到具体数据段、含具体数字):
- "Q1营收+X%、EPS超共识+Y%, 净利率Z%"(基本面 — 每个数字必须能在"基本面指标"段找到出处)
- "某大客户约N家门店改用本公司产品"(公司层面独有事件, 须来自新闻事实流, 不是通用"营收增长")
- "某业务拟分拆IPO"(独有催化, 从输入新闻中挖掘; 输入没有就不写)
示例bear_case论据:
- "M家机构目标价均值仅余+Q%, PE高于行业中位"(独有计算: 目标价均值+剩余空间, 两个数字都要有出处)
- "高管近3个月零买入、套现约$V"(内部人时序数据, 须引自"内部人交易/内部人情绪"段)
- "散户情绪一边倒看多约U%偏拥挤"(情绪数据+反向警示, 须引自情绪段)
反例(不合格, 禁止输出): "营收增长强劲/估值偏高/近期可能回调" — 无数字、无出处、可移植到任何股票。

【核心原则】
1. 每条论点必须引用具体数据(数字+日期)，不接受无数据观点
2. 多空对等审视、论据按证据取舍: bull_case+bear_case 合计 2-6 条, 最终方向一侧至少 2 条,
   反方允许 0 条——写不出有数据支撑的反方论据就留空, 留空是诚实输出、下游不惩罚;
   禁止为凑数补"估值偏高/情绪可能反转"这类通用修辞。每条标注强度(HIGH/MEDIUM/LOW)和来源类型。
   方向由证据权重决定，不预设偏向——数据支持看空就坚定给SELL_SHORT，不必强凑看多论据；数据支持看多同理
3. 因果链至少3层(宏观→板块→个股 或 事件→估值→价格)
4. 交易计划必须有具体价位和条件触发，不是"逢低买入"这种废话
5. 如果某维度数据缺失，直接说明影响并降低确信度
6. 禁止生成模板式免责废话
7. 所有论点必须是该股独有的，不能复制到其他股票

【论据质量的机检条件】(触发条件不满足时对应条款不适用)
- 拥挤警示: 当输入含散户情绪数据且一边倒(>85%看多或看空)时, bear_case 或 risks 中
  必须有 1 条 claim_type=sentiment_crowded 的论据
- 估值锚定: 当输入同时含本股 PE/PB 与行业中枢时, valuation_* 论据必须同时含
  本股值与行业中枢两个数字 (不能只说"PE 27.7"而不说"高于行业中位 22.3")
- 风险日历: 当输入含财报日历且财报日落在持有期内时, risks 必须含具体日期
  (并标注"财报前3日不开仓")
- 入场计划只写1个可执行首批(价位+触发条件); 后续加仓只能作为研究背景, 不写第二个可执行触发价、不承诺成交 (见【关键要求】)
数据覆盖不足时按【数据充分性路由】处理, 禁止为凑深度而编造数字。

【与执行层的接口事实】
1. 止损钳制: 你给出的止损距离会被执行层按波动桶钳制到合理边界(约1.5-5.8%, 视持有期与方向),
   不会因此拒单。直接给 2-5% 区间内的止损即可; 小于2%易落入噪声扫损区
2. 方向性硬门(接飞刀拦截/破位硬买拦截/追涨降级)由执行层代码统一执行, 不依赖你的自我审查:
   被拦的指令会被规则层拒单, 价格回升出拦截区后自动复评。你只需按证据给方向与价位;
   若你判断暴跌是错杀(恐慌性抛售+基本面完好+大资金承接), 照常给 LONG 并把错杀证据写进 thesis,
   入场时机由执行层裁定。
   ⚠️ 飞刀几何一致性 (2026-09-14 新增, 机器可执行): 执行层飞刀门(B门)对 LONG 的语义是
   "实时价 vs 昨收 < -5% 拒入场, 回升出飞刀区自动放行"。因此当盘前涨跌 < -5% 且你仍给 LONG:
   ① chase_cap_price 必须 ≥ 昨收×0.95 — 否则价格一旦回升出飞刀区(可成交时刻)必然越过
      你的禁追线, 被追高闸终态拒单, 指令结构性不可成交 (实证 9/14: HPE 昨收62.09/禁追58.00
      < 飞刀线58.98 → 空集; NOK 昨收11.13/禁追10.50 < 10.57 → 空集);
   ② entry_price/confirmation.price_level 允许在飞刀线下方 (那是低吸确认位, 执行层会等回升
      后按当前锚复评, 不是限价单), 但 chase_cap 是"可成交区上界", 必须与飞刀线兼容;
   ③ 若你的证据只支持在飞刀区深处接货、不支持回升到 -5% 以内后买入, 应给 SKIP 并在
      skip_reason 写右侧回补计划 (什么条件出现后重新评估), 而不是产出永不成交的指令。
   ⚠️ 当前执行能力 (2026-10-23): 系统只按**一个首批/单一执行锚**下单; `confirmation` 是研究确认位, 不是限价单;
   执行价/止损/止盈会按实时行情与波动率重算。**不要写"第二腿必成交"或第二个可执行触发价**; 后续加仓只能作为研究背景。
   ⚠️ 若你仍写了第二腿触发价 P2, 机器只做记录、不会执行; 最安全是只写首批、不写 P2。
   同理: cap 是"计划锚附近的追价容忍上界", 不要把它写成 invalidation 的突破失效位 (那是另一个字段)。
3. 盘前异动是当日趋势的第一个信号 (2026-08-11 新增): 若数据段给出盘前/盘后
   涨跌幅, 你必须显式表态 — 判断它是"趋势延续"(顺势方向入场)还是"过度反应"
   (逆势博反弹, 需强证据)。所有价位(入场/止损/止盈)必须锚定【有效现价】
   (盘前时段=盘前成交价), 严禁锚定昨收价 — 锚定昨收的入场带开盘后永不触发
   或开盘即深度套牢 (实证: LIF 盘前-19.5% 仍按昨收出 BUY 指令, thesis 已死)。

【校准原则 — 信息 edge 判定】
- score_band 由锚定判据逐条数出(见输出契约), 不由信心感觉决定: HIGH 的三条判据
  必须全部真实满足, 缺一不可; 不满足就给 MEDIUM/LOW
- 信息 edge 统一判定测试 (2026-08-25 修复旧两条规则互斥, DEB-20260825 第12轮§C):
  判定依据不是新闻的"年龄"，而是**该信息是否已被价格反映**。判断材料 = 盘前异动
  (盘前涨跌幅/盘前成交量, 见⚡盘前时段数据段; ⚠️盘前时段量比/换手率为上一交易日收盘
  口径, 不得用于判断今日信息是否已被反映 — RFC-133 时点错配修正):
  · 若信息(含 24h 内发布的 8-K/突发)已伴随盘前大幅同向异动(放量确认) → 已被价格反映
    → 无增量 edge，该论据只能作为背景陈述；若全部论据均属此类 → score_band 给 LOW
    (判据③: 全部论据已被价格反映); 若因此给 SKIP, 在 skip_reason 写明"无增量信息"
  · 若信息尚未被反映(盘前无异动/量能不支撑)且可溯源核实 → 这是真 edge 来源，
    可作为高分论据，但必须在 thesis 中给出"尚未被价格反映"的证据(盘前数据引用)
- 多空对称适用: 上述纪律对 BUY 与 SELL_SHORT 完全一致 — 不存在"某方向天然更需谨慎"
  的全局规则; 唯一允许的方向不对称是结构性风险核验 (见 SHORT 风险三问: 空头理论亏损
  无上限, 故挤空为必答题), 不是历史胜率偏见
- 真正的 edge 来源是: 市场还不知道的信息(如刚发布的8-K/盘前突发/内部人异动)，
  而不是已经在价格中反映的旧新闻。你必须区分这两者。
"""


def _fundamental_view(snap: Dict[str, Any]) -> Dict[str, Any]:
    """[v0.70 审计修复 2026-08-26] collect_snapshot 真键 → prompt/落盘统一视图。

    根因: 本模块原读 change_pct/pe_ttm/eps/market_cap/dividend_yield/high_52w/
    low_52w, 而 data_collector.collect_snapshot 真键是 pe_ttm_ratio/
    total_market_val/highest52weeks_price/lowest52weeks_price, 且根本不产出
    change_pct/eps/dividend_yield → 实测 100% 分析里 LLM 看到 涨跌幅+0.0%/PE=0/
    EPS=0/市值$0.0B/52周高低$0, 叠加 USER_PROMPT"PE为负或0必须在bear_case提及"
    = 每只股票被强制注入一条虚假"亏损公司"看空论据 (SKILL.md pitfall #22 实例)。
    修法: 以 collect_snapshot 返回结构为单一真源就地归一化 (presentation 层);
    涨跌幅由 last_price/prev_close 现算; eps/dividend_yield 数据源不提供 → None
    (渲染为"数据未采集"), 不再伪装成 0。回滚: cp scripts/llm_synth.py.bak-deb20260826-chainaudit
    """
    def _nonzero_number(value):
        try:
            number = float(value)
            return number if number != 0 else None
        except (TypeError, ValueError):
            return None

    view = {
        "pe_ttm": _nonzero_number(snap.get("pe_ttm_ratio")),
        "pe_ratio": _nonzero_number(snap.get("pe_ratio")),
        "pb_ratio": _nonzero_number(snap.get("pb_ratio")),
        "eps": None,
        "dividend_yield": None,
        "market_cap": _nonzero_number(snap.get("total_market_val")),
        "high_52w": _nonzero_number(snap.get("highest52weeks_price")),
        "low_52w": _nonzero_number(snap.get("lowest52weeks_price")),
    }
    try:
        lp = float(snap.get("last_price", 0) or 0)
        pc = float(snap.get("prev_close", 0) or snap.get("prev_close_price", 0) or 0)
        view["change_pct"] = round((lp - pc) / pc * 100.0, 2) if (lp > 0 and pc > 0) else None
    except Exception:
        view["change_pct"] = None
    return view


def _fmt_or_na(v, fmt=None):
    """[v0.70] None(数据未采集) 渲染为显式 N/A, 数值按 fmt 渲染 — 防 0 假象。"""
    if v is None:
        return "数据未采集(N/A)"
    return f"{v:{fmt}}" if fmt else str(v)


def _list_source_status(value, reported_status: Optional[str] = None):
    if isinstance(value, dict):
        return "failed"
    if not isinstance(value, list):
        return "failed"
    status = getattr(value, "source_status", reported_status)
    if status is None:
        return "ok" if value else "empty"
    return str(status)


def _fmt_unix_et(value: Any) -> str:
    """unix 秒/毫秒/ISO → ET 显示时间; 无法解析返回空串 (不编时间)."""
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        if ts > 1e9:
            from datetime import datetime as _dt
            try:
                from zoneinfo import ZoneInfo as _ZI
                tz = _ZI("America/New_York")
            except Exception:
                tz = None
            d = _dt.fromtimestamp(ts, tz) if tz else _dt.utcfromtimestamp(ts)
            return d.strftime("%m-%d %H:%M") + ("ET" if tz else "UTC")
    except (TypeError, ValueError):
        pass
    return s


def _source_key(item: Dict[str, Any]) -> str:
    """跨栏目同稿键: 优先 news_id, 其次 URL 的 id/规范 URL, 最后标题."""
    if not isinstance(item, dict):
        return ""
    nid = item.get("news_id") or item.get("id")
    if nid not in (None, ""):
        return "id:" + str(nid)
    url = str(item.get("url") or "").strip()
    if url:
        p = urlparse(url)
        qid = parse_qs(p.query).get("id")
        if qid:
            return "url_id:" + str(qid[0])
        return "url:" + p._replace(query="", fragment="").geturl().rstrip("/")
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip().lower()
    return "title:" + title if title else ""


def _source_age_days(value: Any, ref_ts: float) -> Optional[int]:
    try:
        ts = float(str(value))
        if ts > 1e12:
            ts /= 1000.0
        if ts > 1e9:
            return max(0, int((ref_ts - ts) / 86400.0))
    except (TypeError, ValueError):
        pass
    return None


def _research_time_label(item: Dict[str, Any], ref_ts: float) -> str:
    """研报时间真源: publish_time 优先; 缺失显式标未知, 过期显式标龄.

    2026-10-23: 原渲染只读 r['time'], 10/22 实测 100/100 条研报渲染为空,
    模型无法区分当日催化与 37-42 天前报告. 本改动只补时点, 不改研报内容.
    """
    pub = item.get("publish_time")
    label = _fmt_unix_et(pub) or _fmt_unix_et(item.get("time"))
    if not label:
        return "发布时间未知"
    age = _source_age_days(pub, ref_ts)
    if age is not None and age > 7:
        label += f" (⚠️{age}天前)"
    return label


def _format_data_for_prompt(collected: Dict[str, Any]) -> str:
    """把8维采集数据格式化为 LLM prompt 输入"""
    _source_truth = os.environ.get("DA_SOURCE_TRUTH_FIX", "1") != "0"
    snap = collected.get("snapshot", {})
    _fs = _fundamental_view(snap)  # [v0.70] 字段错配修复, 见 _fundamental_view docstring
    _chg_s = ("+" if (_fs["change_pct"] or 0) >= 0 else "") + f"{_fs['change_pct']:.1f}" \
        if _fs["change_pct"] is not None else "数据未采集(N/A)"
    klines = collected.get("kline", [])
    if not isinstance(klines, list):
        klines = []
    cap_dist = collected.get("capital_distribution", {})
    cap_flow = collected.get("capital_flow", {})
    tech_anom = collected.get("tech_anomaly", {})
    cap_anom = collected.get("cap_anomaly", {})
    deriv_anom = collected.get("deriv_anomaly", {})
    news = collected.get("news", [])
    research = collected.get("research", [])
    # [2026-10-12 v5审计修复] 同 sentiment: executor 异常会写入 {"_error":..} dict,
    #   list 型源统一在读取处归一化, 防 L534/L557 渲染迭代 dict 键崩溃。
    if not isinstance(news, list):
        news = []
    if not isinstance(research, list):
        research = []

    # ── P2 数据充分性路由 (DEB-20260814): 头部注入覆盖统计 + 现价缺失显式告警 ──
    # 让 LLM 在分析前就知道数据完整度, 触发 prompt 里的【数据充分性路由】规则。
    # 借鉴 financial-data-query "空结果如实降级"纪律: 数据不足时给 SKIP 而非硬凑价位。
    # [2026-08-31 DA链复查] 覆盖口径单一真源: 原头部读 collect_all._quality 宽松口径
    #   (空壳也算 ok → 恒 19/19=100%), 与下方审计段严格口径矛盾并存。现严格模式下
    #   头部与审计段共用 _classify_data_sources (19维, 缺失+空壳皆计缺失)。
    #   回滚: DA_COVERAGE_STRICT=0 → 头部回旧 _quality 口径, 审计段回旧 16 源局部重算。
    _strict_cov = os.environ.get("DA_COVERAGE_STRICT", "1") != "0"
    _cov_total = _cov_ok = 0
    _cov_degraded: List[str] = []
    _cov_missing: List[str] = []
    if _strict_cov:
        _cov_total, _cov_ok, _cov_degraded, _cov_missing = _classify_data_sources(collected)
    _cov_header = ""
    try:
        if _strict_cov:
            _total, _ok = _cov_total, _cov_ok
        else:
            _q = collected.get("_quality", {}) or {}
            _total = int(_q.get("total", 0) or 0)
            _ok = int(_q.get("ok", 0) or 0)
        # 现价有效性判断 (与 snapshot effective_price 口径一致)
        _px = float(snap.get("last_price", 0) or snap.get("effective_price", 0) or 0)
        if _total:
            # 严格口径: 缺失+空壳皆不可作论据 → 一并计入; 宽松口径: total-ok
            _missing_n = (len(_cov_missing) + len(_cov_degraded)) if _strict_cov else (_total - _ok)
            _cov_header = (f"## 📊 数据覆盖统计: {_ok}/{_total} 维度有效"
                           + (f", {_missing_n} 项缺失/降级" if _missing_n else "") + "\n")
            # [DEB-20260825 r15 P8锚点修复] 数据充分性标记由代码算好注入 —
            #   原 prompt 锚点"≥12/18维有效"有两病: ①采集器实际19维, "18"是错的;
            #   ②模型无法自行清点维度数, 实操退化为感觉档 (违反"能由代码判定
            #   一律不交给模型"准则)。阈值与【数据充分性路由】单一真源对齐:
            #   缺失<6维 = 充分 (路由规则"≥6缺失优先SKIP"的镜像), 且与 LOW
            #   条件②"缺失≥6维"边界互斥自洽。模型只读本行标记, 不再自行计数。
            _cov_header += ("- score_band 锚点③【数据充分性】: "
                            + ("充分 ✓ (缺失维度<6)" if _missing_n < 6
                               else f"不充分 ✗ (缺失{_missing_n}维≥6)") + "\n")
            if _px <= 0:
                _cov_header += ("- ⚠️ 实时快照/现价缺失(=$0)。按【数据充分性路由】: direction 只能 SKIP, "
                                "禁止输出任何具体价位。\n")
            elif _missing_n >= 6:
                _cov_header += (f"- ⚠️ 缺失维度≥6, 按【数据充分性路由】优先 SKIP; "
                                f"仍给方向时按数据不充分处理(score_band 落入 LOW 判据②)。\n")
    except Exception:
        _cov_header = ""
    sentiment = collected.get("sentiment", [])
    # [2026-10-12 v5审计修复] executor 异常路径会写入 {"_error": ...} dict,
    #   原代码直接进 L576 sentiment[:5] → TypeError 崩溃。dict 型源渲染端均有
    #   "_error" not in 守卫, list 型源此处补齐: 非 list 一律视为缺失(与空 list 同义),
    #   失败详情由 _quality/coverage 段呈现, 不在此泄漏错误文本。
    if not isinstance(sentiment, list):
        sentiment = []

    sections = []

    # P2: 数据覆盖统计置于最前 (LLM 先看到数据完整度, 再看具体数据)
    if _cov_header:
        sections.append(_cov_header)

    _status_notes = []
    _source_statuses = collected.get("_source_status", {})
    if not isinstance(_source_statuses, dict):
        _source_statuses = {}
    for label, key in (
        ("Futu新闻", "news"),
        ("Finnhub新闻", "news_v2"),
        ("研报", "research"),
        ("社区情绪", "sentiment"),
    ):
        status = _list_source_status(collected.get(key), _source_statuses.get(key))
        if status == "failed":
            _status_notes.append(f"- {label}: FAILED(采集失败; 不等于无新闻/无情绪, 禁止写成'没有催化剂')")
        elif status == "empty":
            _status_notes.append(f"- {label}: EMPTY(源成功但无结果)")
        elif status == "disabled":
            _status_notes.append(f"- {label}: DISABLED(配置关闭)")
    if _status_notes:
        sections.append("## 数据源状态\n" + "\n".join(_status_notes))

    # 基本面快照 ([v0.70] 键名对齐 collect_snapshot 真源; 缺失项显式 N/A 非 0)
    # [2026-08-31 DA链复查] _session 前移: 盘前时段此处"当前价格"实为昨收
    #   (last_price), 显式标注防模型锚定昨收 (与 LIF 盲区修复同一病灶;
    #   下方 ⚡盘前段给出真实现价)。
    _session = str(snap.get("session", "") or "")
    _px_note = " (⚠️盘前时段此值为昨收, 有效现价见盘前段)" \
        if _session == "premarket" and (snap.get("pre_price") or 0) > 0 else ""
    # 2026-09-14 (盘前数据有效性审计 P2): 快照的**累计型**字段在盘前/盘后是
    #   "上一交易日收盘"口径 —— 换手率/振幅/量比/成交量均由最近一个**完整** session
    #   算出, 盘前不刷新 (DA 跑 06:22 ET 盘前)。原实现只给"当前价格"加了昨收提示,
    #   这几项裸奔 ⇒ 模型把昨日量能当今日量能 (实证风险: "量比 2.1x 放量" 实为昨日,
    #   据此判"今日有资金进场") → 顺风/逆风与 confidence 判断错位。
    #   同 LIF 盲区病灶家族: 时点不清的值必须标注, 不能静默混用。
    _t1_note = ""
    if _session in ("premarket", "afterhours"):
        _t1_note = ("\n- ⚠️ 口径: 上方 成交量/换手率/振幅/量比/买卖盘比 均为**上一交易日收盘**"
                    "口径 (盘前/盘后不刷新 —— 此处盘前量比实为昨日量能, 不是今日放量);"
                    " 今日量能只能看下方盘前段的盘前成交量。")
    sections.append(f"""## 标的基本面
- 代码: {snap.get('code','')}
- 名称: {snap.get('name','')}
- 当前价格: ${snap.get('last_price',0)}{_px_note}
- 涨跌幅: {_chg_s}%
- 成交量: {snap.get('volume',0):,}
- 换手率: {snap.get('turnover_rate',0)}%
- PE(TTM): {_fmt_or_na(_fs['pe_ttm'])}
- PE(动态): {_fmt_or_na(_fs['pe_ratio'])}
- PB: {_fmt_or_na(_fs['pb_ratio'])}
- EPS: {_fmt_or_na(_fs['eps'])}
- 总市值: {f"{_fs['market_cap']/1e9:.1f}B" if _fs['market_cap'] else "数据未采集(N/A)"}
- 股息率: {_fmt_or_na(_fs['dividend_yield'])}%
- 52周最高: {_fmt_or_na(_fs['high_52w'], '.2f')}
- 52周最低: {_fmt_or_na(_fs['low_52w'], '.2f')}
- 振幅: {snap.get('amplitude',0)}%
- 买盘/卖盘比: {snap.get('bid_ask_ratio',0)}
- 量比: {snap.get('volume_ratio',0)}{_t1_note}""")

    # 2026-08-11 (LIF 盲区修复): 盘前/盘后时段段 — 盘前波动是当日趋势第一个信号。
    #   盘前时段上方"当前价格"=昨收(正股收盘价), 必须显式给出盘前真实价与涨跌幅,
    #   否则 LLM 按昨收出价位 (实证: LIF 盘前-19.5% 仍按昨收出 BUY, 入场带永不可达)。
    #   (_session 已在上文标的基本面处计算)
    if _session == "premarket" and (snap.get("pre_price") or 0) > 0:
        sections.append(f"""## ⚡ 盘前时段数据 (当前处于盘前, 这是今日趋势的第一个信号)
- 盘前现价(有效价格): ${snap.get('pre_price',0)}
- 盘前涨跌幅(vs昨收): {snap.get('pre_change_rate',0):+.2f}%
- 隔夜涨跌幅: {snap.get('overnight_change_rate',0):+.2f}%
- 盘前成交量: {snap.get('pre_volume',0):,.0f}股
- 昨日收盘价: ${snap.get('prev_close_price',0)}
- ⚠️ 上方"当前价格"是昨日正股收盘价, 不是今日真实价。入场/止损/止盈必须锚定盘前现价 ${snap.get('pre_price',0)}。
- ⚠️ 你必须对盘前波动显式表态: 趋势延续(顺势入场) 还是 过度反应(逆势博反弹, 需强证据)。""")
    elif _session == "afterhours" and (snap.get("after_price") or 0) > 0:
        sections.append(f"""## ⚡ 盘后时段数据 (当前处于盘后)
- 盘后现价(有效价格): ${snap.get('after_price',0)}
- 盘后涨跌幅(vs昨收): {snap.get('after_change_rate',0):+.2f}%
- ⚠️ 上方"当前价格"是正股收盘价。若出指令, 价位必须锚定盘后现价 ${snap.get('after_price',0)}。""")

    # K线技术面
    if klines:
        last = klines[-1] if klines else {}
        closes = [k.get("close", 0) for k in klines]
        volumes = [k.get("volume", 0) for k in klines]
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else 0
        ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else 0
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else 0
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else 0
        avg_vol_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
        vol_ratio = volumes[-1] / avg_vol_20 if avg_vol_20 else 0
        chg5 = ((closes[-1] - closes[-6]) / closes[-6] * 100) if len(closes) >= 6 else 0
        chg20 = ((closes[-1] - closes[-21]) / closes[-21] * 100) if len(closes) >= 21 else 0

        # 近5日K线
        recent_5 = klines[-5:] if len(klines) >= 5 else klines
        kline_text = "\n".join([
            f"  {k.get('time','')}: O={k.get('open',0):.2f} H={k.get('high',0):.2f} "
            f"L={k.get('low',0):.2f} C={k.get('close',0):.2f} V={k.get('volume',0):,}"
            for k in recent_5
        ])

        sections.append(f"""## 技术面 (60日K线)
- MA5: {ma5:.2f} | MA10: {ma10:.2f} | MA20: {ma20:.2f} | MA60: {ma60:.2f}
- 5日涨跌: {chg5:+.1f}% | 20日涨跌: {chg20:+.1f}%
- 量比(20日均量): {vol_ratio:.2f}x
- 近5日K线:
{kline_text}""")

    # 资金面 — 从capital_distribution提取(资金分布:大单/中单/小单)
    if isinstance(cap_dist, dict) and "_error" not in cap_dist and cap_dist.get("code") == 0:
        in_super = cap_dist.get("capital_in_super", 0) or 0
        out_super = cap_dist.get("capital_out_super", 0) or 0
        in_big = cap_dist.get("capital_in_big", 0) or 0
        out_big = cap_dist.get("capital_out_big", 0) or 0
        in_mid = cap_dist.get("capital_in_mid", 0) or 0
        out_mid = cap_dist.get("capital_out_mid", 0) or 0
        in_small = cap_dist.get("capital_in_small", 0) or 0
        out_small = cap_dist.get("capital_out_small", 0) or 0
        net_main = (in_super + in_big) - (out_super + out_big)
        total_in = in_super + in_big + in_mid + in_small
        total_out = out_super + out_big + out_mid + out_small
        main_pct = (net_main / (total_in + total_out) * 100) if (total_in + total_out) else 0
        sections.append(f"""## 资金面
- 主力净流入: {net_main/1e8:.2f}亿
- 超大单净: {in_super - out_super:.0f}
- 大单净: {in_big - out_big:.0f}
- 主力占比: {main_pct:.2f}%""")
    elif isinstance(cap_flow, dict) and "_error" not in cap_flow:
        # capital_flow返回 {code:0, data:[{in_flow, super_in_flow, ...}]}
        flow_data = cap_flow.get("data", [])
        if isinstance(flow_data, list) and flow_data:
            latest = flow_data[-1] if isinstance(flow_data[-1], dict) else {}
            sections.append(f"""## 资金面 (分时)
- 最新主力流入: {latest.get('main_in_flow', 'N/A')}
- 超大单流入: {latest.get('super_in_flow', 0)}
- 大单流入: {latest.get('big_in_flow', 0)}""")

    # 异动信号
    # [v0.87] schema 错配修复: 旧代码 len(adata) 把 dict 键数当条数 → 恒报「4条异动」,
    #   err_code=1「无异常」也谎报有条目, 而 content 真实异动文本 (2026-09 实测 474/642
    #   payload=74% 有效) 从不进 prompt。现走 anomaly_parse.render_anomaly_section 单一
    #   真源: 逐条文本 + 真实条数 + 「无异动」/「采集失败」显式区分 (缺数据不编数据)。
    #   9/24 ORCL 实锤: 被丢的 content 含「9/23 收盘跌破MA5/MA10/MA30 + MA10死叉 → 下行
    #   动能延续」(与当日 BUY 直接冲突) + 事件当晚 03:40/02:51 机构卖出看涨大单。
    _anom_sec = render_anomaly_section(collected)
    if _anom_sec:
        sections.append(_anom_sec)

    # 新闻
    # [v0.86] 时间戳修复: futu news 的字段是 publish_time (unix 秒 string), 旧代码读
    #   n.get('time') 恒空 → prompt 渲染 "[] 标题", LLM 无法分辨 91 秒前还是 3 天前的
    #   新闻 (9/24 ORCL 不可抗力 flash 实锤: 延迟感知为零)。转 ET HH:MM 渲染;
    #   解析失败回退原 time 字段/空串 (缺时间不编时间)。
    def _news_time_et(n):
        _pt = n.get("publish_time") or n.get("time") or ""
        try:
            _ts = float(str(_pt))
            if _ts > 1e9:  # unix 秒
                from datetime import datetime as _dt
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    _tz = _ZI("America/New_York")
                except Exception:
                    _tz = None
                _d = _dt.fromtimestamp(_ts, _tz) if _tz else _dt.utcfromtimestamp(_ts)
                return _d.strftime("%m-%d %H:%M") + ("ET" if _tz else "UTC")
            return str(_pt)
        except Exception:
            return str(_pt)
    _ref_ts = float(collected.get("_timestamp") or time.time())
    _seen_source_keys = {
        _source_key(_n) for _n in news
        if isinstance(_n, dict) and _source_key(_n)
    }
    if news:
        news_text = "\n".join([
            f"  [{_news_time_et(n)}] {n.get('title','')}"
            for n in news[:8]
        ])
        sections.append(f"## 最新新闻 (共{len(news)}条, 时间为ET)\n{news_text}")

    # P1 (2026-08-14): Finnhub 带摘要新闻事实流 — 新闻维度深度升级
    # 开关 DA_NEWS_V2=0 时采集端即返回空, 此处自然不显示。
    news_v2 = collected.get("news_v2", [])
    if isinstance(news_v2, list) and news_v2:
        _low = news_v2[0].get("low_relevance", False)
        _v2_lines = []
        for _n in news_v2:
            _k = _source_key(_n) if isinstance(_n, dict) else ""
            _dup_tag = (" [与新闻同稿]" if (_source_truth and _k and _k in _seen_source_keys)
                        else "")
            if _k:
                _seen_source_keys.add(_k)
            _line = (f"  [{_n.get('time','')}] ({_n.get('source','')})"
                     f"{_dup_tag} {_n.get('title','')}")
            if _n.get("summary"):
                _line += f"\n    摘要: {_n['summary']}"
            _v2_lines.append(_line)
        _v2_hdr = ("## Finnhub 新闻事实流 (近7天, 含摘要 — 新闻类论据必须引用此处事实)"
                   + (" [低相关度降级: 标题少含ticker, 已取最新N条]" if _low else ""))
        sections.append(_v2_hdr + "\n" + "\n".join(_v2_lines))

    # 研报
    # 2026-10-23: ①读 publish_time 补日期/龄 (原只读 time, 100/100 为空);
    #   ②与新闻共用 news_id/URL 的条目不再作为独立研报重复注入 (10/22 实测 51 条).
    if research:
        if not _source_truth:
            # 回滚: DA_SOURCE_TRUTH_FIX=0 → 旧渲染 (只读 time, 不去重)
            _res_text = "\n".join([
                f"  [{_r.get('time','')}] {_r.get('title','')}"
                for _r in research[:5] if isinstance(_r, dict)
            ])
            sections.append(f"## 研报 (共{len(research)}条)\n{_res_text}")
        else:
            _res_unique, _res_dup = [], 0
            for _r in research:
                if not isinstance(_r, dict):
                    continue
                _k = _source_key(_r)
                if _k and _k in _seen_source_keys:
                    _res_dup += 1
                    continue
                if _k:
                    _seen_source_keys.add(_k)
                if len(_res_unique) < 5:
                    _res_unique.append(_r)
            if _res_unique:
                _res_text = "\n".join([
                    f"  [{_research_time_label(_r, _ref_ts)}] {_r.get('title','')}"
                    for _r in _res_unique
                ])
            else:
                _res_text = "  （无独立研报: 全部与新闻同稿, 已省略重复证据）"
            _res_note = f", 去重{_res_dup}条与新闻同稿" if _res_dup else ""
            sections.append(f"## 研报 (共{len(research)}条{_res_note})\n{_res_text}")

    # 社区情绪
    # [v0.86] 爆发速率行: 30 条帖子只进聚合/标题, 丢了时间密度信号 (9/24 ORCL 实锤:
    #   采集前 5 分钟 23 条刷屏, 比新闻 flash 早 4 分钟, prompt 零体现)。publish_time
    #   已在 checkpoint, 只加计数渲染, 零新采集。阈值 5min≥15 条标"爆发" (ORCL 案 23 条;
    #   常规标的 30 条跨 ~70min → 5min 窗典型 <5 条, 不误标)。
    if sentiment:
        sections.append(f"## 社区讨论 (共{len(sentiment)}条帖子)")
        try:
            _pts = sorted(float(s["publish_time"]) for s in sentiment
                          if s.get("publish_time"))
            if _pts:
                _burst = sum(1 for p in _pts if p >= _pts[-1] - 300)
                _span_min = round((_pts[-1] - _pts[0]) / 60, 1)
                _burst_line = (f"  发帖节奏: 最近一条前5分钟内 {_burst} 条 / 全部{len(_pts)}条跨 {_span_min} 分钟"
                               + (" ⚠️ 爆发式刷屏 (事件驱动嫌疑, 结合新闻判断方向)" if _burst >= 15 else ""))
                sections.append(_burst_line)
        except Exception:
            pass  # 时间解析失败 → 不渲染节奏行, 不编数字
        for i, s in enumerate(sentiment[:5]):
            import re, html
            raw = s.get("title", "") + " " + s.get("desc", "")
            # 先去HTML标签，再反转义HTML实体（如 &#38328; → 闪）
            text = re.sub(r'<[^>]+>', '', raw)
            text = html.unescape(text).strip()
            if len(text) > 5:
                sections.append(f"  帖子{i+1}: {text[:120]}")

    # 基本面指标 (Finnhub)
    fund = collected.get("fundamentals", {})
    if isinstance(fund, dict) and "_error" not in fund:
        sections.append(f"""## 基本面指标 (Finnhub, TTM)
- 营收增长(YoY): {fund.get('revenue_growth_yoy','N/A')}%
- 营收增长(5年CAGR): {fund.get('revenue_growth_5y','N/A')}%
- EPS增长(5年): {fund.get('eps_growth_5y','N/A')}%
- ROE: {fund.get('roe_ttm','N/A')}%
- ROA: {fund.get('roa_ttm','N/A')}%
- 净利润率: {fund.get('net_profit_margin_ttm','N/A')}%
- 毛利率: {fund.get('gross_margin_ttm','N/A')}%
- 营业利润率: {fund.get('operating_margin_ttm','N/A')}%
- PE(标准化): {fund.get('pe_normalized','N/A')}
- PB: {fund.get('pb_annual','N/A')}
- PS: {fund.get('ps_annual','N/A')}
- 每股营收: {fund.get('revenue_per_share_ttm','N/A')}
- 每股净资产: {fund.get('book_value_per_share','N/A')}
- 负债/权益比: {fund.get('debt_to_equity','N/A')}
- 股息率: {fund.get('dividend_yield','N/A')}%
- 派息率: {fund.get('payout_ratio','N/A')}%
- Beta: {fund.get('beta','N/A')}""")

    # 分析师评级 (Finnhub)
    analyst = collected.get("analyst_consensus", {})
    if isinstance(analyst, dict) and "_error" not in analyst:
        sections.append(f"""## 分析师评级 (Finnhub, {analyst.get('period','')})
- 强烈买入: {analyst.get('strong_buy',0)} | 买入: {analyst.get('buy',0)} | 持有: {analyst.get('hold',0)} | 卖出: {analyst.get('sell',0)} | 强烈卖出: {analyst.get('strong_sell',0)}
- 覆盖分析师: {analyst.get('total_analysts',0)}位
- 买入占比: {analyst.get('buy_pct',0)}%""")

    # 内部人交易 (Finnhub)
    insider_tx = collected.get("insider_transactions", {})
    if isinstance(insider_tx, dict) and "_error" not in insider_tx:
        txs = insider_tx.get("recent_transactions", [])
        tx_text = "\n".join([
            f"  {t.get('date','')} {t.get('name','')} {t.get('type','')} "
            f"shares={t.get('shares','')} price=${t.get('price','')}"
            for t in txs[:5]
        ])
        sections.append(f"""## 内部人交易 (Finnhub)
- 最近交易: {insider_tx.get('recent_buy_count',0)}笔买入 / {insider_tx.get('recent_sell_count',0)}笔卖出
- 最近交易总额: ${insider_tx.get('total_recent_value',0):,}
{tx_text}""")

    # 内部人情绪 (Finnhub MSPR)
    insider_sent = collected.get("insider_sentiment", {})
    if isinstance(insider_sent, dict) and "_error" not in insider_sent:
        sections.append(f"""## 内部人情绪 (Finnhub MSPR)
- 6个月平均MSPR: {insider_sent.get('avg_mspr_6m',0)} ({insider_sent.get('signal','')})
- MSPR>0=净买入, <0=净卖出""")

    # 公司概况 (Finnhub)
    profile = collected.get("company_profile", {})
    if isinstance(profile, dict) and "_error" not in profile:
        sections.append(f"""## 公司概况
- 行业: {profile.get('industry','N/A')}
- 交易所: {profile.get('exchange','N/A')}
- 国家: {profile.get('country','N/A')}""")

    # v0.39新增: 大盘关联 (SPY/QQQ/VIX)
    mkt = collected.get("market_context", {})
    mkt_parts = []
    if isinstance(mkt, dict) and "_error" not in mkt:
        spy = mkt.get("spy", {})
        qqq = mkt.get("qqq", {})
        vix = mkt.get("vix", {})
        market_trend = mkt.get("market_trend", {})
        if isinstance(market_trend, dict):
            premarket = market_trend.get("premarket", {})
            daily = market_trend.get("daily", {})

            def _format_change(value):
                return "N/A" if value is None else f"{float(value):+.2f}%"

            trend_parts = []
            if isinstance(premarket, dict) and premarket.get("trend") not in (None, "UNKNOWN"):
                trend_parts.append(
                    f"盘前{premarket.get('trend')} "
                    f"(SPY {_format_change(premarket.get('spy_chg_pct'))}, "
                    f"QQQ {_format_change(premarket.get('qqq_chg_pct'))})")
            if isinstance(daily, dict) and daily.get("trend") not in (None, "UNKNOWN"):
                trend_parts.append(f"日线{daily.get('trend')}")
            if trend_parts:
                mkt_parts.append("- 大盘趋势(代码判定, SPY/QQQ): " + "; ".join(trend_parts))
        # 2026-09-24 (DEB-20260924 F1 方案B): SPY/QQQ 的 current 是**日线口径最后一根收盘**;
        #   盘前那根就是昨日 bar ⇒ 不标注 = 让模型把"昨收"读成"今天大盘在涨/持平"
        #   (实测 25/25 份 raw.json: spy.current 恒 = prev_close)。同 VIX (9/14 P0-2) 做法:
        #   口径可疑的值必须自带标签, 且盘前腿缺失时显式**禁止推断**而不是静默留白。
        def _idx_caliber(_d: dict) -> str:
            _asof = _d.get("current_asof") or "unknown"
            _s = _d.get("snapshot")
            _lab = f" [日线口径, 基准日={_asof} 收盘"
            if isinstance(_s, dict) and "_error" not in _s and _s.get("pre_chg_pct") is not None:
                _lab += (f"; 今日盘前 {_s['pre_chg_pct']:+.2f}% (盘前价 {_s.get('pre_price')} / "
                         f"昨收 {_s.get('prev_close')}; 快照 {_s.get('snapshot_time')} "
                         f"{_s.get('session','')})")
            else:
                _lab += "; 盘前快照不可用 — 禁止推断今日盘前指数涨跌"
            return _lab + "]"

        if isinstance(spy, dict) and "_error" not in spy:
            mkt_parts.append(
                f"- SPY: ${spy.get('current',0)} (MA20={spy.get('ma20',0)}, "
                f"趋势={spy.get('trend','?')}, 5日{spy.get('chg_5d_pct',0):+.1f}%, "
                f"20日{spy.get('chg_20d_pct',0):+.1f}%)" + _idx_caliber(spy)
            )
        if isinstance(qqq, dict) and "_error" not in qqq:
            mkt_parts.append(
                f"- QQQ: ${qqq.get('current',0)} (MA20={qqq.get('ma20',0)}, "
                f"趋势={qqq.get('trend','?')}, 5日{qqq.get('chg_5d_pct',0):+.1f}%)" + _idx_caliber(qqq)
            )
        if isinstance(vix, dict) and "_error" not in vix:
            vix_val = vix.get("current", 0)
            vix_tag = " (高波动⚠️)" if vix_val and vix_val > 25 else (" (低波动)" if vix_val and vix_val < 15 else "")
            # 2026-09-14 (P0-2): 如实标注代理口径 —— DA 跑盘前, 而 VIX 现货无盘前交易,
            #   该值来自 US.VIXY×0.85 (contango 校准, backwardation 时偏差变大)。
            #   不给口径标签 = 让模型以为这是现货 VIX 读数, 据此下"波动率已定价"结论。
            _vix_lab = ""
            if vix.get("proxy"):
                _vix_lab = (f" [代理值: {vix.get('proxy')}×{vix.get('proxy_factor')} 校准, "
                            f"VIXY=${vix.get('vixy_price')}; VIX 现货无盘前交易]")
            mkt_parts.append(f"- VIX: {vix_val}{vix_tag} 今日{vix.get('chg_pct',0):+.1f}%{_vix_lab}")
        else:
            # 2026-09-14 (P0-2): VIX 缺失必须**显式写进 prompt**并禁止编造。
            #   原实现静默跳过该行 ⇒ 模型看不到 VIX 却仍可能引用具体数值 (幻觉源),
            #   同时 19 维覆盖审计因 market_context 有 spy/qqq 而记 ok ⇒ 缺失不可见。
            #   同 DA-A1 "行业PE中枢"先例: 代码能算/能取的缺口就写进 prompt。
            mkt_parts.append(
                "- VIX: **不可用** (VIXY 代理取数失败) — 禁止在论据/可观测指标里引用任何"
                " VIX 具体数值; 波动率判断只能依据 SPY/QQQ 与个股自身数据"
            )
    scan_condition = str(mkt.get("scan_condition", "") or "").strip() if isinstance(mkt, dict) else ""
    if scan_condition:
        mkt_parts.append(f"- 扫描层市况(当日候选池广度): {scan_condition}")
    if mkt_parts:
        sections.append("## 大盘环境\n" + "\n".join(mkt_parts))

    # v0.39新增: 行业peer对比
    peers_data = collected.get("industry_peers", {})
    if isinstance(peers_data, dict) and "_error" not in peers_data:
        industry = peers_data.get("industry", "N/A")
        peers = peers_data.get("peers", [])
        avg_5d = peers_data.get("industry_avg_chg_5d_pct", 0)
        avg_today = peers_data.get("industry_avg_chg_today_pct", 0)
        industry_trend = peers_data.get("industry_trend", {})
        industry_trend_line = ""
        if isinstance(industry_trend, dict):
            trend_today = industry_trend.get("today")
            trend_five_day = industry_trend.get("five_day")
            trend_combined = industry_trend.get("combined")
            if "UNKNOWN" not in (trend_today, trend_five_day, trend_combined):
                industry_trend_line = (
                    f"- 行业趋势(代码判定, peer均值): 今日{trend_today}, "
                    f"5日{trend_five_day}, 综合{trend_combined}\n")
        peer_lines = []
        for p in peers[:5]:
            peer_lines.append(
                f"  {p.get('symbol','')}: ${p.get('price',0)} "
                f"今日{p.get('chg_pct',0):+.1f}% 5日{p.get('chg_5d_pct',0):+.1f}% "
                f"PE={p.get('pe',0)}"
            )
        # [DA-A1 2026-08-29, DEB-20260829 第3轮批准] 估值中枢代码化注入 —
        #   prompt 硬规D要求"PE必须与行业中枢对比", 但此前输入只给 peer 各自 PE 无中枢,
        #   模型自编中枢 → 被幻觉校验器罚 (D1/D2 病灶)。代码现算中位数注入一行,
        #   模型直接引用不再编造。同步进 _extract_source_numbers 白名单。
        #   数据段渲染改动不动 PROMPT_HASH (指纹只含 prompt 文本, 与 v0.70 先例一致)。
        _pe_med = _industry_pe_median(collected)
        med_line = ""
        if _pe_med:
            _m, _n, _nout = _pe_med
            med_line = (f"- 行业PE中位数: {_m} (由{_n}家peer算得"
                        + (f", 剔除{_nout}家离群)" if _nout else ")") + "\n")
        sections.append(
            f"## 行业对比 ({industry})\n"
            f"- 行业平均: 今日{avg_today:+.1f}% 5日{avg_5d:+.1f}%\n"
            + industry_trend_line
            + med_line
            + f"- peer明细 (共{peers_data.get('peer_count',0)}只):\n" + "\n".join(peer_lines)
        )

    # 财报日历 (Finnhub)
    earnings = collected.get("earnings_calendar", {})
    if isinstance(earnings, dict) and "_error" not in earnings:
        next_e = earnings.get("next_earnings")
        if next_e:
            from datetime import datetime
            # [v0.70 审计修复] month-based DST 近似 → 真实时区 (3月初/11月初各~2周错档)
            try:
                from zoneinfo import ZoneInfo as _ZI
                _ET_TZ = _ZI("America/New_York")
            except Exception:
                from pytz import timezone as _ptz
                _ET_TZ = _ptz("America/New_York")
            today = datetime.now(_ET_TZ).strftime("%Y-%m-%d")
            days_to = ""
            try:
                d1 = datetime.strptime(today, "%Y-%m-%d")
                d2 = datetime.strptime(next_e.get("date",""), "%Y-%m-%d")
                days_to = f" (距今{(d2-d1).days}天)"
            except Exception:
                pass
            sections.append(f"""## 财报日历
- 下次财报: {next_e.get('date','')}{days_to}
- EPS预期: {next_e.get('epsEstimate','N/A')} | 营收预期: {next_e.get('revenueEstimate','N/A')}
- ⚠️ 财报前3日不宜开新仓""")

    # 数据质量审计 — 三级评估：完整(有信息量) / 降级(API返回但内容空壳) / 缺失
    # [2026-08-31 DA链复查] 严格模式复用头部同源 _classify_data_sources (19维,
    #   含 news_v2/market_context/industry_peers), 消除头部与审计段双口径矛盾。
    #   DA_COVERAGE_STRICT=0 保留旧 16 源局部重算 (回滚路径)。
    if _strict_cov:
        total, ok_count = _cov_total, _cov_ok
        degraded, missing = list(_cov_degraded), list(_cov_missing)
    else:
        all_sources = {
            "snapshot": snap,
            "kline": klines,
            "capital_distribution": cap_dist,
            "capital_flow": cap_flow,
            "tech_anomaly": tech_anom,
            "cap_anomaly": cap_anom,
            "deriv_anomaly": deriv_anom,
            "news": news,
            "research": research,
            "sentiment": sentiment,
            "fundamentals": collected.get("fundamentals", {}),
            "analyst_consensus": collected.get("analyst_consensus", {}),
            "insider_transactions": collected.get("insider_transactions", {}),
            "insider_sentiment": collected.get("insider_sentiment", {}),
            "company_profile": collected.get("company_profile", {}),
            "earnings_calendar": collected.get("earnings_calendar", {}),
        }
        total = len(all_sources)
        ok_count = 0
        degraded = []   # API返回了但内容空壳（如capital_flow全0）
        missing = []    # 完全缺失

        for name, val in all_sources.items():
            if isinstance(val, dict):
                if "_error" in val or not val:
                    missing.append(name)
                else:
                    # 检查是否是空壳：所有值都是0/None/空列表
                    meaningful = _has_meaningful_data(val)
                    if meaningful:
                        ok_count += 1
                    else:
                        degraded.append(name)
            elif isinstance(val, list):
                if val:
                    # 列表非空但可能全是空壳——检查第一个元素
                    if isinstance(val[0], dict) and _has_meaningful_data(val[0]):
                        ok_count += 1
                    elif not isinstance(val[0], dict):
                        ok_count += 1  # 简单值列表，非空就算
                    else:
                        degraded.append(name)
                else:
                    missing.append(name)
            else:
                missing.append(name)

    audit_parts = [
        f"## 数据质量审计",
        f"- 数据源总数: {total}",
        f"- 完整(有信息量): {ok_count}项",
        f"- 降级(空壳): {len(degraded)}项",
        f"- 缺失: {len(missing)}项",
    ]
    if degraded:
        audit_parts.append(f"- 降级明细: {','.join(degraded)} (API返回但无实质数据，相关论据应降级)")
    if missing:
        audit_parts.append(f"- 缺失明细: {','.join(missing)}")
    # 诚实告诉LLM哪些维度不可用
    if degraded or missing:
        audit_parts.append("- ⚠️ 降级/缺失维度不可作为论据依据，必须在data_quality_note中说明影响")
    sections.append("\n".join(audit_parts))

    # ── 论据溯源清单：把输入的关键数字列出，供LLM引用 ──
    source_numbers = _extract_source_numbers(collected)
    if source_numbers:
        sections.append(f"## 论据溯源清单（以下为输入数据中的真实数字，LLM输出必须引用这些，不得编造）\n{source_numbers}")

    # 2026-07-28 (板块级联批): 板块上下文 — prompt 要求"宏观→板块→个股"因果链但此前
    #   材料里无板块事实 (LLM 只能靠内部知识编板块层; 07-27 Technology 最弱仍 12/12 LONG)。
    #   由 pipeline 注入 collected["sector_context"], 无则不展示 (兼容旧调用)。
    _sec_ctx = collected.get("sector_context") or ""
    if _sec_ctx:
        sections.append(f"## 板块上下文（真实轮动数据, 板块层因果必须引用此处事实; 逆板块风方向须自证为何成立）\n{_sec_ctx}")

    # UE→DA 候选管道 (2026-08-03): 事件扫描器浮现的假设, 待用真实数据验证
    _ue_ctx = collected.get("ue_candidate_context") or ""
    if _ue_ctx:
        sections.append(f"## 事件扫描器假设（UE候选, 待验证 — 方向以你的独立分析为准）\n{_ue_ctx}")

    # ── 前序回吐率 shadow (2026-08-14): 该标的历史大涨日(>+5%)后的守成/回吐统计 ──
    # 纯K线统计, 零LLM; 只作参考证据注入, 不构成方向依据 (IC验证未过前)。
    # 回滚: 环境变量 DA_PULLBACK_SHADOW=0 或删除 pullback_shadow.json。
    if os.environ.get("DA_PULLBACK_SHADOW", "1") != "0":
        try:
            _pb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pullback_shadow.json")
            if os.path.exists(_pb_path):
                _pb = json.load(open(_pb_path)).get(collected.get("symbol", ""), {})
                if _pb and _pb.get("spike_count", 0) >= 3:
                    sections.append(
                        f"## 前序回吐率（历史统计 shadow, 近1年大涨>+5%后表现）\n"
                        f"- 大涨样本: {_pb['spike_count']}次 | 5日守成率: {_pb.get('hold_5d','N/A')}% | "
                        f"5日后均涨幅: {_pb.get('avg_fwd_5d','N/A')}% → {_pb.get('momentum_bias','N/A')}\n"
                        f"- 3日/10日: 守成 {_pb.get('hold_3d','N/A')}%/{_pb.get('hold_10d','N/A')}%, "
                        f"均涨 {_pb.get('avg_fwd_3d','N/A')}%/{_pb.get('avg_fwd_10d','N/A')}%\n"
                        f"- 用法: 若本次触发源于大涨, 该统计反映该标的自身的涨幅留存性格; "
                        f"样本少(<10)时仅作弱参考。"
                    )
        except Exception:
            pass  # shadow 失败不影响主链路

    return "\n\n".join(sections)


def _has_meaningful_data(d: dict) -> bool:
    """检查dict是否有信息量——不是全0/None/空"""
    if not d:
        return False
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if isinstance(v, (list, dict)) and v:
            return True
        if isinstance(v, (int, float)) and v != 0:
            return True
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("n/a", "none", "null", ""):
            return True
    return False


def _classify_data_sources(collected: Dict[str, Any]) -> Tuple[int, int, List[str], List[str]]:
    """[2026-08-31 DA链复查] 采集维度严格三级分类 — 头部覆盖统计与审计段共用单一真源。

    口径与旧审计段相同 (_has_meaningful_data): 完整(有信息量)/降级(空壳)/缺失;
    较旧审计新增 news_v2/market_context/industry_peers 三维 → 与 collect_all 的
    19 个任务对齐, 头部"X/N 维度有效"与审计段"数据源总数"从此同数同源。
    根因: collect_all._quality 是宽松口径 (空 dict/空 list 也算 ok), 实测恒
    19/19=100%, 与审计段严格口径并存 = 同一 prompt 内两套矛盾覆盖数字, 模型
    自行计数输出矛盾数据 (生产实证: data_quality_note "15完整/1降级/3缺失"
    把 EPS/股息率字段当维度数)。
    返回 (total, ok_count, degraded_names, missing_names)。
    """
    all_sources = {
        "snapshot": collected.get("snapshot", {}),
        "kline": collected.get("kline", []),
        "capital_distribution": collected.get("capital_distribution", {}),
        "capital_flow": collected.get("capital_flow", {}),
        "tech_anomaly": collected.get("tech_anomaly", {}),
        "cap_anomaly": collected.get("cap_anomaly", {}),
        "deriv_anomaly": collected.get("deriv_anomaly", {}),
        "news": collected.get("news", []),
        "news_v2": collected.get("news_v2", []),
        "research": collected.get("research", []),
        "sentiment": collected.get("sentiment", []),
        "fundamentals": collected.get("fundamentals", {}),
        "analyst_consensus": collected.get("analyst_consensus", {}),
        "insider_transactions": collected.get("insider_transactions", {}),
        "insider_sentiment": collected.get("insider_sentiment", {}),
        "company_profile": collected.get("company_profile", {}),
        "earnings_calendar": collected.get("earnings_calendar", {}),
        "market_context": collected.get("market_context", {}),
        "industry_peers": collected.get("industry_peers", {}),
    }
    ok_count = 0
    degraded = []   # API返回了但内容空壳（如capital_flow全0）
    missing = []    # 完全缺失
    for name, val in all_sources.items():
        if isinstance(val, dict):
            if "_error" in val or not val:
                missing.append(name)
            elif _has_meaningful_data(val):
                # 2026-09-14 (P0-2): market_context 是三合一复合源 (spy/qqq/vix)。
                #   原实现只要有**一个**子项有值就整项记 ok ⇒ VIX 子项**结构性恒缺失**
                #   (富途不供 US.VIX 指数快照, 实测 `_error: empty`;
                #   DEB-20260818-futu-capability-audit.md:89,144) 在 19 维覆盖审计里
                #   完全不可见。而 prompt 里 VIX>25 是"降低所有做多信号"的依据 ——
                #   缺口不可见 ⇒ 模型无据可依却仍可能引用具体 VIX 数值。
                #   改为: 子项缺失即降级并**具名列出**。计数口径不变 (total 仍 19),
                #   只是 ok_count 不再把"半个 market_context"记成满分。
                if name == "market_context":
                    _sub_miss = [k for k in ("spy", "qqq", "vix")
                                 if not isinstance(val.get(k), dict)
                                 or "_error" in val.get(k, {})]
                    if _sub_miss:
                        degraded.append(f"market_context({'/'.join(_sub_miss)}缺失)")
                        continue
                ok_count += 1
            else:
                degraded.append(name)
        elif isinstance(val, list):
            if val:
                if isinstance(val[0], dict) and _has_meaningful_data(val[0]):
                    ok_count += 1
                elif not isinstance(val[0], dict):
                    ok_count += 1  # 简单值列表，非空就算
                else:
                    degraded.append(name)
            else:
                missing.append(name)
        else:
            missing.append(name)
    return len(all_sources), ok_count, degraded, missing


def _industry_pe_median(collected: Dict[str, Any]) -> Optional[Tuple[float, int, int]]:
    """[DA-A1 2026-08-29, DEB-20260829 第3轮批准] 从 collected["industry_peers"] 算行业 PE 中位数。

    返回 (中位数, 参与家数, 剔除离群数) 或 None (有效 peer<3 / 数据缺失)。
    离群剔除 (分歧4防护): |pe−中位数|/中位数 > 2 剔除 — 比 3σ 稳健:
      peer 数≤5 时 σ 估计本身不稳。以中位数为锚, 剔除后不足3家则回退全量(防御)。
    零新数据源 — 只用已采集的 peers 现算。
    """
    peers_data = collected.get("industry_peers", {})
    if not isinstance(peers_data, dict) or "_error" in peers_data:
        return None
    pes = []
    for p in (peers_data.get("peers") or []):
        try:
            pe = float(p.get("pe", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pe > 0:
            pes.append(pe)
    if len(pes) < 3:
        return None
    import statistics
    med = statistics.median(pes)
    if med <= 0:
        return None
    kept = [pe for pe in pes if abs(pe - med) / med <= 2.0]
    n_out = len(pes) - len(kept)
    if len(kept) < 3:  # 防御: 离群剔除后不足3家 → 回退全量
        kept, n_out = pes, 0
    return round(statistics.median(kept), 2), len(kept), n_out


def _extract_source_numbers(collected: Dict[str, Any]) -> str:
    """从输入数据中提取关键数字，生成溯源清单供LLM引用。
    LLM输出的每个数字必须在这个清单中能找到，否则视为编造。
    """
    lines = []
    snap = collected.get("snapshot", {})
    if snap and "_error" not in snap:
        for field in ["last_price", "pe_ratio", "pe_ttm_ratio", "pb_ratio",
                       "total_market_val", "highest52weeks_price", "lowest52weeks_price",
                       "amplitude", "bid_ask_ratio", "volume_ratio", "turnover_rate"]:
            v = snap.get(field, 0)
            if v and v != 0:
                lines.append(f"- {field}={v}")

    fund = collected.get("fundamentals", {})
    if isinstance(fund, dict) and "_error" not in fund:
        for field in ["revenue_growth_yoy", "roe_ttm", "net_profit_margin_ttm",
                       "pe_normalized", "beta", "debt_to_equity"]:
            v = fund.get(field)
            if v is not None and v != "":
                lines.append(f"- {field}={v}")

    insider = collected.get("insider_sentiment", {})
    if isinstance(insider, dict) and "_error" not in insider:
        mspr = insider.get("avg_mspr_6m")
        if mspr is not None:
            lines.append(f"- avg_mspr_6m={mspr}")

    analyst = collected.get("analyst_consensus", {})
    if isinstance(analyst, dict) and "_error" not in analyst:
        for field in ["strong_buy", "buy", "hold", "sell", "total_analysts", "buy_pct"]:
            v = analyst.get(field)
            if v is not None and v != 0:
                lines.append(f"- {field}={v}")

    earnings = collected.get("earnings_calendar", {})
    if isinstance(earnings, dict) and "_error" not in earnings:
        next_e = earnings.get("next_earnings", {})
        if next_e:
            lines.append(f"- next_earnings_date={next_e.get('date','N/A')}")

    # [DA-A1 2026-08-29] 行业PE中位数进白名单 — 与行业对比段注入同源同值,
    #   保证模型引用该数字时校验器判 verified (预注册判据: 中枢引证100%可溯源)。
    _pe_med = _industry_pe_median(collected)
    if _pe_med:
        lines.append(f"- industry_pe_median={_pe_med[0]}")

    return "\n".join(lines) if lines else ""


# [2026-09-09 F3 缓存重排] USER 模板拆为变动头 (_USER_HEAD, 含 {data}) 与稳定契约段
#   (_USER_CONTRACT, 跨候选逐字节相同)。v1 = 两段拼接 (回滚路径); v2 = 契约前置序 (默认,
#   见文件尾部 USER_PROMPT_STABLE_V2 与 build_user_prompt)。
_USER_HEAD = """请基于以下真实数据，对该标的进行深度多空辩论分析。

{data}

---

"""
_USER_CONTRACT = """请输出严格的JSON格式（不要markdown包裹），结构如下：
{{
    "executive_summary": "一句话结论+核心动作（必须具体，如：Q1营收+12%但新高日量能平平，回调到81.7分2批建仓）",
    "rating": "强烈看多/看多/持有/观望/看空/强烈看空",
    "score_band": "HIGH/MEDIUM/LOW (conviction确信度档位, 方向无关: 高确信看空=HIGH, 禁止用LOW表达看空; 方向只由direction字段表达)。锚定判据(可判定, 逐条数, 不要凭感觉给档):
      HIGH = 同时满足全部3条: ①bull/bear_case中至少1条claim_type为product_catalyst或earnings_beat(公司独有事件) ②核心信息未被价格反映(盘前同向异动|涨跌幅|<2%, 或盘前成交量未见放量, 以⚡盘前时段数据段为准; 注意量比/换手率是上一交易日口径, 不得用于本判据) ③数据头部标记\"数据充分性: 充分 ✓\"(该标记由系统按实际采集结果算好, 直接读, 禁止自行估计维度数)
      LOW = 满足任一: ①多空证据互斥强度相当 ②数据头部标记\"数据充分性: 不充分 ✗\" ③全部论据均已被价格反映(盘前已放量同向异动)
      MEDIUM = 不满足HIGH也不满足LOW
    另: 兼容字段 "score": 0-10数字 (可留空, 系统将以score_band为准重算)",
    "direction": "BUY/SELL_SHORT/SKIP",
    "skip_reason": "direction=SKIP时必填: 具体写明缺哪个维度数据/哪条证据不足/双模型反向改判等观望原因; 非SKIP留空",
    "bull_case": [
        {{"thesis": "具体论点(必须有数据)", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据引证", "source_type": "technical/fundamental/news/sentiment/macro", "claim_type": "论据类型枚举"}}
    ],
    "bear_case": [
        {{"thesis": "具体论点", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据引证", "source_type": "...", "claim_type": "论据类型枚举"}}
    ],
    "constraints": [
        {{"thesis": "强制约因素", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据"}}
    ],
    "causal_chain": "3步以上因果传导链(用→连接)",
    "execution_plan": {{
        "entry_strategy": "具体入场策略(只写1个可执行首批: 价位+触发条件; 后续加仓只能写研究背景, 不承诺成交)",
        "entry_archetype": "BREAKOUT / PULLBACK_LIMIT / FADE_SHORT (枚举语义与判定规则见【关键要求】)",
        "confirmation": {{
            "price_level": "首批研究确认位数字(不是订单; 执行端按当前锚重算; 设计即市价直入时填null)",
            "side": "研究方向条件(below=回落到确认位附近 / above=反弹到确认位附近); 执行端按当前锚复评, 不保证成交",
            "ttl_min": "研究确认位有效分钟数(仅供研究背景; 执行端使用自己的确认窗, 不由本字段驱动)"
        }},
        "chase_cap_price": "禁止追涨/追空的最高(买)/最低(空)价位数字(如thesis/entry_strategy写明禁止追涨/追空则填, 否则null)。⚠️方向语义: BUY填上限(必须>entry_price, 价格涨过它=追高禁买); SELL_SHORT填下限(必须<entry_price, 价格跌破它=追空禁卖)。它**不是**invalidation的突破失效位 — SHORT的上破失效位(如'放量突破$X前高')写进invalidation/stop_loss, 绝不填这里",
        "stop_loss": "止损价位(具体数字)",
        "take_profit": "止盈目标(具体数字)",
        "entry_price": 具体入场价数字,
        "stop_loss_price": 具体止损价数字,
        "take_profit_price": 具体止盈价数字,
        "invalidation": "使交易逻辑失效的条件(一句话总述)",
        "tripwires": [
            {{"metric": "可观测指标(如: 下次财报EPS/NAND合约价/VIX/收盘价)", "threshold": "量化阈值(如: 低于$5B / 连两季-10% / 跌破$186.5)", "deadline": "观测截止日或事件日期", "source": "数据出处(输入数据中的哪条事实)"}}
        ],
        "invalidation_triggers": [
            {{"metric": "限本股可判定指标(收盘价/量比/5日涨跌/盘前涨跌幅)", "operator": "<或>", "value": "纯数字", "deadline": "日期"}}
        ]
    }},
    "key_levels": {{
        "current": 当前价,
        "support": [支撑价位列表],
        "resistance": [阻力价位列表]
    }},
    "risks": ["风险1", "风险2", "风险3"],
    "data_quality_note": "数据完整性说明(几项完整/几项缺失及影响)",
    "evidence_gaps": ["证据缺口数组, 可空: 你需要但输入中没有的数据、无法核实的关键假设。有缺口但仍给方向时写这里(不确定性出口), 不要写进 skip_reason; 无缺口填空数组"]
}}

【关键要求】
- bull_case/bear_case的每条论据必须锚定本股: 含 ≥1 个来自输入数据的数字; 本股特有命名实体不能替代数字锚点; 纯定性通用句将被机检记为 generic 违约
- execution_plan的价位必须是具体数字，不是"逢低买入"
- tripwires必须≥2条且全部可量化观测: 每条写清metric+threshold+deadline, 禁止"近期走弱/情绪恶化"这类无法判定真假的修辞; 写不出量化证伪条件说明thesis本身不成立, 应降低 score_band 档
- invalidation_triggers≥2条 [P10 第14轮, 机器可核销]: 与 tripwires 的区别是——tripwires 是叙事性观测(可为外部标的/事件), invalidation_triggers 必须是**本股、代码可判定**的失效条件: metric 限 {{收盘价, 量比, 5日涨跌, 盘前涨跌幅}}, operator 限 </>, value 为纯数字。收盘价单位=USD; 量比=当日累计成交量÷最近20个完整交易日平均成交量(无量纲, 如2.0); 两个涨跌幅指标单位=百分比百分点(如-5)。当前下游 tripwire_checker 仅做影子评估, `tripwire_checker_enforce=false` 时不修改指令; 恢复自动核销须另行裁决。写不出2条本股可判定条件, 说明 thesis 失效边界不清, 应降低 score_band 档
- 如果PE<0，说明该公司亏损，必须在bear_case中提及；PE=0或缺失只能写入evidence_gaps，禁止当作"亏损/无盈利"论据
- 如果5日涨幅>20%，必须在risks中提及追高风险
- direction必须与rating一致：看多→BUY，看空→SELL_SHORT，持有/观望→SKIP
- SHORT 风险三问 (2026-08-25 重构: 删除已过时的"近14日命中29%"胜率论据 — 历史胜率不是
  方向偏见的好理由; 保留的是结构性风险核验: 空头理论亏损无上限, 挤空是独有尾部风险):
  direction=SELL_SHORT 时必须在 thesis 内回答三问 — ①具体下跌催化剂是什么(仅"估值高/
  涨多了"不合格) ②催化剂时效多少小时 ③逼空风险自评(拥挤空头/低流通/逼空燃料)。
  三问答不全 → 改 SKIP 并在 skip_reason 说明缺哪问。不禁做空。
  多空镜像义务 (对称): direction=BUY 时 bull_case 同样必须给出同等具体的上行催化剂与
  时效 — "跌多了该反弹"与"涨多了该跌"同属不合格论据。
- bull_case/bear_case的每条必须标注claim_type，从以下枚举中选择最匹配的：
  product_catalyst(产品/合约/IPO/管理层变动等公司独有事件)
  earnings_beat(营收/EPS/利润率超预期或高增长)
  valuation_cheap(PE/PB低于行业中枢) / valuation_expensive(PE/PB高于行业中枢)
  insider_bearish(高管净卖出/MSPR为负) / insider_bullish(高管净买入/MSPR为正)
  technical_breakout(突破/金叉/站上均线) / technical_breakdown(破位/死叉/跌破均线)
  sentiment_crowded(散户一边倒/反向警示)
  macro_tailwind(降息/政策顺风) / macro_headwind(加息/政策逆风)

【论据独有性——必须】(KO分级制已改为机检条件, 见系统指令, 此处保留硬约束)
- bull_case/bear_case 每条论据必须含 ≥1 个输入数据中的数字锚点;
  公司独有事件/独有计算为高分来源(已由 score_band 判据①激励), 无则允许基于技术面/资金面/估值给方向, 禁止为凑事件或数字编造
- 如果提供了analyst_consensus数据(分析师评级)，必须在bull/bear_case中引用买入占比和分析师数量
- 如果提供了insider_sentiment(MSPR)数据，必须引用具体MSPR数值并说明方向(净买入/净卖出)
- 如果提供了earnings_calendar，必须在risks中标注"财报前3日不开仓"及具体日期
- entry_strategy 只写1个可执行首批(价位+触发条件); 后续加仓只能作为研究背景, **不写第二个可执行触发价、不承诺成交**
- 当前执行端只按单一首批/执行锚下单; confirmation 是研究确认位, 不是限价单; 执行价/止损/止盈会按实时行情重算
- entry_archetype [DEB-20260901, 机器可执行语义]: 从以下枚举选一, 与entry_strategy语义必须一致 —
  BREAKOUT=突破/放量追入(价格向上突破确认位才买, 或向下跌破才空);
  PULLBACK_LIMIT=等回踩低吸(价格**回落**到确认位才买, 禁止追高; entry_strategy含"低吸/回踩/承接/企稳接"时必选此项);
  FADE_SHORT=等反弹做空(价格**反弹**到确认位才空, 不接飞刀)。
  判定以策略意图为准: "低吸/锚定XX回踩"=PULLBACK_LIMIT, "突破XX追入/放量上攻"=BREAKOUT。
- confirmation.price_level=首批研究确认位数字(不是订单, 不是盘前现价锚; 执行端按当前锚重算, 除非设计即市价直入);
  chase_cap_price=文中"禁止在$X以上追涨"的X(无禁追语句则null)。⚠️SHORT 对称语义 [RFC-120, 机器可执行]:
  SHORT 的 cap = "禁止在$X以下追空"的X, 是价格**下限**, 必须 < entry_price — 执行层判定"下单价 < cap
  = 追空违例拦截"。反例(实证9/30三条死指令): SELL_SHORT entry=298 时填 cap=302(理由"放量突破$302.35
  前高失效") → 这是上破失效位不是禁追线, 下单价298天然<302 → 出生即违例永不可成交。上破失效位写进
  invalidation 或 stop_loss; cap 应填如 298×0.95≈283 的追空下限。BUY 对称: cap 必须 > entry_price,
  填成下方支撑位同样出生即违例。三字段写不出=策略语义不清, 应降低 score_band 档
  ⚠️**cap 除外**: 无禁追语句时 **cap=null 是正确输出**, 不算"写不出"(执行层对 null 不拦, 不会因此漏风控); 但给了
  非 null 值就必须满足下面全部几何关系。若你在"给 null"与"给个大概的数"之间犹豫, **给 null**。
  ⚠️ 若你仍写第二腿触发价 P2, 机器只做记录、不会执行; 最安全是只写首批、不写 P2。
- 飞刀几何一致性(下游机检, 判据见系统指令【与执行层的接口事实】第2条): LONG 且盘前涨跌<-5%时,
  chase_cap_price 必须 ≥ 昨收×0.95(飞刀线), 否则指令在执行层结构性不可成交;
  证据只支持飞刀区深处接货 → direction 给 SKIP + skip_reason 写右侧回补计划
- 当前执行不执行第二腿; 建议不写第二个可执行触发价, 只保留首批。
- data_quality_note必须量化：直接照抄数据中"数据质量审计"段的数字(完整X/降级Y/缺失Z)，禁止自行清点维度；并说明降级/缺失维度对分析结论的具体影响

【数据充分性路由——硬性约束】(2026-08-14, DEB-20260814 P2, 借鉴 financial-data-query 空结果纪律)
- 数据头部标注了本次数据覆盖统计 (X/N, N=本次采集总维度数, 非固定值) 与"数据充分性"标记。若"实时快照/现价"缺失或为0:
  direction 只能给 SKIP, skip_reason 写"数据不足: 现价缺失", 禁止输出任何具体价位
  (入场/止损/止盈) — 无锚定价位带开盘即失效, 宁缺毋滥
- 若 ≥6 个维度缺失: 优先 SKIP; 仍给方向时按数据不充分处理(score_band 落入 LOW 判据②),
  且必须在 data_quality_note 与 evidence_gaps 说明缺失维度对结论的具体影响
- 数据充足但证据弱 ≠ 数据不足: 证据弱时按正常分析给低分方向或观望, 不得滥用
  "数据不足"当逃生通道 (审计: SKIP 必须指明具体缺失维度)

【论据溯源——硬性约束】
- 你的输出中每个数字必须来自"论据溯源清单"或输入数据的其他字段，不得编造
- 输入数据中没有的数字一律不写(2026-09-07 起删除"(未验证)/估算"例外 — 该例外与
  反虚构硬规直接矛盾): 你需要但缺失的数据记入 evidence_gaps, 不要在论据中携带无出处数字,
  证伪官会逐条核验数字出处, 来源不明数字会被记为幻觉
- 特别注意：ROE/营收增长率/净利润率等基本面数字，必须从"基本面指标(Finnhub)"section引用，不得从新闻标题中摘取当事实
- MSPR/内部人交易金额必须从"内部人情绪"或"内部人交易"section引用具体数值
- 如果"论据溯源清单"中没有某项数据，说明该数据不可用，不要编造

【交易经验参考——如果提供了"近期交易经验"数据，按以下原则使用】
(2026-08-25 重写: 旧版"本股历史全亏→同方向需更强理由"已被实证证伪并删除 —
 标的级历史胜负对本股下一笔无预测力, 半程自相关 pearson=−0.40 且反持续 (DEB-20260825 第2轮);
 亏损后的再入场恰是回放证明的少数盈利行为之一 (DEB-20260825 第10轮熔断回放))
- 这是背景事实，不是方向门禁！不要因本股或某方向的既往亏损而拒绝同方向信号，
 也不要因既往盈利而放松证据标准 — 历史胜负对下一笔无预测力, 多空对称适用。
- 经验数据里只有两类信息可用:
  ①具体失败模式的事实 (如"上次财报日跳空打止损") → 检查本次是否存在同类未定价事件,
    存在则计入 risks; 不存在则不构成反对理由
  ②退出机制效果统计 (如保本出场对死胎仓位的保险价值) → 只用于 execution_plan 的
    止盈/止损松紧参考, 禁止用于方向判断
- 如果本股无历史交易记录，照常分析，缺数据不是 SKIP 理由。
"""


# v1 原版序 (数据在前): 两段的逐字节拼接, 回滚路径 (DA_USER_CACHE_REORDER=0) 用它。
USER_PROMPT_TEMPLATE = _USER_HEAD + _USER_CONTRACT
# v2 缓存友好序 (默认): 契约段 (~6 成 token, 跨候选不变) 前置为可缓存前缀, 数据段最后。
#   与 v1 段落集合相同, 仅顺序不同; 文本差异仅上方 照抄"上方"→"数据中" 中性化一处。
_USER_TAIL_V2 = "\n---\n\n请基于以下真实数据，对该标的进行深度多空辩论分析。\n\n{data}\n"
USER_PROMPT_STABLE_V2 = _USER_CONTRACT + _USER_TAIL_V2
# 契约段渲染文本 ({{ }}→{ } 转义还原; 契约段无 {data} 占位, replace 即等价 format 结果),
# 供 _call_llm user_tail 直接作缓存断点内容, 免每调用重复 replace。
_USER_CONTRACT_RENDERED = _USER_CONTRACT.replace("{{", "{").replace("}}", "}")


def build_user_prompt(data_text: str):
    """当前 flag 下的 DA user prompt: 返回 (user_text, user_tail)。

    v2 (config.DA_USER_CACHE_REORDER=1, 默认): user_text=数据尾段, user_tail=契约渲染
      文本 (调用侧以 cache_control 块前置; 拼接 == USER_PROMPT_STABLE_V2.format(data=...))。
    v1 (=0 回滚): (USER_PROMPT_TEMPLATE 渲染全文, None) — 单块纯文本, 原行为。
    """
    if getattr(config, "DA_USER_CACHE_REORDER", False):
        return _USER_TAIL_V2.format(data=data_text), _USER_CONTRACT_RENDERED
    return USER_PROMPT_TEMPLATE.format(data=data_text), None


def effective_user_template() -> str:
    """指纹/审计口径: 当前 flag 下模型实际所见的 user 模板 (PROMPT_HASH 随 flag 切分)。"""
    if getattr(config, "DA_USER_CACHE_REORDER", False):
        return USER_PROMPT_STABLE_V2
    return USER_PROMPT_TEMPLATE


def _prompt_fingerprint(*parts: str) -> str:
    """[DEB-20260825 第13轮 P6] prompt 版本指纹 (12 位 sha1).

    用途: 每条 DA 决策落盘时带上"当时模型看见的指令是哪一版", 使 prompt 改版前后的
    基线可切分 (数据口径七律#2 新旧基线必须切分)。此前唯一版本标记是 SKILL.md 的
    人工 changelog, 而 prompt 常在未升版本号的情况下被修改 → 混窗统计不可避免。
    (本轮实测教训: 第12轮曾因未按 challenger v1/v2 切分而得出"38%弃权"的假象。)
    """
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "ignore"))
    return h.hexdigest()[:12]


# 分析师侧指令指纹 (SYSTEM + USER 模板). 挑战者侧另算, 见 _run_challenger.
# [2026-09-09 F3] 指纹按 flag 生效后的有效模板计 — 重排前后基线可切分 (口径七律#2)。
# [2026-09-21 temp0] 采样配置断点: 温度纳入指纹 (自动派生 tag — 改温度即自动断基线,
#   不依赖"记得改标签"); 修补原 _prompt_fingerprint 对采样参数不可见的结构缺口
#   (施工单 §基线断点)。旧数据保留旧哈希, 勿回改。
_SAMPLING_TAG = f"t{config.LLM_TEMPERATURE:g}"
PROMPT_HASH = _prompt_fingerprint(SYSTEM_PROMPT, effective_user_template(), _SAMPLING_TAG)


# [DEB-20260825 第14轮 P8] score_band 三档枚举 → 数字分映射.
#   根因: prompt 要模型给 0-10 精确点估值, 但同一 prompt 又告知模型"你的方向命中率≈50%"
#   → 模型收敛到安全中值 7.0 (v2 窗 131/195=67%), score 方差塌缩 → merge 的
#   |orig-chall|>3→SKIP 规则实测 0/107 结构性死亡 (观测上界 1.0)。
#   修复: 模型只数三条可判定锚定条件给档位 (HIGH/MEDIUM/LOW), 档位→数字由代码定,
#   恢复档间差 (HIGH vs LOW = 5 > 3) 使辩论改判规则重新可触发。
#   回滚: DA_SCORE_BAND=0 → 沿用模型原始数字 score。
_SCORE_BAND_MAP = {"HIGH": 8.0, "MEDIUM": 6.0, "LOW": 3.0}


def _apply_score_band(result: Dict[str, Any]) -> None:
    """把模型给的 score_band 映射为数字 score; 原始数字 score 保留在 score_legacy 供影子对照.

    优先级: score_band (新契约) > score (旧数字, 作 legacy)。
    仅在 DA_SCORE_BAND != "0" 且模型真的给了合法 band 时改写。
    """
    try:
        if os.environ.get("DA_SCORE_BAND", "1") == "0":
            return
        band = str(result.get("score_band") or "").strip().upper()
        if band not in _SCORE_BAND_MAP:
            return  # 模型没给合法 band → 不动 (向后兼容, 不惩罚)
        # 保留原始数字 score 供影子对照 (若模型同时给了)
        if "score" in result and result.get("score") is not None:
            try:
                result["score_legacy"] = float(result.get("score"))
            except (TypeError, ValueError):
                pass
        result["score"] = _SCORE_BAND_MAP[band]
        result["_score_band_applied"] = band
    except Exception:
        pass  # 永不阻断主流程


def _reclassify_claim(thesis: str, source_type: str, evidence: str = "") -> str:
    try:
        import ic_writer
        return ic_writer._classify_claim(thesis, source_type, evidence)
    except Exception:
        return "unclassified"


def _audit_claims(result: Dict[str, Any]) -> Dict[str, Any]:
    claim_rows = []
    mismatches = []
    unique_types = {"product_catalyst", "earnings_beat"}
    unique_event = False
    for section in ("bull_case", "bear_case"):
        claims = result.get(section) or []
        if not isinstance(claims, list):
            continue
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            thesis = str(claim.get("thesis") or "")
            source_type = str(claim.get("source_type") or "")
            evidence = str(claim.get("evidence") or "")
            reported = str(claim.get("claim_type") or "").strip()
            inferred = _reclassify_claim(thesis, source_type, evidence)
            claim_rows.append({
                "path": f"{section}[{index}]",
                "reported": reported or None,
                "inferred": inferred,
            })
            if reported and reported != inferred:
                mismatches.append({
                    "path": f"{section}[{index}]",
                    "reported": reported,
                    "inferred": inferred,
                })
            if reported in unique_types or inferred in unique_types:
                unique_event = True
    return {
        "unique_event": unique_event,
        "claims": claim_rows,
        "mismatches": mismatches,
    }


def _audit_priced_in(direction: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {"LONG": "BUY", "SHORT": "SELL_SHORT", "SELL": "SELL_SHORT"}.get(
        str(direction or "").upper(), str(direction or "").upper()
    )
    if normalized not in ("BUY", "SELL_SHORT"):
        return {"priced_in": "unknown", "reason": "direction_missing_or_skip"}
    try:
        effective_price = float(
            snapshot.get("effective_price")
            or snapshot.get("pre_price")
            or snapshot.get("after_price")
            or snapshot.get("last_price")
            or 0
        )
        prev_close = float(
            snapshot.get("prev_close_price")
            or snapshot.get("prev_close")
            or 0
        )
    except (TypeError, ValueError):
        return {"priced_in": "unknown", "reason": "price_unparsable"}
    if effective_price <= 0 or prev_close <= 0:
        return {"priced_in": "unknown", "reason": "price_missing"}
    move_pct = (effective_price - prev_close) / prev_close * 100.0
    same_direction = (
        (normalized == "BUY" and move_pct > 0)
        or (normalized == "SELL_SHORT" and move_pct < 0)
    )
    priced_in = "yes" if same_direction and abs(move_pct) >= 2.0 else "no"
    return {
        "priced_in": priced_in,
        "effective_price": effective_price,
        "prev_close": prev_close,
        "move_pct": round(move_pct, 2),
    }


def _audit_data_sufficiency(collected: Dict[str, Any]) -> Dict[str, Any]:
    try:
        total, ok_count, degraded, missing = _classify_data_sources(collected)
        missing_count = len(degraded) + len(missing)
        return {
            "sufficient": missing_count < 6,
            "total": total,
            "ok": ok_count,
            "missing_count": missing_count,
        }
    except Exception as exc:
        return {
            "sufficient": None,
            "reason": f"classification_error:{type(exc).__name__}",
        }


def _score_band_audit(
    result: Dict[str, Any],
    collected: Dict[str, Any],
    *,
    band_key: str = "score_band",
    direction_key: str = "direction",
) -> Dict[str, Any]:
    reported_band = str(result.get(band_key) or "").strip().upper()
    direction = str(result.get(direction_key) or "SKIP")
    claim_audit = _audit_claims(result)
    priced_in = _audit_priced_in(direction, collected.get("snapshot") or {})
    data_sufficiency = _audit_data_sufficiency(collected)
    criteria = {
        "unique_event": {"met": claim_audit["unique_event"]},
        "not_priced_in": {
            "met": priced_in["priced_in"] == "no",
            "priced_in": priced_in["priced_in"],
        },
        "data_sufficient": {
            "met": data_sufficiency["sufficient"] is True,
            "sufficient": data_sufficiency["sufficient"],
        },
    }
    reasons = []
    for name, detail in criteria.items():
        if detail["met"] is not True:
            reasons.append(name)
    high_supported = not reasons
    should_downgrade = reported_band == "HIGH" and not high_supported
    suggested_band = None
    if should_downgrade:
        suggested_band = "LOW" if data_sufficiency["sufficient"] is False else "MEDIUM"
    return {
        "mode": "shadow_no_enforcement",
        "reported_band": reported_band or None,
        "direction": direction,
        "criteria": criteria,
        "high_supported": high_supported,
        "should_downgrade": should_downgrade,
        "suggested_band": suggested_band,
        "reasons": reasons,
        "priced_in": priced_in,
        "data_sufficiency": data_sufficiency,
        "claim_type_recheck": {
            "claim_count": len(claim_audit["claims"]),
            "mismatch_count": len(claim_audit["mismatches"]),
            "mismatches": claim_audit["mismatches"],
            "claims": claim_audit["claims"],
        },
    }

_CLAIM_TYPE_ENUM = {
    "product_catalyst", "earnings_beat",
    "valuation_cheap", "valuation_expensive",
    "insider_bearish", "insider_bullish",
    "technical_breakout", "technical_breakdown",
    "sentiment_crowded", "macro_tailwind", "macro_headwind",
}


def _as_num(v):
    """能转成有效正数则返回 float, 否则 None (0/空/非数字一律视为缺失)."""
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _validate_output_contract(result: Dict[str, Any], *, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """[DEB-20260825 第13轮 P1] 把 prompt 里写的"必须"改由代码判定 — 只报告, 不修改.

    病灶 (v2 窗 n=107 存活候选实测): prompt 明写 "tripwires 必须≥2条且可量化",
    实际 **0 条占 17% (18/107)** 仍照常下发 → 硬要求此前唯一的执行器是模型的自觉。
    本函数不改任何字段 (零行为风险), 输出违约清单供落盘与 repair 定向补全。

    repairable = 可安全定向补全的项 (纯结构缺失); 方向/评级/评分类矛盾**不列入**,
    因为修它们等于改交易决策 — 那属于门禁范畴, 9 月冻结期明文禁止。
    """
    violations, repairable = [], []
    _d = str(result.get("direction", "SKIP")).upper()
    # 方向词表归一: llm_synth 内部用 BUY/SELL_SHORT, 而 bridge 落库与部分下游用 LONG/SHORT。
    #   不归一的后果不是报错而是**静默失效** — 未知词表会落进下面的 else(SKIP) 分支,
    #   校验器对该候选退化为空操作, 对外表现却是"零违约"的假通过 (本轮回放实测踩到)。
    direction = {"LONG": "BUY", "SHORT": "SELL_SHORT", "SELL": "SELL_SHORT"}.get(_d, _d)
    plan = result.get("execution_plan") or {}

    if direction in ("BUY", "SELL_SHORT"):
        tw_ok = [t for t in (plan.get("tripwires") or [])
                 if isinstance(t, dict) and str(t.get("metric") or "").strip()
                 and str(t.get("threshold") or "").strip()]
        if len(tw_ok) < 2:
            violations.append("tripwires_lt2: 有效%d条(要求>=2)" % len(tw_ok))
            repairable.append("tripwires")

        # [P10 第14轮] invalidation_triggers: 机器可核销的本股失效条件.
        #   根因: tripwires 的 metric 常为外部标的(SOX/金价)、threshold 为自由文本、
        #   极性未编码 → tripwire_checker 实测无法消费, "写tripwires"沦为仪式。
        #   invalidation_triggers 限定本股可判定指标 + </> + 纯数字 value, 才真正可核销。
        #   兼容: 模型只写旧 tripwires 时, 若其条目符合本股可判定格式则计入(回填),
        #   避免旧 prompt 缓存期全部违约。
        _IT_METRICS = ("收盘价", "量比", "5日涨跌", "盘前涨跌幅")
        _IT_OP = ("<", ">")
        def _it_ok(t):
            if not isinstance(t, dict):
                return False
            m = str(t.get("metric") or "").strip()
            op = str(t.get("operator") or "").strip()
            v = t.get("value")
            if m not in _IT_METRICS or op not in _IT_OP:
                return False
            try:
                float(v)
                return True
            except (TypeError, ValueError):
                return False
        it_list = plan.get("invalidation_triggers") or []
        it_ok = [t for t in it_list if _it_ok(t)]
        # 回填: 从旧 tripwires 里挑出符合可判定格式的 (metric匹配且threshold是<数字/>数字)
        if len(it_ok) < 2:
            for t in tw_ok:
                th = str(t.get("threshold") or "").strip()
                m = str(t.get("metric") or "").strip()
                mm = re.match(r"^([<>])\s*(-?[\d.]+)\s*(%)?$", th)
                unit_ok = mm and (not mm.group(3) or m in ("5日涨跌", "盘前涨跌幅"))
                if m in _IT_METRICS and unit_ok:
                    it_ok.append({"metric": m, "operator": mm.group(1),
                                  "value": mm.group(2), "_backfilled": True})
        if len(it_ok) < 2:
            # 只报告不 repair: invalidation_triggers 可由合规 tripwires 回填, 且
            #   tripwire_checker 实际消费的是自然语言 invalidation — 为它单花 repair
            #   token 价值低。先 record-only 观测模型适配率, 不进入定向补全白名单。
            violations.append("invalidation_triggers_lt2: 有效%d条(要求>=2本股可判定)" % len(it_ok))

        px = {k: _as_num(plan.get(k)) for k in
              ("entry_price", "stop_loss_price", "take_profit_price")}
        missing = [k for k, v in px.items() if v is None]
        if missing:
            violations.append("prices_missing: " + ",".join(missing))
            repairable.append("prices")
        else:
            e, s, t = px["entry_price"], px["stop_loss_price"], px["take_profit_price"]
            # 方向自洽 (纯几何判定, 不涉阈值): LONG 需 止损<入场<止盈, SHORT 需 止盈<入场<止损
            if direction == "BUY" and not (s < e < t):
                violations.append("price_order_invalid_long: sl=%s entry=%s tp=%s" % (s, e, t))
            elif direction == "SELL_SHORT" and not (t < e < s):
                violations.append("price_order_invalid_short: tp=%s entry=%s sl=%s" % (t, e, s))

        # ── 飞刀几何一致性机检 (2026-09-14, record-only) ──
        # 病灶: LLM 不知执行层 B门(实时价vs昨收<-5%拒LONG)与 chase_cap 终态闸的存在,
        #   盘前暴跌日产出 chase_cap < 昨收×0.95 的 LONG → 价格回升出飞刀区(唯一可成交
        #   时刻)必越禁追线 → 结构性空集 (实证 9/14: HPE/NOK 两条死指令)。
        # 判定: 只报告不 repair — 改 chase_cap = 改交易决策, 属门禁范畴非结构补全
        #   (与本函数"零方向变更"哲学一致); prompt 三处已同步教模型自洽 (SYSTEM/契约/挑战者e)。
        # 输入: snapshot 由调用点注入 (None = 老调用点/测试, 判定跳过, 零行为变更)。
        if direction == "BUY" and snapshot:
            _pc = _as_num(snapshot.get("prev_close_price") or snapshot.get("prev_close"))
            # ⚠️ 涨跌幅不能用 _as_num (它只接受正数, -7.7 会被当缺失) — 直接 float 解析
            try:
                _pchg = float(snapshot.get("pre_change_rate"))
            except (TypeError, ValueError):
                _pchg = None
            _cap = _as_num(plan.get("chase_cap_price"))
            if _pc and _pchg is not None and _pchg < -5.0 and _cap:
                _knife_line = _pc * 0.95
                if _cap < _knife_line:
                    violations.append(
                        "knife_geometry_conflict: chase_cap=%.2f < 飞刀线=%.2f (昨收%.2f×0.95), "
                        "盘前%.1f%% LONG 结构性不可成交" % (_cap, _knife_line, _pc, _pchg))

        # ── [RFC-120 2026-09-30] chase_cap 方向语义倒挂机检 (record-only) ──
        # 病灶: 执行层禁追门语义 = BUY cap 为上限 / SHORT cap 为下限 (entry_routing.
        #   chase_cap_violated)。LLM 常把 SHORT 的 cap 填成 invalidation 的上破失效位
        #   (上界语义) → 下单价天然低于它 → 出生即违例死锁 (9/4 enforce 上线后
        #   born-violated 空头 18/18 零执行 vs 合规组 24/33; 9/30 实证 BE cap=302.0/
        #   entry=298.0「放量突破$302.35前高」= 把失效位当 cap; KMX 63.0/60.5;
        #   TSLA 358.0/355.0, TSLA 单指令空转 37+ 轮)。BUY 侧同病 (cap<=entry) 18 条零执行。
        # 判定: 只报告不 repair — 与 knife_geometry 同哲学: 改 cap = 改交易决策;
        #   执行层已有 normalize_chase_cap 归一兜底 (entry_band.py, 同源 SSOT),
        #   本违约行 = prompt 合规率的攒样尺子 (修复后应趋零)。
        _cap_ic = _as_num(plan.get("chase_cap_price"))
        _e_ic = _as_num(plan.get("entry_price"))
        if _cap_ic and _e_ic:
            if direction == "SELL_SHORT" and _cap_ic >= _e_ic:
                violations.append(
                    "chase_cap_inverted_short: cap=%.2f >= entry=%.2f — SHORT 的 cap 是"
                    "禁追空**下限**(必须<入场价), 不是 invalidation 的上破失效位; "
                    "此指令出生即违例不可成交" % (_cap_ic, _e_ic))
            elif direction == "BUY" and _cap_ic <= _e_ic:
                violations.append(
                    "chase_cap_inverted_long: cap=%.2f <= entry=%.2f — BUY 的 cap 是"
                    "禁追涨**上限**(必须>入场价); 此指令出生即违例不可成交" % (_cap_ic, _e_ic))

        # ── [2026-10-17] 第2批几何一致性机检 (record-only) ──
        # 病灶: LLM 写了两腿计划 (第1批低吸 + 第2批突破追入), 却把 cap 放在第2批触发价**内侧**。
        #   价格必须先越过 P2 才触发第2批, 而越过 P2 即越过 cap ⇒ 该腿**结构上永不执行**
        #   (实测 10/16 US.CMC cap=66.50 vs 第2批 MA60 67.53 → 该指令空转 25 轮;
        #    10/15 US.REAL cap=9.5 vs 第2批 9.60 → 整日 0 成交)。性质是"计划自相矛盾", 不是"追高被拦"。
        # 判定: 只报告不 repair — 与 knife_geometry / RFC-120 同哲学 (改 cap = 改交易决策);
        #   本违约行 = prompt 合规率的攒样尺子 (修复后应趋零), 见 docs/decisions/RFC-144。
        # ⚠️ 抽取局限 (2026-10-17 实测, 写在此处防后人把命中率当精确比率用):
        #   ① 只在"第2批/第二批"之后的子串里找价位 — 避免把止盈价/第1批价当触发价;
        #   ② 同时抓"突破MA60(67.53)"(均线括号形态) 与 "突破 9.60"(裸数字);
        #   ③ 个位数价格必须支持 (早期正则要求整数≥2位 ⇒ 漏掉 9.60 这类);
        #   ④ 抓不到数字就不判 (宁漏不误), 不猜均线值 — 命中数是**下界**不是精确率。
        _es_b2 = plan.get("entry_strategy")
        if _cap_ic and isinstance(_es_b2, str):
            _m_b2 = re.search(r"(第\s*2\s*批|第二批)", _es_b2)
            if _m_b2:
                _tail_b2 = _es_b2[_m_b2.start():]
                # 方向感知的触发动词: BUY 的第2批是"向上突破", SHORT 的是"向下跌破" —
                # 用同一套词会把"回踩接回"之类的低吸腿也当触发价 (10/17 实测 SHORT 漏报一次)
                _verb_b2 = (r"突破|站上|上破|越过" if direction == "BUY"
                            else r"跌破|下破|失守")
                _lvl_b2 = []
                for _mm in re.finditer(
                    r"(?:" + _verb_b2 + r")\s*(?:MA\d+\s*[（(]\s*)?\$?([0-9]{1,4}(?:\.[0-9]{1,2})?)",
                    _tail_b2,
                ):
                    try:
                        _lvl_b2.append(float(_mm.group(1)))
                    except ValueError:
                        pass
                if _lvl_b2:
                    if direction == "BUY" and max(_lvl_b2) > _cap_ic:
                        violations.append(
                            "chase_cap_vs_batch2_conflict: 第2批触发价=%.2f > cap=%.2f — "
                            "第2批落在自设禁追线之外, 该腿结构上永不执行"
                            "(二选一: 抬 cap 或删第2批触发价)" % (max(_lvl_b2), _cap_ic))
                    elif direction == "SELL_SHORT" and min(_lvl_b2) < _cap_ic:
                        violations.append(
                            "chase_cap_vs_batch2_conflict: 第2批触发价=%.2f < cap=%.2f — "
                            "第2批落在自设禁追线之外, 该腿结构上永不执行(SHORT)"
                            % (min(_lvl_b2), _cap_ic))

        # [DEB-20260907] 论据计数规则与新契约对齐: 方向侧>=2, 反方允许0条(诚实留空),
        #   合计 2-6 条 (旧规则"两侧各>=2"是凑数制造机, 已随 prompt 同步删除)。
        _dir_sec = "bull_case" if direction == "BUY" else "bear_case"
        _opp_sec = "bear_case" if direction == "BUY" else "bull_case"
        _n_dir = len(result.get(_dir_sec) or [])
        _n_opp = len(result.get(_opp_sec) or [])
        if _n_dir < 2:
            violations.append("%s_lt2: 方向侧%d条(要求>=2)" % (_dir_sec, _n_dir))
        if _n_dir + _n_opp > 6:
            violations.append("cases_gt6: 合计%d条(上限6)" % (_n_dir + _n_opp))
        for sec in ("bull_case", "bear_case"):
            for c in result.get(sec) or []:
                ct = str((c or {}).get("claim_type") or "").strip()
                if not ct:
                    violations.append("claim_type_missing: " + sec)
                elif ct not in _CLAIM_TYPE_ENUM:
                    violations.append("claim_type_invalid: %s/%s" % (sec, ct))
                # [DEB-20260907] generic 论据机检 (record-only): thesis+evidence 无任何数字
                #   = 不可溯源的通用修辞, prompt 已声明将被机检记为 generic 违约。
                _blob = str((c or {}).get("thesis") or "") + str((c or {}).get("evidence") or "")
                if not re.search(r"\d", _blob):
                    violations.append("generic_claim: %s 论据无数字锚点" % sec)
    else:
        # SKIP 侧唯一契约: 必须说明缺口 (prompt 多处要求写 skip_reason, 但 JSON schema
        # 模板里并未列出该字段 = 指令与契约不一致, 此处按指令判定并可定向补全)
        if not str(result.get("skip_reason") or "").strip():
            violations.append("skip_reason_missing")
            repairable.append("skip_reason")

    return {"violations": violations, "repairable": sorted(set(repairable))}


def _repair_output_contract(result: Dict[str, Any], system: str, data_text: str,
                            repairable: List[str]) -> Optional[Dict[str, Any]]:
    """[DEB-20260825 第13轮 P1] 单次定向补全: 只回喂缺失字段, 不重跑全分析.

    **零方向变更保证是结构性的, 不靠模型配合**: 本函数只把补丁写进
    execution_plan.tripwires / 三个价位 / skip_reason 这几个白名单键,
    direction / rating / score / bull_case / bear_case 一律不接受补丁 —
    即使模型在响应里夹带方向改写也不会生效。
    回滚: DA_SCHEMA_ENFORCE=0 (连校验一起关)。
    """
    ask = []
    if "tripwires" in repairable:
        ask.append('"tripwires": [{"metric":"可观测指标","threshold":"量化阈值",'
                   '"deadline":"观测截止日或事件日期","source":"输入数据中的哪条事实"}] '
                   '— 至少2条, 每条必须能被第三方判定真假, 禁止"情绪恶化/走弱"这类修辞')
    if "prices" in repairable:
        ask.append('"entry_price"/"stop_loss_price"/"take_profit_price": 三个具体数字, '
                   '锚定有效现价; 止损距入场 2-5%; LONG 需 止损<入场<止盈, SHORT 需 止盈<入场<止损')
    if "skip_reason" in repairable:
        ask.append('"skip_reason": 一句话写明观望的具体缺口 (缺哪个维度数据 / 哪条证据不足), '
                   '禁止"综合考虑"这类空话')
    if not ask:
        return None

    user = (
        "你此前对 " + str(result.get("symbol") or "该标的") + " 的分析缺少下列必填字段。\n"
        "现在**只补这些字段**: 不要重新分析, 不要改变方向/评级/评分, 不要输出其他字段。\n\n"
        "你的原结论: direction=" + str(result.get("direction")) +
        ", rating=" + str(result.get("rating")) + ", score=" + str(result.get("score")) + "\n"
        "原 execution_plan: " + json.dumps(result.get("execution_plan") or {},
                                           ensure_ascii=False)[:800] + "\n\n"
        "原始数据 (取值必须溯源到这里, 不得编造):\n" + (data_text or "")[:2500] + "\n\n"
        "只返回如下 JSON:\n{\n  " + ",\n  ".join(ask) + "\n}"
    )

    patch = _call_llm(system, user, temperature=config.LLM_TEMPERATURE, max_tokens=800)
    if not patch or "_error" in patch:
        return {"ok": False, "error": (patch or {}).get("_error", "no_response")}

    applied = []
    plan = result.setdefault("execution_plan", {})
    if "tripwires" in repairable:
        tw = patch.get("tripwires")
        if isinstance(tw, list):
            tw_ok = [t for t in tw if isinstance(t, dict)
                     and str(t.get("metric") or "").strip()
                     and str(t.get("threshold") or "").strip()]
            if len(tw_ok) >= 2:
                plan["tripwires"] = tw_ok
                applied.append("tripwires")
    if "prices" in repairable:
        got = {k: _as_num(patch.get(k)) for k in
               ("entry_price", "stop_loss_price", "take_profit_price")}
        if all(v is not None for v in got.values()):
            plan.update(got)
            applied.append("prices")
    if "skip_reason" in repairable:
        sr = str(patch.get("skip_reason") or "").strip()
        if sr:
            result["skip_reason"] = sr
            applied.append("skip_reason")

    return {"ok": True, "applied": applied, "requested": list(repairable),
            "usage": patch.get("_usage"), "finish_reason": patch.get("_finish_reason")}


def _load_claim_ic_for_prompt() -> str:
    """读取claim IC缓存，生成LLM可读的论据有效性反馈文本。

    告诉LLM：哪些论据类型历史验证有预测力（多用），哪些无效（少用或不用）。
    如果缓存不存在或样本不足，返回空字符串（不影响现有流程）。
    """
    try:
        from alpha_backfill import load_claim_ic
        cache = load_claim_ic()
        if not cache or cache.get("total_samples", 0) < 10:
            return ""  # 样本不足，不注入

        lines = [f"基于{cache['total_samples']}条{cache.get('strategy_id','')}历史信号的alpha超额验证（剥离大盘beta后）："]

        effective = cache.get("effective_claims", [])
        ineffective = cache.get("ineffective_claims", [])

        if effective:
            lines.append(f"✅ 有效论据(历史平均超额>0，可重点使用): {', '.join(effective)}")
        if ineffective:
            lines.append(f"⚠️ 无效论据(历史平均超额<0，跑输大盘，避免作为核心依据): {', '.join(ineffective)}")

        # 具体超额收益明细（前3最优+前3最差, 按 avg_alpha_t1 排序, n≥10）
        claims = cache.get("claims", {})
        rated = [(ct, d) for ct, d in claims.items()
                 if d.get("avg_alpha_t1") is not None and d.get("count", 0) >= 10]
        if rated:
            rated.sort(key=lambda x: x[1]["avg_alpha_t1"], reverse=True)
            lines.append("超额收益明细(T+1 alpha, 方向已调整):")
            for ct, d in rated[:3]:
                lines.append(f"  {ct}: α={d['avg_alpha_t1']*100:+.2f}% (n={d.get('count',0)})")
            if len(rated) > 3:
                lines.append("  ...")
                for ct, d in rated[-3:]:
                    lines.append(f"  {ct}: α={d['avg_alpha_t1']*100:+.2f}% (n={d.get('count',0)})")

        lines.append("注意：无效论据不代表不能用，但不应作为核心看多/看空依据，需有其他有效论据支撑。")
        return "\n".join(lines)
    except Exception:
        return ""  # 静默失败，不影响主流程


def _load_negative_claims_warning_for_prompt() -> str:
    """负向论据警示前置 (2026-09-01 信息供应链审计建议②, 用户裁"按建议处理").

    与 2026-07-18 用户裁关的 _load_claim_ic_for_prompt 的区别 (不复活旧注入):
      ① 只注负向清单 (正向清单诱导过度自信, 且旧注入含原始 IC 明细跨 regime 误导);
      ② 只保留语义论据 (过滤 debate_outcome/quality_score 等内部状态, 防模型困惑);
      ③ 口径=全时段 T+1 alpha (方向调整超额, 与 signal_gate 事后警告同源),
         显式声明"历史参考非否决"; ④ 配置开关可回滚。
    回滚: unified_config.yaml playbook.claim_negative_prompt_enabled: false
    """
    try:
        import yaml as _yaml
        with open("/opt/ftseek/FTSeek-US/config/unified_config.yaml", encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        if not (_cfg.get("playbook") or {}).get("claim_negative_prompt_enabled", True):
            return ""
        from alpha_backfill import load_claim_ic
        cache = load_claim_ic()
        if not cache:
            return ""
        claims = cache.get("claims", {})
        _INTERNAL = ("debate_outcome:", "quality_score:", "gate_decision:",
                     "position_size_hint:", "bucket_reason:")
        neg = [(ct, d) for ct, d in claims.items()
               if not ct.startswith(_INTERNAL)
               and d.get("avg_alpha_t1") is not None
               and d.get("count", 0) >= 10 and d["avg_alpha_t1"] < -0.002]
        if not neg:
            return ""
        neg.sort(key=lambda x: x[1]["avg_alpha_t1"])
        lines = [
            "以下为历史上(全时段口径)验证为负超额收益的论据类型 — 仅作背景参考, 不是否决:",
            "若将其作为核心依据, 需当日数据给出更强证据, 并在 risks 中显式标注该论据历史表现为负。",
        ]
        for ct, d in neg[:10]:
            lines.append(f"  - {ct}: T+1 alpha {d['avg_alpha_t1']*100:+.2f}% (n={d['count']})")
        lines.append("(口径: 剥离大盘 beta 的方向调整超额, 与执行门事后警告同源; 历史统计不保证未来)")
        return "\n".join(lines)
    except Exception:
        return ""  # 静默失败不影响主流程 (诊断型注入, 非关键路径)


def _load_condition_blacklist_for_prompt() -> str:
    """F1 (DEB-20260804, 2026-08-04 用户裁"先实施执行方案"):
    读取 condition 级负期望黑名单, 注入 DA prompt 作软约束。

    数据源: /opt/ftseek/FTSeek-US/scripts/condition_expectancy_report.py
            → data/condition_expectancy_latest.json (crontab 05:42 CST 每日更新)
    门槛: 滚动30日窗 n≥8 + 双窗口同号 + 跨regime同号 + bootstrap 显著性。
    回滚: unified_config.yaml playbook.condition_expectancy_prompt_enabled: false
    """
    try:
        import yaml as _yaml
        with open("/opt/ftseek/FTSeek-US/config/unified_config.yaml", encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        if not (_cfg.get("playbook") or {}).get("condition_expectancy_prompt_enabled", True):
            return ""
        import json as _json
        from datetime import datetime as _dt
        _p = "/opt/ftseek/FTSeek-US/data/condition_expectancy_latest.json"
        with open(_p, encoding="utf-8") as _f:
            _d = _json.load(_f)
        # 新鲜度守卫: 超过 3 天的旧表不注入 (防止 cron 停摆后旧黑名单误导)
        try:
            _gen = _dt.fromisoformat(_d.get("generated_at", ""))
            if (_dt.now() - _gen.replace(tzinfo=None)).days > 3:
                return ""
        except Exception:
            return ""
        return _d.get("prompt_block") or ""
    except Exception:
        return ""  # 静默失败，不影响主流程


def _load_symbol_card_for_prompt(symbol: str, current_regime: str = ""):
    """股性卡注入 (DEB-20260819 §十三/§十四, 2026-08-19).

    读取 data/symbol_rule_cards/{sym}.json, 格式化为 prompt 段注入 DA 双模型。
    Qoder §14.3 裁决落地:
      Q2: 只注当前标的 + top-3 条 (按|t|排序, 卡内已截断)
      Q3: regime 失配 → 分钟族(marginal)不注入; 日K族注入但附警示
      Q4: 附"与K线技术面同源勿双重计权"声明
    返回: (text, injected_rules) — injected_rules = 实际写入 prompt 的规则名列表
      (§十七-L2修复: 曾被 regime 失配 continue 跳过的规则不进标签, 与注入内容一致)
    回滚: unified_config.yaml playbook.symbol_card_prompt_enabled: false
    """
    try:
        import yaml as _yaml
        with open("/opt/ftseek/FTSeek-US/config/unified_config.yaml", encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        if not (_cfg.get("playbook") or {}).get("symbol_card_prompt_enabled", True):
            return "", []
        import json as _json
        from datetime import datetime as _dt, date as _date
        _sym_key = symbol.replace(".", "_")
        _p = f"/opt/ftseek/FTSeek-US/data/symbol_rule_cards/{_sym_key}.json"
        import os as _os
        if not _os.path.exists(_p):
            return "", []
        with open(_p, encoding="utf-8") as _f:
            _card = _json.load(_f)
        # 新鲜度守卫: 卡 generated >7 天不注入
        try:
            _gen = _dt.strptime(_card.get("generated", ""), "%Y-%m-%d")
            if (_date.today() - _gen.date()).days > 7:
                return "", []
        except Exception:
            return "", []
        rules = _card.get("rules", [])
        if not rules:
            return "", []
        # DEB-20260819 §十六: 触发状态确定性预计算 — LLM 只消费判定结果, 不自行估算边界
        #   (8/19 演练实锤: LLM 目测 SNDK "贴近日低"误判触发 R9b, 实算 20.5%>20% 未触发)
        _trig = {}
        try:
            import sys as _tsys
            if "/opt/ftseek/FTSeek-US/scripts" not in _tsys.path:
                _tsys.path.insert(0, "/opt/ftseek/FTSeek-US/scripts")
            import symbol_card_trigger_check as _tc
            # 陈旧守卫: 日K缓存末日距今>4自然日 → 不附触发判定 (宁缺勿错, 卡体仍注入)
            _bars = _tc._load_daily(symbol)
            _stale = True
            if _bars:
                from datetime import datetime as _dt2
                _bar_date = _dt2.strptime(_bars[-1][0], "%Y-%m-%d")
                if (_date.today() - _bar_date.date()).days <= 4:
                    _stale = False
            if not _stale:
                _trig = _tc.evaluate(symbol, [r["rule"] for r in rules])
        except Exception:
            _trig = {}
        # Q3: regime 失配处理
        _cur_reg = (current_regime or _card.get("current_regime", "")).upper()
        lines = [f"以下为 {symbol} 历史统计验证的股性档案 (L2软提示, 非交易指令):"]
        _injected = []  # §十七-L2: 实际写入 prompt 的规则名 (跳过/截断的不计)
        for r in rules[:3]:
            tier = r.get("evidence_tier", "standard")
            reg = r.get("sample_regime", "")
            # 分钟族(marginal) 在 regime 失配时跳过
            if tier == "marginal" and _cur_reg and "RISK_ON" not in reg.upper() and "RISK_ON" not in _cur_reg:
                continue
            if tier == "marginal" and _cur_reg and _cur_reg not in ("RISK_ON", "UNKNOWN"):
                continue  # 当前非 risk_on 且卡来自 risk_on 段 → 停注分钟族
            direction_hint = "次日偏强" if r["next_day_mean_pct"] > 0 else "次日偏弱"
            base_note = ""
            pb = r.get("pool_baseline_pct")
            if pb is not None and abs(r["next_day_mean_pct"] - pb) < 0.15:
                base_note = f" (≈全池共性{pb:+.2f}%)"
            warn = " ⚠边际证据(7.5月)" if tier == "marginal" else ""
            trig_note = ""
            _t = _trig.get(r["rule"])
            if _t:
                # §十七-L5: 日K族触发判定用 QFQ 缓存, K线段是未复权 — 标注口径防 LLM 困惑
                _cal = "（前复权口径）" if r["rule"].startswith(("R5", "R9", "R10")) else ""
                trig_note = f" | 触发判定: {_t}{_cal}"
            lines.append(
                f"- {r['rule']}: 触发后{direction_hint} {r['next_day_mean_pct']:+.2f}% "
                f"(n={r['n']}, t={r['t']}{warn}{base_note}){trig_note}"
            )
            _injected.append(r["rule"])
        if len(lines) <= 1:
            return "", []
        lines.append("注: 此为该股自身历史统计摘要, 与K线技术面同源, 勿作独立证据双重计权; "
                     "可凭独立强逻辑覆盖但须在 thesis 中说明。")
        return "\n".join(lines), _injected
    except Exception:
        return "", []


def _load_trading_card_for_prompt(symbol: str):
    """成交口径股性档案注入 (DEB-20260831 §四, 2026-09-01).

    读 data/symbol_trading_cards/{sym}.json — 本系统实际成交结果+盘前行为+动量留存
    (区别于 symbol_rule_cards 的"规则×标的"统计). 8/31 溯源实锤: 旧卡用 DA 分析次数
    口径 (MU "wr=0.5/trades=2"), 与本系统成交真值 (MU n=7 WR=14% 净-$500) 相反, 无预测力.
    纪律: insufficient (n<30) 档只显示样本数不读倾向词 — 由本函数强制, 不依赖模型自律.
    回滚: unified_config.yaml playbook.trading_card_prompt_enabled=false
    """
    try:
        import yaml as _yaml
        with open("/opt/ftseek/FTSeek-US/config/unified_config.yaml", encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        if not (_cfg.get("playbook") or {}).get("trading_card_prompt_enabled", True):
            return ""
        import json as _json
        from datetime import datetime as _dt, date as _date
        import os as _os
        _p = f"/opt/ftseek/FTSeek-US/data/symbol_trading_cards/{symbol.replace('.', '_')}.json"
        if not _os.path.exists(_p):
            return ""
        with open(_p, encoding="utf-8") as _f:
            _card = _json.load(_f)
        # 新鲜度守卫: >7 天不注入
        try:
            _gen = _dt.strptime(_card.get("generated", ""), "%Y-%m-%d")
            if (_date.today() - _gen.date()).days > 7:
                return ""
        except Exception:
            return ""
        lines = [f"以下为 {symbol} 本系统实际成交与行为档案 (背景事实, 非方向先验):"]
        tr = _card.get("trading_record")
        if tr:
            if tr.get("tier") == "rated":
                lines.append(
                    f"- 本系统成交: n={tr['n']} 胜率={tr['wr']:.0%} 净={tr['net']:+.0f} "
                    f"单均={tr['avg']:+.1f} 入场即逆占比={tr['mfe0_pct']:.0%} "
                    f"SL_INITIAL占比={tr['sl_initial_pct']:.0%}")
            else:
                lines.append(f"- 本系统成交样本不足 ({tr.get('tier')}, 不读倾向): "
                             f"n={tr.get('n')} 净={tr.get('net')}")
        pb = _card.get("premarket_behavior")
        if pb and pb.get("tier") == "rated":
            lines.append(f"- 盘前行为: 触发率={pb['trigger_rate']:.0%} "
                         f"影子单均={pb['avg_shadow_pnl_pct']:+.2f}% (n={pb['n']})")
        mr = _card.get("momentum_retention")
        if mr and mr.get("tier") == "rated":
            lines.append(f"- 开盘15min动量留存率={mr['retention']:.0%} ({mr['n_days']}日)")
        if len(lines) <= 1:
            return ""
        lines.append("注: 此为该股在本系统的历史成交摘要, 仅作背景事实; "
                     "样本不足项禁止外推为该股倾向。")
        return "\n".join(lines)
    except Exception:
        return ""


def _apply_prev_close_fix(collected: Dict[str, Any]) -> Dict[str, Any]:
    """在 Prompt 组装前修正 prev_close (默认 apply; 回滚=DA_PREV_CLOSE_BAR_RECONCILE=off).

    2026-10-23: 10/22 实测 28 份中 8 份 prev_close 是两天前收盘; 现有 1% 推导
    阈值漏过 0.24%-0.93% 的小偏差. 修正只在独立模块守卫全过时发生; 原始值保留
    在 snapshot.prev_close_raw, 供审计/回滚. 这不改任何交易门, 只让模型看到
    与上一已完成日K一致的价格基准.
    """
    if reconcile_prev_close is None or not isinstance(collected, dict):
        return collected
    mode = str(os.environ.get("DA_PREV_CLOSE_BAR_RECONCILE", "apply") or "apply").lower()
    if mode == "off":
        return collected
    snap, kline = collected.get("snapshot"), collected.get("kline")
    if not isinstance(snap, dict) or not isinstance(kline, list):
        return collected
    try:
        collected["snapshot"] = reconcile_prev_close(snap, kline, mode=mode)
    except Exception as exc:  # 纯数据修正失败不得阻断分析
        print(f"[prev_close] reconcile skipped: {exc}", file=sys.stderr)
    return collected


def synthesize(collected: Dict[str, Any], trade_experience: str = "",
               system_override: Optional[str] = None) -> Dict[str, Any]:
    """
    主入口：采集数据 → 2模型辩论合成 → 结构化分析结果

    v0.39: 2模型辩论替代单模型。
    Pass1: 分析师(qwen3.7-max) → 初步分析(含direction/score/bull_case/bear_case)
    Pass2: 挑战者(glm) → 读初步分析,挑战薄弱论据,确认或修正方向
    融合: 方向一致→confidence+1档; 不一致→confidence-1档或SKIP

    trade_experience: 近期交易经验文本（背景事实注入, 非方向先验 — 见 USER_PROMPT_TEMPLATE
      【交易经验参考】段的使用纪律）。
    system_override: [2026-09-01 DEB-20260901c] 蒸馏影子专用 — 替换分析师
      SYSTEM_PROMPT 文本, 其余组装逐字节同源 (单一变量=system)。None=生产
      SYSTEM_PROMPT (默认, 行为不变)。生产调用方永不传此参数。

    【校准原则】已于 2026-08-25 从本 docstring 移入 SYSTEM_PROMPT 正式生效 —
    此前该段只存在于 docstring 中, 从未进入任何模型输入 (死链路修复, DEB-20260825 第11轮)。

"""
    collected = _apply_prev_close_fix(collected)
    data_text = _format_data_for_prompt(collected)

    # 构建prompt——如果有交易经验，注入到数据末尾
    # 2026-08-25: 标签从"方向先验"改为"背景事实, 非方向先验" — 标的级历史胜负已证
    #   无预测力 (DEB-20260825 第2轮 pearson=−0.40), 旧标签诱导模型把噪声当先验。
    if trade_experience:
        data_text = data_text + "\n\n## 近期交易经验（背景事实, 非方向先验）\n" + trade_experience

    # 2026-07-18 (用户): 关闭 claim IC 注入 — claim_ic_cache 是全时段聚合(跨新旧策略regime)
    #   的历史指标, 与"以最新统一管线 live 数据驱动"冲突。LLM 只依据当日实时数据+双模型辩论。
    #   (保留 _load_claim_ic_for_prompt 函数体, 仅停用注入, 便于回滚。)
    # claim_ic_text = _load_claim_ic_for_prompt()
    # if claim_ic_text:
    #     data_text = data_text + "\n\n## 论据有效性反馈（基于历史IC验证）\n" + claim_ic_text

    # 2026-09-01 (信息供应链审计建议②): 负向论据警示前置 — 窄口径新注入, 非复活 7/18 裁关的
    #   旧 IC 注入 (只负向/只语义论据/alpha口径/显式声明参考性)。门侧事后警告
    #   (signal_gate §6.5) 保留, 形成"事前警示+事后警告"闭环。
    _neg_claims_text = _load_negative_claims_warning_for_prompt()
    if _neg_claims_text:
        data_text = data_text + "\n\n## 历史负向论据警示\n" + _neg_claims_text

    # 2026-08-04 F1 (DEB-20260804, 用户裁"先实施执行方案"): condition 级负期望黑名单注入
    #   软约束: 仅提示 DA 规避近期统计亏损的条件, 不硬挡; 回滚=关 condition_expectancy_prompt_enabled
    cond_bl_text = _load_condition_blacklist_for_prompt()
    if cond_bl_text:
        data_text = data_text + "\n\n## 条件级历史期望警示\n" + cond_bl_text

    # 2026-08-19 (DEB-20260819 §十三/§十四): 股性卡注入 — 该股历史统计验证的性格档案
    #   Q1: 分析师+挑战者对称 (data_text 共享); Q2: top-3; Q3: regime失配停注分钟族;
    #   Q4: 附防双重计权声明。回滚: symbol_card_prompt_enabled=false
    _card_sym = collected.get("symbol", "")
    _card_reg = collected.get("market_regime", "") or ""
    _card_text, _card_injected = _load_symbol_card_for_prompt(_card_sym, _card_reg)
    if _card_text:
        data_text = data_text + "\n\n## 股性档案（历史统计验证）\n" + _card_text

    # 2026-09-01 (DEB-20260831 §四): 成交口径股性档案 — 本系统实际交易该股的结果
    #   (旧规则卡是"规则×标的"统计; 本卡是"本系统×标的"成交真值, 8/31 溯源证旧口径无预测力)
    #   回滚: playbook.trading_card_prompt_enabled=false
    _tcard_text = _load_trading_card_for_prompt(_card_sym)
    if _tcard_text:
        data_text = data_text + "\n\n## 本系统成交档案（背景事实）\n" + _tcard_text

    # [2026-09-09 F3] v2: (数据尾段, 契约缓存块); v1 回滚: (完整模板, None)。
    #   落盘 _user_prompt 须为模型所见全文 → 下方按是否有 tail 拼接还原。
    user_prompt, _user_tail = build_user_prompt(data_text)

    # ── Pass 1: 分析师初步分析 ──
    # [2026-09-01 PE 审阅] cache_system: 静态 SYSTEM_PROMPT 前缀缓存 (同轮候选共享),
    #   开关 config.DA_PROMPT_CACHE; 挑战者侧不启用 (其模型数组格式未验证)。
    # [2026-09-01 DEB-20260901b 用户裁"直接切生产·修尺子"] 思考模式: qwen3.7-max
    #   系推理模型, 开 enable_thinking + thinking_budget。推理 token 计入输出预算
    #   → max_tokens 配套 3000→6000 防 empty_response (网关 7/6 P0 实证教训)。
    #   回滚: DA_THINKING_ENABLED=0 → 关思考+3000 上限, 逐字节原行为。
    _thinking_on = bool(getattr(config, "DA_THINKING_ENABLED", False))
    _extra_body = None
    _budget = 0
    if _thinking_on:
        _extra_body = {"enable_thinking": True}
        _budget = int(getattr(config, "DA_THINKING_BUDGET", 0) or 0)
        if _budget > 0:
            _extra_body["thinking_budget"] = _budget
    # [2026-09-14 用户裁"确保正常的思考不被中断"] max_tokens 与思考预算**联动**:
    #   推理 token 计入输出预算, 两者脱钩正是本次病灶 (budget 2048 撞顶 48/48,
    #   正文被挤到 1222-2310)。硬编码 6000 会随预算上升而重新变成枷锁, 故改为
    #   由预算推导: budget + 8000。+8000 是正文余量 — 跨 8 标的实测正文 1266-1921
    #   tok (p50 1547), 约 4x 余量; 实测总输出 max 7445 (旧硬编码 6000 会截断正文)。
    #   实测 (budget=8192 回放, n=8): reasoning max 5898 / 延迟 max 80.9s / 0 撞顶。
    #   回滚: 与 config.DA_THINKING_BUDGET 一同回滚 (见 config.py 该行注释)。
    _maxtok = 3000
    if _thinking_on:
        _maxtok = (_budget + 8000) if _budget > 0 else 16000
    import time as _time_p1
    _t0_p1 = _time_p1.time()
    result = _call_llm(system_override or SYSTEM_PROMPT, user_prompt, temperature=config.LLM_TEMPERATURE,
                       max_tokens=_maxtok,
                       cache_system=config.DA_PROMPT_CACHE, extra_body=_extra_body,
                       timeout=240 if _thinking_on else 60, user_tail=_user_tail)
    _p1_ms = int((_time_p1.time() - _t0_p1) * 1000)
    if isinstance(result, dict):
        # 观测落盘 (修尺子): 延迟/思考态随 _llm_output 进 checkpoint → 日报可核
        result.setdefault("_llm_latency_ms", _p1_ms)
        result.setdefault("_thinking_enabled", _thinking_on)

    if not result or "_error" in result:
        return {
            "_error": "LLM synthesis failed",
            "_raw_data": data_text[:500],
            **_fallback_synthesize(collected),
        }

    # ── [P8 第14轮] 分析师 score_band → 数字 score (在挑战者/合并之前,
    #     使 merge 的 |orig-chall|>3→SKIP 判定用映射后分, 档间差 HIGH vs LOW=5 可真正触发) ──
    _apply_score_band(result)
    result["_score_band_audit"] = _score_band_audit(result, collected)

    # ── Pass 2: 挑战者辩论 (v0.39) ──
    challenge_result = _run_challenger(result, data_text)
    if challenge_result:
        result = _merge_debate_results(result, challenge_result)

    # ── 输出契约执行化 (DEB-20260825 第13轮 P1) ──
    #   先判定 prompt 里那些"必须"是否真的做到, 再对**纯结构缺失**打一次定向补丁。
    #   放在辩论合并之后: 挑战者可能改写 direction (score差>3→SKIP), 契约随最终方向判定。
    #   零方向变更 (补丁白名单不含 direction/rating/score, 见 _repair_output_contract)。
    #   回滚: DA_SCHEMA_ENFORCE=0。
    if os.environ.get("DA_SCHEMA_ENFORCE", "1") == "1":
        try:
            _contract = _validate_output_contract(
                result, snapshot=(collected.get("snapshot") or None))
            if _contract["repairable"]:
                _dir_before = str(result.get("direction", "")).upper()
                _contract["repair"] = _repair_output_contract(
                    result, SYSTEM_PROMPT, data_text, _contract["repairable"])
                _contract["violations_after"] = _validate_output_contract(
                    result, snapshot=(collected.get("snapshot") or None))["violations"]
                # 不变量自检: 定向补全绝不允许改方向 (结构上不可能, 此处仍留断言式记录)
                if str(result.get("direction", "")).upper() != _dir_before:
                    _contract["ERROR_direction_changed"] = True
            if _contract["violations"]:
                print("[Contract] %s 违约: %s%s" % (
                    collected.get("symbol", "?"), "; ".join(_contract["violations"]),
                    " → 已补:%s" % ((_contract.get("repair") or {}).get("applied"))
                    if _contract.get("repair") else ""), file=sys.stderr)
            result["_contract"] = _contract
        except Exception as _ce:
            result["_contract"] = {"violations": ["validator_error: %s" % _ce]}

    # ── rating/score/direction 一致性校验 (record-only, DEB-20260816 §8.3 项1) ──
    # 不修改任何字段; 矛盾记录进 result["_rating_consistency"] + stderr 供影子观测。
    # 回滚: DA_SCORE_CONSISTENCY=0。
    _consistency = _check_rating_score_consistency(result)
    if _consistency is not None:
        result["_rating_consistency"] = _consistency
        if not _consistency["consistent"]:
            print(f"[ScoreConsistency] ⚠️ {collected.get('symbol','?')}: "
                  + "; ".join(_consistency["issues"]), file=sys.stderr)

    # 补充原始数据（供适配器用）
    snap = collected.get("snapshot", {})
    result["symbol"] = collected.get("symbol", "")
    result["name"] = snap.get("name", "")
    # DEB-20260819 §十七-L1/L2: pangan_injected 打标 — 本次实际写入 prompt 的规则列表
    #   (§十七-L2: 直接取注入循环收集的 _card_injected, 不重读卡文件 — regime 失配
    #    被跳过的规则不进标签; §十七-L1: 语义="注入标记"非"引用标记", 判分分组用
    #    文本引用(cited) vs 注入未引用, 而非"非空vs空"——后者是带卡票vs无卡票的选样混淆)
    if _card_text:
        result["pangan_injected"] = _card_injected
    # [第13轮 P6] 指令版本指纹随决策落盘 → 新旧 prompt 基线可切分
    # [2026-09-01 复查收口] system_override (蒸馏影子 V2 臂) 时按实际 system 文本
    #   计指纹 — 否则 V2 臂会顶着 V1 的 PROMPT_HASH 落盘, 归因切分反向污染
    #   (DEB-20260901c §三承诺的 "V2 独立指纹落盘归因" 由此兑现)。
    result["_prompt_hash"] = (
        _prompt_fingerprint(system_override, effective_user_template(), _SAMPLING_TAG)
        if system_override else PROMPT_HASH
    )
    # [2026-09-01 复查收口] 最终 user_prompt 快照 — 本函数拼装的 5 段动态注入
    #   (交易经验/负向论据警示/条件黑名单/股性卡/成交档案) 只存在于调用时刻,
    #   collected 里没有; pipeline 侧 pop 进生料档 v2 (逐字节回放"模型当时看到了
    #   什么"), 不进分析 checkpoint (防 {symbol}.json 膨胀)。
    result["_user_prompt"] = ((_user_tail + user_prompt) if _user_tail else user_prompt)
    result["_timestamp"] = time.time()
    # [v0.70] 回填同样经 _fundamental_view 归一化 — 键名保持旧口径兼容下游,
    #   值从恒 0 变为真值 (字段错配修复, 见 helper docstring)
    _fs_out = _fundamental_view(snap)
    result["_snapshot"] = {
        "price": snap.get("last_price", 0),
        "change_pct": _fs_out["change_pct"] if _fs_out["change_pct"] is not None else 0,
        "pe_ttm": _fs_out["pe_ttm"],
        "pe_ratio": _fs_out["pe_ratio"],
        "pb_ratio": _fs_out["pb_ratio"],
        "eps": _fs_out["eps"] if _fs_out["eps"] is not None else 0,
        "market_cap": _fs_out["market_cap"],
        "high_52w": _fs_out["high_52w"],
        "low_52w": _fs_out["low_52w"],
        "volume": snap.get("volume", 0),
    }

    # ── 后验证：检查LLM输出的数字是否在溯源清单中 ──
    # 2026-08-11 (DEB-20260811): 传入 data_text — LLM 实际所见完整数据段
    #   (含交易经验/条件黑名单段), 白名单必须与"LLM看到什么"对齐, 否则
    #   注入段里出现的数字会被误判为编造。
    verification = _verify_sourced_numbers(result, collected, data_text=data_text)
    if verification:
        result["_source_verification"] = verification

    return result


def _challenger_data_view(data_text: str) -> str:
    """P3 (DEB-20260814 §9.4 折中方案): 挑战者数据视图重排。

    原 data_text[:4000] 是盲目截断 — 技术面/价格/指标段短且可数值核对, 却被截掉,
    挑战者无法核验分析师的数字 → 低质量审查 → 低 agreed 率 → conf 卡 0.50 (P0 同根)。
    折中: 技术/价格/指标段全量 (这些段落短), 新闻/研报/社区段维持截断 (长文本, 挑战者
    只需知道"有/无"和主旨)。token 增量可控, 直接提升其核验能力。
    回滚: DA_CHALLENGER_FULL_DATA=0 恢复 [:4000]。
    """
    if os.environ.get("DA_CHALLENGER_FULL_DATA", "1") != "1":
        return data_text[:4000]
    try:
        import re as _re
        _FULL = ("数据覆盖统计", "标的基本面", "盘前时段数据", "盘后时段数据", "技术面",
                 "资金面", "异动信号", "基本面指标", "分析师评级", "内部人交易",
                 "内部人情绪", "公司概况", "大盘环境", "板块上下文", "论据溯源清单",
                 "近期交易经验", "条件级历史期望警示",
                 # DEB-20260819 §14.3-Q1: 股性档案挑战者必须同卡可见,
                 # 否则分析师引卡而挑战者无法核证=信息不对称辩论。
                 "股性档案（历史统计验证）",
                 # DEB-20260814 §十二: UE 假设段可数值核验 (因果链/时效/失效条件),
                 #   此前不在白名单 → 挑战者看不到 UE 候选假设, 五维验证漏核事件驱动论点。
                 "事件扫描器假设",
                 # P1/P3 (2026-08-14): Finnhub 新闻事实流是结构化数字事实, 全量放行
                 #   (6条×标题+摘要≈2k字), 挑战者核验新闻类论据数字必须看到它。
                 "Finnhub 新闻事实流",
                 # [2026-08-31 DA链复查] 补漏 4 段 —
                 #   行业对比: 含 DA-A1 注入的行业PE中位数, 分析师引用后挑战者无法核验;
                 #   财报日历: 核验维度d明要求查硬日期风险(财报/CPI/FOMC), 挑战者却不可见;
                 #   数据质量审计: 核验降级判据所需; 前序回吐率: 统计类论据所需。
                 "行业对比", "财报日历", "数据质量审计", "前序回吐率")
        _TRUNC = ("最新新闻", "研报", "社区讨论")
        parts = _re.split(r'(?=^## )', data_text, flags=_re.M)
        out = []
        for p in parts:
            _head = p.lstrip()[:40]
            if any(t in _head for t in _FULL):
                out.append(p)
            elif any(t in _head for t in _TRUNC):
                out.append(p[:600] + ("\n…(原文截断, 挑战者仅需主旨)\n" if len(p) > 600 else ""))
            # 其余未识别段 (空串/杂项) 丢弃
        view = "".join(out)
        return view if view.strip() else data_text[:4000]
    except Exception:
        return data_text[:4000]


def _challenger_analysis_view(initial_analysis: Dict[str, Any]) -> str:
    """[2026-08-31 DA链复查] 挑战者看分析师输出的视图 — 去噪 + 重排 + 截断保护。

    根因: 旧 json.dumps(initial_analysis)[:3000] 是盲目截断 — 实测 8/26-28
    n=50: execution_plan 起始于第 1500-3338 字符, key_levels 被切 48/50 (96%),
    invalidation_triggers 被切 41/50 (82%), 止损/止盈价被切 ~30% → 挑战者
    核验维度 e"价位合理性"与失效条件审查结构性失明; dump 还含 _usage/
    _finish_reason/_model_reported/score_legacy 等噪声字段挤占预算。
    修法: 剥 _ 前缀内部字段, execution_plan/key_levels 前置 (截断若复现
    也只砍低优先尾部), 默认上限 3000→6000 (每候选约 +1k 输入 token)。
    回滚: DA_CHALLENGER_ANALYSIS_CAP=3000 回旧上限 (完整回滚还原 .bak)。
    """
    try:
        _cap = int(os.environ.get("DA_CHALLENGER_ANALYSIS_CAP", "6000") or 6000)
    except (TypeError, ValueError):
        _cap = 6000
    if _cap <= 0:
        _cap = 6000
    _PRIORITY = ("direction", "rating", "confidence", "score", "score_band",
                 "executive_summary", "execution_plan", "key_levels",
                 "bull_case", "bear_case", "constraints", "causal_chain",
                 "risks", "data_quality_note", "skip_reason")
    view: Dict[str, Any] = {}
    for _k in _PRIORITY:
        if initial_analysis.get(_k) is not None:
            view[_k] = initial_analysis[_k]
    for _k, _v in initial_analysis.items():
        if _k.startswith("_") or _k in view:
            continue  # 内部字段(_usage/_finish_reason/score_legacy等)不进挑战者视图
        view[_k] = _v
    return json.dumps(view, ensure_ascii=False, indent=2)[:_cap]


def _run_blind_direction_shadow(data_text: str) -> Optional[Dict[str, Any]]:
    if os.environ.get("DA_CHALLENGER_BLIND_SHADOW", "0") != "1":
        return None
    system = """你是独立方向影子评审。只依据原始数据判断方向，不参考任何已有分析结论。返回纯JSON，不要markdown。"""
    user = f"""原始数据：
{_challenger_data_view(data_text)}

请输出：
{{
  "independent_direction": "BUY/SELL_SHORT/SKIP",
  "top_evidence": "一条最强证据，必须引用输入数据",
  "data_blind_spots": ["无法判断的关键数据缺口"],
  "confidence": "HIGH/MEDIUM/LOW"
}}"""
    started = time.time()
    try:
        result = _call_llm(
            system,
            user,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=2000,
            model=CHALLENGER_MODEL,
            timeout=60,
        )
    except Exception:
        return {"error": True}
    if not isinstance(result, dict) or "_error" in result:
        return {"error": True}
    return {
        "direction": str(result.get("independent_direction") or "SKIP").upper(),
        "top_evidence": result.get("top_evidence"),
        "data_blind_spots": result.get("data_blind_spots") or [],
        "confidence": str(result.get("confidence") or "").upper(),
        "model": CHALLENGER_MODEL,
        "latency_ms": int((time.time() - started) * 1000),
    }


def _run_challenger(initial_analysis: Dict[str, Any], data_text: str) -> Optional[Dict]:
    """Pass 2: 挑战者模型独立验证分析师的方向判断, 产出真共识或真问题.

    返回: {agreed: bool, direction: str, challenged_claims: [...],修正建议: str}
    如果挑战者不可用, 返回None(不影响Pass1结果).
    """
    # DEB-20260814 §十一 (2026-08-14, 用户裁决): 挑战者 v2 — 从"挑毛病审查官"转"独立验证官"。
    #   根因: 旧人设"找出最薄弱的论据"结构上不可能给 AGREE (任何分析都挑得出毛病) →
    #   verdict 恒 PARTIAL_AGREE (历史 21:2) → agreed 恒 False → gate decision_conf 恒 0.50。
    #   v2: 独立先行 (防锚定, GS 案: 首采 BUY 脱离锚定后两次翻 SELL_SHORT) +
    #   五维验证 + AGREE 判据可达 (方向一致且无实质硬伤)。回滚: DA_CHALLENGER_V2=0。
    # [P11 第14轮] v1 分支已删, DA_CHALLENGER_V2 开关随之移除 (恒为 v2)

    challenger_system_v2 = """你是投资证伪官。[DEB-20260825 第14轮 P9 重构] 你的任务不是"独立复现分析"(实测107/107与分析师同向=伪独立, 同一份证据第二次阅读必然趋同), 而是**主动证伪当前候选方向**: 假设它错了, 从输入数据里找出最可能让它亏损的具体证据。

铁律(反仪式化, 针对实测病灶: 旧挑战者107/107都能找到4条missed_risks=为凑数而找):
- 每条反证必须引用输入数据中的具体数字/事实, 写不出出处就**不要写**。
- 反证按实质与出处评价，不设条数门槛。找不到有数据支撑的实质反证时，在missed_risks写["无实质反证: 当前方向的数据支撑成立"]；verdict仍须满足下述方向条件，禁止为凑数编造风险。
- 你的价值不在于"总能找到毛病", 而在于"当真有毛病时能抓出来"。

验证原则:
1. 【独立先行】先忽略分析师的结论, 仅凭原始数据独立判断方向与最强论据; 然后与分析师结论对比。
   若你的独立判断与分析师不同, 必须如实给出 — 独立判断是你的核心价值。
2. 【多维度验证】逐项核验并给 通过/不通过 + 一句证据:
   a. 数据核验: 关键数字是否与原始数据一致 (引用原始数据中的具体值比对); 反虚构硬规:
      分析师论点中的每个具体数字必须能在原始数据中找到出处 (财报/K线/新闻事实流/评级),
      找不到的数字在 missed_risks 标注 "来源不明数字: <数字>出现于<哪条论点>"。
      如实标注是你的核心价值 — 上报数量不影响对你的评价, 幻觉计数由代码独立核验。
   b. 逻辑核验: 证据→方向的因果链是否成立
   c. 方向-市场一致性: 方向与大盘/板块环境是否相符
   d. 风险完备性: 重大风险与硬日期风险(财报/CPI/FOMC)是否已覆盖
   e. 价位合理性: 入场/止损/止盈是否锚定有效现价、止损距离是否合理;
      飞刀几何(2026-09-14): 分析师给 LONG 且盘前涨跌<-5%时, 核验 chase_cap_price 是否
      ≥ 昨收×0.95 — 低于飞刀线的禁追线 = 指令在执行层结构性不可成交(实质硬伤, 计入 PARTIAL 判据);
      第二腿(2026-10-23): 当前执行端不执行第二腿; 若分析师写了第二个可执行触发价, 记为研究背景并建议删掉, 不作为入场条件
3. 【verdict 判据 — 必须携带真实信息】:
   - AGREE: 你的独立方向与分析师一致, 且验证未发现实质硬伤。真正认同时就给 AGREE,
     不要为了显得严格而编造毛病 — 措辞瑕疵/次要补充风险不构成 PARTIAL (写进 missed_risks 即可)。
   - PARTIAL_AGREE: 方向一致, 但验证发现至少一条实质硬伤。实质硬伤 = 足以改变确信度或
     入场条件的错误 (数字失实/因果断裂/关键风险未定价)。必须指明: 哪条论据、与哪条数据矛盾。
   - DISAGREE: 你的独立方向与分析师相反。必须给出反证 (你独立判断所依据的数据)。
4. 对称验证: 分析师看多则检验是否过度乐观, 看空则检验是否过度悲观。不预设偏向。
5. 【表态】my_direction 给 BUY/SELL_SHORT/SKIP。
   证据支持选边就选边; 证据不足就给 SKIP 并在 skip_reason 写明具体证据缺口 —
   弃权是中性输出(不加分不惩罚), 优于被迫选边。弃权率本身是观测信号。
6. SHORT 加严审查: 若你或分析师主张 SELL_SHORT, 必须核验三问 — 具体下跌催化剂/催化剂时效/
   逼空风险。任何一问缺失即在 missed_risks 中标注 "SHORT三问不全", 且 my_score 不得超过 4。
7. 【证伪启发式】(方向性闸门由执行层代码执行, 你不产指令; 以下为证伪视角):
   a. 当日跌 >5% 的标的若分析师仍推 LONG, 重点核验其"企稳/错杀证据"是否真实存在于输入数据;
      若你仍选 BUY, 必须在 skip_reason 中写明企稳证据, 否则 my_score_band 给 LOW。
   b. 若分析师已 SKIP, 你选 BUY/SELL_SHORT 时必须给出"为什么仍值得交易"的理由。

返回纯JSON格式（不要markdown）:
{
    "independent_direction": "BUY/SELL_SHORT/SKIP",
    "validation": {
        "data": "通过/不通过+一句证据",
        "logic": "通过/不通过+一句证据",
        "market": "通过/不通过+一句证据",
        "risk": "通过/不通过+一句证据",
        "levels": "通过/不通过+一句证据"
    },
    "agreed": true/false,
    "my_direction": "BUY/SELL_SHORT/SKIP",
     "my_score_band": "HIGH/MEDIUM/LOW (对my_direction的确信度档位, 锚定判据同分析师: HIGH需独有事件+未被价格反映+数据充分; SKIP时给LOW)",
    "my_score": 0-10 (兼容字段, 可留空, 系统以my_score_band为准),
    "skip_reason": "选边理由+证据缺口说明 (必填)",
    "falsification_target": "若这笔交易亏损, 最可能的单一机械原因 — 具体可观测条件+时间窗 (如'收盘价跌破X'/'财报日落地后利好出尽'); 原始数据不支持精确价位时只写可观测条件, 禁止编造数字。AGREE 时同样必填 — 这是 AGREE 携带信息的方式",
    "flip_condition": "出现什么数据/事实会让你改变 verdict (一句话, 可引用阈值)。AGREE 时同样必填",
    "challenged_claims": [
        {"original_thesis": "被挑战的论点", "weakness": "实质硬伤原因", "counter_evidence": "反证数据(必须含原始数据出处)"}
    ],
    "missed_risks": ["被忽视的风险; 若无实质反证写'无实质反证', 禁止编造"],
    "verdict": "AGREE / PARTIAL_AGREE / DISAGREE"
}"""

    challenger_system = challenger_system_v2  # v1 已删 (P11, 第14轮)

    challenger_user = f"""【证伪任务 — 第14轮 P9】候选方向 = {initial_analysis.get('direction','?')}。

第一步【假设它错了】:
仅根据以下原始数据, 假设 {initial_analysis.get('direction','?')} 方向会亏损, 找出最可能导致亏损的具体证据。
把你在"不看分析师结论"下独立倾向的方向填入 independent_direction (作为防锚定观测)。

原始数据:
{_challenger_data_view(data_text)}

---

第二步【对照分析师的论据】:
分析师的结论与论据如下。逐条核验其数字是否与上方原始数据一致:

{_challenger_analysis_view(initial_analysis)}

---

第三步【裁决 verdict】:
- 独立方向与分析师相反，且有足以推翻方向的数据反证 → DISAGREE, my_direction 给反方向
- 同向但至少一条有出处的实质硬伤(足以改变确信度或入场条件) → PARTIAL_AGREE, 指明哪条论据与哪条数据矛盾
- 同向且无实质硬伤 → AGREE；次要风险只写进 missed_risks，不构成PARTIAL
- 证据不足 → my_direction=SKIP，并在 skip_reason 写明具体证据缺口

无论 verdict 为何, falsification_target 与 flip_condition 必填 (AGREE 也不例外) —
"证伪失败"不等于零信息: 你必须指出这笔交易最可能的亏损机制, 以及什么数据出现会让你翻向。

【反虚构硬规】: 你的每条 challenged_claims 的 counter_evidence 与 missed_risks 里若含数字,
该数字必须能在上方原始数据中找到。找不到来源的数字 = 你的幻觉, 会被记录。
validation 五维照常核验(数据/逻辑/市场/风险/价位), 每维给 通过/不通过+一句证据。

my_score / my_score_band: 表达你对"自己my_direction"的确信度档位 (HIGH/MEDIUM/LOW,
锚定判据与分析师相同)。若你 AGREE (证伪失败), my_direction = 分析师方向, my_score_band 给分析师论据的实际强度档位。

返回JSON。"""
    blind_shadow = _run_blind_direction_shadow(data_text)
    try:
        # [2026-09-14 用户裁"都开启 enable_thinking, 确保正常的思考不被中断, 效果优先"]
        #   挑战者位原**无思考** — 本调用点不传 extra_body, 而 _call_llm 的 payload
        #   硬编码 "enable_thinking": False ⇒ 生产实测 reasoning=0 (n=40)。
        #   三处联动改动 (缺一即坏), 依据=真实生产载荷同构回放 (n=4, prompt 截获自
        #   _run_challenger 本体, 非手工复刻):
        #     实测: reasoning 6905-9505 / 正文 1472-2227 / 延迟 48.2-75.5s /
        #           12 键契约 4/4 全过 / finish=stop。
        #   ① max_tokens 2000 → 16000: 推理 token 计入输出预算, 2000 会被思考吃穿
        #      → 正文空 → JSON 解析必败 → 静默退化"同模型自我辩论"。
        #      (2026-09-14 探针反证: 关思考时 max_tokens=2000 正常出 12 键;
        #       一旦开思考而仍用 2000, 两模型 completion=reasoning=2000 且 content 为空。)
        #   ② timeout 缺省 60 → 300: **_call_llm 默认 timeout=60, 而实测延迟
        #      48.2-75.5s 已越过 60s 硬墙** — 不抬 timeout 则开思考后必超时, 且超时
        #      走 8s 重试 = 双倍耗时仍可能失败。这是本改动最易漏的一处。
        #   ③ 不传 thinking_budget: 实测该模型**不遵守** thinking_budget —— 传 8192 时
        #      reasoning 反而到 9505 (n=4 全部超出所报预算)。真正的闸门是 max_tokens。
        #      传一个被静默忽略的参数会给后来者虚假安全感, 故不传并在注释留证。
        #   未同步改下方 fallback 调用 (同模型自我辩论路径): 该路径 2026-09-14 时是冷路径
        #   (当时实测 challenger_fallback 全 False); **2026-09-18 实测已转热** — 全窗
        #   5/109 (9/18 当日 3/23=13%), 且这批样本笔均净 −$60.92 (n=4 配对, 方向性)。
        #   [2026-09-18 用户裁「做」— 独立模型 fallback 接线] 原 fallback = 主模型
        #   (qwen3.7-max = 分析师本体) ⇒ 同模型自辩, 违反协议硬规「禁止一个模型自辩自裁」
        #   (multi-model-debate SKILL:39), 且信息量为 0、污染挑战者归因。现改为三级:
        #     T1 (primary): CHALLENGER_MODEL = deepseek-v4.1-flash      同参 (思考+16000+t300)
        #     T2 (fallback1, 换家族): CHALLENGER_FALLBACK_MODEL = kimi-k2.6  同参 (可比口径)
        #     T3 (fallback2, 换家族+换失败模式): CHALLENGER_FALLBACK_MODEL_2 = glm-5.2
        #        关思考+2000+t60 — 专治"思考模式下的超时/JSON 解析失败"(全档开思考会一起死)
        #     T4 (末位兜底): 主模型模型 = 同模型自辩, **显式标记 _challenger_self_debate=true**
        #        供下游/判据排除 (不再伪装成独立挑战者)
        #   选型实弹探针 (2026-09-18, 5 模型 × 2 参数档, 小 prompt 只读):
        #     deepseek-v4.1-flash 思考✅1.7s / 关思考✅0.9s; kimi-k2.6 ✅4.2s / ✅1.1s;
        #     glm-5.2 ✅5.4s / ✅0.8s; **glm-5.3 关思考必 HTTP 400 (强制思考模型)** — 故
        #     T3 用 glm-5.2 而非 glm-5.3; deepseek-v4-pro ✅2.0s (同家族 primary, 不用于 T2/T3)。
        #   不选 qwen3.8-flash/qwen3.7-max: 与分析师同家族 (违反禁同家族)。
        #   成本: ~$0.011/次 (kimi-k2.6 8-10k in / 1.5-2k out) × ~1 次/日 ≈ $0.01/日。
        #   回滚: llm_synth.py.bak-challengerchain-20260918 回退本文件 (config 三个常量可留, 零引用无害)。
        _challenger_specs = (
            ("primary", CHALLENGER_MODEL, dict(max_tokens=16000, extra_body={"enable_thinking": True}, timeout=300)),
            ("fallback_independent", CHALLENGER_FALLBACK_MODEL,
             dict(max_tokens=16000, extra_body={"enable_thinking": True}, timeout=300)),
            ("fallback_independent_nonthinking", CHALLENGER_FALLBACK_MODEL_2,
             dict(max_tokens=2000, timeout=60)),
            ("self_debate", MODEL, dict(max_tokens=2000, timeout=60)),
        )
        _first_err = None
        for _tier, _mdl, _kw in _challenger_specs:
            result = _call_llm(challenger_system, challenger_user,
                               temperature=config.LLM_TEMPERATURE, model=_mdl, **_kw)
            if result and "_error" not in result:
                # tier 打标无条件化 (2026-09-23 复查批 P2): primary 成功也记 tier="primary" ——
                # 此前只在回落分支打标, 主路径运行落 tier=null, 与「v0.83 之前旧数据无此
                # 字段」不可区分 (实测 9/21-9/22 34/35 次运行 tier=None), 归因记账无法分桶。
                result["_challenger_tier"] = _tier
                result["_challenger_self_debate"] = (_tier == "self_debate")
                if _tier != "primary":
                    # 原发失败原因先留档 (2026-09-18 可见性补: 此前整段丢弃 ⇒ 不可定因)
                    result["_challenger_fallback"] = True
                    if _first_err:
                        result["_challenger_primary_error"] = str(_first_err.get("_error"))[:300]
                        result["_challenger_primary_finish"] = _first_err.get("_finish_reason")
                        result["_challenger_primary_raw_len"] = _first_err.get("_raw_len")
                        result["_challenger_primary_raw_tail"] = _first_err.get("_raw_tail")
                result["_challenger_model_used"] = _mdl
                if _tier == "primary":
                    # [DEB-20260825 第13轮 P3a — 影子结案] 原 DEB-20260723 H5 采样稳定性 shadow
                    #   (同输入再采样 2 次测方向翻转率) 按其自设判据结案并下线:
                    #   v2 窗 (8/14-8/24) n=107 全部落库, **方向翻转 0/107**, 且 challenge_direction_samples
                    #   全代码库零消费方 (grep app/ scripts/ 仅命中 archive 补丁与 DEB 文档)
                    #   → 立项时"一周后翻转率高再议 3 取众数"的触发条件已被否证, 且逾期一个月无裁决。
                    #   收益: 省 2 次 LLM 调用/候选 (v2 窗口径 −214 次 / 11 天)。
                    #   字段保留单元素 = 下游 _debate.challenge_direction_samples 契约不变 (零破坏)。
                    #   回滚: 恢复 for 循环即可 (但按 §E 判据本项无需再观测)。
                    result["_direction_samples"] = [str(result.get("my_direction", "?")).upper()]
                # [P6] 挑战者侧 prompt 指纹 (与分析师侧 PROMPT_HASH 分开, 便于按侧切分基线)
                result["_challenger_prompt_hash"] = _prompt_fingerprint(
                    challenger_system, "v3-falsify", _SAMPLING_TAG)
                if blind_shadow is not None:
                    result["_blind_independent_shadow"] = blind_shadow
                return result
            if _first_err is None and result and "_error" in result:
                _first_err = dict(result)
    except Exception as e:
        pass
    return None


_RATING_DIR_MAP = {
    "强烈看多": "BUY", "看多": "BUY",
    "看空": "SELL_SHORT", "强烈看空": "SELL_SHORT",
    # 持有/观望 → SKIP (中性档, 与 prompt L658 "direction必须与rating一致" 对齐)
    "持有": "SKIP", "观望": "SKIP",
}


def _check_rating_score_consistency(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """rating/direction/score 三字段一致性校验 (record-only, DEB-20260816 §8.3 项1).

    根因: prompt 原只写 "score: 0-10的数字" 无语义定义 → LLM 双字段自由生成,
    8/15 出现 看多(4.0)/看空(7.0) 矛盾对。score 语义现已定义为 conviction
    (方向无关确信度), 在此语义下的矛盾判定 (§7.2 裁决对齐):
      - 看多(4.0) 本身自洽 (低确信看多), 不判矛盾
      - rating 与 direction 方向相反 = 任何语义下都矛盾
      - rating 中性档(观望/持有) 却有方向 = 违反 prompt "direction必须与rating一致"
      - "强烈" 档断言高确信, score≤4 = 低确信, 自相矛盾
    本函数不修改任何字段 (零执行风险), 仅记录供 shadow 观测。
    回滚: DA_SCORE_CONSISTENCY=0。
    """
    if os.environ.get("DA_SCORE_CONSISTENCY", "1") != "1":
        return None
    try:
        rating = str(result.get("rating", "")).strip()
        direction = str(result.get("direction", "SKIP")).upper()
        score = float(result.get("score", 5.0))
        issues = []

        # 1) rating ↔ direction 方向相反 (硬矛盾)
        expected_dir = _RATING_DIR_MAP.get(rating)
        if expected_dir in ("BUY", "SELL_SHORT") and direction in ("BUY", "SELL_SHORT") \
                and expected_dir != direction:
            issues.append(f"rating_direction_conflict: rating={rating} 期望{expected_dir} 实际direction={direction}")

        # 2) rating 中性档却有方向 (违反 prompt L658 "direction必须与rating一致")
        if expected_dir == "SKIP" and direction in ("BUY", "SELL_SHORT"):
            issues.append(f"neutral_rating_with_direction: rating={rating} 但 direction={direction}")

        # 3) rating 有方向却 SKIP (镜像矛盾, 观测级)
        if expected_dir in ("BUY", "SELL_SHORT") and direction == "SKIP":
            issues.append(f"directional_rating_but_skip: rating={rating} 但 direction=SKIP")

        # 4) "强烈" 档断言高确信 vs score≤4 低确信 (conviction 语义下自相矛盾)
        if rating in ("强烈看多", "强烈看空") and score <= 4:
            issues.append(f"strong_rating_low_conviction: rating={rating} 但 score={score}≤4")

        return {
            "rating": rating,
            "direction": direction,
            "score": score,
            "consistent": not issues,
            "issues": issues,
        }
    except Exception as e:
        return {"consistent": False, "issues": [f"check_error: {e}"]}


def _merge_debate_results(initial: Dict[str, Any], challenge: Dict[str, Any]) -> Dict[str, Any]:
    """融合2模型辩论结果.

    规则:
    - agreed=True → confidence提升1档, 在risks追加"挑战者确认"
    - agreed=False但方向一致 → confidence不变, 追加挑战者质疑到risks
    - 方向不一致 → confidence降1档; score差距>3 → direction改SKIP
    """
    # [P8/P9 第14轮] 挑战者 my_score_band → my_score 数字映射 (与分析师同映射表).
    #   挑战者侧字段是 my_score_band; 若给了合法 band 则覆盖 my_score, 保留 my_score_legacy.
    try:
        if os.environ.get("DA_SCORE_BAND", "1") != "0":
            _cband = str(challenge.get("my_score_band") or "").strip().upper()
            if _cband in _SCORE_BAND_MAP:
                if challenge.get("my_score") is not None:
                    try:
                        challenge["my_score_legacy"] = float(challenge.get("my_score"))
                    except (TypeError, ValueError):
                        pass
                challenge["my_score"] = _SCORE_BAND_MAP[_cband]
                challenge["_my_score_band_applied"] = _cband
    except Exception:
        pass

    # [E2 2026-08-31 DEB-20260830 第4轮执行项] 模型原始自评五档置信落库 (record-only,
    #   不入任何门)。背景: 辩论逻辑在本函数内覆盖 confidence (conf_map 升/降档),
    #   adapter.py:_compute_confidence 又硬置 0.50+辩论调整 — 模型原话从不存档,
    #   "模型自评能否用" (B2) 无法取证。此处必须在首次覆盖前捕获。
    #   注意区分: signal_gate.py:94 的 llm_self_conf 读的是本函数【输出】(辩论后),
    #   落 da_gate_decisions.jsonl; 本字段落指令级 raw_instruction_json (辩论前),
    #   两者时间点不同、用途不同 (后者可 join fwd_return 做校准), 非重复埋点。
    initial["_llm_self_confidence"] = initial.get("confidence")
    try:
        _ch_band = challenge.get("my_score_band")
        if _ch_band is not None:
            initial["_challenger_self_confidence"] = _ch_band
    except Exception:
        pass

    orig_dir = str(initial.get("direction", "SKIP")).upper()
    chall_dir = str(challenge.get("my_direction", "SKIP")).upper()
    verdict = challenge.get("verdict", "")
    _agreed_raw = challenge.get("agreed", False)
    # 2026-10-23: verdict/方向是权威; agreed 布尔只作原始记录. PARTIAL_AGREE
    # 即使 agreed=true 也不得升级为独立一致; gate/adapter 继续读
    # challenger_agreed, 但该值已在这里归一为规范状态.
    _state_fix = os.environ.get("DA_DEBATE_STATE_FIX", "1") != "0"
    if _state_fix and classify_debate is not None:
        _dstate = classify_debate(orig_dir, chall_dir, verdict, True)
        agreed = _dstate["state"] == "AGREE"
    else:
        # 回滚: DA_DEBATE_STATE_FIX=0 → 旧布尔/字符串口径
        _dstate = {"state": "LEGACY", "reason": "legacy_agreed_boolean"}
        agreed = bool(_agreed_raw or verdict == "AGREE")
    # P1 (2026-07-10): fallback 时挑战者=主模型(非独立). 记真实模型名,
    #   且"方向一致"不升 confidence — 同模型自我辩论的一致是伪独立信号.
    _is_fallback = bool(challenge.get("_challenger_fallback"))
    # [2026-09-18 用户裁「做」] 真正决定"是否独立"的是 self_debate (T4 同模型自辩),
    #   不再是笼统的 fallback: T2/T3 是**换家族的独立模型** ⇒ 其一致可正常升 confidence,
    #   T4 才是伪独立 (不升)。原写法 (not _is_fallback) 会把 T2/T3 误判成非独立。
    _self_debate = bool(challenge.get("_challenger_self_debate"))
    _actual_challenger = (challenge.get("_challenger_model_used")
                          or (MODEL if _is_fallback else CHALLENGER_MODEL))

    # 记录辩论信息
    # DEB-20260814 §十一: v2 新增 independent_direction/validation 落库;
    #   锚定背离 (独立方向≠最终方向) 标记供影子期观测防锚定效果。
    _ind_dir = str(challenge.get("independent_direction", "") or "").upper()
    initial["_debate"] = {
        "challenger_model": _actual_challenger,
        "challenger_fallback": _is_fallback,
        # [2026-09-18 用户裁「做」] 三级链归因: tier ∈ primary/fallback_independent/
        #   fallback_independent_nonthinking/self_debate; self_debate=true 的样本是
        #   **同模型自辩** (T4 末位兜底) ⇒ 判据/IC 记账须排除, 不得当独立挑战者样本。
        "challenger_tier": challenge.get("_challenger_tier"),
        "challenger_self_debate": bool(challenge.get("_challenger_self_debate")),
        "challenger_agreed": agreed,
        "challenger_agreed_raw": _agreed_raw,
        "challenger_direction": chall_dir,
        "challenger_score": challenge.get("my_score"),
        "challenger_verdict": verdict,
        "challenger_verdict_state": _dstate["state"],
        "challenger_state_reason": _dstate.get("reason"),
        "challenger_independent_direction": _ind_dir or None,
        "challenger_anchor_divergence": bool(_ind_dir and _ind_dir != chall_dir),
        "challenger_validation": challenge.get("validation"),
        "challenged_claims": challenge.get("challenged_claims", []),
        "missed_risks": challenge.get("missed_risks", []),
        # [DEB-20260907] AGREE 信息量载体: 最可能亏损机制 + 翻向条件 (任何 verdict 必填)
        "falsification_target": challenge.get("falsification_target"),
        "flip_condition": challenge.get("flip_condition"),
        "challenger_model_reported": challenge.get("_model_reported"),
        "challenger_skip_reason": challenge.get("skip_reason"),
        "challenge_direction_samples": challenge.get("_direction_samples"),
        "challenger_blind_shadow": challenge.get("_blind_independent_shadow"),
        # [第13轮 P6] 挑战者指令指纹 (v1/v2 与其内容变更均可切分)
        "challenger_prompt_hash": challenge.get("_challenger_prompt_hash"),
        # [2026-09-09 F1] 挑战者 token 用量落 checkpoint — 此前日报只计次数,
        #   挑战者成本 (~$0.23/日) 完全失明; 与分析师 _usage 同构 (DashScope usage dict)。
        "challenger_usage": challenge.get("_usage"),
        "challenger_finish_reason": challenge.get("_finish_reason"),
        # [2026-09-18 可见性补] 伪独立辩论 (fallback) 的原发失败原因此前**被整段丢弃**
        #   ⇒ 13% 的 fallback 率只能观测到"发生了", 无法定因 (2026-09-18 探针: 现役参数
        #   enable_thinking+16000+t300 在同一模型上 3/3 正常, 故非参数性拒绝 ⇒ 必须留痕
        #   才能区分 429/超时 vs JSON 解析失败 (二者处置相反: 前者加重试/错峰, 后者改 prompt)。
        "challenger_primary_error": challenge.get("_challenger_primary_error"),
        "challenger_primary_finish": challenge.get("_challenger_primary_finish"),
        "challenger_primary_raw_len": challenge.get("_challenger_primary_raw_len"),
        "challenger_primary_raw_tail": challenge.get("_challenger_primary_raw_tail"),
    }

    # 方向一致
    if orig_dir == chall_dir:
        # [丙1 PROPOSAL-20260905 P1, 2026-09-05 用户裁"如无问题可以实施"] 同向低分不再覆写 score.
        #   根因(glm 复核实证): HIGH 档 33/43=77% 被 8.0→6.0 拉平, score_band 单调性
        #   (HIGH +35.70 / MEDIUM·0.9 +9.43 / MEDIUM·0.8 −29.93) 在合成层被摧毁。
        #   P12 取低逻辑原意是"挑战者更保守信号入合成", 但实际效果是分析师区分度归零。
        #   改为影子记录: score 保持分析师原值 (band 映射后), 挑战者分进
        #   _challenger_score_shadow; _score_lowered_by_challenger 改义为"若覆写本会
        #   生效"标记 (仅归因, 离线反事实测算见 §丙1 影响面)。
        try:
            _o_sc = float(initial.get("score", 5))
            _c_sc = challenge.get("my_score")
            if _c_sc is not None:
                initial["_challenger_score_shadow"] = float(_c_sc)
                if float(_c_sc) < _o_sc:
                    initial["_score_lowered_by_challenger"] = True
        except (TypeError, ValueError):
            pass
        if (agreed or verdict == "AGREE") and not _self_debate:
            # 提升confidence (仅当挑战者为**独立模型** — 2026-09-18 起按 self_debate 判,
            #   换家族的 T2/T3 视为独立)
            conf_map = {"低": "中低", "中低": "中", "中": "中高", "中高": "高", "高": "高"}
            old_conf = initial.get("confidence", "中")
            initial["confidence"] = conf_map.get(old_conf, old_conf)
            initial["debate_outcome"] = f"✅ 2模型一致({orig_dir}), confidence提升"
        elif agreed or verdict == "AGREE":
            initial["debate_outcome"] = f"✅ 方向一致({orig_dir}), 但挑战者=同模型自辩(非独立), confidence不升"
        else:
            initial["debate_outcome"] = f"⚠️ 方向一致但挑战者有质疑({orig_dir})"
    elif "SKIP" in (orig_dir, chall_dir):
        # 2026-07-18 根因修复: 一方弃权(SKIP) != 真反向。一方有观点、一方观望,
        #   不等于"唱反调"。保持有观点方的方向, confidence 中性不重罚 (弃权非负信号)。
        #   (7/17 实证: 14条'一方SKIP'原被当方向分歧降conf, 压进tiny/BLOCK档。)
        # 2026-09-07 (DEB-20260907): 挑战者 prompt 侧已允许 SKIP (弃权中性) — 此处为正常承接分支:
        #   挑战者 SKIP = 合法弃权 → 标记 _challenger_abstained 供弃权率统计,
        #   方向按"有观点方保持"处理 (不罚)。
        _view_dir = orig_dir if orig_dir != "SKIP" else chall_dir
        if chall_dir == "SKIP" and orig_dir != "SKIP":
            initial["_challenger_abstained"] = True
            initial["debate_outcome"] = f"➖ 挑战者弃权(SKIP) [合法弃权, 不罚], 保持{orig_dir}, confidence不罚"
        else:
            initial["debate_outcome"] = f"➖ 一方弃权({orig_dir} vs {chall_dir}), 保持有观点方向, confidence不罚"
    else:
        # 真反向分歧 (BUY↔SELL_SHORT)
        orig_score = float(initial.get("score", 5))
        chall_score = float(challenge.get("my_score", 5))
        if abs(orig_score - chall_score) > 3:
            # 分歧大 → SKIP
            initial["direction"] = "SKIP"
            initial["debate_outcome"] = f"❌ 2模型真反向({orig_dir} vs {chall_dir}), score差距{abs(orig_score-chall_score):.1f}→SKIP"
            # 降低score
            initial["score"] = min(orig_score, chall_score)
            # [第14轮] 代码改判SKIP后补默认skip_reason — 否则触发契约skip_reason_missing违约
            if not str(initial.get("skip_reason") or "").strip():
                initial["skip_reason"] = f"双模型方向真反向({orig_dir} vs {chall_dir})且确信度差距大, 系统改判观望"
        else:
            # 分歧不大 → 降低confidence, 保持原方向但标注
            conf_map = {"高": "中高", "中高": "中", "中": "中低", "中低": "低", "低": "低"}
            old_conf = initial.get("confidence", "中")
            initial["confidence"] = conf_map.get(old_conf, old_conf)
            initial["debate_outcome"] = f"⚠️ 2模型真反向({orig_dir} vs {chall_dir}), score接近→降confidence"

    # 追加挑战者发现的风险
    missed_risks = challenge.get("missed_risks", [])
    if missed_risks:
        existing_risks = initial.get("risks", [])
        for r in missed_risks:
            if r and r not in existing_risks:
                existing_risks.append(f"[挑战者] {r}")
        initial["risks"] = existing_risks

    # 追加被挑战的论据标记
    challenged = challenge.get("challenged_claims", [])
    if challenged:
        for c_item in challenged:
            orig_thesis = c_item.get("original_thesis", "")
            # 在bull_case/bear_case中找到对应论据并标注
            for section in ["bull_case", "bear_case"]:
                for claim in initial.get(section, []):
                    if orig_thesis and orig_thesis[:20] in claim.get("thesis", ""):
                        claim["challenged"] = True
                        claim["challenge_weakness"] = c_item.get("weakness", "")
                        claim["challenge_counter"] = c_item.get("counter_evidence", "")

    return initial


def _is_sourced_derivative(num: float, source_numbers: set) -> bool:
    """num 是否为两个源数字的简单运算 (容差 0.5%) — 限两两配对, 防任意算术自由化.

    支持的运算 (DEB-20260829 A2, 第3轮批准):
      ① 两两均值 (原始实现): num ≈ (a+b)/2
      ② 两两差: num ≈ a−b (如"目标价差 $12")
      ③ 两两比率%: num ≈ a/b×100 (如"为同业的1.8倍"→180)
    容差 = max(|num|×0.5%, 0.01), 与原均值容差同尺。

    实证案例 (DEB-20260811): GS 目标价 $1222.5 = mean(星展1200, 巴克莱1245),
    两源均在输入研报中, 属合理引用。
    第3轮背景: quality_score 对 PASS/BLOCK 影响恒为零 (判定链无该字段),
    本扩展只恢复"unverified 率"的测量价值, 不改变任何交易行为。
    """
    if not source_numbers:
        return False
    tol = max(abs(num) * 0.005, 0.01)
    # ① 均值: 只在量级相近的源数字里配对 (剪枝: ±25%)
    band = sorted(x for x in source_numbers if num * 0.75 <= x <= num * 1.25)
    if len(band) >= 2:
        # band 通常很小 (同量级数字); 双指针找两数均值≈num 的配对
        i, j = 0, len(band) - 1
        while i < j:
            m = (band[i] + band[j]) / 2.0
            if abs(m - num) <= tol:
                return True
            if m < num:
                i += 1
            else:
                j -= 1
    # ②③ 需要全量有序列表做容差邻域查找 (bisect)
    import bisect
    ordered = sorted(source_numbers)

    def _near_match(value: float, exclude: float, tol_abs: float) -> bool:
        """source_numbers 中是否有容差内命中 value 的数, 且与 exclude 不同数
        (防平凡自匹配: num=0 差/num=100 比率会恒真)。
        tol_abs 由调用方按被比较量纲给出 (见下两支注释)。"""
        idx = bisect.bisect_left(ordered, value)
        for k in (idx - 1, idx):
            if 0 <= k < len(ordered):
                cand = ordered[k]
                if abs(cand - value) <= tol_abs and abs(cand - exclude) > tol_abs:
                    return True
        return False

    # ② 差: num ≈ a−b ⇔ b ≈ a−num。b 侧比较误差 1:1 等于结果误差
    #    (|b−(a−num)| = |(a−b)−num|), 故容差 ∝ |num| 量纲正确。
    #    |num|≤tol 时 b≈a 自匹配无意义, 跳过。
    #    守卫 (2026-08-30 复核): b 必须 |b|>tol — 否则 a−0≈a 换皮放行
    #    "num≈某源数字" (比末尾 0.5 绝对容差的四舍五入兜底更宽, 对大数源失控)。
    if abs(num) > tol:
        for a in ordered:
            v = a - num
            idx = bisect.bisect_left(ordered, v)
            for k in (idx - 1, idx):
                if 0 <= k < len(ordered):
                    b = ordered[k]
                    if abs(b - v) <= tol and abs(b - a) > tol and abs(b) > tol:
                        return True
    # ③ 比率%: num ≈ a/b×100 ⇔ a ≈ num×b/100 (num≈0 无意义, 跳过)。
    #    容差维度 (2026-08-30 复核修复): a 侧误差放大 100/b 倍到结果,
    #    故 a 侧容差必须 ∝ |target|=|num×b/100| 而非 |num| —
    #    否则大 num 配小 b 时容差虚胖 (复核实弹: 编造 999.9 被 6.0/0.8=750 放行,
    #    实际差 25%; tol=5.0 是按 num 算的, 对 target=8.0 是 62% 容差)。
    if abs(num) > 1e-9:
        for b in ordered:
            if abs(b) <= 1e-9:
                continue
            target = num * b / 100.0
            tol_t = max(abs(target) * 0.005, 0.01)
            if _near_match(target, exclude=b, tol_abs=tol_t):
                return True
    return False


def _kline_derived_whitelist(collected: Dict[str, Any]) -> set:
    """P5 机器验证 (DEB-20260814 §9.4): 可由真实K线计算的派生量白名单。

    立项根因 (§9.3 MU案): "放量突破短期均线压制"是可计算事实, LLM 从60日K线段
    自行推算的量比倍数/均线距离不在文本数字里 → 被溯源校验误判幻觉 → 真论据
    被文本惩罚压成 SKIP。幻觉校验从事后文本惩罚改为事前数据验证: 数字能由
    collected["kline"] 真实数据算出 (容差内) 即视为已溯源。
    DEB 原文提 replay_bars — 此处用 collected["kline"] (分析师实际所见同源,
    同一次采集, 无新依赖/无 IO), 语义等价且更短路径。回滚: DA_P5_KLINE_VERIFY=0。
    """
    out = set()
    if os.environ.get("DA_P5_KLINE_VERIFY", "1") != "1":
        return out
    try:
        klines = collected.get("kline", []) or []
        if not isinstance(klines, list) or len(klines) < 5:
            return out
        closes = [float(k.get("close", 0) or 0) for k in klines]
        highs = [float(k.get("high", 0) or 0) for k in klines]
        lows = [float(k.get("low", 0) or 0) for k in klines]
        vols = [float(k.get("volume", 0) or 0) for k in klines]
        # 均线 (与 _format_data_for_prompt 同口径)
        for w in (5, 10, 20, 60):
            if len(closes) >= w:
                out.add(round(sum(closes[-w:]) / w, 2))
        # 涨跌幅 (1/3/5/10/20日)
        for w in (1, 3, 5, 10, 20):
            if len(closes) > w and closes[-w - 1] > 0:
                out.add(round((closes[-1] - closes[-w - 1]) / closes[-w - 1] * 100, 2))
        # 量比 (近5日每日 vs 20日均量) — LLM 常引用"放量X倍"
        if len(vols) >= 20:
            avg20 = sum(vols[-20:]) / 20
            if avg20 > 0:
                for i in range(1, min(6, len(vols))):
                    out.add(round(vols[-i] / avg20, 2))
        # 振幅/极值 (近5日)
        for i in range(1, min(6, len(klines))):
            out.add(round(highs[-i], 2)); out.add(round(lows[-i], 2))
        # 现价距均线距离% (近端均线)
        if closes and closes[-1] > 0:
            for w in (5, 10, 20):
                if len(closes) >= w:
                    ma = sum(closes[-w:]) / w
                    if ma > 0:
                        out.add(round((closes[-1] - ma) / ma * 100, 2))
    except Exception:
        pass
    return out


def _verify_sourced_numbers(result: Dict[str, Any], collected: Dict[str, Any],
                            data_text: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """后验证：解析LLM输出bull_case/bear_case中的数字，检查是否在溯源清单中。

    data_text: LLM 实际所见的完整数据段 (synthesize 传入)。full 模式下白名单
      从它提取; 缺省则回退 _format_data_for_prompt(collected)。

    Returns: {
        "verified": [数字列表],
        "unverified": [数字列表],  # 不在输入数据中的数字
        "flagged_claims": [论点列表],  # 包含未验证数字的论点
    }
    或 None（无数字或无数据）
    """
    import re

    # 从collected提取所有真实数字
    # 2026-08-11 (DEB-20260811 O5 实锤修复, 方案A): 白名单扩为 LLM 实际所见 prompt 全文
    #   的数字。旧白名单仅覆盖 snapshot/fundamentals/insider_sentiment/analyst_consensus
    #   4 个 dict, 而 prompt 实际注入 12 类数据 (新闻/研报/社区/peer/大盘/财报日历/板块…)
    #   → LLM 引用其余 8 类输入中的任何数字必被判"未溯源", 8/10 实测 30/30 全中
    #   (误报率 100%, GS "$1222.5=星展1200/巴克莱1245均值" 被误判为幻觉, 两源数字均真实
    #   存在于输入研报)。修复: 对 _format_data_for_prompt 输出做同一 regex 提取。
    #   回滚: DA_SOURCE_VERIFY_MODE=legacy (恢复旧 4-dict 白名单)。
    _sv_mode = os.environ.get("DA_SOURCE_VERIFY_MODE", "full")
    source_numbers = set()
    if _sv_mode == "full":
        try:
            _prompt_text = data_text if data_text else _format_data_for_prompt(collected)
            for _ns in re.findall(r'-?\d[\d,]*\.?\d*', _prompt_text):
                try:
                    source_numbers.add(round(float(_ns.replace(",", "")), 2))
                except ValueError:
                    pass
        except Exception:
            source_numbers = set()
        if not source_numbers:
            _sv_mode = "legacy"  # 提取失败降级旧逻辑
        else:
            # P5 机器验证 (DEB-20260814): 并入可由真实K线计算的派生量 —
            #   LLM 从60日K线段自行推算的量比/均线距离等"可计算事实"不再误判幻觉。
            try:
                source_numbers |= _kline_derived_whitelist(collected)
            except Exception:
                pass

    if _sv_mode == "legacy":
        snap = collected.get("snapshot", {})
        for v in snap.values():
            if isinstance(v, (int, float)) and v != 0:
                source_numbers.add(round(float(v), 2))

        fund = collected.get("fundamentals", {})
        if isinstance(fund, dict):
            for v in fund.values():
                if isinstance(v, (int, float)) and v != 0:
                    source_numbers.add(round(float(v), 2))

        insider = collected.get("insider_sentiment", {})
        if isinstance(insider, dict):
            for v in insider.values():
                if isinstance(v, (int, float)) and v != 0:
                    source_numbers.add(round(float(v), 2))

        analyst = collected.get("analyst_consensus", {})
        if isinstance(analyst, dict):
            for v in analyst.values():
                if isinstance(v, (int, float)) and v != 0:
                    source_numbers.add(round(float(v), 2))

    if not source_numbers:
        return None

    # 从LLM输出提取数字
    verified = []
    unverified = []
    flagged_claims = []

    for section in ["bull_case", "bear_case", "constraints"]:
        for item in result.get(section, []):
            thesis = item.get("thesis", "") + " " + item.get("evidence", "")
            # 提取所有数字（包括小数和负数）
            numbers = re.findall(r'-?\d+\.?\d*', thesis)
            for num_str in numbers:
                try:
                    num = round(float(num_str), 2)
                    # 过滤掉非数据数字（如年份、版本号、百分比等）
                    if 1800 < num < 2100:  # 年份
                        continue
                    if num in source_numbers:
                        verified.append(num)
                    elif _is_sourced_derivative(num, source_numbers):
                        # 2026-08-11 (DEB-20260811): 双源均值派生容差 —
                        #   GS 铁证: "$1222.5=星展1200/巴克莱1245均值" 两源数字均在输入
                        #   研报中, LLM 做平均是合理引用而非编造。限两源配对(防任意
                        #   算术自由化), 容差 0.5%。
                        verified.append(num)
                    else:
                        # 检查是否接近某个源数字（四舍五入差异）
                        closest = min(source_numbers, key=lambda x: abs(x - num)) if source_numbers else 0
                        if abs(closest - num) > 0.5:  # 差异大于0.5才算未验证
                            unverified.append(num)
                            if item not in flagged_claims:
                                flagged_claims.append(item.get("thesis", "")[:100])
                except ValueError:
                    pass

    if not verified and not unverified:
        return None

    return {
        "verified_count": len(verified),
        "unverified_count": len(unverified),
        "unverified_numbers": list(set(unverified))[:10],
        "flagged_claims": flagged_claims[:3],
    }


def _fallback_synthesize(collected: Dict[str, Any]) -> Dict[str, Any]:
    """LLM失败时的规则降级（不理想但比空好）"""
    snap = collected.get("snapshot", {})
    return {
        "rating": "观望",
        "confidence": "低",
        "score": 5.0,
        "direction": "SKIP",
        "executive_summary": f"{snap.get('name','')} LLM分析降级，数据已采集但合成失败",
        "bull_case": [],
        "bear_case": [],
        "constraints": [],
        "execution_plan": {},
        "key_levels": {},
        "risks": ["LLM分析不可用"],
    }


def main():
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "US.AAPL"
    # 采集数据
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import data_collector
    print(f"采集 {symbol} 数据...", file=sys.stderr)
    data = data_collector.collect_all(symbol)
    print(f"数据质量: {data.get('_quality', {})}", file=sys.stderr)
    print(f"调LLM合成...", file=sys.stderr)
    result = synthesize(data)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
