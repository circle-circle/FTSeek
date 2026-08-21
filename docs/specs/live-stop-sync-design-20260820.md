# Live STOP 单与保本线同步设计（方案 C 实盘预留）

> 状态：设计稿（2026-08-20，用户裁"方案C的实盘预留很有必要，提升做好设计准备"）
> 定位：live 毕业准入项之一。验证期内**零代码改动**，仅预注册设计与判据。
> 前置：方案 A（commit 90f5a230）已上线 — 保本触发线+滑点垫，规则引擎侧的净保本。
> 本文解决的是方案 A 覆盖不到的场景：**服务断连时，保本/锁利承诺依然物理生效**。

## 一、问题陈述

规则引擎（`monitor_playbook_sim_exits`，20s 轮询）是退出的唯一大脑。它活着，方案 A 的
净保本成立。**它死了（进程崩溃/OpenD 断连/主机故障），保本线只存在于内存与 DB，
broker 侧没有任何订单执行它。**

现状缺口（file:line 实查）：

| # | 事实 | 锚点 |
|---|------|------|
| G1 | live 成交时已挂初始 STOP @ initial_sl（-5% 兜底） | `playbook_service.py:1521-1528` |
| G2 | **武装/trailing ratchet 后 STOP 价不上移** — 止损停在 -5%，浮盈回吐不受保护 | 全链路无 modify 调用 |
| G3 | 规则引擎正常退出后**不撤 STOP** — 残留 STOP 可在价格再度下探时二次触发 | 退出路径无 cancel |
| G4 | sim 不支持 STOP（代码显式跳过），本设计仅 live 生效 | `dual_broker.py:649-651` |
| G5 | position_sync 外部平仓检测已存在（broker 主动平仓→实价建 trade record+归因） | `position_sync.py:1480-1607` |

G1+G5 已就位；**要补的是 G2（跟随）与 G3（回收）**。

## 二、目标架构

STOP 单定位为**断连保险丝**，不是主退出通道：

```
正常态:  规则引擎(20s轮询) 主导退出 → 方案A净保本限价单 → 成交后撤STOP(G3)
断连态:  broker STOP 接管 → 触发市价成交 → 服务恢复后 position_sync
         外部平仓路径(G5)对账: 实价建record+归因+推送
```

两条路径共用同一价格源：`effective_sl`（ratchet 单调上升 → STOP 只跟涨不跟跌）。

## 三、三阶段实施（每阶段独立可回滚）

### 阶段 1：STOP 随 ratchet 上移（修 G2）

- **触发点**：monitor 循环内，`effective_sl` 较 `meta["broker_stop_price"]` 上移
  ≥5bps 时（节流，防高频改单），调 `ctx.modify_order(MODIFY, order_id, new_stop)`。
- **状态持久化**：`broker_stop_order_id` / `broker_stop_price` 已写入
  trade_records.metadata_json（`post_trade_hook.py:184-200` 同款模式，playbook 侧补）。
- **失败语义**：modify 失败 → warning + 下轮重试；连续 3 轮失败 → CRITICAL 推送
  （保险丝失效必须可见，不可静默）。
- **幂等**：每轮先 `order_list_query` 确认 STOP 存活且价位落后，再改。

### 阶段 2：退出后回收 STOP（修 G3）

- **规则引擎任意原因平仓成功后**（close_res.success=True），撤对应 STOP：
  `modify_order(CANCEL, order_id)`。撤单失败 → 持仓已 0，STOP 触发只会报
  "持仓不足"被 broker 拒单，但残留挂单是脏状态 → 重试至撤净 + 落审计。
- **服务重启恢复**：启动时对所有 open_position 查 STOP 状态（order_list_query），
  缺失则按当前 effective_sl 补挂（重启即失忆的止损必须自愈）。

### 阶段 3：跨日与演练（毕业前最后一环）

- **STOP 是当日单**：隔夜仓必须在每个交易日开盘前重挂（按当前 effective_sl）。
  挂载点：premarket 流程，在触发监控启动之前。
- **断连演练**（毕业准入测试）：人工 kill 服务 → 价格打穿 STOP → 恢复后验证
  position_sync 外部平仓对账：trade record 实价、归因、推送三件套齐全。
- **双触发竞态**：规则引擎与 STOP 同时动作的窗口（引擎下单瞬间 STOP 也触发）→
  以 broker 成交回报为唯一真相，position_sync 按 dealt_qty 去重（既有机制，需测试覆盖）。

## 四、风险登记

| 风险 | 等级 | 缓解 |
|------|------|------|
| STOP=触发市价，成交有滑点 | 中 | 物理必然，与方案A降级市价同性质；保险丝的职责是"保证出场"不是"优雅出场" |
| modify_order live 侧行为未实测 | 中 | 阶段1上线前用 1 股小额真单验证 modify 语义（改价/撤单各一次） |
| STOP 残留导致裸卖 | 高 | 阶段2撤单失败重试+审计；broker 拒单兜底（持仓不足） |
| 高频改单被限速 | 低 | 5bps 节流 + 每轮至多一次 |
| sim 无法预演 STOP 路径 | 中 | sim 由规则引擎全权负责（现状）；STOP 逻辑用契约测试+断连演练覆盖 |

## 五、预注册判据（毕业准入，不过不放行）

1. STOP modify 成功率 ≥99%（连续 5 个交易日）
2. 规则引擎退出后 STOP 残留率 = 0（启动对账连续 5 日零残留）
3. 断连演练：外部 STOP 成交 → position_sync 对账三件套（实价 record/归因/推送）100% 命中
4. 双触发竞态测试：无重复平仓、无裸卖单
5. 隔夜仓 STOP 重挂覆盖率 100%（开盘前对账）

## 六、与验证期纪律的关系

- 本文全部实施项属**机制变更**，排队 8/29 终裁后，且仅在终裁输出为 GO
  （毕业路径）时启动；PIVOT/STOP 则冻结为遗产文档。
- 验证期内唯一允许的动作：断连演练的**只读预演**（kill 服务观察 position_sync
  对账，不挂真 STOP）——属白名单"攒样本"。
- 回滚总开关：`playbook.live_stop_sync_enabled: false`（三阶段共用，一键回退到
  "仅入场初始 STOP"现状）。

## 七、断连演练只读预演记录（2026-08-21，验证期白名单动作）

**方法**：kill -9 主进程（模拟崩溃，非正常停止）→ 观察 systemd 自愈 + 恢复后对账。

| 步骤 | 结果 |
|---|---|
| kill -9 前状态 | active, Main PID 834656, 运行 32min |
| systemd 自愈 | Restart=on-failure + 30s → 17:06:52 自动拉起（kill 于 17:06:22，实际 ~30s） |
| 恢复后 traceback | 0（journalctl + ftseek_us.log 双源核验） |
| 恢复后对账 | broker 2 vs 内部 2 · 漂移 0 · 外部持仓 0 ✅ |
| 订阅恢复 | QUOTE/K_1M/ORDER_BOOK/TICKER 4 只自动重订阅，Ticker/OrderBook/Push 数据到达 ✅ |
| 恢复后巡检 | 盘前检查通过 + 盘后检查通过（Signal IC 0.1116, N=1102）✅ |

**结论**：G5 对账链的前置条件（崩溃自愈 + 持仓对账）实弹验证通过。
**新发现（设计稿补充风险点）**：`Restart=on-failure` 意味着 **SIGTERM 正常停止
（systemctl stop）不会自动重启**——若运维误操作 stop 后忘记 start，服务静默缺席。
建议毕业前评估改 `Restart=always` 或加独立 watchdog 进程（排队，不在验证期动）。

**演练边界（如实标注）**：
- sim 环境无 STOP 单，无法预演"STOP 触发 → position_sync 外部平仓三件套"全链；
  该链只能 live 小额真单验证（风险登记第 2 行已预注册）。
- G2/G3 代码缺口未动（设计稿纪律：验证期零代码改动，排队 8/29 终裁后）。

## 八、关联

- 方案 A commit 90f5a230（规则引擎侧净保本：触发线+滑点垫+限价超时）
- `DEB-20260820-architecture-quant-dual-audit.md` B2.4（三态状态机=毕业通道最脆环节）
- `place_protective_stop`（`dual_broker.py:638`）/ `place_stop_order`（`broker_adapter.py:583`，
  旧路径，post_trade_hook 用，与本设计同语义可收敛）
