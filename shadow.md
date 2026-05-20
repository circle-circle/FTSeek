  一、先肯定方向：为什么这是对的

  1. 绕开了 LLM 系统最大的回测痛点 —— LLM 输出不可重放（同样的 prompt + 同样的市场，不同时间调可能给不同 plan），传统回测对 LLM 系统几乎失效
  2. 数据基础设施零成本 —— 项目没有完整 1min/tick 历史，重建回测框架要花 1-2 个月；shadow 用真实未来数据，0 数据成本
  3. 真实市场分布 —— 不存在"过拟合历史"问题，因为是用未来检验
  4. 产业界标准做法 —— Two Sigma 内部叫 "paper portfolio"，AQR 叫 "shadow book"，几乎所有大型量化都这么做

  二、5 个必须警惕的陷阱（按危险度排序）

  🚨 陷阱 1: "假成交"造成结构性偏差 ⭐⭐⭐ 最致命

  现象：
  - 主策略 NVDA @ $180.00 实际成交 → $180.05（含 $0.05 滑点 + 1bp 手续费）
  - 影子策略"如果我买" → 用 $180.00 记账
  - 单笔差异 = 2.8bp ≈ 0.028%
  - 一天 10 笔交易 → 影子假"赚" 0.28%/天
  - 30 天累积 8.4% 优势 100% 来自"没扣成本"

  → 切换后实盘扣回这部分，胜率提升假象瞬间消失

  修复：影子记账时必须扣 等同的或更保守的 滑点 + 手续费 + spread。建议：
  shadow_fill_price = market_price + slippage_estimate  # 不是 market_price 本身
  slippage_estimate = atr × 0.05 + spread_bps × 0.0001  # 至少这样
  shadow_pnl_after_costs = shadow_pnl_gross - 2 × commission_bps - 2 × slippage

  🚨 陷阱 2: 反事实虚高（Counterfactual Bias）⭐⭐⭐

  现象：
  - 主策略 confidence threshold = 0.6，今天没买 TSLA（confidence 0.55）
  - 影子策略 threshold = 0.5，"如果我买了 TSLA" → 后续涨 +2% → 影子记 +2%
  - 这个 +2% 是真的吗？不一定：
    - 主策略当时账户已开 5 个仓位，资金占满 80%
    - 影子假设"再买 TSLA"，但真上线后买 TSLA 会挤掉哪个仓位？
    - 那个被挤掉的仓位本来可能赚 +3% → 影子的 +2% 实际是 -1% 机会成本

  修复：影子必须模拟资金约束 + 风控约束：
  - 维护 ShadowAccount，初始资金 = 主账户净值
  - 影子开仓必须扣资金占用
  - 影子开仓必须过同样的 RiskGateway（PDT、组合敞口、单仓上限）
  - 影子达到资金上限后和主策略一样跳过新机会

  否则影子永远"赢"，因为它是个无限资金的幻想账户。

  🚨 陷阱 3: 3-5 天样本量远远不够 ⭐⭐

  现象: 用户原方案"连续 3 天胜出 → 自动切换"
  - 美股一周 5 个交易日，3 天 = 3 个独立"日 PnL"样本
  - 假设主策略日 PnL 标准差 = 1.5%（典型）
  - t-test 检测出 0.5% 的真实 alpha 差异，需要 ~36 个样本（power 0.8, alpha 0.05）
  - 3 天的"胜出"，82% 概率是随机噪声（type I error 严重）

  实战例子：
  - 周一 SPY +1.2%（趋势日）→ 趋势型影子参数赢主策略 0.3%
  - 周二 SPY +0.8% → 影子又赢 0.4%
  - 周三 SPY +0.5% → 影子赢 0.2%
  - 系统判定 "影子胜出 3 天"，自动切换
  - 周四市场转震荡 → 趋势型参数 -1.5%
  - → 完美的"切换即亏"

  修复：
  - 最少 20 个交易日（约 4 周），且至少包含 5 个趋势日 + 5 个震荡日
  - 用 t-test 或 Wilcoxon signed-rank test（影子 PnL - 主策略 PnL 配对样本），p-value < 0.05 才算显著
  - 不只看胜率，还要看 Sharpe 比的 95% 置信区间

  🚨 陷阱 4: Selection Bias（选择性偏差）⭐⭐

  现象：LLM 一周提议 5 个参数候选 → 5 个影子并行 → 4 周后看哪个赢主策略
  - 即使所有 5 个都和主策略没差异，纯随机有 ~22% 概率某个"赢"主策略 p<0.05（多重检验）
  - 选最赢的那个上线 → 切换后回归均值 → 立刻亏

  修复：
  - Bonferroni 校正：5 个候选并行 → p-value 阈值收紧到 0.05/5 = 0.01
  - 或 预登记机制：每个候选必须事先声明"预期改善多少 + 验证窗口多长"，事后超额改善才算赢
  - 候选数量上限：同时跑的影子 ≤ 3 个

  ⚠ 陷阱 5: 信号同质性导致的"伪平行宇宙"⭐

  现象：影子和主策略只改了 1-2 个参数（如 TP 从 3% → 4%），其他完全一样 →
  - 它们的开仓时机 100% 一致（信号源相同）
  - 它们的 PnL 高度相关（ρ > 0.95）
  - t-test 容易显著但实际差异微小

  修复：
  - 看 PnL 差异的绝对值 而非相对优势：要求 mean(shadow_pnl - main_pnl) > 0.3% / day 才算"实质差异"
  - 高相关性下，少量异常日就能让结果反转 → 必须看分布稳定性（IQR、stress test）

  ---
  三、推荐架构：分层影子（Layered Shadow）

  不要用单一"影子策略"，分 3 层用不同的严格度：

  Level 1: 信号影子（最便宜，秒级反馈）

  只比较"是否触发信号"，不模拟成交。

  # 主线生成信号时, 同时跑 shadow
  main_signal = scorer_main.score(context)        # threshold = 0.6
  shadow_signal = scorer_shadow.score(context)    # threshold = 0.5

  # 写入 signal_divergence.db
  record_divergence(
      symbol, timestamp,
      main_action="OPEN" if main_signal.confidence > 0.6 else "SKIP",
      shadow_action="OPEN" if shadow_signal.confidence > 0.5 else "SKIP",
      market_price_at_signal,
      follow_up_5m, follow_up_30m, follow_up_close,  # 后续看 5min/30min/收盘价
  )

  用途：
  - 快速识别"明显糟糕的候选"——如果影子触发率比主线多 3 倍，且后续盈利率反而低 → 立刻淘汰
  - 1-2 天就能淘汰差候选，节省下游验证成本

  Level 2: 退出影子（中等成本，依附主策略持仓）

  主策略真实开了仓位，同时跑影子退出参数。

  # 当主策略持有 NVDA 时
  class ShadowExitTracker:
      def on_each_scan(self, position, current_price, atr):
          # 主线退出参数: TP=3%, SL=5%, chandelier=1.2 ATR
          # 影子退出参数: TP=4%, SL=3%, chandelier=0.8 ATR
          shadow_action = self.shadow_exit_engine.check(position, current_price, atr)
          if shadow_action == "EXIT" and not position.shadow_exited:
              position.shadow_exit_price = current_price + shadow_slippage
              position.shadow_exit_time = now()
              position.shadow_exited = True
          # 主策略真实退出后, 比较两边 PnL

  用途：直接对比"如果用 candidate 退出参数，会赚多少 / 亏多少"，完全可控，没有反事实问题（因为开仓相同）。

  Level 3: 全栈影子（最贵，独立账户）

  完整模拟入场 + 退出 + 资金占用。

  class ShadowAccount:
      """完整虚拟账户, 与主账户隔离, 但订阅同样的市场数据"""
      def __init__(self, params: Dict, initial_capital: float):
          self.params = params           # 候选参数集
          self.cash = initial_capital
          self.positions = {}
          self.trades = []

      def on_market_event(self, ctx):
          # 跑同样的 IntradayScorer (但用 shadow params)
          signal = IntradayScorer(self.params).score(ctx)
          # 过同样的 RiskGateway (但用 shadow 账户状态)
          decision = RiskGateway(...).evaluate(signal, self.account_context())
          if decision.approved:
              self._open_shadow_position(signal, ctx, with_slippage=True)

      def daily_eod(self):
          # 计算当日 PnL, 写入 shadow_account_eod.db
          ...

  用途：最终切换决策依据。只对 Level 1+2 都通过的候选启用（节省成本）。

  ---
  四、统计学纪律（最容易被忽视）

  4.1 切换决策必须基于显著性，不是"看起来赢"

  def should_promote(main_pnl: List[float], shadow_pnl: List[float]) -> Tuple[bool, str]:
      if len(shadow_pnl) < 20:
          return False, f"insufficient samples ({len(shadow_pnl)} < 20)"

      # 配对 t-test (因为同一天的 PnL 不独立)
      diff = np.array(shadow_pnl) - np.array(main_pnl)
      t_stat, p_value = scipy.stats.ttest_1samp(diff, 0)

      if p_value > 0.05:
          return False, f"not significant (p={p_value:.3f})"

      # 实质差异检查
      mean_diff = np.mean(diff)
      if mean_diff < 0.003:  # 平均每日改善 < 0.3%
          return False, f"effect size too small ({mean_diff:.3%}/day)"

      # 风格鲁棒性检查
      trend_days = [i for i, ctx in enumerate(daily_context) if ctx.regime == "TREND"]
      range_days = [i for i, ctx in enumerate(daily_context) if ctx.regime == "RANGE"]
      trend_mean = np.mean(diff[trend_days]) if trend_days else 0
      range_mean = np.mean(diff[range_days]) if range_days else 0
      if min(trend_mean, range_mean) < 0:
          return False, f"loses in one regime (trend={trend_mean:.3%}, range={range_mean:.3%})"

      return True, f"PROMOTE: mean_diff={mean_diff:.3%}, p={p_value:.3f}"

  4.2 多候选并行 → Bonferroni 校正

  # 同时跑 3 个 shadow → 显著性阈值收紧
  ALPHA = 0.05 / num_active_shadows

  4.3 切换后保留观察期（最关键的安全网）

  def promote(candidate, current_active):
      # 不是替换, 是降级
      new_active = candidate
      new_shadow = current_active   # 旧主策略变成"反向影子"
      rollback_buffer.add(current_active, observe_days=10)
      # 如果新主策略 5 天内 PnL 比"反向影子"差 > 0.5% → 自动回滚

  ---
  五、与现有系统的接合（具体实现）

  5.1 改最小的代码做 Level 1+2

  # 新增: app/shadow/__init__.py
  class ShadowRegistry:
      """主策略 + 候选参数集的注册表"""
      active = MainParams(...)
      candidates = [
          CandidateParams(name="tp4_sl3", patches={"day_trade.tp1": 4.0, "day_trade.sl": 3.0}),
          CandidateParams(name="ch08", patches={"chandelier.activation": 0.8}),
      ]

  # 改 IntradayScorer.score 末尾:
  def score(self, context):
      main_signal = self._compute(context, self.params)
      # 新增: shadow scoring
      if shadow_registry.candidates:
          for cand in shadow_registry.candidates:
              shadow_signal = self._compute(context, cand.merged_params)
              shadow_writer.write(context.symbol, main_signal, shadow_signal, cand.name)
      return main_signal

  # 改 ExitMonitor.scan_positions 末尾:
  def scan_positions(self, positions, ...):
      main_actions = self._scan_with_params(positions, self.params)
      if shadow_registry.candidates:
          for cand in shadow_registry.candidates:
              shadow_actions = self._scan_with_params(positions, cand.merged_params, dry_run=True)
              shadow_exit_writer.write(main_actions, shadow_actions, cand.name)
      return main_actions

  5.2 数据落地

  -- shadow_signals.db
  CREATE TABLE signal_divergence (
      timestamp REAL, symbol TEXT, candidate_name TEXT,
      main_action TEXT, shadow_action TEXT,
      main_confidence REAL, shadow_confidence REAL,
      market_price REAL, atr REAL,
      follow_up_5m_price REAL, follow_up_30m_price REAL, follow_up_eod_price REAL,
      main_pnl_5m REAL, shadow_pnl_5m REAL,  -- 等同条件下后续 PnL
      market_regime TEXT
  );

  CREATE TABLE shadow_exit (
      position_id TEXT, candidate_name TEXT,
      main_exit_time REAL, main_exit_price REAL, main_pnl_pct REAL,
      shadow_exit_time REAL, shadow_exit_price REAL, shadow_pnl_pct REAL,
      diff_pnl_pct REAL,  -- shadow - main
      regime TEXT
  );

  -- shadow_account.db (Level 3 用)
  CREATE TABLE shadow_account_daily (
      date TEXT, candidate_name TEXT,
      starting_equity REAL, ending_equity REAL,
      daily_pnl REAL, daily_pnl_pct REAL,
      trades_count INT, win_count INT,
      main_account_daily_pnl_pct REAL,  -- 对照
      diff_pnl_pct REAL,
      regime TEXT
  );

  5.3 自动切换 trigger

  # app/shadow/promotion_engine.py
  class PromotionEngine:
      """每日 EOD 评估候选, 触发切换决策"""
      MIN_SAMPLES = 20
      MIN_EFFECT_SIZE = 0.003     # 0.3%/day
      SIGNIFICANCE_ALPHA = 0.05

      def daily_eval(self):
          for cand in shadow_registry.candidates:
              samples = self._load_paired_pnl(cand, days=30)
              if len(samples) < self.MIN_SAMPLES:
                  continue

              decision = self._statistical_test(samples)
              if decision.promote:
                  self._execute_promotion(cand)
                  notifier.send_alert(f"[Shadow] PROMOTE {cand.name}: {decision.summary}")
              elif decision.eliminate:
                  shadow_registry.remove(cand)
                  notifier.send_alert(f"[Shadow] ELIMINATE {cand.name}: {decision.reason}")

  5.4 LLM 提议接入

  # 每周末 LLM 看 trade_attribution → 生成候选
  def llm_weekly_proposal():
      insights = load_recent_attribution(days=14)
      prompt = f"分析下面的交易归因, 提议 1-2 个参数调整..."
      proposals = llm_call(prompt)  # 输出 JSON list of CandidateParams
      for p in proposals:
          shadow_registry.add(p, max_observe_days=30)

  ---
  六、按你的方案改进的最终建议（取你方案 + 我的纪律）

  你的方案（保留）：

  1. ✅ 主策略真实下单
  2. ✅ 影子策略后台跑分
  3. ✅ 自动切换

  我的硬性补充：

  1. 影子必须扣等同滑点 + 手续费（陷阱 1）
  2. 影子必须共享资金/风控约束（陷阱 2）
  3. 样本量 ≥ 20 个交易日，不是 3-5 天（陷阱 3）
  4. 多候选并行需 Bonferroni 校正（陷阱 4）
  5. 效应量门槛 ≥ 0.3%/day，不只是统计显著（陷阱 5）
  6. 风格鲁棒性：趋势日和震荡日都不能输
  7. 切换后回滚保险：旧参数变"反向影子"持续观察 10 天，劣化即回滚

  修订后的切换决策树：

  LLM 提议 candidate
      ↓
  Level 1 信号影子 (1-3 天)
      ├─ 触发率差异 > 50% 且后续 PnL 显著差 → 直接淘汰
      └─ 通过 → 进 Level 2
          ↓
  Level 2 退出影子 (5-10 天)
      ├─ 在主策略持仓上模拟新退出参数
      ├─ 配对 PnL t-test, p > 0.10 → 淘汰
      └─ 通过 → 进 Level 3
          ↓
  Level 3 全栈影子 (20+ 个交易日)
      ├─ 独立 ShadowAccount, 完整模拟
      ├─ p < 0.05 + 效应量 ≥ 0.3% + 双 regime 不亏 → PROMOTE
      └─ 否则继续观察, 60 天上限后强制淘汰
          ↓
  PROMOTE: 旧参数降级为 rollback shadow, 观察 10 天
      ├─ 新参数表现劣化 > 0.5% → AUTO ROLLBACK
      └─ 稳定 → 永久退役旧参数

  ---
  七、与你方案的关键差异点

  ┌────────────┬────────────┬─────────────────────────────────┐
  │    维度    │ 你的原方案 │            我的建议             │
  ├────────────┼────────────┼─────────────────────────────────┤
  │ 影子成交价 │ 用真实价   │ 真实价 + 等同滑点 + 手续费      │
  ├────────────┼────────────┼─────────────────────────────────┤
  │ 资金/风控  │ 无约束     │ 必须模拟同等约束                │
  ├────────────┼────────────┼─────────────────────────────────┤
  │ 样本量     │ 3-5 天     │ 20+ 交易日                      │
  ├────────────┼────────────┼─────────────────────────────────┤
  │ 切换标准   │ "胜率高"   │ t-test 显著 + 效应量 + 风格鲁棒 │
  ├────────────┼────────────┼─────────────────────────────────┤
  │ 候选数量   │ 未定       │ ≤ 3 并行, 多候选 Bonferroni     │
  ├────────────┼────────────┼─────────────────────────────────┤
  │ 切换后     │ 直接生效   │ 旧参数继续观察, 劣化回滚        │
  ├────────────┼────────────┼─────────────────────────────────┤
  │ 候选来源   │ LLM        │ LLM + 规则提议都可              │
  └────────────┴────────────┴─────────────────────────────────┘

  ---
  八、为什么这套机制最终会比"独立回测"更强

  1. 真实未来数据：彻底消除 "回测好实盘亏" 的过拟合风险
  2. 真实成本结构：包括滑点、spread、broker 行为，回测难以精确模拟
  3. 真实资金压力：模拟真实账户的 buying_power、PDT、组合敞口
  4. 真实事件响应：财报、新闻、突发事件按真实时间线发生
  5. 持续在跑：不像回测一锤子买卖，shadow 永远在累积证据

  唯一代价：验证周期长（20 天 vs 回测 5 分钟），但这恰恰是优势 —— 强迫团队不要 "今天提议明天上线" 的浮躁

  ---
  九、落地路线图

  第 1 周：Level 1 信号影子

  - 新增 app/shadow/registry.py、app/shadow/signal_writer.py
  - IntradayScorer 加并行 shadow scoring
  - shadow_signals.db 落地

  第 2-3 周：Level 2 退出影子

  - 新增 app/shadow/exit_tracker.py
  - ExitMonitor.scan_positions 增加 dry_run 模式
  - shadow_exit.db 落地

  第 4-6 周：Level 3 全栈影子 + Promotion Engine

  - 新增 app/shadow/account.py（虚拟账户）
  - app/shadow/promotion_engine.py（统计检验 + 自动切换）
  - 接入 ConfigWriter 做实际参数切换

  第 7+ 周：LLM 提议接入

  - 每周末 LLM weekly_proposal 自动生成候选注册到 ShadowRegistry
  - 闭环：LLM 提议 → 3 层验证 → 自动切换 → rollback 安全网
