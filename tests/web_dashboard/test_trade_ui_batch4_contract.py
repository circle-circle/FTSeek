# -*- coding: utf-8 -*-
"""交易界面批4 契约 (2026-09-08, REVIEW-20260908 §H: 用户裁「理由非必填」+ 三项代裁落地).

批4 = **裁决落地批**。与批1-3 不同, 这一批动到了后端 (`.py` ⇒ 需重启), 且三项里有两项
是"删掉一个恒真/恒假的分支"与"改一张查表" —— 坏法比前几批更隐蔽: 删完看着更干净, 但
前提一旦回滚 (PDT 重新启用 / 券商改档位语义) 就静默失去防护; 门面签名漂移更是已经
**潜伏 3 个月**没人发现。故钉子按四类立:

  1. **去阻断不得顺手去掉别的防线** (B11): 平仓理由改非必填 = 删掉一处 `return`,
     但数量校验、后端兜底留痕、"chips 只写 input" 的单一真源必须逐条保住 ——
     否则"少填一个框"会变成"少一道校验"或"留痕变空串"。
  2. **门面签名漂移** (平仓记账断链): `executor.py` 自 2026-06-19 起传 `entry_reason=`,
     而 `ManualOrderService._record_manual_close` 委派一直没这个形参 ⇒ 每次手动平仓抛
     TypeError 被降级成一行 warning ⇒ **券商已平仓、账本零记录** (实证 trade_records 中
     `exit_reason LIKE '[%'` = 0 行)。这类坏法靠肉眼审 diff 抓不到 (两边各自都"看着对"),
     故钉子直接**遍历调用点 × 比对签名**, 覆盖 order/ 下所有 `service._xxx(...)` 委派。
  3. **退役的前提必须被钉住** (PDT): 删掉恒放行分支的前提是 `pdt_guard.enabled=false`。
     若哪天为 moomoo US 实体把它改回 true, 调用点已经没了 = 静默失去防护 ⇒ 钉子把
     "前提"本身钉死, 并在断言消息里写清回滚时必须做什么 (让下一个读者被拦下来而不是踩空)。
  4. **跨页一套词表** (风险九档): 主看板 `_parse_risk_level` 与港股页 `hkRiskBadge()`
     读的是**同一个富途枚举**, 两处各写一张表 ⇒ 必须逐档同标签、同严重度带,
     否则同一账户在两个页面一个说"较安全"一个说"警告"(正是本批改掉的病)。
"""
import ast
import inspect
import pathlib
import re

import pytest
import yaml

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
US_TMPL = PROJECT_ROOT / "app" / "web_dashboard" / "templates" / "trade.html"
HK_TMPL = PROJECT_ROOT / "app" / "web_dashboard" / "templates" / "hk_trade.html"
US_JS = PROJECT_ROOT / "app" / "web_dashboard" / "static" / "js" / "trade.js"
US_ROUTES = PROJECT_ROOT / "app" / "web_dashboard" / "routes" / "trade.py"
DASH_ROUTES = PROJECT_ROOT / "app" / "web_dashboard" / "routes" / "dashboard.py"
VALIDATOR = PROJECT_ROOT / "app" / "web_dashboard" / "order" / "validator.py"
ORDER_DIR = PROJECT_ROOT / "app" / "web_dashboard" / "order"
SVC = PROJECT_ROOT / "app" / "web_dashboard" / "manual_order_service.py"
PDT_GUARD = PROJECT_ROOT / "app" / "risk" / "pdt_guard.py"
CONFIG = PROJECT_ROOT / "config" / "unified_config.yaml"


def _src(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def _identifiers(src: str) -> str:
    """AST 里所有**标识符** (去注释、去 docstring、去字符串字面量) 拼成一个小写串。

    退役说明写在注释/docstring 里是刻意的留痕 (下一个读者要知道为何这里没有 PDT),
    只有**代码**里重新出现 import/调用才算复活 ⇒ 负面断言必须查标识符而不是查全文。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names += [a.name for a in node.names] + [getattr(node, "module", "") or ""]
    return " ".join(names).lower()


def _js_code(js: str) -> str:
    """去掉整行 `//` 注释 —— 注释里引用"被删掉的那道拦截"是取证/留痕, 不算复活。"""
    return "\n".join(l for l in js.splitlines() if not l.strip().startswith("//"))


# ══════════════════════════════════════════════════════════════
#  1. B11 平仓理由非必填 (用户裁决) — 去阻断不得顺带去掉防线
# ══════════════════════════════════════════════════════════════

def test_close_reason_is_no_longer_mandatory():
    """B11: 「请填写理由」这道拦截必须消失, 且 executeClose 不得因空理由 return。"""
    js = _src(US_JS)
    exe = _slice(js, "async function executeClose()", "function showResult(")
    assert "请填写理由" not in _js_code(js), "理由必填拦截未删净 (用户裁: 非必填)"
    assert "!reason" not in exe, "不得残留任何以空理由为由的分支"
    # 既有防线逐条保住 (去阻断 ≠ 去校验)
    assert "数量无效" in exe, "数量校验是既有防线, 不得随理由拦截一起被删"
    assert "closeQty > absQty" in exe


def test_close_reason_empty_still_leaves_audit_trail():
    """B11: 前端可以送空串, 但**留痕不得为空** —— 后端必须兜底成「手动平仓」。

    `body.get("reason", 默认)` 只在键**缺失**时兜底, 前端送 `reason: ""` 会穿透成
    "[平仓] " (空留痕) ⇒ 必须用 `or` 兜底。这一条是"去摩擦"与"不失真"的接缝。
    """
    routes = _src(US_ROUTES)
    assert 'body.get("reason", "手动平仓")' not in routes, "键缺失才兜底, 空串会穿透成空留痕"
    assert re.search(r'reason=\(str\(body\.get\("reason"\) or ""\)\.strip\(\) or "手动平仓"\)', routes), \
        "空/缺键必须统一落「手动平仓」, 保证 trade_records.exit_reason 留痕不断"


def test_close_reason_chips_are_single_source_and_non_blocking():
    """B11: chips 只写 `closeReasonInput.value` ⇒ 提交路径逐字不变 (不引入第二套真源)。"""
    js, html = _src(US_JS), _src(US_TMPL)
    assert "const CLOSE_REASONS = ['止损触发', '止盈达成', '计划变更', '风控降敞口'];" in js
    bind = _slice(js, "function bindCloseReasonChips()", "// ── Symbol Intelligence")
    assert "inp.value = btn.dataset.reason" in bind, "chip 点击必须写回 input (单一真源)"
    for forbidden in ("disabled", "executeClose", "errEl", "showResult"):
        assert forbidden not in bind, f"chips 只是填字, 不得触碰提交/禁用/报错: {forbidden}"
    exe = _slice(js, "async function executeClose()", "function showResult(")
    assert "closeReasonInput" in exe and "CLOSE_REASONS" not in exe, \
        "提交仍只读 input; 若改读 chip 状态 = 两套真源, 清空 input 就会送出陈旧理由"
    # 弹窗入口: 普通平仓不预填文本, SL/TP 入口预选「止损触发」
    assert "renderCloseReasonChips('')" in _slice(js, "function closePos(", "// 加载入场 thesis")
    at_price = _slice(js, "function closePosAtPrice(", "// ── B3")
    assert "renderCloseReasonChips('止损触发')" in at_price
    # 模板侧: 标为可选 + 兜底口径写在 placeholder 上 (不靠用户猜)
    assert "<label>理由（可选）</label>" in html
    assert 'id="closeReasonChips"' in html and "留空 = 手动平仓" in html
    assert ".rec-chip.on" in html, "选中态缺 CSS ⇒ chips 点了没有视觉反馈"
    assert "bindCloseReasonChips();" in js, "监听未挂 ⇒ chips 是死的"


# ══════════════════════════════════════════════════════════════
#  2. 门面签名漂移 (平仓记账断链, 潜伏 3 个月的静默失败)
# ══════════════════════════════════════════════════════════════

def test_manual_close_delegate_accepts_entry_reason():
    """根因钉: executor 传 entry_reason=, 委派层必须收 —— 否则 TypeError 被 warning 吞掉。"""
    from app.web_dashboard.manual_order_service import ManualOrderService
    from app.web_dashboard.order.audit import record_manual_close

    sig = inspect.signature(ManualOrderService._record_manual_close)
    assert "entry_reason" in sig.parameters, "委派签名漂移 ⇒ 手动平仓账本静默断链"
    # 委派必须**真的往下传** (收了不用 = 另一种断链: entry_reason 永远为空)
    body = _slice(_src(SVC), "def _record_manual_close", "def _mark_position_manual")
    assert "entry_reason=entry_reason" in body
    # 下游确实消费它 (否则传了也是 write-only)
    audit_src = _src(PROJECT_ROOT / "app" / "web_dashboard" / "order" / "audit.py")
    fn = _slice(audit_src, "def record_manual_close", "\ndef ")
    assert "entry_reason" in fn and fn.count("entry_reason") >= 2
    assert "entry_reason" in str(inspect.signature(record_manual_close))


def test_all_service_delegate_calls_match_signatures():
    """**同类坏法一网打尽**: order/ 下所有 `service._xxx(...)` 调用点 × 委派签名逐一对表。

    这条钉子是上一条的泛化 —— 签名漂移靠审 diff 抓不到 (调用点与委派各自都"看着对"),
    只有把两边放一起比才炸。用 `bind_partial(**kwargs)` 只检"是否接受这些关键字",
    不检类型/位置参数, 因此对任意调用点都安全。
    """
    from app.web_dashboard.manual_order_service import ManualOrderService

    checked, names = 0, set()
    for py in sorted(ORDER_DIR.glob("*.py")):
        tree = ast.parse(_src(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    and fn.value.id == "service"):
                continue
            name = fn.attr
            meth = getattr(ManualOrderService, name, None)
            if meth is None or not callable(meth):
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            if not kwargs:
                continue
            try:
                inspect.signature(meth).bind_partial(**{k: None for k in kwargs})
            except TypeError as exc:
                pytest.fail(f"{py.name}:{node.lineno} service.{name}({', '.join(sorted(kwargs))}) "
                            f"与委派签名不符 ⇒ 运行期 TypeError 会被 except 吞成静默失败: {exc}")
            names.add(name)
            checked += 1
    assert "_record_manual_close" in names, f"必须覆盖到出事的那个委派, 实测 {{sorted(names)}} ⇒ 遍历失效"
    assert checked >= 2, f"调用点覆盖数异常偏低 ({checked}) ⇒ 钉子空转"


# ══════════════════════════════════════════════════════════════
#  3. PDT 死分支退役 — 退役的前提必须被钉住
# ══════════════════════════════════════════════════════════════

def test_pdt_dead_branch_retired_from_manual_validator():
    """用户裁「PDT 在当前富途不存在」: 恒放行分支必须删净 (含 import), 注释可留出处。"""
    src = _src(VALIDATOR)
    assert "pdt" not in _identifiers(src), "validator 代码体内不得残留 PDT 逻辑 (裁决: 富途香港不适用)"
    assert "PDT Guard 已退役" in src, "退役必须留痕 (含裁决出处与证据链), 不得静默消失"
    # 过期注释一并更正: 原文案声称"仅保留 PDT + 仓位 + 回撤", 两者其实都已不在
    # 钉**整句**原文案 (更正注释里引用了它的片段作取证, 故不能只钉片段 —— 同款坑这批踩了三次)
    assert "仅保留 PDT + 仓位 + 回撤 等风控检查" not in src, "过期整句未更正"
    assert "6. PDT Guard (实盘 only)" not in src, "docstring 检查清单未更正"


def test_pdt_retirement_premise_is_pinned():
    """前提钉: 退役之所以安全, 是因为 `pdt_guard.enabled=false` 让调用点恒放行。

    若为 moomoo US 实体把开关改回 true, **调用点已经没了** ⇒ 会静默失去 PDT 防护。
    这条钉子故意在开关翻转时炸, 逼下一个改动者同时把调用点接回去 (而不是踩空)。
    """
    cfg = yaml.safe_load(_src(CONFIG))
    enabled = (cfg.get("risk", {}).get("pdt_guard", {}) or {}).get("enabled", False)
    assert enabled is False, (
        "risk.pdt_guard.enabled 已翻回 true, 但 validator.py 的 PDT 调用点已于 2026-09-08 "
        "退役 (REVIEW §H) ⇒ 必须同时把调用点接回 validate_order(), 否则 PDT 形同不存在"
    )
    guard = _src(PDT_GUARD)
    assert "def _is_enabled" in guard and "if not self._is_enabled():" in guard, \
        "pdt_guard 本体必须保留 (退役的是调用点, 不是能力)"


# ══════════════════════════════════════════════════════════════
#  4. 富途风险九档口径 — 两页一套词表
# ══════════════════════════════════════════════════════════════

def _dash_levels() -> dict:
    src = _src(DASH_ROUTES)
    fn = _slice(src, "def _parse_risk_level", "\ndef ")
    return {int(n): (label, color) for n, label, color in
            re.findall(r'"LEVEL(\d)": \{"label": "([^"]+)", "color": "(\w+)"', fn)}


def _hk_levels() -> dict:
    src = _src(HK_TMPL)
    tbl = _slice(src, "const HK_RISK_STATUS_CN", "const HK_RISK_SEV")
    return {int(n): (label, sev) for n, label, sev in
            re.findall(r"LEVEL(\d): \['([^']+)', '(\w+)'\]", tbl)}


def test_dashboard_risk_levels_follow_futu_nine_grades():
    """主看板口径更正: LEVEL1 最安全 ⇒ LEVEL9 最危险 (原表把 LEVEL3 当「警告」= 假警报,
    且 LEVEL6-9 全落 default gray = 真危险反而不报警, 两头都错)。"""
    levels = _dash_levels()
    assert sorted(levels) == list(range(1, 10)), "必须是完整九档, 缺档就会落 gray「未知」"
    labels = [levels[n][0] for n in range(1, 10)]
    assert labels == ["非常安全", "安全", "较安全", "较低风险", "中等风险",
                      "偏高风险", "预警", "危险", "危险"], f"标签必须逐字用富途官方口径: {labels}"
    # 严重度单调 (green < yellow < orange < red), 反序 = 把最安全染红
    rank = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
    colors = [levels[n][1] for n in range(1, 10)]
    assert [rank[c] for c in colors] == sorted(rank[c] for c in colors), f"色阶必须单调: {colors}"
    assert colors[:3] == ["green"] * 3 and colors[-2:] == ["red"] * 2
    # 旧的错误词表必须删净 (留着就会被下一个人当"另一套口径"复活)
    table = _slice(_src(DASH_ROUTES), "risk_levels = {", "\n    }")
    for wrong in ('"强平"', '"注意"', '"接近维持保证金"'):
        assert wrong not in table, f"旧错表残留 (docstring 里引用作取证不算): {wrong}"


def test_risk_wording_is_identical_across_pages():
    """同一个富途枚举、两处查表 ⇒ 逐档必须同标签同严重度带, 否则两页各说各话。"""
    dash, hk = _dash_levels(), _hk_levels()
    assert sorted(dash) == sorted(hk) == list(range(1, 10))
    band = {"green": "ok", "yellow": "warn", "orange": "warn", "red": "bad"}
    for n in range(1, 10):
        assert dash[n][0] == hk[n][0], f"LEVEL{n} 标签两页不一致: 看板={dash[n][0]} / 港股={hk[n][0]}"
        assert band[dash[n][1]] == hk[n][1], f"LEVEL{n} 严重度带不一致: {dash[n][1]} vs {hk[n][1]}"


def test_unknown_risk_stays_neutral_on_both_pages():
    """未知 ≠ 危险: 看板 N/A/新档位 → gray + 原样透出; 港股页 → 中性灰 (批3 已钉, 此处对齐口径)。"""
    from app.web_dashboard.routes.dashboard import _parse_risk_level
    for raw in ("N/A", "NONE", "LEVEL12", ""):
        detail = _parse_risk_level({"risk_status": raw, "interest_charged": 0})["detail"]
        assert detail["color"] == "gray", f"{raw!r} 必须中性灰, 实测 {detail['color']}"
        assert detail["label"] == raw, f"{raw!r} 必须原样透出而非编造标签"
    assert _parse_risk_level({})["detail"]["color"] == "gray"
    assert _parse_risk_level(None)["detail"]["color"] == "gray"
    hk = _src(HK_TMPL)
    assert "HK_RISK_UNKNOWN" in hk and "sev: worst ? worst.hit[1] : ''" in hk


def test_live_payload_level3_is_not_an_alarm():
    """实测锚: 2026-09-08 线上账户 `risk_status=LEVEL3` (现金 17.7 万/购买力 55.5 万, 健康)
    曾被渲染成「警告 / orange / 接近维持保证金」。更正后必须是绿色「较安全」。"""
    from app.web_dashboard.routes.dashboard import _parse_risk_level
    detail = _parse_risk_level({"risk_status": "LEVEL3", "interest_charged": 61694.16})["detail"]
    assert detail == {"label": "较安全", "color": "green",
                      "desc": "有杠杆但流动性充裕，无需动作"}, detail


# ══════════════════════════════════════════════════════════════
#  5. 发布口径
# ══════════════════════════════════════════════════════════════

def test_cache_version_bumped_for_batch4():
    """批4 又改了 trade.js ⇒ /static/ 30 天 immutable, 必须再 bump。"""
    m = re.search(r'src="/static/js/trade\.js\?v=(\S+?)"', _src(US_TMPL))
    assert m and m.group(1) >= "20260908d", f"批4 改了 trade.js, 版本号必须 bump: {m and m.group(1)}"
