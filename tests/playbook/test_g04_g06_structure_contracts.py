"""G-04/G-05/G-06 结构契约测试 (2026-09-05 审计修复批).

三条缺陷的共同病根是**控制流结构**而非字面量, 故本文件用 AST 锁结构, 不用
substring 匹配 (注释/措辞改动不应误红, 但嵌套关系改动必须红):

G-04 (playbook_service 回滚平仓假成功):
  dual_broker.close_position 在 order_filled=False 时 price_source="broker_close_failed"
  却仍 return success=True → 上层只检 success 便记"孤儿仓已防", 实际仓位残留 broker 侧
  且 DB 无 trade_record (monitor 永远查不到), 恰好造成本回滚要避免的孤儿仓。
  锁: 回滚分支的 if 判定必须同时引用 order_filled 与 dealt_qty。

G-05 (playbook_service 成交簿记裸奔):
  update_instruction_tracking/save_user_feedback 无 try → SQLite 锁忙时异常冒出
  execute_instruction, 被调用方 except 记为 error+skipped → broker 已成交而漏斗报"跳过",
  executed_count/_max_deploy 不累加 → 同轮后续指令额度被高估。
  锁: 两处簿记调用必须在 try 内, 且异常分支不得释放幂等锁 (释放=可被重捞→重复下单),
      且返回值须带 bookkeeping_failed 标记 (调用方据此按已部署敞口计入)。

G-06 (pipeline chase_cap 闸可被异常整体跳过):
  原遥测+enforce 整体嵌在 re-price try 内且自身再套吞异常 except → re-price 任何异常
  = 禁追高闸静默跳过, 指令按 DB 原始价执行 (P0-A 87ebc785 等于纸面)。
  锁: ① chase_cap 闸的 try 不得是 re-price try 的后代; ② 判定失败必须 fail-close 终态跳过;
      ③ enforce 开关读取失败取保守方向 (视为 ON); ④ 遥测写盘失败不得冒泡 (判定/留痕分离)。

已知局限 (诚实声明): 本文件锁结构, 不做端到端行为覆盖 — execute_instruction 的完整
成交路径需大量桩, 现有 tests/test_playbook_service.py 亦仅覆盖 early-return 分支。

回滚: 删本文件即可 (不影响被测代码)。
"""
import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

_SVC = PROJECT_ROOT / "app" / "playbook" / "playbook_service.py"
_PIPE = PROJECT_ROOT / "app" / "playbook" / "pipeline.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _handler_binds(try_node: ast.Try, name: str) -> bool:
    """该 try 的任一 except 是否把异常绑定到指定名字."""
    return any(h.name == name for h in try_node.handlers)


def _all_tries(tree: ast.AstVisitor if False else ast.AST):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Try)]


def _find_try_by_handler(tree, name):
    hits = [t for t in _all_tries(tree) if _handler_binds(t, name)]
    assert len(hits) == 1, f"期望唯一绑定 {name} 的 try, 实得 {len(hits)} 处"
    return hits[0]


def _descendants(node):
    for child in ast.walk(node):
        if child is not node:
            yield child


def _calls_named(node, func_name):
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            f = child.func
            if isinstance(f, ast.Attribute) and f.attr == func_name:
                yield child
            elif isinstance(f, ast.Name) and f.id == func_name:
                yield child


def _get_keys_in(node):
    """收集 node 内所有 X.get('key') 的 key 字面量 (ast 层, 不做字符串匹配)."""
    out = set()
    for c in ast.walk(node):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "get" and c.args
                and isinstance(c.args[0], ast.Constant)
                and isinstance(c.args[0].value, str)):
            out.add(c.args[0].value)
    return out


def _local_deps(scope_node):
    """局部变量名 → 其赋值表达式引用到的 .get('key') 集合 (一跳数据流)."""
    deps = {}
    for n in ast.walk(scope_node):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)):
            deps[n.targets[0].id] = _get_keys_in(n.value)
    return deps


def _expand_keys(node, deps):
    """if 判定实际依赖的 broker 返回字段 (含经局部变量间接引用)."""
    keys = set(_get_keys_in(node))
    for nm in ast.walk(node):
        if isinstance(nm, ast.Name) and nm.id in deps:
            keys |= deps[nm.id]
    return keys


class TestG04RollbackCloseChecksFill:
    def setup_method(self):
        self.tree = _tree(_SVC)
        # 回滚块的 except 绑定 _rbe (playbook_service 内唯一)
        self.rollback_try = _find_try_by_handler(self.tree, "_rbe")

    def test_rollback_calls_close_position(self):
        assert any(True for _ in _calls_named(self.rollback_try, "close_position")), (
            "回滚块应调用 dual_broker.close_position"
        )

    def test_success_branch_requires_order_filled_and_dealt_qty(self):
        """只检 success 即判"孤儿仓已防" = G-04 本体, 必须同时检 order_filled/dealt_qty."""
        ifs = [n for n in _descendants(self.rollback_try) if isinstance(n, ast.If)]
        assert ifs, "回滚块内应有成功/失败分支 if"
        deps = _local_deps(self.rollback_try)
        succ = [i for i in ifs if "success" in _expand_keys(i.test, deps)]
        assert succ, "应存在基于 broker 返回 success 的成功/失败分支 if"
        keys = _expand_keys(succ[0].test, deps)
        assert "order_filled" in keys, (
            f"回滚成功判定必须检 order_filled (broker_close_failed 时 success 仍为 True); 实得依赖字段: {sorted(keys)}"
        )
        assert "dealt_qty" in keys, (
            f"回滚成功判定必须检 dealt_qty (部分成交残余量仍裸露); 实得依赖字段: {sorted(keys)}"
        )

    def test_partial_fill_not_treated_as_prevented(self):
        """dealt_qty 必须是"≥ 请求量"式比较, 而非仅存在性判断."""
        ifs = [n for n in _descendants(self.rollback_try) if isinstance(n, ast.If)]
        cmp_ops = [
            op for i in ifs for op in ast.walk(i.test)
            if isinstance(op, (ast.GtE, ast.Gt, ast.Eq))
        ]
        assert cmp_ops, (
            "dealt_qty 需与请求量做数量比较 (>=), 仅 bool(dealt_qty) 会把部分成交当已防"
        )


class TestG05BookkeepingGuarded:
    def setup_method(self):
        self.tree = _tree(_SVC)
        self.src = _SVC.read_text(encoding="utf-8")

    def _find_holding_tracking_call(self):
        """定位成交后簿记的 update_instruction_tracking (带 outcome=holding)."""
        hits = []
        for call in _calls_named(self.tree, "update_instruction_tracking"):
            dicts = [a for a in call.args if isinstance(a, ast.Dict)]
            dicts += [kw.value for kw in call.keywords if isinstance(kw.value, ast.Dict)]
            for d in dicts:
                keys = [k.value for k in d.keys if isinstance(k, ast.Constant)]
                if "outcome" in keys and "execution_price" in keys:
                    hits.append(call)
        assert len(hits) == 1, f"期望唯一成交簿记调用, 实得 {len(hits)}"
        return hits[0]

    def test_bookkeeping_is_inside_try(self):
        target = self._find_holding_tracking_call()
        enclosing = [
            t for t in _all_tries(self.tree)
            if any(d is target for d in _descendants(t))
        ]
        assert enclosing, "G-05: 成交簿记必须包在 try 内 (裸奔时 DB 锁忙异常会冒出 execute_instruction)"

    def test_bookkeeping_handler_does_not_release_claim(self):
        """异常分支严禁释放幂等锁 — 释放=指令可被再次捞起→重复下单 (双重持仓)."""
        target = self._find_holding_tracking_call()
        bk_try = [
            t for t in _all_tries(self.tree)
            if any(d is target for d in _descendants(t)) and _handler_binds(t, "_bk_err")
        ]
        assert bk_try, "G-05: 簿记 try 的 except 应绑定 _bk_err"
        for handler in bk_try[0].handlers:
            for call in _calls_named(handler, "release_execution_claim"):
                pytest.fail(f"G-05: 簿记异常分支不得释放幂等锁, 实得 {ast.unparse(call)}")

    def test_return_carries_bookkeeping_failed_flag(self):
        assert '"bookkeeping_failed": _bk_failed' in self.src, (
            "G-05: 返回值须带 bookkeeping_failed, 调用方据此按已部署敞口计入 (不可当 skip)"
        )

    def test_save_user_feedback_also_guarded(self):
        target = self._find_holding_tracking_call()
        bk_try = [
            t for t in _all_tries(self.tree)
            if any(d is target for d in _descendants(t)) and _handler_binds(t, "_bk_err")
        ][0]
        assert any(True for _ in _calls_named(bk_try, "save_user_feedback")), (
            "G-05: save_user_feedback 同样裸奔, 必须一并入 try"
        )


class TestG06ChaseCapGateHoisted:
    def setup_method(self):
        self.tree = _tree(_PIPE)
        self.src = _PIPE.read_text(encoding="utf-8")
        self.reprice_try = _find_try_by_handler(self.tree, "_rp_err")

    def _chase_cap_try(self):
        hits = [t for t in _all_tries(self.tree) if _handler_binds(t, "_cc_err")]
        assert len(hits) == 1, f"期望唯一绑定 _cc_err 的 try, 实得 {len(hits)}"
        return hits[0]

    def test_gate_not_nested_in_reprice_try(self):
        """G-06 本体: chase_cap 闸一旦嵌在 re-price try 内, re-price 异常即静默跳过闸门."""
        cc = self._chase_cap_try()
        assert not any(d is cc for d in _descendants(self.reprice_try)), (
            "G-06: chase_cap 闸的 try 不得是 re-price try (_rp_err) 的后代"
        )

    def test_gate_follows_reprice_try(self):
        cc = self._chase_cap_try()
        assert cc.lineno > self.reprice_try.lineno, (
            "G-06: 闸门应位于 re-price try 之后 (判定对象=真实下单价)"
        )

    def test_judged_price_is_execution_price(self):
        """判定须用"即将真实下单的价格" — custom_params 非空即覆盖 DB entry_price."""
        cc = self._chase_cap_try()
        body_src = "\n".join(
            self.src.splitlines()[n.lineno - 1] for n in ast.walk(cc)
            if hasattr(n, "lineno")
        )
        assert "_reprice_params" in body_src, (
            "G-06: 判定价必须与 custom_params=_reprice_params 消费口径同源"
        )

    def test_enforce_switch_failure_fails_conservative(self):
        """开关读不到 → 保守视为 ON (跳过是安全方向, 放行追高不是)."""
        idx = self.src.index("_cc_enforce = _cc_enforce_f()")
        pre = self.src[max(0, idx - 600):idx]
        assert "_cc_enforce = True" in pre, (
            "G-06: _cc_enforce 初值须为 True, 使开关读取异常时落在保守方向"
        )

    def test_gate_failure_fail_closes(self):
        assert "if not _cc_gate_ok and _cc_enforce:" in self.src, (
            "G-06: 判定未完成 (闸门不可用) 且 enforce=ON 时必须 fail-close"
        )
        idx = self.src.index("if not _cc_gate_ok and _cc_enforce:")
        block = self.src[idx:idx + 1500]
        assert "chase_cap_gate_unavailable" in block, "fail-close 须落 skip_reason 终态 (防被重新捞起)"
        assert "skipped_count += 1" in block and "continue" in block, (
            "fail-close 须走终态跳过三件套 (计数 + 循环位移)"
        )

    def test_telemetry_write_failure_does_not_leak_to_gate(self):
        """判定与留痕分离: 写盘失败不得冒泡到 _cc_err (否则会误触发 fail-close 或漏拦)."""
        cc = self._chase_cap_try()
        inner = [
            t for t in _descendants(cc)
            if isinstance(t, ast.Try) and _handler_binds(t, "_cc_w_err")
        ]
        assert inner, "G-06: 遥测写盘应有独立 try (绑定 _cc_w_err)"
        wrapped = inner[0]
        assert any(True for _ in _calls_named(wrapped, "_shadow_dedup_claim")), (
            "写盘 try 应包住 dedup claim"
        )
        assert any(True for _ in _calls_named(wrapped, "_shadow_dedup_commit")), (
            "写盘 try 应包住 dedup commit (写盘成功后才提交键)"
        )
        # enforce 分支不得被写盘 try 包住 (否则写盘失败会连带跳过拦截)
        assert not any(
            isinstance(d, ast.If) and ast.unparse(d.test) == "_cc_enforce"
            for d in _descendants(wrapped)
        ), "G-06: enforce 终态跳过必须在写盘 try 之外 (留痕失败仍须拦截)"
