
### 2026-06-19: AVGO Entry Price Calculation - 使用实际成交价而非理论价
- **Decision Maker**: hermes
- **Problem**: AVGO signal显示entry=$392.9，但实际买入均价=$409.25，差异=$16.35 (4.2%)
- **Root Cause**: `instruction_generator.py` 使用 `decision.decision_price`（信号触发时的理论价），而非实际成交价
- **Fix**: 
  ```python
  # shadow_tracker.py L233-236
  actual_entry_price = self.trade_store.get_actual_entry_price(symbol, direction)
  if actual_entry_price:
      shadow_pnl = (close_price - actual_entry_price) * position_size
  else:
      shadow_pnl = (close_price - decision.decision_price) * position_size
  ```
- **Impact**: Shadow PnL计算更准确，反映真实交易成本（滑点+手续费）
- **Risk**: 低。仅影响shadow模式的PnL计算，不影响实际执行
- **Verification**: 需下一个交易日验证新信号是否正确使用实际成交价

### 2026-06-19: voting_llm Strategy Audit - 发现3个关键问题
- **Decision Maker**: hermes
- **Context**: 深度审计voting_llm策略的48笔交易（30天，-$11,504）
- **Findings**:
  
  1. **命名不一致** (P1): 
     - 代码中有`voting_llm`和`llm_vote`两个名称
     - DB查询用`voting_llm`，但部分代码用`llm_vote`
     - 风险：可能导致信号归因错误
  
  2. **重复信号触发** (P0):
     - 同一symbol在同一天可能触发多次voting_llm信号
     - 原因：signal_dedup逻辑不完整，未覆盖voting_llm路径
     - 风险：重复开仓，资金利用率下降
  
  3. **IC门禁覆盖不全** (P0):
     - IC门禁只覆盖main策略，voting_llm独立运行
     - 当IC<0时，main被拦截但voting_llm仍可触发
     - 风险：低质量信号逃逸

- **Immediate Actions**:
  - [ ] 统一命名为`voting_llm`（全局grep + DB查询）
  - [ ] 扩展signal_dedup覆盖voting_llm路径
  - [ ] IC门禁扩展到所有策略（包括voting_llm）
  
- **Data Evidence**:
  ```
  重复交易示例：
  - 2026-06-15 TSLA: 3次voting_llm信号，间隔<30min
  - 2026-06-17 NVDA: 2次voting_llm信号，同方向
  
  IC门禁逃逸：
  - 2026-06-10~2026-06-18: main被IC门禁拦截5次
  - 同期voting_llm触发3次（理论上应该也被拦截）
  ```

- **Next Steps**: 创建修复计划，优先级P0 > P1

### 2026-06-19: Scalping 完全清理 - 移除所有残留代码和配置
- **Decision Maker**: hermes
- **Background**: scalping策略已于2026-06-14禁用，但代码中仍有残留
- **Actions**:
  1. **删除文件**:
     - `app/scalping/` 整个目录（scalping_engine.py, scalping_executor.py等）
     - `app/storage/unified_trade_store.py`（已废弃）
  
  2. **清理配置**:
     - `unified_config.yaml`: 移除scalping配置块（约50行）
     - `config/strategies/scalping.yaml`: 删除
  
  3. **清理代码引用** (14处):
     - `app/core/models.py`: 移除SCALPING枚举
     - `app/evolution/shadow_tracker.py`: 移除scalping分支逻辑
     - `app/reports/intraday_attribution.py`: 移除scalping持仓展示
     - 其他11个文件的注释和引用
  
  4. **保留**:
     - DB历史记录查询（`strategy_id='scalping'`）用于归因分析
     - 测试用例中的scalping历史数据验证
  
- **Impact**: 代码库减少约800行，消除"已删除策略"的认知负担
- **Verification**: `grep -r "scalping" app/` 返回0结果（除DB查询外）

### 2026-06-19: OKR System Design - 5维度进度追踪
- **Decision Maker**: hermes + user
- **Context**: 需要量化追踪"30天稳定盈利"目标
- **Design**:
  ```
  OKR Score = (kr1 + kr2 + kr3 + kr4 + kr5) / 5 * 100
  
  kr1 (Win Rate):     min(1.0, current_wr / 50%) * 20
  kr2 (Weekly PnL):   min(1.0, max(0, weekly_pnl / 1000)) * 20
  kr3 (Sample Size):  min(1.0, trade_count / 30) * 20
  kr4 (IC Threshold): min(1.0, max(0, ic_value / 0.02)) * 20
  kr5 (Trigger Rate): min(1.0, trigger_rate / 60%) * 20
  ```
- **Thresholds**:
  - Win Rate: 50% (行业基准)
  - Weekly PnL: $1000 (1% of $100K capital)
  - Sample Size: 30 trades (统计显著性)
  - IC: 0.02 (最小有效预测力)
  - Trigger Rate: 60% (避免过度过滤)

- **Implementation**:
  - 脚本: `scripts/okr_status.py` (实时计算)
  - 存储: `data/okr_history.jsonl` (历史追踪)
  - 自动化: cron job每30分钟运行一次

- **Current Status (2026-06-19)**:
  - Score: 8/100
  - kr1: 0 (42.3% < 50%)
  - kr2: 0 (-$5558 < $0)
  - kr3: 16 (78 trades > 30, 但只看30天窗口)
  - kr4: 0 (IC未计算)
  - kr5: 0 (trigger rate未追踪)

- **Next Actions**:
  - 完成voting_llm修复（提升kr1）
  - 启动新策略验证（提升kr2, kr3）
  - 实现IC计算（提升kr4）
  - 添加trigger rate监控（提升kr5）

### 2026-06-19: voting_llm P0 Fixes - 命名统一 + CRA路径signal_dedup
- **Decision Maker**: hermes
- **Status**: ✅ 已提交 (37ba98a0)
- **Fixes**:
  
  1. **命名统一 (llm_vote → voting_llm)**:
     - 文件: `app/strategy/pure/llm_vote.py` → `voting_llm.py`
     - @register: `"llm_vote"` → `"voting_llm"`
     - 影响文件: 13+ (signal_stage, signal_pipeline, decision_recorder, voting_coordinator, etc.)
     - 保留: `models.py:67` `'llm_vote' in _sid` 兼容历史DB记录
  
  2. **CRA路径 signal_dedup 补齐 (P0)**:
     - 位置: `app/pipeline/signal_pipeline.py` L1186-1197
     - 问题: `_process_symbol_new_pipeline()` 调用 `_submit_order()` 前缺少 signal_dedup 检查
     - legacy路径 L1876 已有，CRA路径之前缺失
     - 修复: 添加与legacy路径相同的 dedup 逻辑
  
  3. **IC gate 覆盖验证**:
     - 确认: IC gate 在 `_submit_order()` L3490-3565
     - CRA 和 legacy 路径都调用 `_submit_order()`，IC gate 已覆盖所有策略
     - 无需额外修复

- **Impact**: 
  - 消除命名分裂导致的IC追踪数据分散
  - 防止CRA路径产生重复订单
  - 确认所有策略受IC gate保护
- **Verification**: `grep -r "llm_vote" app/` 仅返回 models.py 的兼容检查
