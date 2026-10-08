# 议题：DA 模型侧 77% 多头基本盘 —— 偏向分解、成因与可选对策（F6 另案）

## 状态

- [x] 进行中（首轮分析完成，对策待裁）
- [ ] 已决策

**状态头**

| 项 | 值 |
|---|---|
| 类型 | 分析提案（零交易行为变更，纯测量/文档） |
| 立案日 | 2026-10-08（承接 DEB-20261008-direction-gates F6 转介，用户裁「F6 进一步分析」） |
| 提出者 | Claude（复查批） |
| 待谁裁决 | 用户（§5 对策四选一或组合） |
| 数据窗 | 2026-09-01 ~ 2026-10-07 全部 DA checkpoint（n=364 有效方向） |

---

## 1. 一句话结论

多头偏向**不是市场 regime 造成的**（SPY 下跌日多头占比仍 75%），而是**模型对「空头排列」的处理不对称**：scanner 判 bearish 时，LLM 仅 21% 顺势做空，却 62% 逆势抄底（BUY）——**模型把空头信号当抄底机会，而非做空信号**。挑战者（证伪官）不仅未纠偏，方向分布比分析师更多头（80% BUY）。

## 2. 分解漏斗（闸前，与 DEB-20261008 L1 口径一致）

```
scanner_direction (技术面):  bullish 113 / bearish 68   ⇒ 62% 偏多
       │
       ▼  LLM 深分析
_llm_output.direction:        BUY 224 / SHORT 68 / SKIP 72 ⇒ 方向性输出 77% 多头
```

scanner→LLM 交叉（关键证据）：

| scanner | →BUY | →SELL_SHORT | →SKIP | 顺势率 |
|---|---|---|---|---|
| bullish (n=113) | 67 (59%) | 26 (23%) | 19 | 顺势做多 59% |
| **bearish (n=68)** | **42 (62%)** | **14 (21%)** | 12 | **顺势做空仅 21%** |

**不对称核心**：看多信号 59% 被跟随，看空信号只有 21% 被跟随、62% 被反手抄底。
多空处理强度相差 **3:1**。

挑战者侧（n=292 有方向判定）：BUY 183 / SKIP 135 / SELL_SHORT 46 ⇒ 方向性 80% 多头，
与分析师一致率 77% ⇒ 证伪官机制对方向偏向**无纠正作用**（它证伪论据，不证伪方向先验）。

## 3. Regime 控制（排除「市场本来就在涨」假设）

按 SPY 当日开收符号分组：

| SPY 日 | BUY | SELL_SHORT | 多头占比 |
|---|---|---|---|
| 下跌日 | 125 | 41 | **75%** |
| 上涨日 | 99 | 27 | **79%** |

偏向在下跌日**纹丝不动** ⇒ regime 解释不成立，偏向是结构性的（prompt/模型层）。

## 4. 成因分析（待逐条验证的候选假设）

| # | 假设 | 证据现状 |
|---|---|---|
| H1 | **自我定位偏置**：signal_gate.py:426 注释自述「DA 定位优质多头选股器，做空机会交由 UE 不设限浮现负责」——该定位是否也写进了 prompt（prompt 层直接告诉模型"你是多头选股器"） | 待查 prompt 原文（skill 仓 SYSTEM_PROMPT/USER 模板） |
| H2 | **prompt 多空义务不对称**：v0.66 曾修「SHORT 三问 + BUY 镜像义务」，但bearish→BUY 62% 说明镜像义务在「下跌趋势」语境下被解读为「找抄底点」而非「对称评估」 | 待查 v0.66 后 prompt 现行文本 |
| H3 | **训练先验**：美股长期上行 + 做空文本稀疏 ⇒ 基础模型先验偏多，无 prompt 对冲时自然 77% | 不可直接改，只能靠 prompt/流程对冲 |
| H4 | **UE 接应失败放大器**：G1 设计假设「空头交给 UE」，实测 UE 日均空头 0.4 条（DEB §4.6）⇒ DA 放弃空头 + UE 接不住 = 系统性多头 | 已实证（DEB-20261008 §4.6） |

## 5. 可选对策（待用户裁决，均须影子先行）

| 方案 | 内容 | 代价/风险 |
|---|---|---|
| **A. prompt 对称化** | bearish 排列时 prompt 强制「先写做空 thesis 再写抄底 thesis，二选一须证伪另一方」；自检清单加「我是否在抄底」 | 改 prompt = PROMPT_HASH 断点，重开 A/B 对照期；可能降低 BUY 质量（抄底恰是其 alpha 来源？待验证，见 §6） |
| **B. 空头专线** | bearish scanner 标的走单独的空头分析 prompt（角色=空头研究员），产出再与主链合并 | 双 prompt 维护成本；token 成本上升 |
| **C. 接受偏向 + 下游补偿** | 承认 DA 是多头选股器，空头供给明确交给 UE/其他通道并给 UE 空头侧配额/扩源 | 需先解决 UE 空头产量（0.4 条/日）——与 H4 联动 |
| **D. 只观测不动** | 把 bearish→BUY 的 62% 当特征而非缺陷——如果抄底臂 IC 为正，偏向即合理 | 须先完成 §6 验证 |

## 6. 关键待验证（决定 A/B/C/D 的先决问题）

**bearish→BUY 抄底臂的实战成绩如何？** 若抄底臂 fwd_return 显著为正 ⇒ 偏向是 alpha 特征
（选 D）；若显著为负 ⇒ 偏向是真病灶（选 A/B）。此验证可直接用 F4 回填后的 fwd_return
+ checkpoint 方向标签复算（下一个分析批执行，不在本批）。

## 7. 复跑命令

```bash
# §2 分解漏斗 + scanner→LLM 交叉
venv/bin/python - <<'PY'
import json, glob, collections
scan=collections.Counter(); llm=collections.Counter(); flip=collections.Counter()
for d in sorted(glob.glob('data/da_checkpoint/2026-09-*')+glob.glob('data/da_checkpoint/2026-10-*')):
    for p in glob.glob(d+'/*.json'):
        if p.endswith('.raw.json'): continue
        try:
            doc=json.load(open(p)); out=doc.get('_llm_output') or {}
            sd=str(doc.get('_scanner_direction') or ''); ld=str(out.get('direction') or '')
            if ld: llm[ld]+=1
            if sd: scan[sd]+=1
            if ld and sd: flip[(sd,ld)]+=1
        except Exception: pass
print('scanner:', dict(scan)); print('llm:', dict(llm)); print('交叉:', dict(flip))
PY
```

（§3 regime 控制复跑脚本同手法，SPY 日线走 MarketDataRouter；本批命令行已在会话内实证。）

## 8. 自曝 / 边界

- §3 的 regime 分组用 SPY 当日开收符号（粗糙代理），未控制趋势级 regime（MA200 上/下）；
  更严的控制在下一个分析批。
- checkpoint 覆盖 9/1 起 = 偏好测量窗与 9-10 月震荡市重叠，跨年牛市窗未测（H3 无法排除）。
- 挑战者 80% 多头分布受分析师方向锚定影响（77% 一致率），不能读作独立多头倾向。
