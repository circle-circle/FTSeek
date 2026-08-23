# DEB-20260823 全项目体检（五维并行深探 + 逐条独立核验）

> **性质**: 全系统体检报告与执行档案。5 路并行探查（交易安全语义 / 架构结构 / 测试体系 / 配置密钥 / 运维可观测）+ Claude Code 主会话逐条独立核验（**探查报告不直接采信，所有 P0 均以 file:line 实读复核**）。
> **触发**: 用户令"对整体项目进行全方面体检，给出优化调整和提升建议"→"先落档再执行，执行前请确认是真实存在的 bug 或异常问题"。
> **约束对齐**: OKR-2026-09 明日起跑（08-24），KR2 参数冻结全月——本档案所有执行项均按"安全收紧/卫生类，不动退出参数"白名单定性；机制类变更一律排队。
> **全量回归基线**: 1564 passed / 1 skipped（62s，本日实测复现）。

---

## 一、总评

框架与流程**不需要动**：文档全带 file:line 可复核、每提交带回滚方案、有勘误自我纠正文化、四锁实测全闭、告警链真实可用（钉钉/飞书）、DB 备份机制完善。问题集中在四类：
① 少数"门开着但已失效"的安全残留；② 资金路径测试盲区；③ 单分支治理与密钥卫生等工程基建债；④ 数据/配置生命周期缺位。

## 二、P0 核验表（逐条独立复核结论）

| # | 发现 | 核验结果 | 定性 |
|---|------|---------|------|
| 1 | **生产飞书凭据硬编码入库** | ✅ 实锤且**升级**: `scripts/archive/get_feishu_chat_id.py:16-17` 的 app_id/app_secret 与 `.env.us:59-60` 生产推送凭据**完全一致**; `git log -S` 确认在历史提交 6dafd2a0 中 | 真 bug，须止血 |
| 2 | **单分支承载全部近期工作** | ✅ 实锤: `rfc-060-phase1-hotfixes` 领先 origin/main **632 提交**, main/master 停在 2026-06-15, 本地 **16 提交未推送** | 运营风险 |
| 3 | **做空安全门整体空转** | ✅ 实锤但有前史: `data/hard_to_borrow.json`/`short_interest.json` 均 2 字节 `{}`、04-29 起 116 天未更新; `short_specific_checks.py:51-60` 缺失返回空→squeeze(:164)/借券(:101)恒放行。**注意**: `_warn_if_stale:34` 每进程告警一次且注释自证"生产者未接线, 裁决见 DEB-20260726-subtraction-audit-round2"——属已知已裁决缺口, 非新发现 | 已知缺口, 待再裁决 |
| 4 | **equity≤0 静默跳过账户熔断** | ✅ 实锤三处: `order_executor.py:3267` / `playbook_service.py:725` / `orb/engine.py:1216`, 结构均为 `if _equity > 0:` 无 else——capital_safety 的 current_equity 为 0/过期时熔断无声跳过（进程启动首查前/行情断供窗口现实可达）。异常路径已是 fail-close（ps:737 由 acb_fail_mode=closed 驱动）, 唯 equity 缺失路径漏防 | 真 bug，本轮修 |
| 5 | ~~隐藏第五锁: preflight_fail_mode=open~~ | ⚠️ **核验后降级**: yaml:1417 确为 open, pipeline.py:3221/:3467 两处嵌套 fallback 也确为 "open", 但均为 R1a/R1c 文档化设计（注释自证 2026-07-19 DEB-round2 Ω.3-3 裁决"SIM 默认 open"）, 且 `di_container.py:159-176` 已有 LIVE 启动断言**强制 closed 否则拒绝启动**（2026-07-20 修复6a）。残余风险窄: 仅 sim 内 preflight 异常时放行 | 既定设计, 冻结期不动 |
| 6 | **shadow 遥测 TEST 守卫不齐** | ✅ 实锤: 守卫约定在 `pipeline.py` 已有 9 处（:330/:3698/:4272/:4472/:4525/:4851/:5207/:5237/:6724, 且 :6724 注释自证"8/18-19 生产 jsonl 已被测试数据污染"前科）; 但 `exit_shadow/invalidation_shadow/overnight_hold_shadow/sltp_verify_shadow/shadow_tracker/shadow_signal_logger` 六模块 grep '"TEST"' 命中数为 0 | 真 bug，本轮修 |

### 核验中对探查报告的两处修正（勘误）
1. P0-5 由"隐藏地雷"降级为"文档化设计+LIVE 断言兜底"——原报告漏看 di_container 启动断言。
2. "requirements.lock 缺 21 包含 aiohttp/websockets/yfinance/scipy"**夸大**: 实测 venv 107 包 vs lock 84 包, aiohttp/websockets/scipy/yfinance 均在 lock; 真实缺口 = dev 工具群(black/flake8/mypy/py-spy 等, 无害) + 运行时侧 loguru/prometheus_client/rich(+依赖)/aiosqlite。结构性问题仍在: `start.sh` 有 lock 失败回退 requirements.txt 分支, 重建环境即松范围漂移。

## 三、P1 清单（9 月冻结期内做, 全部零行为变更类）

**测量可信度（喂 KR4 毕业门重定义）**
- M1 资金路径测试盲区: `broker_adapter.py`(1202 行真实下单通道)仅 1 个 16 行测试; `tests/harness/mock_broker.py::MockFutuBroker` 现成未用; orb/engine、signal_pipeline、market_data_router、review_orchestrator、agent_bridge、web data_service 六个千行文件零直接测试
- M2 测试卫生: `test_phase1_signal_quality.py:175` 连真主库; conftest 强制 daemon 化 futu 线程防卡死(注释记录最长 11h)=有用例触真连接; 23 个无断言用例(test_option_risk_monitor 6 处/test_shadow_tracker 5 处等); 3 处真实 sleep(pre_entry_gate:214 sleep(10)); `test_recommendations.py::test_calibrate_missing_price` 直连 finnhub/twelvedata 真实 API
- M3 死回撤门二选一: `DrawdownControl.update_assets()` 全仓零调用(实锤)=8% 总回撤永不评估; 且 `record_trade_pnl` 仅主线平仓喂入(order_executor.py:5198 source="main"), playbook 平仓亏损不入日回撤池——口径低估。接线或诚实删除, 待裁

**工程基建**
- E1 重生成 requirements.lock(补运行时缺口 loguru/prometheus_client/rich/aiosqlite)+删 start.sh txt 回退分支
- E2 四数据源 verify=False 收口: twelvedata_provider.py:238 / finnhub_provider.py:182 / rsshub_us_news.py:460 / fundamental_provider.py:48——被 position_sync(对账)/playbook/market_data_router 消费
- E3 配置默认值三层收敛: config_loader.py dataclass 默认(min_confidence=0.70/max_per_symbol=0.25) ≠ config_defaults.py(0.80) ≠ yaml; `playbook/pipeline.py:3298` `.get("max_daily_positions", 5)` fallback 与 SSOT=16 不符(键误删则日开仓帽静默缩到 5)
- E4 CI 加固: ci.yml `pip install ... || true` 吞安装失败; 加 ruff+mypy(scope 到 core/execution 起步)
- E5 `.env.us.example` 全量重生成: 代码实读 95 个 env 变量 vs .env.example 仅 4 个, 52 个双黑户(含 FUTU_UNLOCK_PWD); start.sh:80 引导复制的 .env.us.example 不存在

**运维韧性**
- O1 print() 176 处收口(风控模块最重: option_risk_monitor 19/atr_stop_engine 14/alphavantage 22)——error_alert 按 ERROR 日志计数, print 行漏报
- O2 cron 统一失败上报 wrapper + watchdog 日一次→盘中小时级(现覆盖仅 14 条路径)
- O3 systemd StartLimitBurst=5/300s 静默放弃拉起→接钉钉通知
- O4 jsonl 影子数据轮转(futu_capital_flow.jsonl 13MB≈1.2MB/天; replay_bars_1m/ 已 213MB)+主库每周 VACUUM+清 4 个零字节僵尸库+DB 备份异机副本
- O5 trading_mode.py:174-185 每次属性访问重读 2729 行 yaml(退出循环高频调用, 绕过审计)→改 loader dotted get+mtime 缓存

## 四、P2 清单（10 月+, 机制类, 随毕业判据结果一起裁决）

- 三条下单栈收敛: web_dashboard/order/executor.py 直连 place_order 绕过 broker_adapter+pre_trade_gate; HK 手动单内存 TTL60s 幂等重启失效; 手动线不取共享 SymbolLockManager(自动+手动同标的并发无互斥)
- 巨石拆分(切缝已勘定): playbook/pipeline.py(8297)按 voting/generate/tracking/autoexec/exits/sim_exit 六块, scheduler 已懒加载入口拆分不动调用方; order_executor.py(5420)"私有"函数已被外部当公共 API(_close_position×5/_get_current_price×4/_submit_order×3)应先转正接口
- 死代码清除: app/metrics/(整目录零消费)/benchmark_tracker.py(427 行)/app/scheduled_tasks/(空目录)/agents//factors 未接线部分 ≈2500+ 行
- utils 反向依赖矫正(critical_alerts.py:35,44,100 import notifier/push; 98 文件依赖 utils); ≥15 文件绕过 market_data_router 直连 Futu 行情
- 文档再锚定: ARCHITECTURE/handoff 20 处"排队至 8/31 终裁后"——终裁已废, D1-D7 解锁条件悬空, 应统一改锚"预注册毕业判据通过或用户明示"; ARCHITECTURE §2 已知错误(dual_broker 直连 place_order, DEB-20260823 勘误已指出)趁手改
- 根目录散落 test_direction_config.py/test_playbook_exit_rules.py/test_technical_arbitration.py 移 scripts/(pytest.ini testpaths=tests 本就不收集)

## 五、本轮执行记录（仅限核验为真且无需新裁决的项目）

| 项 | 动作 | 回滚 |
|----|------|------|
| X1 | 删除 `scripts/archive/get_feishu_chat_id.py`(泄露生产凭据的脚本本体) | git revert; **凭据轮换须用户在飞书后台作废重建**(轮换后同步 .env.us:59-60 并重启服务) |
| X2 | equity≤0 三处补 fail-close(拒单+ERROR 日志), 正常路径零行为变化 | git revert |
| X3 | 六个影子模块补 TEST 守卫(对齐 pipeline.py 九处既有约定) + 契约测试 | git revert |
| X4 | git 历史清洗(filter-repo 抹 6dafd2a0 中密钥) | **待用户单独裁决**(重写历史需 force push, 影响所有 clone) |

### 待用户裁决/行动
1. **飞书凭据轮换**（X1 只删了仓内暴露面, 泄露值已在远端历史中, 不轮换等于没修）
2. main 分支快进合并(632 提交)恢复"main=事实主线"
3. 做空门: 接数据源(Futu 借券/空头占比接口) vs 诚实置 disabled+告警升级
4. DrawdownControl: 接线(position_monitor 刷新+playbook 平仓喂 pnl) vs 删除
5. filter-repo 历史清洗是否执行

## 六、与 OKR-2026-09 的节奏对齐

```
本周(冻结生效前窗口): X1-X3 已执行(安全收紧+卫生类, 不动任何退出参数, 符合 KR2 例外通道)
9 月冻结期:           第三节 P1 全部(零行为变更)——M1/M2 直接喂 KR4 毕业门可信度;
                      同步推进 KR1 尺子修复
10 月+:               第四节 P2 机制项, 随毕业判据一起裁决, 勿提前
```

呼应洞察: 8/21 历史回测曾发现"功效预估 σ 与实际 σ 差 6.5 倍"; 本次体检发现测量装置本身存在多处失真源(TEST 污染路径/print 漏报/死门/默认值漂移)。**9 月攒样本的价值上限取决于先把装置修准**——这正是 X1-X3 与 M 节的意义。

## 七、五维探查全文要点归档

### 架构
- 巨石三座: playbook/pipeline.py 8297(87 函数无主类)/order_executor.py 5420/scheduler_core 2804(55 方法 god object)
- 平行宇宙确认: web_dashboard/order/executor.py 非复制但直连 ctx.place_order(L199/438/1069/1455/1505); 全仓 `.place_order(` 15 处三条栈; us_pipeline 是活跃小模块非复制品; simple_dashboard 活着(web_dashboard/app.py:176 作 stats 计算器)
- 僵尸目录: agents/(仅 archive 测试引用)/evolution(单点懒加载 scheduler_triggers:834)/orb(enabled=false 休眠)
- 循环依赖 8 对靠函数内懒加载掩盖; import * 6 处(llm/prompts/__init__:8-12, harness/__init__:9)
- 死码: metrics 整目录/benchmark_tracker.compute_alpha 零引用/factors registry 未接线(活入口仅 TradeQualityGate+GeoEventPreFilter)
- 分层违规: utils 反向依赖上层; ≥15 文件绕过 market_data_router

### 交易安全
- fail-open/close 一览表已建(113 开关中 36 false); 高危残留除 P0 外: 动量门四处异常放行且注释自称"保守"(price_momentum_gate.py:87,133,158,192——与已修 TypeError 吞门同构, 建议冻结期后改 close 或加计数看板); 平仓前持仓校验 except return True(order_executor.py:4276)+store 查无此仓也放行
- 幂等: 自动线 client_order_id 含随机 uuid(order_executor.py:2193,2548,4497)dual_broker.py:238 自认"非真幂等"; SQLite IdempotencyManager 只覆盖 dashboard 美股路径
- shadow TEST 守卫: scripts 侧 conflict_shadow_tag/band_vol_shadow_tag/gap_semantics_shadow_tag/e2_momentum_shadow_daily/entry_sync_shadow_tag/factor_neg_edge_shadow/futu_channel_shadow_collect 等亦无守卫(cron 驱动非 pytest 直调面, 风险低于 app 侧, 一并列入后续收口)

### 测试
- 见第三节 M1/M2; mock 密度健康(17 文件~123 处, top 目标是依赖非被测体); pytest.ini 仅 4 行无 timeout/markers/xdist; 类型注解抽样 31%-68%

### 配置密钥
- census 落档: unified_config.yaml 2729 行/45 顶层键/746 叶, orphan 107+definition_only 7(referenced 632); 口径漏报注意: safety_guards 家族被标 definition_only 实际有消费(position_sizer.py:73-80)
- 两块同名 capital_safety(yaml:994 market_context 与 :1438 risk)易改错位置
- trading_mode 二读取路径+热加载只盯 unified_config.yaml 一文件(config_us 常量/exit_profiles/playbook_horizon[已由 R-4 mtime 自愈补]不在监控)
- .env 不在 git 历史 ✓(git log --all --full-history 实证)

### 运维
- crontab 58 行路径强耦合当前全通; 三层调度(crontab/hermes/scheduler_triggers)边界靠注释; insider 监控双跑/周检三处/error_alert×intraday_diagnose 双轨
- 日志轮转 OS 级 /etc/logrotate.d/ftseek-us daily×14 compress 健康; logs/ 仅 15MB
- sqlite 27 库: 主库 18M/signal_snapshots 15M; db_backup.sh 每夜 .backup 7 天保留+integrity_check 机制完善但单机单副本
- 心跳: 盘中 intraday_heartbeat 30min 推钉钉; 进程死透由次日 watchdog 间接发现

---
**落档**: Claude Code, 2026-08-23。核验方式: 所有 P0 逐条实读源码+git 实证; 探查声明与实读不符处已在第二节勘误。
