# FTSeek-US 自动交易流程图

> 双脑架构:**决策脑(DA 深度分析)** 负责选股与出计划,**执行脑(FTSeek)** 负责下单与风控,
> 通过 `playbook_instructions` 指令库交接;**复盘回归** 持续校准参数(先走模拟盘门)。
> 度量统一用 `pnl_pct`;纪律:数据说话、一次一变量、风控硬约束、全程可回滚。
>
> ⚠️ **本文定位**:上方**流程图=结构/逻辑链路的 SSOT(手维护)**;下方
> **「实时参数快照」块由 `scripts/gen_trading_ssot.py` 自动生成**(从真实配置/代码抽取、带出处)。
> 流程图内的数字仅为**示意**,具体数值一律以自动块为准 —— 配置改了跑一次生成器即刷新,不会过期。

```mermaid
flowchart TD
    subgraph DA["🧠 决策脑 · DA 深度分析 (盘前 ET06:00)"]
        A1["① 市场扫描<br/>宽扫全市场多空动量"] --> A2["② 筛选<br/>Top5 + 自选池 watchlist"]
        A2 --> A3["③ 深度分析<br/>基本面+技术面+催化<br/>出交易计划:方向/入场/SL/TP/失效条件"]
        A3 --> A4{"④ 双模型辩论<br/>主模型 vs 挑战者"}
        A4 -->|"一致"| A5["信心提升"]
        A4 -->|"一方SKIP/质疑"| A6["降信心·保留方向"]
        A4 -->|"分歧极大"| AX["✂️ 放弃 SKIP"]
        A5 --> A7{"⑤ 质量闸门 signal_gate<br/>比对历史赚/亏模式"}
        A6 --> A7
        A7 -->|"BLOCK ~48%"| AX
        A7 -->|"PASS / WARN"| A8[("📥 写入指令库<br/>playbook_instructions")]
    end

    subgraph EX["⚙️ 执行脑 · FTSeek (盘中轮询)"]
        A8 -.->|"缺失/空/非法"| A9["#1 bridge三态告警<br/>飞书HIGH(当日去重)<br/>schema非法→fail-safe降级"]
        A8 --> B1{"开盘后≥5分钟?"}
        B1 -->|"否"| BZ["等待<br/>避开开盘噪声"]
        B1 -->|"是"| B2{"环境闸门<br/>VIX极端/风险OFF?"}
        B2 -->|"是"| BZ2["暂停开仓"]
        B2 -->|"否"| B25{"#2 失效门组<br/>≤14:00ET? 非财报禁建日?<br/>漂移≤5%? expires_at未过期(24h)?"}
        B25 -->|"漂移>5%"| BX["✂️ 指令作废<br/>skip_reason=thesis_drift_invalidate<br/>(trigger_miss遥测留shadow)"]
        B25 -->|"其他否决"| BZ3["不追不接飞刀<br/>继续观察"]
        B25 -->|"通过"| B3{"触发确认<br/>突破当日高/低点 + 放量?"}
        B3 -->|"否"| BZ3
        B3 -->|"是"| B4["🔄 实时重定价 reprice<br/>用实时买卖盘重算 入场/止损"]
        B4 --> B5{"资金三护栏<br/>单笔≤4% · 单日敞口≤20% · ≤5笔"}
        B5 -->|"超限"| BZ4["跳过"]
        B5 -->|"通过"| B6{"#RC1 成交确认<br/>order_status=FILLED?"}
        B6 -->|"未成交"| BX2["✂️ 拒记账+释放抢占<br/>消灭GHOST虚假仓(RC1)"]
        B6 -->|"成交"| B7["✅ 下单记账 (模拟盘)"]
    end

    subgraph SLTP["🎯 止损止盈 (按波动率分桶·数据驱动)"]
        B7 --> C1["按 ATR% 分桶 low/med/high/extreme<br/>止损钉死 2%~5% · 止盈=SL×盈亏比<br/>不变式:多 SL&lt;入场&lt;TP / 空 反之"]
        C1 --> C05["#早期紧SL 已降级 shadow-only (修复M)<br/>不再实单收紧, 改 exit_shadow record-only<br/>(early_tight_30m_1pct 反事实采集)"]
        C1 -.->|"conditions零命中"| C2["#6-A factor_exit缺规则WARN<br/>(去重防刷屏, default兖底不变)"]
    end
    
    subgraph EXIT["🚪 持仓管理与退出 (按优先级)"]
        C05 --> D1["保本棘轮:浮盈≥0.5% → SL提到成本"]
        D1 --> D15["trailing锁利: 回撤0.8%才锁<br/>(0.4→0.8: 给赢家呼吸空间)"]
        D15 --> D2{"触发哪条退出?"}
        D2 -->|"触及止损"| E1["硬止损平仓"]
        D2 -->|"超时且仍亏"| E2["时间退出"]
        D2 -->|"盘末"| E3["盈利落袋/亏损交给SL"]
        D2 -->|"收盘"| E4["EOD 强平兖底 (不留隔夜)"]
        E1 -.-> D3{"#熔断shadow告警<br/>当日≥2笔分钟级亏损→飞书HIGH<br/>(不阻断, 2周数据定阈值)"}
        E2 -.-> D3
        E1 -.->|"平仓失败/未成交/卡住"| D4["#3b 平仓失败告警<br/>结构化WARN+飞书HIGH(去重)<br/>正常/SL/EOD三链路卡住点"]
        E2 -.-> D4
        E4 -.-> D4
    end

    subgraph REC["📝 记录 + 🔁 回归复盘"]
        E1 --> F1[("全生命周期入库<br/>pnl_pct/MFE·MAE/退出原因/结果")]
        E2 --> F1
        E3 --> F1
        E4 --> F1
        F1 --> F2["IC追踪 · 前瞻收益T1/T3/T5+intraday<br/>#11 未触发指令open代理回填(fwd_proxy=1)<br/>毕业统计按 config_version 窗口取样 (修复M graduation_sample)<br/>日报/周报 · 影子统一看板"]
        F2 --> F4["影子矩阵 (DEB-round3)<br/>30min-abort反事实 · exit_param变体<br/>ghost_cause归因(RC1/RC2) · gate_cost_report"]
        F4 --> F3["参数校准<br/>止损桶/闸门阈值/分歧规则<br/>(先走模拟盘门·一次一变量)"]
    end

    F3 -.反馈校准.-> A7
    F3 -.反馈校准.-> C1
    F3 -.反馈校准.-> A4

    classDef brain fill:#e3f2fd,stroke:#1976d2,color:#0d47a1;
    classDef exec fill:#e8f5e9,stroke:#388e3c,color:#1b5e20;
    classDef risk fill:#fff3e0,stroke:#f57c00,color:#e65100;
    classDef stop fill:#ffebee,stroke:#c62828,color:#b71c1c;
    classDef review fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c;
    class A1,A2,A3,A4,A5,A6,A7,A8 brain;
    class B1,B2,B25,B3,B4,B5,B6,B7 exec;
    class C1,D1,D2,A9,C2,C05 risk;
    class E1,E2,E3,E4,AX,BX,BX2,BZ,BZ2,BZ3,BZ4,D3,D4 stop;
    class F1,F2,F3,F4 review;
```

## 图例速读

| 颜色 | 环节 | 一句话 |
|---|---|---|
| 🔵 蓝 | 决策脑 DA | 选股 → 出计划 → 双模型辩论 → 质量闸门 → 写指令库 |
| 🟢 绿 | 执行脑 FTSeek | 开盘门 → 环境门 → #2失效门组(14:00截止/财报/漂移5%/24h过期) → 突破触发 → 重定价 → 资金护栏 → #RC1成交确认 → 记账 |
| 🟠 橙 | 止损止盈 | 波动率分桶,止损 2~5%,方向不变式保证; **早期紧SL 已降级 shadow-only**(修复M, 改 exit_shadow 反事实); #6-A 缺规则WARN |
| 🔴 红 | 退出/拦截 | 保本棘轮 + **trailing回撤0.8%锁利** + 硬止损/时间/盘末/收盘强平; 熔断shadow告警(不阻断); **#3b 平仓失败告警**(正常/SL/EOD卡住→飞书HIGH) |
| 🟣 紫 | 记录与回归 | 全程留痕 → IC/复盘/影子矩阵(30min-abort/exit变体/ghost归因/门成本); **毕业统计按 config_version 窗口取样**(修复M) → 校准参数(反馈回闸门/止损/辩论) |

> **闭环**:紫色的"参数校准"用真实 `pnl_pct` 反哺蓝色的质量闸门、辩论规则和橙色的止损桶——
> 系统持续自我校准,但每次只动一个变量、先影子验证、再切实盘。

---

## 实时参数快照

<!-- AUTOGEN:params:start -->
> 🤖 自动生成 · 请勿手改此块 · 生成时间 2026-07-23 19:31 ET · commit `c5d726d2`
> 数值均从真实配置/代码抽取, 带出处; 结构(上方流程图)才是手维护的 SSOT。

#### 执行护栏 (Execution Guardrails)
| 参数 | 值 | 出处 | 说明 |
|---|---|---|---|
| 开盘后缓冲 | 5 分钟 | pipeline.py:2372 | 避开开盘噪声, 之后才执行 |
| max_position_pct | 4% | pipeline.py:2749 代码默认 | 单笔仓位上限(占基准资金) |
| daily_notional_cap_pct | 20% | pipeline.py:2472 代码默认 | 单日总敞口上限 |
| max_daily_positions | 5笔 | pipeline.py:2474 代码默认 | 单日最多开仓数 |
| risk_pct | 0.5% | pipeline.py:2748 代码默认 | 单笔风险预算(占基准资金) |
| min_confidence | 60 | pipeline.py:2769 代码默认 | 信心基准(仓位加权中性点) |

#### 止损止盈波动率桶 (playbook_horizon.yaml::vol_buckets)
| 桶 | ATR%上限 | SL范围 | TP倍数 | 最长持仓 |
|---|---|---|---|---|
| short/low | ATR<0.015 | 2.0~2.5% | 2.0 | Noneh |
| short/medium | ATR<0.035 | 2.5~4.5% | 1.8 | Noneh |
| short/high | ATR<0.08 | 3.5~5.0% | 1.5 | Noneh |
| short/extreme | ATR<999.0 | 3.5~5.0% | 1.5 | Noneh |
| long/low | ATR<0.015 | 1.5~3.0% | 2.5 | Noneh |
| long/medium | ATR<0.035 | 2.5~5.0% | 2.0 | Noneh |
| long/high | ATR<999.0 | 3.5~5.0% | 1.8 | Noneh |

#### 退出规则 factor_exit_rules (playbook_horizon.yaml)
| 因子 | 保本触发 | 时间退出 | trailing |
|---|---|---|---|
| default | 0.5% | 6.0h | on |
| insider_cluster_buy | 0.5% | 禁用 | on |
| macd_golden_cross | 0.5% | 6h | on |
| above_ma20 | 0.5% | 6h | on |
| news_positive | 0.5% | 4h | on |
| volume_expansion | 0.5% | 0.5h | off |
| rsi_oversold | 0.5% | 0.5h | off |
| pe_below_industry | 0.5% | 0.5h | off |
| above_ma50 | 0.5% | 6h | on |
| ma_golden_cross | 0.5% | 6h | on |
| bollinger_upper | 0.5% | 4h | on |
| bollinger_squeeze | 0.5% | 6h | on |
| near_52w_high | 0.5% | 4h | on |
| insider_net_buy | 0.5% | 禁用 | on |
| revenue_growth_high | 0.5% | 禁用 | on |
| roe_high | 0.5% | 禁用 | on |
| earnings_beat_streak | 0.5% | 8h | on |
| post_earnings_momentum | 0.5% | 8h | on |
| sentiment_bullish | 0.5% | 4h | on |
| below_ma20 | 0.5% | 1.0h | off |
| below_ma50 | 0.5% | 1.0h | off |
| ma_dead_cross | 0.5% | 1.0h | off |
| macd_dead_cross | 0.5% | 1.0h | off |
| bollinger_lower | 0.5% | 0.5h | off |
| near_52w_low | 0.5% | 0.5h | off |
| rsi_overbought | 0.5% | 0.5h | off |
| rsi_neutral_low | 0.5% | 1.0h | off |
| news_negative | 0.5% | 0.5h | off |
| sentiment_bearish | 0.5% | 0.5h | off |
| insider_net_sell | 0.5% | 1.0h | off |
| pe_above_industry | 0.5% | 1.0h | off |
| volume_contraction | 0.5% | 1.0h | off |
| earnings_approaching | 0.5% | 1.0h | off |

#### 调度与指令来源状态 (cron / hermes)
| 作业 | 计划(cron/机器时钟=北京) | 状态 | 出处 |
|---|---|---|---|
| deep-stock-analysis 每日盘前扫描+分析 | 0 18 * * 1-5 | ✅启用 (scheduled) | jobs.json |
| deep-analysis 盘后复盘+记忆更新 | 0 5 * * 2-6 | ✅启用 (scheduled) | jobs.json |
| deep-analysis-bridge-generation | 0 4 * * 2-6 | ⏸暂停 (paused) | jobs.json |
| da_chain_health | 5 6 * * 2-6 | crontab | crontab -l |
| da_bridge_dingtalk | 30 20 * * 1-5 | crontab | crontab -l |
| divergence_arbitration | 8 6 * * 2-6 | crontab | crontab -l |
| shadow_board | 15 6 * * 2-6 | crontab | crontab -l |

#### 总开关与时间框架 (unified_config.yaml / playbook_horizon.yaml)
| 参数 | 值 | 出处 | 说明 |
|---|---|---|---|
| auto_execute | False | unified_config.yaml::playbook | 自动执行总开关 (2026-09-05 §11.3 止损路径关闭, DEB-20260815 §42.4) |
| fallback_when_no_bridge | technical | unified_config.yaml::playbook | 无bridge时兜底策略 |
| short_horizon.rr_ratio | 999 (999=禁TP靠EOD) | playbook_horizon.yaml | 盈亏比 |
| short_horizon.max_hold_days | 1 | playbook_horizon.yaml | 最长持仓天数 |
| long_horizon.rr_ratio | 2.0 | playbook_horizon.yaml | 盈亏比 |
| long_horizon.max_hold_days | 3 | playbook_horizon.yaml | 最长持仓天数 |

<!-- AUTOGEN:params:end -->
