# FTSeek 项目统一化指南

**更新日期**：2026-03-31  
**项目路径**：`/opt/ftseek/FTSeek`  
**适用范围**：港股交易（仅限港股）

---

## 核心原则

1. **单一代码源**：所有代码修改只在 `/opt/ftseek/FTSeek` 项目中
2. **不侵入其他路径**：不修改 `/root/.qoder-server/` 等临时路径
3. **环境变量统一**：所有配置在 `/opt/ftseek/FTSeek/.env` 中
4. **外部依赖最小化**：仅依赖 OpenClaw 的账户选择逻辑

---

## 项目结构

```
/opt/ftseek/FTSeek/
├── app/
│   ├── trader/
│   │   └── executor.py          # 交易执行器（含账户选择）
│   ├── config.py                 # 配置文件
│   └── ...
├── .env                          # 环境变量配置
├── scripts/                      # 工具脚本
└── docs/                         # 文档
```

---

## 账户选择逻辑

### 实现位置

**文件**：`/opt/ftseek/FTSeek/app/trader/executor.py`

**函数**：`_get_account_for_code(code, trd_env)`

### 账户配置

| 账户类型 | 账户 ID | 适用标的 |
|---------|--------|---------|
| **港股正股账户** | `18820895` | 港股正股、ETF、窝轮、牛熊证 |
| **港股期权账户** | `8246818` | 港股期权（Call/Put） |

### 代码识别规则

| 代码类型 | 格式 | 示例 | 账户 |
|---------|------|------|------|
| **港股正股** | `HK.` + 5 位数字 | `HK.09992` | 18820895 |
| **港股 ETF** | `HK.` + 5 位数字 | `HK.02800` | 18820895 |
| **港股窝轮** | `HK.` + 5 位数字 | `HK.02500` | 18820895 |
| **港股期权** | `HK.` + 字母 + 6 位日期 + C/P + 数字 | `HK.TCH260327P470000` | 8246818 |

### 实现逻辑

```python
def _get_account_for_code(code: str, trd_env) -> Optional[int]:
    """
    根据证券代码自动选择账户
    
    优先使用 OpenClaw 的 select_account_for_code 函数，
    如果不可用则使用本地硬编码逻辑。
    """
    from futu import TrdEnv
    
    # 实盘不指定账户
    if trd_env != TrdEnv.SIMULATE:
        return None
    
    # 优先使用 OpenClaw 的账户选择逻辑
    if ACCOUNT_SELECTION_AVAILABLE:
        try:
            selected_acc = select_account_for_code(code)
            acc_id = int(selected_acc)
            logger.info(f"[账户选择] {code} → 账户 {acc_id} (OpenClaw)")
            return acc_id
        except ValueError as e:
            logger.warning(f"[账户选择] {code} 无法识别：{e}，使用本地逻辑")
    
    # 本地账户选择逻辑（备用）
    market = _detect_market(code)
    
    if market == "HK":
        if _is_option(code):
            return 8246818  # 港股期权账户
        else:
            return 18820895  # 港股正股账户
    
    return None
```

### 依赖导入

**位置**：`app/trader/executor.py` 文件顶部

```python
# 账户选择逻辑导入（用于港股多账户支持）
try:
    import sys
    import os
    sys.path.insert(0, '/root/.openclaw/skills/openapi/scripts')
    from common import select_account_for_code
    ACCOUNT_SELECTION_AVAILABLE = True
    logger.info("[账户选择] 模块加载成功")
except Exception as e:
    ACCOUNT_SELECTION_AVAILABLE = False
    logger.warning(f"[账户选择] 模块加载失败，将使用默认账户：{e}")
```

---

## 环境变量配置

### 配置文件位置

`/opt/ftseek/FTSeek/.env`

### 关键配置项

```bash
# 交易环境
TRADE_ENV=1
FORCE_SIMULATE=true

# 默认账户（会被自动账户选择覆盖）
SIMULATE_ACCOUNT_ID=8246818

# 监控标的
WATCH_LIST_HK=["HK.09992", "HK.00981", "HK.03690"]
WATCH_LIST_HK_OPTIONS=["HK.POP260429C190000"]

# AI 配置
FTSEEK_ENABLE_AI=true
AI_PROVIDER=bailian
BAILIAN_API_KEY=sk-xxx
```

---

## 修改流程

### 标准修改流程

1. **定位文件**：
   ```bash
   cd /opt/ftseek/FTSeek
   find . -name "*.py" -exec grep -l "关键词" {} \;
   ```

2. **修改代码**：
   ```bash
   vim app/trader/executor.py
   # 或使用其他编辑器
   ```

3. **语法检查**：
   ```bash
   python3 -c "import ast; ast.parse(open('app/trader/executor.py').read())"
   ```

4. **测试验证**：
   ```bash
   ./start.sh  # 启动 FTSeek
   tail -f logs/ftseek.log  # 查看日志
   ```

### 禁止操作

❌ **不要**直接修改 `/root/.qoder-server/data/User/History/*/` 中的文件  
❌ **不要**在其他路径创建代码副本  
❌ **不要**绕过项目目录直接修改运行中的代码  

---

## 与 OpenClaw 集成

### 集成方式

FTSeek 通过导入 OpenClaw 的 `common.py` 模块实现账户选择：

```python
sys.path.insert(0, '/root/.openclaw/skills/openapi/scripts')
from common import select_account_for_code
```

### 依赖关系

```
FTSeek (/opt/ftseek/FTSeek)
  ↓
OpenClaw Skills (/root/.openclaw/skills/openapi/scripts/common.py)
  ↓
富途 OpenAPI
```

### 同步机制

- **单向依赖**：FTSeek 依赖 OpenClaw 的账户选择逻辑
- **无双向同步**：OpenClaw 的修改不会自动同步到 FTSeek
- **版本兼容**：确保 OpenClaw 的 `select_account_for_code` 函数接口稳定

---

## 日志监控

### 账户选择日志

```log
[账户选择] HK.09992 → 账户 18820895 (OpenClaw)
[账户选择] HK.TCH260327P470000 → 账户 8246818 (OpenClaw)
[账户选择] HK.INVALID 无法识别：无法识别的港股代码格式，使用本地逻辑
```

### 查看日志

```bash
tail -f /opt/ftseek/FTSeek/logs/ftseek.log | grep "账户选择"
```

---

## 故障排查

### 问题 1：账户选择模块加载失败

**症状**：
```log
[账户选择] 模块加载失败，将使用默认账户：No module named 'common'
```

**解决**：
1. 检查 OpenClaw 路径是否正确
2. 确认 `/root/.openclaw/skills/openapi/scripts/common.py` 存在
3. 检查文件权限

### 问题 2：证券类型错误

**症状**：
```log
[HK] 平仓下单失败：当前模拟账号不支持此证券类型
```

**解决**：
1. 检查代码格式是否正确
2. 确认账户选择逻辑已生效
3. 查看日志中的账户选择记录

### 问题 3：代码修改未生效

**症状**：修改代码后行为未改变

**解决**：
1. 确认修改的是 `/opt/ftseek/FTSeek` 中的文件
2. 重启 FTSeek 服务
3. 清除 Python 缓存：`find . -name "*.pyc" -delete`

---

## 备份策略

### 自动备份

FTSeek 项目已有备份目录：
```
/opt/ftseek/FTSeek/backups/
```

### 手动备份

```bash
cd /opt/ftseek
tar -czf FTSeek.backup.$(date +%Y%m%d_%H%M%S).tar.gz FTSeek/
```

### 恢复备份

```bash
cd /opt/ftseek
tar -xzf FTSeek.backup.20260331_143000.tar.gz
```

---

## 相关文档

| 文档 | 说明 | 位置 |
|------|------|------|
| `ACCOUNT_SELECTION.md` | 账户选择功能文档 | `/root/.openclaw/skills/openapi/docs/` |
| `TRADE_SYNC.md` | 交易记录同步文档 | `/root/.openclaw/skills/openapi/docs/` |
| `BUGFIX_ACCOUNT_SELECTION.md` | 账户选择修复报告 | `/root/.openclaw/skills/openapi/docs/` |
| `PROJECT_UNIFICATION.md` | 本文档 | `/opt/ftseek/FTSeek/docs/` |

---

## 变更历史

| 日期 | 变更内容 | 影响范围 |
|------|---------|---------|
| 2026-03-31 | 集成 OpenClaw 账户选择逻辑 | `app/trader/executor.py` |
| 2026-03-31 | 创建项目统一化文档 | `docs/PROJECT_UNIFICATION.md` |
| 2026-03-31 | 清除 /qoder-server 临时修改 | 无（仅清理） |

---

## 联系支持

如有问题，请查阅：
- FTSeek 文档：`/opt/ftseek/FTSeek/docs/`
- OpenClaw 文档：`/root/.openclaw/skills/openapi/docs/`

---

**最后更新**：2026-03-31 14:35  
**维护者**：FTSeek 团队
