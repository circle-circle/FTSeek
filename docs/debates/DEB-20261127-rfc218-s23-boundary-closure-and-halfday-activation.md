# DEB-20261127 · RFC-218 §23 审计边界闭合批：镜像隔离/bridge 硬化/观测原子性＋半日市守卫激活裁决留痕

> 状态：**已落地**（代码＋测试＋激活）；部署凭据见 RFC-218 §23.6 与 handoff 同日条目。
> 授权链：用户转发独立审计（P1×3 已 §22 落地；边界×5）→ 用户裁「请按照建议执行落地优化」。
> 关联：RFC-218 §§20–23、DEB-20261127-da-selection-direction-review-verification（DA 链路解释批）。

## 1. 本批裁决与依据

| 项 | 裁决 | 依据 | 变量类型 |
|---|---|---|---|
| SHORT 实盘镜像实验隔离 | 落地（无开关，今日零流量） | §22.9#5：SHORT 实盘授权前必备隔离；live 闸 LONG/BUY-only ⇒ 零行为变化 | 正确性/纵深防御 |
| bridge 身份硬化 | 落地（四元组四 stat 三比对） | §22.9#2：同 mtime/size 身份替换可穿透旧校验 | 正确性 |
| EXT 观测原子性 | 落地（flock＋mkstemp/os.replace 四处 RMW） | §22.9#4：无锁 RMW＋共享 tmp 重叠调用漏计/双推 | 观测正确性 |
| **半日市守卫激活** | **`.env.us` =1（21:15 CST）** | 时段语义正确性：今日 NYSE 13:00 ET 收盘，静态窗 17:00–19:59 ET 对已收市市场放行；off_sweep expansion=0 只 cap 不扩 | **预注册臂供给面变量修订** |
| EXT dry 静默语义 | **不做，待裁** | dry=True 是否必须静默属语义裁决，不擅改 | 待裁 |
| adapter 同日重跑语义 | **不做，待裁** | 研究候选更新 vs 新指令生成事件未裁决；不删完成标记 | 待裁 |
| watchlist fail-close | **不做，待裁** | §22 已补 warning+栈；异常即停新仓另行裁决 | 待裁 |
| EXT_BLACKLIST_GUARD / DA_CAPITAL_CODE_FIX | **保持 OFF** | 分别 BLOCKED 用户裁 QCOM 适用性 / §10.4 A/B | 未捆绑 |

## 2. 半日市激活的授权解读（显式留痕，可回滚）

- 审计原文：「建议优先完成该项激活裁决，但本轮没有授权启用，也没有进行启用。」
- 用户随后指令：「请按照建议执行落地优化」。本批解读：该指令承接审计建议完成激活裁决，
  且今日（感恩节次日）即真实暴露日，等待=接受 closed-market 暴露。
- **反证保护**：若用户本意不含激活，`.env.us` 改回 0 即回滚（cron fresh-import，无需重启）；
  激活时点（21:15 CST）至首个暴露 tick（CST 周六 06:02）之间守卫零行为差异（今日剩余 tick 全为盘前）。
- 自然影子台账缺席的说明：§20.5 原裁决输入含「今日自然影子台账」，但首个分歧 tick 即暴露 tick；
  分歧机制为日历确定性（全分钟扫描已证），激活后影子以 `mode=blocked` 继续留痕，审计链不断。
- 预注册修订：EXT 臂供给面仅在「半日市∧盘后∧ET 17:00–19:59」收窄；起点扩窗（13:00–16:00）仍不做。

## 3. 镜像隔离合同（SHORT 实盘授权前置条件）

- 标记存在性（`is not None`，空 dict 标记也算）∨ sizing regime 审计（`no_trade`）→ 排除；
  SHORT 两源不可读 → fail-closed；LONG/BUY 无标记永不排除；谓词异常 SHORT fail-closed/LONG fail-open。
- 隔离先于臂闸，拒绝走同一 `(None, gate)` 合同（`NO_TRADE_SHORT_EXP_ISOLATION`）。
- **未来 SHORT 实盘授权裁决时必须复核本谓词**；本批不是授权，不改变 live 闸 LONG/BUY-only 现状。

## 4. HK-US 联动：现状与预注册（本批只读探针，不实施）

- 现有链接面：`app/catalyst/sources/hkex_source.py` HKEX 披露易新闻适配器，7 组 HK→US ADR 映射
  （00700→TCEHY/09988→BABA/09618→JD/03690→MPNGY/09888→BIDU/01810→XIAOMY/09999→NTES），
  公告关键词→urgency；**新闻面，非行情面**。
- 用户问题（白天港股波动→美股波动联动）属**新输入变量假设**：港股时段（ET 21:30–04:00 夏令/
  22:30–05:00 冬令）与美股盘前/盘后重叠，ADR 港股日内动量对美股 ADR 开盘价有理论传导，
  但**传导强度/时滞/可交易性未验证**，不得直接接 sizing/gates。
- 预注册（若用户裁决推进）：
  1. 第一步 record-only 影子采集器：共享 OpenD 港股快照（权限探针见 §5），按 ADR 映射落
     `data/shadow/hk_us_linkage/`（港股日内 chg/量/时点 + 对应 ADR 盘前/开盘反应），零判定消费；
  2. 预注册判据：n≥60 独立交易日、港股大幅波动日（|chg|≥2%）子集、ADR 开盘方向命中率与
     基线（无条件开盘动量）对比、MDE 预注册；通过后才谈接入 DA 输入（再走 §10.4 A/B）；
  3. 回滚=删 cron 行；任何阶段不接 sizing/gates/方向判定。
- **本批不建采集器、不加 cron**（新观测面=新变量，须用户裁决后另批）。

## 5. OpenD 港股行情权限探针（只读）

- **2026-11-27 21:45 CST 单次探针**（`/tmp/rfc218_s23_hk_probe.py`，日志 `/tmp/rfc218_s23_hk_probe.log`）：
  `OpenQuoteContext(127.0.0.1:11111).get_market_snapshot(['HK.00700'])` → **ret_code=0（权限可用）**。
- 返回：腾讯控股 last=352.0 / prev_close=341.0（**今日 +3.23%**，即用户所问港股翻红实例）/
  open=340.0 / high=353.4 / low=339.6 / volume=35,593,034 / turnover≈124.0 亿 HKD /
  update_time=2026-11-27 16:07:57（HKT，收盘后快照）/ sec_status=NORMAL。
- 结论：共享 OpenD 港股快照行情面可读，§4 预注册采集器的数据面可行；**本批不建采集器/不加 cron**
  （新观测面=新变量，须用户裁决后另批）。单次快照≠订阅/历史 K 线权限；采集器若推进，
  订阅额度与历史 K 线权限须另行探针。今日 +3.23% 只是单日描述性事实，不构成传导强度证据。

## 6. 收益边界

- 本批无任何收益改善证据；全部为正确性/风控/观测修复。半日市激活依据=时段语义，
  不声称「避免了亏损」（被 cap 窗口历史成交质量未单独验证）。正确性≠盈利。

## 7. 待裁清单（不在本批实施）

1. adapter 同日重跑语义（研究候选更新 vs 新指令生成事件）。
2. EXT dry=True 静默语义。
3. watchlist 异常 fail-close。
4. 资金分布 §10.4 A/B 批准。
5. EXT_BLACKLIST_GUARD 激活（QCOM 禁令适用性复核）。
6. 半日起点扩窗（13:00–16:00 ET）＋`_pretrade_gate` freshness。
7. score_band×gate 耦合调整（MEDIUM 封顶良是否期望；见 docs/analysis/ANALYSIS-20261127-score-band-gate-coupling.md）。
8. HK-US 联动采集器是否推进（§4 预注册）。

**最后更新：2026-11-27。**
