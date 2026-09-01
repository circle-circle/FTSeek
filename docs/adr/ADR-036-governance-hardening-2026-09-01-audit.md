# ADR-036: 治理加固批 — 挂载核对哨兵 + KR2 冻结豁免通道落地 + 备份轮转

- **状态**: 已实施 (2026-09-01, 用户裁"按建议执行"); 同日复查收口批追加 (见文末)
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

## 复查收口批 (2026-09-01 同日, 用户裁"按建议收口")

对①②③的独立复查发现哨兵存在**注释即挂载**盲区并实锤两例悬空, 全批收口如下:

### ④ 哨兵收紧: 引用判据从"文本含"改为"代码含"
- **病灶**: `_mounted_sources` 对 ops/hermes/scripts 源文件做原文子串匹配 — 注释
  或 docstring 里提到 eval 路径也算"在挂"。实锤: `da_glm_shadow_eval.py` 无任何
  cron, 仅因 scripts/da_glm_shadow.py:165 注释提及而被判在挂 (悬空观察被哨兵
  纵容); overnight 判罪线同理只活在 docstring 里。
- **修法**: `_code_only()` tokenize 剥离 COMMENT + 行首 STRING(docstring), 仅对
  剩余代码判定引用 (subprocess 参数 / runner 常量 = 真实引用形态); tokenize 失败
  回退原文 (宁过挂不误报悬空)。cron 注释行 (`#` 开头) 同步排除。
- **核对面扩容**: `_cron_surface_texts()` 并入 `/etc/crontab` + `/etc/cron.d`
  全部文件 (含点号文件名 — 401bedcb 僵尸 cron 38 天教训: Alinux8 cronie 执行
  dotfile 名), 不再只看 `crontab -l`。
- 验证: 收紧后哨兵首跑报 2 悬空 (da_glm_shadow + overnight_kill_criterion),
  补挂 cron 后归零; 测试 +10 例 (注释/文档串/子进程参数/常量/点号 cron 面)。

### ⑤ da_glm_shadow 裁决时点修正 + 一次性 cron
- 注册表原写"9/14 由 eval 脚本机械裁决"有误导: 窗口 8/31-9/14 的最后一晚采集在
  9/14 夜, 裁决须在 **9/15** 跑。
- 补挂一次性 cron `35 6 15 9 *` (标记 DA-GLM-ONE-SHOT-ADJUDICATION), 跑完
  `_self_remove_cron()` 自删行; eval 落盘 JSON 快照 + 飞书推送 verdict。
- 注册表 count_mode 改 `where:run_mode=live` (test 行不再计入样本量), 判据文案
  同步改 9/15 口径。

### ⑥ overnight 判罪线从 stdout 提为注册表项 (B15)
- 病灶: 8/29 注册的"PROD_KILL_CRITERION_REGISTERED"只在 overnight_arm_report
  stdout, 无 cron 无落盘 — GRADUATED 项被哨兵跳过, 判罪线对一切自动化不可见。
- 收口: 新注册 `overnight_kill_criterion` (OBSERVING, min_n=20, eval=
  overnight_arm_report.py), 补挂 cron `20 6 * * 2-6`, 判罪线进度落盘
  `data/shadow/overnight_arm_report_latest.json` (顶层 n=已平仓数=计数锚);
  判据入字典 B15。首跑 9/1: n=6/20, 修复率 50%, KEEP_SAMPLING。

### ⑦ 小修
- KR2 豁免契约钉子 (`tests/test_kr2_freeze_exemption.py`): exit_versions 键集 ==
  两个已知版本, 新增豁免必须改测试 = 强制留痕; 每条豁免必须带裁决说明。
- da_blocked_signal_cf_eval: quality_score=None 入 unknown 桶 (原 `(q or 0)`
  把缺失扭曲成 le30)。
- rotate_data_backups: tar 先校验 (成员集 + 全文件读通) 再删原件, 校验失败回滚
  (删坏档保原件, failed 计数退出码 1); 既有损坏归档保留待人工 (重打包=毁证)。

### 收口批回滚
- 哨兵: 还原 `_mounted_sources` 为原文匹配 (不建议, 会复活盲区)。
- da_glm: 删 9/15 一次性 cron 行 (自删机制本身即回滚)。
- overnight: 删 cron 行 + 注册表 overnight_kill_criterion 项 + OUT_JSON 落盘段。
- 豁免钉子: 改 KNOWN_EXEMPT 集 = 强制走本 ADR 留痕, 即设计意图。
