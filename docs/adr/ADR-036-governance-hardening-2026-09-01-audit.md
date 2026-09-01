# ADR-036: 治理加固批 — 挂载核对哨兵 + KR2 冻结豁免通道落地 + 备份轮转

- **状态**: 已实施 (2026-09-01, 用户裁"按建议执行")
- **缘起**: 2026-09-01 全链体检 (用户问"除交易结果外全链是否正常") 及其后的建议执行。
  两日内连续发现两例"已登记但从未挂载"悬空观察 (gap_chase_gate B1 / ssr_shadow_daily),
  证明注册表在册 ≠ 观察在跑, 需要机械核对; 同批定性冻结告警与备份增长。

## 决策

### ① 挂载核对哨兵 (`shadow_registry.check_mounting()`, 周检接入)
- 对全部 OBSERVING/READY 事项双向核对:
  - **eval 挂载**: eval_script 是否在 crontab / scheduler_core / ops/hermes / 采集脚本
    引用中 (注册表自身与脚本自身引用不算); `eval_on_demand=True` 事项豁免。
  - **数据产出**: data_path 文件超龄 (>14天无更新=疑似断流) 或缺失 (创建<10天初生豁免;
    DB 内口径跳过)。
- 告警经周六周检推送, 空列表=全部在挂。
- 首跑定性: `p1_3_vol_p50` 悬空+数据停滞 (留告警待用户裁); `vwap_gate_ab` 定性
  裁点触发型 (eval_on_demand=True)。

### ② KR2 冻结告警定性与豁免通道落地
- 周检"参数冻结已破2版本"查实为**已裁决变更**: `45e62f0f` (8/29 17:10, 用户裁
  "盈利三漏治理①③⑤") early_tight_sl 影子毕业转正 (先于 9/1 C1 冻结); 新版本全部
  5 笔交易 8/31 09:30-09:56 ET 退出, 时间线吻合; 9/1 冻结开始后零变更。
- 豁免落 `data/config_freeze_baseline.json` exit_versions (KR2 例外通道, 文件自带
  "改本文件=改判据"纪律注记); 周检 check_freeze 读清单, 仅未豁免版本告警。
- **纪律**: 列口径 (config_version 列, W1 夜检) 与退出参数哈希口径
  (metadata_json.config_version, 周检) 并存不合并 — 前者粗后者细, 各有守卫。

### ③ 备份轮转 (`scripts/rotate_data_backups.py`, cron 周日 08:00)
- 日期目录保留最近 14 个; 超龄目录与散落件 (>30天) → `archive/*.tar.gz`
  **只归档不删证**, 幂等, `--dry-run` 支持。

## 明确不做
- 不合并两套 config_version 口径 (语义不同, 合并=改判据)。
- 不自动处置 p1_3_vol_p50 (杀/复活均需用户裁决, 哨兵只负责让它可见)。
- 不删任何备份原件 (归档物证, 删除须人工裁决)。

## 回滚
- 哨兵: 删 `check_mounting`/`_mounted_sources` + 周检 mounts 两段 + 测试。
- 豁免: 删 exit_versions 键 + 周检豁免分支 (告警恢复)。
- 轮转: 删 cron 行 + 脚本。
