"""交易界面批2 呈现层契约 (2026-09-08, REVIEW-20260908 §E.6 批2: A4/A5/A6/B4/B5/B6).

批2 全部是前端呈现层改动 (模板内联 JS + trade.js + CSS), 没有后端判据可测。
但这一类改动有四种**静默坏法**, 肉眼与手工点测都很容易漏, 故在此立钉子:

  1. 表格列数错位 (B6 给美股持仓表加了一列): thead 的 <th> 数与空态/错误态的
     colspan 不一致 ⇒ 表格整体错位, 但页面不报错、功能"看起来还能用"。
  2. 枚举中文化丢信息 (A4): 券商新增/未收录的枚举被映射成空字符串 ⇒ 实盘订单
     状态在界面上凭空消失。故要求**未收录一律原样透出**, 并用 node 真跑一遍。
  3. 文案与实际口径不符 (B5): 横幅曾硬编码 "15s 内变动", 而实际比较窗口盘中是 5s
     ⇒ 用户对"恶化速度"的判断被系统性放大 3 倍。数字对、时间标错也是错。
  4. 缓存版本号丢失 (C2): /static/ 是 max-age=2592000 immutable (app.py:184),
     script 标签漏掉 ?v= ⇒ 手机上最长 30 天看不到新代码, 且会被误判为"修复无效"。

另钉一条纪律: A5「同标的当日累计」是**信息呈现**, 不得升级为阻断/确认门槛
(港股按设计不接系统风控; 用户 2026-07-22 已裁"去掉 CONFIRM 与理由必填, 降低
手动下单摩擦")。前端唯一允许阻断提交的校验是 A1 标的错配。
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


def _hk_src() -> str:
    return HK_TMPL.read_text(encoding="utf-8")


def _us_js() -> str:
    return US_JS.read_text(encoding="utf-8")


# ── 1. 列数一致性 (B6) ────────────────────────────────────────

def _thead_cols(html: str, tbody_id: str) -> int:
    """取 tbody_id 所在表格的 thead <th> 数量。"""
    idx = html.index(f'id="{tbody_id}"')
    head = html.rindex("<thead>", 0, idx)
    seg = html[head:idx]
    return len(re.findall(r"<th[\s>]", seg))


def _colspans_for(html: str, tbody_id: str) -> list:
    """取该表格 tbody 起始行 + JS 里针对该 tbody 的所有 colspan。"""
    idx = html.index(f'id="{tbody_id}"')
    row = html[idx:html.index("</tr>", idx)]
    return [int(n) for n in re.findall(r'colspan="(\d+)"', row)]


def test_us_positions_table_columns_match_colspan():
    """B6: 加"距止损/SL"列后, thead 与空态 colspan 必须同步 (否则整表错位)。"""
    html = US_TMPL.read_text(encoding="utf-8")
    cols = _thead_cols(html, "posBody")
    assert cols == 7, f"美股持仓表应有 7 列 (含新增距止损), 实测 {cols}"
    assert _colspans_for(html, "posBody") == [7]
    # JS 侧的空态与错误态行也必须同步
    js = _us_js()
    assert 'colspan="6"' not in js, "trade.js 仍有 colspan=6 — B6 加列后未同步, 表格会错位"
    assert js.count('colspan="7"') >= 2, "trade.js 的空态/错误态行应各有一处 colspan=7"


def test_hk_tables_columns_match_colspan():
    """港股三张表同样钉住 (A4 只改文案不得改列数; RFC-078 批B 又加了第三张台账表)。"""
    html = _hk_src()
    for tbody_id, expected in (("pos-tbody", 10), ("ord-tbody", 10), ("ledger-tbody", 9)):
        assert _thead_cols(html, tbody_id) == expected, tbody_id
        assert set(_colspans_for(html, tbody_id)) == {expected}, tbody_id
    # JS 内联在同文件: 每个 colspan 都必须等于**它所属那张表**的列数。
    # 写死 {10} 在加表时会假红, 只写 {9,10} 则挡不住"台账空态行套了持仓表列数"(= 错位)
    # ⇒ 逐处判定: colspan==9 当且仅当它落在台账表自己的区域 (HTML 骨架 / 台账 JS 块)。
    ledger_zones = [
        (html.index("📒 实盘成交台账"),
         html.index("</table>", html.index('<tbody id="ledger-tbody">'))),
        (html.index("// ── RFC-078 批B: 实盘成交台账"),
         html.index("async function syncLedger()")),
    ]
    seen = set()
    for m in re.finditer(r'colspan="(\d+)"', html):
        n = int(m.group(1))
        seen.add(n)
        in_ledger = any(a <= m.start() < b for a, b in ledger_zones)
        assert (n == 9) == in_ledger, \
            f'colspan="{n}" 落在{"台账(应 9)" if in_ledger else "持仓/订单(应 10)"}表区域 ⇒ 空态行错位'
    assert seen == {9, 10}, seen


def test_us_sl_column_renders_stop_price_and_distance():
    """B6: 新列必须同时给"距止损%"与"SL 价位", 且 ATR 估算值显式标注 (不伪装成真实 SL)。"""
    js = _us_js()
    assert "slCell" in js and "${slCell}" in js
    assert "sl_distance_pct.toFixed(1)" in js
    assert "stop_loss" in js
    assert "atr_estimated" in js, "ATR 估算的 SL 必须标注, 否则把估算值当权威值呈现"


# ── 2. 枚举中文化 fail-safe (A4) ──────────────────────────────

def _hk_enum_js_block() -> str:
    """切出 esc + 三张字典 + 三个映射函数, 供 node 真跑。"""
    src = _hk_src()
    esc_start = src.index("function esc(s) {")
    esc_end = src.index("\n}\n", esc_start) + 3
    dict_start = src.index("const HK_SIDE_CN")
    dict_end = src.index("// ── A5 (", dict_start)
    return src[esc_start:esc_end] + "\n" + src[dict_start:dict_end]


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_hk_enum_maps_known_values_and_never_drops_unknown():
    """A4 核心纪律: 已知枚举中文化, **未收录枚举原样透出** (绝不渲染成空)。"""
    harness = _hk_enum_js_block() + """
const out = {
  side: ['BUY', 'SELL', 'SELL_SHORT', 'BUY_BACK', 'SOME_NEW_SIDE'].map(hkSide),
  otype: ['NORMAL', 'MARKET', 'ENHANCED_LIMIT', 'SOME_NEW_TYPE'].map(hkOType),
  status: ['SUBMITTED', 'FILLED_PART', 'FILLED_ALL', 'CANCELLED_ALL', 'FAILED',
           'SOME_NEW_STATUS'].map(hkStatus),
};
console.log(JSON.stringify(out));
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr[:800]
    out = json.loads(res.stdout.strip().splitlines()[-1])

    assert out["side"][:4] == ["买入", "卖出", "卖空", "买回平空"]
    assert out["side"][4] == "SOME_NEW_SIDE", "未收录方向必须原样透出"
    assert out["otype"][:3] == ["限价", "市价", "增强限价"]
    assert out["otype"][3] == "SOME_NEW_TYPE"
    # 状态返回的是带色 pill HTML: 已知的走中文, 未知的必须仍含原始枚举串
    assert "已报" in out["status"][0] and "部分成交" in out["status"][1]
    assert "已成" in out["status"][2] and "已撤" in out["status"][3] and "废单" in out["status"][4]
    assert "SOME_NEW_STATUS" in out["status"][5], "未收录状态不得被吞成空 pill"
    assert all(s.strip() for s in out["status"]), "任何状态都不得渲染成空"


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_hk_enum_status_colors_are_differentiated():
    """A4: 五种状态族必须真的配色不同 (已报黄/部分成交蓝/已成绿/已撤灰/废单红)。"""
    harness = _hk_enum_js_block() + """
console.log(JSON.stringify(['SUBMITTED','FILLED_PART','FILLED_ALL','CANCELLED_ALL','FAILED'].map(hkStatus)));
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr[:800]
    pills = json.loads(res.stdout.strip().splitlines()[-1])
    classes = [re.search(r'class="st ([\w-]+)"', p).group(1) for p in pills]
    assert classes == ["st-wait", "st-part", "st-done", "st-off", "st-bad"]
    assert len(set(classes)) == 5, "五族状态必须可视觉区分"


def test_hk_cancel_gate_still_uses_raw_status():
    """A4 副作用防线: 撤单可用性判断必须继续用**原始**枚举, 不得改用中文化后的文案。"""
    src = _hk_src()
    assert "CANCELLABLE.some(s => String(o.status).toUpperCase().includes(s))" in src


# ── 3. 文案口径 (B5) 与噪声 (B4) ──────────────────────────────

def test_banner_window_is_measured_not_hardcoded():
    """B5: 横幅不得再硬编码 "15s", 必须用实测渲染间隔。"""
    js = _us_js()
    # 断言打在**文案构造处**而非整文件: 解释性注释里会引用旧文案, 否则会假阳
    assert "盈亏快速恶化，15s" not in js, "横幅仍硬编码 15s — 实际比较窗口盘中是 5s"
    assert re.search(r"盈亏快速恶化，近 ' \+ winSecs \+ 's 变动", js), "横幅应改用实测窗口 winSecs"
    assert "_snapshotWindowSecs" in js
    assert "近 ' + winSecs + 's 变动" in js
    # 打点必须在本轮渲染完成之后, 否则窗口恒为 0
    tail = js[js.index("updateAttentionBanner(positions);"):]
    assert "_lastSnapshotAt = Date.now()" in tail[:200]


def test_neutral_trend_arrow_removed():
    """B4: 恒亮的"→ 横盘"零信息量, 必须不再渲染; 有向箭头保留并标注窗口。"""
    js = _us_js()
    assert 'title="横盘"' not in js
    assert "→</span>" not in js
    assert "盈利改善" in js and "盈利恶化" in js, "有信息量的 ↑/↓ 不得一并删掉"
    assert "trendWin" in js, "箭头必须标注真实比较窗口"


# ── 4. 缓存版本号 (C2) ────────────────────────────────────────

def test_trade_js_has_date_cache_buster():
    """/static/ 是 immutable 30 天缓存 ⇒ script 标签必须带日期型 ?v=。"""
    html = US_TMPL.read_text(encoding="utf-8")
    m = re.search(r'src="/static/js/trade\.js\?v=(\S+?)"', html)
    assert m, "trade.js 的 script 标签丢了 ?v= — 手机最长 30 天看不到新代码"
    assert re.fullmatch(r"\d{8}[a-z]", m.group(1)), f"版本号应为 YYYYMMDD+字母, 实测 {m.group(1)}"
    assert m.group(1) >= "20260908b", f"批2 改了 trade.js, 版本号必须 bump, 实测 {m.group(1)}"


# ── 5. 纪律: A5 只呈现, 不阻断 ────────────────────────────────

def test_today_context_is_presentation_only():
    """A5 不得升级成闸门: 上下文行不参与提交拦截, 也不得出现确认关键词。"""
    src = _hk_src()
    body = src[src.index("async function submitOrder()"):]
    body = body[:body.index("\n// 提交/撤单后短时高频轮询")]
    assert "today-ctx" not in body and "renderTodayCtx" not in body
    assert "CONFIRM" not in body, "用户 2026-07-22 已裁去掉 CONFIRM 关键词"
    # 唯一允许的前端阻断仍是 A1 标的错配
    assert body.count("return;") >= 1 and "symbolDirty" in body


def test_a5_aggregation_uses_only_existing_readonly_data():
    """A5 数据源只能是已有的 ordersCache/positionsCache (不得为此新开写路径或新端点)。"""
    src = _hk_src()
    fn = src[src.index("function renderTodayCtx()"):src.index("// ── A2: 提交前预检条")]
    assert "ordersCache" in fn and "positionsCache" in fn
    assert "fetch(" not in fn, "A5 是纯前端聚合, 不得自己发请求"
    assert "disabled" not in fn, "A5 只呈现不裁决"
