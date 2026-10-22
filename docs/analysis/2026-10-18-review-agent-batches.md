# 复查报告：2026-10-18 批次（8 个 hotfix 提交）+ 搭车复核 RFC-146/2a03fd8bb

- 日期：2026-10-18（主会话复查，独立第三只眼）
- 范围：`f26f643a4` 借券补查 / `027653e1d` quality_shadow+事件并发 / `d7960a8b7` event_meta / `8814c374f` LLM A/B harness / `22ba432f0` quality_shadow v2+报表 / `6cad19b34` SEC_PROVIDER 适配器 / `8eb930d4b` Form4 发现 / `520c870ac` SEC 对比工具；搭车复核 `ac1ae5b0c`（RFC-146 十三项）与 `2a03fd8bb`（复查修复批）的高危面。
- 方法：宪章辩论方纪律——不引用作者数字，全部独立重跑；HEAD 态用 detached worktree（`/tmp` 已清理）隔离核到语句级。

## 一、独立验证（全部本会话实跑）

| 验证 | 结果 |
|---|---|
| 棘轮家族 14 文件（naive-now/except-pass/utc4/pipeline-size/W456/W1/W2/W7/shadow_board/stale_bridge/stale_index/conftest原子写/rfc080反向） | **159 passed 全绿**，HEAD 无棘轮红（10-17 批次的复发模式未重演） |
| 批次定向（borrow_verifier/quality_shadow/event_llm_ab/form4_discovery/sec_compare/event_stream_phase1/playbook_db） | **121 passed 全绿**（干净树窗口） |
| test_playbook_pipeline 单独跑 | **74 passed** |
| 纯 HEAD worktree：ue_planned_preview/executability + ue_borrow_brief/contract_validator/evidence_citation/u1_sign | **277 passed 全绿** |
| 三条存量红复判 | `factor_stats_denominator`（干净树复现）、`pending_adjudication_report`（夹具陈旧：白话名"涨幅榜前 100"未命中）、`shadow_artifact_consumers`（p1_1_vwap_slope 终态产物消费者未认领）——**原因与 RFC-146 §5 登记逐字吻合，均非本批引入** |
| quality_shadow_report 独立重跑（只读生产库） | 复现 handoff 数字：降级 True 组 fwd **-56.33bps** / net **-13.71bps**（n=92/76），与作者报 -56.3/-13.7 一致 |
| 生产实证 | source_health 四源全 OK、单源延迟 0.5–3.9s（并发后总量级与"4.2–5.3s"声称一致）；10-18 起 38 条 canonical 中 24 条带 event_meta（其余 14 条为 11:43 提交前旧行，符合"新进程才落"预期）；`quality_shadow` 尚无生产指令样本（周末零指令，周一首轮验证） |
| 当前服务冒烟补跑 | 7 项机器断言全绿（见发现①的缺口说明） |

## 二、代码复查结论（逐 diff 核到语句级）

**成立的设计/修复**：
- `borrow_verifier`：fail-open 方向正确（异常/无数据保持 UNKNOWN），`get_short_pool` 用的是 `is_short_permit/short_pool_remain` 真实借券字段非融资口径；小批(5)+逐票兜底(≤5)+1s 间隔与 Futu 限流实证匹配；`offline_chain_driver` FROZEN_CFG 已同步关闭（断网面纪律有遵守）；prompt 规则 10 文案已同步声明"代码会逐票补查"。
- ingest 并发：网络面并发(max 4)、SQLite 写/健康表主线程串行，方向正确；`mark_source_health` 的 `COALESCE(excluded.last_success_at, …)` 确认不被 ERROR 行清掉（陈旧判定不失明）；单源异常从"炸整轮"变隔离 ERROR，是净改善。
- event_meta：迁移只加可空列（避开 retention NOT NULL 旧坑）；`canonical_events` 全部读取点都已带 `event_meta_json`（无 12/15 列错位）；writers 全部 cron 新进程，主服务无写入路径，**无版本错位窗口**。
- A/B harness：默认 dry-run、只读库(mode=ro)、输出仅 `data/shadow/event_llm_ab.jsonl`、prompt 双侧声明"材料不可信"——record-only 边界属实。
- SEC provider：`sec_provider_source_enabled` 默认 false、二选一不重复采、13E3 过滤修复正确且兼容旧缓存；Form4 复用现有 XML 解析、P/S+$50K+非10b5-1 过滤、全失败路径 warning+exc_info。
- RFC-146 搭车复核：`price=0` 市价单 fail-close（参考价→共享行情→拒单）链完整；`save_instruction` CAS（`WHERE COALESCE(executed,0)=0` + rowcount=0 回滚）把"已认领不重写"从仲裁层下沉到 SQL 层，纵深正确；跨进程 flock 两级锁 + 锁不可用 CRITICAL fail-close 方向正确；`live_readiness` 收口 DI/手动臂双入口、preflight 运行期回落按环境分向（LIVE→closed）修复了"启动锁上运行时放开"的洞；entry_confirm 孤儿重建用 arm 时冻结锚/窗口 + TTL 超期才 HEALED，纪律注释明确。
- `2a03fd8bb`：裸表单号禁用 title 去重（186 条 8-K 聚 1 条的真病灶）、patient_exit `min(配置, 收盘-10min)` 半日市修复中 `get_us_close_minutes_et` 为纯本地查表（不碰 futu，符合非交易路径纪律）、孤儿平仓前移幂等。

**record-only / 默认关闭声称全部属实**：未发现任何门/仓位/止损/出场的未申报改动。

## 三、发现清单（按严重度）

1. **【流程·中】12:53:52 CST 主服务重启未落档**。journalctl 实证干净 stop/start（PID 2136354），但 `22ba432f0` 条目只写"需重启后生效"，未记重启动作本身、无冒烟记录、无 PID——军规「重启必跑启动冒烟」+落档强制双缺口。我已补跑冒烟：7 项机器断言全绿（exit=2 仅因补跑晚于启动段的技术性提示）。影响低（周末无仓），但下一个复查者无法从 handoff 判定进程代码版本。
2. **【代码·低】`quality_shadow_report.py` 净值口径偏差**：`net = pnl_net or 0.0` 缺 `realized_pnl − fees_est` 回退，与项目净值惯例（pnl_net 优先、缺则毛−费）不符。当前实库仅 5/1010 旧行（9/16-17）缺 pnl_net，实际影响≈0；建议补一行回退以免惯例稀释。
3. **【代码·低】UE clue 排序同分 tiebreak 用 event_time 升序**（旧→新），而改动前 rowid DESC 等价新→旧——同材料性桶内新鲜度反转。若有意（novelty=最早信号）应在注释声明；否则改 `-event_time`。
4. **【代码·低】Form4 腿失败不可见**：`discover_recent_form4` 内部吞异常返回 []，`fetch_sec_filings_provider` 无从感知 → source health 仍 OK 不出 PARTIAL。建议返回 None 与 [] 区分，或在 discovery 层向 errors 传一句话。
5. **【代码·低】`source_registry.default_source_registry` 的 `except Exception: enabled=False` 零留痕**（fail-safe 方向对，但配置读取失败应 debug 一句）。
6. **【待激活·提示】13E3 解析修复（6cad19b34）未加载进主服务**：当前进程 12:53 启动早于 13:22 提交，而 `intraday_reentry` 在主服务进程内消费 `fetch_recent_13dg`。风险低（开关默认关、13E3 稀少、周末），并入下次重启窗口即可，但 handoff 未写这一点。
7. **【遗留·非本批】10-17 会话产物未提交**：`scripts/probe_entry_confirm_queue_cost.py` + `docs/analysis/2026-10-17-*.md`×2（OGS 解剖 + 队列成本，结论已裁"队列延迟实为收益勿动 cap"）。落档强制要求可复现锚点入库，建议补提交。

## 四、全量套件状态（诚实声明）

- 本会话首轮全量（14:07 起跑）= **21 failed / 7333 passed**，失败簇全部在 `test_ue_planned_*` 的 `TestInjectSiteWiring`（`inspect.getsource(_cmd_inject_impl)` 源码契约）。
- **该读数被并发编辑污染，不是 HEAD 判定**：跑测期间另一 agent（Qoder 进程实证）正在改 `llm_universe_scan.py` + 同簇测试文件（工作树 14:1x 起出现未提交 WIP：quality_shadow event_family 回退源、clue_max_per_source/family 限流等）。纯 HEAD worktree 复跑同簇 **277 全绿** ⇒ 所审 8 提交不欠这笔账；WIP 自身的契约红归该会话收尾。
- 教训归档（台账已有此型，再+1 实证）：全量跑测前必须先确认无并发编辑会话；HEAD 态判定一律 detached worktree。
- HEAD 全量结论：**定向+棘轮+契约面全绿，3 条存量红复判与登记一致，无证据表明 8 个提交引入新红**。

## 五、裁决建议

- 8 个提交**整体合格可留**：record-only 边界、默认关闭、回滚路径、测试伴随全部到位；无军规红线（无绕过 push hub、无新熔断层、无 prompt 消费方缺失）。
- 待办归口：发现①（重启落档）由本报告代为留痕；发现②③④⑤为低优先打磨项，不阻塞；发现⑥并入下次重启；发现⑦建议补提交。
- 周一观察项（已列在各批 handoff，此处汇总）：`quality_shadow` 首个生产指令样本分层、UE scan 的 `borrow_on_demand` 计数、事件 ingest 并发后 T0 新鲜度、`sec_provider_source_enabled` 影子开关的交易日对比（用 520c870ac 工具）。
