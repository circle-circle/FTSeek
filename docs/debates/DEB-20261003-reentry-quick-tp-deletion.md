# DEB-20261003 · 再入场 quick_tp 参数删除提案

> 状态: **PROPOSED — 未执行**。本文件只把 `reentry_quick_tp_gate` 窗满后的
> L3 处置写成可裁决提案; 不改代码/config/测试, 不重启服务。
> 证据链: `docs/debates/DEB-20260831-reentry-quick-tp-gate-conflict.md` §四、
> `docs/decisions/REVIEW-20261003-shadow-cleanup.md` §A/§五、
> `app/playbook/shadow_registry.py` id=`reentry_quick_tp_gate`。

---

## 一、结论提案

**删除 quick_tp 三参数及其计算路径**:
`quick_tp_atr_mult`、`quick_tp_floor_pct`、`quick_tp_ceil_pct`。

保留:
- `quick_sl_*` — 宽止损仍是现行再入场行为, 不属于本案死参数;
- `tp_rr` fallback — ATR 缺失路径的既有回退;
- `reentry_quick_tp_shadow.jsonl` 13 行历史数据与注册表 KILLED 留痕。

不提案:
- 不改 `min_tp_rr=1.5` 门禁;
- 不恢复「按 decision_id 豁免快 TP」;
- 不删除 quick_sl 或再入场生成器。

## 二、证据与预注册落点

| 证据 | 读数 | 含义 |
|---|---|---|
| 影子窗 | 2026-09-01→09-30 已闭, 结算 13 行 reentry 实弹 | 窗满, 不再顺延 |
| TP 触达 | `tp_touched=0/13` | 预注册 B13 的触达结算轴 n=0, 落「否则」分支 |
| 出场路径 | 13 笔 0 笔 `TAKE_PROFIT` | 快 TP 未通过 `min_tp_rr=1.5` 门禁 |
| 几何 | quick TP ≈ 0.35×ATR, quick SL ≈ 0.6×ATR ⇒ RR≈0.58 | 在现行门禁下结构性被吞 |
| 处置原文 | B13「否则」= 维持门禁 + 提案废 quick_tp | 本案是该预注册处置的落地提案 |

诚实边界: `0/13` **不是快 TP 绩效为负的证明**; 它只说明预注册的恢复条件未成立。
删除的核心理由是「参数已无出口、且会诱导未来误调 `min_tp_rr` 复活旧冲突」。
监控轮 20s 粒度可能低估触达, 但预注册窗口已闭, 不允许事后改判据重采。

## 三、建议变更集 (待用户裁后执行)

1. `config/unified_config.yaml`: 删除 `playbook.execution_parameters` 下
   3 个 `quick_tp_*` 键, 留墓碑注释指向本 DEB。
2. `app/playbook/intraday_reentry.py`: `_DEFAULTS` 删除同名 3 键;
   `quick_sl_tp_pct` 不再计算 quick TP, ATR 有效时返回 `(quick_sl, None)` 或
   等价拆成 `quick_sl_pct`; 调用方统一走现行 `sl_pct × tp_rr` fallback。
3. 测试: 删除 quick TP 缩放/上下限断言; 新增守卫
   「ATR 有效时新 fallback TP 与 quick SL 的 RR 仍 < 1.5, 会被门禁吞掉」,
   以及「ATR 缺失路径输出不变」。
4. 影子代码清理: 可同批删除 `app/playbook/reentry_tp_shadow.py` 与
   `exit_engine.py` / `pipeline.py` 两处 try 调用; 保留 JSONL 与注册表终态。

## 四、风险与为什么必须 L3

- **不是全链零行为变更**: exit 面继续吞 TP, 但未来指令的 `take_profit`
  元数据会从 quick TP 值变为 fallback 值; admission/planned-executability 等
  读指令字段的模块须在实施前逐点审计。
- **代码删除需重启** `ftseek-us` 才生效; 当前 app/ 有其他 agent 在途 WIP,
  不得顺带上线。
- **删除即锁死回滚路径**: 若未来想恢复快 TP, 须新立 DEB + 用户裁 + 影子先行,
  不允许只调 `min_tp_rr` 复活。

## 五、验证门

实施时至少跑:
- `PYTHONPATH=. ./venv/bin/python -m pytest tests/test_intraday_reentry.py
  tests/test_reentry_quick_tp_shadow.py tests/test_shadow_artifact_consumers.py
  tests/test_shadow_board_guard.py -q`
- 全仓 grep: `quick_tp_(atr_mult|floor_pct|ceil_pct)` 仅允许出现在
  本 DEB、REVIEW、注册表 verdict_history 和历史 DEB, 不允许活在现役 config/code/tests。
- 若删除影子模块, `test_ops_scripts_sync` / `test_control_loop_ledger` 一并纳入。

## 六、回滚

git revert 对应删除提交; 历史数据/注册表不因代码回滚而清空。若用户裁
「保留参数」, 则仅将本 DEB 状态改为 REJECTED, 并保留 KILLED 影子与 false 开关。
