# EXEC-20261003 · quick_tp 删除执行记录 (用户裁「按建议执行」)

> **状态**: ✅ 已执行 (2026-10-03)。本文件是 `DEB-20261003-reentry-quick-tp-deletion.md`
> (Codex 提案, 原文未动) 的裁决与执行更正记录。**DEB 状态行「PROPOSED — 未执行」已过期,
> 以本文件为准。**
> **裁决链**: 用户 2026-10-03 裁「删除」→ 执行前复核发现 DEB §三.2/§四/§五 存在 P0 前提
> 错误 (见下) → 用户对三岔路裁「按建议执行」= 采纳行为中性方案。

## 一、执行前复核发现的 DEB 前提错误 (P0)

DEB §四 声称「删除后 exit 面**继续吞 TP**」、§五 提议守卫「断言 fallback TP 的 RR **仍
<1.5** 会被门禁吞」。线上 config 实值 (运行进程口径, 非代码默认):

| 量 | 实值 | 后果 |
|---|---|---|
| `playbook.min_tp_rr` (门禁) | **1.5** | — |
| `playbook.intraday_reentry.tp_rr` (fallback) | **1.6** | RR 1.6 **≥** 门 1.5 |

⇒ 按 DEB §三.2 字面实现 (「调用方统一走 sl_pct×tp_rr fallback」), ATR 有效路径的 TP
将**通过门禁** — 激活一条 2 个月从未触发过的固定 TP 出场腿 = 未验证实盘行为变更,
与 DEB 自己「零行为变更/继续吞 TP」定性直接矛盾; §五 守卫按其自身实现不可满足
(必 FAIL)。根因: 提案未核线上 config 实值 (纪律「口径核线上config实值勿用代码默认」)。

## 二、已执行的变更集 (行为中性版)

**原则**: quick_tp 确属死参数 (RR≈0.5-0.58 恒被吞, 13 笔实弹 0 笔 TAKE_PROFIT),
删除后 ATR 有效路径 **take_profit=0 = 不设固定 TP 腿** — exit_engine 规则1.5 要求
`TP>0` 才进块, TP=0 与被吞后的实际行为**逐字一致** (出场靠 SL/trailing/breakeven/EOD)。

| 文件 | 变更 |
|---|---|
| `app/playbook/intraday_reentry.py` | `quick_sl_tp_pct` → `quick_sl_pct` (仅 SL); `_DEFAULTS` 删三键留墓碑; `_arm_instruction` ATR 有效 ⇒ `take_profit=0.0` (ATR 缺失回退路径**不动**: sl_pct×tp_rr RR1.6≥门1.5, 该腿历史上就是活的); 影子埋点 `tp_pct` 镜像实际武装值 (0.0) |
| `app/playbook/pipeline.py` | re-price 快收块: `_tp_mult=0.0` → 既有假值分支 `_new_tp=0.0`; ⚠️ 注释钉死「不得回退 vol_bucket _tp_mult」(会给 reentry 装活 TP 腿) |
| `config/unified_config.yaml` | 删 `quick_tp_atr_mult/floor_pct/ceil_pct` 三键, 留墓碑注释 (含「勿用 tp_rr 填充」警示) |
| `tests/test_intraday_reentry.py` | quick_sl 6 例改写 + 3 例新守卫: 三键不得回 _DEFAULTS / ATR 有效 ⇒ TP=0 且 SL=quick_sl (双向) / ATR 缺失路径输出不变 |

**未做 (知情偏差, 与 DEB §三.4 的「可同批」项)**: `reentry_tp_shadow.py` 模块与
exit_engine/pipeline 两处 try 调用**保留不删** — ① 该模块测试文件正被并行 agent
(Codex 清理批) 未提交 WIP 占用, 连带删除会销毁他人并行成果; ② 影子已被
`reentry_quick_tp_shadow_enabled=false` 关死 (实测 `_enabled()=False`, note/settle
全早退零写入), 留着零成本。TP=0 后门禁吞块不再进 ⇒ reentry 的 `_tp_rr_gate` 遥测
自然消失 (reentry-only 影响; 非 reentry 指令 TP 不变, 33 笔历史留档不受影响)。

## 三、验证 (全绿)

- DEB §五 验证门: `test_intraday_reentry + test_reentry_quick_tp_shadow +
  test_shadow_artifact_consumers + test_shadow_board_guard` = **90 passed**
- 全仓 grep: `quick_tp_(atr_mult|floor_pct|ceil_pct)` 现役代码/配置面**清零**
  (残留仅墓碑注释 + 守卫测试字面量 + 历史文档, 符合 §五 允许面)
- 端到端行为中性实证 (真签名调 `_check_playbook_exit_rules`): TP=0 且价格 +1.8%
  (越过原 quick_tp 1.75%) ⇒ `exit_reason=""` 无 TAKE_PROFIT, `_tp_rr_gate` 不进;
  旧口径对照 (TP=101.75, RR0.583) ⇒ 同样 `exit_reason=""` (被吞)。**新旧出场行为一致。**
- 历史影响面: 13 笔 reentry 实弹 MFE ≥ fallbackTP(quick_sl×1.6) = **0/13**
  (权威源 ftseek_us.db mfe_pct) ⇒ 即使误用 DEB 原方案, 历史样本也不出场 —
  但那是运气不是设计, 仍按行为中性执行。

## 四、回滚

revert 本执行 commit (代码/config/测试同批)。参数删除即锁死: 恢复快 TP 须新立
DEB + 用户裁 + 影子先行, 不允许只调 `min_tp_rr` 复活旧冲突 (与 DEB §四 一致)。
历史数据 (reentry_quick_tp_shadow.jsonl 13 行) 与注册表 KILLED 留痕不清空。

## 五、生效

代码变更需 `systemctl restart ftseek-us` 生效 (HotReloader 穿不透 playbook.*)。
重启前核查: 未平仓持仓 + 变更归属 (本批文件 = intraday_reentry/pipeline/config
quick_tp 区/测试, 与并行 agent WIP 文件集不重叠; config 用私有 index 只取本批 hunk)。
