"""机器可读判据投影 (RFC-139 L1) — 统一自动验证总线的判据 schema.

设计纪律:
  · **schema 是已冻结预注册判据的机器可读投影, 不是新判据**。SSOT 仍是
    shadow_registry.graduation_criteria (自由文本) + 其出处 RFC/DEB 原文。
  · **改 spec = 改判据 = 重新裁决** (须 DEB + 用户裁; 与 RFC-138 改 fixture 同一纪律)。
  · 未收录 spec 的项, runner 回落「只跑 eval_script 不结构化判分」, 不臆造 spec。
  · 字段设计对齐 riskbound CellSpec 思路: metric / control / min_effect /
    predicted_sign / sample_gates — 但本仓不做统计判分, 只做结构化呈报与机器可查。

消费方 (全部呈报面, 交易链路零接触):
  scripts/auto_adjudicate.py (L2 runner — 读 spec 渲染白话背景与判据门);
  tests/test_criteria_spec.py (schema 完整性钉)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class CriteriaSpec:
    """一条已冻结判据的机器可读投影。

    字段全部从预注册文本机械翻译; `source` 必须指向判据原文出处 (RFC/DEB),
    无出处的 spec 不许入库 (防自造判据)。
    """

    id: str
    # 判据原文出处 (RFC/DEB 编号 + 章节); 改本字段 = 换判据 = 重新裁决
    source: str
    # 主指标是什么 (白话 + 机器键), 如 "配对净差 mean_delta_usd"
    metric: str
    metric_key: Optional[str] = None
    # 对照是什么 (等暴露 / 随机时点 / 同票无事件基线 / 无), 空 = 无对照 (披露)
    control: str = ""
    # 最小效应门槛 (与判据原文同值; None = 原文未设)
    min_effect: Optional[str] = None
    # 预测方向 (+1 有利 / -1 有害 / 0 无方向), 判据原文没写方向则 0
    predicted_sign: int = 0
    # 样本门 (机器可查: 达不到不判, 只续观)
    sample_gates: Dict[str, Any] = field(default_factory=dict)
    # 结算脚本 (与注册表 eval_script 同值; 采集器不是结算器, 应在 runner 分类暴露)
    eval_script: Optional[str] = None
    # 产物 verdict json 路径 (相对 repo root; None = 无产物)
    verdict_artifact: Optional[str] = None
    # 已知缺口 (如实披露, 不遮)
    known_gap: str = ""


# 当前待裁面的 spec (RFC-139 首批 2 项; 77 项全量收录属后续批次, 防一次改大)。
# 逐字段可溯源: source 指向判据原文, metric/control/min_effect 从原文机械翻译。
SPECS: Dict[str, CriteriaSpec] = {
    "entry_band_widen": CriteriaSpec(
        id="entry_band_widen",
        source="RFC-065 §13/§29.3 + shadow_registry.graduation_criteria (2026-10-07 DEB-20261007 方案B 修回原文)",
        metric="配对净差 δ = widen_net − flat_net (美元/事件)",
        metric_key="mean",
        control="同事件 flat 带真实结果 (changed→pnl_net / new→0) vs widen 带反事实重放",
        min_effect="mean δ > 0 且 bootstrap CI95 下界 > 0 (双条件) + 符号检验单侧 p<0.025",
        predicted_sign=+1,
        sample_gates={"n_settled_min": 30, "note": "widen 专属事件已结算 n≥30 (RFC-065 原文)"},
        eval_script="scripts/settle_entry_band_widen.py",
        verdict_artifact="data/reports/entry_band_widen_verdict_latest.json",
        known_gap="",
    ),
    "eth_relative_volume_gate": CriteriaSpec(
        id="eth_relative_volume_gate",
        source="docs/analysis/2026-09-24-eth-microstructure-shadow-preregistrations.md §⑤ (读数前写死)",
        metric="反事实净额 cf_net_usd/笔 中位数 + 胜率 + 剔除最强单日复算",
        metric_key="cf_net_usd",
        control="绝对量闸维持现状 (无对照臂; 上界口径: 反事实无排队/冲击建模)",
        min_effect="①中位数>0 ②胜率≥55% ③剔最强单日仍>0 (三条全满足才立项)",
        predicted_sign=+1,
        sample_gates={"trading_days_min": 15, "would_pass_n_min": 30, "deadline": "2026-11-30"},
        eval_script="scripts/eth_relative_volume_gate_shadow.py",
        verdict_artifact=None,
        known_gap="eval_script 是采集器不是结算器 — 无 verdict 产物; 达门后须先补结算脚本再呈裁 (NO_SETTLER)。"
                  "⛔2026-10-23 已退役 (RFC-162): 上游 RFC-104 计划层 10-15 关停 ⇒ 真轴 "
                  "would_pass_relative=True 恒为 0/30 (数学不可达); 采集 cron 已注释, 数据留档。"
                  "**本 spec 仅作历史判据留档, 不得据它提请转正**; 重开须复供计划层 + 新预注册。",
    ),
}


def get_spec(item_id: str) -> Optional[CriteriaSpec]:
    """按注册表 id 取 spec; 未收录返回 None (回落 eval_script-only, 不臆造)。"""
    return SPECS.get(item_id)


def spec_as_dict(item_id: str) -> Optional[Dict[str, Any]]:
    s = get_spec(item_id)
    if s is None:
        return None
    d = dict(s.__dict__)
    return d
