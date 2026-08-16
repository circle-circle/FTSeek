# DEB-20260816: 全链路代码审查 — 未发现异常与风险清单

> **立案日**: 2026-08-16
> **状态**: 审查完成 (只读, 零代码变更); P0/P1 待裁决修复顺序
> **立案人**: Qoder (应用户要求: "对当前整个交易项目链路的代码 框架和逻辑进行review, 确认有没有未发现的异常 bug和风险问题")
> **关联**: DEB-20260816-short-generation-funnel (§十四 E1 诊断; 本案 #23 为其混淆变量);
>   8/11 JD 盘前锚定 bug (本案 #11 为其复发面); DEB-20260725-timezone-ssot (本案 #17 为残留);
>   DEB-20260815-stable-profit-feasibility (max_per_symbol 0.08 收紧, 本案 #19)
> **方法**: 226,095 行 / 723 文件。按交易链路分 5 域并行深审 (只读):
>   A=UE 扫描生成器 / B=pipeline 信号→指令→DB / C=执行下单链 / D=退出风控链 / E=横切面 (时区/配置/安全/sim-live)。
>   立案人对 9 个最高危项逐行读码复核。
> **证据等级**: ✅ = 立案人逐行复核实锤; ◐ = agent 读码确认 + 立案人抽查关键行; ○ = agent 读码报告, 待复核。
>   修复前对 ○/◐ 项须先按本文位置复现确认。

---

## 一、P0 — 实盘资金直接风险 (4 项)

### P0-1 ✅ sim-live 分歧: 切实盘后所有 TradingMode 风控闸门仍按模拟盘运行

**位置**: `app/core/trading_mode.py:79-85` + `app/web_dashboard/mode_switch_service.py:249-268` + `.env.us:21-29`

```python
# trading_mode.py _detect_mode: 只认 TRADE_ENV
trade_env = _read_env_file("TRADE_ENV", "1")
if trade_env == "0":
    ... return LIVE_*
return TradingModeEnum.SIMULATE          # 恒走此分支 (TRADE_ENV=1 固化)
```
```python
# mode_switch_service.py 2026-06-24 重构: 切实盘只写 broker 路由键
# "切换写 AUTO_TRADE_ACCOUNT + live_trial, 不再写 TRADE_ENV ... TRADE_ENV 保持 1"
write_env_key("AUTO_TRADE_ACCOUNT", "real")
```

**推理链**: 6/24 重构把 **broker 路由**迁到 `AUTO_TRADE_ACCOUNT`, 但 **风控模式判定** (TradingMode) 仍消费已声明 deprecated 的 `TRADE_ENV`。`.env.us:21` 注释"TRADE_ENV 现仅控制 DB 路由"与 trading_mode.py 实际消费矛盾 — 迁移只做了一半。
切实盘 (`AUTO_TRADE_ACCOUNT=real`) 后真实订单发出, 而 `TradingMode().mode == SIMULATE`:
- `app/risk/drawdown_control.py:297` `get_max_daily_loss_pct()` 取 simulate 5.0% 而非 live_conservative 2.0% → 实盘日亏熔断阈值放宽 2.5 倍
- `app/risk/pre_trade_gate.py:65-68,108` `is_live` 恒 False → 实盘限额缩放永不生效
- `app/pipeline/order_executor.py:1770-1777` equity≤0 实盘 fail-close 被绕过, 回落 `equity=100000.0` 假净值继续下单
- 仅 playbook 仓位有独立补偿 (`playbook_service.py:936` live_trial ×0.5), 风控闸门无补偿

**触发条件**: 任何实盘切换。**资金影响**: 高 (熔断/限额/净值三重防护同时失效)。
**修复方向**: broker 路由与风控模式判定统一到同一变量 (TradingMode 认 AUTO_TRADE_ACCOUNT, 或 mode_switch 同步写 TRADE_ENV 并接受 DB 切换代价)。一行级改动, 但须裁决 DB 路由语义。

### P0-2 ✅ 手动市价单的 SL/TP 保护被静默丢弃 (live+sim 双失效)

**位置**: `app/web_dashboard/order/executor.py:1156-1159` (触发链 `routes/trade.py:55` MARKET→price=None, `executor.py:1034-1037` 非限价单走 3s Timer 分支)

```python
_entry = float(order.price or 0)
_qty = int(order.quantity or 0)
if _entry <= 0 or _qty <= 0:
    return          # ← 无任何日志
```

**推理链**: 限价单路径靠 monitor loop 回填 `order.price=dealt_avg`(L1115-1117); 市价单 3s Timer 分支无此回填 → `_entry=0` → 静默 return, 早退在 SIMULATE DB-only 分支(L1218)之前, 连 `current_sl` 都不写。触发: 手动市价开仓带 SL/TP。后果: 实盘无 STOP 单、sim 无 DB 止损、ExitMonitor 无据可依 — 仓位完全裸奔。
(核心静默 return 已逐行复核; "市价单 price 必不回填"前提为 agent 读 routes/trade.py 确认, 修复前复现一次即可闭环。)

### P0-3 ◐ 幂等机制预烧 key + 60s TTL + 纯内存 → 重复下单

**位置**: `app/web_dashboard/order/executor.py:52-65,76-80,207-210`; `manual_order_service.py:86-87`

```python
if key in service._recent_keys: return True   # 拒绝
service._recent_keys[key] = now               # 先写入, 校验/下单结果未知
```

**推理链**: ① key 在 validate/connect/place_order 之前写入, place_order 网络超时 (broker 可能已收单) 后 60s 内同 key 重试被"重复提交"拒绝, 而实际成交无 order_id、无保护单、不进 OrderTracker (手动单从不 register); ② 60s 后同 key 重试或 key=None (前端可不传) → 第二笔真实下单; ③ `_recent_keys` 纯内存, 重启清空且从不查 broker 端已存在订单。
**对照**: `app/execution/idempotency.py:25-38` 已有 SQLite 持久化幂等层 (重启重载 1h 窗口), 但手动单链路未接入 — 修复即接入既有组件。

### P0-4 ✅ grace 期白名单 reason 失配 → 持仓前 45 分钟硬止损被压制

**位置**: `app/risk/position_exit_monitor.py:1374-1386` + `app/risk/atr_stop_engine.py:403`

```python
_GRACE_HARD_STOP_REASONS = {"ATR_STOP", "SL_HIT"}     # 白名单
# atr_stop_engine.py:403 实际发出:
reason_code, reason_cn = "HARD_STOP", f"硬止损({max_loss_pct*100:.0f}%)"
```

**推理链**: 引擎硬止损 reason 是 `HARD_STOP`, 不在白名单; `SL_HIT` 全仓库无生产者。legacy 路径下 `_in_grace` 期间 (min_hold_minutes=45) 硬止损结果不 append; 且 `registry_fallback_on_error=True` — registry 抛异常即回落 legacy。高波动股前 45 分钟实际可亏至 -5% 以上才由其他规则兜底。注释自称"P0 修复(2026-06-14): 硬止损始终放行"与实现相反。
**修复方向**: 白名单补 `HARD_STOP` (一行) + 补一个 reason 契约单测。

---

## 二、P1 — 退出保护与执行正确性 (10 项)

### P1-5 ✅ SL 契约抬线后不重判 triggered → 实际止损系统性宽于设计

**位置**: `app/risk/exit_rules/trailing_stop.py:164-183`

`compute_stop_lines` (atr_stop_engine.py:416) 先用旧 final_stop 判定 `triggered`, 随后 SL 契约把 final_stop 抬回 initial_sl 地板 (L177-183), **不重判**。触发链: 低波动股 initial_sl=entry-2.0×ATR (如 -1.4%), 当前生效线=hard_stop -2% 或 -2.5×eff_ATR (更宽) → 价格跌破 initial_sl 却未越过硬线 → `triggered=False` 永不退出。每次止损多亏 (initial_sl-final_stop), 双向对称。**与 P1-10 叠加 = 退出几何整体宽于 sizing 假设。**

### P1-6 ✅ 报价异常时 fallback 价 = 入场价 → 该标的退出保护失效

**位置**: `app/risk/position_exit_monitor.py:966-984`

```python
_prev_price = trail_state.get("prev_scan_price", entry_price)
_fallback_price = _prev_price if _prev_price > 0 else entry_price
current_price = _fallback_price
```

单标的报价失败 (price_cache 返回 None) → 用入场价当现价: pnl=0、profit_atr=0, 所有退出规则判"无风险"。有 critical+钉钉告警 (非全静默), 但监控继续空转直至报价恢复。`prev_scan_price` 仅开仓时写入成本价、扫描路径从不更新 (agent grep 确认, ○ 级子前提)。

### P1-7 ◐ 部分平仓成功后 `_pending_close` 永久残留 + 仓位改判 manual

**位置**: `executor.py:340-345,364` (成功路径只清失败分支 574-589 与全平 398-414 的标记)

`_meta['_close_in_progress']=True; _meta['_pending_close']=True` → 手动部分平仓成功后残留 `_pending_close=True`; 消费方 `position_exit_monitor.py:690` `if _meta.get('_pending_close'): continue` → 组合止损 (FLAT_ALL/WARN/REDUCE) 永久跳过; L364 `_mark_position_manual` → 单仓退出监控 (`is_strategy_managed` 排除 MANUAL) 也跳过。触发一次部分平仓即生效, 直至重启。

### P1-8 ◐ 限价平仓未成交即写假 exit_price/realized_pnl → 污染日回撤熔断

**位置**: `executor.py:436-460` + `pre_trade_gate.py:182-189`

限价止损/止盈平仓单挂单未成交 → 1s 后 dealt_qty=0 → 非 CANCEL 状态放行 → 用 cur_price 写 trade_records 为已平仓。`_check_daily_drawdown` 以 `SUM(realized_pnl) WHERE exit_price>0` 计算熔断 → 假盈亏错误触发/放过 3% 日熔断, 持仓与账本漂移。

### P1-9 ◐ modify_stop_loss: 撤旧单失败仍下新 STOP (仅实盘)

**位置**: `executor.py:897-934`

撤单返回失败无 return/无复查, 直接 place_order 新 STOP: 旧 STOP 已触发成交 (仓位已平) → 新 STOP SELL 以旧 qty 挂出 → 触发时开出裸空头; 撤单仅网络失败 → 同仓位两张 STOP, 双倍平仓+反手。

### P1-10 ○ ATR 倍数下限 2.5× 覆盖 SSOT 分桶 → 低波动股止损距离 +67%

**位置**: `trailing_stop.py:118-125`

exit_policy.yaml 无 `trailing_stop` 键 → 恒用代码默认 `{"normal":2.5,...}` → `atr_mult = max(atr_mult, regime_mult)` 恒 ≥2.5, 压过 guardian 波动桶 (1.5/2.0/2.5/3.0) 与 SSOT 2.0×。低波动股设计 SL=1.5×ATR, 实际 2.5×ATR, 而仓位按 2.0× 距离定尺寸 → 单笔风险系统性超预算。

### P1-11 ◐ UE 盘前锚定在回退源上锚昨收 (8/11 JD bug 复发面)

**位置**: `scripts/llm_universe_scan.py:230-237` + `app/data/market_data_router.py:348-379`

`pre_price` 仅 `futu_lv3_provider.py:1041` 写入; Futu 快照 miss 回退 Finnhub/TwelveData 时该键缺失 → 盘前时段 (inject cron 09:10 ET 在盘前窗内) 静默退回 `_quote_price` = 昨收。entry/SL/TP 全锚昨收, `anchor_dev_pct` 假漂移 → 误作废或漏拦。8/11 修复只盖住 Futu 主源。

### P1-12 ◐ drawdown_control 保留 >5x 自动重置后门

**位置**: `app/risk/drawdown_control.py:218-226`

单次脏资产读数 (账户错查/单位错) ratio>5 → 重置 baseline → 8% 总回撤与日限同时解除, 无二次确认。`capital_safety.py:202-216` 已于 6/23 判定同款逻辑 fail-dangerous 并删除 ("宁可误熔断"), 本文件未同步。

### P1-13 ✅ 富途交易解锁密码 MD5 硬编码

**位置**: `scripts/archive/close_all_options.py:53-59` (`password = '21eab4e033c379d31c61a4e64419028c'`)

即使 archive 脚本, 凭据对 sim/real 账户体系有效。处置: 移到环境变量 + 轮换密码 + 清 git 历史 (裁决)。

### P1-14 ◐ UE inject 与 DA bridge 读-写竞态 → UPSERT 覆盖 DA 指令

**位置**: `llm_universe_scan.py:1132-1142` (一次读排重) → `1306-1308` (数分钟后写); `db.py:239-247` UPSERT 按 (date,symbol) 无条件覆盖

DA bridge 08:30 ET 起 40min+ 与 UE inject 09:10 ET 窗口重叠; DA 在 UE 读后写前提交同标的 → UE 覆盖 DA 完整分析 (含反向方向), decision_id 归因断裂, 违反 L1128 自称的"DA 优先"。

---

## 三、P2 — 统计失真 / 死配置 / 可观测性 (13 项)

| # | 等级 | 问题 | 位置 |
|---|---|---|---|
| 15 | ✅ | SQLite 不支持 `UPDATE...ORDER BY...LIMIT 1` → 平仓归因 metadata **从不落库**, debug 级静默吞 | executor.py:554-557 |
| 16 | ✅ | `supply_conf_gate_short:55` 死配置: registry 缺省 `SHORT_min_confidence:65` 遮蔽 (BUY 无对应键故 55 生效) → 实际 BUY 55 / SHORT 65 不对称; 8/9 后无 VOTE 行属潜伏 | pipeline.py:1526-1536 + strategy_registry.py:31-34 |
| 17 | ◐ | 日回撤熔断时区错配: 北京 naive exit_time vs ET trade_date, 北京 00:00-12:00 的已实现盈亏错日 | pre_trade_gate.py:186 + audit.py:361 |
| 18 | ○ | 调度器强度门槛实际 50 (`risk.pre_trade.min_effective_strength_floor`), yaml `strategies.defaults.min_strength:60` 两键均死, 注释自称"唯一来源" | scheduler_core.py:1024-1035 |
| 19 | ○ | `max_per_symbol` 代码回退 0.25 vs SSOT 0.08 (yaml 丢失/解析失败时静默 3 倍单名敞口), 回退散布 4 处 | scheduler_triggers.py:506 等 + config_defaults.py:68 |
| 20 | ○ | BUY 日配额把 already_held 幻影执行 (execution_price=0 无成交) 计入 → 无真实成交却耗尽配额 | db.py:330-334 + pipeline.py:3309 + playbook_service.py:828 |
| 21 | ○ | 日志宣称跨日翻转"confidence 门槛+15"从未实现 (实际控制=降仓 50%), `_filter_audit["direction_flip"]` 双语义混桶 | pipeline.py:1500-1504 |
| 22 | ○ | generate 进程中断后幂等闸按"存在任意行"跳过 → 当日剩余候选永不生成, 无告警 | pipeline.py:1823-1827 |
| 23 | ○ | UE prompt 仍向 LLM 宣称"止损 2-5% (>6% 被准入硬拦)" — 该门 8/16 已禁用且生成器恒产 5.91/6.09% 永不满足 → 生成端被死规则持续扭曲, **是 E1 空头衰减诊断的混淆变量** | llm_universe_scan.py:114-115 |
| 24 | ○ | confidence 无 [0,1] 上界钳制, 0-100/0-10 量表异常值过 min_conf 门并抢占 max_targets 配额 | llm_universe_scan.py:731-734,753-757 |
| 25 | ○ | ftseek_us.sh stop/restart: pkill 匹配不到引擎 (cmdline 为 FTSeek-US 大小写) 却匹配脚本自身 → 自杀调用 shell, 引擎继续运行; status 对 tail 误报 | ftseek_us.sh:27-47 |
| 26 | ○ | 执行时 admission 复核漏传 stop_loss → SL 宽度判据执行侧永远空转 (入口有传) — max_sl_pct 重启用前必须补 | playbook_service.py:782-788 |
| 27 | ○ | `decision_conf` 列 (8/14 迁移) 仓库内零写入方, 728 行仅 18 非空 (均外部 DA gate 传入) → 消除测量误差的立项目的对 VOTE/TFB/UE/REENTRY 四路未生效 | db.py:146,237,269,308 |

另有低优先级 3 项: UE↔DA 去重口径分歧 (SKIP 行排除 vs 含入, ADR-019 相邻); ATR=0 候选误标 skip_reason="价格缺失" (instruction_generator.py:393); option DTE 用服务器本地日期 (option_risk_monitor.py:193, 告警级)。

---

## 四、核查无恙区 (负面结果留档)

- **SHORT 符号对称性**: atr_stop_engine / SL / trail / breakeven 阶梯 / scale_out ratchet / pre_close 分层多空分支全部对称正确 (D 域逐行)
- **Web 鉴权**: session+CSRF 同源校验+写接口限速+body 上限; SQL 全部占位符, 无用户可控注入点 (E 域)
- **并发基础**: WAL+busy_timeout=5000 (db_utils 全局 patch)、main.py flock 单实例、execution/idempotency.py SQLite 持久化幂等层正常
- **时区主链路干净**: 调度/开收盘判定/DB date/trade_date 均已 ET 化 (7/25 清扫有效), 残留仅 #17 与展示层时间戳
- **admission 无旁路**: 全仓唯一 INSERT 收口 save_instruction, 61/61 DA 行 raw JSON 含审计块
- **UE `_compute_numbers` 符号/单位/除零**: 除已知 6.09/5.91 结构碰撞 (已由 max_sl_pct=0 处置) 外无新错误; 方向枚举与 admission/db 契约一致

---

## 五、修复裁决请求 (优先级建议)

**预注册纪律**: 本案全部为代码修复 (非参数调整), 但涉及退出几何的修复 (P1-5/6/10) 须先回放历史持仓验证"修复后止损距离恢复设计值", 涉及生成端的 (#23) 与 E1 诊断解耦后再动, 避免多变量同动归因失效。

1. **切实盘前必须先修**: P0-1 (风控模式与 broker 路由统一) — 不修则实盘熔断/限额/净值三重失效; P0-2/3/4 同为实盘前置条件
2. **退出几何三联 (P1-5/6/10)**: 叠加意味着当前 sim 退出保护系统性宽于设计 → 污染所有历史校准证据 (含空头判据的均笔 PnL 基准)。建议合并一个 hotfix 分支 + 回放验证
3. **P1-11/14**: 锚定 bug 家族 + UE/DA 覆盖, 与 E1 诊断合并处理 (#23 提示 prompt 清理须同批)
4. **P2 打包**: 一个 hotfix 分支; #16/#18/#19/#21 属"配置宣称与实际不符"家族, 修法统一为"回退值与 SSOT 同步 + 启动时 diff 告警"
5. **P1-13**: 凭据轮换 (运维动作, 代码侧改环境变量)

**红线**: ① 修复不得顺带改任何阈值参数 (参数走 DEB 影子纪律); ② 每项修复附最小回归测试 (尤其 reason 契约、方向符号); ③ P0-1 涉及 DB 路由语义, 修复方案须用户终裁。

---

## 六、复核命令 (关键项可独立复算)

```bash
# P0-1: 三方交叉
grep -n "TRADE_ENV" .env.us app/core/trading_mode.py app/web_dashboard/mode_switch_service.py
# P0-4: reason 失配
grep -n "HARD_STOP" app/risk/atr_stop_engine.py app/risk/position_exit_monitor.py
# P2-15: SQLite 语法 (预期 OperationalError: near "ORDER")
python3 -c "import sqlite3; c=sqlite3.connect(':memory:'); \
  c.execute('CREATE TABLE t(id INTEGER, k TEXT)'); \
  c.execute('UPDATE t SET k=? WHERE id=? ORDER BY id DESC LIMIT 1', ('x','y'))"
# P2-16: 死配置
grep -n "min_confidence" app/core/strategy_registry.py app/playbook/pipeline.py config/unified_config.yaml | grep -i "short\|buy"
# P1-13: 凭据
grep -rn "21eab4e0" scripts/
```

## 七、审查覆盖声明 (局限)

- agent 深审域外文件仅抽查未全覆盖 (如 playbook_service 1729 行只读执行段); ○ 级发现修复前须先按位置复现
- `__pycache__`/tests/node_modules 未审; skill 侧 (hermes repo, workspace 外) 不在本案范围
- 动态行为 (竞态/时序类 P0-3/P1-7/P1-14) 为静态推理, 建议修复时先加观测日志确认触发频率

---
## 八、Hermes 独立复核 (2026-08-16 19:05): P0/P1/关键P2 逐行复验

### 8.1 复验结果汇总 (不信任描述, 逐行读码)

| # | Qoder 等级 | 复验 | 独立证据 |
|---|---|---|---|
| P0-1 | ✅ | **实锤 ✓** | `_detect_mode` 只读 TRADE_ENV (恒1→SIMULATE); mode_switch 切实盘只写 AUTO_TRADE_ACCOUNT; `.env.us:21-24` 注释与 trading_mode 消费矛盾。三处下游全部核实: drawdown_control L297 走 TradingMode().get_max_daily_loss_pct() (恒 simulate 档); pre_trade_gate L66-68 risk_multiplier 恒 sim; order_executor L1770 `is_live` 恒 False → equity≤0 回落 10万假值继续下单。仅 playbook_service L936 live_trial×0.5 有独立补偿。**结论: 切实盘=风控三重失效, 实盘前置必修第一名** |
| P0-2 | ✅ | **实锤 ✓** | executor L1156-1159 `_entry<=0 → return` 无日志; L1034 市价单走 3s Timer 分支且无 dealt_avg 回填 → SL/TP 静默丢弃 |
| P0-3 | ◐ | **实锤 ✓** (升级为实锤) | L52-65 check_idempotency 先写 `_recent_keys[key]=now` 后执行; 纯内存 dict; key=None 直接放行。与 app/execution/idempotency.py SQLite 持久层并存未接入 |
| P0-4 | ✅ | **实锤 ✓** | atr_stop_engine L403 发 `HARD_STOP`; 白名单 L1376 `{ATR_STOP, SL_HIT}` 不含; `SL_HIT` 全仓 grep 无生产者 (仅消费/注释引用) → grace 45min 内引擎硬止损被压制。注释自称"硬止损始终放行"与实现相反 |
| P1-5 | ✅ | **实锤 ✓** | trailing_stop L164-183: compute_stop_lines 先判 triggered, SL 契约随后抬 final_stop 到 initial_sl 地板, 无重判 |
| P1-10 | ○ | **实锤 ✓** (升级) | exit_policy.yaml 无 trailing_stop 键 (grep 零命中); L118-125 `max(atr_mult, regime_mult)` regime_mult 缺省恒 ≥2.5 → guardian 低波动桶 1.5×/2.0× 被压过 |
| #23 | ○ | **实锤 ✓** (升级, 与我今日变更直接交互) | L114 "止损距离须在 2-5% (>6% 被准入硬拦)" — 该门 8/16 已禁用 (max_sl_pct=0), 且 UE 生成器恒产 5.91/6.09% **永不满足 2-5% 要求** → LLM 被死规则持续扭曲。**这是 E1 空头衰减诊断的混淆变量, 且与 §十六"生成侧衰减仅-28%"的结论相容 (死规则压 SHORT 输出意愿)** |
| #26 | ○ | **实锤 ✓** (与今日变更直接交互) | playbook_service L782-788 执行时复核的 dict **不含 stop_loss 键** → admission SL 宽度判据执行侧恒空转。**今日 max_sl_pct=0 后无实际影响, 但该门若重启必须先补此键, 否则入口拦/执行放=双标** |
| P2-15 | ✅ | **实锤 ✓** | 复跑 DEB 命令: `OperationalError: near "ORDER": syntax error` — SQLite 确实不支持, 平仓归因 metadata 永不落库 |
| P2-16 | ✅ | **实锤 ✓** | strategy_registry L34 缺省 `SHORT_min_confidence:65` 遮蔽 yaml `supply_conf_gate_short:55`; BUY 无对应缺省键 → 55 生效。**实际 BUY 55 / SHORT 65 不对称** — 这是又一个对空头更严的隐性门 (虽 VOTE 通道 8/9 后无行, 属潜伏) |

### 8.2 复验中新增发现 (Qoder 未列)

**N1 — #23 与 §十六 的联动修正**: §十六 判定"生成侧真实衰减仅 -28%"时未考虑 #23 死规则的
持续压制 (prompt 要求 SL 2-5%, 生成器公式恒产 6.09% 永不满足 → LLM 学会少产/不产 SHORT 以
"避免废单")。**E1 诊断须把 #23 prompt 清理作为前置或同批变量, 否则归因混淆** (Qoder §五-3
已提示, 此处升级为硬前置: 不清理 #23 就跑 E1 = 在扭曲场里测基线)。

**N2 — P2-16 与空头漏斗案的并案关系**: SHORT conf 门 65 (registry 缺省) vs yaml 宣称 55 —
与 §十二 同属"对空头更严的隐性不对称", 虽 VOTE 通道当前无流量, 若未来 VOTE 复活即复发。
建议并入 P2 配置宣称家族 (#16/#18/#19/#21) 同批修, 修法 = SSOT 同步 + 启动 diff 告警。

### 8.3 修复顺序裁决意见 (回应 §五)

1. **P0-1 同意第一优先**, 但涉及 DB 路由语义 (TRADE_ENV 兼 DB 路由), 按红线③须用户终裁方案方向:
   (a) TradingMode 改认 AUTO_TRADE_ACCOUNT (一行, 但 DB 路由语义需同步明确) vs
   (b) mode_switch 同步写 TRADE_ENV 并接受 DB 切换代价。**建议 (a)**: broker 与风控同变量, DB 路由
   保持 TRADE_ENV=1 固化 (OKR 数据不丢), 语义最简。
2. **P0-2/3/4 同意实盘前置**。P0-4 修法 (白名单补 HARD_STOP + reason 契约单测) 零风险可先行。
3. **#23 prompt 清理 = E1 硬前置** (§8.2 N1), 且零执行风险 (仅 prompt 文本), 建议与 P0-4 同批立即做。
4. **退出几何三联 (P1-5/6/10) 同意合并 hotfix + 回放验证** — 复验确认三者叠加 = sim 退出保护系统性
   宽于设计, **§十三 反事实回放用的历史持仓即产生于该扭曲场, 但裁决方向 (不修复 DA 转化层) 不受影响**:
   扭曲是双向对称放宽 (多空同宽), 且 §十三 对照组是"未触发的观望标的" (无持仓路径), 结论稳健。
5. **红线全部同意**: 修复不改阈值参数 (参数走 DEB 影子)、每项附最小回归测试、P0-1 用户终裁。

### 8.4 复验方法声明
复验范围: P0×4 + P1-5/6/10 + #23/#26 + P2-15/16, 全部逐行读码或复跑命令; 其余 ◐/○ 项
(P1-7/8/9/11/12/13/14, P2-17~27) 未逐行复验, 维持 Qoder 原等级, 修复前按 §七 纪律先复现。

---
## 九、P0-4 + #23 落地记录 (Hermes, 2026-08-16, 用户批准)

**commit `b3a5b926`**, 两项零风险修复:
1. **P0-4**: `position_exit_monitor.py` legacy grace 白名单 `{ATR_STOP, SL_HIT}` →
   `{ATR_STOP, SL_HIT, HARD_STOP}`。registry 路径本就含 HARD_STOP (双路径行为统一)。
   契约测试 `tests/risk/test_grace_hard_stop_contract.py` 5 组:
   HARD_STOP/ATR_STOP grace 放行 ✓ + CHANDELIER/BREAKEVEN 仍压制 ✓ +
   结构契约 (legacy 白名单 ⊇ registry 硬止损∩引擎 reason) 防双路径再分裂。
   验证: 5/5 + 既有 exit 测试 21/21 全过。
2. **#23**: `llm_universe_scan.py` prompt 删除死规则分句 "止损距离须在 2-5% (>6% 被准入硬拦)"
   (门已禁 + SL 宽度由代码恒产 5.91/6.09% LLM 不控制, 该规则永不满足)。
   保留接飞刀 LONG 约束与回踩企稳优先。**E1 诊断的混淆变量已清除, E1 可在干净场跑。**

**生效**: #23 下次 UE scan cron 自动生效; P0-4 需服务重启 (systemctl, 周日无持仓窗口)。
**剩余待裁**: P0-1 修复方向 (a)/(b) 用户终裁; P0-2/3 实盘前置; 退出几何三联 (P1-5/6/10) 合并 hotfix。
