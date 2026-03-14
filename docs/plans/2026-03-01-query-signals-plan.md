# 信号查询 CLI 工具 - 实施计划

**设计文档**: `docs/plans/2026-03-01-query-signals-design.md`  
**创建日期**: 2026-03-01  
**预计时间**: 15-20 分钟

---

## 任务列表

### Task 1: 创建基础脚本框架

**目标**: 创建 `scripts/query_signals.py` 基础结构，包含 argparse 参数解析

**参数**:
- `--stock`: 股票代码 (可选，如 HK0700)
- `--from`: 开始日期 (可选，YYYY-MM-DD)
- `--to`: 结束日期 (可选，YYYY-MM-DD)
- `--status`: 信号状态 (可选，pending/executed/ignored)
- `--strategy`: 策略名 (可选)
- `--limit`: 限制条数 (默认 50)
- `--format`: 输出格式 (默认 table, 可选 json/csv)
- `--output`: 输出文件 (可选，默认 stdout)
- `--daily-report`: 日报模式 (可选)
- `--weekly-report`: 周报模式 (可选)

**测试**:
```bash
# 测试帮助信息
python3 scripts/query_signals.py --help
# 预期：显示所有参数说明

# 测试无参数 (应返回错误或使用默认查询)
python3 scripts/query_signals.py
# 预期：显示最近 50 条信号
```

**验收**:
- [ ] 脚本可执行
- [ ] `--help` 显示完整参数说明
- [ ] 参数解析正确

---

### Task 2: 实现数据库查询函数

**目标**: 实现核心查询逻辑，从 `data/ftseek.db` 查询 signals 表

**功能**:
- 根据参数动态构建 SQL WHERE 子句
- 支持多条件组合查询
- 支持日期范围过滤
- 支持 ORDER BY created_at DESC
- 支持 LIMIT

**测试**:
```bash
# 测试基础查询 (无参数)
python3 scripts/query_signals.py --limit 5

# 测试股票过滤
python3 scripts/query_signals.py --stock HK0700 --limit 5

# 测试日期过滤
python3 scripts/query_signals.py --from 2026-02-01 --to 2026-02-28 --limit 5
```

**验收**:
- [ ] 查询返回正确数据
- [ ] 多条件组合查询正确
- [ ] 日期过滤正确
- [ ] 无数据时返回空列表 (不报错)

---

### Task 3: 实现终端表格输出

**目标**: 实现默认的输出格式 - 终端表格

**功能**:
- 尝试导入 `tabulate` 库
- 如果可用，使用 `tabulate` 美化输出
- 如果不可用，降级为简单表格
- 显示关键字段：id, code, action, price, confidence, status, created_at

**测试**:
```bash
python3 scripts/query_signals.py --limit 10
# 预期：显示格式化的表格
```

**验收**:
- [ ] 表格对齐美观
- [ ] 中文显示正常
- [ ] 空数据处理正确

---

### Task 4: 实现 JSON 输出

**目标**: 支持 `--format json` 输出

**功能**:
- 将查询结果转换为 JSON 数组
- 支持 `--output file.json` 写入文件
- 默认输出到 stdout

**测试**:
```bash
# 输出到 stdout
python3 scripts/query_signals.py --stock HK0700 --format json --limit 3

# 输出到文件
python3 scripts/query_signals.py --stock HK0700 --format json --output /tmp/test.json
cat /tmp/test.json | python3 -m json.tool
```

**验收**:
- [ ] JSON 格式正确
- [ ] 可被 `json.tool` 解析
- [ ] 文件写入正确

---

### Task 5: 实现 CSV 输出

**目标**: 支持 `--format csv` 输出

**功能**:
- 使用 `csv` 模块输出
- 第一行为表头
- 支持 `--output file.csv` 写入文件
- 默认输出到 stdout

**测试**:
```bash
python3 scripts/query_signals.py --stock HK0700 --format csv --output /tmp/test.csv
head /tmp/test.csv
```

**验收**:
- [ ] CSV 格式正确
- [ ] 可用 Excel 打开
- [ ] 中文无乱码

---

### Task 6: 实现日报/周报模式

**目标**: 支持 `--daily-report` 和 `--weekly-report` 模式

**功能**:
- `--daily-report`: 查询昨天至今的信号
- `--weekly-report`: 查询过去 7 天的信号
- 自动设置日期范围
- 必须配合 `--output` 使用 (输出为 JSON)
- 输出包含统计信息 (总数、各状态数量、平均置信度)

**测试**:
```bash
python3 scripts/query_signals.py --daily-report --output /tmp/daily.json
python3 scripts/query_signals.py --weekly-report --output /tmp/weekly.json

# 验证统计信息
cat /tmp/daily.json | python3 -c "import json,sys; d=json.load(sys.stdin); print('stats' in d)"
```

**验收**:
- [ ] 日期范围自动计算正确
- [ ] 输出包含统计数据
- [ ] 必须指定 `--output` (否则报错)

---

### Task 7: 添加错误处理和日志

**目标**: 完善错误处理和日志记录

**功能**:
- 捕获数据库异常
- 友好的错误消息
- 使用 logging 记录关键操作
- 数据库不存在时的提示

**测试**:
```bash
# 测试数据库不存在
mv /opt/ftseek/FTSeek/data/ftseek.db /opt/ftseek/FTSeek/data/ftseek.db.bak
python3 scripts/query_signals.py --limit 5
# 预期：友好的错误提示
mv /opt/ftseek/FTSeek/data/ftseek.db.bak /opt/ftseek/FTSeek/data/ftseek.db

# 测试无效日期
python3 scripts/query_signals.py --from invalid-date
# 预期：日期格式错误提示
```

**验收**:
- [ ] 错误消息友好
- [ ] 日志记录正常
- [ ] 异常不会导致程序崩溃

---

### Task 8: 最终测试和文档

**目标**: 端到端测试 + 更新文档

**测试**:
```bash
# 完整测试所有功能
python3 scripts/query_signals.py --help
python3 scripts/query_signals.py --limit 10
python3 scripts/query_signals.py --stock HK0700 --format json --limit 3
python3 scripts/query_signals.py --status executed --format csv --output /tmp/executed.csv
python3 scripts/query_signals.py --daily-report --output /tmp/daily.json
```

**文档更新**:
- 在 `QUICKSTART_V2.md` 添加查询工具使用说明
- 或在 `docs/` 下创建 `QUERY_TOOL.md`

**验收**:
- [ ] 所有测试通过
- [ ] 文档完整
- [ ] 代码提交 git

---

## 执行模式

选择以下一种方式执行：

### 选项 A: 子代理驱动 (推荐)
- 每个任务由独立子代理实现
- 每个任务完成后经过 spec review + code quality review
- TDD 强制：先写测试，再实现

### 选项 B: 手动执行
- 用户自己按任务列表逐个实现
- AI 提供指导和代码审查

---

**准备就绪。请选择执行模式。**
