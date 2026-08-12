# DEB-20260812 (二): 富途未利用数据通道第二轮 — 期权大单流 / 分钟资金流 / 空头数据 / 10.9 SDK 升级

| 字段 | 值 |
|---|---|
| 状态 | 立案待辩 (record-only 提案, 零行为变更) |
| 创建 | 2026-08-12 (用户令: 盘点富途现有接口/快讯/数据通道是否充分利用, 调研最新理论与研究项目, 优化补充) |
| 前案 | DEB-20260805-futu-unused-capabilities (主张5 期权链/IV 已被显式否决: "防反复立项, 冻结至可证伪事件驱动假说") |
| 同窗 | DEB-20260812-blackout-1015-oos (RFC-064) 8/24 裁决窗; DEB UE陈旧证据衰减 streak 硬闸同窗清单 |
| 纪律 | 变更预算两周一变量: 本 DEB 全部主张为采集/影子, 不占行为变更额度; 任何转正走 OOS 双窗口 ≥40% 纪律 |

---

## 一、背景与问题陈述 (2026-08-12 实机盘点)

### 1.1 环境实测 (主控亲测, 非纸面)

| 项 | 实测值 |
|---|---|
| OpenD | 10.7.6728 (/opt/ftseek/Futu_OpenD_10.7.6728_Centos7, 运行中) |
| futu-api SDK | 10.03.6308 (venv python3.11) |
| 行情权限 | us_qot_right=LV3 (Nasdaq Basic+TotalView 确认), 港股 LV1 |
| 订阅额度 | sub_quota=1000 席, 盘前实测 total_used≈0, 扩容无瓶颈 |
| 快讯通道 | 富途 OpenAPI **无 24/7 快讯推送接口** (moomoo 快讯系客户端功能); 现役快讯链=Finnhub company-news + RSS (news_v2, observe 模式, breaking_news trigger_immediate_scan 冷却 300s) |

### 1.2 通道使用盘点 (全仓 grep 消费者计数)

已充分使用: 交易链 (下单/改单/撤单/持仓对账)、get_market_snapshot(37处)、QUOTE/K_1M 推送、request_history_kline(35处)、get_market_state、ORDER_BOOK+TICKER (8/10 活跃集启用, OFI 攒样中)。

零消费者但实机验证可用 (本 DEB 核心):

| 通道 | grep 计数 | 实测 (10:15-10:20 CST) |
|---|---|---|
| get_capital_flow | 0 | ★可用: NVDA 返回 391 行分钟级资金流 (超大单/大单/中单/小单净流入) |
| get_derivative_unusual | 0 | ★可用: 期权大单异动文本流 (样本见 §二) |
| get_technical_unusual | 0 | ★可用: 技术面异动 (NVDA 近7日 4 条) |
| get_financial_unusual | 0 | ★可用: 资金面异动 (NVDA 返回"无异常", 接口通) |
| get_short_interest | 0 | ★可用: 空头持仓 (NVDA 7/31: 空头占比 1.209%, 回补 2.30 天) |
| get_daily_short_volume | 0 | ★可用: 每日卖空 (NVDA 8/10: 卖空占比 7.459%, 量 1.16 亿股) |
| get_capital_distribution | 1 (仅 dashboard 展示) | 未入任何因子/门 |
| get_plate_list / get_plate_stock | 0 | 美股板块/成分股全支持 (板块轮动目前只靠 LLM 叙事, 无结构化广度数据) |
| get_stock_filter / get_stock_screen(V2) | 0 | 选股器未用, 候选供给全靠现有扫描池 |

10.9.6908 SDK 新增但当前 OpenD 10.7 **协议不通** (实测返回"未知的协议ID"): get_search_news、get_economic_calendar、get_earnings_calendar、get_hot_list、get_top_movers_rank、get_rise_fall_distribution、get_us_pre_market_rank / get_us_overnight_rank / get_us_after_hours_rank 等约 120 个新接口。需 OpenD+SDK 同步升 10.9 才可用 (§三 主张 C)。

---

## 二、证据包 (实测样本)

### 2.1 get_derivative_unusual("US.NVDA") — 2026-08-12 10:18 CST 实取

```
期权大单异动 (近7个自然日):
8.12 02:41 买入看涨期权 5004张, V/OI=0,   金额 $2,501,946, 行权价 217.5, 到期 2026/08/24
8.12 02:31 卖出看跌期权 3549张, V/OI=2.9, 金额 $1,685,833, 行权价 220,   到期 2026/08/14
8.12 01:23 买入看涨期权 7428张, V/OI=1.9, 金额 $3,714,000, 行权价 217.5, 到期 2026/08/21
8.12 01:05 买入看涨期权 6026张, V/OI=0.2, 金额 $2,350,093, 行权价 220,   到期 2026/08/21
8.11 21:56 卖出看涨期权 4714张 ... (续)
```

注: 时间戳为北京时间, 对应美股盘中/盘后。返回结构含 err_code / retMsg / time_range / content 四字段, 为文本非结构化 — 需解析层。

### 2.2 get_short_interest / get_daily_short_volume ("US.NVDA") — 实取

```
short_interest: 2026-07-31 shares_short=292,667,375, short_percent=1.209, days_to_cover=2.30
                2026-07-15 shares_short=324,052,767, short_percent=1.339, days_to_cover=2.47
daily_short_volume: 2026-08-10 short_percent=7.459, volume=115,846,596
                    2026-08-07 short_percent=10.864
```

### 2.3 理论依据 (研究 → 系统映射)

| # | 研究 | 结论 | 映射到本系统 |
|---|---|---|---|
| R1 | Cremers & Weinbaum (2010, JFQA); Johnson & So (2012, JF); Augustin et al. (2019) | 知情资金先进期权后进正股, 期权成交量比 (OSV) 预测次日及日内方向 | 主张 A: derivative_unusual 是 OSV 类信号的现成数据源 |
| R2 | Cont, Kukanov & Stoikov (2014, JFEconometrics) | 订单流不平衡 OFI 对短期价格变动线性解释力, 增量定义 (Δbid量−Δask量) 更稳 | 8/10 已启用 TICKER/OFI 攒样; 8/24 裁决后若转正改用 Cont 式增量定义 |
| R3 | Gao, Han, Li & Zhou (2018, JFE) "Market Intraday Momentum" | 开盘前 30 分钟收益显著预测后半天方向 | 印证 8/11 自研发现 (方向×当日涨幅一致性); 提示 10:15 后顺早盘方向再入场有正期望 |
| R4 | Lou, Polk & Skouras (2015); Asness "Best Strategy in the World" | 美股日内收益长期为负/隔夜为正 | 支持 EOD 退出结构 (86% WR), 属"继续做别改"证据 |
| R5 | Tetlock (2007); Tetlock et al. (2008) | 新闻漂移集中在发布后 5-30 分钟 | 快讯链瓶颈评估 (主张 D): 现 trigger 冷却 300s, 未吃漂移窗口; 需先验证新闻时间戳精度 |
| R6 | López de Prado《AFML》Ch.3 meta-labeling | 二级模型只答"这单该不该下/下多大", 提升 WR 不改信号生成 | 与 DA 挑战者同构但非概率校准式; 中期研究方向 (样本过门槛后) |
| R7 | Baltussen 等 (2022 后 0DTE 文献) | 0DTE 占 SPX 期权量 ~50%, dealer gamma 流影响日内指数节奏 | VIX regime 目前日线粒度; 0DTE 数据需 SDK 10.9, 升级后调研 |
| R8 | AFML Ch.2 成交量时钟采样 | 按成交量采样降噪 | 20s 轮询+模拟对齐纪律下改造成本高, 长期方向 |
| R9 | 市场广度文献 (rise/fall distribution 与日内反转) | 广度恶化是日内反转前兆 | market_regime_detector 现仅 VIX 轴, 加广度轴需 SDK 10.9 (§三 C) |

R1/R2/R3 为强证据通道 (多篇顶刊, 与现有攒样方向一致); R5-R9 为候选方向, 不构成立项依据。

---

## 三、主张拆解

### 主张 A: get_derivative_unusual 期权大单流 → record-only 攒样 + DA 上下文注入

- 内容: 盘前/盘中对当日可执行标的池拉取期权大单异动, 解析落库 (时间戳/方向/张数/金额/行权价/到期日), 作为 DA prompt 上下文 + 证据项。**不触发任何交易行为**。
- 依据: R1; §2.1 实证数据质量 (含 V/OI、金额, 可构造知情流强度代理)。
- 与前案关系: DEB-20260805 主张5 否决的是"期权链/IV 信号"(IV 异常/期权数据做因子), 冻结条件为"至可证伪事件驱动假说"。本主张是**该假说的具体化**: 事件=期权大单异动, 假说=大单流领先正股日内方向, 可证伪判据见 §五。非反复立项, 是解冻条件的满足。
- 成本: ~1 次/标的/日 拉取, 文本解析层 ~100 行, 零订阅成本。

### 主张 B: get_capital_flow 分钟资金流 → record-only 攒样

- 内容: 活跃集标的盘中按节流周期 (建议 5min 对齐现有遥测粒度, 勿犯 8/10 遥测重复行教训 — 先查上游真实频率再定去重) 拉取分钟级大小单净流入, 落库供 IC 分析。
- 依据: R2 同源逻辑 (资金流不平衡), 391 行/日数据密度足够。
- 风险: 富途大小单划分口径不透明 (待查文档/实测边界); 与 OFI 共线可能 — 攒样期必须跑增量 IC (在 OFI 基线上), 禁裸边际 IC (DEB-20260805 4.2 方法论先例)。

### 主张 C: OpenD + SDK 升级至 10.9.6908 (单独排期, 不并入本窗)

- 内容: 升级解锁 get_search_news / get_economic_calendar / get_earnings_calendar (原生财报日历, 替代 Finnhub 依赖) / 盘前·隔夜·盘后榜单 (候选供给扩池) / rise_fall_distribution (广度) / 0DTE 系列。
- 现状缺口: premarket_movers 只覆盖 watchlist 内 snapshot, 池外黑马无供给; 财报日历/快讯链均依赖 Finnhub 单点。
- 风险: 升级窗口需避开交易时段; SIM 环境低风险但须回归 1126 测试 + 盘后重启验收; 10.03→10.9 接口签名有变化 (3/4 元组返回), 现有代码不受影响 (不主动迁移旧接口)。

### 主张 D: 快讯链评估 (Finnhub 漂移窗口) — 仅调研, 不立项

- 内容: 验证 news_v2 新闻时间戳精度 (R5 漂移窗口 5-30min 是否可辨识); 富途 get_search_news 升级后是否可作补充源。
- 现状: 富途无 24/7 快讯推送接口 (实测确认), 快讯瓶颈不在富途侧。

### 显式不做 (防范围蔓延)

- get_broker_queue (仅港股, 美股不适用)
- get_rt_data 分时 (K_1M 已覆盖)
- F10 重仓进盘前决策 (对日内 WR 无直接贡献; DA 深度分析已用 Finnhub 链)
- 成交量时钟采样 / meta-labeling (R6/R8 记为研究方向, 等样本量)

---

## 四、辩论点 (预置反方攻击面)

1. **前案既判力**: 主张 A 是否构成对 DEB-20260805 主张5 否决的规避? 解冻条件"可证伪事件驱动假说"是否真的满足 (判据 §五 是否够硬)?
2. **变更预算**: 8/24 窗已有 OFI 转正 + streak 硬闸两项待裁, 再加 A/B 是否超出同期裁决容量 (FWER 交互: A/B 与 OFI 同属资金流证据链, 裁决时必须控制基线)?
3. **文本流脆弱性**: derivative_unusual 返回中文文本非结构化, 解析层是长期维护债; 富途改文案即断流 — 是否需要降级守卫 (DEB-20260805 E1 教训: 前提必须实查)?
4. **口径不透明**: 富途大小单/超大单划分阈值未公开, capital_flow 特征的可复现性弱于自建 OFI — 是否只配做 OFI 的对照臂而非独立特征?
5. **样本量**: A/B 攒样到 n≥30 (用户数据量优先门槛) 需几个交易日? 标的池多大? 若只覆盖活跃集 (~10 标的), 周期可能超 8/24 窗 — 是否顺延至 9/2 (与 RFC-064 样本不足延判同规则)?
6. **成本**: 每分钟级拉取是否触 OpenD 查询限流 (30 次/30s 类限制)? 需实测节流阈值后再定频率。
7. **R3 的再入场含义**: 若早盘动量预测后半日方向成立, 与 10:15 禁触窗 (RFC-064) 交互如何 — 禁触窗裁决出结果前, 是否冻结一切再入场侧提案?

## 五、观察点与预注册判据

### A/B 共同规则 (record-only 期)
- 采集期: 批准次日起, 全程零交易行为变更; 落库带唯一键 (symbol, ts, channel) 防轮询放大 (8/10 教训)。
- 数据质量门先行: 非空率 ≥95% 方可进攒样 (DEB-20260805 4.2 先例)。
- 毕业检验: 增量 rank-IC (在现有因子+OFI 基线上), 双窗口 OOS ≥40% 或 ρ≥0.15 显著; 禁裸边际 IC、禁"门内 vs 门外 pnl"选样恒等式。

### 裁决时点
- 与 8/24 同窗 (OFI/streak/毛利记分卡): 若 A/B 采集在 8/24 前满 n≥30; 否则顺延 9/2, 与 RFC-064 延判同规则。
- 裁决三选项: 转正 (进 DA 证据门, 仍不直接触发交易) / 续采 / 关门 (记录否决理由, 防反复立项)。

### 8/24 同窗裁决·prompt 对称处置清单 (2026-08-12 复查登记)

背景: DA skill 侧 prompt 已存在异动/资金流注入 (llm_synth.py:231 衍生品异动段 +
collect_all 资金流快照), 先于验证进决策 — 裁决动作必须采集侧与 prompt 侧对称执行,
禁只关采集忘关 prompt:

- [ ] **若 A (deriv) 否决/关门** → 同日下线 DA prompt 衍生品异动段
      (deep-stock-analysis skill llm_synth.py:231) + 关 shadow_collect 采集 +
      DEB 记录否决理由 (防反复立项)。
- [ ] **若 B (capital_flow) 否决/关门** → 同日下线 DA prompt 资金流快照注入段
      + 关采集。A/B 分开裁决, 不捆绑 (B 口径不透明风险更高)。
- [ ] **若转正** → prompt 注入保留并标注"已过增量 IC 证据门"; 采集续跑。
- [ ] **若续采** → prompt 段维持现状至下一裁决窗 (9/2), 届时必须出裁决, 不得无限续。

同清单一并处置的 prompt 侧残留 (裁决日顺手, 非独立变更):
- [ ] DA prompt 异动文本北京时间戳加 CST 标注 (防 LLM 误读为 ET 时序)。
- [ ] skill 侧注入按行去重 (V/OI 抖动变体 ~1.4% 噪声, repo 落库侧已修)。

### 主张 C 前置
- 升级单独排期, 需用户批准变更窗口; 升级后第一周仅验证协议连通性清单, 不接任何消费方。

---

## 六、执行记录

### 6.1 主张 C 已执行 (2026-08-12, 用户令"评估无问题可以做升级")

| 项 | 升级前 | 升级后 |
|---|---|---|
| OpenD | 10.7.6728 (/opt/ftseek/Futu_OpenD_10.7.6728_Centos7) | **10.9.6918** (/opt/ftseek/Futu_OpenD_10.9.6918_Centos7/Futu_OpenD_10.9.6918_Centos7) |
| futu-api SDK | 10.3.6308 | **10.9.6908** |
| protobuf | 3.20.3 | 3.20.3 (未动, 已实测 10.9 SDK 兼容) |
| us_qot_right | LV3 | LV3 (不变) |

**兼容性评估结论 (执行前)**: 低难度。
1. SDK 签名 diff: 代码使用面 (quote 15 方法 + trade 全量) 零破坏性变更, 全部为纯增量 (新增可选参数); `request_history_kline` 返回 3 元组为旧版既有行为, 全仓 35 处调用点均已按 3 元组解包, 无适配工作量。
2. 依赖: futu-api 10.9 声明 protobuf>=3.20.0, venv 现版 3.20.3 满足且经隔离加载实测通过; venv 内无其他 protobuf 反向依赖。
3. OpenD: systemd 无头 CLI 模式 (XML 自动登录, 无 X 显示), 迁移=新目录+复制 FutuOpenD.xml+改 unit 路径, 登录凭证保留。
4. 测试基线: 升级前后 1126 passed × 2 轮。

**执行中发现的关键陷阱 (已修)**: `start.sh` 每次启动执行 `pip install -r requirements.lock`, lock 钉死 futu_api==10.3.6308 → SDK 升级在服务重启时被静默回滚。**必须同步更新 requirements.lock** (已改 10.9.6908), 否则升级无效。

**验收 (实机)**:
- 服务: ftseek-us + futu-opend 均 active, 重启后 3 分钟零 ERROR。
- 新协议连通 (升级目标, 全部 [OK]): get_search_news / get_economic_calendar (50行) / get_earnings_calendar (529行) / get_hot_list / get_top_movers_rank / get_us_pre_market_rank / get_rise_fall_distribution / get_period_change_rank。
- 回归 [OK]: get_market_snapshot / get_capital_flow (391行) / get_derivative_unusual / get_short_interest。
- 备份: /opt/ftseek/backups/opend-upgrade-20260812/ (opend_10.9.tar.gz + 旧SDK site-packages tar + 新旧 unit 文件 + VERSION_STAMP)。

**回滚路径**: systemd unit WorkingDirectory/ExecStart 改回旧目录 (futu-opend.service.OLD.bak) + requirements.lock 改回 10.3.6308 + 重启两服务。

### 6.2 主张 A/B 已落地 (2026-08-12, 用户令"开工", record-only)

**立项前纠偏 (关键)**: 初次盘点 grep 只扫 app/ 目录, 漏了 DA skill 生产入口
(/root/.hermes/skills/deep-stock-analysis/scripts/)。实测发现 skill 侧
`collect_all` 已采集 capital_flow + deriv_anomaly 且 llm_synth.py:231 已把
"衍生品异动"格式化进 DA prompt — **主张 A 的"注入 DA 上下文"部分已存在,
真缺口 = 无结构化落库, 无法与成交结局 join 验证领先假说**。按审计教训#16
(跨 Agent 落地检查: 先确认生产入口指向哪个目录) 修正实施范围。

**落地内容** (commit 见 git log, 挂载=stall 影子同款隔离模式):
| 项 | 落点 |
|---|---|
| 采集器 | scripts/futu_channel_shadow_collect.py (capital_flow 按 (symbol,item_time) 增量合并防轮询放大 + derivative_unusual 按事件哈希去重; 动作三态 买入/卖出/中性) |
| TRADING 挂载 | scheduler_core.py 盘末休眠块前, 节流 300s |
| AFTER_HOURS 挂载 | scheduler_core.py 盘后审计后, 每日 1 次幂等 (盘后序列才完整) |
| 配置键 | unified_config.yaml 顶层 futu_channels 节 (enabled/interval/symbols_max) |

**实测验收 (手动端到端, 5 标的)**:
- capital_flow: 1955 行落库, 非空率 100% (≥95% 质量门通过)
- deriv_unusual: 354 事件落库, 非空率 100%; 分布 买入168/卖出165/中性21
- 去重验证: 二次跑增量 0 (flow/deriv 均无重复)
- 解析健壮性: 实测发现富途 content 混合多段 (事件行+[timestamp:]标记+IV评述),
  正则覆盖三态动作后零误报; 真事件行解析失败才告警 (文案变更哨兵)
- 测试: 1126 passed; 重启后零 ERROR

**时区口径声明 (裁决时必须)**: capital_flow item_time = 美东市场时间原样存储;
derivative 事件 ts = OpenD 服务器本地时区 (CST) 原样存储, join 成交结局前
必须统一换算 ET。

**裁决路径 (预注册维持)**: 采集至 8/24 (n 不足则顺延 9/2), 毕业检验=增量
rank-IC (capital_flow 在 OFI 基线上; deriv 大单流领先假说按 §五 判据),
禁裸边际 IC / 禁选样恒等式。

**§6.2 补记 (2026-08-12 独立复查后修复, record-only 层不变更行为)**:
- 病灶: deriv 去重键对整行原文取 MD5, 但富途跨次渲染同一事件时 V/OI 有
  舍入抖动 (实锤 SPY 8.10 23:58 同事件 v_oi 0.3/0.2 两行并存) → 逻辑重复
  5 组/354. §6.2 "二次跑增量 0" 验证只覆盖文本完全一致的回放, 漏此变体。
- 修复: 去重改稳定键 `symbol|ts|action|opt|vol|strike|expiry` (剔除
  OI/V-OI/amount); 存量行结构化字段齐全, seen 集重算兼容, 旧事件零重放。
- 存量清洗: 354→350 (去 4 行真重复; 原第 5 组系同分同量反向两笔, 属合法
  独立事件, 已保留)。备份 data/archive/futu_deriv_unusual.jsonl.bak_20260812_dedup。
- 验收: 实机重跑 SPY/NVDA/GOOG 零重放零新增, 文件零哈希重复。
- 复查撤回项: 曾报"采集器日志不入 ftseek_us.log" — 实系部署后尚未经过
  TRADING/AFTER_HOURS 窗口 (部署于 OVERNIGHT 时段), root 传播链正常,
  非缺陷, 不作改动。

### 6.3 消费方接线落地 (2026-08-12, 用户定性"修尺子, 不改行为")

**用户裁决**: 盘前榜单/财报日历接线 = 修尺子 + 供给侧修正, 非行为变更 —
现有 admission / signal_gate / 风控门禁一律不变, 直接落地不等 8/24 裁决窗
(增加供给帮助 RFC-064 攒样)。

**① 财报日历冗余源 (repo 侧, 零行为变更)**:
- 落点: app/data/earnings_calendar.py `_fetch_futu_calendar_fallback()`
- 逻辑: AV 抓取失败/空结果时 fallback 富途原生 get_earnings_calendar
  (OpenD 本地, 无外部配额); 输出契约与 AV CSV 解析结果逐字段对齐
  (symbol/reportDate/timeOfTheDay/estimate/currency), 下游
  get_watchlist_earnings / earnings_risk_guard 零改动
- 实测: 模拟 AV 失败 → 富途源 603 条 (8/12~8/18), pub_type 三态映射
  BEFORE→pre-market / AFTER→post-market / REGULAR→"";
  get_watchlist_earnings 正常吃到 US.BIDU 8/18 (days_until=5)
- API 约束: 单次窗口 ≤7 天含端点 — 恰好覆盖全部消费方 (days_ahead=7)
- 失败语义与 AV 一致: 返回 [] → 下游降级运行, 不阻断盘前流程

**② 盘前榜单扩池 (skill 侧, 只扩供给)**:
- 落点: futuapi skill 新脚本 quote/get_us_premarket_rank.py
  (双向榜 ascend/descend, count≤200, JSON 契约与 get_stock_filter 一致)
  + data_collector.py `collect_premarket_rank_codes()` 并入
  `collect_premarket_movers` 候选池 (量榜+财报榜+盘前榜三源)
- 零行为变更证明: 扩池函数仅返回 code/name, 后续仍走既有
  collect_snapshot_batch + |涨跌幅|≥4% + 盘前量≥500 阈值门 — 门禁不变
- 实测 (盘前时段端到端): movers 100 只, 其中 **79/100 不在旧池**
  (量榜+财报榜之外) — LIF 型"榜外消息驱动盘前异动"盲区实测补上;
  全链路耗时 102s (盘前扫描窗口内可接受)
- 坑记录: get_us_pre_market_rank 返回 (all_count, DataFrame) 元组
  非标准 (ret, data); sort_dir 必须传 RankSortDir.ASCENDING/DESCENDING
  枚举, 传 SortDir/字符串/int 均报 "the type of sort_dir param is wrong"

**验收**: py_compile 全过 + 1126 passed + 重启后零 ERROR。
**回滚**: ① 删 fallback 调用行即恢复 AV 单源; ② 删 collect_premarket_movers
内 try/except 扩池块即恢复双源池。

**遗留观察 (不在本次范围)**: _load_key_us_symbols 读 config/watchlist_us.json
但文件实际在 config/deprecated/ → 永远走静态 36 只 fallback (动态 53 只未生效)。

### 6.4 质量闸真正启用 (2026-08-12, 用户追问"如何真正有效启用")

追问触发全链验证, 发现质量闸两处病灶导致"名义存在、实际失效":

**病灶① 单位误读 (阈值严 7 倍)**: 原注释"富途 market_val 单位人民币元,
2B美元≈14B人民币"是错的 — 实测锚定 AAPL: 快照与 get_stock_filter 口径
market_val 均 = 4.45e12, 与真实市值一致 → 单位就是 USD。14e9 阈值实际
= $14B, 比设计意图 $2B 严 7 倍 (2-14B 中盘被误杀)。

**病灶② mcap=0 漏洞 (闸对 1/3 候选无效)**: earnings_surprise 等 Finnhub
渠道不带市值, 原 `if mcap` 判断直接放行 — 实测 290 穿门者中 97 只 (33%)
市值未知, 其中快照回填后 69/97 实为 <$2B 微盘。

**修复 (market_scanner.py _quality_gate)**:
- 阈值 14e9 → 2e9 (回归 $2B 设计意图, 单位 USD 注释纠正)
- 无市值候选批量快照回填 (collect_snapshot_batch 单连接分批) 后再判;
  回填失败保持放行 (闸门失败不阻断扫描 = fail-open, 下游 DA 门禁兜底)

**修复验收 (同盘前时段实测)**: 337 → 222 (净杀 68 微盘), 穿门者 <$2B
残留 0; 回填成功率 87/97; 2-14B 中盘恢复放行。
方向性影响: 闸同时保护空头侧 (微盘做空借券难/波动毒), 盘前榜新增的
BCARU -15.44% 等真实中盘空头供给得以保留。
