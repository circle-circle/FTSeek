# FTSeek 港股架构改造计划

## 目标
按交易执行链路（盘前→盘中→盘后）实现模块化和低耦合，统一配置管理。

## 当前问题
1. `data_subscriber.py` (3600行) — 盘中 God Object
2. `consensus_engine.py` (3150行) — 策略共识 God Class
3. 配置读取 6 种方式混杂（app.config / YAML直读 / 环境变量 / config_center / config_container / 硬编码）
4. 跨层依赖混乱（上游 import 下游）

## 实施步骤

### Phase 1: 统一配置层 ✅
- [x] `app/core/config_loader.py` — 统一配置加载器
- [x] 所有模块通过 `cfg.get("key.path")` 获取配置

### Phase 2: 阶段抽象 ✅
- [x] `app/core/stages.py` — 三阶段接口和生命周期管理

### Phase 3: 盘中模块拆分（当前）
- [ ] 拆分 `data_subscriber.py` → `app/intraday/`
  - `market_subscriber.py` — 行情订阅
  - `signal_processor.py` — 信号处理
  - `position_manager.py` — 止损/止盈
  - `worker_pool.py` — Worker 线程

### Phase 4: 策略模块拆分
- [ ] 拆分 `consensus_engine.py` → `app/strategy/consensus/`

### Phase 5: 清理冗余
- [ ] 统一通知层
- [ ] 清理重复配置读取

## 配置统一原则
1. **SSOT**: `config/unified_config.yaml` 是唯一配置数据源
2. **访问方式**: 所有模块通过 `cfg.get("section.key")` 读取
3. **环境变量**: 仅在 ConfigLoader 中解析 `${ENV_VAR}`
4. **禁止**: 直接读 YAML、硬编码默认值、多路径读取

## 模块依赖规则
```
盘前 (pipeline/) → 输出 BattlePlan → 盘中 (intraday/) → 输出 TradeResult → 盘后 (review/)
                                                    ↓
                                             基础设施 (core/)
                                             TradingEnv | Config | EventBus
```
