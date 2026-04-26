# 跨市场LLM预分析链路修复总结

## 修复日期
2026-04-12

## 问题概述

在实现"跨市场套利策略叠加开盘前LLM预分析"功能时,发现了4个关键链路断裂问题,导致09:30开盘时共识引擎无法读取09:25预分析的LLM缓存结果。

---

## 修复内容

### 1. main.py - 传递LLM分析师到DataSubscriber

**文件**: `app/main.py` (L502)

**修改**:
```python
# 修改前
subscriber = DataSubscriber(strategies, notifier)

# 修改后
subscriber = DataSubscriber(strategies, notifier, llm_analyst=_llm_analyst_for_scheduler)
```

**原因**: LLM分析师实例只注入了跨市场调度器,未传递给DataSubscriber,导致共识引擎无法访问LLM缓存。

---

### 2. data_subscriber.py - 初始化共识引擎LLM集成

**文件**: `app/data_subscriber.py`

**修改1**: 构造函数签名 (L94)
```python
# 修改前
def __init__(self, strategies: List[BaseStrategy], notifier: DingTalkNotifier):

# 修改后
def __init__(self, strategies: List[BaseStrategy], notifier: DingTalkNotifier, llm_analyst=None):
```

**修改2**: 共识引擎初始化后添加LLM集成 (L206-L220)
```python
# 【修复 2026-04-11】初始化共识引擎LLM集成
if llm_analyst:
    try:
        from config.config_center import get_config_center
        _llm_cfg = get_config_center().get('ai.llm_market_analyst.consensus_integration', {})
        if hasattr(_llm_cfg, 'to_dict'):
            _llm_cfg = _llm_cfg.to_dict()
    except Exception:
        _llm_cfg = {}
    
    weight = _llm_cfg.get('weight', 0.15)
    adaptive = _llm_cfg.get('adaptive', {})
    self._consensus.init_llm_integration(llm_analyst, weight, adaptive)
    logger.info(f"共识引擎LLM集成已初始化: weight={weight}")
```

**原因**: 共识引擎创建后未调用`init_llm_integration()`,导致`_llm_consensus_enabled`始终为False。

---

### 3. consensus_engine.py - 修复缓存键匹配问题

**文件**: `app/strategy/consensus_engine.py`

**修改1**: BUY方向缓存读取 (L1434)
```python
# 修改前
llm_assessment = self._llm_analyst.get_cached_assessment(code)

# 修改后
llm_assessment = self._llm_analyst.get_cached_assessment(code, trigger_type="SCHEDULED")
```

**修改2**: SELL方向缓存读取 (L1573)
```python
# 修改前
llm_assessment = self._llm_analyst.get_cached_assessment(code)

# 修改后
llm_assessment = self._llm_analyst.get_cached_assessment(code, trigger_type="SCHEDULED")
```

**原因**: 
- 调度器预分析使用`trigger_type="SCHEDULED"`生成缓存键
- 共识引擎默认使用`trigger_type="COMBINED"`读取缓存
- 缓存键包含trigger_type,导致键不匹配,缓存无法命中

---

### 4. config.yaml - 优化缓存TTL配置

**文件**: `config/config.yaml` (L873)

**修改**:
```yaml
# 修改前
cache_ttl: 300  # 缓存有效期(秒)

# 修改后
cache_ttl: 600  # 缓存有效期(秒) - 从09:25预分析到09:30开盘执行,留足余量
```

**原因**: 考虑到20个标的的分析耗时(API响应+间隔),将缓存有效期从5分钟增加到10分钟,确保09:30开盘时缓存仍然有效。

---

## 验证结果

运行验证脚本 `scripts/verify_llm_cross_market_link.py`:

```
✅ LLM分析师初始化: 通过
✅ 共识引擎LLM集成: 通过
✅ 缓存键匹配机制: 通过
✅ DataSubscriber签名: 通过

🎉 所有测试通过! 跨市场LLM预分析链路已正确配置
```

---

## 完整链路说明

修复后的完整运行流程:

### 09:00 - 盘前分析
- 跨市场调度器执行`_job_pre_analysis()`
- 获取美股隔夜数据,分析并生成跨市场套利信号
- 信号状态设为`pending`

### 09:10 - 钉钉推送计划
- 执行`_job_push_plan()`
- 推送跨市场套利计划到钉钉,用户审核

### 09:25 - LLM预分析
- 执行`_job_start_countdown()`
- 启动后台线程`_run_pre_market_llm_analysis()`
- 逐个标的调用qwen3.5-plus分析(`trigger_type="SCHEDULED"`)
- 分析结果写入`_cache`,键格式: `{code}:SCHEDULED:...`
- 钉钉推送汇总卡片和逐标的详情

### 09:30 - 开盘执行
- 跨市场调度器执行`_job_execute()`
- 共识引擎调用`evaluate()`生成交易信号
- **关键**: 通过`get_cached_assessment(code, trigger_type="SCHEDULED")`读取LLM缓存
- LLM研判结果作为置信度调节因子参与决策:
  ```
  最终置信度 = 技术项*0.34 + 情绪项*0.255 + 场景项*0.255 + LLM项*0.15
  ```
- 执行未否决的信号

### 10:00 - 退出清理
- 执行`_job_cleanup()`
- 清理信号记录,趋势策略接手

---

## 关键日志标识

### 09:25 预分析阶段
```
[09:25] LLM预分析已在后台线程启动
[LLM预分析] 开始对跨市场标的运行 LLM 研判...
[LLM预分析] HK.00700: BUY (HIGH)
[LLM预分析] 完成,15/20 个标的有方向性研判,耗时 45.2s
[LLM预分析] 钉钉汇总已推送: 8看多/7看空
```

### 09:30 开盘执行阶段
```
[共识] HK.00700 BUY 置信度计算 | 技术项=0.75 | 情绪项=0.68 | 场景项=0.70 | 资金流=0.60 | LLM项=0.128 | 最终=0.72
LLM置信度调节(BUY): HK.00700 llm_conf=0.85 align=0.80 factor=1.24 llm_part=0.128
```

---

## 回滚方案

如修复后出现异常,可临时降级:

1. **禁用LLM预分析**:
   ```yaml
   # config/config.yaml
   ai:
     llm_market_analyst:
       enabled: false
   ```

2. **重启服务**:
   ```bash
   cd /opt/ftseek/FTSeek
   ./restart.sh
   ```

---

## 后续优化建议

1. **监控缓存命中率**: 添加日志统计SCHEDULED缓存的命中/未命中比例
2. **性能优化**: 考虑并行调用LLM API,减少预分析总耗时
3. **告警机制**: 当缓存未命中时,发送钉钉告警通知
4. **单元测试**: 为缓存键匹配逻辑添加自动化测试用例

---

## 相关文件清单

- `app/main.py` - 主程序入口
- `app/data_subscriber.py` - 数据订阅器
- `app/strategy/consensus_engine.py` - 共识引擎
- `app/strategy/cross_market_scheduler.py` - 跨市场调度器
- `app/ai/llm_market_analyst.py` - LLM市场分析师
- `config/config.yaml` - 配置文件
- `scripts/verify_llm_cross_market_link.py` - 验证脚本(新增)

---

## 验证命令

```bash
# 运行验证脚本
cd /opt/ftseek/FTSeek
python scripts/verify_llm_cross_market_link.py

# 查看预分析日志
grep "LLM预分析" logs/ftseek.log

# 查看共识引擎LLM集成日志
grep "LLM置信度调节" logs/ftseek.log

# 查看共识引擎初始化日志
grep "共识引擎LLM集成已初始化" logs/ftseek.log
```
