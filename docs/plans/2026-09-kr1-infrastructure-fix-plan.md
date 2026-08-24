# 2026-09 修尺子 + 基建修复计划（锚定 OKR-2026-09）

> 创建: 2026-08-24（用户裁决「先修尺子和基建」，承接方案 C）
> 依据: OKR-2026-09 / DEB-20260823-zero-gross-edge-five-rejections（六否决收敛）/ DEB-20260823-system-health-check（P0-P2 清单）/ DEB-20260823-full-chain-review §8
> 状态: 待用户裁决（Gate 2.5）

## 〇、为什么是这个顺序（一句话）

**9 月的 250 笔单版本样本是布，测量装置是尺——尺子不校准入账，9 月样本会像 8 月七个窗口一样作废。** 体检已实锤四类失真源：IC 聚类夸大 8 倍、毕业门测常量、遥测 60% 记错日、TEST 污染/无断言测试。故顺序 = 尺子（KR1）→ 测量管道卫生（KR4 地基）→ 工程债（解耦并行）。

## 一、总原则（三铁律）

1. **零行为变更**：不动退出参数（KR2 冻结，例外仅限 P0 bug + DEB 落档）、不动交易决策路径。全部项目按此白名单定性。
2. **每项有可验证完成判据 + 回滚方案**（默认 `git revert`）。
3. **修尺子先于一切结论**：KR1 完成前，任何统计输出标注「尺子未校准」。

---

## 二、主轴 A：修尺子（KR1，第 1-2 周）—— 最高优先

| # | 项 | 改什么 | 完成判据 | 类型/量级 |
|---|---|---|---|---|
| A1 | IC/记分牌按日聚类成对输出 | `app/playbook/ic_tracker.py`、`scripts/daily_ic_analysis.py`、`scripts/okr_status.py`：所有 IC/t 输出同时给 **pooled + day_clustered 成对值**，t 值强制标注聚类口径 | 脚本输出格式核查（两列成对存在）+ 落档 | L1 / ~100 行 |
| A2 | 撤下 ic_t1=+0.1265 头条 | `scripts/okr_status.py`（okr_latest 生成）：头条 IC 改按日聚类值；旧头条标注「子集选择产物，已撤」 | `okr_latest.json` 头条字段变更可查 | L1 / ~30 行 |
| A3 | 记分牌加 MDE 列 | 同上三文件：任何效应结论附 `MDE = 2.8×sd_daymean×√(2/n_days)`；无 MDE 不采信（宪章第 9 律落地） | 记分牌输出含 MDE 列 | L1 / ~50 行 |
| A4 | 停用 confidence-based 毕业判据 | `app/playbook/ic_tracker.py:619`（ic_positive=ic_t3>0.05）与 `mode_switch_service.py:230` 引用的 `_check_graduation_gate`：**判据标注 DEPRECATED，门禁本体保持现状不动**（fail-close=安全）；新判据待 KR4 预注册+用户批准后才替换 | grep 判据处有 DEPRECATED 注释 + DEB 落档 | L5 / ~10 行 |
| A5 | ~~R-8 遥测时区修复~~ | ✅ **已完成（2026-08-24 核查发现）**: 提交 0e380202 (8-23 10:51) R-8a-c 时区专项已修 `_emit_trigger_miss` → `_now_et()`，服务 8-23 21:15 重启后生效 | 已生效，无需动作 | — |

**A 轴依赖**：A4 的新毕业判据定义是 KR4 交付，需 A1-A3 的尺子先就位（第 3-4 周预注册）。

## 三、主轴 B：测量管道卫生（KR4 地基 + KR2 样本纯度）

| # | 项 | 改什么 | 完成判据 | 类型/量级 |
|---|---|---|---|---|
| B1 | 测试卫生（M2） | `test_phase1_signal_quality.py:175` 连真主库 → fixture/mock；23 个无断言用例补断言或标注；3 处真实 sleep 改 mock；`test_recommendations.py` 直连 finnhub/twelvedata 改 mock | 全量回归通过 + 无测试触真实 API/主库（grep 验证） | L1 / ~200 行 |
| B2 | 资金路径最小测试（M1 收窄版） | `broker_adapter.py`（1202 行，现仅 16 行测试）用现成未用的 `MockFutuBroker` 补**下单/撤单/对账**三条路径契约测试。不追六个千行文件覆盖（范围蔓延） | 新增测试通过 + 覆盖三条资金路径 | L1 / ~150 行 |
| B3 | print 收口（O1 收窄版） | 只收口风控模块：`option_risk_monitor`(19 处)/`atr_stop_engine`(14 处) print → logger。error_alert 按 ERROR 日志计数，print 行 = 测量盲区 | 两模块 `grep -c 'print(' = 0` | L1 / ~70 行 |
| B4 | 遥测存储轮转（O4） | jsonl 轮转（futu_capital_flow.jsonl 1.2MB/天；replay_bars_1m/ 已 213MB）+ 主库每周 VACUUM + 清 4 个零字节僵尸库 + DB 备份异机副本 | 轮转脚本跑通一周 + 备份可恢复验证 | L1 / ~100 行 |

## 四、主轴 C：工程债（与 A/B 解耦，可并行）

| # | 项 | 来源 | 完成判据 | 类型 |
|---|---|---|---|---|
| C1 | **39% 超风控帽核查**（优先，资金相关） | zero-gross-edge DEB 附带发现 1：126/321 笔名义额超 $4,920（$82,000×6%） | 输出核查结论：`risk_base_capital` 过期 vs 帽未执行，二选一定性 + 修复建议（本轮**只查不改参数**，改动另走裁决） | 核查 / 只读 |
| C2 | requirements.lock 重生成（E1） | health-check P1 | 补 loguru/prometheus_client/rich/aiosqlite；删 start.sh 的 requirements.txt 回退分支 | L1 |
| C3 | verify=False 四处收口（E2） | health-check P1 | twelvedata/finnhub/rsshub/fundamental 四 provider 收口（被 position_sync 对账消费） | L1 |
| C4 | 配置默认值三层收敛（E3） | health-check P1 | config_loader dataclass / config_defaults / yaml 三层对齐；**`pipeline.py:3298` `.get("max_daily_positions", 5)` 静默缩帽修复**（键误删则日开仓帽 16→5） | L1 |
| C5 | .env.us.example 全量重生成（E5） | health-check P1 | 代码实读 95 个 env 变量 vs example 仅 4 个；修 start.sh:80 引导复制不存在文件 | L1 |
| C6 | systemd 静默放弃拉起通知（O3） | health-check P1 | StartLimitBurst=5/300s 触发 → 钉钉/飞书告警接线 | L1 |

## 五、明确不做（防范围蔓延）

| 不做 | 原因 |
|---|---|
| 任何退出参数调整 | KR2 冻结，9 月唯一版本窗口 |
| P2 机制项（三条下单栈收敛 / 巨石拆分 / 死代码 ~2500 行 / utils 反向依赖） | 10 月+，随毕业判据结果一起裁决 |
| 板块字段回填 / 板块联动测试 | 已裁暂缓；sector_pulse 攒满 60 日再说 |
| 方案 D（波动选择影子） | 二次复核否决，已落档撤回 |
| 新增任何机械假设 | 六否决收敛；E2 十月裁决前不新增 |
| O5 trading_mode 每次读 yaml 优化 | 性能项非测量项，排队 |

## 六、阻塞项（需用户行动/裁决，非 Agent 可完成）

| # | 项 | 状态 | 建议 |
|---|---|---|---|
| 1 | **飞书凭据轮换** | X1 只删了仓内脚本本体，泄露值仍在远端 git 历史 | 用户在飞书后台作废重建（不轮换=没修）；轮换后同步 .env.us:59-60 + 重启 |
| 2 | main 分支快进（632 提交） | main 停在 2026-06-15，单分支承载全部工作 | 执行快进，恢复 main=事实主线 |
| 3 | filter-repo 历史清洗 | 重写历史需 force push，影响所有 clone | 与 #1 联动裁决：若 #1 已轮换，清洗优先级可降 |
| 4 | ~~R-8/R-9 批准~~ | ✅ **已完成**: R-8=0e380202 时区专项；R-9=635470b1 Dashboard 紧急全平 playbook 感知分流（均 8-23 归档+服务已加载） | 无需动作 |
| 5 | 做空门：接数据源 vs 诚实 disabled | 4/29 起数据恒空，门恒放行 | 建议诚实 disabled + 告警升级（SIM 期无实害，接线成本不对等） |

## 七、排序与里程碑

```
第 1 周 (8/25-8/29): A1-A4（尺子）+ C1（资金核查）      ← 尺子是 KR4 前置，C1 涉资金优先
第 2 周 (9/01-9/05): A5（若批）+ B1-B2（管道卫生）+ C2、C5（环境重建安全）
第 3 周 (9/08-9/12): B3 + C3、C4
第 4 周 (9/15-9/19): B4 + C6 + KR4 新毕业判据预注册（依赖 A 轴完成）
每 周 六 09:05:      周检自动核 KR2 唯一版本数 + 四锁 + 影子进度（已建，只加项）
9/30:                OKR 结算：KR1+KR2+KR4 达成 + KR3 干净证伪/确认
```

**执行纪律**（既有约定）：每项独立提交带回滚；提交前查 staged 防连带 Qoder 在途文件；改后全量回归；只提交自己的文件。

---

## 变更记录
| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-08-24 | 初版 | 用户裁「先修尺子和基建」；方案 C + 体检清单 + 六否决收敛为据 |
| 2026-08-24 | **主轴 A + B + C 第一/二批全部执行完成**：A1-A4(`188591d2`)、B1(`32cd9a2a`)、B2(`dd58f578`)、C2(`93678e6d`)、C5(`6c94b958`)、C3(`f380784d`)、C4(`55840852`)、B3(`cb4726f1`)、B4(`de5d446c`)、C6(`e18809dd`)，共 10 提交，回归 1585 passed，main 已同步 | 用户裁「继续」 |

### 执行备注（实测修正）
- **B3 收窄**：体检"176 处 print 收口"实测甄别后仅 17 处为运行时热路径（143 处在 `__main__` 自测块无害），只收口真热点。
- **B4 收窄**：体检四项（jsonl 轮转/主库 VACUUM/清僵尸库/异机备份）实测推翻前三项（碎片率 0%/回测依赖/有消费方），只做唯一真缺口=异机副本。
- **C1 结论**：39% 超帽系静态旧尺子误读，现行动态帽正常，无需改参（详见 DEB 落档）。
- **遗留**：① filter-repo 历史清洗（用户不轮换凭据下必要性上升，待单独确认）② KR4 新毕业判据预注册（第 3-4 周，依赖本轮尺子）③ O5 trading_mode 读 yaml 优化（性能项排队）。
