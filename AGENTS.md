# FTSeek-US · AGENTS 入口

> 本文件是 agent 入口路由，不含细则。**权威内容在 `AGENT.md`**；两处冲突时以 `AGENT.md` 为准。
> 立此文件的原因：Codex CLI 只自动加载 `AGENTS.md`，Claude Code 只自动加载 `CLAUDE.md`；
> 本仓治理文件原名 `AGENT.md`（单数），两个 harness 都不会自动注入——治理长期靠 agent 碰巧去读。
> Claude Code 侧如需同等接线，另建一行 `CLAUDE.md` 指针即可（本批未建，避免第三份拷贝）。

## 必读顺序（接手任何任务前）

1. `AGENT.md` — 运维/架构规矩 + 军规（权威）
2. `handoff.md` 头部条目 + 各条 `下一步` — 进度队列（唯一 SSOT）
3. `docs/specs/multi-agent-collaboration-charter.md` — 协作宪章
4. `docs/OKR-2026-11.md` — 当前阶段约束（2026-11-01 起，已批准；10/31 前仍按 `docs/OKR-2026-10.md`）
5. 相关 DEB / RFC — 议题状态

术语与口径先查 `GLOSSARY.md` 与宪章「数据口径九律」。

## 高压线（路由摘要，细则见 AGENT.md）

- 重启只用 `systemctl restart ftseek-us`，之后必跑启动冒烟；禁止 `python main.py --start/--stop` 或 `./start.sh` 手动重启。
- Playbook 是唯一下单/持仓管理管线；主管线已冻结新仓，`scan_loop` 保留运行不可整体 gate 掉。
- 重启前核：在途出场 SQL + `git status --porcelain -- app/ scripts/`，不许顺带上线别的 agent 的在途 WIP。
- 新增推送必须走 `app/push/` hub 且默认静默（无料不推）；禁止直连 notifier 或自写 webhook。
- 行为变更一律影子先行 + 落档（DEB/RFC）+ 一次一变量；纯 `.md` 治理接线不触发重启。

## 指针

- 术语表：`GLOSSARY.md`
- 文档放置与生命周期：`docs/DOCS-GUIDE.md`
- 判据字典：`docs/ADJUDICATED-CRITERIA.md`
