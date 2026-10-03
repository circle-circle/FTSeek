# RFC-133: DA prompt 审计修正批 A+B (时点错配 + 死规则删除)

- 日期: 2026-10-03
- 类型: L1 (prompt 文本修正, skill 仓 llm_synth.py + 版本棘轮三处)
- 用户裁决: 「如果无误 请执行」(2026-10-03, 基于本轮 DA/UE prompt 静态审计报告)
- PROMPT_HASH: `c66a9fcacc37` → `43d14dddcad1` (归因基线切分点)
- DA skill version: 0.90.1 → 0.91
- 回滚锚: `llm_synth.py.bak-rfc133-20261003` + git revert (skill 仓 /root)

## 背景

DSPy/prompt 自动优化轴经 IC 实测 KILLED (挑战者五维尺子 vs 前向收益 rho=+0.115, 不显著且反向;
见 signal-ic-analysis skill references/challenger-ruler-dspy-verdict-20261003.md)。
转入静态审计: 逐字读 DA 生产 prompt (分析师 SYSTEM_PROMPT + 输出契约 + 挑战者 v2) 与 UE V2 (v2.14),
对照 15 交易日 305 份 da_checkpoint 实测。UE v2.14 于 10/2 刚切版, 本批不动 UE。

## 变更 A: HIGH 判据②删"量比<1.5"臂 (时点错配)

**病灶**: 判据②原文要求"核心信息未被价格反映(盘前同向异动|涨跌幅|<2% 或 量比<1.5)"。
但量比/换手率在盘前时段为**上一交易日收盘口径** — 同一 prompt 数据段自证
(`_t1_note`, 2026-09-14 盘前数据有效性审计所加: "盘前量比实为昨日量能, 不是今日放量")。
用昨日量能判"今日信息是否已被反映"= 结构性自相矛盾。

**实测暴露面** (2026-09-14→10-02, 305 份 checkpoint):

| 项 | 读数 |
| --- | --- |
| HIGH 档总数 | 149 |
| 其中 \|盘前涨跌\|≥2% (判据②第一臂不满足) | 20 (13%) |
| 其中仅靠昨日量比<1.5 通过第二臂 | 17 |
| 该组 executed 子集 fwd1 均值 | **−0.886%** (全体 executed +0.480%) |

**修法** (最小文本修正, 不新增维度):
- 判据②第二臂改为"盘前成交量未见放量"(⚡盘前时段数据段的 pre_volume, 当日真值);
- 显式标注"量比/换手率是上一交易日口径, 不得用于本判据";
- 【校准原则】edge 判定同步: 判断材料改为"盘前涨跌幅/盘前成交量"并加同口径警告。

**行为影响**: 约 11% 的 HIGH 将降 MEDIUM (score 8→6), gate tier 可能"优"→"良" (仓位 normal→small)。
方向性证据支持 (n=19, −0.89% vs +0.48%), 未达统计显著 — 正当性主要基于 prompt 内部自洽 (时点错配是确定性缺陷)。

## 变更 B: 删"同标的3天内连续推荐须新证据"死规则

**三证**:
1. **数据供给缺失**: 注入的"近期交易经验"只含 obsidian 上次分析 (1 条, 无后续价格走势);
   "后续价格未确认方向"这半个判据从未有数据供给 → 规则不可执行;
2. **实测零 binding**: 245 条 executed 推荐中 133 条 (54%) 为 3 天内同方向再推;
3. **方向与既有裁决相反**: 2026-08-04 回测实证重复推荐标的是盈利主力
   (MRK/NVDA/BABA/LLY 全正 PnL), "去重=砍盈利来源" (llm-decision-input-audit skill 在册)。

删除【校准原则】该条 + 【交易经验参考】③项引用 ("三类信息"改"两类")。**零行为变更** —
规则现在就没有被执行, 删除只是让 prompt 诚实。

## 预注册判据 (切版后 15 交易日, 与既有窗口纪律一致)

1. **A 生效**: HIGH 档中 |盘前涨跌|≥2% 的占比从 13% → <3%;
2. **无害**: HIGH 档 executed 组 T+5 均值不低于切版前基线 +0.81% 超过 0.3pp。

任一不过 → 回滚 .bak 锚 + 版本棘轮三处回退。
评估脚本复用本轮审计配方 (da_checkpoint → _llm_output.score_band + snapshot.pre_change;
signal_snapshots join fwd_ret_t5, join 键 _ic_decision_id ↔ decision_id)。

## 同批登记不动项 (审计发现, 零改动)

| 项 | 发现 | 不动理由 |
| --- | --- | --- |
| rating 六档死字段 | gate/merge/adapter 均不读, 仅展示层 | 删除需动 obsidian/brief 渲染链, 收益仅几十 token/次 |
| claim_ic 多空混装 | product_catalyst bull α=+0.05% vs bear −0.27% pooled 抵消; gate used_claims bull+bear 混采 (LONG 列 valuation_expensive 风险反被扣分, VKTX/AKAM 实锤) | 扣分落 quality_score, 不参与 tier 分级 (死门⑧已裁); 接线时再按 side 拆分 |
| challenger 防锚定仪式化 | independent_direction ≡ my_direction 99.3% (305/307) | 纯观测字段; 不再引用 anchor_divergence 当独立性证据 |
| score_band+gate 联动 | **正向发现**: T+5 单调 (HIGH +0.81%/MEDIUM +0.23%/LOW −11.76%), LOW executed 7 条全被 gate BLOCK = 拦住的恰是最差组 | band 机制在工作, 勿动 |

## 验证记录 (Gate 5)

- `python3 -m py_compile llm_synth.py` ✅
- FTSeek `tests/test_da_output_contract.py`: **56 passed** (改动前后等基)
- skill `tests/`: **18 passed**
- 白盒冒烟: 旧文本零残留 ('量比<1.5'/'过去3天'/'这次有何不同' 全 grep 零) + 新文本在位 + build_user_prompt 正常 + 新 hash `43d14dddcad1` ✅
- 版本棘轮三处同步: SKILL.md `version: 0.91` / CHANGELOG v0.91 条目 / test 断言 "0.91" ✅

## 部署 (Gate 6)

- DA cron (e0d574cc1a3c, 0 20 * * 1-5) 每次新进程加载 skill 脚本 → **无需重启 ftseek-us 服务**
  (grep 确认服务进程零 import llm_synth; shadow_registry 仅描述文本引用)
- 前置核查: open_positions 0 行 (sim 无未平仓), trade_records exit_time NULL 0 条
- 生效点: 下一个 cron 窗口 (ET 08:00 盘前) 自动使用新 prompt
