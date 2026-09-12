# RFC-082: VWAP 斜率证伪门 DA enforce 转正 (用户知情破例)

- **日期**: 2026-09-12 (周六, 休市窗口)
- **裁决**: 用户裁「目前阶段已经过了需要大量收集数据的时候，该出手出手，该拦损失的就拦截，以盈利和降低亏损为目标，请基于此目标执行」
- **类型**: L4 配置变更 (config 一键) + L5 (注册表/周检口径留痕)
- **风险**: 🟡 中 (DA 源入场拦截面扩大; sim 环境; 逐轮拦截可复触发, 非永久否决)

## 1. 改什么

| 项 | 旧 | 新 |
|---|---|---|
| `playbook.execution_parameters.falsification_gate_vwap_slope_da_enforce` | `false` (DA 只记不拦=对比组) | `true` (DA 生产拦截, 全源统一) |
| shadow_registry `p1_1_vwap_slope` | OBSERVING | GRADUATED (verdict=GRADUATED_BY_USER_OVERRIDE, 留痕证据) |
| `scripts/falsification_gate_weekly_review.py` | DA 行混入真被拦/延迟成交桶 | 按 `_DA_ENFORCE_SINCE=2026-09-12` 分界: 此前 DA 行独立桶 `n_da_would_block_pre_enforce`, 此后 DA 行与 reentry+UE 同口径 |
| `ops/hermes/ftseek_weekly_checkpoint.py` | 渲染两段式 | 渲染加 DA 对比组段 (仅历史桶非零时显示) |

## 2. 为什么改 (证据)

近4周 (8/17-9/11) 实弹体检:

- **最大出血点 = SL_INITIAL "入场即逆"**: 18 笔 **-$3,069**, 其中 15 笔 mfe<0.2% (入场后从未浮盈) -$2,370。源分布: DA 12 笔 -$2,298 / UE 5 笔 -$707 / reentry 1 笔 -$65。集中在 8/18 (MU/FIX/CAT)、8/26 (UBER)、8/28 (AFRM/RBRK) —— 全部"盘前大涨追 BUY"模式。
- **DA 对比组反事实 (本门核心证据)**: 8/27-9/11 遥测 13 行 DA would-block, 12 行 join 上真实成交, 合计 **-$735** —— 若当时拦下即省 $735。其中 **8/28 单日 5 行全中, 省 $839** (AFRM -$308 / RBRK -$159 / GAP -$387, 正是当日 SL_INITIAL+隔夜缺口出血主力)。
- **强制源已有实拦样本方向一致**: 真被拦 2 笔 (BWXT 多单次日口径拦对 / GEV 空单当日拦对, 若成交 -$253)。
- **E2 动量回放 (门前置证据)**: 入场动量确认反事实 blocked n=90 净 -$1,026 WR 33% vs passed n=46 净 +$184 WR 52%。

与 DEB-20260911 方向一致: 该辩论裁定"追高/追空职责交给现役门"(退役 A/C 影子规则), 本门正是现役门之一。

## 3. 知情破例披露 (预注册判据未达标)

8/23 预注册判据: **reentry+UE 组 n≥30 + 日聚类 t≤-2 + DA 对比组方向一致** → 翻 da_enforce=true。

实际: 真被拦 n=2 (<30), t=-1.303 (未达-2), 仅"DA 对比组方向一致"一条腿成立。按 ~0.9 笔真拦/周速度攒到 n=30 需 ~7 个月。

**用户知情裁决跳过攒样直接转正** — 本 RFC 即为该破例的书面留痕 (对齐 8/29 vwap_gate_ab KILLED 的"用户知情推翻预注册"先例格式)。周检脚本判据不放宽: n<30 仍输出"继续攒样", 转正后 DA 拦截行开始进真被拦桶, 攒样速度将显著加快 (DA 是最大源), 门有效性仍可事后被证伪。

## 4. 已知局限 (诚实披露)

1. **门形态偏"延迟器"**: 拦截逐轮生效, 逆行斜率消失后下一轮可复触发成交。强制源实测 3/5 拦截单后来照样成交。期望省额 = $735 × (真拦占比), 打折。
2. **DA 对比组负均值靠尾部**: 12 笔中 7 笔赢家, 负值由 8/28 三笔大亏驱动 — 单日聚类不能外推为全天候正期望。
3. **反事实口径**: 对比组按真实退出链计 (真实净额), 与周检"收盘反事实"口径不同; 两口径方向一致故可互证。

## 5. 不改什么 (评估后放弃)

| 候选 | 放弃理由 |
|---|---|
| min_notional 2500→3000 | RFC-071 (9/1) 刚降且有实证 ([2500,3000) 近期 4 笔净 -$2.5 近平衡); 近4周 <$3K 仅 8 笔, 其中亏损主力 BABA/RBRK 是 SL_INITIAL 模式, 本门已覆盖 |
| 盘前失效 C_long_chase 转硬门 | DEB-20260911 (9/11) 刚裁决退役 C 采集、职责交现役门 — 与本 RFC 同向, 不再另立门 |
| insider_divergence 门关闭 | gate_cf 76.5% 命中率是 T+5 描述口径, 系统实际日内退出, 按预注册边界不构成启停判据 |
| DA LONG 拉长持有期 | t5_hold 影子 Δ-$22.58/笔明确不支持; 方向尺子 T+20 读数需预注册辩论, 另案 |

## 6. 回滚

`falsification_gate_vwap_slope_da_enforce: false` + `systemctl restart ftseek-us`; shadow_registry 条目 revert OBSERVING。周检脚本分桶改动向后兼容 (9/12 前行为与旧版逐字一致: DA 行此前全部 executed=True 进延迟成交桶 → 现进独立桶, 数字展示变化但判定口径 true_blocked 不变)。

## 7. 验证清单 (Gate 5/6)

- [x] py_compile: shadow_registry.py / falsification_gate_weekly_review.py
- [x] YAML 解析 + 键值读取 (get_playbook_config)
- [x] 周检脚本实跑: 报告 JSON 含 n_da_would_block_pre_enforce=13, 真被拦仍=2
- [x] 守卫测试: test_shadow_intent_uniqueness / test_vwap_gate_ab / test_phase1_signal_quality / test_ops_scripts_sync
- [x] systemctl restart ftseek-us (周六休市, 持仓 FCX/UBER 由 reconciler 恢复, 不受影响)
- [x] 重启后日志确认配置加载 + 无 ERROR
- [x] ops/hermes ↔ /root/.hermes/scripts 同步 (顺带修复 9/11 已提交未部署的 intent 哨兵漂移)
