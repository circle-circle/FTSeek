# DOCS-GUIDE — 项目文档管理约定

> 用途: 约定"人写的设计/决策/方案文档"如何组织, 让几百个小功能的文档**可检索、不腐烂、有生命周期**。
> 性质: 项目治理层规范 (与 SYSTEM_OBJECTIVE / pre-commit 治理钩子 / OKR 同层), **不属于任何业务模块**。
> 适用范围: 仅 `docs/` 下人写文档。**不含** `obsidian_vault/` (那是 obsidian_writer.py 代码自动生成的运行时知识库, 自带 front-matter, 有独立 tag 体系, 本约定不干预)。

---

## 1. 什么文档才配独立建档 (准入门槛)

大部分小改**不建文档**。只有满足以下**任一**条件才值得单独一份 `.md`:

- 跨会话还要复看 (方案/设计/取舍需要日后回溯)
- 涉及决策取舍 (为什么选 A 不选 B, 有 ponytail 层面的权衡)
- 影响多个模块或改变了系统契约

**不建文档的场景** (只靠 commit message + 代码注释即可):
- 一次性 bugfix / 参数微调 / 重命名
- 纯实现细节, 无取舍, 看代码即懂

> 原则: 文档是**债**不是资产。宁可少建, 建了就维护; 不确定要不要建 → 先不建。

---

## 2. front-matter 元数据模板 (强制)

每份**新**文档第一行起必须是 YAML front-matter (单人 + 多 Agent 维护, 统一可解析是核心价值):

```yaml
---
title: 一句话标题
status: active            # draft | active | deprecated
module: universe_expansion  # 所属业务模块; 跨模块填 cross / governance
related_code: scripts/llm_universe_scan.py   # 相关代码路径; 可空
related_okr: OKR-2026-07   # 可空
related_adr: ADR-007       # 可空
tags: [llm, eval, shadow]
updated: 2026-07-10        # 最后实质修改日期
---
```

字段说明:
- `owner` **刻意不设** — 单人 + Bot 提交, owner 恒等于 bot, 是噪音。将来若真有人协作再加。
- `status` 是生命周期核心 (见 §4)。
- `related_*` 用于将来自动索引时反向关联; 空着无妨, 不强求填满。

---

## 3. 放置规则 (混合)

| 文档类型 | 放置位置 | 示例 |
|---|---|---|
| 跨模块 / 治理 / 决策 | `docs/<类别>/` | `docs/decisions/`, `docs/adr/`, `docs/proposals/` |
| 单个功能的设计文档 | `docs/design/` 或就近该功能语义分类 | `docs/design/universe-expansion-eval.md` |

现有子目录约定沿用: `debates/ decisions/ proposals/ specs/ design/ adr/ architecture/ rollback/ incidents/ references/ analysis/`。

> 就近原则的目的: 让文档和它描述的代码"物理接近", 防止代码改了文档没人记得改 (腐烂)。

---

## 4. status 生命周期

```
draft ──→ active ──→ deprecated
```

- `draft`: 起草中, 结论未定, 可能推翻。
- `active`: 当前有效, 是该功能的事实来源。
- `deprecated`: 已过时。**不删文件**, 在 front-matter 下方加一行 `> ⚠️ 已废弃, 被 <新文档路径> 取代 (日期)`。

同一功能同时最多一份 `active`。新版定稿时把旧版置 `deprecated` 并指向新版。

---

## 5. 索引 (Phase 2, 手动触发)

将来提供一个脚本 (拟 `scripts/gen_docs_index.py`) 扫描 `docs/**/*.md` 的 front-matter, 生成 `docs/INDEX.md` (按 module/status/tag 聚合)。

- **手动跑**, 不进 CI, 不加钩子 (最轻, 避免过度设计)。
- 只认合规 front-matter 的文档; 无 front-matter 的历史文档不纳入。

> 该脚本尚未实现, 需要时再做 (YAGNI)。

---

## 6. 历史文档

现有 `docs/` 下 69 份历史文档**全部第一行不是 front-matter**, 一律**不回填**、不动。
本约定**只从新文档起**生效。历史文档若某天被实质修改, 顺手补 front-matter 即可, 不搞专项迁移。

---

## 7. 与 ponytail 原则的一致性

- **避免过度设计**: 不做版本注册表 / 自动准入判定 / 强制 CI 校验; 准入靠人判断, 索引靠手动脚本。
- **实证不虚构**: 本约定基于对现状的实测 (obsidian_vault=机器产物 / docs 0 份 front-matter / 单人维护) 制定, 非拍脑袋。
- **一次一变量**: 先定约定 (本文件), 再做工具 (索引脚本), 分阶段。
