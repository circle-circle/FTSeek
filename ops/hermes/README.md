# ops/hermes/ — Hermes 运维脚本版本控制 SSOT (2026-08-24, 复查A5)

## 问题

`/root/.hermes/scripts/` 下 16+ 个 FTSeek 运维脚本（盘中诊断、止损监控、
alpha 回填、影子采集…）由 hermes cron 调度，却不受 git 版本控制。
2026-08-20 的 alpha_backfill.py 超时修复（commit 0d0a6a6f）就发生在库外文件上
——修了什么、何时修的、能否回滚，git 里零痕迹。

## 方案：复制入库 + 双向校验和漂移告警（不用符号链接）

- 本目录是**记录性 SSOT**：每次修改线上脚本后，必须把副本同步回本目录并随
  commit 入库；反向，改本目录文件后需部署到 `/root/.hermes/scripts/` 才生效。
- `tests/test_ops_scripts_sync.py` 做**双向 sha256 对账**：
  1. 库内每个文件 ↔ 线上同名文件内容一致；
  2. 线上目录里不得出现库内没有的新脚本（alpha_backfill 事故的复发防线）。
  任一方向漂移 → 测试红 → 强制同步动作进 diff。
- **刻意不用 symlink**：冻结期把生产 cron 直接执行的文件路径耦合到 git 工作区
  状态（revert/reset/clean 都会瞬时改变线上行为）风险大于收益。解冻后若要
  切换为 symlink SSOT，另行评审。

## 边界

- 只收编 `/root/.hermes/scripts/` 下的 FTSeek 运维脚本。
- `/root/.hermes/skills/deep-stock-analysis/**` 是强耦合 DA skill 包（含数据
  文件与 .bak），整体迁移风险高，不在本批范围。
- hermes 自身调度配置 (`cron/jobs.json`) 由 hermes 管理，不入库。

## 流程

```bash
# 改了线上脚本 → 回库
cp /root/.hermes/scripts/foo.py ops/hermes/foo.py && git add -A && git commit

# 改了库内副本 → 部署上线
cp ops/hermes/foo.py /root/.hermes/scripts/foo.py

# 对账
PYTHONPATH=. venv/bin/python -m pytest tests/test_ops_scripts_sync.py -q
```
