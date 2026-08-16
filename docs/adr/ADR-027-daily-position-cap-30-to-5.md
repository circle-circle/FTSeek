# ADR-027: 单日开仓帽 max_daily_positions 30→5 — 复活被解除的僵尸护栏

**状态**: Accepted (用户指令"打磨sim实现稳定收益", DEB-20260815 延伸)
**日期**: 2026-08-15
**决策者**: Hermes (数据驱动) + 用户方向裁定
**回滚**: `max_daily_positions: 30` + `systemctl restart ftseek-us` (单行)

## 背景: 护栏存在但形同虚设

DEB-20260815 诊断"稳定收益"瓶颈时, 发现 config 里的日频护栏与实际行为严重脱节:

1. **8/14 实锤**: 单日成交 14 笔 (10:16-10:17 十笔 LONG 挤在 87 秒), 净 -$794。
2. auto_execute 循环里**明明有**单日开仓数帽 (`pipeline.py:3628`,
   `_today_opened_count >= _max_daily_positions` → skip), 计数从 trade_records
   持久化读取 (重启不丢, ET 时区正确) —— 但 config 值被设为 **30**, 永远不触发。
3. git blame: `71033ed0` 引入 `max_daily_positions: 10`, `da49266d`
   (FTSeek Bot, 7/16 "全面修复交易全链路"大杂烩提交) **无证据地抬到 30**。
   同期被解除的还有 `daily_notional_cap_pct 0.60→0.80`。
4. 水坝修复 (8/14, `max_new_positions_per_cycle=3`) 只是**单轮**刹车
   (每 300s 轮最多 3 笔, 剩余顺延下轮) —— 日级总量无帽, 14 笔分 5 轮照样倾泻。

## 证据 (双窗口 cap 扫描, 时序截断反事实)

7/1 起逐日按入场时序截断, 保留前 N 笔:

| 窗口 | 实际 | cap=4 | cap=5 | cap=6 |
|---|---|---|---|---|
| 7月 (184笔) | -$804 | -$116 | -$245 | +$61 |
| 8月 (78笔) | -$1,215 | -$416 | -$594 | -$695 |
| 全量6-8月 (342笔) | -$118 | — | **+$1,442** (183笔) | — |

- 双窗同号改善 (7月 -$804→-$245, 8月 -$1,215→-$594), 方向稳健。
- 被砍掉的第 6+ 笔质量更差: 保留部分均笔 -$6.7 vs 砍掉部分均笔 -$9.9
  (n=119, 合计 -$1,180)。
- cap=5 与代码默认值一致 (`pb_cfg.get("max_daily_positions", 5)`), 即恢复
  代码本意而非发明新参数。

**诚实声明**: 时序截断是 ex-post 反事实, 非因果证明 —— 安静日本身可能是
赢日 (频率与 regime 相关)。但"同日第 6+ 笔更差"是 within-day 证据, 且 cap
的目的是止血而非提 alpha: 即使砍掉的笔不亏, 少开仓也直接降费用暴露
(费用占毛亏 121%, 见 §二十三)。

## 决策

`playbook.max_daily_positions: 30 → 5`。单变量, 不碰水坝 (3/轮)、
notional 帽 (0.8)、禁触窗 (1015) 等其他护栏。

## 生效方式 (关键)

`PlaybookConfig` 是**启动时快照单例** (`config.py:61 self._config = self._load()`,
无 reset 点), 热重载回调 (`main.py _on_config_reload`) 只同步 ConfigContainer
不清该单例 → **本改动必须重启生效**。重启窗口: 周六 19:40 CST (美股休市,
open_positions=0, 无未平仓风险)。

## 关联发现 (本次不修, 留档治理)

- `dual_mode.max_daily_auto_trades` (3/4/5) 计数器是**内存态**
  (`dual_mode_params.py:119`, 重启归零) 且只挂在老链路 `_submit_order_inner`
  (order_executor.py:3275) —— DA playbook 通道 (现役唯一成交通道) 完全绕过。
  即"第二把日频锁"也是死的。playbook 路径的 DB 计数帽 (本 ADR 复活者) 是
  现役唯一有效的日频 SSOT。
- Qoder 未提交的注释清理 diff 已从工作区隔离存档:
  `docs/debates/qoder_comment_cleanup_unified_config.patch.deprecated-20260816`
  (与 ADR-024"误删证据块"裁决冲突)。⛔ 2026-08-16 用户裁废弃, 禁止应用
  (基线过期: cap=5/blackout=1015 会回退 8/15 生效的 cap=8/0935)。

## 验证

1. YAML 解析通过 (yaml.safe_load)。
2. 新 PlaybookConfig 读值 = 5 (venv 实测)。
3. 重启后服务 active, 周一首个 auto_execute 轮日志应出现
   `max_daily_positions=5` (guardrail 行)。
