# AGENTS.md

This file provides guidance for AI agents (Hermes, Claude, OpenAI, Cursor, etc.) working with this repository.

## ⚠️ 必读 (Start Here)

Before any work, read `SYSTEM_OBJECTIVE.md` in the project root. It defines:
- Current phase and North Star metrics
- Top 3 focus areas (out-of-scope work requires approval)
- Agent autonomy levels (🟢🟡🔴🚫)
- Decision iron rule (5-question validation)

Then read `AGENT_INTERACTION.md` for the Agent interaction handbook (5-question template + approval flow + git hooks).

## ⚠️ 跨项目注意

本文件属于 **FTSeek 港股** 项目 (`/opt/ftseek/FTSeek/`)。
美股项目 (`/opt/ftseek/FTSeek-US/`) 有独立的治理文件，**不得交叉修改**。

## Quick Rules

1. **不读宪法不动手** — `SYSTEM_OBJECTIVE.md` 是所有决策的起点
2. **5 问必答** — 任何改动必须回答: 服务哪个焦点 / 改什么指标 / 怎么验证 / 最坏情况 / 怎么回滚
3. **🔴 级须人审批** — 交易主路径、风控、启用策略相关改动，停下来等人确认
4. **🚫 禁止事项** — 不切实盘、不删 DB、不删凭据、不 force push
5. **止血 > 优化** — 发现 P0 问题时立即修复，不等排期

## Git Hooks (已启用)

- `pre-commit`: 拦截 .env 修改、治理文件删除、data/*.db 删除、yaml enabled→true、Python 语法错误、SSOT 配置漂移
- `commit-msg`: 高风险改动必须引用 [ADR-XXX]/[RFC-XXX]/[hotfix]

## 治理检查

```bash
# 运行治理合规检查 (待建)
python3 scripts/governance_check.py
```
