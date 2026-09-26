"""UE tier 闸「通道级经济性备证」的只印不判契约 (2026-09-26 用户裁「按建议执行」= 建议1)

背景: 本轮实测 UE 通道 86 笔毛利 −$40.38 / 费用 $524.88 ⇒ 毛利率 −1.02bps < 费率 13.25bps
(费用归零仍不赚 = 无毛利来源), 且前瞻 t 检验 LONG t=+0.109 / SHORT t=+0.874 **两方向皆零 alpha**。
⇒ 「禁 UE LONG / 不对称限流」无数据依据, 且撞用户 8/2「市场双向, 反对不对称限流」与
ue_tier_gate 判据「方向分层只并列呈报不另设门 (防多重比较挪门柱)」禁令。

处置 = 登记 record-only 备证 (`channel_economics`, gating=False), **不立门**。
本测试用 AST 钉死三条不变量, 防后人把披露段悄悄接进判决:
  ① verdict 计算块不得引用 channel_economics / chan_econ (只印不判的核心);
  ② channel_economics 必须 gating=False 且只在 report dict 里作独立键出现;
  ③ _channel_economics 自身不得写库/改配置 (纯读) — 防「备证」变成副作用源。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

EVAL = REPO / "scripts" / "ue_tier_gate_shadow_eval.py"
SRC = EVAL.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"函数 {name} 不存在")


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def test_channel_economics_function_exists():
    fn = _func("_channel_economics")
    assert fn is not None
    # 必须自带 gating=False (record-only 的机器可读标记)
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Constant) and n.value is False]
    assert assigns, "_channel_economics 必须含 gating=False 字面量"


def test_channel_economics_is_pure_read_no_side_effects():
    """③ 备证段纯读: 不得写文件/写库/改配置 (防「备证」变成副作用源)。"""
    src = ast.get_source_segment(SRC, _func("_channel_economics")) or ""
    for banned in ("_atomic_write", "open(", "write_text", "INSERT", "UPDATE", "DELETE",
                   "executescript", "yaml.dump", "json.dump"):
        assert banned not in src, f"_channel_economics 含副作用调用 {banned!r} — 备证必须纯读"


def test_verdict_block_does_not_reference_channel_economics():
    """① 核心不变量: verdict 计算不得引用备证段 (只印不判)。

    判决块 = 从 `b_all, p_all = report["buckets"]...` 到 `report["verdict"] = ...` 赋值结束。
    用 AST 定位所有对 report["verdict"] 的赋值语句及其条件父节点, 检查其中不出现备证变量。
    """
    main = _func("main")
    verdict_assigns = []
    for node in ast.walk(main):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "verdict"):
                    verdict_assigns.append(node)
    assert len(verdict_assigns) >= 2, "应至少有初值 + 判据分支两处 verdict 赋值"

    # 判决链上的全部名字 (含包裹它们的 If 条件)
    judged: set[str] = set()
    for va in verdict_assigns:
        judged |= _names_in(va)
    for node in ast.walk(main):
        if isinstance(node, ast.If):
            # If 条件与 body 里若有 verdict 赋值 ⇒ 条件也算判决链
            if any(isinstance(x, ast.Assign) and any(
                    isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "verdict" for t in x.targets)
                    for x in node.body):
                judged |= _names_in(node.test)

    for banned in ("chan_econ", "channel_economics", "_channel_economics",
                   "gross_bps", "fee_bps", "net_t", "fwd_alpha_t"):
        assert banned not in judged, (
            f"判决链引用了 {banned!r} ⇒ 「只印不判」被破坏 (备证会影响 verdict)")


def test_report_contains_channel_economics_as_separate_key():
    """② 备证只作 report 的独立键出现, gating 标记为 False。"""
    main = _func("main")
    report_dicts = [n for n in ast.walk(main) if isinstance(n, ast.Dict)
                    and any(isinstance(k, ast.Constant) and k.value == "channel_economics"
                            for k in n.keys)]
    assert report_dicts, 'report 必须含 "channel_economics" 键'
    d = report_dicts[0]
    keys = [k.value for k in d.keys if isinstance(k, ast.Constant)]
    assert "verdict" in keys and "buckets" in keys, "report 主结构不得因加备证而变形"


def test_stdout_uses_print_only_not_judged_prefix():
    """stdout 披露行必须带「只印不判」前缀 (防把披露读成裁决, 同 top_movers_eval 先例)。

    按 **print 语句** 检查而非按行 — 多行 f-string 的续行不重复前缀, 逐行扫会假失败。
    """
    main = _func("main")
    marks = ("UE 通道经济性", "方向并列披露", "前瞻 ")
    hits = 0
    for node in ast.walk(main):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Name) and f.id == "print"):
            continue
        stmt_src = ast.get_source_segment(SRC, node) or ""
        if any(m in stmt_src for m in marks):
            hits += 1
            assert "只印不判" in stmt_src, (
                f"披露 print 缺「只印不判」前缀:\n{stmt_src[:200]}")
    assert hits >= 3, f"应至少有 3 条备证披露 print (通道/方向/前瞻), 实 {hits}"


def test_docstring_records_no_asymmetric_gate_ruling():
    """裁决依据必须留在 docstring (防后人重新提议「禁 UE LONG」时找不到已被证伪的记录)。"""
    doc = ast.get_docstring(TREE) or ""
    for kw in ("不对称限流", "零 alpha", "不立门"):
        assert kw in doc, f"模块 docstring 缺 {kw!r} — 裁决依据必须可被后人检索到"


def test_direction_split_is_disclose_only_not_gated():
    """方向读数必须是「披露」而非「门」: 不得出现按 direction 拦/放行的判定分支。"""
    fn_src = ast.get_source_segment(SRC, _func("_channel_economics")) or ""
    assert "by_direction_disclose_only" in fn_src, "方向段必须以 disclose_only 命名"
    for banned in ("would_block", "SKIP", "reject", "block_direction"):
        assert banned not in fn_src, f"备证段出现拦截语义 {banned!r} ⇒ 已越界成门"
