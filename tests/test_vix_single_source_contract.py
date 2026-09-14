"""VIX 单一真源 + fail-close 可达性钉子 (2026-09-14, 用户裁「全部处理」P0-1)。

病根 (盘前数据有效性审计, 两处独立病灶同源于"VIX 取值口径各写一份"):

① **守卫纸面化**: `app/analysis/market_state.py::_fetch_vix_data` 末行原为
   `return 20.0  # 合理默认值` ⇒ 该函数**永不返回 None** ⇒ `get_vix_cached()`
   文档承诺的 "取不到返回 None (fail-close 友好)" 不可能成立 ⇒ 两处守卫分支
   `app/pipeline/order_executor.py`(下单前) 与 `app/playbook/playbook_service.py`
   (Playbook 开仓前) 的 `if vix is None: 拒绝新仓` **恒不可达**。实测后果:
   VIX 源全挂时 VIXGuard 看到兜底 20.0 (<28) → 不拦截, 即"看不到就放行",
   与该守卫注释的"宁可错过不可放错"完全相反 (同型: 回撤熔断静默失效案)。

② **口径分裂**: 执行门走 FRED VIXCLS 日频 (实测 2026-09-14 序列最新仅到 09-10
   ⇒ T-1/T-2 收盘值), 而 regime 侧 `app/playbook/market_regime.py` 走
   Futu VIXY×0.85 实时代理 (2026-08-03 修复) ⇒ 同一天的 VIX 门读两个口径,
   盘中 VIX 冲高在执行门上不可见。另, 0.85 系数此前复制在 router /
   alphavantage_provider / vix_monitor 三处, 任一漂移即口径再次分裂。

本文件钉住的不变量:
  1. `_fetch_vix_data()` 全源失败**如实返回 None** —— 严禁恢复任何数值兜底
     (20.0/18.0 之类); 兜底只能由调用方按各自语义显式声明;
  2. `get_vix_cached()` 如实传播 None ⇒ 两处 fail-close 分支真实可达;
  3. VIXY→VIX 换算仅有 `app/data/vix_source.py::vixy_to_vix` 一个入口,
     三个原复制点不得再出现字面量换算;
  4. 盘前/盘中优先 VIXY 实时代理 (与 router 同条件), 且**禁止直查 US.VIX**
     (富途不供指数快照, 实测 `_error: empty`; DEB-20260818 §89/§144);
  5. 显式 fail-open 的两处调用方 (`vix_below_25` / `get_vix_regime`) 语义不变。

回滚: 删本文件 + 还原 market_state._fetch_vix_data 末行 `return 20.0` 与三处
      字面量换算 (回滚须另开辩论 —— 恢复兜底假值等于让 fail-close 再次纸面化)。
"""
import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _src(rel):
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _func_node(rel, func_name):
    """按名取函数 AST 节点 (dotted 名取末段)。"""
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            return node
    raise AssertionError(f"{rel} 中找不到函数 {func_name}")


def _code_strings(node):
    """函数体内出现的字符串字面量 (docstring 除外) —— 用于禁止直查死符号。"""
    doc = ast.get_docstring(node, clean=False)
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if doc is not None and sub.value == doc:
                continue
            out.add(sub.value)
    return out


def _mult_by_factor(node, factor=0.85):
    """函数体内 `x * 0.85` 形式的字面量换算 (AST 级, 不误伤注释/锚点常量)。"""
    hits = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mult):
            for side in (sub.left, sub.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, float) \
                        and abs(side.value - factor) < 1e-9:
                    hits.append(sub.lineno)
    return hits


def _return_consts(node):
    """函数体内 `return <常数>` 的常数值集合 (docstring 里的示例不算)。"""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
            out.add(sub.value.value)
    return out


# ── 1. 换算唯一入口 (行为) ──────────────────────────────────────────────

class TestVixyToVix:
    def test_conversion_is_applied_and_rounded(self):
        from app.data.vix_source import vixy_to_vix
        assert vixy_to_vix(20.0) == 17.0
        assert vixy_to_vix(17.3) == 14.71  # 2026-09-14 实测 VIXY 报价

    @pytest.mark.parametrize("bad", [None, 0, 0.0, -1.0, "", "abc"])
    def test_invalid_input_returns_none_never_a_fallback(self, bad):
        """无效输入必须 None —— 绝不兜底成数值 (否则换算层又变成假值源)。"""
        from app.data.vix_source import vixy_to_vix
        assert vixy_to_vix(bad) is None

    def test_factor_is_single_constant(self):
        from app.data import vix_source
        assert vix_source.VIXY_CONTANGO_FACTOR == 0.85


# ── 2. 三处原复制点不再自带字面量换算 (源码棘轮) ─────────────────────────

class TestNoDuplicatedConversion:
    # (文件, 原换算所在函数)
    _SITES = (
        ("app/data/market_data_router.py", "get_vix_or_none"),
        ("app/data/alphavantage_provider.py", "_fetch_finnhub_vixy"),
        ("app/risk/vix_monitor.py", "_fetch_vix_finnhub"),
    )

    @pytest.mark.parametrize("rel,func", _SITES)
    def test_no_literal_factor_multiplication(self, rel, func):
        """AST 级: 这些函数体内不得再有 `x * 0.85` 字面量换算。"""
        hits = _mult_by_factor(_func_node(rel, func))
        assert not hits, f"{rel}::{func} 第 {hits} 行仍自带字面量 VIXY 换算"

    @pytest.mark.parametrize("rel,func", _SITES)
    def test_imports_shared_source(self, rel, func):
        assert "vix_source" in _src(rel), f"{rel} 未走单一真源 vix_source"


# ── 3. 全源失败如实返回 None (病灶①根治点) ──────────────────────────────

class TestNoNumericFallback:
    def test_fetch_vix_data_returns_none_when_all_sources_fail(self, monkeypatch):
        """VIXY/FRED/AlphaVantage 全挂 → None (修前为 20.0)。"""
        from app.analysis import market_state as ms
        from app.data import vix_source

        monkeypatch.setattr(vix_source, "fetch_vixy_via_opend", lambda *a, **k: None)
        monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "***")   # 代码显式跳过 "***"
        monkeypatch.setenv("FRED_API_KEY", "***")

        def _boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr("requests.get", _boom)

        assert ms.MarketStateChecker()._fetch_vix_data() is None

    def test_fetch_vix_data_never_returns_20_sentinel(self):
        """棘轮 (AST): 函数体不得再 `return 20.0`, 且必须存在如实 `return None`。"""
        node = _func_node("app/analysis/market_state.py", "_fetch_vix_data")
        consts = _return_consts(node)
        assert 20.0 not in consts, "禁止恢复数值兜底 (守卫会再次纸面化)"
        assert 18.0 not in consts, "禁止恢复数值兜底"
        assert None in consts, "全源失败必须如实返回 None"

    def test_vixy_leg_is_guarded_to_premarket_and_trading(self):
        """代理腿只在盘前/盘中生效 (与 router 同条件), 盘后/周末仍走 FRED 日频。

        且**禁止直查 US.VIX** —— 富途不供指数快照 (实测 `_error: empty`;
        DEB-20260818-futu-capability-audit.md:89,144), 只能取 US.VIXY 代理。
        """
        node = _func_node("app/analysis/market_state.py", "_fetch_vix_data")
        strings = _code_strings(node)
        body = ast.dump(node)
        assert "PRE_MARKET" in body and "TRADING" in body, \
            "代理腿必须限定在盘前/盘中 (与 MarketDataRouter 同条件)"
        assert "US.VIX" not in strings, "禁止直查 US.VIX"
        assert "US.VIXY" not in strings, "取数统一走 vix_source.fetch_vixy_via_opend"


# ── 4. fail-close 分支真实可达 (病灶①后果钉住) ─────────────────────────

class TestFailCloseReachable:
    def test_get_vix_cached_propagates_none(self, monkeypatch):
        """缓存层如实传播 None —— 这是两处 fail-close 的前提。"""
        from app.analysis import market_state as ms
        checker = ms.MarketStateChecker()
        monkeypatch.setattr(checker, "_fetch_vix_data", lambda: None)
        assert checker.get_vix_cached() is None

    def test_fail_open_callers_keep_explicit_defaults(self, monkeypatch):
        """显式 fail-open 的两处语义不变 (改动不得把它们变成 fail-close)。"""
        from app.analysis import market_state as ms
        checker = ms.MarketStateChecker()
        monkeypatch.setattr(checker, "_fetch_vix_data", lambda: None)
        assert checker.vix_below_25() is True      # 失败默认放行 (原语义)
        # 兜底 20.0 落 VIX_REGIMES=(20,30) ⇒ "caution" (仓位乘数 0.7, 偏保守)。
        # 注: 该处原注释写"默认正常"与边界不符, 2026-09-14 只改注释不改行为 —
        #     兜底偏保守是想要的语义, 故本测试钉住的是"caution"而非"normal"。
        assert checker.get_vix_regime() == "caution"

    @pytest.mark.parametrize("rel", [
        "app/pipeline/order_executor.py",
        "app/playbook/playbook_service.py",
    ])
    def test_reject_branch_still_wired(self, rel):
        """两处 `if vix is None → 拒绝新仓` 分支不得被删/绕 (守卫在, 且不再纸面)。"""
        src = _src(rel)
        assert re.search(r"if\s+_vix_val\s+is\s+None\s*:", src), \
            f"{rel} 的 VIX None 拒绝分支缺失"