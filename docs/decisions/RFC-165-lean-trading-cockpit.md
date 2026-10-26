---
title: "RFC-165: 精简实盘驾驶舱（4 Tab + 交易信息优先）"
status: active
module: web_dashboard
related_code:
  - app/web_dashboard/templates/base.html
  - app/web_dashboard/templates/dashboard.html
  - app/web_dashboard/templates/trade.html
  - app/web_dashboard/templates/hk_trade.html
  - app/web_dashboard/templates/battle_plan.html
  - app/web_dashboard/templates/playbook.html
  - app/web_dashboard/templates/watchlist.html
  - app/web_dashboard/static/css/dashboard.css
  - app/web_dashboard/static/js/dashboard.js
  - app/web_dashboard/static/js/playbook.js
  - app/web_dashboard/routes/dashboard.py
  - app/web_dashboard/routes/trade.py
  - app/web_dashboard/runtime_status.py
  - app/web_dashboard/data_service.py
  - app/web_dashboard/order/executor.py
  - app/web_dashboard/manual_order_service.py
  - app/web_dashboard/routes/hk_trade.py
  - app/web_dashboard/routes/battle_plan.py
  - app/web_dashboard/routes/playbook.py
  - tests/web_dashboard/test_trade_runtime_status.py
related_okr: OKR-2026-11
tags: [web-dashboard, live-trading, information-architecture, lean, degraded-mode]
updated: 2026-10-25
---

# RFC-165: 精简实盘驾驶舱（4 Tab + 交易信息优先）

> **状态**: Batch 1 已实施并完成页面级验证；Batch 2 的 runtime/capabilities、断线只读态、持仓保护口径与订单来源标记已落地并验证。重连对账状态机、改止损快捷入口仍未实现。
> **实施边界**: Batch 1 改 Jinja 模板、CSS、前端 JS，并修正今日摘要对过期作战计划的只读聚合口径。Batch 2 只读汇总现有连接/持仓/订单/闸门状态，新增 `/api/trade/snapshot.runtime` 和前端禁用 guard；未改订单 validator、四锁、仓位、止损止盈、交易频率或 Playbook 执行管线。仅重启 dashboard 进程，未重启主交易服务。
> **日期**: 2026-10-25
> **作者**: Codex (Bot)
> **审批人**: 项目所有者

## 摘要

把现有 Dashboard 从“页面和分析模块的集合”收敛为“实盘交易驾驶舱”：一级导航从 7 个减到 4 个；以 Playbook 作为计划的唯一事实源；US/HK 交易共用一个入口；首页只保留会影响当下决策和动作的信息；断线时明确进入只读态，绝不把缓存数据伪装成实时数据。

本 RFC 不引入 React、Tailwind、独立设计系统或第二套前端工程，不新增下单管线，不改任何交易闸门、仓位、止损止盈和交易频率。第一阶段只做导航收敛、重复内容下线、现有组件复用；第二阶段才加只读运行状态字段和断线只读态。

## 问题陈述

修改前一级导航为：指挥中心、作战计划、Playbook、交易、交易 HK、股票池、系统。其中：

- `/plan` 仍展示旧作战计划链，而后端已明确“旧 discovery 链停用，当前交易以 Playbook 指令为准”；与 `/playbook` 形成双计划入口。
- `/trade` 与 `/trade/hk` 是两套页面，共 701 / 1294 行模板，核心任务相同但交互和视觉分叉。
- 首页同时承载状态、决策摘要、链路漏斗、持仓、毕业门禁、因子、退出画像、校准、记分牌和研究统计，首屏无法稳定回答“现在要我做什么”。
- 交易页默认混入 AI 建议、归因和研究信息，真正需要快速操作的持仓保护、平仓、改止损、撤单和异常订单反而需要寻找。
- 当前新鲜度只在快照级提供 `stale` / `as_of`（`app/web_dashboard/routes/trade.py:508`、`:563`），缺少“券商连接、报价、持仓、订单、可执行动作”的统一状态表达。
- 页面债务可量化：`dashboard.html` 约 158 处 inline style，`trade.html` 约 72 处；模板/CSS/JS 内约 2135 处硬编码颜色、115 个唯一色值；`trade.js` 2264 行、`dashboard.js` 1770 行。

问题不是“页面不够多”，而是入口重复、动作路径过长、数据可信度表达不足，以及研究噪音挤占实盘注意力。

## 提议方案

### 1. 一级导航收敛为 4 个

| 新入口 | 职责 | 旧路由处理 |
|---|---|---|
| 今日 `/` | 连接/数据状态、账户风险、最多 3 条待办、持仓/挂单摘要 | 保留 `/`，重写首屏 |
| 交易 `/trade` | 下单、持仓保护、平仓、改止损、撤单、成交/台账；US/HK 用市场切换 | `/trade/hk` 保留独立模板，但不再占一级 Tab |
| 计划 `/playbook` | Playbook 指令、候选与执行状态；唯一计划事实源 | `/plan` 作为旧页保留，加停用横幅，不再占一级 Tab |
| 系统 `/system` | 四锁、连接、自动交易、健康、审计、危险操作 | 保留 |

股票池不占一级导航，`/watchlist` 路由保留，并可从今日、交易、计划页进入。个股详情 `/stock/{symbol}` 保留为统一下钻页。复盘/完整分析不再占用一级导航，仍在今日页的“研究数据”展开区；本批未新建 `/review` 页面。

### 2. 交易与保护信息优先

交易页首屏围绕四块组织：账户摘要、报价与订单票据、持仓交易组、挂单/成交。深度研究、因子统计、退出画像和归因不进入交易首屏。现有标的智能卡/AI 建议条仍在订单票据内（本批未删除），因为它是下单上下文；后续按实际使用频率再决定是否折叠。

持仓交易组至少回答四个问题：仓位多少、保护单覆盖多少、距离止损多远、下一步可以做什么。快捷动作限定为平 25%/50%/100%、改止损、撤单和查看生命周期。所有动作继续调用现有 API，后端 validator 仍是唯一裁决方。当前已落地平仓/撤单/接管路径与 SL 保护状态展示；改止损快捷入口仍在下一批。

### 3. 计划只保留一个事实源

`/playbook` 是当前唯一计划主入口，继续消费现有 Playbook API、执行/跳过逻辑和确认弹窗，不新增平行通道。旧 `/plan` 页面（battle plan / discovery 链）保留兼容访问，但已从导航移除，并在页顶明确标注“旧 Pipeline 页面已停用，前往 Playbook”；本批未做 301/307 重定向，也未物理删除旧模板或后端路由。物理退役需先完成零引用审计。

### 4. 数据可信度与断线只读态

> **实施状态（2026-10-25）**: 最小版已落地。`/api/trade/snapshot` 已新增 `runtime`，前端已按服务端 capabilities 锁定写动作，并在断线时显示只读横幅、最后数据时间与“离线”摘要点。重连对账状态机、改止损快捷入口仍未实现；不得据此宣称“富途 App 不可用时系统已完全可交易”。

在现有 `/api/trade/snapshot` 上做最小加法，不改已有字段：

```text
runtime: {
  broker: connected | degraded | disconnected | unknown,
  account: real | sim | unknown,
  quotes: live | degraded | unknown,
  positions: live | degraded | unknown,
  orders: live | degraded | unknown,
  market: regular | pre | after | closed | unknown,
  as_of: 数据成功落地时间,
  runtime_as_of: 本次响应时间,
  capabilities: {place, reduce, cancel, modify_stop},
  reasons: [string]
}
```

`capabilities` 由服务端根据连接标志、账户/持仓/订单的数据来源与权威性、报价新鲜度和现有 `blocks_now` 闸门生成，前端只消费。市场时段只用于展示，不新增阻断；盘前/盘后/休市的排队、GTC 与限价约束仍由现有订单流程和 validator 处理。fail-safe 口径：

- 券商断线或状态未确认：`place / reduce / cancel / modify_stop` 全部关闭。
- 持仓来自 DB fallback 或来源未确认：关闭 `place / reduce / modify_stop`，但不无谓关闭仍来自券商权威应答的 `cancel`。
- 订单来自审计 DB 或来源未确认：关闭 `cancel`，状态可能滞后。
- 持仓报价降级：关闭 `place`，仍允许 `reduce`（减仓不依赖新开仓的报价假设）。
- 现有手动闸门 `blocks_now`（kill switch、资本 BLACKOUT、日回撤、当日单数上限）：只关闭 `place`；减仓/撤单仍由后端裁决。
- 账户资金不可用：关闭 `place`。

口径边界：

- 手动看板当前固定走 REAL 账户（`ManualOrderService._is_live()` 恒为 `True`）；`ALLOW_REAL_TRADING` **不是**手动交易总闸，不得在 UI 把它误标成手动能力锁。
- `protection_state=broker_attached` 只表示本地 DB 记录了券商 STOP 单 ID，**尚未逐单向券商核验该单仍活跃**；UI tooltip 明确写“本地记录显示”。
- 断线时只读展示本地台账与最近缓存，不能下单。重连后 runtime 按下一轮账户/持仓/订单证据重新计算；没有独立的“本地 vs 券商差异确认”状态机，因此不得宣称已自动完成对账。
- runtime 是提示层，后端 validator/executor 仍是最终防线；本批未改后端裁决逻辑。

### 5. 不造新轮子

- 复用现有 `base.html`、卡片、按钮、模态框、toast、`apiFetch`、WebSocket 和 polling。
- 不引入前端构建链，不新增 CDN 依赖。
- 不建立独立原型工程或第二套组件库；视觉验证直接在现有 Jinja 模板上做灰度修改。
- Beautiful UI / BeUI / shadcn/ui 只作为信息层级、留白、状态表达和可访问性的参考，不作为运行时依赖。

## 影响范围

- 服务焦点: F1（交易执行与保护动作）+ F3（退出可观测性）
- 已达成: 一级导航数 7 → 4；首页默认研究模块数 → 0；旧计划页不再占一级入口；390px 无横向溢出；交易页桌面双栏、移动端动作顺序可读；runtime capabilities 覆盖下单/减仓/撤单/改止损；断线/持仓 DB fallback/订单审计 fallback/报价降级均有明确锁定原因。
- 仍需完成: 重连对账状态机；改止损快捷入口；移动端平仓/改止损/撤单三条路径全部 ≤ 3 步；首页待办严格 ≤ 3；所有交易数字统一 as-of/来源标识。
- 交易行为: 未改 gate、仓位、SL/TP、频率、Playbook 执行器或订单 API 的裁决逻辑。runtime 只做前端提示与禁用。
- Batch 1 以模板/CSS/前端 JS 为主；额外修正 `/api/dashboard/snapshot` 的今日摘要聚合：过期 `daily_battle_plan_us.json` 整体作废，不再只清 `targets` 而让旧 `position_actions` 冒充今日待办。Batch 2 新增只读 runtime/capability 契约、订单来源元数据与持仓保护口径；未改后端执行管线。

## 风险评估

### 最坏情况

- 合并计划入口时误把旧作战计划当成今日交易依据，产生过期信号。
- 页面隐藏了某个仍被高频使用的功能，降低操作效率。
- 断线时前端仍允许提交，或把最后报价显示成实时价，导致错误下单。
- 迁移导航和模板时破坏现有交易 UI 契约测试。

### 缓解措施

- 计划页只接 Playbook；旧 `/plan` 保留兼容访问并明确标注停用，不做静默重定向，也不在本批物理删除。
- 第一阶段不改后端交易行为；每次只动一个页面，先灰度再替换默认入口。
- `runtime.capabilities` 由服务端输出，前端禁用只是提示，后端校验仍是最终防线。
- 保留并运行 `tests/web_dashboard/` 契约测试，新增 runtime 字段和断线态测试后再切换默认页面。
- 独立原型工程取消，避免同时维护两套 UI。

## 验证计划

```bash
cd /opt/ftseek/FTSeek-US

# 1. 导航与重复入口静态检查
rg -n 'href="/(plan|playbook|trade/hk|watchlist)' app/web_dashboard/templates/base.html

# 2. 现有交易 UI/闸门/台账契约
./venv/bin/python -m pytest tests/web_dashboard -q

# 3. 今日摘要过期计划口径 + 页面级 JS 语法
./venv/bin/python -m pytest tests/web_dashboard/test_dashboard_decision_summary.py -q
./venv/bin/python -m pytest tests/web_dashboard/test_trade_runtime_status.py -q

# 4. 页面级冒烟（本轮仅重启 ftseek-dashboard，主服务 ftseek-us 未重启）
node --check app/web_dashboard/static/js/dashboard.js
node --check app/web_dashboard/static/js/playbook.js
node --check app/web_dashboard/static/js/trade.js
```

Batch 2 已用 Playwright 在桌面 1440×1000 / 移动 390×844 验证真实连接态与拦截快照模拟的断线态：均 HTTP 200、0 console/page error、0 页面级横向溢出；断线态横幅为 `danger`、`#btnSubmit`/确认按钮 disabled、摘要点显示“离线”。重连对账与改止损快捷入口仍未实现，因此没有把它们标为已验证。

## 回滚计划

Batch 1 的回滚面为 Jinja 模板、CSS、前端 JS，以及今日摘要的只读聚合函数；未改交易 API/执行管线，也未迁移旧 `/plan` 路由。回滚时：

```bash
git revert <implementation-commit>
```

**回滚触发条件**：导航缺失导致无法到达交易/系统、交易页 DOM 层级破坏、股票池/港股触控退化、旧 Playbook 指令无法执行、交易契约测试失败，或用户确认首屏信息不足。

## 替代方案

### 方案 A：全站迁移 React + shadcn/ui

能获得统一组件生态，但要引入构建链、状态管理和全量重写，远超当前问题所需；不选。

### 方案 B：保留 7 个 Tab，只换视觉皮肤

不解决入口重复、计划双源和首屏噪音；不选。

### 方案 C：保留独立 `/trade/hk` 页面

短期少改代码，但长期继续维护两套交易交互和视觉状态；先以统一入口 + 兼容跳转落地，再按使用情况决定是否物理合并模板。

## 5 问自检

1. 改哪里：导航、今日首屏、交易首屏、计划入口和只读状态字段。
2. 影响谁：操作实盘/模拟盘的用户；不改策略和执行器。
3. 如何验：现有 web_dashboard 契约 + 页面级断线演练 + 移动端三条动作路径。
4. 如何回滚：按页面独立提交，直接 revert；旧路由保留跳转。
5. 有没有更简单方案：有。先只做入口收敛和默认内容隐藏，不物理删除后端页面；待引用清零后再删文件。

## 审批

- [x] 5 问自检完成
- [x] Batch 1 测试覆盖完成；Batch 2 runtime/断线只读契约覆盖完成（重连对账未开始）
- [x] 回滚路径明确（文档/前端独立提交）
- [ ] 审批人签字

## 实施记录

**实施日期**: 2026-10-25
**Commit**: 未提交（工作树改动）
**实施范围**:

- 一级导航 7 → 4；`/trade/hk` 改为交易页市场切换；`/watchlist` 改为今日/交易/计划的次级入口。
- 今日页增加统一页头；研究模块默认隐藏，点击“研究数据”才请求并展示；待办/零待办提到历史日报之前；决策脑健康仅在异常或研究展开时占首屏；过期作战计划整体作废，不再把旧 `position_actions` 作为今日 must-do。
- 交易页增加页头与美股/港股切换；桌面“下单票据 + 持仓/交易记录”双栏，移动端保持下单 → 持仓 → 交易记录顺序；移动端账户摘要压缩到约 119px，次要指标与闸门条单行横向滑动。
- 股票池增加页头、交易入口、移动端两行记录布局和显式“做多/做空/移除”动作；港股快捷手数、系统风险档位按钮提升到 40px 触控高度。
- 港股页在 <1280 宽度改单栏，表格局部横向滚动而不撑宽页面；旧 `/plan` 修复 320px 文本溢出，页顶保留停用横幅，未改后端路由。
- 高频交易路径移除依赖 emoji 字体的装饰图标，改用文字/CSS 状态点；系统页与 Playbook regime 徽标同步收敛。

**验证结果**:

- `./venv/bin/python -m pytest tests/web_dashboard -q` → `150 passed`（含过期计划摘要 3 例）。
- 交易 UI 定向契约（batch2/3/4/5、RFC-078、gates、close_position）→ `112 passed`。
- `node --check dashboard.js` / `playbook.js` / `trade.js` 通过；`git diff --check` 通过。
- Playwright 7 个页面 × 桌面 1440×1000 / 移动 390×844：全部 HTTP 200、0 console error、0 横向溢出；交易页 `.trade-workspace` 仅 2 个直接子节点，右栏含“持仓 + 交易记录”两张卡，移动端账户摘要 sticky 高约 119px 且不遮挡顶栏；研究模块默认 9/9 隐藏，展开后触发 5 个研究接口、8/9 模块可见（校准卡当前无数据）；补测 320/768/1024 宽度，修复旧 `/plan` 320px 与港股页 1024px 页面级横向溢出。

### Batch 2：runtime 与断线只读态（最小版，已落地）

**实施日期**: 2026-10-25
**Commit**: 未提交（工作树改动）

- 新增 `app/web_dashboard/runtime_status.py`：纯函数把连接、账户、报价、持仓、订单、市场时段和现有闸门压成 `runtime`，并计算 `place / reduce / cancel / modify_stop`。
- `/api/trade/snapshot` 新增 `runtime`；数据来源证据不足时 fail-closed，不再只信 `_connected` 标志。
- 新增 `get_today_orders_with_status()`：返回 `{orders, source, broker_ok, error, as_of}`；旧 `get_today_orders()` 签名保留兼容。`/api/orders/today` 同步附带来源元数据。
- `_parse_positions()`：展示优先 `current_sl`，保留 `initial_stop_loss_price`；补充止盈、券商单 ID、`protection_state=broker_attached|db_only|none|unknown`；只有存在止损来源列时才宣称保护状态已知。
- `_calc_stop_distances()`：优先使用真实 DB/券商 SL/TP，ATR 仅 fallback，并输出 `sl_source/tp_source`。
- `trade.html` / `trade.js`：账户摘要上方新增单行 runtime 横幅；下单、确认、平仓、撤单、接管均消费服务端 capabilities；状态读取失败/无 runtime 时 fail-closed 只读；订单行显示“撤单锁定”；持仓 SL 列显示“券商单/仅DB/无保护/未知”；断线摘要点显示“离线”，不再显示绿色“实时”。
- 静态资源版本 `trade.js?v=20261025b`。

**验证结果**:

- `./venv/bin/python -m pytest tests/web_dashboard -q` → **164 passed**（新增 `test_trade_runtime_status.py` 14 例）。
- `py_compile`（runtime/trade/dashboard/data_service/executor/manual_order_service）、`node --check trade.js`、`git diff --check` 通过。
- 真实服务快照：`broker=connected / account=real / positions=live / orders=live / market=closed`，capabilities 全开；`/api/orders/today` 返回 `source=broker, broker_ok=true`。
- Playwright 1440×1000 / 390×844：真实连接态 2 次、拦截 `/api/trade/snapshot` 模拟 `disconnected` 2 次，全部 HTTP 200、0 console/page error、0 横向溢出；断线态横幅 `danger`、`#btnSubmit` disabled、摘要点“离线”；真实态摘要高度 169.8px（桌面）/145.8px（移动），断线态 158.8/134.8px。

**明确未做 / 下一批**:

- 未实现真正的重连对账状态机：断线期间只读，恢复后由下一轮券商证据重新计算；没有“本地 vs 券商差异确认”门槛。
- 未实现改止损快捷入口（capability 已计算，但页面只有接管/平仓/撤单路径）。
- 不是富途 App 的完整替代：OpenD/券商不可达时不能交易，只能只读本地台账与最近缓存。
- `protection_state=broker_attached` 只代表 DB 有券商 STOP 单 ID，尚未逐单向券商核验活跃性。
- 移动端平仓/改止损/撤单三条动作路径未全部压到 ≤3 步；首页待办未严格限制 ≤3。
- 未物理删除 `/plan` 或 `/api/battle-plan`；`/api/battle-plan` 仍被交易/股票池读取。
- 未引入 React、Tailwind、BeUI/shadcn 运行时或第二套组件系统；只借鉴其信息层级、留白、状态表达和可访问性原则。
