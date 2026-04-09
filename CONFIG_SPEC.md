# FTSeek 配置规范文档

**版本号**: 5.0.0  
**生效日期**: 2026-03-31  
**适用范围**: FTSeek 港股、FTSeek-US 美股

---

## 📢 重要更新 (v5.0.0 - 2026-03-31)

### P1 配置统一化完成

**变更内容**:
1. ✅ 创建统一配置容器 (`config/unified_config.py`)
   - 实现 UnifiedConfig 类
   - 统一港股和美股的配置结构
   - 提供类型安全的配置访问
   - 支持配置热重载

2. ✅ 合并配置文件
   - `app_config.yaml` + `risk_config.yaml` → `unified_config.yaml`
   - 统一版本号管理 (所有配置使用 v5.0.0)
   - 旧配置文件保留至 2026-04-30 (30 天向后兼容期)

3. ✅ 统一版本管理
   - 创建 `config/version.yaml` 统一管理版本号
   - 更新所有配置文件的版本引用
   - 编写版本一致性检查脚本 (`scripts/check_version_consistency.py`)

**迁移指南**:
```bash
# 1. 备份旧配置
cp config/app_config.yaml config/backups/
cp config/risk_config.yaml config/backups/

# 2. 运行版本一致性检查
python3 scripts/check_version_consistency.py

# 3. 更新代码引用
# 从: from config_loader import load_config
# 到: from unified_config import get_config

# 4. 验证服务正常
./restart.sh
```

**向后兼容性**:
- 旧配置文件 (`app_config.yaml`, `risk_config.yaml`) 保留至 2026-04-30
- 之后将移至 `config/deprecated/` 目录
- 代码应尽快迁移至使用 `unified_config.py`

---

## 1. 版本号规则 (语义化版本)

遵循 [Semantic Versioning 2.0.0](https://semver.org/) 规范：

```
主版本号。次版本号。修订号
MAJOR.MINOR.PATCH
```

### 1.1 版本号定义

| 版本类型 | 触发条件 | 示例 |
|---------|---------|------|
| **MAJOR** (主版本) | 不兼容的 API 变更、配置结构重大调整 | 1.0.0 → 2.0.0 |
| **MINOR** (次版本) | 向后兼容的功能新增、配置项新增 | 1.0.0 → 1.1.0 |
| **PATCH** (修订号) | 向后兼容的问题修复、配置默认值修正 | 1.0.0 → 1.0.1 |

### 1.2 配置文件版本标注

所有配置文件必须在文件头部标注版本：

```yaml
# config.yaml
# version: 1.0.0
# updated: 2026-03-31
```

```json
{
  "_meta": {
    "version": "1.0.0",
    "updated": "2026-03-31"
  }
}
```

---

## 2. 配置文件格式规范

### 2.1 格式选择优先级

| 场景 | 推荐格式 | 说明 |
|-----|---------|------|
| 应用主配置 | **YAML** | 可读性好，支持注释 |
| 环境变量 | **.env** | 标准格式，便于容器化部署 |
| 程序生成配置 | **JSON** | 机器读写友好 |
| 敏感信息 | **.env** | 配合 .env.example 模板 |

### 2.2 YAML 配置规范

```yaml
# ============================================================
#  文件头 (必须)
# ============================================================
# version: 1.0.0
# updated: 2026-03-31
# description: FTSeek 主配置文件

# ============================================================
#  分组注释 (使用分隔线 + 标题)
# ============================================================

# 基础配置
base:
  app_name: FTSeek
  debug: false
  log_level: INFO

# 数据库配置
database:
  host: localhost
  port: 5432
  name: ftseek
```

**规范要点**:
- 使用 2 空格缩进
- 字符串值可不用引号 (除非包含特殊字符)
- 布尔值使用 `true`/`false` (小写)
- 数组使用 `-` 前缀

### 2.3 .env 文件规范

```bash
# ============================================================
#  FTSeek 环境变量配置
#  version: 1.0.0
# ============================================================

# 🔐 安全相关 (必须修改)
API_TOKEN=your_secure_token_here

# 服务配置
OPEND_HOST=127.0.0.1
OPEND_PORT=11111

# 布尔值使用 true/false (小写)
FTSEEK_DEV_MODE=false

# 数组使用 JSON 格式
WATCH_LIST_HK=["HK.00700","HK.09988"]
```

**规范要点**:
- 全部大写 + 下划线命名
- 敏感值必须有示例模板 (.env.example)
- 注释使用 `#` 开头
- 不提交 .env 到版本控制

### 2.4 JSON 配置规范

```json
{
  "_meta": {
    "version": "1.0.0",
    "updated": "2026-03-31",
    "description": "FTSeek 策略配置"
  },
  "strategy": {
    "name": "momentum",
    "enabled": true,
    "params": {
      "threshold": 0.05,
      "window": 20
    }
  }
}
```

**规范要点**:
- 必须包含 `_meta` 元数据
- 使用 2 空格缩进
- 键名使用 snake_case
- 末尾无逗号

---

## 3. 变量命名规范

### 3.1 命名风格

| 场景 | 命名风格 | 示例 |
|-----|---------|------|
| 环境变量 | **UPPER_SNAKE_CASE** | `API_TOKEN`, `OPEND_HOST` |
| 配置键名 | **snake_case** | `log_level`, `watch_list` |
| 代码常量 | **UPPER_SNAKE_CASE** | `MAX_RETRY_COUNT`, `DEFAULT_TIMEOUT` |
| 代码变量 | **snake_case** | `user_id`, `order_qty` |

### 3.2 命名前缀约定

| 前缀 | 含义 | 示例 |
|-----|------|------|
| `FTSEEK_` | FTSeek 全局配置 | `FTSEEK_DEV_MODE` |
| `OPEND_` | OpenD 相关配置 | `OPEND_HOST`, `OPEND_PORT` |
| `FUTU_` | 富途相关配置 | `FUTU_LOGIN_ACCOUNT` |
| `DINGTALK_` | 钉钉推送配置 | `DINGTALK_WEBHOOK_URL` |
| `CALLBACK_` | 回调服务配置 | `CALLBACK_PORT` |

### 3.3 敏感变量命名

敏感信息必须使用明确的前缀标识：

| 类型 | 命名模式 | 示例 |
|-----|---------|------|
| Token | `*_TOKEN` | `API_TOKEN`, `WEBHOOK_TOKEN` |
| 密码 | `*_PWD` 或 `*_PASSWORD` | `FUTU_LOGIN_PWD` |
| 密钥 | `*_SECRET` 或 `*_KEY` | `DINGTALK_SECRET`, `API_KEY` |
| 私钥 | `*_PRIVATE_KEY` | `RSA_PRIVATE_KEY` |

---

## 4. 配置加载优先级

### 4.1 优先级顺序 (从高到低)

```
1. 命令行参数 (--config, --port 等)
   ↓
2. 环境变量 (OPEND_HOST, API_TOKEN 等)
   ↓
3. .env 文件 (项目根目录)
   ↓
4. config.yaml / config.json (配置文件)
   ↓
5. 代码默认值 (硬编码默认值)
```

### 4.2 加载流程图

```
应用启动
   ↓
加载 .env 文件 (load_dotenv)
   ↓
读取环境变量 (os.getenv)
   ↓
读取配置文件 (YAML/JSON)
   ↓
应用命令行参数
   ↓
验证配置完整性
   ↓
启动应用
```

### 4.3 配置覆盖规则

- **高优先级覆盖低优先级**: 环境变量 > .env 文件 > 配置文件 > 默认值
- **同优先级不合并**: 后加载的配置覆盖先加载的
- **缺失处理**: 必需配置缺失时启动失败，可选配置使用默认值

### 4.4 配置验证

启动时必须验证:

```python
# 必需配置检查
REQUIRED_ENV_VARS = [
    "API_TOKEN",
    "DINGTALK_WEBHOOK_URL"
]

for var in REQUIRED_ENV_VARS:
    if not os.getenv(var):
        raise ConfigurationError(f"必需环境变量 {var} 未设置")
```

---

## 5. 配置变更流程

### 5.1 新增配置项流程

```
1. 在 .env.example 中添加示例
   ↓
2. 在 CONFIG_SPEC.md 中更新文档
   ↓
3. 更新配置加载代码 (支持新配置项)
   ↓
4. 添加配置验证逻辑
   ↓
5. 更新版本号 (MINOR 或 PATCH)
   ↓
6. 通知相关开发人员
```

### 5.2 修改配置项流程

```
1. 评估兼容性影响
   ├─ 向后兼容 → PATCH 版本
   └─ 不兼容 → MAJOR 版本
   ↓
2. 更新 .env.example 和文档
   ↓
3. 添加迁移脚本 (如需)
   ↓
4. 更新版本号
   ↓
5. 发布变更通知
```

### 5.3 废弃配置项流程

```
1. 标记为 deprecated (保留一个版本周期)
   ↓
2. 添加迁移警告日志
   ↓
3. 提供迁移指南
   ↓
4. 下一主版本移除
```

### 5.4 敏感信息变更流程

```
1. 生成新密钥/Token
   ↓
2. 更新 .env 文件 (不提交到版本控制)
   ↓
3. 重启应用使配置生效
   ↓
4. 验证功能正常
   ↓
5. 作废旧密钥 (如有)
```

---

## 6. 安全规范

### 6.1 敏感信息处理

| 要求 | 说明 |
|-----|------|
| ✅ 必须通过环境变量 | API_TOKEN, 密码，密钥等 |
| ✅ 禁止硬编码 | 代码中不得出现明文密钥 |
| ✅ .env 不提交 | 添加到 .gitignore |
| ✅ 提供示例模板 | .env.example 不含真实值 |
| ✅ 使用安全随机数 | `secrets.token_hex(32)` 生成 Token |

### 6.2 配置文件权限

```bash
# 敏感配置文件权限设置
chmod 600 .env           # 仅所有者可读写
chmod 644 .env.example   # 所有人可读
chmod 644 config.yaml    # 配置文件可读
```

### 6.3 密钥轮换

- **API_TOKEN**: 每 90 天轮换
- **Webhook Secret**: 每 180 天轮换
- **服务账号密码**: 每 90 天轮换

---

## 7. 配置示例

### 7.1 完整 .env 示例

```bash
# ============================================================
#  FTSeek 环境变量配置
#  version: 1.0.0
# ============================================================

# 🔐 安全相关 (必须修改)
API_TOKEN=your_secure_token_here
TRADINGVIEW_WEBHOOK_TOKEN=your_webhook_token

# 钉钉推送配置
DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
DINGTALK_SECRET=SECxxx

# OpenD 配置
OPEND_HOST=127.0.0.1
OPEND_PORT=11111
OPEND_DIR=/opt/ftseek/Futu_OpenD_Centos7

# 富途账号 (建议使用 OpenD 配置文件)
FUTU_LOGIN_ACCOUNT=
FUTU_LOGIN_PWD=
TRADE_ENV=1

# 回调服务
CALLBACK_HOST=0.0.0.0
CALLBACK_PORT=8899
CALLBACK_EXTERNAL_URL=http://your-server-ip:8899

# 监控配置
WATCH_LIST_HK=["HK.00700","HK.09988"]
WATCH_LIST_US=["US.AAPL","US.TSLA"]

# 日志配置
LOG_LEVEL=INFO
FTSEEK_DEV_MODE=false
```

### 7.2 完整 config.yaml 示例

```yaml
# version: 1.0.0
# updated: 2026-03-31

# 基础配置
base:
  app_name: FTSeek
  debug: false
  log_level: INFO

# OpenD 配置
opend:
  host: 127.0.0.1
  port: 11111
  trade_env: 1  # 1=模拟，0=实盘

# 监控配置
watch_list:
  hk:
    - HK.00700
    - HK.09988
    - HK.09992
  us:
    - US.AAPL
    - US.TSLA
    - US.NVDA

# 交易配置
trading:
  default_order_qty_hk: 100
  default_order_qty_us: 1
  max_order_amount: 50000.0

# 共识引擎
consensus:
  window: 60
  threshold: 1.5
  cooldown: 300
```

---

## 8. 变更记录

| 版本 | 日期 | 变更内容 | 变更人 |
|-----|------|---------|-------|
| 5.0.0 | 2026-03-31 | P1 配置统一化：创建 unified_config.py，合并配置文件，统一版本管理 | FTSeek Team |
| 1.0.0 | 2026-03-31 | 初始版本，定义配置规范 | FTSeek Team |

---

## 9. 参考文档

- [Semantic Versioning](https://semver.org/)
- [YAML 规范](https://yaml.org/spec/)
- [dotenv 格式](https://github.com/bkeepers/dotenv)
- [12-Factor App 配置](https://12factor.net/config)
