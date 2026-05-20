  Scalping 与主线策略协同审核

  一、两套策略的本质属性（先讲清楚边界）

  ┌───────────┬─────────────────────────────────────────────────────────────────────────────────────────┬───────────────────────────────────────────────────┐
  │   维度    │                         主线（IntradayScorer + Day Trade Mode）                         │                     Scalping                      │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ 策略类型  │ 动量/趋势跟踪（lifecycle accelerating, MA 突破）                                        │ 均值回归（RSI 超买卖、VWAP 偏离、布林带触边）     │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ 入场逻辑  │ 6 因子加权 + LLM 投票（大决策）                                                         │ 3 信号投票（RSI/VWAP/Boll）+ 5s polling           │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ 决策频率  │ 5–10min/symbol                                                                          │ 5s/symbol                                         │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ 持仓时间  │ 30min–72h                                                                               │ 4–8min                                            │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ TP/SL     │ TP=3% / SL=5% (R/R 0.6) ⚠                                                               │ TP=1.2% / SL=0.7% (R/R 1.7)                       │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ 资金预算  │ max_total_position=80%                                                                  │ budget_pct=30%                                    │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ 退出引擎  │ ExitMonitor 6 规则（含 PRE_CLOSE_EXIT 15:45）                                           │ FixedExit/TimeExit/ATRExit + force_close_at=15:55 │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ 风控入口  │ RiskGateway（BattlePlan→USRiskPipeline→RiskEngine）                                     │ ScalpingCircuitBreaker（自有，独立）              │
  ├───────────┼─────────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ 共享 SSOT │ position_store / _open_position_lock / RiskStateMachine / ExecutionEngine / data_router │                                                   │
  └───────────┴─────────────────────────────────────────────────────────────────────────────────────────┴───────────────────────────────────────────────────┘

  专业判断: 这是一个 "动量主线 + 均值回归卫星" 的常见组合。在量化产业界（Renaissance、Two Sigma 都有类似结构），理论上是互补的 ——
  趋势策略在盘整期亏损、均值回归策略在盘整期最赚；趋势策略在突破期最赚、均值回归策略在突破期亏损。

  但前提是两者的"信号源 / 持仓 / 资金"必须严格隔离或显式协调。当前实现存在 6 处结构性冲突，会让两者从"互补"变成"互打"。

  ---
  二、6 处结构性冲突（按危害排序）

  ⚠ C1. 资金预算合计 110% — 模拟盘可能成立，现金账户必爆 ⭐

  事实:
  - unified_config.yaml:1103: scalping.budget_pct: 0.3（30%）
  - unified_config.yaml:48-49: risk.MAX_TOTAL_POSITION: 0.80（80%）
  - 主线 + scalping 同时打满 = 110% 总资金

  专业判断:
  - 期货/保证金账户：杠杆能撑，但 PDT/Reg-T 监管会查
  - 现金账户/IRA：直接 broker reject，scalping 后开仓失败，但 budget 已经"账面占用"→ 数据漂移
  - 模拟盘没爆是因为 Futu OpenD 不严格校验，实盘必爆

  修复:
  # unified_config.yaml
  risk:
    MAX_TOTAL_POSITION: 0.65          # 从 0.80 → 0.65 (主线)
  scalping:
    budget_pct: 0.25                  # 从 0.30 → 0.25 (scalping)
    # 0.65 + 0.25 = 0.90, 留 10% 缓冲应对滑点和 buying_power 波动
  更彻底的做法：建立 CapitalAllocator 单例，强制 main + scalping + reserve = 1.0，违反即拒绝下单。

  ---
  ⚠ C2. ExitMonitor 不区分 strategy_id — Scalping 仓位被主线规则接管 ⭐⭐

  事实: position_exit_monitor.py:225 for position in positions: 无 strategy_id 过滤。
  Scalping 通过 _register_in_position_store(strategy_id="scalping") 写入后，主线的 ExitMonitor.scan_positions 会把它当成普通仓位处理：
  - _check_day_trade_exit 会检查 _should_use_day_trade_mode()（exit_policy.yaml:213）→ scalping 持仓不属于 ETF 也不在财报期 → 走 day_trade_mode 退出
  - day_trade_mode TP1=+3% / SL=-5%
  - 但 scalping 自己的 FixedExit TP=+1.2% / SL=-0.7%

  两套退出引擎并行扫同一仓位 → 谁先触发谁先平：
  - Scalping 5s 轮询通常先触发自己的紧 TP/SL（健康）
  - 但偶尔 ExitMonitor 60s 轮询遇到大 ATR 波动 → 触发 chandelier 把 scalping 仓位提前平掉
  - 退出原因不可预测，无法做 attribution

  专业判断: 这是最严重的设计缺陷。两套退出引擎管同一笔单，等于两个司机同时握方向盘。

  修复:
  1. 在 ExitMonitor.scan_positions 开头增加过滤：
  def scan_positions(self, positions, ...):
      # ↓ 新增: 跳过 scalping 仓位 (由 ScalpingEngine._monitor_loop 自管)
      main_positions = [
          p for p in positions
          if (p.metadata or {}).get("source") != "scalping"
          and getattr(p, "strategy_id", "") != "scalping"
      ]
      for position in main_positions:
          ...
  2. 在 ScalpingEngine._monitor_loop 中也对应过滤（只管自己注册的）
  3. 同时给 Position 模型增加 owner_engine: str 字段（"main" / "scalping" / "manual"），任何退出引擎只允许操作自己的仓位

  验收:
  - 单测：构造一个 metadata={"source": "scalping"} 的 position 喂给 ExitMonitor.scan_positions → 不被处理
  - 实盘 1 周：daily_review 中 exit_reason=DAYTRADE_* 的仓位 100% 应该来自主线，0% 来自 scalping

  ---
  ⚠ C3. 反向冲突无检查 — 主线 SHORT + Scalping LONG 可能同时存在 ⭐

  事实: scalping/engine.py:2575 if ps.has(symbol): return 只检查 是否有任何方向的仓位，不检查方向。
  但 _register_in_position_store 会跳过已存在的 symbol → 实际上 scalping 不会在主线已有仓位时入场。

  问题在反向：
  - T0：scalping 5s 周期开 NVDA LONG（RSI=22 触发）
  - T0+30s：主线 IntradayScorer 评分判定 NVDA SHORT（lifecycle=declining + 动量恶化）
  - 主线检查 position_store.has("NVDA") → True → 跳过开仓
  - 结果：主线信号被 scalping 屏蔽了

  反过来：
  - T0：主线开 NVDA LONG (持仓 30min)
  - T0+30min：NVDA RSI 飙到 78（超买）→ scalping 想做 SHORT
  - scalping 检查 ps.has("NVDA") → True → 跳过
  - 结果：scalping 错过了反转信号

  专业判断: 这是"先到先得"模式的典型缺陷。在量化产业界，这种情况通常用 "signal_priority" 或 "strategy_quota" 解决：
  - 给每只股票分配 "策略席位"（A: trend, B: scalping, AB: both）
  - 入场前先查席位归属
  - 同时持有同方向仓位时，scalping 可以叠加（按风险预算）；反向时禁止

  修复（最小可用版）:
  # 在 scalping/engine.py 入场前
  def _can_open(self, symbol: str, direction: str) -> Tuple[bool, str]:
      ps = self.risk_engine.position_store
      existing = ps.get(symbol) if ps.has(symbol) else None
      if not existing:
          return True, "no existing position"

      existing_dir = existing.direction.value
      existing_owner = (existing.metadata or {}).get("source", "main")

      # 同方向、不同策略 → 允许叠加（但有风险预算限制）
      if existing_dir == direction and existing_owner != "scalping":
          if self._can_pyramid(existing):
              return True, "pyramid main position"
          return False, "main holds same direction, scalping skip"

      # 反向 → 禁止
      if existing_dir != direction:
          # 触发对冲告警: 信号矛盾
          logger.warning(f"[Scalping] {symbol} 反向冲突: existing={existing_dir} owner={existing_owner}, scalping想做{direction}")
          return False, f"opposite to existing {existing_owner} {existing_dir}"

      return False, "duplicate"

  并在主线侧对应增加：发现 scalping 持有但方向相反时 → 写入 signal_disagreement.jsonl 供盘后归因。

  ---
  ⚠ C4. Time Gate 不同步 — 收盘前 10min 行为冲突

  事实:
  - Scalping closing_drive: force_close_at=955 (15:55)，no_new_entry_after=950 (15:50)
  - 主线 pre_close_exit: window_minutes=15 → 15:45 开始触发
  - 主线 min_hold_minutes_before_pre_close=30

  冲突场景: 15:35 主线开仓 NVDA → 15:50 主线 PRE_CLOSE_EXIT 想触发但被 min_hold 30min 卡住（只持有 15min）→ 16:00 收盘后留过夜
  但同一时段 scalping 已经 15:50 之后不开新仓、15:55 强平所有

  专业判断: 主线对盘末仓位的处理太"温柔"，scalping 太"激进"。两套系统的 EOD 协议没有协同。

  修复:
  1. 统一收盘协议（unified_config.yaml）：
  risk:
    end_of_day:
      no_new_entry_after_et: "15:50"  # 全局适用 (主线 + scalping)
      force_close_loss_at_et: "15:55" # 亏损仓位强平时间
      overnight_threshold_atr: 1.0    # 浮盈 > 1ATR 才允许过夜
      min_hold_minutes_before_eod_exit: 0  # 取消 30min 保护
  2. 主线 pre_close_exit 改为读这个全局配置
  3. Scalping closing_drive.force_close_at 同步从这里取

  ---
  ⚠ C5. 信号源各自为政 — 没有任何"反向信号融合"

  事实: 两套引擎完全独立计算。同一只 NVDA 在同一秒可以同时收到：
  - 主线 IntradayScorer：LONG, strength=72, confidence=0.68 (lifecycle=accelerating)
  - Scalping SignalDetector：SHORT, signals=3 (RSI=78, VWAP+1.5%, Boll 上轨)

  这两个信号都是对的（强趋势中超买仍可继续涨；同时也有回调风险）。但它们指向完全相反的操作。

  专业判断: 这是信息浪费。Scalping 检测到的"超买回归"信号，对主线来说应该是部分止盈的强提示；主线检测到的"动量加速"信号，对 scalping 来说应该是抑制做空的提示。

  修复（中级）:
  建立 SignalConflictBus：
  class SignalConflictBus:
      """两套引擎共享的信号公告板, 仅作 advisory 用途, 不强制干预"""
      def publish(self, source: str, symbol: str, direction: str, strength: float, ts: float): ...
      def query_recent(self, symbol: str, max_age_s: int = 300) -> List[Signal]: ...

  # 主线在生成 LONG 信号前查 scalping 是否有 SHORT 强信号 (RSI>78)
  # → 不阻止入场, 但调低 confidence × 0.85 (反向警示)
  # → 同时记录到 trade_attribution 的 "signal_disagreement_at_entry" 字段, 供归因

  # Scalping 在生成 SHORT 信号前查主线最近 5min 是否有 LONG 强信号
  # → 同样不阻止, 但调小仓位 × 0.7

  这是顶级量化系统的标配——多策略不应该完全隔离，应该共享一个 signal layer 做"软投票"。

  ---
  ⚠ C6. 退出归因混乱 — 同一笔仓位多个 exit_reason

  事实: 由于 C2 + C4，一笔 scalping 仓位可能：
  - 触发 scalping 的 FIXED_TP（+1.2%）平 50%
  - 剩余 50% 被 ExitMonitor 触发 DAYTRADE_PROFIT_LOCK 平掉
  - trade_records 写入两条 exit_reason，归因彻底乱套

  修复: C2 修复后自动解决（每条仓位由唯一 owner 处理）。

  ---
  三、各自策略的内部优化建议

  主线（IntradayScorer + Day Trade Mode）

  已在 OPTIMIZATION_REPORT_2026-05-12.md 详细列出。重点：
  - F10 风险单位 R 统一
  - F11 Day Trade TP 重设（R/R 0.6 → 2.67）
  - F16 thesis-driven exit

  Scalping 内部建议

  S-1. min_signals=3 是隐式 AND 门，过紧

  unified_config.yaml:1121 min_signals: 3 —— 必须 RSI + VWAP + Bollinger 三个都同向才入场。
  均值回归策略的真正价值是捕捉短期失衡，三信号必须共振 = 错过大量机会。

  优化:
  - 改为加权投票（每个信号 0/0.5/1 分），总分 ≥ 1.5 即入场
  - 增加 signal_strength 字段，1.5/2/2.5/3 分对应不同 position_pct（强信号大仓位）

  S-2. signal_cooldown_sec=300 与 polling_interval=5s 严重不匹配

  5s 一轮 → 一分钟 12 次扫描 → 每个 symbol 5min 内最多入场 1 次。
  但均值回归策略在剧烈震荡日（如 FOMC 决议日）应该能多次进出，5min 冷却把这种 alpha 全砍了。

  优化:
  scalping:
    signal_cooldown_sec: 60      # 1min 冷却 (替代 300)
    daily_max_trades_per_symbol: 5  # 增加日上限保护

  S-3. midday 时段 tp_scale=0.7 / pos_scale=0.6 — 缩窄但没有止盈调整

  午盘波动小，缩 TP 是对的，但仓位也缩 → R 反而变小。应该反过来：缩 TP + 不缩仓位 = 更多小赚换稳定。

  优化:
  midday:
    tp_scale: 0.7    # 保持
    sl_scale: 0.5    # 进一步缩 SL (午盘小波动, 不需要宽 SL)
    pos_scale: 1.0   # 不缩仓位
    # 实际效果: R/R 提升 (TP×0.7 / SL×0.5 = 1.4 倍 R)

  S-4. closing_drive 只允许 LONG，逻辑可疑

  unified_config.yaml:1222 directions: [LONG] —— 收盘 30min 只做多。
  但收盘前主力出货是常态，SHORT 在 closing_drive 应该也是有效的（ETF rebalance、机构清仓）。

  优化: 改为 directions: [LONG, SHORT]，但提高 signal_strength 门槛到 ≥ 2.5（避免假突破）。

  S-5. stock_selector 的 min_amplitude=0.015 偏小

  均值回归需要"足够的振幅"才有 alpha。min_amplitude=1.5% 包括很多窄幅震荡股，触发的"超买卖"实际是噪音。

  优化: 提到 0.025（2.5%）+ 增加 min_realized_vol_5d 过滤。

  ---
  四、最终建议路线图

  立刻（本周）

  1. C1 资金预算合计 110% — 改 yaml，安全垫立刻就有
  2. C2 ExitMonitor 区分 strategy_id — 一行 filter 解决最严重冲突 ⭐
  3. C3 反向冲突检查 — _can_open 增加方向判断 ⭐

  本月

  4. C4 EOD 协议统一 — 全局 risk.end_of_day 配置
  5. C6 退出归因 — C2 完成后自动解决
  6. S-1 / S-2 — scalping 入场放宽 + 冷却缩短
  7. S-5 — stock_selector 振幅门槛提高

  本季度

  8. C5 SignalConflictBus — 跨策略信号共享层（顶级量化系统标配）
  9. S-3 / S-4 — 时段策略精细化
  10. 建立"策略归因层" — 每周输出"主线赚多少、scalping 赚多少、互相干扰损失多少"，没有这个数据永远不知道双策略组合是不是真的优于单策略

  ---
  五、最关键的"专家判断"

  1. 当前架构是对的方向：动量主线 + 均值回归卫星是产业级标配
  2. 但执行层有 6 处结构性裂缝，让两者更像"内耗"而非"互补"
  3. 建议把 C1+C2+C3 作为本周 P0 修复，否则实盘启用 scalping mode=active 时几乎确定会出事
  4. 真正的长期价值在 C5（SignalConflictBus）：让两套策略的信号互为佐证 → 强信号双重确认 → 高胜率；矛盾信号互相调低 → 低误开
  5. 建议把"双策略协同效果"做成日报必测指标：
    - cross_strategy_conflict_count（同一日内同 symbol 不同方向次数）
    - cross_strategy_correlation（PnL 相关性，理想 < 0.3，如果 > 0.7 说明根本没真正分散）
    - each_strategy_alpha（剥离另一个后的独立 PnL）
