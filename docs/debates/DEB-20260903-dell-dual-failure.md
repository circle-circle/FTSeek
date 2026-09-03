# DEB-20260903-dell-dual-failure — 追高帽违例升级被去重吞没(尺子) + 净保本限价被动侧快跌滑点(出场)

状态: **已裁决已执行** (2026-09-03, 用户裁"按建议执行" — 议题一/二均方案A; 独立 agent 核查 14 条证据链零失实后裁决)
裁决请求: 议题一 (尺子) / 议题二 (出场) 各自方案三选一 (本文均建议 A)。
锚定样本: DELL 2026-09-02, trade_records id=1425, 毛 -$45.12 / 净 -$49.30, 持仓 6.4 分钟。

## 一、缘起

用户问: DELL 是否全链双重失效样本 — "影子追高帽判据在场但无牙齿 + 净保本兜底快跌带滑点"。
全链核查 (日志/DB/影子 jsonl/1分bar/代码) 确认**成立**, 且两侧各有一处超出提问本身的加重情节:

1. **入场侧加重**: 追高帽违例不仅 record-only (DEB-20260901 Phase B 裁决设计, 明文"不拦截"),
   二次 re-price 的**更糟违例 (464.41→474.27, +2.1%) 被影子去重整条吞掉** — 无 jsonl 行、
   无 warning 日志。尺子本身在少记, 而该计数正是 DEB-20260901 Phase C 的裁决证据。
2. **出场侧加重**: 净保本限价在"击穿式检测"下**结构性不可成交** — 8/20 方案A (slip_cushion
   0.2% + 超时 8s→3s) 修的是"贴线检测"几何 (触发线>限价→主动侧), 对"20s 轮询间隙内价格
   直接击穿限价线"分支无效。DELL 为该分支家族第 5 案, 也是 8/20 修复上线后**证明缺口仍在**
   的首例。

## 二、证据链

| # | 证据 | 出处 |
|---|------|------|
| 1 | DA 论文倒置: "Q2 AI服务器需求爆发…建议回踩$445-450区间分2批低吸**而非追涨**", confirm_level=450.0 (below, ttl 15min), chase_cap=460.0 | trade_records id=1425 metadata.thesis; pullback_limit_shadow.jsonl trigger 行 (inst_id=2072) |
| 2 | 违例遥测打出但明文不拦截: `chase_cap违例遥测: re-price=$464.41 越过禁追线 $460.00 (DEB-20260901 Phase B record-only, 不拦截)` | logs/ftseek_us.log-20260903, 2026-09-02 21:37:55.683 |
| 3 | **升级违例被去重吞没**: 21:37:55.684 因单轮开仓上限顺延 (本轮已开 3 笔) → 21:42:32 二次 re-price 464.41→474.27, 无遥测无影子行。`_shadow_dedup_ok` 按 (inst_id, type) 全文件去重, 且 logger.warning 在去重门**内** → 同指令终身只记首次违例, 升级静默 | 同日志 21:42:32; app/playbook/pipeline.py:4170-4187 |
| 4 | 成交 474.305 (滑点 6.96bps): 比 confirm_level 高 **5.4%**, 比禁追线高 **3.1%**; MFE +1.31% (peak 480.525) | trade_records id=1425 metadata.execution |
| 5 | **反事实**: CF 状态 `expired_unfilled` (450 全天未触及, 限价路径=不成交=$0) → -$49.30 为纯追高路径损失, 100% 归因失效① | data/shadow/pullback_limit_replay_latest.json DELL 行 (chase_cap_violated=true, actual_net=-49.3) |
| 6 | 触发线数学: peak 480.525 (+1.31%) → 锁利线 476.68 (peak×0.992, drawdown_from_peak_pct=0.8) > BE 触发线 475.60 (entry×(1+fee 0.0733%+slip 0.2%)) > 净保本限价地板 474.6533 (entry×(1+fee)) | config/playbook_horizon.yaml:54-56; pipeline.py:6571-6574, 7109-7112 |
| 7 | **击穿式检测**: 检测价 473.1199 (src=futu_lv3_push) 在净保本限价线下 **1.53 点**、锁利线下 3.56 点 — 20s 轮询 (PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC) 间隙内价格击穿, 8/20 slip_cushion 几何修复对此分支无效 | pipeline.py:8423-8426 触发日志行; scheduler_core 退出快循环 |
| 8 | 被动侧限价 3s 必死: `限价平仓单 3s 未成交 → 撤单降级市价 (净保本兜底路径)` → 09:49:06 成交 470.545。触发后 (09:49 窗口) 价格区间高至 ~472.4, 限价 474.65 结构性不可能回摆成交 | dual_broker.py:423; data/replay_bars/US_DELL_2026-09-02.json (bar 末时间戳口径: 09:49 bar 覆盖 09:48, 09:50 bar 覆盖 09:49, 后者 high 472.36) |
| 9 | **损失分解** (设计保本≈$0 → 实际净 -$49.30): ① $18.40 (37%) = (474.6533−473.1199)×12, 轮询间隙击穿, 本 DEB 不解决 (见§七); ② $30.90 (63%) = (473.1199−470.545)×12, 3s 死等+撤单+市价往返 ~12s 延迟, **可回收** | 算术; 时戳: 触发 21:48:54.511 → 撤单降级 21:48:59.195 → 成交 21:49:06.558 |
| 10 | **家族 5 案全负**: SNDK 8/18 −$8.99 (击穿, SL_TRAIL_LOCK 纳入限价保护的直接动因) / MU 8/20 −$24.42 / WPM 8/20 −$12.14 / AU 8/20 −$3.80 (超时降级, 促成 8/20 方案A) / DELL 9/2 −$49.30 (击穿, 8/20 修复后首证缺口仍在)。均值 ≈ −$19.7/案 | pipeline.py:8397-8403, 8430-8432 历史注释; trade_records |
| 11 | 公平注记 (出场机制保护职能及格): 若锁利不触发, 初始 SL 450.56 = **−$285** (当天日低 432.27, 日高 497.99 — 先瀑布后新高两段)。它是"没保住本", 不是"没拦住灾" | data/replay_bars/US_DELL_2026-09-02.json (09:49-09:55 分钟级瀑布 470→453.5; 日低/日高) |
| 12 | 侧分类遥测已在位: `_be_close_telemetry.limit_side` (passive/active) 每笔净保本类出场已记录 — 议题二方案A 的判分基建现成 | pipeline.py:8409-8421 |
| 13 | replay 消费兼容: load_shadow_rows 按 inst_id→{type:row} 合并 (同 type 多行时末行胜出 = 最末次 reprice, 恰为执行口径); chase_cap_violated 判定按行**存在性** → 同指令多条违例行后向兼容, replay 零改动 | scripts/cf_pullback_limit_replay.py:30-75 |
| 14 | 注册表现状: 16 行 (9/1-9/2), 12 可配对, DELL 为**首个** chase_cap 违例; DEB-20260901 Phase C 门槛 n≥20, ~1-2 交易日可达 | data/shadow/pullback_limit_replay.jsonl |

## 三、议题一 (尺子): 违例升级被去重吞没

| 方案 | 内容 | 判断 |
|------|------|------|
| **A (荐)** | 去重键带 reprice 值 (`"chase_cap_violation:" + round(_new_entry,2)` 或等价) — 同值重报仍去重 (防同轮刷屏), 更差 reprice 记新行; `logger.warning` 移出去重门 (每次违例 reprice 都打, 仅 jsonl 行去重) | 尺子完整; 一行级改动; record-only 零交易行为变更 |
| B | 按 (inst_id, 轮次) 全记 | 更全但每轮 reprice 一行, 刷屏/文件膨胀 |
| C | 不动 ("少记无碍"论) | 反驳: n_chase_cap_violated 是 Phase C 预注册判据的证据列, 系统性少记 = 证据偏"违例罕见", 会误导 Phase C 判向 — 尺子不准比没有尺子更危险 |

## 四、议题二 (出场): 被动侧净保本限价 → marketable limit (8/20 方案A 的击穿分支补完)

**方案A (荐)**: 下限价单前侧判定从"仅遥测"升级为"执行分支" — 检测时限价已在**被动侧**
(LONG: 限价>检测价; SHORT 镜像。几何含义=击穿已发生), 则不挂保本限价, 改挂
**marketable limit = 检测价−垫** (config 新键, 建议默认 ~15bps): 立即成交且带地板,
砍掉"3s 死等→撤单→市价"的 ~12s 往返。主动侧 (贴线检测, 8/20 设计的正常几何) 行为
**完全不变**。复用 close_position(limit_price=) 既有管线; SL_BREAKEVEN / SL_TRAIL_LOCK /
PATIENT_EXIT_FILL 三退出原因同轨 (现限价保护集, pipeline.py:8406)。
- DELL 测算: 触发即挂 marketable (~472.5-473.0 成交) → 回收 ~$24, 净 ≈ −$25 (损失回收 63% 分量的主要部分)
- 几何论证: 被动限价唯一获益场景是"价格回摆到限价", 而它被触发的场景恰是快跌 — 家族 5 案 0 回摆成交

| 方案 | 内容 | 判断 |
|------|------|------|
| B | 被动侧直接市价 (不挂限价) | 最简; 但无地板, 市价滑点无界 (DELL 12s 往返吃 54bps) |
| A' | 影子先行: 被动侧仍走现状, 仅加遥测记 would-have marketable 价, N 天后裁 | 保守; 但侧遥测+家族 5 案已在, 边际证据增益低 |
| C | 不动 | 每次击穿案平均吐 ~$20-50; 8/20 修复后仍复现 (证据 10) |
| D | 事件驱动退出 (LV3 push 直触发退出检查, 消灭 20s 轮询间隙) | 治本 ($18.40 那份也能回收) 但工程量大, 本 DEB 仅挂账 (§七) |

## 五、实施细节 (裁决后执行)

1. 议题一: pipeline.py:4170 去重键 + warning 移位 (仅此两处); replay 零改动 (证据 13)。
   钉死测试: 同指令更差 reprice → 新行 + 新 warning; 同值 reprice → 仍去重。
2. 议题二: pipeline.py:8405-8426 段侧判定升级为执行分支; config 新键
   `breakeven_passive_marketable_pad_bps` (默认建议 15, 仅控垫宽)。测试: ①主动侧行为不变
   (回归红线) ②被动侧走 marketable 且地板=检测价−垫 ③SHORT 镜像 ④侧遥测照记
   (_be_close_telemetry 增记所用路径)。
3. 生效: 两处均属运行中服务文件 — 盘后编辑, 下次重启生效。

## 六、回滚

议题一: revert 即可 (纯记录侧, 零数据风险)。议题二: revert 提交即可 (config 键随提交删除)。

## 七、未做与诚实边界

- **入场侧牙齿 (轨3) 不在本 DEB**: DEB-20260901 Phase C 预注册路径 (n=12/20, 快达标),
  DELL 正是其 P0 假设 ("确认位限价入场优于市价穿越入场") 的教科书样本 (CF $0 vs −$49.30,
  证据 5)。提前上硬牙破坏预注册纪律 — 除非用户显式加速裁决。
- $18.40 分量 (轮询间隙击穿) 只有事件驱动退出 (方案D) 能治, 本 DEB 不解决, 挂账。
- 单轮开仓上限顺延与追高升级的复合效应 (21:37 顺延 → 21:42 更高价执行): 任何 reprice
  检查点方案 (Phase C 或未来牙齿) 天然每轮覆盖, 不单独立项。
- 判分全部以"出场时刻可达信息"为准, 无后视镜 (DELL 出场后先瀑布 432 后新高 497.99,
  均不改变当时决策正确性评估)。
- 样本为 SIM 环境; 实弹 (PLAYBOOK_LIVE) 同管线同判据。

## 八、验收判据

- 议题二 (毕业判分, 沿 _be_close_telemetry 既有按侧分组): 被动侧出场笔数与其净结局
  vs 家族基线 (5 案均值 ≈ −$19.7/案); 主动侧成交率/净结局不得回退 (回归红线)。
- 议题一: 升级违例入行数 (历史恒 0 → 预期 >0); n_chase_cap_violated 口径修正后
  DEB-20260901 Phase C 判据证据列复审。

## 九、补记: 全天轨迹与两个反事实边界 (2026-09-03, 用户回看收盘 497 引出)

全天 (bar 末时间戳口径, data/replay_bars/US_DELL_2026-09-02.json):
昨收 425.0 → 跳空 +8.90% 开 462.84 → 09:42 追高成交 474.305 (首段冲高局部顶)
→ 09:47-48 峰 480.525 → 09:49:06 出场 470.545 (首段瀑布局部底) → 瀑布续跌,
450 首触 09:58 → 日低 432.2651 @10:14 → 全天 V 型修复, TP 490 首触 15:32
→ 日高 497.9899 @15:44 → 收 492.2。日内振幅 65.7 点 (**15.2%**); 入场时 ATR≈5% (proxy)。

- **边界一 (V 型不给出场侧翻案)**: 现行出场纪律下不存在"入场 474.305 拿到收盘"的路径 —
  re-price 后初始 SL 450.56 恰在瀑布体内, 持仓现实反事实 = 09:52 前后 **−$285**,
  而非 +$260 (收盘价差)。V 型只加重入场侧 (证据 5), 不改变本 DEB 任一议题判分。
- **边界二 (DA 全语义反事实, 仅注记不计判分)**: 限价 450 / SL 432 / TP 490 — 若 TTL
  ≥ ~20min 则 09:58 于 450 成交, 日低 432.27 以 **27 分钱**之差未扫 SL, 15:32 TP 命中
  → 毛 +$480。两点诚实注: ① TTL 15min 是 DA 自身规格, 回踩迟到 ~5 分钟 → 预注册 CF
  仍是 expired_unfilled=$0 (TTL 窗口内最低 454.55, 见证据 5); ② 27 分钱止损幸存属
  刀锋运气, 单样本只证明论文方向, 不证明稳健优势。
- **判读**: 论文两端全对 (回踩来了/TP 命中), 执行两端全倒置 (买 09:42 局部顶,
  卖 09:49 局部底) — 证据 1 的"论文倒置"以时间戳坐实。
- **波动适配注记**: sim 采集期 CAUTIOUS 仓位乘数覆写 0.5→1.0 (live 将减半敞口),
  6% notional 上限与 5% SL 已知 ATR 高波动; 现无高 ATR 原型门 (低 ATR 门独存) —
  不立项, 仅注记。

## 九、执行记录 (2026-09-03, 用户裁"按建议执行", 双方案A 落地)

**议题一 (尺子)**: pipeline.py chase_cap 块 — warning 移出去重门 (每次违例 reprice 都打);
去重键改 `f"chase_cap_violation:{round(_new_entry,2)}"` (同值重报去重, 更差 reprice 记新行);
`_shadow_dedup_ok` 文件扫描侧补种值键 (重启后同值不误判首次); 行 type 保持裸值
"chase_cap_violation" (replay 按存在性判, 零改动, 证据13 成立)。

**议题二 (出场)**: pipeline.py 净保本限价块 — 侧判定从仅遥测升级为执行分支: 被动侧改挂
marketable limit=检测价∓15bps (`breakeven_passive_marketable_pad_bps`, yaml SSOT, 0=回滚旧行为),
主动侧一行未动; 遥测 `_be_close_telemetry` 增记 path/floor_px/pad_bps (毕业判分基建沿用);
8/18 注释"TRAIL_LOCK→结构性主动侧"假设打勘误标记 (已被 DELL 击穿证伪)。

**KR2 冻结联动**: 新键入退出参数哈希快照 (防漏键假绿, R-4b 纪律) → 新版本
`exit-de49b1299290` (剔除新键复算=旧基线 f837b002896d, 证明哈希位移仅此一键);
已走豁免通道登记 config_freeze_baseline.json + test_kr2_freeze_exemption 钉子同步。

**测试**: 新增 10 例 (议题一 3: warning 在门外/键内嵌值/值键去重功能级; 议题二 7:
分支把守+双向镜像/主动侧回归红线/垫0回滚/遥测三键/yaml SSOT/入哈希功能性/豁免登记闭环);
既有 test_chase_cap_telemetry_uses_parse_raw_semantics 窗口 2500→4200 适配块体变长
(断言意图不变); except:pass 棘轮约束 → 值键补种用 isinstance 免 except。
**全量 2402 passed / 0 failed** (基线 2386+新增 13+patient_exit 3 例归队)。

**生效**: 运行中服务持旧码, 下次重启窗口生效 (盘后编辑纪律); 议题一为纯记录侧零交易行为变更。
**判分**: 议题二毕业判分沿 §八 — 被动侧出场笔数与净结局 vs 家族基线 (-$19.7/案),
主动侧成交率/净结局不得回退; 议题一判据=升级违例入行数 >0 + Phase C 证据列复审。

## 十、议题二后续: 穿透判别器 + V型反转研究闭环 (2026-09-03, 用户裁"A"全量)

**缘起**: 用户问"击穿瞬间如何区分快速下跌趋势 vs 缓慢临时回撤(V型), 且不要引入混乱"。
答: 判别特征今天无人知道 (DELL n=1), 拍脑袋写阈值才是混乱 → 先 record-only 攒样,
LLM 出题代码判卷, 攒够样本走 DEB 裁决才动执行。

**落地三件套 (全 record-only, 零交易行为变更)**:
1. `app/playbook/penetration_shadow.py` — 穿透触发瞬间采集判别特征 (穿透深度bps/自峰值回吐/
   是否刷新低/持仓时长/时间带/ATR归一/侧/路径/quote_src) → `data/shadow/penetration_shadow.jsonl`;
   pipeline 净保本限价块 1 处埋点 (主/被动两侧都采, 异常仅 warn 留痕, 棘轮纪律合规)。
2. `scripts/penetration_outcome_backfill.py` — 每晚用离场后 1 分 bar 回填结局标签
   (recovered_1/3/5/10m=V型回摆判定 / continued_bps_10m / cf_floor_fill_saved_bps 反事实);
   幂等自愈 (默认处理全部未回填事件); 可见窗不足落 null 非 False (防假零口径)。
   cron 周二-六 06:05 北京 (早于 shadow_board 06:15)。
3. `scripts/penetration_llm_analysis.py` — 周频 (周六 07:30): 确定性基线统计永远可得;
   n≥8 才调 LLM (`penetration_pattern_analysis`, qwen→deepseek) 按 schema 提假设,
   每个假设确定性 replay 全样本判分; **毕业门控 n≥20 且 matched≥8 且 precision≥0.7
   → 自动开 DEB 草稿+飞书推送呈裁决** (阶段2 第三执行路径只在裁决后写码)。

**防遗忘 (用户明确要求)**: ① 接入 shadow_board 段11 (每日 06:15 飞书推送攒样进度/V型率);
② 攒样里程碑 n=10/20/30 飞书一次性提醒 (state 去重); ③ 毕业即自动开 DEB — 三级提醒,
不开新通知渠道。

**KR2**: 影子键零进 exit config 哈希, 版本复算仍 `exit-de49b1299290` 不变。
**测试**: test_penetration_shadow.py 14 例 (record-only 不变量/TEST守卫/方向镜像口径/
回填标签手工bar核验/幂等/判分器/源级契约/KR2红线/flag默认/看板注册);
**全量 2416 passed / 0 failed** (基线 2402+新增14)。
**生效**: 采集埋点随下次服务重启窗口生效 (盘后编辑纪律); 回填/分析环为独立 cron 进程即时生效。
**回滚**: `llm_features.penetration_shadow.enabled=false` 单键全关 + crontab 两行删除;
执行路径零改动无需回滚。

## 十一、§十核查补记 (2026-09-03, 用户令"核查给出建议和补充")

**P0 修复 (回填窗口端标 off-by-one, 已修)**: `label_one` 原窗口 `b[0][11:16] > et_hm` —
bar 为端标 (标签=分钟结束时刻), 标签 hm+1 的 bar 覆盖触发分钟本身, 其 high/low 大概率
**先于触发时刻** → 被动侧击穿样本被系统性误标 V-recovery (恰是本研究最关键人群, 偏向
"等回摆"一侧)。DELL 9/2 实事件实证 (et_hm=09:48, 地板474.6533, 真实触发 09:48:54):
原版输出 recovered_1/3/5/10m 全 True + cf_saved 85.5bps, 与同行 continued_bps_10m=−540
自相矛盾; 地面真值=触发后 10 分钟最高 472.36 < 地板 → 全 False。修复=锚点改触发分钟+1
(跳过触发分钟 bar, 分钟算术防跨小时), 修后 DELL 输出与家族 5 案 0 回摆地面真值对齐。
**修复先于任何生产标签发生** (jsonl 尚未创建, 埋点未随运行中服务生效), 无存量污染。
测试 14→16 例 (+触发分钟bar污染钉 / +跨小时锚点钉; partial 窗口断言 4→3 随口径),
test_penetration_shadow 16/16 通过。附带行为变化: 收盘尾段 (15:59) 事件原会拿
触发分钟 bar 标 1-bar partial, 现如实封账 no_bars_after。

**核查通过项**: 议题一/二实施 (§九) 与 DEB 规格逐项吻合, 值键去重+重启补种/
KR2 豁免闭环/勘误标注均验证; §十三件套 infra 全数在场 (yaml 路由 qwen→deepseek、
flag 默认 on、shadow_board 段11 入 build_board、crontab 两行、看板/里程碑/毕业门)。

**遗留观察 (未改, 备案)**: ① 平仓异常重试路径可对同一经济事件重复采集 (event_id=
时间戳秒级, 重试 20s 后即新 id) — 仅 dual.close_position 抛异常时发生 (罕见且有告警);
若 jsonl 中出现同 symbol 同分钟多行, 建议分析层按 (symbol, et_date, et_hm) 去重取首行。
② 毕业门自动开 DEB 草稿由 cron 周六写 docs/debates/ — 双 Agent 环境下无冲突机制,
文件名含日期无碰撞风险, 但草稿生效仍需用户裁决, 仅立案不实施, 风险可接受。

## 十二、§八 验收判据可执行性核算 + 改判 (2026-09-03 复盘批, 用户令"确认问题存在无误后按建议做完整修复")

**核算结论: §八 原判据不可执行 — 会在本世纪内一直"攒样中", 成为第二个悬置尺子。**

| # | 事实 | 出处 |
|---|------|------|
| 1 | 被动侧只占 BE 类出场 **13/101** (8/17 起): active 88 笔 (均 +41.08, 合计 +3614.77) / passive 13 笔 (均 −5.76, 合计 −74.85) | `trade_records.metadata_json._be_close_telemetry.limit_side` 分组统计 |
| 2 | 8/24→9/1 **九个交易日零 passive 事件**; 家族 5 案分布在 3 天 (8/18×1, 8/20×3, 9/2×1) ≈ 0.4 案/交易日 | 同上 + §二 证据 10 |
| 3 | 要在 passive 侧检出 ~$20/案 的改善 (结局 sd 量级 $20-40) 需 n≈50+ → 按 0.4/日 ≈ **125 交易日** | 算术 |
| 4 | 本日刚退役的 `DEB-20260903-trade-attribution-zombie-watch` 病灶正是"观察 2 周后定阈值"悬置 7 周未决 | 同 DEB §一 |

**改判 (预注册, 2026-09-03 起, 窗内禁改; 与判据字典 B17 同源同文)** — 三层, 各层有自己的时钟:

1. **机制判据 (逐事件即判, 不等 n)**: 被动侧击穿出场必须 0 次"3s 死等→撤单→市价";
   成交滑点 ≤ pad(15bps) + 5bps 容差; `_be_close_telemetry.path=marketable_limit` 逐笔可查。
   **回归红线**: 主动侧成交率/净结局不得回退 (证据 1 — 主动侧是利润引擎, 一行未动必须保持)。
2. **预注册回滚线**: 前 10 笔被动侧 marketable 成交 **或 9/30 结算 (取先到)** —
   若 ≥5 笔 `cf_floor_fill_saved_bps` > 垫成本 ∧ 中位节省 >20bps (即"等回摆"更优)
   → 提请裁 `breakeven_passive_marketable_pad_bps=0` 回旧保本限价行为;
   否则议题二转正, 家族基线 (−$19.7/案) 作废留痕。
3. **毕业门 (慢环, 判别器研究)**: n_labeled≥20 ∧ 某假设 matched≥8 ∧ precision≥0.7
   → 周六分析环自动开 DEB 草稿 + 推送呈裁 (仅立案, 写执行代码须用户裁)。
   **本层不是议题二的前置** — 议题二按机制证据 (0/5 回摆成交) 独立生效。

**§八 原文保留不删** (裁决留痕), 但其"被动侧净结局 vs 家族基线"一句自本日起按上述第 1/2 层执行。

## 十三、§十 三件套实施核查: 一处 P0 写读契约断裂 (已修) + 三处登记补齐 (2026-09-03 复盘批)

**P0 (已修, 修复先于任何生产标签)**: `penetration_outcome_backfill.py` 的 `label_one` 返回
`{"status": "labeled", ...}`, 原写法 `st = lab.pop("status")` 把 status 摘掉后才 `**lab` 落盘
→ 落盘的 labeled 结局行**没有 status 字段**, 而三个消费端都按 `type=="outcome" and
status=="labeled"` 选行: ①`penetration_llm_analysis.load_samples` (判分/毕业门 n_labeled)
②`shadow_board` 段11 (攒样看板 0/20) ③本脚本自己的 `v_recovery_5m_this_run` 汇总与逐行明细打印。
后果 = **研究环与防遗忘看板结构性静默零**: cron 照跑、jsonl 照长、看板照渲染, 但永远 0/20 不毕业
(与 AGENT.md 军规"常量退化"同类, 且恰是 §十 承诺的三级防遗忘全部失灵)。
修: `lab.pop` → `lab.get` (status 随行落盘, outcome 行 status 口径统一为 labeled/no_bars_after)。
钉死 7 例 (`tests/test_penetration_shadow.py` 第 6 组): writer↔analysis↔board 三方写读配对、
汇总非结构性零、载体心跳、注册表计数不双计、源级禁 `pop("status")`。全文件 23 passed。

**三登记补齐 (§十 承诺"防散落"但落地时三处全缺, 属 DEB-20260901 §十一 严令禁止的游离观察)**:
- ①注册表: `app/playbook/shadow_registry.py` 新增 `penetration_shadow` (OBSERVING, min_n=20,
  config_key=`llm_features.penetration_shadow.enabled`, eval_script=分析环,
  **count_mode=`where:type=penetration`** — 每事件 1 行 penetration + ≤1 行 outcome,
  用默认 `lines` 会把样本量双倍计数, 20 例门变 10 例真样)。`check_mounting()` 实测 0 告警。
- ②判据字典: 新增 **B17** (即 §十二 三层判据 + 基率诚实披露)。
- ③控制环路台账: §2.3 挂 `crontab-pen-backfill` (5 6 * * 2-6) / §2.4 挂 `crontab-pen-analysis`
  (30 7 * * 6), 首列沿 `crontab-<名>` 先例并写明载体脚本路径。
- **顺带闭合守卫盲区**: `tests/test_control_loop_ledger.py` 原先只核 Hermes `jobs.json`,
  系统 crontab 无守卫 — 实测 68 个被 crontab 调用的仓内脚本仅 2 个在台账, 穿透两环挂 cron 时
  零拦截即首例实证。现加棘轮: 存量 64 个锁 `docs/references/crontab-loop-baseline.txt`
  (带理由, 反向核对陈旧豁免), 新增环路必须入台账或显式加基线; 台账 §5 G4 与 §6.1 同步注记。
  存量 1:1 回填属专项 (REVIEW-20260903 §五 建议④), 不在本批。

**载体心跳 (稀有事件影子的黄灯误报防线)**: 穿透是稀有事件 (证据 2), "文件不存在"与"当期无事件"
对注册表产出核对长得一模一样 → 初生豁免期 (10 天) 过后必误报黄灯。回填 cron 每日 `SHADOW.touch()`
→ 此后 mtime 新鲜度 = 回填环活着 (断流 >14 天即告), 行数 = 样本量, 样本新鲜度看行内 `et_date`。

**cohort 注记 (判分口径)**: 9/3 起全部样本均为议题二 marketable 生效后 (post-fix cohort),
旧"被动限价死等"行为无实时样本 — 判别器研究因此**不能**用实时样本回答"该不该等", 只能审计
"不等"的代价 (`cf_floor_fill_saved_bps`), 这与 §十二 第 2 层回滚线正好同源。历史 13 条 passive
事件可由 `trade_records._be_close_telemetry` + `data/replay_bars` (1793 文件) 重建, 但须打
`reconstructed=true` 并与实时行分段判分 — **属另案, 本 DEB 不做** (见 REVIEW-20260903 §五 建议③)。

**仍挂账 (未做)**: ①$18.40 分量 (20s 轮询间隙击穿) 只有方案D 事件驱动退出能治 — 建议列为
10 月首项, 由本影子的 `depth_bps`/`continued_bps_10m` 分布提供设计输入; ②议题一修尺子后
`n_chase_cap_violated` 口径断裂 (违例计数会上升, 因升级违例不再被吞) — DEB-20260901 Phase C
判分时须按 9/3 前后分段, 不得解读为"违例变多"; ③Phase C 现 n=16/20, 预计 1-2 交易日达标,
建议提前备好裁决包 (取数脚本 + 三判据实算), 达标当天即可裁。
