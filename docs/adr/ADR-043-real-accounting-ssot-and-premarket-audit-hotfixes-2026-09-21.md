# ADR-043: REAL 会计口径 SSOT 收口 + 盘前全链路复查修复批 (切实盘预备)

- **状态**: 已实施 (2026-09-21)。两笔提交均已部署生效：
  - `483f7375f` [切实盘·会计收口]（11:38:13 CST 重启生效，MainPID 2233634）
  - `eb8cd1301` [hotfix] 盘前全链路复查批（12:13:13 CST 重启生效，MainPID 2238706）
  系统仍为 simulate（AUTO_TRADE_ACCOUNT=sim, ALLOW_REAL_TRADING=False），未切实盘、未下单。
- **主记录**: `handoff.md` 2026-09-21 四条增量（10:43 / 11:38 / 12:16 / 下午裁决落地批）
  + `docs/analysis/2026-09-21-realtime-price-authority-audit.md`（RFC-092f）
  + `docs/analysis/2026-09-21-ue-thinking-eval-archive-note.md`。
  本 ADR 只固化决策与理由（兑现 `eb8cd1301` 的 [hotfix] "事后补 ADR" 承诺，按 ADR-038~042 体例）。
- **立此 ADR 的缘由**: `eb8cd1301` 触碰交易主路径文件（broker_adapter/dual_broker/exit_lifecycle/
  position_sync/pre_trade_gate 邻域），commit-msg 治理钩子按词表拦截要求 [ADR/RFC/hotfix] 标签；
  当日盘前窗口内以 [hotfix] 通过（用户令「盘前解决明显异常/bug/冲突」授权），本 ADR 事后补齐决策记录。

## 背景（病灶族）

切实盘（REAL, HKD 综合账户）预备期实测暴露一族"**口径分散**"病灶：

1. **币种折算五处各自为政**：broker_adapter / DataService / dashboard / trade 容量 / position_sync
   各写一套 HKD→USD 逻辑，字段取舍互不一致；REAL 实测曾致净值高估 +9.68%（usd_assets 毛侧当净值）、
   cash 错读 8.6 倍（us_cash 未交叉校验）、"USD 净值 − 本币债务"混币。
2. **综合账户跨市场泄漏**：REAL 综合账户 position_list_query 返回港股行；`filter_trdmarket=US`
   不能推断行级市场。HKD/USD 浮盈直接相加会污染日回撤熔断（3% 线）。
3. **账户探测静默降级**：多账户未显式指定时自动选第一个 —— REAL 下选错账户 = 真金白银下错户。
4. **转换器输出被部分丢弃**（审计发现，P0-1）：`key in result` 条件 overlay 使 long_mv/fx/raw_currency
   等 8 个字段静默丢失 —— 契约违反，切实盘后新消费方会踩坑。
5. **同类防御缺口**：dual_broker.get_positions 无 US. 过滤（靠消费方精确匹配巧合安全）、
   exit_lifecycle.preflight 无跨市场守卫。

## 决策

1. **单一转换器 SSOT**：`app/execution/futu_accounting.normalize_futu_account()` 为 Futu accinfo
   币种转换唯一入口；FX 用字段自洽式 `usd_assets/(total_assets − 同币种子账户资产)`（联系汇率兜底），
   cash 与 us_cash 交叉校验（≤1% 才采信券商字段），debt/利息/保证金/功率全折 USD，
   无法定谳则 raise（消费方 fail-close 返回 {}）。SIM（currency='N/A'）走直通路径零变化。
2. **跨市场隔离按 code 前缀**：所有持仓消费路径统一 `US.` 前缀过滤（broker_adapter /
   dual_broker / exit_lifecycle / pre_trade_gate 浮盈统计 / position_sync 三层防御）。
3. **账户探测 fail-close**：多账户未显式指定 / 指定 ID 不存在 / REAL 无账户 → 阻断连接，
   不再静默选第一个。
4. **accinfo 透出 `account_scope="global"`**：显式提醒综合账户总口径 ≠ 美股策略专用权益；
   美股下单容量优先 `usd_net_cash_power` / `us_avl_withdrawal_cash`。
5. **[hotfix] 批 8 处防御性收口**（详见 handoff 12:16 增量）：overlay 全量覆盖、dual_broker/preflight
   隔离、2 处裸 datetime.now() fallback 翻 ET（棘轮基线 445→435）、trade_mode 值域归一
   （'sim'→'simulate'，含存量 8 行数据修复，先备份）、signal_fusion YAML 键登记（=代码兜底值）、
   测试 schema 补 lazy-ALTER 列。

## 验证与回滚

- 测试：209（会计批）+ 555/87/11（hotfix 批，0 failed）+ 新增回归防护 8 例；冒烟 7/7；
  `live_switch_audit.py --assert-ready` exit=0；重启后观察期零 ERROR。
- 回滚：两笔提交独立可 revert；trade_mode 数据修复有前置备份
  `data/backups/manual/ftseek_us_pre_trademode_fix_20260921_121215.db`。

## 未决 / 移交

1. **切实盘前必须实证** Futu `position_list_query.pl_val`（浮盈亏）币种口径为 USD——
   pre_trade_gate 日回撤依赖，混币会致 3% 熔断约 7.8 倍误敏感（SIM 不受影响）。已列 handoff 待验清单。
2. `AUTO_TRADE_ACCOUNT` 直读散落 18+ 处绕过 TradingMode SSOT（零行为差异，维护风险）——
   迁移列入 backlog，优先 order_executor / position_sync。
3. 存量测试腐化：`test_exit_recovery`(8)/`test_breakeven_shadow_c`(5)/`test_trade_recorder`(2)/
   `test_daily_llm_token_report_da_direct`(1) 直跑失败（基线 48ba2043e 同状，非本日引入）；
   根因同 config_version 一族——测试 schema/mock 与生产 lazy-ALTER 漂移。待专项批清理。
4. ETH 链切实盘前置三项未接线（REAL 购买力/可借量硬门、STOP 单实盘语义实测、ETH 退出延迟建模）
   ——见 `live_switch_audit.py` 输出。
