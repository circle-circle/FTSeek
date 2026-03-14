# FTSeek V4 快速参考指南

**版本:** 4.3.0  
**更新日期:** 2026-03-09

---

## 🚀 快速开始

### 1. 导入核心模块

```python
from app.core import (
    StateManager,
    DecisionLog,
    SessionContext,
    ConfigManager,
    VersionManager
)
```

### 2. 初始化

```python
# 状态管理
state = StateManager()

# 决策日志
log = DecisionLog()

# 会话管理 (自动恢复上次会话)
session = SessionContext()

# 配置管理
config = ConfigManager()

# 版本管理
version_mgr = VersionManager()
```

---

## 📊 常用命令

### 状态管理

```python
# 设置/获取状态
state.set('key', 'value')
value = state.get('key')

# 保存/加载
state.save()
state.load()

# 检查点
checkpoint_id = state.checkpoint('before_trade')
state.restore(checkpoint_id)

# 自动快照
state.start_auto_snapshot(300)  # 5 分钟
```

### 决策日志

```python
# 记录交易决策
log.log_trade_decision('HK.00700', 'BUY', 0.85, 'manual_confirm')

# 记录配置变更
log.log_config_change('trading.mode', 'paper', 'real', 'Switch to real')

# 查询
entries = log.query(symbol='HK.00700')
entries = log.query(decision_type='trade_decision')

# 统计
stats = log.get_statistics()

# 导出
log.export_to_json('decisions.json')
log.export_to_csv('decisions.csv')
```

### 会话管理

```python
# 启动新会话
session_id = session.start_session()

# 设置上下文
session.set_context('trading_state', {...})

# 获取上下文
state = session.get_context('trading_state')

# 保存
session.save()

# 恢复最近会话
session.restore()
```

### 配置管理

```python
# 获取配置
mode = config.get('trading.mode')
timeout = config.get('notification.dingtalk.timeout')

# 设置配置
config.set('trading.mode', 'real', reason='Go live')

# 热重载
config.reload()

# 注册变更回调
def on_change(key, old, new):
    print(f"{key}: {old} -> {new}")
config.register_callback(on_change)
```

### 版本管理

```python
# 获取当前版本
version = version_mgr.get_current_version()

# 创建版本快照
version_mgr.create_version_snapshot('4.3.0', config.get_all())

# 列出版本
versions = version_mgr.list_versions()

# 回滚
version_mgr.rollback_to_version('4.2.0')

# 兼容性检查
is_compatible, warnings = version_mgr.check_compatibility('4.2.0', '4.3.0')
```

---

## 🔧 配置文件

**位置:** `config/app_config.yaml`

**主要配置项:**

```yaml
trading:
  mode: paper
  max_daily_trades: 10
  max_single_position: 0.20

notification:
  dingtalk:
    enabled: true
    timeout: 600

features:
  four_step_confirmation: true
  auto_confirm: true
  smart_push: true
```

---

## 📝 运行测试

```bash
cd /opt/ftseek/FTSeek
python3 -m pytest tests/unit/test_core_modules.py -v
```

---

## 🐛 故障排查

### 问题：状态无法保存

**检查:**
```python
state = StateManager()
print(state.save())  # 应该返回 True
```

**解决:** 确保目录存在且有写权限

### 问题：决策日志查询为空

**检查:**
```python
log = DecisionLog()
print(log.get_entry_count())  # 应该 > 0
```

**解决:** 确保先记录决策再查询

### 问题：会话无法恢复

**检查:**
```python
session = SessionContext()
print(session.list_sessions())  # 应该有会话
```

**解决:** 确保会话已保存

---

## 📊 文件结构

```
/opt/ftseek/FTSeek/
├── app/core/
│   ├── __init__.py
│   ├── state_manager.py
│   ├── decision_log.py
│   ├── session_context.py
│   ├── config_manager.py
│   └── version_manager.py
├── config/
│   └── app_config.yaml
├── data/
│   ├── state_snapshot.json
│   ├── decision_log.json
│   └── sessions/
├── tests/unit/
│   └── test_core_modules.py
└── docs/plans/
    └── phase1-3-completion-report.md
```

---

## 🎯 核心功能

| 功能 | 类 | 说明 |
|------|-----|------|
| 状态持久化 | StateManager | 解决长对话遗忘 |
| 决策日志 | DecisionLog | 记录所有决策 |
| 会话管理 | SessionContext | 跨会话状态保持 |
| 配置管理 | ConfigManager | 统一配置 + 热重载 |
| 版本管理 | VersionManager | 版本控制 + 回滚 |

---

**🎉 FTSeek V4 - 长对话遗忘问题已解决！**
