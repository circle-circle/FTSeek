# -*- coding: utf-8 -*-
"""券商真值层台账面板 呈现契约 (2026-09-08, RFC-078 批B 的看板面).

批B 建了券商真值账本 (`live_deal_records` / `live_position_snapshots`), 但**建完不接前端
= 数据存在而用户看不见**: 港股历史成交此前在看板上完全没有 (只有实时持仓 + 今日订单),
美股页的「交易记录」是回合台账 + 审计, 而回合台账断链过 3 个月 ⇒ 两页都需要真值层。

本文件钉的是这一类面板**最容易安静坏掉**的五种方式:

  1. **三态被前端塌缩**: 后端辛苦分了 `OK` / `NO_DEALS_IN_WINDOW` / `NO_DATA`, 前端一句
     "无成交" 全包 ⇒ "还没回补" 被读成 "本周没交易", 而这正是本 RFC 要消灭的误读
     (与 A3「查询失败 ≠ 空仓」同族)。故钉: 两页都必须分支, 且失败/空台账文案必须
     **自陈**它不是零成交。
  2. **口径提醒被"美化"掉**: 金额全是**毛/成交额** (富途 deal 级不返回手续费, fee 全 NULL)。
     琥珀提醒一旦被当作视觉噪声删掉, 读的人会把它当净额 ⇒ 钉提醒存在且**绑定后端字段**
     (`fee_rows_null`), 不是写死一句文案 (写死的文案会在口径变化时留着说谎)。
  3. **信噪比反向退化**: 美股页已有「交易记录」表, 再加一张成交流水表 = 今日成交出现两次
     ⇒ 钉美股页**不得**出现第二张台账表, 只允许一行上下文条, 且必须在记录表**之前**
     (先读口径再读数据) 并随卡片折叠一起显隐 (不留孤立一行)。
  4. **表头与 data-label 漂移** (承批3 A12 纪律): 移动端堆叠靠 `td[data-label]` 出标签,
     这是第二套列名 ⇒ 新表的两个序列必须逐一相等。
  5. **前端另写一份方向语义**: 开/平仓额若在前端按 `trd_side` 自己判, 就会出现"两边错得
     一样"(净现金流符号反) 且互相印证 ⇒ 钉前端只**展示**后端 `summary`, 不自行聚合金额。
"""
import pathlib
import re

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
HK_TMPL = PROJECT_ROOT / "app" / "web_dashboard" / "templates" / "hk_trade.html"
US_TMPL = PROJECT_ROOT / "app" / "web_dashboard" / "templates" / "trade.html"
US_JS = PROJECT_ROOT / "app" / "web_dashboard" / "static" / "js" / "trade.js"
US_ROUTES = PROJECT_ROOT / "app" / "web_dashboard" / "routes" / "trade.py"
HK_ROUTES = PROJECT_ROOT / "app" / "web_dashboard" / "routes" / "hk_trade.py"


def _src(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


# ══════════════════════════════════════════════════════════════════
#  1. 三态不得被前端塌缩
# ══════════════════════════════════════════════════════════════════
def test_hk_panel_branches_on_all_three_states():
    js = _slice(_src(HK_TMPL), "async function loadLedger()", "loadPositions();")
    assert "'NO_DATA'" in js or '"NO_DATA"' in js, "空台账必须单独分支 (还没回补 ≠ 零成交)"
    assert "台账为空 = 还没回补过" in js, "NO_DATA 文案必须自陈原因, 不得只写'无数据'"
    assert "是真没交易, 不是没同步上" in js, \
        "NO_DEALS_IN_WINDOW 必须与 NO_DATA 说清楚区别 (两种空的语义相反)"
    assert "这**不是**\"本窗口无成交\"" in js or "不是\"本窗口无成交\"" in js.replace("“", '"'), \
        "读取失败必须声明它不等于零成交 (与 A3「查询失败 ≠ 空仓」同族)"


def test_us_strip_branches_on_all_three_states():
    js = _slice(_src(US_JS), "async function loadUsLedger()", "// ── Expose handlers")
    assert "'NO_DATA'" in js or '"NO_DATA"' in js
    assert "台账为空 = 还没回补过" in js
    assert "是真没交易, 不是没同步" in js
    assert "这**不是**\"无成交\"" in js, "读取失败不得渲染成'无成交'"


def test_sync_failure_is_not_rendered_as_success():
    """回补失败必须说"台账未更新", 否则用户以为点过了就是最新数据."""
    us = _slice(_src(US_JS), "async function syncUsLedger()", "// ── Expose handlers")
    assert "台账**未更新**, 这不等于零成交" in us
    assert "unavailable_legs" in us, "一腿不可达必须点名, 不得只报 overall"
    hk = _slice(_src(HK_TMPL), "async function syncLedger()", "loadPositions();")
    assert "台账**未更新**, 这不等于零成交" in hk
    assert "不可达腿" in hk


# ══════════════════════════════════════════════════════════════════
#  2. 毛口径提醒必须存在且绑定后端字段
# ══════════════════════════════════════════════════════════════════
def test_gross_caliber_warning_is_bound_to_backend_field():
    """提醒必须引用 `fee_rows_null` (后端真值), 不是写死文案 — 写死的文案会在口径变了以后继续说谎."""
    hk = _slice(_src(HK_TMPL), "function renderLedger(", "async function syncLedger()")
    assert "fee_rows_null" in hk and "不得当净额用" in hk
    assert "毛" in hk and "不含手续费" in hk
    us = _slice(_src(US_JS), "function renderUsLedger(", "async function syncUsLedger()")
    assert "fee_rows_null" in us and "不得当净额用" in us
    # 成本基准不完整 / 未知方向 两条诚实性声明也必须在页面上可见 (后端算了不给看等于没算)
    for blob, name in ((hk, "港股页"), (us, "美股页")):
        assert "incomplete_basis_symbols" in blob, f"{name}未展示 FIFO 成本基准不完整声明"
        assert "未计入" in blob and "不编造基准" in blob, f"{name}未说明不完整标的未计入合计"


def test_window_scale_is_surfaced_not_implicit():
    """窗口尺度必须显示 (ET / HKT): 不写出来读者无从判断这是哪把尺切的窗口."""
    assert "window_tz" in _src(HK_TMPL) and "window_tz" in _src(US_JS)
    assert "window_tz" in _src(HK_ROUTES) and "window_tz" in _src(US_ROUTES)
    hk = _slice(_src(HK_TMPL), "function renderLedger(", "async function syncLedger()")
    assert "本地日" in hk
    us = _slice(_src(US_JS), "function renderUsLedger(", "async function syncUsLedger()")
    assert "本地日" in us


# ══════════════════════════════════════════════════════════════════
#  3. 信噪比: 美股页不得出现第二张成交表
# ══════════════════════════════════════════════════════════════════
def test_us_page_uses_a_strip_not_a_second_deals_table():
    """美股页已有「交易记录」表 (回合台账 + 审计) ⇒ 真值层只允许一行上下文条.

    证伪: 往 trade.html 塞一张 ledger 表 ⇒ 本条转红 (今日成交会在同页出现两次)。
    """
    us = _src(US_TMPL)
    assert 'id="ledgerStrip"' in us, "美股页缺券商真值层上下文条"
    for banned in ("ledger-tbody", "ledger-days"):
        assert banned not in us, f"美股页出现了第二套台账表控件 {banned} (信噪比反向退化)"
    body = _slice(us, '<div id="ordersBody"', '<!-- Preview modal')
    assert body.index('id="ledgerStrip"') < body.index('<table class="pos-stack">'), \
        "上下文条必须在记录表**之前** (先读口径提醒再读数据)"
    assert 'class="ledger-strip"' in us and ".ledger-strip {" in us, "缺样式定义 (裸 div 会被当正文读)"


def test_hk_panel_caps_rows_and_says_so():
    """港股页的成交表必须限行并**说出来**限了行 — 静默截断 = 让人以为看到了全部."""
    hk = _slice(_src(HK_TMPL), "function renderLedger(", "async function syncLedger()")
    assert "slice(0, 50)" in hk and "本窗口共" in hk
    assert "reverse()" in hk, "后端按时间升序, 展示必须新→旧 (否则最近一笔在最后一屏)"


def test_ledger_is_not_in_the_broker_polling_loop():
    """台账是本地只读 ⇒ 不得进 30s 券商轮询 (白耗富途频率额度, 且它并不需要实时)."""
    hk = _src(HK_TMPL)
    poll = _slice(hk, "function startDataTimer()", "function stopDataTimer()")
    assert "loadLedger" not in poll, "台账被塞进了券商轮询"
    js = _src(US_JS)
    assert "loadUsLedger" not in _slice(js, "async function loadTradeSnapshot()",
                                        "// ── RFC-078 批B"), "台账被塞进了快照轮询"
    # 比"不在轮询函数体里"更强的一层: 任何定时器都不许挂台账 (换个定时器名就绕过的锚点没意义)
    for src, name in ((hk, "港股页"), (js, "美股页")):
        timers = " ".join(re.findall(r"set(?:Interval|Timeout)\s*\(([^;]{0,120})", src))
        assert "loadLedger" not in timers and "loadUsLedger" not in timers, \
            f"{name}把台账挂进了定时器"


# ══════════════════════════════════════════════════════════════════
#  4. 表头与 data-label 不得漂移 (承批3 A12 纪律)
# ══════════════════════════════════════════════════════════════════
def test_hk_ledger_table_headers_match_data_labels():
    src = _src(HK_TMPL)
    card = _slice(src, "📒 实盘成交台账", "{% endblock %}")
    thead = re.search(r"<thead><tr>(.*?)</tr></thead>", card, re.DOTALL).group(1)
    headers = [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", thead, re.DOTALL)]
    labels = re.findall(r'data-label="([^"]*)"', _slice(src, "tb.innerHTML = shown.map", "}).join('')"))
    assert headers == labels, f"移动端第二套列名漂移:\nthead={headers}\nlabels={labels}"
    assert len(headers) == 9, headers
    colspan = re.findall(r'colspan="(\d+)"', card)
    assert all(int(c) == len(headers) for c in colspan), \
        f"空/错态行 colspan 与列数不一致: {colspan} vs {len(headers)}"


# ══════════════════════════════════════════════════════════════════
#  5. 前端不得另写一份方向语义 (金额只展示后端 summary)
# ══════════════════════════════════════════════════════════════════
def test_frontend_does_not_recompute_open_close_amounts():
    """开/平仓额的 SSOT 是 `live_ledger.summarize_deals` (与实盘周报同源).

    证伪: 在前端按 trd_side 自己累加金额 ⇒ 本条转红。两处各判方向的后果是"两边错得
    一样"(净现金流符号反) 且互相印证, 肉眼审不出来。
    """
    for path, name in ((HK_TMPL, "港股页"), (US_JS, "美股页")):
        src = _src(path)
        blob = _slice(src, "renderLedger(" if path is HK_TMPL else "function renderUsLedger(",
                      "async function syncLedger()" if path is HK_TMPL else "async function syncUsLedger()")
        assert "open_amount" in blob and "net_cash" in blob, f"{name}未展示后端汇总"
        assert not re.search(r"(reduce|forEach)\s*\(", blob), \
            f"{name}在前端聚合金额 = 另写了一份方向语义"
        assert "SELL_SHORT" not in blob or "开空" in blob, \
            f"{name}若出现方向枚举, 只能用于**展示标签**, 不得参与金额分类"
    # 展示标签必须四值齐全 (缺一个就会把新方向显示成空白, 而不是如实显示原值)
    hk_labels = _slice(_src(HK_TMPL), "function ledgerSide(", "async function loadLedger()")
    for side in ("BUY", "SELL", "SELL_SHORT", "BUY_BACK"):
        assert side in hk_labels, f"缺 {side} 的展示标签"
    assert "未见过的方向原样显示, 不猜" in hk_labels


def test_both_markets_expose_a_local_only_ledger_endpoint():
    """两市场都要有**本地只读**台账端点 (broker 挂了也能看历史), 且尺度/币种各自正确."""
    hk, us = _src(HK_ROUTES), _src(US_ROUTES)
    assert '@router.get("/api/hk/ledger")' in hk and '@router.get("/api/us/ledger")' in us
    assert 'window_for("HK"' in hk and 'window_for("US"' in us, "窗口必须由 SSOT 给, 不得各自算日期"
    assert '"HKD"' in hk and '"USD"' in us
    assert "gross_deal_amount_no_fee" in hk and "gross_deal_amount_no_fee" in us
    # 只读: GET 端点里不得出现 sync_all (回补必须走 POST, 否则刷新页面就打券商)
    assert "sync_all" not in _slice(hk, '@router.get("/api/hk/ledger")', '@router.post("/api/hk/ledger/sync")')
    assert "sync_all" not in _slice(us, '@router.get("/api/us/ledger")', '@router.post("/api/us/ledger/sync")')


def test_us_sync_endpoint_is_the_same_sync_all_as_hk():
    """综合账户下不存在"只补美股一腿" ⇒ 两个 POST 必须是同一个 sync_all, 不得各写一份."""
    us = _slice(_src(US_ROUTES), '@router.post("/api/us/ledger/sync")', "@router.get(\"/api/trade/attribution\")")
    # hk_trade.py 里台账回补是**最后一个**路由 (其后只有 include_router), 用它当结束锚点;
    # 旧锚点 "@router.post" 是 start 自身的前缀 ⇒ index 命中 i 本身 ⇒ 切出空串 (测试假绿/假红)
    hk = _slice(_src(HK_ROUTES), '@router.post("/api/hk/ledger/sync")', "app.include_router(router)")
    assert "live_ledger.sync_all" in us and "live_ledger.sync_all" in hk
    assert "两市场券商腿均不可达" in us and "两市场券商腿均不可达" in hk, \
        "全挂必须 503 且声明'不是零成交'; 一腿挂必须 PARTIAL 而不是 503 (否则好腿被坏腿掩盖)"


# ══════════════════════════════════════════════════════════════════
#  6. 批 D · 回合(券商真值派生) 的呈现契约
# ══════════════════════════════════════════════════════════════════
def test_batch_d_adds_one_line_not_a_second_table():
    """派生回合与成交表是**同一批钱的两种粒度** ⇒ 两页都只许加一行, 不许加第二张表.

    证伪: 往任一页塞 `roundtrip-tbody` / 第四张 `<table>` ⇒ 本条转红。
    """
    hk, us_tmpl, us_js = _src(HK_TMPL), _src(US_TMPL), _src(US_JS)
    for banned in ("roundtrip-tbody", "rt-tbody", "round-trips-table"):
        assert banned not in hk and banned not in us_tmpl and banned not in us_js, \
            f"批 D 出现了第二套回合表控件 {banned} (信噪比反向退化)"
    assert hk.count("<table") == hk.count('class="hk-stack"') == 3, \
        "港股页表数被批 D 改变了 (应仍是 持仓/今日订单/成交台账 三张)"
    assert us_tmpl.count("<table") == us_tmpl.count('class="pos-stack"') == 2
    # 港股: 回合行挂在既有的 ledger-ctx 里 (与成交表同一张卡, 不新开卡)
    hk_blob = _slice(hk, "function renderLedger(", "async function syncLedger()")
    assert "d.round_trips" in hk_blob and "券商真值派生 FIFO" in hk_blob
    assert "round_trip_panel" not in hk_blob, "前端不得自己拼派生视图, 只展示后端给的字段"
    # 美股: 回合行由 _usRtLine 给, 且**三个分支都要挂**(NO_DATA/非OK/OK),
    # 只挂 OK 分支会让"台账为空"时读者以为根本没有回合这一层
    calls = len(re.findall(r"(?<!function )_usRtLine\(d\)", us_js))
    assert calls == 3, f"美股页有分支漏挂回合行 (定义 1 处 + 调用应 3 处, 实测调用 {calls})"


def test_win_rate_gate_is_surfaced_not_bypassed():
    """胜率过闸门才给: 计数是事实、比率是推断 (实测小样本/低覆盖率都会算出 100% 假胜率)."""
    hk = _slice(_src(HK_TMPL), "function renderLedger(", "async function syncLedger()")
    us = _slice(_src(US_JS), "function _usRtLine(", "async function loadUsLedger()")
    for blob, name in ((hk, "港股页"), (us, "美股页")):
        assert "不给胜率" in blob, f"{name}未声明胜率闸门"
        # 两个闸门分支都要有处理: 样本量 (SMALL_SAMPLE) + 覆盖率 (scored_coverage)
        assert "SMALL_SAMPLE" in blob, f"{name}缺样本闸门分支"
        assert "scored_coverage" in blob, f"{name}缺覆盖率闸门分支"
        assert "小样本比率无信息量" in blob, f"{name}缺样本闸门的理由 (只写状态码读者看不懂)"
        assert "排除掉的往往正是亏损行" in blob, f"{name}缺覆盖率闸门的理由"
        assert "win_rate_pct" in blob and "rs.wins" in blob, \
            f"{name}必须给胜负计数(事实) + 过闸门时才给比率(推断)"
        assert "'%'" not in blob.split("win_rate_state === 'OK'")[0], \
            f"{name}在判闸门之前就拼了百分号 = 绕过闸门"


def test_two_ledger_layers_do_not_impersonate_each_other():
    """派生层(执行事实) 与 trade_records(决策记录) 必须自陈不是同一层.

    病灶: 派生层不断链但**没有决策理由**, 决策台账有理由但断链过 3 个月 ⇒ 若两层标题
    看起来一样, 读者会把派生的盈亏当成"有 exit_reason 可归因的回合"。
    """
    us = _slice(_src(US_JS), "function _usRtLine(", "async function loadUsLedger()")
    assert "不是同一层" in us and "trade_records" in us
    assert "决策理由未捕获" in us and "不把猜测写成事实" in us
    hk = _slice(_src(HK_TMPL), "function renderLedger(", "async function syncLedger()")
    assert "同一批钱的两种粒度" in hk, "港股页必须说明回合行与上面那张成交表不是两笔数据"
    assert "决策理由未捕获" in hk and "不把猜测写成事实" in hk
    # 未闭环批次(= 在仓) 必须在两页都出现: 否则"0 回合"会被读成"没有持仓"
    assert "不算回合" in us and "不算回合" in hk


def test_round_trip_line_reuses_the_same_read_only_endpoint():
    """回合行必须复用 `GET /api/{hk,us}/ledger`, 不得新开一个端点或多打一次券商."""
    hk_js = _slice(_src(HK_TMPL), "async function loadLedger()", "function renderLedger(")
    us_js = _slice(_src(US_JS), "async function loadUsLedger()", "function renderUsLedger(")
    assert hk_js.count("fetch(") == 1 and us_js.count("fetch(") == 1, \
        "回合行另开了一次请求 (白耗一次往返, 且两个请求可能给出不一致的两层数据)"
    assert "round_trip" not in hk_js and "round_trip" not in us_js, \
        "回合数据必须随台账端点一起来, 不得单独取"
    for path, name in ((HK_ROUTES, "港股"), (US_ROUTES, "美股")):
        src = _src(path)
        assert "round_trip_panel" in src, f"{name}端点未下发派生回合视图"
        blob = _slice(src, f'@router.get("/api/{"hk" if name == "港股" else "us"}/ledger")',
                      f'@router.post("/api/{"hk" if name == "港股" else "us"}/ledger/sync")')
        assert "sync_all" not in blob and "sync_round_trips" not in blob, \
            f"{name}的只读 GET 里出现了回补/派生写操作 (刷新页面就会打券商)"


def test_two_realized_calibers_are_labeled_and_reconciled():
    """同页出现两个"已实现"必须各自标出**配对口径**, 且不等时当场解释.

    实测 (2026-09-08, 港股 30 天窗口): 成交层 HK$49,200 (仅用窗口内成交配对) vs
    回合层 HK$59,775 (全量台账配对 + 按出场日归集) —— 差在跨窗持仓被窗口配对口径
    误标"基准不完整"而**整段排除**。两个数都"看起来合理", 不标口径读者只会挑一个信。
    """
    hk = _slice(_src(HK_TMPL), "function renderLedger(", "async function syncLedger()")
    us = _src(US_JS)
    for blob, name in ((hk, "港股页"), (us, "美股页")):
        assert "仅用本窗口成交配对" in blob, f"{name}未标成交层的配对口径"
        assert "全量台账配对" in blob, f"{name}未标回合层的配对口径"
        assert "不相等" in blob and "窗口口径偏低" in blob, \
            f"{name}两个已实现不等时未解释 (同页两个数而不解释 = 让读者自己挑一个信)"
        assert "为准" in blob, f"{name}未指明本窗口以哪个口径为准"
        assert "0.005" in blob, f"{name}的相等判定必须是数值容差, 不得用字符串比"
