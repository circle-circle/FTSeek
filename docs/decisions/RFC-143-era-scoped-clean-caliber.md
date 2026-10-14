# RFC-143: 现役时代口径 SSOT + 报表过滤接线 (A10)

- 日期: 2026-10-14
- 类型: L1 (报表/口径层, **零交易行为变更**)
- 状态: 已实施 (用户 2026-10-14 裁「同意 后续统计和数据筛选过滤旧数据」)
- 判据登记: `docs/ADJUDICATED-CRITERIA.md` **A10**; 回归钉子 `tests/test_era_caliber.py`

## 1. 背景: 头条净益符号由旧时代人工单决定

全库 991 笔混合两个资金量级完全不同的时代:

| 口径 | n | 净 | 单笔名义 | 备注 |
|---|---|---|---|---|
| 全库 (报表旧状) | 991 | **+$12,526** | — | ORCL 4/24 人工单 +$17,843 = 头条的 **142%** |
| 旧时代 (4–6月, 1M 资金, MAIN_PIPELINE/SCALPING) | 351 | +$13,997 | $17k~$1.14M | 非现役系统, 含人工/legacy 单 |
| **现役时代 (7/1 起, ~10万资金, playbook)** | **638** | **−$1,455** | p50 $5.5k, 零笔 >$15k | 当前自动化系统 |

现役时代干净口径 (trade_date≥2026-07-01 ∧ data_quality∈{clean,real} ∧ 剔 POSITION_SYNC/MANUAL) 实测:

- 毛 **+$2,183 (+7.14bps)** / 费 **$3,638 (11.90bps)** / 净 **−$1,455 (−4.76bps)** ⇒ 毛 edge 为正, 卡在费用线下 ~5bps
- WR 59.9% / R:R 0.60 / 持仓中位 1.0h / 日聚类 t=−0.64 (与 0 不可分辨)
- LONG n=517 −$1,544 (费/毛 206%, t=−0.73) / SHORT n=121 **+$90** (费/毛 88%, t=+0.14, 剔 top3 赢家→−$512 脆)
- 逐月演进: 7月 WR 42.3%/净−795 → 8月 54.6%/−242 → 9月 76.8%/−509 → **10月 77.9%/+$91 (首次转正)**; R:R 同期 1.04→0.30 (胜率换赔率形态)
- MFE max 7.67%, ≥15% 零笔 ⇒ 低胜率高赔率类外部策略 (Martin Luk 型) 右尾阻断在现役口径下复验仍成立

**连带撤回**: 「SHORT 腿已证伪」系混时代口径伪影 (旧时代两笔异常单均为 SHORT; 现役口径 SHORT≈0 且费/毛比优于 LONG)。方向配置结论须按现役口径重读。

**治理注记**: DEB-20260823 几何变更搁置前提措辞为「毛期望为零」; 现役口径毛期望为 **+7.14bps (正, 但低于费用线)**。搁置裁决本身不动 (净仍≈0, t=−0.64), 但引用该前提时须按本行留痕更新措辞。

## 2. 变更

1. **SSOT** `app/core/db_utils.py`: `CURRENT_ERA_START="2026-07-01"` + `ERA_CLEAN_PERFORMANCE_WHERE` 常量 + `era_clean_where(conn)` 探测函数 (缺列 fail-open 返回空串, 与 A9 `a9_clean_where` 同法同取舍)。边界与 `scripts/long_expectancy_ledger.py` `ERA_START` (DEB-20260816 §九9.4) 一致, 测试钉住两处不漂移。
2. **接线** (表现类读数, 全部只读脚本):
   - `scripts/entry_window_cohort.py`: 队列 SQL 拼 era 谓词 + 输出口径标注行;
   - `scripts/shadow_board.py` 段9 exec_gate 结局回填 join: 拼 era + A9 谓词 + 段头口径标注;
   - `scripts/rth_fade_graduation_check.py` `channel_readiness()`: **修大小写 bug** — DB 混存 `EXTERNAL_CLOSE`/`external_close` 两态, SQLite `IN` 大小写敏感, 旧写法只剔小写 (ORCL 4/24 人工单即大写态, 此前靠 entry_time≥8/25 窗口挡住, 窗口一放宽即漏) → `UPPER(exit_reason)` (NULL 语义不变, 未平仓行仍排除); 并拼 era 谓词防窗口放宽后旧时代倒灌。实测该查询 SHORT-only, 而 A9/duplicate 批全为 LONG ⇒ 无污染, 不扩改动。
3. **判据落档**: ADJUDICATED-CRITERIA.md 新增 **A10** (表现类必用 SSOT 谓词; 账户级资金对账与费用/成本标定豁免, 豁免须注释留痕)。
4. **回归**: `tests/test_era_caliber.py` (边界钉死 + 过滤语义 + NULL dq 剔除 + 缺列 fail-open + 消费方接线 + rth_fade 小写字面量禁回潮)。

## 3. 明确不改 (刻意豁免, 非遗漏)

- `daily_alert_summary.py`: 账户级日盈亏对账, 含人工单是对的 (A10 豁免条款);
- `fee_reconcile_report.py`: 已按 `source_type='PLAYBOOK_SIM'` 过滤, 天然现役;
- 滚动窗口类报表 (weekly_trade_summary / post_market_audit 等): 窗口天然在现役时代内且已接 A9; era 谓词接线随后续改动顺带做, 本批不扩散 (最小变更);
- `long_expectancy_ledger.py`: 已有同值 ERA_START inline (预注册判据载体, 不动其字面量; 测试钉两处一致防漂移);
- 交易链路/判据/参数/config: **零改动**。

## 4. 验证 (Gate 5)

- `py_compile` 全部改动文件;
- `tests/test_era_caliber.py` 全绿; 受影响既有测试 (weekly_trade_summary / shadow_board_guard / except_pass_ratchet / production_data_write_isolation / rth_fade_borrow_fee) 无新增失败;
- 三脚本真跑: entry_window_cohort (--days 10 与 --days 200 各一次, 后者验证旧时代不倒灌) / shadow_board (无 --push) / rth_fade_graduation_check, 输出带口径标注。

## 5. 回滚

revert 本提交即可 (报表层, 无状态迁移, 无 cron 变更, 无需重启服务 — 三脚本均为 cron/手动独立进程, 下次运行自动加载)。A10 行按判据字典规则改状态不删行。
