# DEB-20261114 — 高频模拟流 edge 发现：EXT 盘前催化剂入场是唯一稳健正期望子集

- 状态：**发现已落档（只读 screener）；探索性，前瞻确认前不称「可用」、不改交易行为**
- 用户请求：「目前是模拟交易，同意聚焦，不加无意义的新门，但是希望通过能够在每日高频交易找到真正的交易可用的策略和逻辑」（2026-11-14）
- 纪律：切片=假设生成非证明；反过拟合稳健门（n≥30 ∧ 日聚类 t≥3.0 ∧ 留一日 t 全>0 ∧ 前后半同号 ∧ top3<70% ∧ ≥2 月同号）；多重检验明示；转正须前瞻预注册。**零交易行为变更、零新影子、零重启。**

## 1. 方法（可复现）

`scripts/edge_discovery_screener.py`（只读 mode=ro）把每日高频模拟成交按 **8 维事前可识别特征**（owner_strategy/entry_session/market_regime/direction/session×source/EXT research_source/EXT entry_grade/EXT gate_mode）切片，**共测 23 切片**，逐片算 n/净bps/胜率/日聚类 t/80%CI/MDE80 + 留一日 t/前后半 t/top3 集中度/逐月符号。

## 2. 结果：23 切片仅 1 个过稳健门

| 切片 | n | 净bps | 日聚类t | 留一日min t | top3 | 判定 |
|---|---:|---:|---:|---:|---:|---|
| **entry_session=premarket（=EXT 盘前）** | 36 | **+32.50** | **3.44** | **3.08** | **45%** | **★ROBUST** |
| EXT research_beat | 18 | +32.58 | 2.72 | 2.07 | 65% | 探索（n<30、t<3） |
| direction=SHORT | 68 | +11.63 | 1.32 | 0.86 | 127% | 探索（集中） |
| EXT 整体 | 64 | +21.10 | 1.06 | 0.51 | 77% | 探索（集中） |
| EXT afterhours | 28 | +7.07 | 0.36 | −0.46 | 489% | 探索（NVDA 尾部主导） |
| DA regular | 200 | −11.93 | −0.69 | −0.95 | — | 负期望 |
| regime=cautious | 60 | −27.95 | −1.15 | −1.5 | — | 负期望 |

- **唯一稳健 edge = EXT 盘前催化剂入场**（premarket 与 premarket|EXT 是同一信号的两视角）。t=3.44 越 Harvey-Liu-Zhu t>3.0 多重检验门槛；留一日 t 全 3.08–3.85（非单日运气）；前后半 t=2.30/2.51 同号；逐月 10月+31.16 / 11月+34.16 同号；top3=45%（非 C9/C15 那种 93% 单点主导）。
- **关键精炼**：edge 在「EXT **盘前**」，**不是 EXT 整体**（top3=77% 集中）也**不是 EXT 盘后**（top3=489%，被 NVDA −1408bps 跳空主导）。"无脑做 EXT" 不稳健。

## 3. 交易逻辑（为什么它赚，可口述）

EXT 盘前入场 = **催化剂驱动（research_beat 财报超预期 / research_watchlist）+ RVOL 量能确认（volume_ratio≥阈值）+ VWAP 贴近（vwap_dev 小）+ A/B 级 + 保本/锁盈管理**。
- 出场结构健康：36 笔里 SL_BREAKEVEN 17 + SL_TRAIL_LOCK 11 = 78% 保本或锁盈出，**SL_INITIAL 仅 1（死单率 2.8%）**。
- 对照 DA regular：死单率 20.2%、本周 6 笔 SL_INITIAL（开盘追进 RANGE 噪声）⇒ 负期望。
- 这正是实务「stocks in play / 盘前 RVOL+缺口验证」打法（DEB-20261114-weekly-pnl-diagnosis §2 它山之石），**在本仓高频流里被数据证实为唯一稳健正期望**。
- 赢家例：PEP（watchlist，volume_ratio 100×，vwap_dev −0.03%）+106.6；APP（beat）+69.6。

## 4. 前瞻确认预注册（探索→可用的唯一路径；窗内禁改）

- cohort 起点 **2026-11-16**（前瞻，不复用 10-06→11-13 探索窗）。
- 人群：`owner_strategy=extended_hours_entry ∧ entry_session=premarket` ∧ A9 干净。
- 确认门：前瞻 **n≥30 笔 ∧ ≥15 交易日**；日聚类 **t≥3.0**；留一日 t 全>0；top3<70%；≥2 月同号。
- 跟踪工具：`edge_discovery_screener.py --start 2026-11-16` 每周复跑（系统已在产 EXT 盘前单，无需新影子/新写方）。
- 达标 → 提请「聚焦 EXT 盘前」战略 Pivot（**另立 DEB + 用户裁**）；不达标 → 记探索性，不转正。

## 5. 战略含义（待用户裁；本批不动作）

- 数据指向：**正期望在 EXT 盘前；DA regular 主通道负期望（−11.93bps）**。与 OKR 主 KR「DA-only 转正」存在张力。
- 三条合规路径（OKR §6 Pivot）：① **聚焦 EXT 盘前**（换策略类别/聚焦 edge 源，须新预注册+用户裁，本 DEB §4 即其确认面）；② **降频救 DA**（RFC-209，探索 t<2 待前瞻）；③ 承认 DA 无 edge 转研究。
- **不据本探索窗放大/转正**（OKR §7「不据 EXT 小样本放大」仍守）；n=36/16 日、策略 10 月才起、23 切片选 1 ⇒ 仍须前瞻确认。

## 6. 诚实边界

- 探索性（in-sample 10–11 月）；n=36/16 日样本薄；EXT 盘前策略 10 月才上线（无更早历史）；多重检验 23 选 1（t>3.0 已校正但仍需前瞻）。
- **不承诺收益、不宣称 live readiness、不宣称已证 edge**；「可用」须 §4 前瞻确认 + 用户裁。
- 复现：`PYTHONPATH=. ./venv/bin/python scripts/edge_discovery_screener.py --start 2026-10-06`（前瞻确认改 `--start 2026-11-16`）。
- 测试：`tests/test_edge_discovery_screener.py` 3 passed（稳健门/集中度否决/小样本否决）。
