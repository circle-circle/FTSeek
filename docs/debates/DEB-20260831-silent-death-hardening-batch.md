# DEB-20260831-silent-death-hardening-batch — 静默死亡族根源加固批（P1/P2 零行为批）

> 立项源: 2026-08-31 全链路完整性评估 (Codex) → 用户令"按照建议执行"。
> 性质: **零行为变更批** — 全部为仪器/守卫/度量/留痕, 不触碰任何交易路径与参数,
> 符合 9 月冻结白名单 (修尺子/攒样本/做减法)。判据引用 `docs/ADJUDICATED-CRITERIA.md`。
> 上游: 方案承 `DEB-20260830-exit-reentry-chain-audit` (C2/E5/F2⑤) 与 OKR-2026-09 KR2/KR4。

## 状态

- 2026-08-31: 立项 + 全批落地 (用户裁"按照建议执行")。验证: 全量 2185 过 / 37 跳 / 3 失败均为沙箱网络限制 (socket PermissionError/数据源不可达致 quality=partial, 与本批零关联)。
- 2026-08-31 (第二轮): 死闸核查执行 (用户裁"明确是死闸需要废的，请核查无误后执行")。逐门三线实证结论: **5 道疑似死闸全部为休眠闸 (源可读+逻辑可达+机会有), 不满足废除条件, 一道未废**。同时坐实并回灌 C2 探针两处自身缺陷 (家族名错配×3 + 窗口分母含门不存在天数, 16→5 交易日), 见 §六。

## 一、落地清单 (逐项含回滚)

| # | 项 | 载体 | 判据/出处 | 回滚 |
|---|---|---|---|---|
| P0 | **W1 config_version 冻结窗守卫**: KR2 判分从周六人工 SQL → 每晚机器对账, 版本分裂**当天**告警 (旧: 月底结算才知, 作废整月)。豁免通道=人工更新基线文件+DEB 落档 (KR2 例外) | `scripts/daily_invariant_check.py` + `data/config_freeze_baseline.json` (基线: v2, 窗口 2026-08-24→, n=63 实测) | OKR-2026-09 KR2 | revert 脚本段 + 删基线文件 |
| P1② | **C2 命中率判据**: 门活性探针从"源可读"叠加"窗口内命中>0", 零命中翻 `dead` (留 `prior_status` 痕)。social=设计关断豁免; 影子窗<10 交易日不判 | `scripts/gate_liveness_probe.py` | DEB-20260830-exit-reentry E5/C2 (冻结期例外通道·仪器) | revert C2 段 |
| P1③ | **影子覆盖心跳**: 最近已完结 bridge 扫描日无 `edge_gate_shadow` 写入 → 告警 (抓**新增断流**; 存量 42% 已知债归结算 F2⑤ 管, 不重复告)。产物 `data/shadow_coverage_latest.json` | `scripts/business_heartbeat.py` | F2⑤ 判据引用 (≥0.8 为结算线; 实测近10桥日覆盖=0.8 恰压线, 8/17+8/24 缺) | revert 心跳段 |
| P1④ | **except:pass 批#1**: `ic_tracker` conditions_json 解析 + `feedback_learner` 修改样本解析 → warn 留痕 (测量/学习链不得静默丢样本, R5 同款) | `app/playbook/ic_tracker.py` / `app/playbook/feedback_learner.py` | R5 模式 (126ae224) | revert 两处 |
| P1⑤ | **except:pass 棘轮守卫**: 基线 **957** (本测试同款正则 2026-08-31 实测; 排除 tests/archive/venv/obsidian)。只降不升; 新增即 FAIL | `tests/test_except_pass_ratchet.py` | 同上 | 删测试 |
| P2① | **DB chokepoint 棘轮**: 字面路径 `sqlite3.connect` 新增即 FAIL。祖父名单 **10 个全在 scripts/**; **app/ 层实测已全净** (变量路径/SSOT 形态不在捕捉面, 诚实边界) | `tests/test_db_connection_chokepoint.py` | RFC-070 归口剧本 (先堵增量再收编) | 删测试 |
| 补 | **幽灵 job 闭合**: 台账 `5401548b2034` (富途通道裁决, 已销一次性 job) 触发守卫 — 根因=台账声明"留痕不入守卫"但守卫正则扫全文, 意图未机器化。修=守卫按行标记豁免留痕行 (其余检查面不放松) + 台账行合并+标注 | `tests/test_control_loop_ledger.py` + `docs/CONTROL-LOOP-LEDGER.md` | 第7轮已登记"既有幽灵 job_id 台账问题" | revert 两处 |

## 二、范围调整声明 (实施中就地更正)

1. **except:pass "批量清剿"降级为"批#1+棘轮"**: 原方案措辞为一次性普查→批量改; 实测全仓 **957 处** (非初估 869, 口径差异=正则不同), 单批全改违反一次一变量且审查面失控。改为: 批#1 只修测量/学习链两处最高价值点 + 棘轮冻结增量, 存量分批清剿 (每批调低基线)。
2. **intraday_reentry 8 处门侧 fail-open `except:pass` 本批不动**: 均带"不拦 (fail-open)"设计注释且处 78 次/日热路径, warn 化需限频设计, 登记为批#2 (C2 探针已覆盖其门活性, 风险敞口已收窄)。
3. **watchdog "阶段产出带"收窄为影子覆盖心跳**: 核查发现 bridge 产出带已存在 (`app/playbook/pipeline.py` `_check_bridge_health_and_alert` + v0.70 P0-2 运行期重载), 表级活性已有 (`data_freshness_watchdog` D1/D2), 真正缺口只剩影子挂载断流 → 只做这一件, 不重复造。
4. **C2 验收**: DEB 判据"上线次日 6 道零命中闸至少 5 道翻 dead" — 离线实测 **5 道精确翻转** (short_squeeze/analyst_divergence/earnings_proximity/sec_event/ticker_sentiment; 影子窗 16 交易日), 3 有命中门保持 ok, social 保持 off。周一 (9/1) cron 实跑为线上确认点。
5. **C2 窗口分母勘误 (第二轮实证推翻本条第4项口径)**: 第4项"影子窗 16 交易日"含 8/6~8/23 共 **11 天证伪门根本不存在的日子** (9 门 8/22 上线 73c853b9/6e0e09ea/0379a90d + 当晚 22b/c 修复 581142dc, 8/22-23 周末) → 有效分母 = 8/24~8/28 **5 交易日**。"5 道翻转"结论仅在假分母下成立; 已修探针加 `_GATE_LIVE_DATE="2026-08-24"`, 修正后窗口不足 10 天 → 判定正确延后, 首个合法判点 = 9/9 探针 (见 §六)。
6. **C2 家族名错配回灌 (P11)**: `_GATE_REASON_FAMILY` 三名与真实拒因串不符 (short_squeeze→`short_squeeze_risk` / sec_event→`sec_event_divergence` / ticker_sentiment→`ticker_sentiment_divergence`)。当前命中皆零未影响判定, 但未来命中会被误判死门 → 已修。

## 三、冻结合规声明

本批零交易行为变更: 不改任何配置阈值/退出参数/入场门/仓位逻辑; 全部为只读仪器、守卫测试与留痕。不违反 C1~C7 任何红线; 未新增 cron job (无需控制环路台账入账)。

## 四、验证记录 (真实结果)

- py_compile 全过; W1 实跑: `config_freeze={versions:{v2:63}, status:ok}` + 不变量 58/58 全过。
- C2 离线: 5 道翻 dead (≥5 达标); 影子窗 2026-08-06~08-28 共 16 交易日。
- 影子覆盖心跳: 真实场景无告警 (8/28 有写入, 覆盖 0.8) / 空影子变异场景告警 ✓。
- 守卫测试: 棘轮×2 + 台账 6 例全过。
- 全量: **2185 passed / 37 skipped / 3 failed** — 3 失败均为本会话沙箱网络限制 (socket PermissionError ×2 / 数据源不可达致 quality=partial ×1), 与本批零关联 (改动面不含 earnings/sentiment 采集)。
- (第二轮) C2 修正后离线: `trading_days=5 (8/24~8/28), window 不足 → 翻转 0` ✓; 阈值压到 3 的机制自检: 恰好 5 道零命中门翻转、3 有命中门 (capital_flow 283/sentiment_extreme 172/insider_divergence 170) 不翻 ✓ (证明家族名修复生效)。
- (第二轮) 全量: **2185 passed / 37 skipped / 3 failed**, 与基线逐项一致 (3 失败同沙箱网络, 与本轮零关联)。

## 五、挂账

- 批#2: intraday_reentry 8 处 fail-open warn 化 (需限频设计)。
- 存量收编: DB 字面连接 10 scripts (祖父名单) + except:pass 957 处分批。
- 9/1~9/8 探针: c2 段应报 `shadow_trading_days` 5→9 (窗口不足, 不翻转) — 这是**修正后的正确行为**, 非故障。
- **9/9 (周三) 探针 = C2 首个合法判点** (≥10 活日): 若 5 门仍零命中 → 按判据翻 dead, 届时再议处置选项 (阈值降档/影子扩窗/废除), 本轮预裁为不废。
- 覆盖缺口登记 (第二轮发现, 非死闸问题): ①SEC CIK 映射未覆盖池内 8 标的 (BABA/GOOG/GS/JD/NTES/ORCL/SKHY/TSM, ADR 为主) → sec 门对这 8 票恒不触发; ②LLY 8/7 候选日与财报日关系离线无法定案 (疑 8/6 已报=门外合法), 待 9 月线上财报季自然验证。
- 批#2 / 存量收编: 同前。


## 六、死闸核查执行 (第二轮, 2026-08-31 用户裁"明确是死闸需要废的，请核查无误后执行")

**授权条件**: "核查无误后执行" — 核查结果是废除的前置条件, 非走过场。
**核查标准**: 死闸 = 输入恒空 / 分支不可达 / 源未订阅 (逻辑坏, 永不触发, 不修代码不可能触发); 休眠闸 = 源可读 + 逻辑可达 + 触发机会存在, 仅事件稀有。**只有确证死闸才废。**

### 6.1 决定性时间线证据 (git 实证)

| 事实 | 证据 |
|---|---|
| 9 道证伪门全部 2026-08-22 上线 | 73c853b9 (分析师/财报/情绪) + 6e0e09ea (卖空/内部人) + 0379a90d (重大事件/社交/逐笔) |
| 上线当晚 22b/c 修复批 (含 short_squeeze TypeError 死门) | 581142dc 22:05 (修复前该门恒不触发, 修复后实活验 NVDA 12.67%) |
| 8/22-23 = 周末 → 首个全活交易日 8/24 | 日历 |
| 影子窗 8/6~8/28 中 8/6~8/23 共 11 天门不存在 | 上述合并 → **有效观察窗 = 5 交易日** |

→ C2 原"16 交易日零命中"判据分母造假 (11 天门不存在的天数被当作门存活证据), 已修正。

### 6.2 逐门三线实证 (源可读 / 逻辑可达 / 机会有)

| 门 | 源侧 | 逻辑侧 (纯函数直测) | 机会侧 | 定性 |
|---|---|---|---|---|
| short_squeeze | 探针 8/28 ok: NVDA short_percent=10.785 真值 | `35%>30% → short_squeeze_risk` 触发 ✓ | 修复后窗口 391 个 SELL_SHORT 候选 (8/24~28: 19/105/109/83/75) | **休眠**: 池偏大盘, 日卖空比常<30%; >30% 可达性离线不可证伪 |
| analyst_divergence | 探针 8/28 ok: NVDA periods=4 (Yahoo 源活) | LONG+downgrade / SHORT+upgrade 双分支触发 ✓ | 候选×±5pp 死区外的环比修订动量双过滤 | **休眠**: 修订动量 ≥5pp 为低频事件 |
| earnings_proximity | 探针 8/28 ok: 日历 103 条活跃; 8/21 覆盖修复 (动态池可查) + TZ-2 ET 锚定 | `days=2 → earnings_proximity(2d<3d)` 触发 ✓ | 财报=季度事件, 候选×3 天窗口重叠本就低频; NVDA 窗口内从未成为候选 (其 8/27 财报无矛盾) | **休眠** |
| sec_event | 探针 8/28 ok: 8-K 70 条/利空 26; 周六快照离线复现: 26 利空全落池外小盘, mapped∩pool=0 | LONG+利空 item / SHORT+SC13D 双分支触发 ✓ | CIK 映射覆盖池内 37/45; 池标的利空 8-K 低频 | **休眠** (附 8 票映射盲区登记) |
| ticker_sentiment | LV3 盘中实活 (8/26 日志): 订阅 18 只 TICKER 恰含候选池 (AMD/COIN/RIVN/SMTC/ORCL/LLY/BSX/AVGO…), "Ticker 数据到达" push 实证, 退出链 `src=futu_lv3_push` 实消费; 盘外 confidence=0 合法 | LONG+bearish / SHORT+bullish 双分支触发 ✓ | 再入场候选本沿动量选出, 与逐笔情绪背离天然低频 | **休眠** |

### 6.3 裁决

**5 门全部通过三线检验, 零命中归因 = 事件稀有 × 有效窗口仅 5 天, 无一构成结构性死亡证据 → 不满足"确证死闸"废除条件, 本轮一道未废。** 若强行废除: 财报/重大事件/卖空拥挤恰是冻结期最需要的低频高伤证伪保险, 废休眠闸 = 拆保险丝换整洁, 违反判据字典"证伪门宁存勿废"取向。

**探针自身两缺陷回灌 (P11, 已修)**: ①家族名错配×3 (见 §二.6); ②窗口分母造假 (见 §二.5, `_GATE_LIVE_DATE` 修正)。回滚: revert `scripts/gate_liveness_probe.py` C2 修正两段。

**再裁点**: 9/9 探针 (≥10 活日)。届时仍零命中 → C2 判据翻 dead 生效, 处置选项再议 (阈值降档/池扩样/废除), 需新一轮用户裁决 — 本轮不预支。
