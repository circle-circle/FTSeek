# DEB-20261128 · 全链路审计:守卫卫生批(except_pass 扫描面+清剿 / W6 补栈 / F 分层修尺子 / W2 两字段登记 / 台账幽灵 job 对账)

- **日期**:2026-11-28(周六 CST;美股周五 11-27 半日市已收,下一交易日周一 11-30)
- **触发**:用户指令「请核查分析当前系统中全链路问题,进行合理优化和完善,确保整个全链路交易关联服务和模块、功能最优合理」
- **性质**:纯正确性 / 日志卫生 / 守卫修尺子批。**零控制流变更、零交易行为变更、零判据阈值/方向变更**(F 分层与 W2 登记属「修尺子/登记已裁决事实」,先例见各节)。
- **先例**:RFC-197 / DEB-20260910-logging(日志卫生就地清剿)、2026-10-28 扫描面收敛(用户裁「按建议执行」)、2026-11-14 da_sample 分层(用户裁)、1e96c03c945e(台账已销 job 留痕标记)。
- **HEAD(实施前)**:`54fd01132`;服务 `ftseek-us` active;重启后日志 0 ERROR/CRITICAL。

---

## §0 审计总览(违反全景 → 处置分流)

`data/invariant_check_latest.json`(checked_at 2026-11-27T16:05 ET,窗 since 2026-11-20,sample 15):

| 违反 | 计数 | 处置 | 依据节 |
|---|---|---|---|
| F_field_dropout(slippage_bps 7/15) | 1 | **修尺子**(分母分层,收养行单列) | §4 |
| W2_new_field_degen(confidence_semantic / borrow_data_basis) | 2 | **登记基线**(已裁决事实,不改 prompt/模型/代码) | §5/§6 |
| W6_except_stack_grew ×4 + new_file ×1 | 5 | **补 exc_info=True**(10 点位) | §3 |
| nightly_ratchet_guard: except_pass 899→1817 | 1F | **扫描面收敛 + 清剿 13 处新增** | §2 |
| nightly_ratchet_guard: W456 real_repo | 1F | 同 §3(补栈后零违反) | §3 |
| test_control_loop_ledger: 幽灵 job 68cc76b16ebd | 1F | **台账留痕标记**(job 已销,六面零活体) | §7 |
| nightly_ratchet_guard: pipeline_size 3793>3700 | 1F | **只报不修**(拆分债,另批) | §8 |
| W7_chg_denominator_divergence | 40 | **只报不修**(存量基线登记,分母修复另批) | §8 |
| I4_mfe_recalc | 6 | **只报不修**(收养行 MFE 近似值,同 §4 病灶族) | §8 |
| W1_kr2 / W1_oct cohort BROKEN | 2 | **只报不修**(判据 BROKEN 不得自愈,须开跑会级裁决) | §8 |
| test_control_loop_ledger: crontab us_adr_context_collector 未入账 | 1F | **不修**(他人未提交 WIP,不代入账) | §8 |

审计方法:代码+数据双证。所有根因均经实码定位与只读 DB/产物复核;本批不新开影子、不新开推送、不动四锁 sim、不动 C8–C18 红线、主管线冻结新仓与 scan_loop 运行态不变。

## §1 病灶族

本批五件事同属一族:**测量/证据链的静默失真**——
1. 异常被吞(except:pass)= 证据静默消失(R5/E13 教训);
2. 有告警无栈(W6)= 值班人猜根因;
3. 尺子分母被结构性不可算行污染(F)= 把「不可算」误报成「断线」,真断线反而被狼来了掩埋;
4. 已裁决的真值修复效果未登记(W2)= 守卫长期常驻红,掩埋真缺口;
5. 台账记载与活体调度脱钩(幽灵 job)= 审计面失真。

共同纪律:**修尺子 ≠ 放宽判据**。F 分层只改 slippage_bps 一维的分母口径(收养行单列不抹账);W2 登记只豁免「已证实为预期效果」的两字段,阈值与其余字段判定不动;except_pass/W6 基线只降不升。

## §2 except_pass 棘轮:扫描面 bug + 13 处真实新增清剿

### §2.1 扫描面 bug(1817 的 907 是假红)

`tests/test_except_pass_ratchet.py::_count_except_pass` 用 `REPO.rglob("*.py")` 全仓扫描,排除项 `_EXCLUDE_PARTS` 无 `.qoder`。`.qoder/worktrees/trade-recovery-20261121/` 是**他人 agent 的 worktree 副本**(83M,非生产代码,无 import 引用、无调度载体),贡献 **907** 处计数。

实证(2026-11-28 本机实测):
- 不排除:1817;排除 `".qoder"`:**910**;排除 `"qoder"`(无点):1817(不生效——path.parts 是 `.qoder`)。
- 判据对齐:W4/W6 的 `_HYGIENE_ROOTS = (app, scripts, ops, config) + main.py` 从来不含 `.qoder`;2026-10-28 已有「扫描面收敛到生产可见面」先例(排 docs,用户裁「按建议执行」)。
- **修法**:`_EXCLUDE_PARTS` 增 `".qoder"`(带点),注释写明边界声明:`.qoder/` 下不得放生产逻辑,否则逃出本棘轮(同 docs 先例口径)。

### §2.2 真实新增 13 处(对账基线 commit `61acb818d`)

同口径(排 `.qoder`)逐文件 diff:**+13 新增 −2 他批已愈合 = 净 +11**(899→910)。
- 他批愈合(已提交,非本批功劳):position_sync 20→19(−1)、playbook_service 旧 L269 消失(−1)。

13 处新增点位(文件:行 / 语义 / 修法,全部零控制流变更):

| # | 点位 | 语义 | 修法 |
|---|---|---|---|
| 1 | app/data/futu_lv3_provider.py:1604 | 日K磁盘缓存 fetched_at float 失败 | logger.warning(exc_info=True) + continue(原 continue 保留) |
| 2 | app/playbook/late_fill_evidence.py:72 | _tail_rows 坏行 json.loads | logger.warning(exc_info=True) + continue |
| 3 | app/playbook/late_fill_evidence.py:113 | _fill_instant tz 解析失败(后回退 now_et) | logger.warning(exc_info=True)(原 pass→留痕,回退语义不变) |
| 4 | app/playbook/pipeline_parts/auto_execute_flow.py:1183 | no_trade_short cohort merge_instruction_metadata | logger.warning(exc_info=True)(logger 为函数参数在 scope) |
| 5 | app/playbook/playbook_service.py:2006 | MOM 最终复核拒单 skip_reason 回写 | logger.warning(exc_info=True) |
| 6 | app/playbook/playbook_service.py:2150 | reject_sim_submission DEFINITIVE_REJECT | logger.warning(exc_info=True) |
| 7 | app/playbook/playbook_service.py:2155 | unlock_execution_if_zero_fill_terminal | logger.warning(exc_info=True) |
| 8 | app/playbook/shadow_registry.py:5352 | 目录型坏 JSON(_j.loads) | _log.warning(exc_info=True) + continue |
| 9 | app/risk/position_exit_monitor.py:181 | _cal.is_us_holiday 异常降级 | logger.warning(exc_info=True)(保留「按工作日计」降级语义) |
| 10 | scripts/da_entry_quality_trend.py:96 | chase_cap_violation 坏行 | print(..., file=sys.stderr) + continue(需补 import sys) |
| 11 | scripts/extended_hours_entry_executor.py:260 | _atomic_write_json unlink tmp(嵌套在 raise 前) | _log.warning(exc_info=True)(**保留外层 raise**) |
| 12 | scripts/extended_hours_entry_executor.py:4445 | reject_sim_submission DEFINITIVE_REJECT | _log.warning(exc_info=True) |
| 13 | scripts/thesis_management_shadow_report.py:51 | forward 坏行 | print(..., file=sys.stderr) + continue(sys 已有) |

模式纪律(同 RFC-197):app 模块 → `logger.warning(..., exc_info=True)`(一律 warning 不用 debug,W4 把 except 内 debug 判违反);无 logger 脚本 → `print(..., file=sys.stderr)` 带异常类型;continue → 留痕+continue。**新增留痕全部带 exc_info=True ⇒ 不引入新 W6/W4 违反。**

### §2.3 基线对账(只降不升)

899(11-09 基线)+ 13(新增)− 2(他批愈合)− 13(本批清剿)= **897**。BASELINE 899→897,注释留全对账。修后实测须 =897(§9 验证)。

## §3 W6 except-stack:10 点位补 exc_info=True

判据:`scripts/daily_invariant_check.py::_scan_log_hygiene` —— except 块内 warning/warn/error/critical 无 exc_info 计违反;基线 `data/except_stack_baseline.json` per-file counts(**本批不改基线文件**)。

当前 vs 基线(实测 AST 扫描,对账 commit `61acb818d` 逐行 NEW/STOCK 标记):

| 文件 | 基线 | 当前 | 新增点位(NEW) |
|---|---|---|---|
| app/playbook/late_fill_evidence.py | (新文件) | 1 | L78 |
| app/core/position_sync.py | 14 | 16 | L1082, L1107 |
| app/playbook/pipeline_parts/auto_execute_flow.py | 13 | 17 | L250, L265, L472, L557, L1719 |
| app/playbook/pipeline_parts/risk_gates.py | 10 | 11 | L286 |
| app/risk/trail_state_store.py | 9 | 10 | L278 |

注:auto_execute_flow 当前 17 = 基线 13 + 5 新增 − 1 他批愈合(旧点位已不在)。修 5 处后 =12 < 13 ⇒ **healed 报告**(守卫不自改基线,人工收缩另批),不构成违反。其余文件修后恰好回落基线。

修法:10 处告警调用补 `exc_info=True`(多行调用在末参后追加;L250/L265 文本相同,补丁用上下文区分——250 在 EntryDataUnavailable 首取数块、265 在 quote_fallback 块)。**告警文本、级别、控制流逐字不变。**

## §4 F_field_dropout(slippage_bps):分母分层修尺子

### §4.1 根因(当前窗口只读实证,2026-11-28)

窗 trade_date≥2026-11-20 了结 PLAYBOOK 行 15 条:
- **7/7 真实信号行**(entry_reason=playbook_futu_simulate)slippage_bps 全有(2.3 / 0.67 / −12.64 / 3.48 / 0.78 / 0.6 / −0.0);
- **8/8 收养补录行**(entry_reason=sync_recovery_backfill,metadata.adopted=True)**execution 块整体缺失**——收养路径无 arrival 锚,滑点**结构性不可算**,不是写入链断线。

7/15=46.7% < 50% ⇒ 假红。测量链(信号行滑点采集)实际 100% 健康。

### §4.2 先例与修法

先例:`scripts/utils/da_sample.py`(2026-11-14 用户裁「保留 DA + 流程成熟高效」批):死单率/MFE/regime 度量同型分层——收养行 MFE/peak/regime 为收养时近似值,混入虚高死单率(11 月表观 22.8% → 真实信号 15.8%)。**分层 ≠ 抹账**:收养行是真实持仓真实盈亏,单列不丢弃。

修法(`scripts/daily_invariant_check.py` F 块):
- `from scripts.utils.da_sample import is_recovery_artifact`(cron `PYTHONPATH=.` 可达,crontab L150 实证;da_entry_quality_trend 同款 import 已在 cron 运行);
- **仅 slippage_bps 一维**的非空率分母改为真实信号行(`entry_reason ∉ ARTIFACT_ENTRY_REASONS`,sqlite3.Row 经 `_col` 取列包装 dict,缺列按 signal 处理=旧库形态不变);
- field_health 注记 `nn/signal_total (signal; artifact k 行无 arrival 锚, 单列不计)`,收养行数显式披露;
- 告警阈值(跌破 50%、样本≥10)与其余四维(exit_telemetry/entry_price_source/attribution/fees_est)**逐字不动**。

效果:当前窗口 slippage_bps = 7/7(signal)+ artifact 8 单列 ⇒ 假红消除。告警条件不变:`len(_pop)≥10 ∧ 非空率<50%`——阈值作用对象从「混装全体」改为「真实信号行」这一被测量总体本身。当前窗信号行仅 7 行(<10),按既有小样本纪律不判(旧码用混装 15 行凑够样本才误判);未来信号行样本攒到 ≥10 后若滑点真断线(非空率<50%),仍照常告警。**判据方向与阈值逐字未放宽,只是把不可算行移出分母并显式披露;小样本不判是既有纪律,非本批新增豁免。**

## §5 W2 登记①:da_instruction.confidence_semantic(已裁决真值修复的预期效果)

现状(2026-11-28 复跑 shadow_field_cardinality --days 30):n=407,unique=2,top=0.5 @ **92.9%**,entropy 0.371 ⇒ DEGEN。

审计证据链(代码+数据双证,上一阶段完成):
- 断崖精确 **2026-10-23**:逐日 ✅(语义一致)率 10-22 5/15 → 10-23 1/15 → 10-30 0/18;
- 起因 = 仓外 DA skill commit `78a3dca`(10-23 13:00)引入 debate_state.py(`DA_DEBATE_STATE_FIX` 默认 ON):**PARTIAL_AGREE + agreed=true 不再冒充独立一致**——此前 0.7 档大量来自「表面一致」,修复后诚实落 0.5;
- gate 侧交叉验证:10-26 周 AGREE 36 行中 33 行 (SKIP,SKIP)、仅 3 行 (BUY,BUY),与指令级 3✅ 吻合 ⇒ adapter 无第二 bug;
- 挑战者模型全程 deepseek-v4.1-flash 未换 ⇒ 非模型漂移;
- **app/ 零消费方**(2026-11-28 grep 复核:0 命中)⇒ write-only 观测字段,塌缩不影响任何交易判定。

处置:登记 `data/field_cardinality_baseline.json` degen + refs(证据出处)。**不改 prompt/模型/代码**;字段区分度恢复(若未来真一致率回升)由新行带来,届时人工收缩基线。

## §6 W2 登记②:ue_scan.borrow_data_basis(ADR-046 后健康常量)

现状(同日复跑):n=115,unique=1,top=MATERIAL @ **100%**,entropy 0 ⇒ DEGEN。

审计证据链:
- ADR-046(2026-10-14)后 `llm_universe_scan.py::_render_borrow_snapshot`(~L2344)把借券快照渲染进 brief;
- 时间线:10-12/13 全 UNKNOWN → 10-15/16 混合 → **10-20 起 100% MATERIAL 稳定 5+ 周** ⇒ 借券快照持续物化成功,是**健康常量**而非写入方退化;
- 写入方 `app/data/borrow_verifier.py`(无数据 UNKNOWN / 物化 MATERIAL);消费方 `app/playbook/quality_shadow.py`(record-only 影子,无判定消费);UE C18 关闭后仅观察。

处置:登记 degen + refs。**盲点声明**:W2 只判塌缩不判方向——若未来塌回 UNKNOWN(借券供给面断线)W2 不会响,由 UE 空头供给面另守;refs 写明此边界。

## §7 台账幽灵 job 对账:68cc76b16ebd 已销留痕 + fcec2b6482b6 状态更正

### §7.1 68cc76b16ebd(NVDA-RTH-10AM-checkpoint)——六面零活体消费方

job 真相(backup `data/crontab.backup.20261114T230500_push_noise_jobs.json` L2475 实证):一次性 read-only NVDA 检查点(created 2026-11-12 07:08 CST,once 2026-11-12 23:00 CST,repeat.times=1 completed=1,**state=completed**,enabled=false,last_status=ok);现**已不在 jobs.json**;executions.db 残留 **0** 行。

六面引用审计(2026-11-28 实测):

| 面 | 命中 | 性质 |
|---|---|---|
| 仓内代码(*.py/*.sh/*.yaml) | 0 | — |
| 仓内文档 | CONTROL-LOOP-LEDGER.md L374(定义)+ DEB-20261119/RFC-218/DEB-20261128-entry-confirm/handoff(历史提及) | 留痕,非消费 |
| data/ 备份 | crontab.backup.20261114T230500_push_noise_jobs.json | 历史快照 |
| Hermes jobs.json / executions.db | 0 / 0 | 已销 |
| 系统 crontab / /etc/cron.d / /etc/crontab | 0 | — |
| systemd / git hooks | 0 | — |

修法(先例 1e96c03c945e):台账 L374 行加「**留痕不入守卫**」标记(守卫按行标记豁免,test_ledger_job_ids_are_real L67)+ 注明 job 已销与证据出处。**不删行**(历史登记保留),不动任何活体调度。

### §7.2 fcec2b6482b6(gap-rank-attribution-daily)——台账准确性更正

jobs.json 实证:enabled=**false**,state=paused,paused_at 2026-11-13T06:50 CST,last_error="Script exited with code 1"(11-13 incident),repeat.completed=4。**仍在 jobs.json ⇒ 非幽灵**,但台账原写「enabled job 补登记」与事实不符 ⇒ 改为 disabled 留痕登记(写明禁用时间与原因)。

## §8 只报不修(显式留痕,防误读为本批遗漏)

1. **pipeline_size 3793>3700**:pipeline.py 受控拆分债(DEB-20261008 §9.5),本批不触碰 pipeline.py,不修不调基线。
2. **W7_chg_denominator_divergence ×40**:昨收分母口径分叉存量(chg_denominator_baseline.json 已登记),分母修复属行为相邻变更须影子+用户裁,另批。
3. **I4_mfe_recalc ×6**:收养行 MFE 近似值重算差(同 §4 病灶族),修尺子涉及判据口径另批裁。
4. **W1_kr2 / W1_oct BROKEN**:判据 BROKEN 不得自愈,须 DEB 落档 + 开跑会级裁决(已有 DEB-20260909/20260911 在案),本批不动。
5. **crontab us_adr_context_collector.py 未入账**:他人未提交 WIP(scripts/us_adr_context_collector.py + tests + DEB-20261128-us-adr-leg.md 均 untracked),不代入账、不修、不提交。
6. **W6 healed 存量 17 文件**(他批愈合未收缩基线):不属本批,留待日志卫生批统一收缩。

## §9 验证(实施后实测,2026-11-28 17:52–18:01 CST)

测试统一 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python -m pytest … -q -p no:cacheprovider`。

1. **编译/静态**:14 个改动 py 文件 `py_compile` exit0;`scripts/check_undefined_names.py` rc0(`/tmp/fullchain_undefined.log`)。
2. **目标测试**:except_pass(2P)+ w456(16P)+ control_loop(9P/1F)= **27P/1F**(`/tmp/fullchain_target_tests.log`)。唯一红 = `test_crontab_loops_are_ledgered_or_baselined`(us_adr_context_collector 他人未提交 WIP,§8.5 显式不修)。
3. **except_pass 实测**:`_count_except_pass() = 897 == BASELINE 897`。
4. **守卫实跑**:`scripts/daily_invariant_check.py --no-push`(演练路径,零外部推送;产物 `data/invariant_check_latest.json`,改前产物备份 `/tmp/fullchain_invariant_before.json`):
   - violations 只剩 **I4_mfe_recalc×6 / W1_kr2×1 / W1_oct×1 / W7×40**(§8 只报不修面);**F_field_dropout / W2_new_field_degen / W6_except_stack_grew / W6_new_file 全消**;
   - `field_health.slippage_bps = "7/7 (signal; artifact 8 行无 arrival 锚, 单列不计)"`;
   - W6:total=1083 / files=242 / grew=[] / new_files=[] / parse_errors=0;healed 含 `auto_execute_flow.py: 13→12`(**预期 healed**,守卫不自改基线);position_sync / risk_gates / trail_state_store / late_fill_evidence 恰回落基线(无 grew 无 healed);
   - W2:degen 登记面含 `da_instruction.confidence_semantic` / `ue_scan.borrow_data_basis`,均入 silenced_known,status=ok。
   - 注:`data/field_cardinality_baseline.json` 为 gitignored 运行时基线(`.gitignore` `data/*`,与既有 6 条 DEGEN 登记同款约定),不入 commit;登记依据以本 DEB §5/§6 + 文件内 refs 为准,可重建。
5. **变异测试 4 组**(摘修复→红→SHA256 还原核验→绿;脚本与日志 `/tmp/fullchain_mutation/`,汇总 `summary.json`,4 组 restore_sha_ok 全 true):
   - m1 摘 late_fill_evidence tz 留痕还原 pass → except_pass 898>897,**1F/1P** → 还原后 2P;
   - m2 摘 risk_gates L286 exc_info → W6 10→11 grew,**1F/15P** → 还原后 16P;
   - m3 F 分母还原混装 → **F_field_dropout×1(7/15)复现** → 还原后消失;
   - m4 摘台账「留痕不入守卫」标记 → 幽灵 job **1F** → 还原后 1P。
6. **相邻全量回归**(同 44 文件清单 `/tmp/fullchain_regress_files.txt`):改前 **14F/873P**(`/tmp/fullchain_before.log`)→ 改后 **11F/876P**(`/tmp/fullchain_after.log`,195s)。失败清单 comm diff:**修绿恰 3**(except_pass_not_growing / w456 real_repo / ledger job_ids_are_real),**新增红 0**;留红 11 条逐条既有(us_adr WIP crontab / dead_db_literal_gate / pipeline_size 3793>3700 / TestDirectionGateC ×8)。
7. **重启 + 冒烟**:重启前核 ① 未平仓 4 仓:BA(id1939,SHORT,23)/GS(1951,SHORT,5)/BABA(1952,LONG,42)/META(1953,LONG,6);② `data/exit_ledger.db` exit_intents/exit_legs 在途 **0**;③ `git status --porcelain -- app/ scripts/` 仅本批改动(us_adr WIP 为 untracked cron 脚本,app/ 与 main.py 零 import)。**18:00:53 CST `systemctl restart ftseek-us`** → `startup_smoke_check.py --wait 90` **7/7**(`/tmp/fullchain_smoke.log`);PID 242602→299974;启动段(≥18:00:57)`logs/ftseek_us.log` **0 ERROR/CRITICAL/Traceback**;4 仓方向/数量/入场时戳重启前后逐字一致。

**未验证**:全量 pytest/CI 未跑(以 44 文件相邻面为界);新留痕点位无自然生产触发证据(周末休市,首个自然窗口周一 11-30);W2 两字段是登记豁免非区分度恢复;F 分层后当前窗信号行 n=7<10 属小样本不判(既有纪律);无收益证据(正确性≠盈利)。

## §10 回滚

- 代码点位:`git revert` 本 commit(app/ 改动须 `systemctl restart ftseek-us` + 冒烟;scripts/ cron fresh-import 即生效)。
- 基线/台账/DEB:纯文件还原,无服务依赖。
- 全部改动零控制流/零交易行为变更,回滚无数据迁移、无判据转向风险。
