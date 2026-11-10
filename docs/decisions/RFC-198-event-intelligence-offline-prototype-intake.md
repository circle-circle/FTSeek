# RFC-198 未入库 WIP 归位: Event-Intelligence 离线原型 + 回放/审计脚本 (2026-11-10)

状态: **已落地** (纯 git 归位; 零运行时行为变更, 无需重启)
分类: L5 文档/归档 (代码内容逐字未改, 唯一例外见 §3 的 5 处 except 留痕)
关联: DEB-20261106 §17.1/§17.7 (离线原型隔离契约, `tests/test_offline_prototype_isolation.py`),
      RFC-192 / RFC-194, RFC-165 (同类"归档前序会话 WIP"先例), RFC-197 (棘轮清剿)

## 1. 为什么必须归位

这批文件在工作区滞留 1–6 天 (最早 mtime 11-02, 最晚 11-10 08:59), 全部 `??`/未跟踪, 已造成三处读数不可复现:

1. **棘轮计数按磁盘 rglob 扫**: 未跟踪脚本照样进分母 ⇒ "HEAD 上到底几处"与"盘上几处"是两个数,
   任何"清剿后是多少"的结论都不可复现 (本轮 RFC-197 就踩到, 只能靠逐文件差集拆归因)。
2. **全文测试面按磁盘收集**: 这 7 个测试已在每晚回归里跑 (61 passed), 但它们不在 git 里 ⇒
   换机/重建即静默消失, 而"测试全绿"这条证据链随之断裂。
3. **职责不可区分**: 后续任何 P0 修复与这批改动共处同一工作树, 谁改的、改了什么无从二分。

## 2. 入了什么 (逐字未改)

| 类 | 文件 | 说明 |
|---|---|---|
| app 离线原型 | `app/event_stream/{instructions,intelligence,market_brief}.py` (487/490/484 行) | **零生产消费者** (契约测试第 2 条守住); 只被 `scripts/da_event_*.py` 与 `tests/` 引用 |
| 离线回放/审计脚本 | `scripts/{da_citation_ab_replay,da_event_arbitration_replay,da_event_instruction_replay,da_event_intelligence,da_expectancy_report,rfc192_verification,restore_da_admission_instructions}.py` | **均不在 crontab** (逐个核对); 除 `restore_da_admission_instructions` 外全部 `mode=ro` 只读开库 |
| 研究批脚本 | `scripts/{early_tight_acceptance_replay,entry_confirm_chase_cap_evidence,playbook_fee_gate_audit,da_regime_divergence_check}.py` | 同样不在 cron; 只读 `trade_records` / checkpoint |
| 测试 | `tests/test_{event_intelligence,instruction_arbitration,instruction_lifecycle,intraday_instructions,market_brief,restore_da_admission_instructions,signal_ic_tracker}.py` | 已在盘上跑绿 (**61 passed**) |
| 文档 | `docs/analysis/llm-endpoint-eval-2026-11.md`, 本文件, `handoff.md` (11-09 批收口记录) | 纯文档 |

## 3. 与"逐字未改"的唯一例外 (诚实声明)

`scripts/{early_tight_acceptance_replay,entry_confirm_chase_cap_evidence,playbook_fee_gate_audit,da_regime_divergence_check}.py`
这 4 个文件里的 **5 处 `except: pass/continue`** 已由 RFC-197 就地清剿 (改 stderr 留痕, 零控制流变更) ——
那批被判定"不属本批、不 add", 故那次提交把修复留在工作区。**本提交把它们随文件一起入库**,
侧记: 归位动作因此顺带清掉 RFC-197 遗留的 5 处, 棘轮计数不变 (899, 修复前这 5 处已在盘上被清掉)。

## 4. 安全面核对 (逐项实测)

| 项 | 结果 |
|---|---|
| 语法 | 21 个 `.py` 全部 `py_compile` 通过 |
| 导入 | `app.event_stream.{instructions,intelligence,market_brief}` 三模块均 import OK |
| 隔离契约 | `tests/test_offline_prototype_isolation.py` **3 passed** —— 包 `__init__` 无 re-export、`app/` 下无指向三模块的 import 字符串 (生产面零消费者) |
| 完整测试 | 7 个新测试 **61 passed** |
| 写口 | 除 `restore_da_admission_instructions` (dry-run 默认, `--apply` 才写且写前落 backup JSON) 外, 全部 `sqlite3.connect(... ?mode=ro)` |
| 生产库/相对路径 | 无 `Path("data/...")` 相对写口; 库路径一律 `Path(__file__).resolve().parents[1]` 锚定 |
| 密钥 | 无 `sk-` / api_key / token / password 字面量 |
| lint | 只有 E402 (脚本 sys.path 后再 import, 仓内同风格) 与 3 处 F401 未用 import; 无 F821 |
| cron 载体 | 上述脚本 **无一条**在 `crontab -l` 里 ⇒ 纯离线工具 |
| `/root/.hermes/skills/...` 硬编码 | 3 处, 与已入库代码同形态 (仓内 57 处既有), 属既定约定 |

## 5. 行为与部署

- 零运行时变更: 无生产消费者、无 cron 载体、无 schema/配置改动 ⇒ **不需重启** (当前进程 09:18 启动,
  这些文件 08:59 前已在盘上且不被 import, 与本提交内容逐字一致)。
- 归位后 HEAD 与工作区在两个棘轮上同读数 (899), 全文测试面也和 git 一致了。

## 6. 回滚

`git revert <this>` 即回到未跟踪态 (文件仍在盘上运行, 与提交前完全等价)。不涉及数据/配置回滚。