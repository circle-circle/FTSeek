# -*- coding: utf-8 -*-
"""交易界面批3 呈现层契约 (2026-09-08, REVIEW-20260908 §D 批3: A8–A12 / B7–B10).

批3 = 清洁与一致性。这一批的坏法比批1/批2 更"安静": 删死码删错对象、重构把行为
顺手改掉、移动端标签与表头漂移、颜色口径与券商枚举不符。故钉子按**五类静默坏法**立:

  1. **删除过头 / 删除不足** (B7): 死 CSS 与死端点退役后, 既不能留下半死实现
     (墓碑里还挂着旧聚合逻辑 = 下次有人以为它活着), 也不能把路由整个删掉
     (仓外 skill 文档仍按旧契约描述它, 404 与"路径打错"不可区分 ⇒ 必须 410 + warning)。
  2. **重构改变行为** (B8): 两段 ~60 行内联 HTML 收敛成一个函数后, 文案/阈值/
     放行契约必须逐条保持 —— 尤其 `window._priceWarnAck` → `acknowledge_price_warning`
     是与后端 validator.py:229 的软拦截契约, 顺手"清理全局变量"就会把闸门拆掉。
  3. **表头与单元格漂移** (A12 / A12-US): 移动端堆叠靠 `td[data-label]` 出标签,
     这是**第二套列名**, 加列/改列名时极易只改一边。故直接钉 data-label 序列
     == thead <th> 文本序列 (含空标签列), 两页四张表全钉。
     同族: audit 行往"价格"列塞 order_type (表头说价格、内容写 MARKET) 已改为真价格。
  4. **防护被"优化"掉** (B10): curQuote 标的校验、在途响应过期丢弃、可见性守卫、
     arguments.callee 替换 —— 这些都必须是**只呈现不阻断**(除既有 A1/价格偏离勾选),
     故同时钉"不得新增前端阻断"。
  5. **配色口径错 = 假警报** (A11 修正, 上线复核实测抓到): 用正则猜券商枚举语义,
     把富途 CltRiskStatus 九档里的实盘常态 LEVEL3「较安全」染成红色。红色天天误报,
     真危险 (LEVEL8/9) 就不再是信号 ⇒ 逐档钉死官方档位表, 并钉其单调性 (反序必须炸)。
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
HK_TMPL = PROJECT_ROOT / "app" / "web_dashboard" / "templates" / "hk_trade.html"
US_TMPL = PROJECT_ROOT / "app" / "web_dashboard" / "templates" / "trade.html"
US_JS = PROJECT_ROOT / "app" / "web_dashboard" / "static" / "js" / "trade.js"
US_ROUTES = PROJECT_ROOT / "app" / "web_dashboard" / "routes" / "trade.py"
HK_ROUTES = PROJECT_ROOT / "app" / "web_dashboard" / "routes" / "hk_trade.py"
HK_SVC = PROJECT_ROOT / "app" / "web_dashboard" / "hk_order_service.py"
VALIDATOR = PROJECT_ROOT / "app" / "web_dashboard" / "order" / "validator.py"
AUDIT = PROJECT_ROOT / "app" / "web_dashboard" / "order" / "audit.py"


def _src(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def _th_labels(html: str, tbody_id: str) -> list:
    """该表 thead 的 <th> 文本序列 (去标签、去 Jinja 注释)。"""
    idx = html.index(f'id="{tbody_id}"')
    head = html.rindex("<thead>", 0, idx)
    seg = html[head:html.index("</thead>", head)]
    cells = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", seg, re.S)   # 不得匹配 <thead>
    return [re.sub(r"<[^>]+>", "", c).strip() for c in cells]


def _data_labels(src: str, start: str, end: str) -> list:
    return re.findall(r'data-label="([^"]*)"', _slice(src, start, end))


def _node(js: str) -> str:
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr[:1200]
    return res.stdout.strip().splitlines()[-1]


def _js_fn(src: str, name: str) -> str:
    """切出一个顶层 (8 空格缩进) 或 0 缩进的完整函数体, 供 node 真跑。"""
    i = src.index(f"function {name}(")
    line_start = src.rindex("\n", 0, i) + 1
    indent = src[line_start:i]
    closer = "\n" + indent + "}\n"
    j = src.index(closer, i) + len(closer)
    return src[line_start:j]


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")


# ── 3. 表头 ↔ data-label 一致性 (A12 / A12-US) ─────────────────

HK_TABLES = [
    ("pos-tbody", "async function loadPositions() {", "async function loadOrders() {"),
    ("ord-tbody", "async function loadOrders() {", "function openHkModal("),
]


@pytest.mark.parametrize("tbody_id,start,end", HK_TABLES)
def test_hk_stack_labels_match_thead(tbody_id, start, end):
    """A12: 移动端堆叠的第二套列名必须与 thead 逐格对应 (含末尾空标签列)。"""
    html = _src(HK_TMPL)
    assert _data_labels(html, start, end) == _th_labels(html, tbody_id)


US_TABLES = [
    ("posBody",
     "_diagData[sym] = {pos: p, slData, signal: sig, qty, isLong};",
     "// ── 注意力优先引擎"),
    ("ordersBodyTable",
     "sortKey: sortKey,",
     "// 系统操作记录 (audit)"),
    ("ordersBodyTable",
     "sortKey: a.timestamp || '',",
     "'</tr>'"),
]


@pytest.mark.parametrize("tbody_id,start,end", US_TABLES)
def test_us_stack_labels_match_thead(tbody_id, start, end):
    """A12-US: 持仓 7 列 + 交易记录 8 列 (券商订单行与 audit 行共用同一表头)。"""
    js = _src(US_JS)
    assert _data_labels(js, start, end) == _th_labels(_src(US_TMPL), tbody_id)


def test_stack_css_present_on_both_pages():
    """堆叠方案是 CSS 变形而非第二套渲染 (单一渲染路径 ⇒ 不会出现表格/卡片数据不一致)。"""
    hk, us = _src(HK_TMPL), _src(US_TMPL)
    assert ".hk-stack td::before { content: attr(data-label)" in hk
    assert ".pos-stack td::before { content: attr(data-label)" in us
    assert hk.count('class="hk-stack"') == 2, "港股两张表都要挂 hk-stack"
    assert us.count('class="pos-stack"') == 2, "美股两张表都要挂 pos-stack"
    # 空态/错误态是 colspan 单元格, 堆叠时必须回到整块居中, 不能被 flex 撕开
    assert ".hk-stack td[colspan]" in hk and ".pos-stack td[colspan]" in us


def test_audit_price_column_shows_real_price_not_order_type():
    """B7 附带: 表头写"价格"、内容塞 order_type 是标签与数据不符 (实测 audit.price 有值)。"""
    js = _src(US_JS)
    seg = _slice(js, "var aTime = escHtml(", "'</tr>'")
    assert "var aPrice = (a.price != null && Number(a.price) > 0)" in seg, "audit 行应显示真价格"
    assert "var aType = escHtml(a.order_type || '')" in seg, "订单类型不得丢, 降为后缀小字"
    assert "aPrice +" in seg and "aType ?" in seg
    assert 'data-label="价格" style="text-align:right;font-size:11px' not in js
    # 数据确实存在: manual_order_audit 建表语句里有 price 列
    assert re.search(r"price REAL", _src(AUDIT)), "audit 表 schema 应有 price 列"


# ── 1. 死码退役 (B7) ──────────────────────────────────────────

def test_dead_css_retired_from_us_template():
    """B7: 三组零消费 CSS 已退役 (实测全仓 grep 仅命中 CSS 自身)。"""
    html = _src(US_TMPL)
    # 钉"规则定义"而非裸类名: 退役说明的注释里会提到旧类名 (那正是防后人重新加回来的线索)
    dead_rules = re.findall(
        r"\.(dq-[a-z-]+|cap-grid|cap-item|pos-cards-mobile|pos-card-m[a-z-]*)\s*\{", html)
    assert not dead_rules, f"死 CSS 规则仍在 trade.html: {dead_rules}"
    # 但同名的**活**样式不得被误删
    for alive in (".fund-info", ".gate-chip", ".sig.red", ".diag-card", ".preview-row"):
        assert alive in html, f"{alive} 被误删"


def test_slow_data_endpoint_is_410_tombstone():
    """B7: 死端点走减法三步的"证零消费 → 停", 留墓碑不删路由。"""
    src = _src(US_ROUTES)
    seg = _slice(src, '@router.get("/api/trade/slow-data")', "\n    # ═")
    assert seg.count("@router.get") == 1, "路由必须仍在 (删掉 ⇒ 404 与打错路径不可区分)"
    assert "status_code=410" in seg and "ENDPOINT_RETIRED" in seg
    assert "logger.warning" in seg, "命中墓碑要留痕, 才能发现仍在打的调用方"
    # 只留墓碑, 不留半死实现 (docstring 里会点名退役了哪些键, 故只在**代码**部分断言)
    body = seg[seg.index('\"\"\"', seg.index('\"\"\"') + 3) + 3:]
    for dead in ("decision_quality", "trade_cards", "plan_adherence", "profit_factor",
                 "behavioral", "get_audit_history"):
        assert dead not in body, f"墓碑里仍有旧聚合逻辑: {dead}"
    assert "decision_quality_v2" not in src, "旧缓存键应随实现一起消失"


def test_snapshot_logging_downgraded_and_deduplicated():
    """B7: 盘中 5s 一轮 ⇒ 前后端各一份等量日志 = 每分钟 24 行噪声。"""
    routes = _src(US_ROUTES)
    i = routes.index("[trade/snapshot]")
    window = routes[max(0, i - 700):i + 300]
    assert "logger.debug" in window, "后端快照日志应降为 debug"
    assert "logger.info" not in window
    assert not re.search(r"^\s*import json\s*$", window, re.M), \
        "快照函数内的 import json 从未使用, 应随本批删除"
    # 前端那份直接删掉 (异常路径的 console.error 保留)
    js = _src(US_JS)
    assert not re.search(r"^\s*console\.log\('\[snapshot\]", js, re.M), \
        "每轮 console.log('[snapshot]', …) 应删 (注释里提到它不算复活)"
    assert "console.error('Trade snapshot error" in js, "异常路径日志不得一起删掉"


# ── 2. 重构不改行为 (B8) ──────────────────────────────────────

def test_price_warn_has_single_implementation():
    """B8: 两段 ~60 行内联 HTML 收敛为一个函数, 且不再把 JS 写成 HTML 属性字符串。"""
    js = _src(US_JS)
    assert "priceWarnHard_" not in js and "priceWarnCheck_" not in js
    assert "function _renderPriceWarn(" in js and "function _bindPriceWarn(" in js
    assert js.count("_renderPriceWarn(") >= 2 and js.count("_bindPriceWarn(") >= 2
    warn = _slice(js, "function _renderPriceWarn(", "function _bindPriceWarn(")
    assert "onchange=" not in warn, "内联 onchange 字符串应改 addEventListener"
    assert 'class="price-warn gated ${kind}"' in warn, "样式走 CSS class, 不再内联 style"
    assert "let priceWarnAcknowledged" not in js, "声明后从未读取的死变量应一起清掉"


def test_price_warn_ack_contract_with_backend_preserved():
    """B8 关键: 勾选放行是与后端 validator 的契约, 重构不得把它"顺手清理"掉。"""
    js = _src(US_JS)
    bind = _slice(js, "function _bindPriceWarn(", "function stepQty(delta)")
    assert "window._priceWarnAck = on;" in bind
    assert "confirmBtn.disabled = !on;" in bind
    assert "order.acknowledge_price_warning = true" in js
    assert "window._priceWarnAck = false" in js, "closePreview 必须复位, 否则跨单沿用确认"
    # 后端仍按此标志软拦截 ⇒ 这确实是契约而非前端自娱
    assert "if not order.acknowledge_price_warning:" in _src(VALIDATOR)


def test_price_warn_thresholds_come_from_gates_not_hardcoded():
    """B8: 前端不再硬编码 5/10 —— 后端 validator 用的是服务常量, 同一判据不得写两处。"""
    js = _src(US_JS)
    preview = _slice(js, "function previewOrder()", "function closePreview()")
    assert "dev >= 10" not in preview and "dev >= 5" not in preview
    assert "(_gates && _gates.price_deviation_warn_pct) || 5" in preview
    assert "(_gates && _gates.price_deviation_hard_pct) || 10" in preview
    assert preview.count("price_deviation_hard_pct") == 1, "阈值只取一次, 盘中/盘外共用"
    # fail-open 回落值必须与后端常量同值 (否则"取不到阈值"会静默改变判据)
    assert "PRICE_DEVIATION_WARN_PCT = 5.0" in _src(
        PROJECT_ROOT / "app" / "web_dashboard" / "manual_order_service.py")
    assert "PRICE_DEVIATION_HARD_PCT = 10.0" in _src(
        PROJECT_ROOT / "app" / "web_dashboard" / "manual_order_service.py")


@needs_node
def test_price_dev_scenario_wording_matches_backend():
    """B8: 场景化文案四条与后端 validator 逐字同构 (买卖 × 高于/低于市价)。"""
    js = _src(US_JS)
    harness = _js_fn(js, "_priceDevScenario") + """
const cases = [[ 'BUY', 110, 100, 10 ], [ 'BUY', 90, 100, 10 ],
               [ 'SELL', 90, 100, 10 ], [ 'SELL', 110, 100, 10 ]];
console.log(JSON.stringify(cases.map(c => _priceDevScenario(...c))));
"""
    out = json.loads(_node(harness))
    assert out == [
        "买入限价 $110.00 高于市价 $100.00 达 10.0%，将以市价立即成交（可能非预期）",
        "买入限价 $90.00 低于市价 $100.00 达 10.0%，订单不会立即成交（请确认是否输入正确）",
        "卖出限价 $90.00 低于市价 $100.00 达 10.0%，将以市价立即成交（可能非预期）",
        "卖出限价 $110.00 高于市价 $100.00 达 10.0%，订单不会立即成交（请确认是否输入正确）",
    ]
    backend = _src(VALIDATOR)
    for phrase in ("将以市价立即成交（可能非预期）", "订单不会立即成交（请确认是否输入正确）"):
        assert phrase in backend, f"后端文案已改, 前端必须同步: {phrase}"


def test_price_warn_css_classes_exist():
    """B8: JS 只用 class ⇒ CSS 必须在 (否则警告块变成无样式裸文本, 权重感全失)。"""
    html = _src(US_TMPL)
    for cls in (".price-warn", ".price-warn.hard", ".price-warn.soft", ".price-warn.gated",
                ".price-warn.ext", ".pw-scenario", ".pw-note"):
        assert cls in html, f"缺 CSS: {cls}"
    js = _src(US_JS)
    assert 'class="price-warn ext ${strong ? \'hard\' : \'soft\'}"' in js, "盘外提示同样 class 化"


# ── 4. 防护不得被优化掉 (B10 / A10 / A11) ─────────────────────

def test_us_no_arguments_callee():
    """B10: arguments.callee 在严格模式下直接抛 TypeError (未来加 'use strict' 的地雷)。"""
    js = _src(US_JS)
    assert "setInterval(arguments.callee" not in js
    assert "callee" not in re.sub(r"//[^\n]*", "", js), "注释外不得再有 arguments.callee"
    assert "var fastTick = function fastTick() {" in js
    assert "setInterval(fastTick, desired)" in js, "重挂定时器必须仍指向自己"
    assert "setInterval(fastTick, fastInterval)" in js


def test_us_recent_symbol_chips_are_fail_open_and_injection_safe():
    """B10: chips 只存代码、只在报价成功时记忆、localStorage 不可用不得影响下单。"""
    js, html = _src(US_JS), _src(US_TMPL)
    assert 'id="recentChips"' in html and ".rec-chip" in html
    assert "us_recent_symbols" in js
    block = _slice(js, "const RECENT_SYMBOLS_KEY", "// ── Symbol Intelligence")
    assert block.count("try {") >= 2 and "catch (e)" in block, "读写都要 fail-open"
    assert "return [];" in block, "localStorage 坏 ⇒ 无 chips, 不是抛错"
    assert "data-sym=" in block and "onclick=" not in block, "用属性 + 事件委托, 不拼内联 JS"
    assert "closest('.rec-chip')" in block and "addEventListener('click'" in block
    assert "onSymbolChanged();" in block, "点 chip 必须走与手输+失焦相同的路径"
    # 只在报价成功时记忆 (打错的代码不进 chips)
    quote = _slice(js, "async function fetchQuote()", "function setQty(")
    assert quote.index("if (q.cur_price) {") < quote.index("saveRecentSymbol(sym);")
    assert ".slice(0, 5)" in block, "上限 5 枚, 不无限增长"


def test_us_cur_quote_symbol_validation_inform_not_block():
    """B10: 报价标的校验只呈现不阻断 (前端唯一阻断仍是价格偏离勾选)。"""
    js = _src(US_JS)
    # ① 换标的即作废上一只票的报价 (否则预估金额/偏离基准用错票价格)
    changed = _slice(js, "function onSymbolChanged()", "// ── B10")
    assert "curQuote = null;" in changed and "curCap = null;" in changed
    # ② 在途响应过期 ⇒ 整份丢弃 (后到的旧响应不得盖掉新标的)
    quote = _slice(js, "async function fetchQuote()", "function setQty(")
    assert "if (nowSym !== sym) return;" in quote
    assert quote.index("if (nowSym !== sym) return;") < quote.index("curCap = c;")
    # ③ 回包标的不符 ⇒ 丢弃 + 明说 (三态: 报错 ≠ 空)
    assert "报价标的不一致" in quote and "curQuote = null;" in quote
    # ④ 预览用校验后的报价, 且把"检查已跳过"说出来
    preview = _slice(js, "function previewOrder()", "function closePreview()")
    assert "const quoteMatches = " in preview
    assert "const est = quoteMatches ? qty * curQuote.cur_price : 0;" in preview
    assert preview.count("quoteMatches") >= 4, "est + 盘中偏离 + 盘外偏离 + 提示行都要用它"
    assert "限价偏离检查已跳过" in preview
    # 纪律: 不得因标的不符而 disable 提交 (那是新增前端闸门)
    mismatch = _slice(preview, "if (curQuote && !quoteMatches) {", "el.innerHTML = html;")
    assert "disabled" not in mismatch


def test_hk_visibility_guard_stops_all_polling():
    """A10: 隐藏 ⇒ 全停; 回前台 ⇒ 补画, 且离开超过一个周期要补拉 (不补 = 看到旧持仓)。"""
    html = _src(HK_TMPL)
    assert "document.addEventListener('visibilitychange', applyPollingState);" in html
    guard = _slice(html, "function applyPollingState()", "document.addEventListener('visibilitychange'")
    assert "if (document.hidden) {" in guard
    for stop in ("clearInterval(ageTimer)", "stopDataTimer()", "clearTimeout(quoteTimer)"):
        assert stop in guard, f"隐藏时未停: {stop}"
    assert "if (away > 30000) { loadOrders(); loadPositions(); loadAccount(); }" in guard
    # 收盘降频 + 时段判定单一真源 (标签与轮询口径必须同源)
    assert "function hkPollMs(base) { return hkSessionState().open ? base : base * 2; }" in html
    assert "hkPollMs(10000)" in html and "hkPollMs(30000)" in html
    # 不得再有绕过守卫的常驻 setInterval
    tail = html[html.index("loadPositions();\nloadOrders();"):]
    assert "setInterval" not in tail, "初始化段不得直接挂 setInterval (必须走 applyPollingState)"


def test_hk_no_native_dialogs():
    """A11: window.confirm / alert 换成自绘 modal (与全站一致), 且撤单确认信息量不得缩水。"""
    html = _src(HK_TMPL)
    code = html[html.index("{% block scripts %}"):]
    assert "window.confirm(" not in code and "alert(" not in code
    assert 'id="hk-modal"' in html and "function confirmHkModal()" in html
    cancel = _slice(html, "function cancelOrder(", "async function doCancelOrder(")
    assert "openHkModal(" in cancel
    # 原来 confirm 只给一个 order_id; 现在必须给完整订单明细 (信息量只增不减)
    assert "ordersCache.find" in cancel


def test_hk_account_bar_is_three_state():
    """A11: 实盘直通页必须能看到可用资金; 查询失败不得显示成"资金 0"。"""
    html, routes, svc = _src(HK_TMPL), _src(HK_ROUTES), _src(HK_SVC)
    assert 'id="acct-bar"' in html and "async function loadAccount()" in html
    load = _slice(html, "async function loadAccount()", "function renderAccountBar(")
    assert "const fail = (txt) => {" in load and "重试" in load
    assert "if (!j.ok) {" in load and "catch (e)" in load, "HTTP 失败与异常都要走 fail 分支"
    bar = _slice(html, "function renderAccountBar(", "loadPositions();")
    for field in ("a.cash", "a.power", "a.market_val", "a.total_assets",
                  "a.realized_pl", "a.unrealized_pl", "a.currency"):
        assert field in bar, f"账户条缺字段: {field}"
    # 后端三态: 失败 ⇒ 503 retryable, 不是 {"cash": 0}
    assert '@router.get("/api/hk/account")' in routes
    seg = _slice(routes, '@router.get("/api/hk/account")', "app.include_router(router)")
    assert "503" in seg and "retryable=True" in seg and "BROKER_UNAVAILABLE" in seg
    fn = _slice(svc, "def get_account_info_with_status", "# ── 审计")
    assert '"error": "broker_not_connected"' in fn
    assert "self._connected = False" not in fn, "资金查询失败不等于连接断了 (会引发重连风暴)"
    assert "accinfo_query" in fn


@needs_node
def test_hk_risk_badge_follows_futu_enum_not_regex():
    """A11 修正 (上线复核实测): 风险配色按富途枚举分级, 实盘常态 LEVEL3 不得染红。

    旧实现 `riskBad = risk_status && !/SAFE|NORMAL|OK/i.test(risk_status)` 对富途的
    CltRiskStatus 九档 LEVEL1..LEVEL9 **永远为真** (字面里没有 SAFE), 于是 2026-09-08
    实盘 risk_status=LEVEL3「较安全」每次打开页面都被染成红色 —— 红色天天误报,
    真危险 (LEVEL8/9) 来临时就不再是信号。这属批3 第 5 类静默坏法: **配色口径错**。
    故把富途官方档位表逐档钉死 (含单调性: 档位号越大越危险, 反了必须炸)。
    """
    html = _src(HK_TMPL)
    # 注释里引用旧实现是**文档**(说明为何不能再这么写), 只有可执行代码里的复活才算坏
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    assert "riskBad" not in code and "/SAFE|NORMAL|OK/" not in code, "旧正则口径必须删净"
    for cls in (".acct-risk.ok", ".acct-risk.warn", ".acct-risk.bad"):
        assert cls in html, f"缺 CSS ⇒ 染档失效变成无样式裸文本: {cls}"
    bar = _slice(html, "function renderAccountBar(", "loadPositions();")
    assert "hkRiskBadge(a)" in bar and "risk.sev" in bar, "账户条必须走分档函数"

    start, end = html.index("const HK_RISK_LEVEL_CN"), html.index("function renderAccountBar(")
    harness = html[start:end] + """
const sev = n => (hkRiskBadge({risk_status: 'LEVEL' + n}) || {}).sev;
const cases = {
  ladder: [1, 2, 3, 4, 5, 6, 7, 8, 9].map(sev),
  live: hkRiskBadge({risk_level: 'N/A', risk_status: 'LEVEL3'}),   // 2026-09-08 实盘回报
  worstOf: hkRiskBadge({risk_level: 'SAFE', risk_status: 'LEVEL9'}),
  coarse: hkRiskBadge({risk_level: 'WARNING', risk_status: 'NONE'}),
  optDanger: hkRiskBadge({risk_level: 'OPT_DANGER', risk_status: 'LEVEL2'}),
  unregistered: hkRiskBadge({risk_level: 'N/A', risk_status: 'DANGER'}),
  allUnknown: hkRiskBadge({risk_level: 'N/A', risk_status: 'NONE'}),
  missing: hkRiskBadge(null),
};
console.log(JSON.stringify(cases));
"""
    out = json.loads(_node(harness))
    # 九档单调: LEVEL1-3 安全 / 4-6 有隐患 / 7-9 危险 (反序 = 把最安全染红)
    assert out["ladder"] == ["ok", "ok", "ok", "warn", "warn", "warn", "bad", "bad", "bad"]
    assert out["live"]["sev"] == "ok" and out["live"]["text"] == "风险 较安全"
    assert "LEVEL3" in out["live"]["title"], "原样档位要留在 tooltip 里以便与富途 App 对照"
    assert out["worstOf"]["sev"] == "bad", "两字段取较严重的一档"
    assert out["coarse"]["sev"] == "warn" and out["coarse"]["text"] == "风险 预警"
    assert out["optDanger"]["sev"] == "bad" and out["optDanger"]["text"] == "风险 危险(期权)"
    # 未知 ≠ 危险: 未登记档位中性透出原文; 两档皆未知则不出芯片 (不编造结论)
    assert out["unregistered"]["sev"] == "" and out["unregistered"]["text"] == "风险 DANGER"
    assert out["allUnknown"] is None and out["missing"] is None


@needs_node
def test_hk_tick_offset_is_float_safe():
    """A9: 用整数刻度比较 —— 直接 price % tick 会因浮点误差把合法价判成非法。"""
    html = _src(HK_TMPL)
    harness = _js_fn(html, "tickOffset") + """
const r = {
  onGrid: tickOffset(1.0, 0.2),
  naiveTrap: tickOffset(0.7, 0.1),      // 0.7 % 0.1 = 0.0999... ⇒ 朴素实现会误判
  offGrid: tickOffset(0.3, 0.2),        // 0.1+0.2 家族: 必须吸附到 0.4 而非 0.30000000004
  bigPrice: tickOffset(1000.75, 0.5),
  fineGrid: tickOffset(0.503, 0.002),
};
console.log(JSON.stringify(r));
"""
    out = json.loads(_node(harness))
    assert out["onGrid"] is None, "已在价位表上 ⇒ 静默, 不打扰"
    assert out["naiveTrap"] is None, "浮点误差被误判成非法价 (朴素 % 实现的坑)"
    assert abs(out["offGrid"] - 0.4) < 1e-9
    assert abs(out["bigPrice"] - 1001.0) < 1e-9
    assert abs(out["fineGrid"] - 0.504) < 1e-9


def test_hk_tick_snap_informs_but_does_not_block():
    """A9/A8: 价位表与卖空 up-tick 都是**呈现**; 港股页唯一阻断仍只有 A1 标的错配。"""
    html = _src(HK_TMPL)
    assert 'onblur="snapPriceToTick()"' in html
    snap = _slice(html, "function snapPriceToTick()", "// ── 手数快捷")
    assert "if (snapped === null) return;" in snap, "已合法 ⇒ 不打扰"
    assert "disabled" not in snap, "吸附不得升级成禁用提交"
    pre = _slice(html, "function renderPrecheck()", "function setAction(")
    assert "港股卖空须 ≥ 卖一价（up-tick）" in pre, "A8: 券商必拒的规则要前置提示"
    assert "填卖一价" in pre, "A8: 提示要带一键修正入口"
    assert "不是 ${t} 的整数倍" in pre and "失焦自动吸附" in pre
    submit = _slice(html, "async function submitOrder()", "function burstRefresh(")
    assert "symbolDirty" in submit, "A1 标的错配仍是唯一前端阻断"
    assert "tickOffset" not in submit, "价位/卖空提示不得在提交路径上新增阻断"


def test_cache_version_bumped_for_batch3():
    """C2: /static/ 是 max-age=2592000 immutable ⇒ 改 trade.js 必须 bump ?v=。"""
    html = _src(US_TMPL)
    m = re.search(r'src="/static/js/trade\.js\?v=(\S+?)"', html)
    assert m, "trade.js 的 script 标签丢了 ?v="
    assert m.group(1) >= "20260908c", f"批3 改了 trade.js, 版本号必须 bump, 实测 {m.group(1)}"


def test_us_direction_toggles_are_real_buttons():
    """B9: <div onclick> 无键盘可达性; 改真 button 后必须补 UA 样式复位 (视觉不变)。"""
    html = _src(US_TMPL)
    assert html.count('<button type="button" class="dir-btn') == 4
    assert '<div class="dir-btn' not in html
    assert "font-family: inherit" in _slice(html, ".dir-btn {", ".dir-btn.active.long")
    assert ":focus-visible" in html, "键盘用户要看得见焦点"
    # JS 侧选择器不依赖标签名 ⇒ 无需改动 (钉住这一点, 防有人把选择器写成 div)
    js = _src(US_JS)
    assert "querySelectorAll('[data-dir]')" in js and "querySelectorAll('[data-op]')" in js


def test_both_templates_still_parse_and_js_is_valid():
    """最低保障: 两份模板 Jinja 可解析, 内联 JS 与 trade.js 语法可过 node --check。"""
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(US_TMPL.parent)))
    for name in ("trade.html", "hk_trade.html"):
        env.parse(_src(US_TMPL.parent / name))
    if shutil.which("node") is None:
        pytest.skip("node 不可用")
    res = subprocess.run(["node", "--check", str(US_JS)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr[:800]
    hk = _src(HK_TMPL)
    i0 = hk.index("{% block scripts %}") + len("{% block scripts %}")
    hk_js = hk[i0:hk.index("{% endblock %}", i0)]
    tmp = pathlib.Path("/tmp/_batch3_hk_inline.js")
    tmp.write_text(hk_js, encoding="utf-8")
    res = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr[:800]
