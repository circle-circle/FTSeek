# REVIEW-20260908 · 交易界面（港股 /trade/hk · 美股 /trade）信噪比与用户体验审计

**日期**: 2026-09-08
**性质**: 审计（§A–§D）**+ 批 1 实施记录（§E）+ 批 2 实施记录（§F）**，两批均于 2026-09-08 同日落地（批 1 已重启生效，批 2 免重启）。
**边界**: 不含任何下单/退出管线变更 —— `validate_order()`（美股）与 `hk_order_service.validate()`（港股）**逐字未改**，Playbook 唯一管线、退出参数冻结（OKR-2026-09 C1/C7）均不受影响；零判据阈值变更、零新闸门、DB 只读。
**⚠️ 一处更正（用户 2026-09-08 指出）**: B1 原列的 **PDT 项作废** —— PDT 守卫已于 **2026-08-02 用户裁决关闭**（`config/unified_config.yaml:1646-1647` `risk.pdt_guard.enabled: false`；`app/risk/pdt_guard.py:347-350` 开关关闭即 `return True`），富途证券(香港)账户不适用 FINRA PDT。审计里那三次拒单发生在 **2026-06-30**，**早于关闭日**，属历史截断而非现存约束。详见 §E.1。
**范围**: `app/web_dashboard/templates/hk_trade.html`、`app/web_dashboard/routes/hk_trade.py`、`app/web_dashboard/hk_order_service.py`、`app/web_dashboard/templates/trade.html`、`app/web_dashboard/static/js/trade.js`、`app/web_dashboard/routes/trade.py`、`app/web_dashboard/order/validator.py`。
**方法**: 代码通读 + **真实使用痕迹反推**（`data/audit/hk_manual_orders.jsonl` 63 条、`ftseek_us.db::manual_order_audit` 83 条）。结论一律附可复现命令与代码锚点（宪章 §二.7「遥测实测 > 文档自述」）。

---

## 一句话结论

两页功能都跑得通，但信噪比的**主要失衡是同一个**：**后端已知的硬约束与决策上下文不在前端呈现，用户只能"提交后才知道"**；与此同时前端把版面花在了低信息密度元素上（券商英文枚举、恒亮趋势箭头、死 CSS / 死端点 / 每轮双份日志）。

实测拒绝率：

| 页面 | 提交尝试 | 被拒/异常 | 其中"前端可预防" |
|---|---|---|---|
| 港股 `/trade/hk` | 59 | 13（22.0%） | 11 |
| 美股 `/trade` | 83（含撤单） | 13（15.7%） | 9 |

港股剔除上线调试日 2026-07-22（当日 9 次拒绝多为阈值试探）后仍为 **4/49 ≈ 8.2%**，全部属可预防类型（卖空价位规则 ×2、价位表 ×1、限价偏离确认 ×1）。

复现命令：

```bash
python3 - <<'PY'
import json, collections
rows=[json.loads(l) for l in open('data/audit/hk_manual_orders.jsonl') if l.strip()]
print(collections.Counter((r['op'], r['status']) for r in rows))
for r in rows:
    if r.get('status') not in ('submitted','cancelled'):
        print(r['ts'][:16], r['symbol'], r['status'], str(r.get('error'))[:120])
PY

python3 - <<'PY'
import sqlite3
db=sqlite3.connect('data/ftseek_us.db')
for r in db.execute("select timestamp,symbol,status,error_msg from manual_order_audit where status not in ('submitted','cancelled') order by timestamp"):
    print(r[0][:16], r[1], r[2], (r[3] or '')[:110])
PY
```

---

## A. 港股页 `/trade/hk`

> 设计前提（不改）：无状态直通、不写 DB、不接入本系统风控/退出监控（`hk_trade.py` docstring、`hk_trade.html:62` banner）。**以下建议全部是"把 HK 服务自己的规则与已有数据提前呈现"，不引入任何新风控层、不写库**，与物理隔离原则一致。

### A1【安全 · P0】提交时忽略输入框代码，优先用上一次报价的标的
`hk_trade.html:385` — `const symbol = (lastQuote?.symbol) || document.getElementById('q-symbol').value.trim();`
输入框 `hk_trade.html:74` 只有 `onkeydown=Enter → fetchQuote()`，**无 `oninput`/`blur` 失效逻辑**。
后果：查过 00700 后把代码改成 09988 但没点"查询"就直接提交 → **订单打到 HK.00700**，且 `lot_size` 也用旧标的（`hk_trade.html:389`）。按钮文案不含标的（A6），用户无从发现。
修法（任一即可，建议全上）：① `q-symbol` 加 `oninput` → `lastQuote=null` + 报价卡置灰；② 提交前比对 `输入框规范化值 !== lastQuote.symbol` 即拦截并提示"报价与代码不一致，请先查询"；③ 按钮文案直接写标的（A6）。

### A2【信噪比 · P0】5 类硬约束只在拒单后可见
后端常量：金额上限 `HK$400,000`（`hk_order_service.py:59`）、当日上限 `20` 单（`:60`）、整手校验（`:227`）、限价偏离 5%/10%（`:62-63`、`:242-251`）、价位表。
前端只在 `hk_trade.html:363-380` 显示"= N 股，约 HK$X"，**上限、余量、偏离、整手全部不可见**；偏离警告更是只在服务端拒回后才弹出（`hk_trade.html:421-424`）。
实测代价：金额超限拒绝 **4 次**（2026-07-22，HK.00700/00981）、整手拒绝 **3 次**、偏离往返 **2 次**。
修法：把 `qty-hint` 升级成常驻"提交前检查条"（红/黄/绿即时）：
`约 HK$92,400（占单笔上限 23%）· 今日 3/20 单 · 偏离现价 -1.8% · 整手 ✓（100股/手）`
其中"今日 n/20"需在 `GET /api/hk/orders/today` 响应里加一个只读字段（`svc._count_today_orders()`，`hk_order_service.py:450`），零行为变更。
活跃日实测已到 9 单/日（2026-09-07），上限不是理论值。

### A3【信噪比 · P1】静默失败伪装成"空状态"
`hk_order_service.py:351/360/387`（持仓）与 `:392/401/425`（订单）在**未连接 / 查询失败 / 异常**时一律 `return []`；路由 `hk_trade.py` 再包成 `api_ok` → 前端渲染"无港股持仓"（`hk_trade.html:455`）/"今日无订单"（`:483`）。
后果：OpenD 掉线时页面看起来"干净无持仓"，这是实盘页最危险的误读。
修法：服务返回区分 `error`（或路由改 `api_error` + 复用现成 `err_broker_unavailable()`，`api_response.py:51`）；前端三态渲染：**空 / 加载失败+重试按钮 / 数据降级**（对齐美股页 `trade.js:1347-1358` 的三态圆点做法）。

### A4【信噪比 · P1】订单表直出券商英文枚举 ✅ 已实施（§F.1）
`hk_trade.html:492-493` 原样打印 `trd_side` / `order_type` / `status` → 界面出现 `BUY` / `NORMAL` / `SUBMITTED` / `FILLED_ALL` / `CANCELLED_ALL`。中文界面里的纯噪声，且 `NORMAL`（=限价）对用户无意义、状态无颜色区分。
修法：一份"券商枚举 → 中文 + 颜色"字典（已报=黄 / 部分成交=蓝 / 已成=绿 / 已撤=灰 / 废单=红），方向中文化（买入/卖出/卖空）。同字典可服务 A5 聚合行。

### A5【信噪比 · P1】同标的当日累计无聚合（实测最痛的缺口） ✅ 已实施（§F.1）
审计显示典型用法是**同标的日内多笔、连续向下加仓**：
`HK.02513` 2026-09-04 买 1090/1089/1078 → 09-07 买 1070/1051/1030 → 09-08 买 998/956/924（三日 9 笔，价格 -15%）；`HK.00100` 2026-09-07 卖 399/392 后买 20 股 ×4 笔（MKT/361/351/330）。
页面只有原始流水（`hk_trade.html:476-502`），**在点"提交"的那个位置看不到自己已经在这只票上加了多少**。
修法：报价卡下方加一行上下文（数据源 = 已有 `/api/hk/orders/today` 的 `dealt_qty × dealt_avg_price` + `/api/hk/positions`，**纯前端聚合，不落库、不新增闸门、不阻断**）：
`今日该标的：买 3 笔 / 300 股 / 均价 1,019 · 现价 924 · 持仓浮亏 -9.3% · 累计投入 HK$305,700`
注意口径：这是**信息呈现**而非风控。港股按设计不接入系统风控，因此不得升级为阻断/确认关键词（用户 2026-07-22 已裁"去掉 CONFIRM 与理由必填，降低手动下单摩擦"）。

### A6【UX · P1】提交按钮恒定暗红 + 恒定文案，且与方向色语义冲突 ✅ 已实施（文案 §E.2 / 方向色 §F.1）
`hk_trade.html:146` 文案永远是"提交实盘订单"；`.btn-primary` 恒为 `#b91c1c`（暗红），而方向按钮是买入=绿 / 卖出=红（`hk_trade.html:22-23`）→ **选了"买入"（绿）却要按一个红按钮**，且按钮不复核标的/数量/金额。
对照美股页做得对：`trade.js:647` 按钮实时变成 `预览: 开仓 做多 $12,340`，`trade.js:879` 确认键变成 `确认 买入 100 股`。
修法：按钮跟随方向色 + 文案带全要素：`买入 HK.02513 · 100 股 · 约 HK$92,400`（顺带兜住 A1）。

### A7【安全 · P1】最高风险页面反而没有常驻"实盘"标识
`base.html:298/315/321` 的 `nav_extra` / `nav_extra_tablet` / `nav_extra_mobile` 三块，`trade.html:9-17`、`dashboard.html`、`system.html`、`watchlist.html` 都定义了，**`hk_trade.html` 一个都没定义** → 港股页头部/移动端头部没有任何模式徽标，只剩一条会随页面滚走的 banner（`hk_trade.html:62`）。而港股页是**恒实盘直通**（`hk_order_service.py` `TrdEnv.REAL` + `unlock_trade`）。
修法：三处 `nav_extra*` 补 `🔴 港股实盘` 徽标；banner 改 `position: sticky`（或缩为一行常驻细条）。

### A8【UX · P2】卖空价位规则（uptick）零提示
实测 `HK.09988` 2026-08-20 连续两次 `卖空订单价格不可低于卖一价，请修改价格。`（间隔 24s，说明用户照原样重试了一次）。
页面已有卖一价（`hk_trade.html:88`，`usePrice('ask')`）却没把它和卖空规则连起来。
修法：`SELL` 且 `quantity > can_sell_qty`（= 卖空）时提示"港股卖空须 ≥ 卖一价"，并给一键"填卖一价"。

### A9【UX · P2】价位表只对 ± 按钮生效，手输价格不校验
`hkTick()`（`hk_trade.html:278-290`）只被 `adjPrice()`（`:292`）使用；`f-price` 是 `step="0.001"` 的自由输入，`usePrice()`/自动带入现价也不吸附。
实测 `HK.00700` 2026-08-28 `该产品的最小变动价位为0.2，请按其整数倍调整价格。`
修法：`blur` 时按 `hkTick` 吸附（或红字提示"该股价位 0.2，最近合法价 X"）。

### A10【性能 · P2】无可见性守卫，后台标签页持续打券商 API
`hk_trade.html:522` `setInterval(..., 30000)`（持仓+订单）与 `:260` `setInterval(fetchQuote(true), 10000)`（报价）**都没有 `visibilitychange` 停轮询**，也不区分开盘/收盘；收盘后仍每 10s 拉快照。富途 API 有频率限制，白耗额度。
对照美股页已实现：`trade.js:1489-1520`（盘中 5s / 盘外 15s + Visibility API + WS 在场时跳过 HTTP）。
修法：照搬同一套守卫（hidden 停、收盘降频到 60s 或停）。

### A11【UX · P2】交互语言不一致 + 缺账户条
- 撤单用 `window.confirm`（`hk_trade.html:505`）与 `alert`（`:513-514`），全站其它地方用自绘 modal（`trade.html:606-615`）。
- 港股页**没有账户现金/购买力/市值条**（`hk_order_service.py` 全文无 `accinfo_query`），实盘下单却看不到可用资金；美股页有完整摘要条（`trade.html:318-332`）。
修法：撤单改复用 modal；补 `accinfo_query(TrdMarket.HK)` → 顶部一行"现金 / 购买力 / 证券市值 / 今日盈亏"。

### A12【移动端 · P3】10 列表格只能横向滚动
持仓表 10 列（`hk_trade.html:160`）、订单表 10 列（`:174`）都靠 `overflow-x:auto`；≤900px 时网格塌成单列（`:12`），手机上必须横滑才能看到"盈亏/可卖/撤单"。
现成资产：美股页已写好卡片视图 CSS `.pos-cards-mobile` / `.pos-card-m*`（`trade.html:130-143`）但**从未被渲染**（全仓零消费）——可直接复用到港股页做移动端卡片。

---

## B. 美股页 `/trade`

### B1【安全 + 信噪比 · P0】闸门只在拒单后可见（含 kill switch）
`validator.py` 里手动单要过：PDT（`:89-98`）、购买力（`:108-141`）、日回撤熔断（`:143-155`）、资本安全 BLACKOUT（`:157-170`）、手动预算 80% 上限（`:172-183`）、限价偏离（`:185-232`）。前端**一个都不呈现**：`trade.js` 的全部 fetch 目标里没有 `/api/risk/*`（实测 grep：仅 battle-plan / orders / quote / order-capacity / snapshot / symbol-intel / thesis）。
- ~~`PDTGuard.get_status_report()`（`app/risk/pdt_guard.py:375-385`）现成返回 `day_trade_count_5d / max_day_trades / risk_level`，**但无任何路由暴露**（`rg -n 'pdt|PDT' app/web_dashboard/routes/*.py` 零命中）。实测代价：2026-06-30 23:19 / 23:22 / 23:27 连续三次 `PDT 风控拦截: 已达日内交易上限 (3次/5交易日)`（换标的重试仍被拦）。~~
  **【2026-09-08 更正 · 本条作废，不得实施】** 用户指出「美股 PDT 限制条件在当前富途中不存在」，代码复核确认：
  ① `config/unified_config.yaml:1646-1647` → `risk.pdt_guard.enabled: false`；
  ② `app/risk/pdt_guard.py:347-350` → `if not self._is_enabled(): return True, "PDT 守卫已关闭 (2026-08-02 裁决: 富途香港不适用 FINRA PDT)", PDTRiskLevel.SAFE`；
  ③ 故 `validator.py:89-98` 的第 6 步是**死分支**（永不产生 error）。
  上述三次拒单发生在 **2026-06-30**，早于 2026-08-02 关闭日 —— 是**历史样本分层标记**（`DEB-20260802-live-cut-prereg.md §七`：解除日前批次受 PDT 截断），不是现存约束。
  **在 UI 上呈现一个不存在的约束 = 纯噪声，且会诱导用户按 3 次/5 日自我设限**，与本审计「提信噪比」的目标相反 ⇒ 批 1 **不实施**该项，并加钉子测试禁止回归（§E.4）。
  **附带发现（待裁，不在本批动手）**：`validator.py:89-98` 这段死分支可按减法三步（证零消费 → 停 → 退役）清理，但它位于**下单校验管线**内，须单独立 DEB 由用户裁，本批不碰。
- `/api/risk/command`（`risk.py:215-276`，15s 缓存）已返回 `kill_switch_active / vix / margin_pct / earnings_count / suggestions`，交易页从不请求 → **紧急制动已激活时，交易页照常显示"预览下单"**，用户点了才知道。
- 其它实测拒单：`仓位限制 [单品种上限] US.SNDK SHORT … > 上限=$14,994`（2026-06-26）、`购买力不足`、`手动预算不足`、`ALLOW_REAL_TRADING 未设为 true`（2026-06-24）。
修法：① 摘要条（`trade.html:326-331`）加 chip：~~`PDT 2/3`~~（**已作废**，见上）、`Kill Switch`、`水位/手动预算余量`、`今日手动单 n/上限`；② 预览弹窗内列出"本单将触发的闸门"；③ 新增 `GET /api/trade/gates`（只读聚合 `capital_safety` + `check_kill_switch()` + `DrawdownController` + 服务常量，15s 缓存，**不含 PDT**）。**只呈现、不改判据、不改阈值**。→ 实施结果见 §E.2 B1。

### B2【信噪比 · P1】盘外时段限价偏离检测两端同时跳过 = 零防护
前端 `trade.js:792` `_skipDevCheck = (phase !== 'REGULAR')`；后端 `validator.py:189-196` `_skip_deviation = get_market_phase() != MarketPhase.TRADING`。理由（`trade.js:681`、`validator.py:187`）是"盘外 `cur_price` 是上次收盘价，不反映盘外报价"——**但报价响应里已经带了时段价**：`pre_price` / `after_price` / `overnight_price`（`manual_order_service.py:262-265`），前端甚至已经在展示它们（`trade.js:546-557`），只是没拿来算偏离。
后果：偏离检测（因 SNDK 事故而生，`validator.py:186`）在盘前/盘后/夜盘**完全失效**；实测 2026-06-25 04:43-05:04（= ET 盘后）SNDK 连续 4 次 12.3%/10.7%/11.3% 偏离拒单，正是该场景。
修法：盘外改用时段对应价作基准，文案标注基准（"对比盘后价 $1,610"），阈值不变。

### B3【信噪比 · P1】平仓弹窗没有开仓那套时段/类型守卫
开仓侧：`trade.js:746-748` 盘前盘后禁市价、休市禁市价并自动 GTC + 明示"排队至下一交易时段"。
平仓侧：`executeClose`（`trade.js:1118-1175`）**无任何时段判断**，未填限价即市价，且 `trade.js:1149` 自动 `fill_outside_rth = extClose || isClosedClose`；后端 `executor.py:427` 直接 `FutuOrderType.MARKET` 发出。
后果：盘外市价平仓大概率被券商拒，用户只看到 `❌ <富途原文>`，没有"改限价"的下一步。
同类实测（开仓侧 GTC 语义）：2026-08-20 09:48 `US.BABA SELL 100 LIMIT` → `撤单前有效订单不支持股票卖空。股票卖空请使用当日有效订单。`（休市自动 GTC + 卖空 = 券商不接受）。
修法：平仓弹窗复用开仓的时段逻辑（盘外禁市价、给"填入盘后价"一键按钮）；卖空/空头平仓强制 `DAY` 并在预览里写明原因。

### B4【信噪比 · P2】恒亮趋势箭头 = 稳定噪声 ✅ 已实施（§F.1）
`trade.js:1405`：首轮之后**每一行、每一次轮询**都会渲染 ↑ / ↓ / **→**，其中"→ 横盘"零信息量；盘中 5s 一轮（`trade.js:1485`）导致整列持续闪动。
修法：仅当 |Δ| 超阈值才显示箭头（并标注比较窗口），中性态不渲染。

### B5【准确性 · P2】横幅文案硬编码"15s 内变动" ✅ 已实施（§F.1）
`trade.js:1253` 文案写死 `15s 内变动 $X`，而实际比较窗口 = 上一轮渲染，盘中是 **5s**（`trade.js:1482-1487`）。数字对、时间标错 → 用户对"恶化速度"的判断被系统性放大 3 倍。
修法：用实际间隔动态生成，或改为"上次刷新以来"。

### B6【信噪比 · P2】最关键的风控信息被压成 10px 圆点 ✅ 已实施（§F.1）
持仓表表头只有 代码/市值·数量/现价·成本/今日盈亏/持仓盈亏/占比（`trade.html:505`）；"距止损"只体现为代码旁一个 10px 色点（`trade.js:1417`），要看数字必须点开弹层（`showDiag`，`trade.js:997-1043`）。而注意力横幅的最高优先级恰恰是"距止损 < 2%"（`trade.js:1242-1250`）。
修法：加一列"止损 / 距止损%"文本（数据已在 snapshot 的 `stop_distances`，`trade.py:434-442`、`trade.js:1364-1367`），弹层保留为动作入口。

### B7【噪声 · P2】死码 + 每轮双份日志
- 死 CSS（零消费，实测 `grep -c` 均为 0）：`.dq-*` 决策质量面板 28 行（`trade.html:165-197`，随 slow-data 一起退役）、`.cap-grid`/`.cap-item`（已被 `.fund-info` 取代）、`.pos-cards-mobile`/`.pos-card-m*`（`trade.html:130-143`，见 A12 可复用）。
- 死端点：`GET /api/trade/slow-data`（`trade.py:475-686`，约 210 行）前端已摘除（`trade.js:1467` 注释），全仓无消费方。
- 日志噪声：每轮轮询前端 `console.log('[snapshot]', …)`（`trade.js:1286`）+ 后端 `logger.info("[trade/snapshot] …")`（`trade.py:472`）；盘中 5s 一轮 = **每分钟 24 行日志**，且 `trade.py:471` 在函数体内 `import json` 后并未使用。
修法：按减法三步（证零消费 → 停 → 退役）处理死码/死端点；snapshot 日志降为 `debug` 或加变化才记。

### B8【可维护性 · P2】预览弹窗偏离警告两段 ~60 行重复内联 HTML
`trade.js:810-872`：硬（≥10%）与软（≥5%）两分支各写一份内联 HTML + 内联 `onchange` 字符串（含 `window._priceWarnAck` 全局态），差异只有颜色/文案。
对照港股页已有干净的 `warn-box` + checkbox 模式（`hk_trade.html:142-145`）。
修法：抽成一个 `_renderPriceWarn(kind, dev, scenarioMsg)`，样式走 CSS class，去掉 inline JS 字符串。

### B9【可访问性 · P2】方向/操作切换是 `<div onclick>`
`trade.html:351-352`（做多/做空）、`:358-359`（开仓/平仓）是 `div` + `onclick`，无 `role`/`tabindex`/键盘支持；港股页同一控件用的是真 `<button type="button">`（`hk_trade.html:104-105`）。
修法：改 `button`（样式不变）或补 `role="button" tabindex="0"` + Enter/Space 处理。

### B10【UX · P3】三处小口子
- **无最近标的记忆**：港股页有 localStorage chips（`hk_trade.html:327-344`），美股页零 localStorage（实测 grep 无命中），只能靠 URL 参数（`trade.js:1713-1799`）。
- **`arguments.callee` 重挂定时器**（`trade.js:1500`）：严格模式下直接抛 `TypeError`，是该文件未来加 `'use strict'` 的地雷。
- **`curQuote` 不校验 symbol**：报价响应带 `symbol`（`manual_order_service.py:255`）但前端从不比对；快速改代码后立刻点预览，预估金额/偏离会用**上一只票**的价格（`trade.js:740`）。与 A1 同族，修法相同（比对 + 不一致则重取/置灰）。

### B11【口径不一致 · 需用户裁】平仓理由必填且纯手打
`trade.js:1132-1137`：理由为空直接拦（"请填写理由"）。这是**最紧急动作上的摩擦**，且与港股页口径相反（港股 2026-07-22 用户已裁"去掉理由必填"，`hk_order_service.py:19`）。
建议（保留留痕、去掉打字）：给 5 枚一键 chips（止损触发 / 止盈达成 / 计划变更 / 风控降敞口 / 其它）+ 可选补充文本。
**此项涉及口径统一，建议单独立 DEB 由用户裁一次**，不在本批自动执行。

---

## C. 跨页一致性（信息架构层）

1. **两套实现、质量不齐**：港股页 CSS+JS 全内联在模板（523 行），美股页在 `trade.js`（1807 行）。同一件事（撤单确认、空/错三态、枚举中文化、轮询守卫、模式徽标、最近标的、方向控件语义）各写一遍，且**港股页普遍缺美股页已有的防护**（Visibility、三态、动态按钮文案），美股页普遍缺港股页已有的轻量便利（chips、±tick）。
   → 建议：抽 `static/js/hk_trade.js` + 一份共享 `trade-ui.css` 片段 + 两份"券商枚举→中文"字典（HK/US 各一），把上面 6 个共性点收敛到单实现。
2. **发布陷阱（必须先知道）**：`/static/` 响应头是 `Cache-Control: public, max-age=2592000, immutable`（`app.py:184`），而 `trade.html:638` 的版本号还停在 `?v=20260625b`。**改 `trade.js` 不同步 bump `?v=` = 手机上最长 30 天看不到新代码**（历史上这类"改了没生效"极可能被误判为修复无效）。港股页 JS 内联在模板，不受此影响。
3. **重启口径**：改 `.py`（`routes/trade.py`、`routes/hk_trade.py`、`hk_order_service.py`）必须 `systemctl restart ftseek-us` + 跑启动冒烟（AGENT.md 军规）；只改模板/JS/CSS 不需重启，但要 bump `?v=`。
4. **IA 层最大的信噪比提升（改动也最大，建议单独立 DEB）**：移动端底部已 6 个 tab（`base.html:327-332`，Playbook 缺席），"交易"与"交易 HK"并列两个 tab 但共享度极高（账户条、订单流水、模式徽标）。合并为单一"交易"入口 + 市场段控件（US / HK）可省一个 tab、共享摘要条与枚举字典；代价是港股"物理隔离"要在前端用清晰的视觉分隔维持（隔离本体在服务层，不受影响）。

---

## D. 建议分批（均不触碰下单/退出管线与任何判据阈值）

| 批 | 主题 | 条目 | 风险 | 状态（2026-09-08） |
|---|---|---|---|---|
| 批 1 | 防错与安全（纯呈现层） | A1、A2、A3、A7、B1、B2、B3 | 低：只加信息/校验，不改闸门与阈值；需 bump `?v=`；B1 需新增只读 `/api/trade/gates` | **✅ 已实施**（详见 §E；B1 的 PDT 子项**作废**；A6 按钮文案顺带做完，仅"按钮跟随方向色"留批 2） |
| 批 2 | 信噪比 | A4、A5、A6（余：方向色）、B4、B5、B6 | 低：字典 + 聚合行 + 列/文案修正 | **✅ 已实施**（重启后接续，详见 §F；纯模板/JS，免重启） |
| 批 3 | 清洁与一致性 | A8–A12、B7–B10、C2、C3（**C1 共享化重构本批不做**，留待单独立项） | 低-中：含死码退役（走减法三步）与 `.py` 改动（需重启） | **✅ 已实施**（详见 §G；含重启+冒烟+线上实测，复核中抓到并修正一处自造假警报） |
| 批 4 | 裁决落地（首次含后端 `.py` ⇒ 需重启） | **B11 理由非必填（用户明裁）**、PDT 死分支退役、`_parse_risk_level` 九档更正、**手动平仓账本静默断链根治**、仓外 skill 文档退役标注、C2 | 中：动到校验层，但只删恒放行分支 + 改只读查表；判据/阈值/闸门零变化 | **✅ 已实施**（详见 §H；重启 18:33:18 + 冒烟 7/7 + 线上前后对照实证 + 全量 3344 passed） |
| 待裁 / 延后 | 结构类 | C1（共享化重构）、C4（IA 合并，以 C1 为前置）、`slow-data` 墓碑物理删除、`pre_trade_check.py:425` 自动管线同款 PDT 死分支、`exit_reason` 手动自由文本 vs 自动枚举双口径 | C1/C4 属结构性搬迁；后三项涉自动管线或判据层 | 🔒 **代裁延后**（依据与重开条件见 §H.4 / §H.5） |

**验收建议**：批 1/批 2 落地后，用同一份审计口径复测拒绝率（本文开头两段脚本），目标：港股可预防类拒绝 11 → 0，美股 9 → ≤2；并对比 `hk_manual_orders.jsonl` 新增记录中 `status != submitted` 的占比。

---

## E. 批 1 实施记录（2026-09-08 同日落地）

> 用户裁「需要」⇒ 批 1（A1/A2/A3/A7/B1/B2/B3）当日实施；随后用户指出「美股 PDT 限制条件在当前富途中不存在」⇒ B1 的 PDT 子项**作废并加钉子**（§E.1）。

### E.0 不变量声明（先说没动什么）

| 不变量 | 证据 |
|---|---|
| `validate_order()`（美股）逐字未改 | `git diff -- app/web_dashboard/order/validator.py` = 空 |
| `hk_order_service.validate()` 判据/阈值逐字未改 | diff 只落在 `get_positions*` / `get_today_orders*` / 新增 `get_order_limits()` |
| 零新闸门、零阈值变更、不阻断任何原本合法的单 | 前端除 A1 外一律不 `disable`（§E.3-1） |
| 不写库、不碰 Playbook / 退出管线 | 新增只有 1 个只读 GET 端点 + 1 个只读聚合模块 |
| 港股「物理隔离」不变 | `hk_order_service` 仍不接系统风控；A5 聚合行按设计只做呈现 |

### E.1 PDT 更正（用户裁决复述 + 代码复核）

**裁决链**：`DEB-20260802-live-cut-prereg.md §七`（用户裁「按照建议关闭」）→ `config/unified_config.yaml:1646-1647` `risk.pdt_guard.enabled: false` → `app/risk/pdt_guard.py:347-350` 开关关闭即 `return True, "PDT 守卫已关闭 (2026-08-02 裁决: 富途香港不适用 FINRA PDT)", SAFE`。

**推论**：`validator.py:89-98` 第 6 步是**死分支**（`PDTGuard()` 走 `_is_enabled()` → False → 恒放行），故手动单**当前不可能**被 PDT 拒。审计中 2026-06-30 那三次 `PDT 风控拦截` 早于关闭日，属**历史样本分层**证据，不得当作现存约束呈现。

**处置**：
- `/api/trade/gates` **不输出任何 PDT 字段**；`app/web_dashboard/order/gates.py` **不 import** `pdt_guard`。
- 加**回归钉子**（防止后来者"顺手补上 PDT chip"）：`tests/web_dashboard/test_trade_ui_gates_contract.py::test_pdt_never_surfaced_in_gates` + `::test_gates_module_does_not_import_pdt_guard`。
- **附带发现（待裁，本批未动）**：上述死分支本身可按减法三步退役，但它在下单校验管线内 ⇒ 已登记进 §D「待裁」，须单独立 DEB。

### E.2 已实施清单（批 1 全 7 项 A1/A2/A3/A7/B1/B2/B3 + A6 部分）

**新增文件（2）**
- `app/web_dashboard/order/gates.py`（161 行）— `collect_manual_gates()` 只读聚合：kill switch（`emergency_interceptor.check_kill_switch`）/ 资本安全水位 + 手动预算余量（`capital_safety`）/ 日回撤熔断（共享 `DrawdownController` 实例，非 fresh）/ 单笔金额上限 / 当日手动单数 / 限价偏离软硬阈值（随服务常量下发，前端不再硬编码 5%/10%）。输出 `blocks_now[]` / `warnings_now[]`。
- `tests/web_dashboard/test_trade_ui_gates_contract.py`（291 行，17 例）。

**修改文件（6）**

| 条目 | 文件:锚点 | 做了什么 |
|---|---|---|
| **B1** 闸门可见 | `app/web_dashboard/routes/trade.py:476` | 新增 `GET /api/trade/gates`（`ds.cache` 15s，与 `/api/risk/command` 同口径；纯进程内读取，无 broker RPC） |
| | `app/web_dashboard/static/js/trade.js:1612` `loadGates` / `:1622` `renderGates` / `:756` `orderGateLines` | 摘要条下 `#gatesRow` 常驻；**正常态只一枚暗色 chip**（防噪声），有阻断/警告才逐条展开；预览弹窗内列出「本单将触发的闸门」+ 金额占上限比 + 预算余量；60s 刷新 |
| | `app/web_dashboard/templates/trade.html:31-41`（CSS）/`:343`（`#gatesRow`） | chip 三态样式（ok/warn/block）；`.gates-row:empty{display:none}` 避免空条占位 |
| **B2** 盘外偏离基准 | `trade.js:616` `_extPriceBasis` / `:708` 实时提示 / `:943` 预览弹窗 | 盘外改用 `pre_price`/`after_price`/`overnight_price` 作基准并**标注基准名**；顺带修掉「注释称跳过、代码从未跳过」的口径不一致（原拿陈旧收盘价算偏离，既假警报也假安心）。**纯提示**：后端盘外同样不拦，故不加勾选门槛、不 disable 确认键 |
| **B3-a** 平仓时段守卫 | `trade.js:1202` `hydrateCloseSessionHint` / `:1222` `fillClosePrice`；`trade.html:578` `#closeSessionHint` | 盘外打开平仓弹窗即提示「美股盘外不接受市价单，留空限价直接提交大概率被券商拒」+ 一键填入时段价（复用 B2 的 `_extPriceBasis`）；**不改提交语义**（是否放行仍由券商/后端裁决）。依据是代码推断（`executeClose` 自动置 `fill_outside_rth=true` → 后端直发 MARKET），**非实测拒单** |
| **B3-b** 卖空 + GTC 警告 | `trade.js:842-845`（预览弹窗内） | 休市自动置 GTC 且方向为卖空时，预览里直接红字「🚫 券商不接受“撤单前有效”的卖空单 — 请等盘中时段提交」。**实证**：2026-08-20 09:48 `US.BABA SHORT SELL LIMIT tif=GTC` → `撤单前有效订单不支持股票卖空。股票卖空请使用当日有效订单。`；同标的同日 17:36 改 `tif=DAY` 后 `submitted` 成功（构成对照，证明拒因就是 GTC） |
| **A1** 标的错配防护 | `hk_trade.html:223` `symbolDirty` / `:241` `normalizeHk` / `:247` `syncSymbolState` / `:526` 提交拦截 | `q-symbol` 加 `oninput`；规范化比对输入值 vs `lastQuote.symbol`，不一致 → 报价卡置灰 + 红字提示 + **提交前拦截**；`sellFromPosition`/`pickRecent` 程序改值也同步标脏（`:455`/`:481`） |
| **A2** 提交前预检条 | `hk_order_service.py:352` `get_order_limits()`；`hk_trade.py:103`（随 `/api/hk/orders/today` 下发 `limits`）；`hk_trade.html:172` `#precheck` / `:296` `renderPrecheck` | 常驻红/黄/绿 chip：`金额 HK$X · 占单笔上限 n%`、`整手 ✓/✗（每手 N 股）`、`限价偏离 x%`（含 ≥10% 硬拦截明示）、`今日手动单 n/20`。阈值全部后端下发，前端零硬编码 |
| **A3** 三态错误行 | `hk_order_service.py:367` `get_positions_with_status` / `:417` `get_today_orders_with_status`；`hk_trade.py:113`/`:122`；`hk_trade.html:600`/`:625`/`:636`/`:662` | 查询失败不再伪装成 `[]`：路由返回 **503 `BROKER_UNAVAILABLE`（retryable）**，前端渲染「⚠️ 加载失败 + 重试」而非「无港股持仓」。旧签名 `get_positions()`/`get_today_orders()` 保留为兼容包装（仍返回 list） |
| **A7** 实盘徽标 | `hk_trade.html:11`/`:14`/`:17` | 补齐 `nav_extra` / `nav_extra_tablet` / `nav_extra_mobile` 三块 → 桌面/平板/手机头部常驻 `🔴 港股实盘`（此前恒实盘直通页一个模式标识都没有） |
| **A6**（部分，原属批 2） | `hk_trade.html:276` `renderSubmitBtn` | 按钮文案动态带全要素：`买入 HK.02513 · 100 股 · 约 HK$92,400`（顺带兜住 A1）；数据不全时退回 `买入 · 提交实盘订单`。**未完成**：按钮仍恒为 `#b91c1c` 暗红，未跟随方向色（买绿/卖红）→ 留批 2 |

### E.3 关键设计决策（4 条）

1. **「只呈现不裁决」**：前端**除 A1 标的错配外一律不 `disable`、不阻断**。理由：后端 `validate_order()` / `hk_order_service.validate()` 是唯一权威；前端另立判据一旦与后端口径漂移，会把**合法单卡死**（比拒单更糟），且违反「不新增闸门」纪律。A1 是例外，因为它防的是「按错标的下单」——这是**前端独有的状态错误**，后端无从判断。
2. **fail-open 而非 fail-close**：`gates.py` 任一依赖读不到 → 该项标 `unknown` 并**不进入 `blocks_now`**。呈现层绝不能因为自己读不到数据就宣称「被阻断」（否则把依赖故障变成假拦截）——与 `scripts/trading_day_check.py` 的 fail-open 纪律同源。
3. **对旧后端优雅降级**：`limits` 缺失 → 不显示额度 chip；`/api/trade/gates` 返回非 2xx（旧进程未重启）→ `loadGates` 静默返回（`trade.js:1615`）。故「前端已生效、后端待重启」的中间态是安全的，不会白屏或误报。
4. **量纲在契约里钉死**：金额 = 绝对值（`max_order_value_usd`）、阈值 = **百分数**（`5.0` 而非 `0.05`）、次数 = 计数。测试 `test_units_are_absolute_money_and_percentage_not_fraction` 专门守这条（0.05/5.0 混用是这类前端最常见的静默错误）。

**偏离说明（与审计报告不一致处，据实登记）**
- **A7 的「banner 改 `position: sticky`」未做**：实测与 `base.html` 已有的 sticky nav 在 `top`/`z-index` 上冲突，且手机纵向空间被吃掉两行。改用**三处 `nav_extra` 实盘徽标**达成同一目标（常驻、不随滚动消失），banner 保持原位。
- **B1 的 PDT chip 未做**（§E.1，作废）。
- **A2 的「今日 n/20」数据源**：审计建议新增字段，实施选择**并入既有 `/api/hk/orders/today` 响应的 `limits`**（前端本就在轮询该端点），避免为呈现层多开一个端点、也避免多一次 broker 往返。

### E.4 验证台账

```bash
PYTHONPATH=. ./venv/bin/python -m pytest tests/web_dashboard/ -q
# → 48 passed（含新增 17 例）

PYTHONPATH=. ./venv/bin/python -m pytest tests/web_dashboard/test_trade_ui_gates_contract.py -q
# → 17 passed：PDT 不得出现 ×2 / fail-open / 量纲 / write-only 键 / HK 失败≠空 / 旧签名兼容

node --check app/web_dashboard/static/js/trade.js          # 语法通过

PYTHONPATH=. ./venv/bin/python - <<JINJA                    # 两份模板 Jinja 解析通过
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader("app/web_dashboard/templates"))
for n in ("hk_trade.html", "trade.html"): env.get_template(n); print("OK", n)
JINJA

PYTHONPATH=. ./venv/bin/python -c "from app.web_dashboard.order.gates \
    import collect_manual_gates as c; import json; print(json.dumps(c(), ensure_ascii=False, indent=1))"
```

- 上述最后一条**实测输出**（2026-09-08，真实进程内读取，非 mock）：`kill_switch_active=false`、`water_level=green`、`water_blocks_manual=false`、`manual_budget_usd=184532.82`、`drawdown_halt=false`、`max_order_value_usd=50000`、`today_manual_orders=0` / `max_daily_manual_orders=20`、`price_deviation_warn_pct=5.0` / `hard=10.0`、`blocks_now=[]`、`warnings_now=[]` —— 即「此刻无全局阻断」，前端据此只显示一枚暗色 `闸门 ✓` chip。
- FastAPI 路由内省确认 `/api/trade/gates` 已注册。
- **全量回归**：`3277 passed / 37 skipped / 1 failed`。唯一失败 `tests/test_except_pass_ratchet.py::test_except_pass_not_growing`（`955 → 959`）为**既有红，与本批无关**，已定量证明：
  - 用该测试同款正则逐文件比对工作树 vs `HEAD`：全仓**只有** `scripts/check_undefined_names.py` 计数变化（1 → 0，−1），即 **HEAD 本身 = 960 > 955，红在 HEAD 就已存在**；
  - 本批 4 个 `.py`（`order/gates.py`、`hk_order_service.py`、`routes/hk_trade.py`、`routes/trade.py`）计数**全为 0，delta = 0**；
  - 增量来自其它未提交批次（09-06/09-07 治理与尺子批）新增的 `scripts/*.py`。按纪律「存量分批清剿、只降不升」，该项应由**引入增量的那一批**处理，不在本批越权修（`AGENT.md`：不修无关红）。

### E.5 发布与回滚

- **需要重启**（改了 `.py`）：`sudo systemctl restart ftseek-us`，随后 `PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90`（AGENT.md 军规）。**未重启前**：前端已生效但 `/api/trade/gates` 返回 404、`/api/hk/*` 仍返回旧结构 —— 按 §E.3-3 优雅降级，页面照常可用，只是闸门条/额度 chip 不显示。
  → **✅ 已于 2026-09-08 15:19:25 CST 重启并验证通过**（用户裁「重启后 在继续」）：PID 519401 → 640135、冒烟 7/7、启动段零 ERROR、freshness 守卫由 `stale_drift=116356s` 转为 exit=0 静默、三个端点线上实测 200。逐项证据见 **§F.0**。
- **JS 缓存**：`/static/` 是 `max-age=2592000, immutable`（`app.py:184`），已把 `trade.html:652` 版本号 `?v=20260625b` → **`?v=20260908a`**（不 bump = 手机最长 30 天看不到新代码）。港股页 JS 内联在模板，不受影响。
- **回滚**：`git checkout -- app/web_dashboard/` + `rm app/web_dashboard/order/gates.py tests/web_dashboard/test_trade_ui_gates_contract.py`（本批全部改动集中在这 7 个文件 + 2 个新文件，无跨批耦合），然后重启。

### E.6 待办（批 2 / 批 3 / 待裁）—— 批 2、批 3 均已于同日完成，见 §F / §G

- ~~**批 2（信噪比）**~~ → **✅ 已于 2026-09-08 实施，见 §F**（A4 枚举中文化 + 状态配色 / A5 同标的当日累计上下文行 / A6 余项按钮方向色 / B4 趋势箭头降噪 / B5 横幅窗口口径 / B6 距止损独立列）。
- ~~**批 3（清洁与一致性）**~~ → **✅ 已于 2026-09-08 实施并重启生效，见 §G**（A8 卖空 uptick 提示 + 一键填卖一价 / A9 手输价按价位表吸附 / A10 `visibilitychange` 轮询守卫 / A11 撤单自绘 modal + `accinfo_query` 账户条 / A12 两页移动端堆叠 / B7 死码与死端点退役 / B8 弹窗重复 HTML 抽取 / B9 a11y / B10 三处小口子 / C2 `?v=` / C3 重启口径；C1 共享化重构刻意留待立项）。
- **待裁（须立 DEB，本批不碰）**：B11 平仓理由必填（与港股 2026-07-22「去掉理由必填」口径相反）、C4 IA 合并「交易 / 交易 HK」两个 tab、**`validator.py:89-98` PDT 死分支退役**（§E.1 附带发现）、**`dashboard.py:1111-1124` 风险九档口径**（§G.3 附带发现）、C1 共享化重构立项、`slow-data` 墓碑物理删除时机。
- **验收**：批 2/批 3 落地后用本文开头两段脚本复测拒绝率，目标港股可预防类 11 → 0、美股 9 → ≤2（需下一个交易日的真实下单样本，暂挂）。

---

## F. 批 2 实施记录（2026-09-08，批 1 重启生效后接续）

> 用户裁「重启后 在继续」⇒ 先重启验证批 1 后端（§F.0），再实施批 2（A4/A5/A6余项/B4/B5/B6）。批 2 **全部是模板 + JS + CSS**，按 `AGENT.md` 免重启，但必须 bump `?v=`（已 bump 到 `20260908b`）。

### F.0 前置：批 1 后端重启与线上实测

| 项 | 结果 |
|---|---|
| `systemctl restart ftseek-us` | PID **519401 → 640135** @ 2026-09-08 15:19:25 CST，`active/running`，`NRestarts=0` |
| `scripts/startup_smoke_check.py --wait 90` | **7/7 全过，exit=0**（ConfigValidator / 漂移检测 / 热重载 / SSOT 审计 0 BLOCKING / 模拟盘安全 / 退出快循环 / 零 Traceback·CRITICAL） |
| 启动段日志 | 290 行，`ERROR\|CRITICAL\|Traceback` = **0** |
| `scripts/deploy_freshness_check.py` | **exit=0 且静默**（重启前为 `skip: stale_drift=116356s_outside_safe_window`，newest=`order/gates.py`）⇒ 磁盘==运行态已恢复 |
| `/health` | 8901 → **200** `{"status":"ok"}`；8899 无监听（既有状态，非回归） |
| `GET /api/trade/gates` | **200**，首次 0.62s / 二次 **0.005s**（15s 缓存生效）；payload 与进程内直调逐字一致，`blocks_now=[]` |
| `GET /api/hk/orders/today` | **200**，4 条真实订单 + `limits`（`max_order_value_hkd=400000` / `today_manual_orders=4` / `max_daily_manual_orders=20` / 偏离 5.0·10.0） |
| `GET /api/hk/positions` | **200**，3 条真实持仓（HK.02513 400股 / HK.00700 200股 / HK.00100 80股） |
| `/static/js/trade.js` 响应头 | `cache-control: public, max-age=2592000, immutable` ⇒ **印证 C2 的发布陷阱**，每批改 JS 必须 bump `?v=` |

> 端点需登录（未带 cookie 返回 401 `未登录`，这本身证明路由已注册并抵达鉴权中间件）。实测用 `create_session_token()` 在本进程内签发 HMAC cookie，未改任何鉴权配置。

### F.1 已实施（6 项，全部纯呈现层）

| 条目 | 位置 | 做了什么 |
|---|---|---|
| **A4** 枚举中文化 + 状态配色 | `hk_trade.html` `HK_SIDE_CN` / `HK_OTYPE_CN` / `HK_STATUS_CN` / `hkSide()` / `hkOType()` / `hkStatus()`；订单表三格改用 | `BUY/SELL/SELL_SHORT/BUY_BACK` → 买入/卖出/卖空/买回平空；`NORMAL`→限价、`MARKET`→市价、`ENHANCED_LIMIT`→增强限价…；状态五色 pill：已报=黄 / 部分成交·撤单中=蓝 / 已成=绿 / 已撤·已删=灰 / 废单·失败·超时=红 |
| **A5** 同标的当日累计上下文 | `hk_trade.html` `#today-ctx` + `renderTodayCtx()` + `ordersCache` | 下单卡内、预检条上方常驻一行：今日各方向成交笔数/股数/VWAP/投入 + 未成交笔数 + 持仓股数/成本/现价/浮动盈亏。数据源**只有**已有的 `ordersCache`（`/api/hk/orders/today`）与 `positionsCache`（`/api/hk/positions`），不发新请求 |
| **A6 余项** 按钮跟随方向色 | `hk_trade.html` `.btn-primary.dir-buy/.dir-sell` + `renderSubmitBtn()` | 买入 → `#15803d`、卖出 → `#b91c1c`，与方向键同语义，消除「选绿按钮却按红按钮」的冲突 |
| **B4** 趋势箭头降噪 | `trade.js` `trendIcon` | 删掉零信息量的「→ 横盘」（盘中 5s 一轮导致整列持续闪动）；只在 \|Δ\|>5 时渲染 ↑/↓，并把真实比较窗口写进 `title` |
| **B5** 横幅窗口口径 | `trade.js` `_lastSnapshotAt` + `_snapshotWindowSecs()` | 文案 `15s 内变动 $X` → `近 N s 变动 $X`，N = **实测**两轮渲染间隔（盘中 5s），拿不到才退回轮询间隔。原实现把恶化速度系统性放大 3 倍 |
| **B6** 距止损独立列 | `trade.html` 表头 + `trade.js` `slCell`；`colspan` 6→7（模板 1 处 + JS 2 处） | 「距止损%」+「SL 价位」两行文本列，色阶 danger 红 / <5% 黄 / 其余灰；`atr_estimated=true` 时追加 `~` 与 title 说明。10px 色点与 `showDiag` 弹层**保留**为动作入口 |

### F.2 关键取舍（5 条）

1. **A4 fail-safe 优先于覆盖率**：未收录的枚举一律**原样透出 + 中性色**，绝不渲染成空。中文化若把券商新增状态吞成空白，等于在实盘订单表上制造信息黑洞 —— 比英文枚举更糟。已用 node 真跑 `hkStatus('SOME_NEW_STATUS')` 钉住。
2. **A4 不得污染判断逻辑**：撤单可用性 `CANCELLABLE.some(...)` 继续匹配**原始**英文枚举，不改成匹配中文文案（否则改字典就会静默改行为）。已立钉子。
3. **A5 严格「只呈现」**：不 `disable`、不拦截、不引入 `CONFIRM` 关键词（港股按设计不接系统风控；用户 2026-07-22 已裁「降低手动下单摩擦」）。标的脏（A1）或该标的无任何数据 → **整行隐藏**，不制造噪声。
4. **B6 估算值必须自我声明**：`stop_distances.atr_estimated=true` 表示 SL 是 `cost ± 4×ATR`（ATR 缺失时退化为 `cur×2%`）的**估算**，不是系统实际 SL。不标注 = 把估算当权威呈现。实测当前两条持仓（MU SHORT 6.27% / TSLA LONG 6.33%）**都是估算** ⇒ `~` 会真实出现。
5. **B4/B5 同根因，收敛到单一真源**：两者都源于「比较窗口」从未被显式化。新增 `_lastSnapshotAt` + `_snapshotWindowSecs()`，横幅与箭头共用；打点**必须在本轮渲染完成之后**（否则窗口恒为 0），已立钉子。

### F.3 验证（含真实数据实跑，非纸面推演）

- **语法/模板**：`node --check trade.js` 通过；`hk_trade.html` 的 553 行内联 JS 提取后 `node --check` 通过；两份模板 Jinja 解析通过。
- **A5 用线上真实 API 数据在 node 里跑过**：
  - `HK.02513` → 「今日买入 **2** 笔成交 / **200** 股 / 均价 **961.000** / 投入 **HK$192,200**」+「另有 **1** 笔未成交（已撤）」+「持仓 **400** 股 / 成本 1000.750 / 现价 913.500 / 浮动 **-HK$34,900（-8.72%）**」——正是审计里那个「三日 9 笔连续向下加仓」的标的，现在在点提交的位置就能看到。
  - `HK.00100` → 今日 1 笔 / 20 股 / 339 / HK$6,780 + 持仓 80 股 -7.73%。
  - `HK.00700`（今日无订单）→ **只显示持仓行**，不编造「今日 0 笔」。
  - `HK.99999`（无数据）→ **整行隐藏**。
- **线上页面已渲染新元素**（Jinja `auto_reload`，免重启即生效）：`/trade` 含 `距止损/SL` + `colspan="7"` + `?v=20260908b`；`/trade/hk` 含 `#today-ctx` + 三张枚举字典 + `dir-buy` + `st-bad`；`/static/js/trade.js?v=20260908b` 含 `_snapshotWindowSecs`/`slCell`/`trendWin`。
- **测试**：`tests/web_dashboard/` **59 passed**（批1 的 48 + 批2 新增 11），并于双方编辑落定后（15:47）单独重跑确认。
- **⚠️ 并发写入者（读数归因）**：执行期间另有一个 Codex 会话（PID 627828，13:47 启动）在并发编辑本仓，15:28–15:32 改了 `scripts/llm_universe_scan.py` 与 `app/playbook/{admission,db,playbook_service}.py`（**注释-only**，内容经核属实：`config/unified_config.yaml:1404-1406` = `enabled/enforce: true`、`min_notional_usd: 2500`）等。三条影响：①**批1 的重启（15:19:25）早于它 15:30:39–41 的 `app/` 编辑 ⇒ 未加载其半成品**（实测该窗口内 `app/`+`main.py` 只有本批 `order/gates.py`）；②`deploy_freshness_check.py` 现报的 `stale_drift=675s | newest=app/playbook/playbook_service.py` **是它的**，批2 只改模板/JS/CSS 而该守卫是 `rglob("*.py")`（`scripts/deploy_freshness_check.py:140`）⇒ **批2 的 freshness delta = 0**；③全量回归窗口与其编辑窗口重叠，故 `3304 passed` 不是对固定树的严格陈述（`tests/web_dashboard/` 59 passed 已单独重跑确认）。
- **全量回归**：**1 failed / 3304 passed / 41 skipped**（150s，`--ignore=tests/archive -p no:randomly`）。唯一失败 = `tests/test_except_pass_ratchet.py`（`955 → 959`），**既有红**，批2 后**再次定量复核**：用该守卫同款正则逐文件比对工作树 vs `HEAD` ⇒ 全仓只有 `scripts/check_undefined_names.py` 变化（1→0）⇒ **HEAD 本身 = 960 > 955**；`app/web_dashboard/` 计数全 0；批2 唯一新增 `.py` 是测试文件，而 `tests` 在该守卫 `_EXCLUDE_PARTS` 排除名单内 ⇒ **本批 delta = 0**（实测 959 与批2 前逐字相同）。按「不修无关红」纪律不越权处理，增量应由引入它的 09-06/09-07 批次清剿。

### F.4 批 2 钉子（`tests/web_dashboard/test_trade_ui_batch2_contract.py`，11 例）

前端改动没有后端判据可测，但有四种**静默坏法**（页面不报错、功能"看起来还能用"），故逐种立钉：

1. **表格列数错位**（B6 加列的经典坑）：`thead` 的 `<th>` 数必须 == 空态/错误态 `colspan`；`trade.js` 不得残留 `colspan="6"`；港股两表钉死 10 列（A4 只改文案不得改列数）。
2. **枚举中文化丢信息**：用 node **真跑**映射函数 —— 已知枚举出中文、未收录枚举必须原样透出且非空、五族状态配色必须互不相同、撤单判断必须仍用原始枚举。
3. **文案与实际口径不符**：不得残留 `盈亏快速恶化，15s`；必须用 `winSecs`；`_lastSnapshotAt` 打点必须在 `updateAttentionBanner` 之后；`title="横盘"` 与 `→</span>` 必须消失，但 ↑/↓ 不得被一并删掉。
4. **缓存版本号丢失**：script 标签必须带 `?v=YYYYMMDD+字母` 且 `>= 20260908b`（`/static/` 是 30 天 immutable，漏 bump = 手机最长 30 天看不到新代码，且历史上极可能被误判为「修复无效」）。

另加一条**纪律钉子**：`submitOrder()` 内不得引用 `today-ctx`/`renderTodayCtx`、不得出现 `CONFIRM`，前端唯一阻断仍只有 A1 的 `symbolDirty`。

### F.5 待办（批 3 / 待裁）—— 批 3 已于同日完成，见 §G

- **✅ 批 3（清洁与一致性）已完成**：A8 卖空 uptick + 一键填卖一价、A9 手输价按价位表吸附、A10 `visibilitychange` 轮询守卫、A11 撤单改自绘 modal + 补 `accinfo_query` 账户条、A12 移动端堆叠视图、B7 死码/死端点退役、B8 弹窗重复 HTML 抽取、B9 a11y、B10 三处小口子、C2 `?v=` bump、C3 重启口径 —— 逐项落地位置与验证见 **§G**。
- **🔒 待裁（须立 DEB）**：B11 平仓理由必填、C4 IA 合并两个 tab、`validator.py:89-98` PDT 死分支退役、`dashboard.py:1111-1124` 风险九档口径（§G.3 附带发现）、C1 共享化重构立项、`slow-data` 墓碑物理删除时机。
- **验收**：批 3 落地后用本文开头两段脚本复测拒绝率，目标港股可预防类 11 → 0、美股 9 → ≤2（需下一个交易日的真实下单样本，暂挂）。

---

## G. 批 3 实施记录（2026-09-08，批 2 之后接续；含上线复核抓到的一处假警报）

> 用户裁「继续 完成后再重启」⇒ 先实施批 3（A8–A12 / B7–B10 / C2、C3），其中 `.py` 改动按 `AGENT.md` 军规重启 + 跑启动冒烟（§G.0），重启后做线上实测复核 —— **复核中抓到本批自己引入的一处假警报并当场修正**（§G.3）。前端部分仍是模板 + JS + CSS，`?v=` 已 bump 到 `20260908c`。C1（共享化重构）**本批不做**，理由见 §G.2 第 7 条。

### G.0 重启与线上实测（批 3 含 `.py` 改动 ⇒ 必须重启）

| 项 | 结果 |
|---|---|
| `systemctl restart ftseek-us` | PID **640135 → 651770** @ 2026-09-08 16:55:10 CST，`active/running`，`NRestarts=0`（修正假警报后复核仍是同一 PID ⇒ 模板改动确实免重启） |
| `scripts/startup_smoke_check.py --wait 90` | **7/7 全过，exit=0** |
| 启动段日志 | `ERROR\|CRITICAL\|Traceback` = **0** |
| `scripts/deploy_freshness_check.py` | **exit=0 且静默** ⇒ 磁盘 == 运行态 |
| `/health` | **200** |
| `GET /api/trade/slow-data`（B7 墓碑） | **410** + `{"ok":false,"error":{"code":"ENDPOINT_RETIRED","message":"/api/trade/slow-data 已于 2026-09-08 退役 (前端零消费)..."}}`；不带 cookie 是 401（鉴权在路由之前，既有行为） |
| `GET /api/hk/account`（A11 新增） | **200**，真实资金：现金 **177,233** / 购买力 **555,486** / `risk_level="N/A"` / `risk_status="LEVEL3"`（17:1x 复核：现金 177,219 / 购买力 555,229，随行市微动） |
| `GET /api/hk/positions` / `orders/today` / `api/trade/gates` | 均 **200** |

### G.1 已实施（11 项 + 1 项上线修正）

| 条目 | 位置 | 做了什么 |
|---|---|---|
| **A8** 卖空 up-tick 前置提示 | `hk_trade.html` `renderPrecheck()` | 券商必拒的规则前置到提交前：「港股卖空须 ≥ 卖一价（up-tick）」+ 当前卖一价 + **一键「填卖一价」**；只提示不阻断 |
| **A9** 手输价按价位表吸附 | `hk_trade.html` `hkTick()` / `tickOffset()` / `snapPriceToTick()`（`onblur`） | 手输价失焦即按港交所价位表吸附；**用整数刻度比较**而非 `price % tick`（`0.7 % 0.1 = 0.0999…` 会把合法价判成非法）；已在表上 ⇒ 静默不打扰 |
| **A10** 可见性守卫 | `hk_trade.html` `applyPollingState()` + `visibilitychange` | 后台标签页停掉 `dataTimer`/`sessionTimer`，回到前台再续；收盘降频 ×2（`hkPollMs`）统一走同一入口 |
| **A11** 撤单自绘 modal | `hk_trade.html` `openHkModal/closeHkModal/confirmHkModal` + `#hk-modal` | 取代 `window.confirm`/`alert`（手机样式不可控、阻塞主线程、只显示 order_id）；撤单前可核对标的/方向/类型/数量/已成交/价格/状态，**确认摩擦不降低** |
| **A11** 账户资金条（实盘直通页首次可见资金） | `hk_order_service.get_account_info_with_status()`（新）+ `hk_trade.py GET /api/hk/account`（新）+ `hk_trade.html` `loadAccount()`/`renderAccountBar()` | 本页是实盘直通却全文无 `accinfo_query`，下单前看不到可用资金/购买力只能开富途 App 对照。三态：失败 ⇒ 503 `BROKER_UNAVAILABLE`(retryable) + 前端「加载失败 + 重试」，**绝不显示成「资金 0」**；只下发前端真会渲染的 8+1 键，`currency` 由券商回报不假设 HKD |
| **A11 修正**（上线复核抓到） | `hk_trade.html` `HK_RISK_LEVEL_CN` / `HK_RISK_STATUS_CN` / `hkRiskBadge()` + `.acct-risk.ok/.warn/.bad` | 风险档位配色改按富途官方枚举分级，修掉「LEVEL3 较安全被染红」的每次打开必现假警报（详见 §G.3） |
| **A12** 移动端堆叠（港股） | `hk_trade.html` `.hk-stack` + `td[data-label]`（21 处） | 10 列表格从横向滚动改为窄屏卡片堆叠；`data-label` 与 `thead` 逐列对齐（立钉子） |
| **A12-US** 移动端堆叠（美股） | `trade.html` `.pos-stack` CSS + `trade.js` `data-label`（23 处）；audit 行 | 持仓表（B6 加列后 7 列）与审计表同套方案，两页共用心智模型；**audit 行「价格」列此前塞的是 `order_type`（表头说价格、内容写 MARKET）⇒ 改为真价格** |
| **B7** 死码退役 | `trade.html`（`.dq-*` 28 行 + `.cap-grid/.cap-item` + `.pos-cards-mobile/.pos-card-m*`）、`trade.js`（每轮 `console.log('[snapshot]')`）、`trade.py`（snapshot `logger.info`→`debug`、删函数体内死 `import json`、`GET /api/trade/slow-data` 约 210 行聚合 → **410 墓碑**） | 盘中 5s 一轮 = 前后端每分钟 24 行噪声日志，清零；死端点走减法三步的「停」阶段（见 §G.2 第 1 条） |
| **B8** 偏离警告单实现 | `trade.js` `_renderPriceWarn()` / `_bindPriceWarn()` / `_priceDevScenario()` + `trade.html` `.price-warn.{hard,soft,gated,ext}` / `.pw-scenario` / `.pw-note` | 硬（≥10%）软（≥5%）两段 ~60 行重复内联 HTML + 内联 `onchange` 字符串收敛成一个函数，样式走 class；**阈值改读 `/api/trade/gates`**（与后端 validator 同一组服务常量），gates 缺失 ⇒ fail-open 回落 5/10；`window._priceWarnAck` → `acknowledge_price_warning` 与 `validator.py:229` 的放行契约逐字保持 |
| **B9** a11y | `trade.html:354-364` 4 个 `<div onclick>` → `<button type="button" data-dir/data-op>` | 做多/做空、开仓/平仓可聚焦可键盘操作，与港股页同一控件语义；`active` 态复位随 `setDir/setOp` |
| **B10** 三处小口子 | `trade.js` `us_recent_symbols` chips（5 枚）/ `fastTick` 具名函数 / `quoteMatches` | ①美股页此前零 localStorage，最近标的 chips 补齐（**只存代码**，不存价格/股数/方向，点击走与手输同一条 `onSymbolChanged()` 路径）；②`arguments.callee` 重挂定时器（严格模式直接抛 `TypeError`）改具名 `fastTick`；③`curQuote.symbol` 与输入标的比对，不一致 ⇒ 预估金额与偏离检查按「无报价」处理**并在预览里明说「已跳过」**（静默跳过会让人以为检查过了） |
| **C2 / C3** | `trade.html:645` `?v=20260908b → 20260908c`；`.py` 改动重启 + 冒烟 | `/static/` 是 `max-age=2592000, immutable`，漏 bump = 手机最长 30 天看不到新代码 |

### G.2 关键取舍（7 条）

1. **B7 死端点走 410 墓碑，不直接删路由**：仓外 3 份 Hermes skill 文档仍按旧契约描述 `/api/trade/slow-data`，删成 404 与「路径打错」不可区分；410 + `ENDPOINT_RETIRED` + `logger.warning` 记调用方 ⇒ 先看清还有谁在打，再决定物理删除时机（减法三步的「停」，不是「删」）。
2. **B8 阈值单一真源**：前端不再各写一份 5/10，改读 `/api/trade/gates`（`manual_order_service.py:73-74` 的服务常量）。gates 未加载 ⇒ **fail-open 回落同值 5/10**，绝不让「取不到阈值」变成「不做偏离检查」。
3. **B8 全局态不是脏代码**：`acknowledge_price_warning` 是与后端 `validator.py:229` 的软拦截放行契约，重构内联 HTML 时最容易被当「脏全局变量」顺手清掉 ⇒ 单独立钉子。
4. **A8/A9 只呈现不阻断**：up-tick 与价位吸附都走 `renderPrecheck` 提示 + 一键修正；港股页唯一前端阻断仍只有 A1 `symbolDirty`（用户 2026-07-22 已裁「降低手动下单摩擦」，港股按设计不接系统风控）。
5. **A11 三态优先于好看**：资金查询失败必须明确报错，显示成「资金 0」会让人误以为没钱而放弃合法下单 —— 比没有账户条更糟。且异常时**不**把 `_connected` 置 False（资金查询失败 ≠ 连接断了，置 False 会引发无谓重连风暴）。
6. **A12 复用 `td[data-label]` 而非新增移动端 DOM**：一套列名两种呈现，但 `data-label` 是**第二套列名**，加列/改名时极易只改一边 ⇒ 钉死「data-label 序列 == thead `<th>` 文本序列」（两页四张表全钉，含空标签列）。
7. **C1（抽 `hk_trade.js` + 共享 `trade-ui.css`）本批不做**：那是跨两页的结构性搬迁，与「清洁与一致性」混在一个 diff 里会让改动不可审、回滚不可分。批 3 已把两页的**口径**对齐（三态、枚举中文化、堆叠、modal、chips、守卫），共享化留待单独立项。

### G.3 验证（含线上复核抓到的一处假警报）

- **语法/模板**：`node --check trade.js` 通过；`hk_trade.html` 内联 JS 提取后 `node --check` 通过；两份模板 Jinja 解析通过。
- **⚠️ 上线复核抓到本批自造的假警报（已当场修正）**：A11 账户条首版用 `riskBad = a.risk_status && !/SAFE|NORMAL|OK/i.test(a.risk_status)` 决定染色。但富途 `risk_status` 是 **CltRiskStatus 九档** `LEVEL1`（非常安全）⇒ `LEVEL9`（危险），字面永不含 "SAFE" ⇒ 实盘常态 `LEVEL3`「较安全」**每次打开页面都被染红**。红色天天误报 = 真危险（LEVEL8/9）来临时不再是信号，与本审计的信噪比主题正面冲突（这是「重构/新增功能时凭直觉猜券商枚举语义」的典型坏法，故已入钉子第 5 类）。
  - 修法（查富途 OpenAPI 官方枚举，非猜测）：`CltRiskLevel` → SAFE/ABSOLUTE_SAFE=安全、WARNING=预警、DANGER/OPT_DANGER=危险；`CltRiskStatus` → LEVEL1-3 安全档（非常安全/安全/较安全）、LEVEL4-6 隐患档（较低风险/中等风险/偏高风险）、LEVEL7-9 危险档（预警/危险/危险）。两字段**取较严重的一档**；已知值给中文标签 + 档位色，**未登记档位与 `N/A`/`NONE`/空一律中性灰 + 原样透出**（未知 ≠ 危险，A4 同款纪律），两档皆未知则不出芯片（没信息不占注意力）；原始档位留在 `title` 里以便与富途 App 对照。
  - 复核：用**线上真实 payload**（`risk_level="N/A"` / `risk_status="LEVEL3"`）在 node 里跑 `hkRiskBadge` ⇒ `{"sev":"ok","text":"风险 较安全"}`；`{SAFE, LEVEL9}` ⇒ `bad`（取严）、`{WARNING, NONE}` ⇒ `warn`、`{N/A, DANGER}`（错档字段）⇒ 中性透出原文、`{N/A, NONE}`/`null` ⇒ 不出芯片。九档 sev 序列 `ok×3 / warn×3 / bad×3` 单调性已钉死（反序必须炸）。模板改动免重启，线上页面已含 `hkRiskBadge` + `.acct-risk.ok/.warn`。
- **顺带发现（未动，属主看板不属交易页）**：`app/web_dashboard/routes/dashboard.py:1111-1124` `_parse_risk_level` 把 `LEVEL1-5` 当成 安全/注意/警告/危险/强平（`LEVEL2=注意`、`LEVEL3=警告 接近维持保证金`、`LEVEL5=强平`）—— 与富途九档口径不符（LEVEL3 实为「较安全」、LEVEL5 实为「中等风险」），会让主看板/风险页对常态账户给出偏严的橙/红警告，与本批修掉的是**同族假警报**。因涉及另一页的图例与 `risk_levels` 字典消费方，按「不修无关红」纪律**未越权改动**，列入 §G.5 待裁。
- **测试**：`tests/web_dashboard/` **86 passed**（批1 48 + 批2 11 + 批3 27）；批 3 钉子文件单跑 **27 passed**。全量回归见 §G.3 末条。
- **并发写入者（读数归因）**：本批期间另一 Codex 会话修好了 `scripts/check_undefined_names.py`，此前既有红的 `tests/test_except_pass_ratchet.py` 因此转为通过 —— 全量 0 failed 里这一条**不是本批的功劳**，归因需分清。

### G.4 批 3 钉子（`tests/web_dashboard/test_trade_ui_batch3_contract.py`，27 例）

批 3 的坏法比批 1/批 2 更「安静」（页面不报错、功能看起来还能用），故按**五类静默坏法**立钉：

1. **删除过头 / 删除不足**（B7）：墓碑里不得残留旧聚合逻辑（半死实现 = 下次有人以为它活着），路由也不得整个删掉（仓外 skill 文档仍按旧契约描述 ⇒ 必须 410 + `ENDPOINT_RETIRED` + warning）；死 CSS 必须真消失。
2. **重构改变行为**（B8）：两段内联 HTML 收敛成一个函数后，四条场景化文案与后端 validator **逐字同构**（node 真跑比对）、阈值必须来自 `_gates` 而非硬编码、`acknowledge_price_warning` 放行契约必须还在。
3. **表头与单元格漂移**（A12 / A12-US）：两页四张表的 `data-label` 序列 == `thead <th>` 文本序列（含空标签列）；audit 行「价格」列必须是真价格而非 `order_type`。
4. **防护被「优化」掉**（B10 / A9 / A10）：`curQuote` 标的校验、在途响应过期丢弃、可见性守卫、`arguments.callee` 替换必须都在，且**不得新增前端阻断**（`submitOrder()` 里唯一阻断仍只有 `symbolDirty`；价位/卖空提示不得进提交路径）。
5. **配色口径错 = 假警报**（A11 修正）：富途风险九档逐档钉死 + 单调性；旧正则口径必须删净（注释里引用旧实现是文档，只有可执行代码里的复活才算坏）；三色 CSS 必须在（否则染档失效变成无样式裸文本）。

### G.5 待办 / 待裁（批 3 后）—— 其中 B11 / PDT / 九档口径 / C1 / C4 / 墓碑删除已于同日由**批 4** 处理（用户裁「理由非必填，其他按合理性修复更正」），逐项结果见 §H

- **验收（暂挂）**：用本文开头两段脚本复测拒绝率，目标港股可预防类 11 → 0、美股 9 → ≤2；需下一个交易日的真实下单样本，并对比 `hk_manual_orders.jsonl` 中 `status != submitted` 占比。
- **🔒 待裁（须立 DEB）**：
  - ~~**B11** 平仓理由必填且纯手打~~ → **✅ 批 4 已落地**（用户裁「非必填」）：删拦截 + 4 枚 chips + 可选文本 + 后端兜底「手动平仓」，见 §H.1。
  - **C4** IA 合并：「交易」与「交易 HK」两个 tab 合一 + 市场段控件 → **🔒 批 4 代裁延后**（以 C1 为前置；依据与重开条件见 §H.4）。
  - ~~**`validator.py:89-98` PDT 死分支退役**~~ → **✅ 批 4 已退役**（`pdt_guard.py` 本体保留，前提 `enabled=false` 已立钉子），见 §H.1；自动管线同款调用点 `pre_trade_check.py:425` 仍登记待裁（§H.5）。
  - ~~**`dashboard.py:1111-1124` `_parse_risk_level` 九档口径更正**~~ → **✅ 批 4 已更正并线上实证**（同一账户 LEVEL3：警告/orange → 较安全/green；且修掉 LEVEL6-9 落 gray 的**漏报**），见 §H.1 / §H.0。
  - **C1 共享化重构立项**（抽 `hk_trade.js` + 共享 `trade-ui.css` + HK/US 各一份枚举字典）→ **🔒 批 4 代裁延后**（批3/批4 已对齐两页口径，剩余收益属维护性；依据见 §H.4）。
  - **`/api/trade/slow-data` 墓碑物理删除时机** → **批 4 已完成前置**（仓外 3 份现役 skill 文档已标注 410 退役，见 §H.1）；墓碑本体**暂不删**，删除条件 = 连续 ≥2 周 410 warning 零命中（§H.5）。

---

## H. 批 4 实施记录（2026-09-08，用户裁「理由非必填，其他按合理性修复更正」）

> 用户裁「仓非必填 其他都按照合理性进行修复和更正」⇒ ①**B11 平仓理由改非必填**（用户明裁）；②其余待裁项由我**代裁**并按合理性落地或延后（做/不做的判据见 §H.2 第 4-5 条与 §H.4）。本批**动了后端 `.py`**（`validator.py` / `dashboard.py` / `routes/trade.py` / `manual_order_service.py`）⇒ 已按 `AGENT.md` 军规重启 + 跑冒烟（§H.0）。代裁过程中在同一条平仓链路上挖出一处**潜伏 3 个月的静默断链**并一并根治（§H.1 第 2 行 / §H.2 第 6 条）。

### H.0 重启与线上实测

| 项 | 结果 |
|---|---|
| 重启前并发写入者核查 | `find app main.py -name "*.py" -mmin -90` ⇒ 只有本批 4 个文件（18:23–18:26）⇒ **未加载他人在途代码** |
| `systemctl restart ftseek-us` | PID **651770 → 659912** @ 2026-09-08 18:33:18 CST，`active/running`，`NRestarts=0` |
| `scripts/startup_smoke_check.py --wait 90` | **7/7 全过，exit=0** |
| 启动段日志 | 5 行，`ERROR\|CRITICAL\|Traceback` = **0** |
| `scripts/deploy_freshness_check.py` | **exit=0 且静默** ⇒ 磁盘 == 运行态 |
| `/health` | **200** |
| `GET /api/risk/status` → `futu_risk` | **口径更正实证**：同一账户、同一 `risk_status="LEVEL3"`，由 `{"label":"警告","color":"orange","desc":"接近维持保证金"}` 变为 **`{"label":"较安全","color":"green","desc":"有杠杆但流动性充裕，无需动作"}`** |
| `GET /api/trade/gates` | **200**，payload 无任何 PDT 键（`kill_switch/water_level/manual_budget/drawdown/max_order_value/today_manual_orders/price_deviation_*`），`blocks_now=[]` |
| `/trade` + `/static/js/trade.js?v=20260908d` | **200**；页面含 `#closeReasonChips` + 「理由（可选）」 + `?v=20260908d`；JS 含 `CLOSE_REASONS` / `bindCloseReasonChips` |
| `GET /api/trade/slow-data` / `GET /api/hk/account` | 仍 **410**（批3 墓碑未受影响）/ 仍 **200**（批3 账户条未受影响） |

### H.1 已实施（用户明裁 1 项 + 代裁 3 项 + 附带根治 1 项 + 发布口径 1 项）

| 条目 | 位置 | 做了什么 |
|---|---|---|
| **B11** 平仓理由**非必填**（用户明裁） | `trade.js` `executeClose()` / `CLOSE_REASONS` / `renderCloseReasonChips()` / `bindCloseReasonChips()`；`trade.html` 「理由（可选）」+ `#closeReasonChips` + `.rec-chip.on`；`routes/trade.py:112` reason 兜底 | 删掉「请填写理由」这道拦截（平仓是**最紧急动作**，且与港股 2026-07-22「去掉理由必填」口径相反）；改 4 枚一键 chips（止损触发 / 止盈达成 / 计划变更 / 风控降敞口）+ 可选补充文本；留空 ⇒ 后端落「手动平仓」。SL/TP 入口预选「止损触发」（仍可改可清空）。**数量校验等既有防线逐字未动**（去阻断 ≠ 去校验） |
| **附带根治**：手动平仓账本**静默断链** | `manual_order_service.py:425` `_record_manual_close` 委派签名 | `order/executor.py:711` 自 **2026-06-19**（`768e443f`）起就传 `entry_reason=`，而委派层一直没这个形参 ⇒ 每次手动平仓在此抛 `TypeError`，被 `executor.py:784` 的 `except Exception` 降级成一行 warning「trade_records 写入失败 (不影响平仓)」⇒ **券商侧已平仓、账本侧零记录**（用户还看到「平仓成功」）。实证：`trade_records` 中 `exit_reason LIKE '[%'` = **0 行**，最后一笔手动平仓留痕停在 **2026-06-09**（= 该 commit 之前）。修法 = 委派补 `entry_reason: str = ""` 并**真的往下传**；下游 `audit.record_manual_close` 早就收这个参数，只是门面没跟着改 |
| **PDT 死分支退役**（代裁，依用户 2026-08-02 / 2026-09-08 两次裁决） | `validator.py` 第 6 段 + docstring 检查清单 | 证据链：`risk.pdt_guard.enabled: false`（`config/unified_config.yaml:1646`）⇒ `PDTGuard.check_trade_permission()` 首行即 `return True, "PDT 守卫已关闭…", SAFE`（`app/risk/pdt_guard.py:350`）⇒ `if not _pdt_ok` **自 2026-08-02 起永不成立** = 恒放行死分支（只贡献一次 import + 对象构造，和一处「看起来有 PDT 防护」的错觉）。删调用点、**保留 `pdt_guard.py` 本体**（moomoo US 实体适用 PDT，届时 `enabled` 改回 true 并接回调用点）；同时更正两处过期注释/docstring（原写「仅保留 PDT + 仓位 + 回撤」——仓位集中度 2026-06-26 已移除、PDT 现退役 ⇒ 注释与代码漂移两处） |
| **风险九档口径更正**（代裁，§G.3 附带发现） | `dashboard.py:1111` `_parse_risk_level` | 原表把 `LEVEL1-5` 当成 安全/注意/警告/危险/强平，与富途官方 `CltRiskStatus` 九档**相反**（LEVEL1 最安全 ⇒ LEVEL9 最危险）。后果**两头都错**：①常态误报——线上实测健康账户 `LEVEL3` → 「警告 / orange / 接近维持保证金」；②真危险漏报——`LEVEL6-9`（偏高风险/预警/危险/危险）全落 default gray「未知状态」= 最该报警的四档被渲染成中性灰。新表逐档用官方中文标签，色阶单调（1-3 green / 4-5 yellow / 6 orange / 7-9 red），未知（`N/A`/`NONE`/新档位）gray + 原样透出；`desc` 不再编造保证金阈值（富途文档只给档位名与「LEVEL1 最安全 / LEVEL9 最危险」，原「接近维持保证金」无出处）。与港股页 `hkRiskBadge()` **逐档同标签、同严重度带**（立钉子防两页各说各话） |
| **`slow-data` 墓碑的仓外文档更正**（代裁） | `/root/.hermes/skills/devops/ftseek-dashboard/` 的 `SKILL.md:839` / `references/decision-quality-panel.md:3` / `references/frontend-contract-pitfalls.md:38,82` | 3 份**现役契约文档**仍把 `/api/trade/slow-data` 描述成可用端点（含 CSS 类与数据流图）⇒ 标注 410 退役 + 替代数据源（`trade_records.metadata_json` / `/api/trade/review` / `/api/analytics/*`）。两份**带日期的审计快照**（`dashboard-api-audit-20260614` / `dashboard-data-overlap-20260615` / `screening-to-execution-chain-audit-20260706`）按时点取证**保留不改** |
| **C2** 发布口径 | `trade.html:652` | `?v=20260908c → 20260908d`（改了 `trade.js`；`/static/` 是 `max-age=2592000, immutable`） |

### H.2 关键取舍（7 条，含代裁判据）

1. **非必填 ≠ 不留痕**：前端可以送空串，但后端必须兜底。注意 `body.get("reason", "手动平仓")` 只在键**缺失**时兜底，前端送 `reason: ""` 会穿透成 `[平仓] `（空留痕）⇒ 改 `str(...) or "").strip() or "手动平仓"`。这是「去摩擦」与「不失真」的接缝，已立钉子。
2. **chips 只给 4 枚**：建议里的第 5 枚「其它」与下面的自由文本框**完全重复** = 纯噪声（本审计的主题就是删这种东西）。文本框 placeholder 直接写「留空 = 手动平仓；其它理由可手填」，兜底口径不靠用户猜。
3. **单一真源**：chips 点击只写 `closeReasonInput.value`，`executeClose()` 仍只读 input ⇒ 提交路径逐字不变，也不会出现「清空输入框却送出陈旧 chip 值」的第二套真源。已立钉子（`CLOSE_REASONS` 不得出现在 `executeClose` 内）。
4. **代裁「做」的判据**（三条同时满足才动）：①有官方文档或线上实测证据支撑（富途九档枚举、`enabled=false` 的实际返回）；②改动局限在**呈现/只读层**，不碰判据、阈值、闸门与退出参数（C1/C7 冻结不受影响）；③可立钉子、可单命令回滚。PDT 退役、九档更正、理由非必填、断链修复全部满足。
5. **代裁「不做」的判据**：结构性搬迁（跨页/跨层）、需重设计视觉语义、或收益属维护性而非信噪比 ⇒ 延后并写明重开条件（C1/C4 见 §H.4）。
6. **断链修复不是「顺手改无关 bug」**：它就在 B11 的**同一条链路**上（`executeClose` → `POST /api/orders/close` → `executor.close_position` → `_record_manual_close` → `trade_records`），而 B11 改的正是这条链路的留痕口径。修完「理由留痕」却留着「整条记录写不进去」的断链 = 把水倒进漏桶。
7. **签名漂移必须泛化立钉**：只钉 `_record_manual_close` 一处，下次别的委派照样漂（这类坏法肉眼审 diff 抓不到——调用点与委派各自都「看着对」）。故钉子遍历 `order/*.py` 全部 `service._xxx(**kwargs)` 调用点 × `inspect.signature(...).bind_partial()` 对表，并断言**必须覆盖到出事那个委派**（防钉子空转）。

### H.3 验证

- **语法/静态**：`node --check trade.js` 通过；`_parse_risk_level` 九档 + 大小写（`level3`）+ 未知（`N/A`/`NONE`/`LEVEL12`/`""`）+ 空账户 + `None` 账户逐个实跑；四个改动 `.py` `ast.parse` 通过；`pyflakes` 新增告警 **0**（报出的 `datetime`/`sqlite3`/`JSONResponse`/`os as _os` unused 均为既有，按「不修无关红」未动）；`cur_price` 在 PDT 段删除后仍被 3 处消费（未产生死变量）。
- **线上实测（重启后）**：见 §H.0 表格 —— 关键是 `futu_risk` 对**同一个真实账户**的前后对照（警告/orange → 较安全/green），以及 gates payload 无 PDT 键。
- **未做实盘下单/平仓验证**：不会为了验证 UI 而动真实持仓。平仓链路的行为等价性由「钉子 + 签名对表 + 后端兜底表达式」覆盖；`_record_manual_close` 的**实际写库**需下一笔真实手动平仓才能实证 ⇒ 已登记为观察点并给出复核 SQL（§H.5）。
- **测试**：`tests/web_dashboard/` **98 passed**（批1 48 + 批2 11 + 批3 27 + 批4 **12**）；**全量 3344 passed / 41 skipped / 0 failed**（152s，`--ignore=tests/archive -p no:randomly`；较批3 的 3332 正好 +12 = 本批新增钉子）。

### H.4 批 4 钉子（`tests/web_dashboard/test_trade_ui_batch4_contract.py`，12 例）

四类静默坏法：

1. **去阻断顺手去掉别的防线**（B11）：「请填写理由」必须删净（去注释后查，注释里引用作文案取证不算复活）；数量校验必须在；后端必须 `or` 兜底而非键缺失兜底；chips 不得触碰 `disabled`/`executeClose`/`errEl`/`showResult`；提交仍只读 input。
2. **门面签名漂移**（断链）：委派必须收 `entry_reason` **且真的往下传**（收了不传 = 另一种断链）；下游必须真消费；再泛化到 `order/*.py` 全部 `service._xxx(**kwargs)` 调用点与委派签名逐一对表。
3. **退役的前提没被钉住**（PDT）：validator 代码体（AST 标识符级，忽略注释/docstring/字符串）零 PDT；退役必须留痕（`PDT Guard 已退役` + 证据链）；**`pdt_guard.enabled` 必须仍为 false** —— 若哪天翻回 true，这条钉子会炸并在消息里写明「必须同时把调用点接回 `validate_order()`」；`pdt_guard.py` 本体必须还在（退役的是调用点，不是能力）。
4. **跨页词表漂移**（九档）：看板必须完整九档 + 官方标签逐字 + 色阶单调（green<yellow<orange<red）+ 旧错表（`"强平"`/`"注意"`/`"接近维持保证金"`）在字典字面量里删净；两页**逐档同标签同严重度带**；未知一律 gray + 原样透出；并用线上真实档位（`LEVEL3`）钉一条实测锚。

### H.5 待办 / 观察点（批 4 后）

- **观察点（下一笔真实手动平仓后复核）**：`SELECT trade_date, symbol, exit_reason, realized_pnl, entry_reason FROM trade_records WHERE exit_reason LIKE '[%' ORDER BY id DESC LIMIT 5` —— 应出现 `[平仓] 手动平仓` 或 `[平仓] 止损触发` 行且 `realized_pnl != 0`；若仍 0 行，查日志 `[手动平仓] trade_records 写入失败` 的具体异常。
- **同族未动（已登记，属自动管线）**：`app/risk/pre_trade_check.py:425` 是自动管线里的同款 PDT 恒放行调用点 —— 属退出参数冻结期（判据字典 C1/C7）的交易管线，不在本呈现层批授权范围；重开条件与 §H.1 一致（若 PDT 开关翻转，两处一起接回）。
- **口径观察（不动，需单独裁）**：手动平仓写 `exit_reason = "[平仓] <理由>"`（自由文本），自动链路写枚举（`EXTERNAL_CLOSE` / `TRAILING_STOP` / …）⇒ **同一列两种口径**。统一它会改变 `trade_analyzer` / OKR 读数的前置分布，属判据层，不在呈现层批内动。
- **🔒 仍待裁 / 延后**：**C1** 共享化重构（延后，单独立项）、**C4** IA 合并两个 tab（延后，以 C1 为前置）、**`slow-data` 墓碑物理删除**（暂不删；删除条件 = 连续 ≥2 周 410 warning 零命中，仓外文档已于本批标注）。依据见下。
- **验收（暂挂）**：拒绝率复测目标不变（港股可预防类 11 → 0、美股 9 → ≤2），需下一个交易日真实下单样本。

**C1 / C4 延后依据（代裁，写明重开条件）**

- **C1（抽 `hk_trade.js` + 共享 `trade-ui.css` + HK/US 各一份枚举字典）— 延后**：①跨两页结构性搬迁（`trade.js` 1900+ 行、港股内联 1000+ 行），与呈现层批混在一个 diff 里不可审、不可分段回滚；②批3/批4 已把两页**口径**对齐（三态、枚举中文化、堆叠、modal、chips、守卫、风险词表），共享化的收益已从「修口径不一致」降级为「减少重复代码」= 维护性收益，不该占交易页的变更预算；③它是 C4 的前置（先共享再合并入口，顺序反了要做两遍）。**重开条件**：单独立项、独立 diff、独立回滚点。
- **C4（「交易」与「交易 HK」合并为单一入口 + 市场段控件）— 延后**：①合并后港股「物理隔离」需在前端重新表达（隔离本体在服务层不受影响，但视觉分隔是设计题不是搬运算）；②两页各自独立的轮询/守卫/枚举逻辑若未先共享，合并入口会让首屏同时加载两套逻辑或引入动态加载分支 = 用新复杂度换一个 tab；③收益（省一个 tab）< 代价（结构性改动 + 隔离表达重设计）。**重开条件**：C1 落地后再评。
