# Agent 交互手册 — FTSeek-US

> 本文件供所有 Agent（Hermes/Claude Code/其他 LLM）在执行任何任务前阅读。

---

## 1. 启动后第一件事

```
1. cat /opt/ftseek/FTSeek-US/SYSTEM_OBJECTIVE.md    # 读宪法
2. cat /opt/ftseek/FTSeek-US/CLAUDE.md              # 读行为准则
3. python3 scripts/governance_check.py              # 查当前状态
4. git log --oneline -5                              # 查最近提交
```

**不做这 4 步就开始改代码 = 违规。**

---

## 2. 判断该不该做：5 问模板

在开始任何改动前，先回答这 5 个问题：

```markdown
## 决策自检

1. **为何**: 我做的这个改动服务 F1/F2/F3 中的哪一条？
   - 答: 

2. **预期**: 如果成功，哪个量化指标会变好？变多少？
   - 答: 

3. **回滚**: 如果搞砸了，一行命令回滚是什么？
   - 答: 

4. **最简**: 有没有不改代码的方案？
   - 答: 

5. **边界**: 这个改动在 🔴/🟡/🟢 哪个级别？
   - 答: 
```

如果答不出任何一个问题 → **停下来，先问用户**。

---

## 3. 分级审批流程

### 🔴 级（必须人审）

触发条件：
- 修改交易主路径代码
- 风控参数
- 启动/停止服务
- 策略 enabled → true

流程：
```
1. 写 RFC 文档到 docs/decisions/RFC-XXX.md
2. 用以下格式提交给用户:
   ┌─────────────────────────────────┐
   │ 🔴 需要审批                     │
   │                                 │
   │ 改动: [描述]                    │
   │ 影响: [F1/F2/F3]                │
   │ 预期: [量化指标]                │
   │ 回滚: [一行命令]                │
   │ RFC: docs/decisions/RFC-XXX.md  │
   │                                 │
   │ 请回复 OK / 修改 / 拒绝         │
   └─────────────────────────────────┘
3. 等待用户回复 OK 后才执行
```

### 🟡 级（可自主但须记录）

触发条件：
- 新增非核心功能
- 跨模块重构
- 性能优化

流程：
```
1. 写 ADR 文档到 docs/decisions/ADR-XXX.md
2. 执行改动
3. git commit -m "[ADR-XXX] 描述"
4. 在 commit message 中包含 5 问中至少 2 个
```

### 🟢 级（可自主）

触发条件：
- Bug 修复（已有 ADR 范围内的）
- 日志调整
- 文档更新

流程：
```
直接执行，commit message 包含变更理由即可
```

---

## 4. Git hooks 拦截说明

项目配置了 2 个 git hooks：

### pre-commit hook
- ❌ 阻止修改 .env 文件
- ❌ 阻止删除治理文件 (SYSTEM_OBJECTIVE.md 等)
- ❌ 阻止删除 data/*.db 文件
- ⚠️ 警告 yaml enabled→true 改动
- ✅ 自动 Python 语法检查

### commit-msg hook
- ❌ 高风险文件改动必须引用 [ADR-XXX]/[RFC-XXX]/[hotfix]
- ⚠️ 非 chore 类型需包含 5 问中至少 2 个

绕过方式（仅限紧急情况）：
```bash
git commit --no-verify    # 跳过所有 hooks
```

---

## 5. 提交格式

```
[ADR-XXX] 简短描述
[RPC-XXX] 简短描述
[hotfix] 简短描述
[chore] 简短描述

# 建议包含的决策要素（写在 body 里）:
为何: 服务 F1/F2/F3
预期: 量化指标
回滚: 一行命令
```

---

## 6. 常用命令速查

```bash
# 查当前状态
python3 scripts/governance_check.py

# 查最近提交
git log --oneline -10

# 查决策记录
ls docs/decisions/

# 回滚最近一次提交
git reset --soft HEAD~1

# 查看宪法
cat SYSTEM_OBJECTIVE.md

# 查看服务状态
systemctl status ftseek-us
```

---

## 7. 反指标清单（绝对不能触碰）

| 反指标 | 阈值 | 后果 |
|--------|------|------|
| 数据库文件大小 | > 500MB | 立即清理 |
| data/*.db 删除 | 任何 | pre-commit 拦截 |
| .env 文件修改 | 任何 | pre-commit 拦截 |
| 日志文件大小 | > 100MB | 配置 rotate |
| 单次改动文件数 | > 10 | 需要 RFC |

---

## 8. 紧急情况

如果发现自己即将违反治理规则但有"合理理由"：

1. **停下来**
2. **记录理由**到 docs/decisions/emergency-YYYY-MM-DD.md
3. **使用 --no-verify** 执行
4. **立即通知用户**
5. **事后补 ADR**

这不是鼓励违规，而是确保违规有据可查。
