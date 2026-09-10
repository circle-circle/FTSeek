# -*- coding: utf-8 -*-
"""交易界面批5 呈现层契约 (2026-09-09, 用户反馈「手机端持仓/订单都是一个正方框, 拥塞不美观,
要一条一条记录 + 自适应」⇒ A13: 移动端「堆叠方块」→「自适应记录行」).

A12 (批3) 把窄屏表格堆叠成「一条记录 = 一个带边框方块, 每列独占一行」: 持仓 7 列 / 交易记录
8 列 / 港股 9~10 列 ⇒ 手机上每条记录是 7~10 行的盒子, 一屏放不下两条, 且 label 左 value 右
被 flex 撕成两栏, 配对关系反而难读。批5 改成「一条记录 = 一行列表项」: 记录之间只留 1px 分隔线,
单元格走 CSS Grid auto-fit 自适应多列, 首列 (代码/标的) 作记录标题、每张表的关键列
(持仓盈亏 / 状态 / 盈亏 / 方向) 钉在标题行右端。

这一批的坏法全部**静默** (页面无报错, 只在手机上看得到), 故钉子按四类立:

  1. **方块回归**: 有人把 `tr` 的 `display: grid` 改回 `block`, 或重新给 `tr` 加边框/圆角/底色
     ⇒ 每条记录又变回盒子 = 本次返工的原因。钉 tr 规则的 display/分隔线, 并禁盒子三件套。
  2. **自适应丢失**: `repeat(auto-fit, minmax(...))` 被换成写死列数, 或 `.pos-stack` 的
     `min-width: 0` 被删 (全局 `table { min-width: 400px }` 立刻生效) ⇒ 窄屏横向滚动回来。
  3. **标题行抢格**: 关键列钉右端是按 **tbody id + data-label** 点名的 (同一个 stack 类下挂
     2~3 张表, 只按 data-label 点名会让两张表的关键列抢同一格, 而 grid **允许重叠** ⇒ 字压字)。
     钉: 右端格规则的 selector 必须以 `#<tbody id>` 开头、该 id 必须在模板里存在、
     其 data-label 必须在该表 thead 里存在 (改列名/加表当天就红)。
  4. **冗余行/空槽复活**: 无按钮的动作格 (券商行「已成交」文字, 与标题行状态列重复) 与空格子
     (无备注) 必须整格不渲染; 空态/错误态的 colspan 单元格必须回到整行且不出空白标签行。
"""
import pathlib
import re

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
HK_TMPL = PROJECT_ROOT / "app" / "web_dashboard" / "templates" / "hk_trade.html"
US_TMPL = PROJECT_ROOT / "app" / "web_dashboard" / "templates" / "trade.html"
US_JS = PROJECT_ROOT / "app" / "web_dashboard" / "static" / "js" / "trade.js"

GRID_COLS = "grid-template-columns: repeat(auto-fit, minmax(min(100%, 92px), 1fr))"

# 每张表标题行右端那格: (tbody id, 该表 thead 里必须存在的列名)
RIGHT_SLOT = {
    US_TMPL: [("posBody", "持仓盈亏"), ("ordersBodyTable", "状态")],
    HK_TMPL: [("pos-tbody", "盈亏"), ("ord-tbody", "状态"), ("ledger-tbody", "方向")],
}
# 记录标题列 (占满标题行左侧)
IDENTITY = {
    US_TMPL: [("posBody", "代码"), ("ordersBodyTable", "标的")],
    HK_TMPL: [("pos-tbody", "代码"), ("ord-tbody", "代码"), ("ledger-tbody", "代码")],
}


def _src(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _media_blocks(src: str) -> list:
    """所有 @media 块 (花括号配平切出), 供按内容定位, 不靠行号。"""
    blocks = []
    for m in re.finditer(r"@media[^{]*\{", src):
        i = m.end() - 1
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(src[m.start():j + 1])
                    break
    return blocks


def _mobile_css(src: str, marker: str) -> str:
    hit = [b for b in _media_blocks(src) if marker in b]
    assert hit, f"未找到含 {marker!r} 的 @media 块 (移动端记录行 CSS 被删了?)"
    return hit[0]


def _rules(block: str) -> list:
    """media 块内的一层规则 → [(selector, decls)] (规则内无嵌套, 注释先剥)。"""
    body = _strip_comments(block)
    body = body[body.index("{") + 1:body.rindex("}")]
    return [(sel.strip(), decls) for sel, decls in re.findall(r"([^{}@]+?)\s*\{([^{}]*)\}", body)]


def _rule(block: str, selector: str) -> str:
    for sel, decls in _rules(block):
        if sel == selector:
            return decls
    raise AssertionError(f"移动端 CSS 里找不到规则 {selector!r}")


def _th_labels(html: str, tbody_id: str) -> list:
    """该表 thead 的 <th> 文本序列 (与批3 同口径)。"""
    idx = html.index(f'id="{tbody_id}"')
    head = html.rindex("<thead>", 0, idx)
    seg = html[head:html.index("</thead>", head)]
    cells = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", seg, re.S)
    return [re.sub(r"<[^>]+>", "", c).strip() for c in cells]


PAGES = [(US_TMPL, "pos-stack"), (HK_TMPL, "hk-stack")]


@pytest.mark.parametrize("tmpl,cls", PAGES)
def test_record_row_is_list_line_not_box(tmpl, cls):
    """坏法1: 记录又变回带边框的盒子 (7~10 列 = 7~10 行高, 一屏放不下两条)。"""
    decls = _rule(_mobile_css(_src(tmpl), f".{cls} tr {{"), f".{cls} tr")
    assert "display: grid" in decls, "记录行必须是网格 (多列自适应), 不是 block 盒子"
    assert "border-bottom: 1px solid" in decls, "记录之间只留 1px 分隔线 (列表观感)"
    for banned in ("border-radius", "background:", "margin-bottom"):
        assert banned not in decls, f".{cls} tr 不得再出现盒子外观 {banned!r}"


@pytest.mark.parametrize("tmpl,cls", PAGES)
def test_columns_fluid_and_no_horizontal_scroll(tmpl, cls):
    """坏法2: 列数写死 / min-width 回来 ⇒ 窄屏横向滚动、宽屏不跟随。"""
    decls = _rule(_mobile_css(_src(tmpl), f".{cls} tr {{"), f".{cls} tr")
    assert GRID_COLS in decls, "列宽必须 auto-fit 自适应 (320px 2 列 → 768px 6~7 列)"
    if cls == "pos-stack":
        css = _mobile_css(_src(tmpl), f".{cls} tr {{")
        assert "table { min-width: 400px; }" in css, "前提变了这条钉子就失去意义, 请一并更新"
        assert re.search(r"\.pos-stack\s*\{\s*min-width:\s*0", css), \
            ".pos-stack 必须豁免全局 table min-width, 否则窄屏横向滚动回来"


@pytest.mark.parametrize("tmpl,cls", PAGES)
def test_right_slot_key_column_scoped_per_table(tmpl, cls):
    """坏法3: 标题行右端格未按表点名 ⇒ 两张表的关键列抢同一格 (grid 允许重叠 = 静默压字)。"""
    css = _mobile_css(_src(tmpl), f".{cls} tr {{")
    html = _src(tmpl)
    scoped = [(sel, decls) for sel, decls in _rules(css) if "grid-column: -2 / -1" in decls]
    assert scoped, "标题行右端必须有钉关键列的规则"
    for sel, decls in scoped:
        assert "grid-row: 1" in decls, f"{sel} 右端格必须钉在标题行"
        for part in sel.split(","):
            part = part.strip()
            assert part.startswith("#"), f"右端格规则必须按 tbody id 点名, 实际: {part!r}"
            tbody_id = part[1:part.index(" ")]
            assert f'id="{tbody_id}"' in html, f"模板里没有 tbody#{tbody_id} (改名未同步 CSS?)"
            label = re.search(r'data-label="([^"]*)"', part).group(1)
            assert label in _th_labels(html, tbody_id), \
                f"#{tbody_id} 的 thead 里没有 {label!r} 列 (改列名未同步 CSS?)"
    declared = {(tid, lab) for sel, _ in scoped
                for part in sel.split(",")
                for tid in [part.strip()[1:part.strip().index(" ")]]
                for lab in [re.search(r'data-label="([^"]*)"', part.strip()).group(1)]}
    assert declared == set(RIGHT_SLOT[tmpl]), \
        f"右端格点名与登记表不符: 多/少了 {declared ^ set(RIGHT_SLOT[tmpl])}"


@pytest.mark.parametrize("tmpl,cls", PAGES)
def test_identity_column_is_title_of_each_record(tmpl, cls):
    """标题列 (代码/标的) 必须真实存在于每张表的 thead, 且占满标题行左侧。"""
    css = _mobile_css(_src(tmpl), f".{cls} tr {{")
    html = _src(tmpl)
    decls = None
    for sel, d in _rules(css):
        if sel.startswith(f".{cls} td[data-label=") and "grid-row: 1" in d and "grid-column: 1 / -2" in d:
            decls = d
    assert decls is not None, "缺少『记录标题占满标题行左侧』的规则"
    for tbody_id, label in IDENTITY[tmpl]:
        assert label in _th_labels(html, tbody_id), f"#{tbody_id} thead 缺标题列 {label!r}"


@pytest.mark.parametrize("tmpl,cls", PAGES)
def test_empty_and_colspan_cells_leave_no_holes(tmpl, cls):
    """坏法4: 空格子/冗余格占位留空槽; 空态 colspan 格被网格撕开或带空白标签行。"""
    css = _mobile_css(_src(tmpl), f".{cls} tr {{")
    assert f".{cls} tbody > tr > td:empty {{ display: none; }}" in _strip_comments(css)
    assert f".{cls} td[colspan]::before {{ content: none; }}" in _strip_comments(css), \
        "空态/错误态行不得渲染出空白标签行"
    decls = _rule(css, f".{cls} td[colspan]")
    assert "grid-column: 1 / -1" in decls, "空态/错误态必须回到整行, 不能被网格撕开"


@pytest.mark.parametrize("tmpl,cls", PAGES)
def test_action_cell_only_renders_when_it_has_a_button(tmpl, cls):
    """「已成交」文字在手机上与标题行状态列重复 ⇒ 无按钮的动作格整格不渲染;
    有按钮时补到 36px 可点高度 (撤单/卖出是最紧急动作, 不能缩成 23px)。"""
    css = _strip_comments(_mobile_css(_src(tmpl), f".{cls} tr {{"))
    assert f".{cls} td[data-label=\"\"]:not(:has(button)) {{ display: none; }}" in css
    assert f".{cls} td[data-label=\"\"] button {{ min-height: 36px" in css


@pytest.mark.parametrize("tmpl,cls", PAGES)
def test_labels_still_come_from_data_label_single_render_path(tmpl, cls):
    """单一渲染路径: 标签仍取 td[data-label] (A12 口径), 不得另写一套移动端 DOM/JS 渲染。"""
    css = _src(tmpl)
    assert f".{cls} td::before {{ content: attr(data-label)" in css
    js = _src(US_JS)
    for banned in ("pos-card", "posCards", "renderMobileCards", "matchMedia"):
        assert banned not in js, f"trade.js 出现第二套移动端渲染痕迹: {banned}"
