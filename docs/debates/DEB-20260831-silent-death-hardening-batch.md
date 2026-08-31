# DEB-20260831-silent-death-hardening-batch — 静默死亡族根源加固批（P1/P2 零行为批）

> 立项源: 2026-08-31 全链路完整性评估 (Codex) → 用户令"按照建议执行"。
> 性质: **零行为变更批** — 全部为仪器/守卫/度量/留痕, 不触碰任何交易路径与参数,
> 符合 9 月冻结白名单 (修尺子/攒样本/做减法)。判据引用 `docs/ADJUDICATED-CRITERIA.md`。
> 上游: 方案承 `DEB-20260830-exit-reentry-chain-audit` (C2/E5/F2⑤) 与 OKR-2026-09 KR2/KR4。

## 状态

- 2026-08-31: 立项 + 全批落地 (用户裁"按照建议执行")。验证: 全量 2185 过 / 37 跳 / 3 失败均为沙箱网络限制 (socket PermissionError/数据源不可达致 quality=partial, 与本批零关联)。

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

## 三、冻结合规声明

本批零交易行为变更: 不改任何配置阈值/退出参数/入场门/仓位逻辑; 全部为只读仪器、守卫测试与留痕。不违反 C1~C7 任何红线; 未新增 cron job (无需控制环路台账入账)。

## 四、验证记录 (真实结果)

- py_compile 全过; W1 实跑: `config_freeze={versions:{v2:63}, status:ok}` + 不变量 58/58 全过。
- C2 离线: 5 道翻 dead (≥5 达标); 影子窗 2026-08-06~08-28 共 16 交易日。
- 影子覆盖心跳: 真实场景无告警 (8/28 有写入, 覆盖 0.8) / 空影子变异场景告警 ✓。
- 守卫测试: 棘轮×2 + 台账 6 例全过。
- 全量: **2185 passed / 37 skipped / 3 failed** — 3 失败均为本会话沙箱网络限制 (socket PermissionError ×2 / 数据源不可达致 quality=partial ×1), 与本批零关联 (改动面不含 earnings/sentiment 采集)。

## 五、挂账

- 批#2: intraday_reentry 8 处 fail-open warn 化 (需限频设计)。
- 存量收编: DB 字面连接 10 scripts (祖父名单) + except:pass 957 处分批。
- 9/1 (周一) C2 线上确认: `gate_liveness_latest.json` 应见 5 道 dead + c2 段。
