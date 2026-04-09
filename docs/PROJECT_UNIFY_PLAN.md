# FTSeek 项目代码统一和路径规范化方案

## 一、现状分析

### 1.1 当前项目结构

**主项目目录**: `/opt/ftseek/FTSeek`

```
/opt/ftseek/FTSeek/
├── app/                      # 主应用代码
│   ├── core/
│   │   ├── trade_executor.py  # 交易执行器 (带人工确认模式)
│   │   └── ...
│   ├── trader/
│   │   ├── executor.py        # 交易执行器 (自动模式)
│   │   └── ...
│   ├── execution/
│   │   ├── smart_router.py    # 智能订单路由
│   │   ├── order_retry.py     # 订单重试
│   │   └── auto_executor.py   # 自动执行器 (含账户选择逻辑)
│   └── ...
├── config/                    # 配置文件
│   ├── app_config.yaml
│   ├── risk_config.yaml
│   └── ...
├── scripts/                   # 脚本工具
│   ├── trade_executor.py      # 独立交易执行脚本
│   └── ...
├── data/                      # 数据目录
├── .env                       # 环境变量配置
└── ...
```

### 1.2 分散代码位置

| 文件 | 位置 | 内容 | 行数 |
|------|------|------|------|
| djH3.py | `/root/.qoder-server/data/User/History/6830f7e7/djH3.py` | 交易执行器完整实现 | 1029 |
| common.py | `/root/.openclaw/skills/openapi/scripts/common.py` | 账户选择逻辑 + 公共工具 | 589 |

### 1.3 关键问题

1. **代码分散**: 交易执行器代码分散在 3 个位置
   - `/opt/ftseek/FTSeek/app/trader/executor.py` (自动模式)
   - `/opt/ftseek/FTSeek/app/core/trade_executor.py` (人工确认模式)
   - `/root/.qoder-server/data/User/History/6830f7e7/djH3.py` (最新版本，包含账户选择逻辑)

2. **账户选择逻辑分散**:
   - `/root/.openclaw/skills/openapi/scripts/common.py` 中的 `select_account_for_code()`
   - `/opt/ftseek/FTSeek/app/trader/executor.py` 中的 `_get_account_for_code()`
   - `/opt/ftseek/FTSeek/app/execution/auto_executor.py` 中的账户选择逻辑

3. **环境变量配置**:
   - `.env` 文件在 `/opt/ftseek/FTSeek/.env`
   - 部分配置在 `app/config.py`
   - 部分配置在 `app/core/config_container.py`

4. **路径依赖**:
   - djH3.py 中硬编码引用 `/root/.openclaw/skills/openapi/scripts/common.py`
   - 这违反了"不引入到其他路径"的核心要求

---

## 二、迁移目标

### 2.1 核心原则

1. **单一数据源**: 所有代码和配置统一到 `/opt/ftseek/FTSeek`
2. **向后兼容**: 保持现有功能不受影响
3. **模块化**: 将公共逻辑提取为可复用模块
4. **可追溯**: 保留代码来源和历史记录

### 2.2 目标结构

```
/opt/ftseek/FTSeek/
├── app/
│   ├── core/
│   │   ├── trade_executor.py      # 统一交易执行器 (整合 djH3.py)
│   │   └── ...
│   ├── execution/
│   │   ├── account_router.py      # 【新增】账户选择逻辑 (从 common.py 迁移)
│   │   ├── smart_router.py        # 智能订单路由
│   │   └── ...
│   └── ...
├── config/
│   └── accounts.yaml              # 【新增】账户配置文件
├── .env                           # 统一环境变量
└── docs/
    └── PROJECT_UNIFY_PLAN.md      # 本方案文档
```

---

## 三、迁移任务拆解

### 3.1 任务列表

| 任务 ID | 任务名称 | 描述 | 优先级 | 风险 |
|---------|---------|------|--------|------|
| T1 | 项目结构分析 | 分析现有代码依赖关系 | P0 | 低 |
| T2 | 账户选择逻辑迁移 | 将 common.py 中的账户选择逻辑迁移到 app/execution/account_router.py | P0 | 中 |
| T3 | 交易执行器整合 | 将 djH3.py 整合到 app/core/trade_executor.py | P0 | 高 |
| T4 | 环境变量统一 | 清理和统一环境变量配置 | P1 | 中 |
| T5 | 依赖关系更新 | 更新所有引用路径 | P1 | 中 |
| T6 | 测试验证 | 执行单元测试和集成测试 | P0 | 高 |
| T7 | 备份和回滚 | 创建备份和回滚方案 | P0 | 低 |

### 3.2 详细任务说明

#### T1: 项目结构分析
- 分析现有 trade_executor 的三个版本差异
- 识别所有引用账户选择逻辑的代码位置
- 绘制依赖关系图

#### T2: 账户选择逻辑迁移
**源文件**: `/root/.openclaw/skills/openapi/scripts/common.py`
**目标文件**: `/opt/ftseek/FTSeek/app/execution/account_router.py`

迁移内容:
- `select_account_for_code()` 函数
- `HK_STOCK_ACCOUNT` 和 `HK_OPTION_ACCOUNT` 常量
- 相关工具函数

#### T3: 交易执行器整合
**源文件**: `/root/.qoder-server/data/User/History/6830f7e7/djH3.py`
**目标文件**: `/opt/ftseek/FTSeek/app/core/trade_executor.py`

整合策略:
- 保留现有 `app/core/trade_executor.py` 的人工确认模式
- 合并 djH3.py 的自动执行功能
- 使用新的 `account_router` 模块替代硬编码路径

#### T4: 环境变量统一
- 审查 `.env` 文件
- 审查 `app/config.py`
- 审查 `app/core/config_container.py`
- 合并重复配置，消除冲突

#### T5: 依赖关系更新
- 更新所有引用 `select_account_for_code` 的代码
- 移除对 `/root/.openclaw/skills/openapi/scripts/common.py` 的硬编码引用
- 更新导入路径

#### T6: 测试验证
- 单元测试：账户选择逻辑
- 单元测试：交易执行器
- 集成测试：完整交易流程
- 回归测试：现有功能验证

#### T7: 备份和回滚
- 备份现有代码
- 创建回滚脚本
- 记录回滚步骤

---

## 四、执行计划

### 4.1 阶段划分

**阶段 1: 准备 (15 分钟)**
- [ ] 创建备份
- [ ] 记录当前状态
- [ ] 准备测试环境

**阶段 2: 迁移 (30 分钟)**
- [ ] 创建 account_router.py
- [ ] 整合 trade_executor.py
- [ ] 更新依赖引用

**阶段 3: 测试 (30 分钟)**
- [ ] 运行单元测试
- [ ] 运行集成测试
- [ ] 验证功能完整性

**阶段 4: 清理 (15 分钟)**
- [ ] 移除旧代码引用
- [ ] 更新文档
- [ ] 最终验证

### 4.2 时间估算

| 阶段 | 预计时间 | 实际时间 |
|------|---------|---------|
| 准备 | 15 分钟 | - |
| 迁移 | 30 分钟 | - |
| 测试 | 30 分钟 | - |
| 清理 | 15 分钟 | - |
| **总计** | **90 分钟** | - |

---

## 五、风险评估

### 5.1 风险矩阵

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 代码整合后功能异常 | 中 | 高 | 充分测试，保留回滚方案 |
| 账户选择逻辑错误导致交易失败 | 低 | 高 | 单元测试覆盖所有场景 |
| 环境变量配置冲突 | 中 | 中 | 逐步迁移，逐项验证 |
| 依赖路径更新遗漏 | 中 | 中 | 全局搜索，全面检查 |

### 5.2 回滚方案

如迁移失败，执行以下回滚步骤:

```bash
# 1. 停止 FTSeek 服务
pkill -f "python.*main.py"

# 2. 恢复备份
cd /opt/ftseek/FTSeek
cp -r backups/pre_unify_backup/app app
cp backups/pre_unify_backup/.env .env

# 3. 重启服务
./start.sh
```

---

## 六、验收标准

### 6.1 功能验收

- [ ] 账户选择逻辑正确工作（正股→18820895，期权→8246818）
- [ ] 交易执行器正常下单（港股/美股）
- [ ] 环境变量配置正确加载
- [ ] 所有现有测试通过

### 6.2 代码验收

- [ ] 无对 `/root/.qoder-server` 的引用
- [ ] 无对 `/root/.openclaw/skills/openapi/scripts/common.py` 的硬编码引用
- [ ] 所有代码在 `/opt/ftseek/FTSeek` 目录内
- [ ] 代码结构清晰，模块职责明确

### 6.3 文档验收

- [ ] 更新 README.md
- [ ] 更新配置说明
- [ ] 记录迁移日志

---

## 七、执行记录

### 7.1 迁移日志

| 时间 | 操作 | 执行人 | 状态 |
|------|------|--------|------|
| - | - | - | - |

### 7.2 问题记录

| 问题 | 解决方案 | 状态 |
|------|---------|------|
| - | - | - |

---

## 八、后续优化建议

1. **配置管理**: 考虑使用配置中心统一管理环境变量
2. **代码复用**: 将公共工具函数提取为独立模块
3. **测试覆盖**: 增加单元测试覆盖率至 80%+
4. **文档完善**: 补充 API 文档和使用说明

---

*文档版本：v1.0*
*创建时间：2026-03-31*
*最后更新：2026-03-31*
