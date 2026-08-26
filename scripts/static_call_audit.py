#!/usr/bin/env python3
"""AST 静态调用审计: 捕获"调用即 TypeError 但被 except 吞掉"的静默死亡类.

背景 (2026-08-26·用户令深度检查): subscription_manager._auto_loop 曾对零参的
scheduler_triggers.get_market_phase() 传参调用 → TypeError → 被裸 except 吞掉
→ 周末特性静默死亡数月, 日志无任何异常。此类病灶运行期不可见, 只能静态扫。

扫描逻辑 (保守, 只报告可解析到定义的调用):
  1. 收集全仓模块级函数签名 (min/max 位置参数、参数名、*args/**kwargs)
  2. 解析每文件 import 映射 (alias → 定义处全名)
  3. 对可解析的 Call 做:
     - TOO_MANY_ARGS      : 位置实参超上界且被调方无 *args
     - UNKNOWN_KWARG      : 关键字实参不在形参表且被调方无 **kwargs
     - MISSING_REQUIRED   : 必填参数未提供 (且被调方无 **kwargs 兜底)
方法调用 (obj.method) 不做类型推断, 不在覆盖范围 — 宁漏勿误。

用法: ./venv/bin/python scripts/static_call_audit.py [--json OUT]
"""
import ast
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["app", "scripts"]
SKIP_PARTS = {"venv", "__pycache__", ".git", "node_modules"}


class Sig:
    __slots__ = ("qname", "file", "line", "pos_params", "req_count",
                 "all_names", "vararg", "kwarg", "kwonly_req")

    def __init__(self, qname, file, line, pos_params, req_count,
                 all_names, vararg, kwarg, kwonly_req):
        self.qname = qname          # 全限定名
        self.file = file
        self.line = line
        self.pos_params = pos_params  # 可位置传参的参数名列表
        self.req_count = req_count    # 无默认值的位置参数个数
        self.all_names = all_names    # 全部关键字可接受名
        self.vararg = vararg
        self.kwarg = kwarg
        self.kwonly_req = kwonly_req  # 必填 keyword-only 参数名


def _param_sig(node: ast.FunctionDef | ast.AsyncFunctionDef, drop_first: bool):
    pos, req = [], []
    args = node.args
    pargs = args.posonlyargs + args.args
    if drop_first and pargs:
        pargs = pargs[1:]  # self / cls
    ndefaults = len(args.defaults)
    # defaults 与 pargs 尾部对齐: 前 len-ndefaults 个为必填
    first_default = len(pargs) - ndefaults
    for i, p in enumerate(pargs):
        pos.append(p.arg)
        if i < first_default:
            req.append(p.arg)
    allnames = set(pos) | {p.arg for p in args.kwonlyargs}
    kwonly_req = [p.arg for p, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
    return pos, len(req), allnames, args.vararg, args.kwarg, kwonly_req


def collect_sigs() -> Dict[str, Sig]:
    """模块级函数 + 类方法 (Class.method 键), 方法仅登记不主动解析."""
    sigs: Dict[str, Sig] = {}
    for rel in _iter_py():
        path = ROOT / rel
        mod = str(rel.with_suffix("")).replace("/", ".")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                pos, req, names, va, kw, koreq = _param_sig(node, False)
                sigs[f"{mod}.{node.name}"] = Sig(
                    f"{mod}.{node.name}", rel, node.lineno,
                    pos, req, names, va and va.arg, kw and kw.arg, koreq)
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if sub.name.startswith("__") and sub.name != "__init__":
                            continue
                        # @staticmethod 首参非 self/cls, 不可删
                        is_static = any(
                            isinstance(d, ast.Name) and d.id == "staticmethod"
                            for d in sub.decorator_list)
                        pos, req, names, va, kw, koreq = _param_sig(sub, not is_static)
                        sigs[f"{mod}.{node.name}.{sub.name}"] = Sig(
                            f"{mod}.{node.name}.{sub.name}", rel, sub.lineno,
                            pos, req, names, va and va.arg, kw and kw.arg, koreq)
    return sigs


def _iter_py():
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if SKIP_PARTS & set(p.parts):
                continue
            yield p.relative_to(ROOT)


def analyze_file(path: Path, rel: Path, sigs: Dict[str, Sig], findings: List[dict]):
    mod = str(rel.with_suffix("")).replace("/", ".")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return

    # import 映射: 本地名 → 定义全名; 模块别名 → 模块路径
    fn_alias: Dict[str, str] = {}
    mod_alias: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if a.name == "*":
                    continue
                target = f"{node.module}.{a.name}"
                if node.module.startswith(("app.", "scripts.")) and target in sigs:
                    fn_alias[a.asname or a.name] = target
                elif node.module.startswith(("app.", "scripts.")):
                    mod_alias[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(("app.", "scripts.")):
                    mod_alias[a.asname or a.name.split(".")[0]] = (
                        a.name if a.asname else a.name)

    # 同文件模块级函数可直接调用
    local_fns = {k.rsplit(".", 1)[1]: k for k in sigs
                 if k.startswith(mod + ".") and k.count(".") == mod.count(".") + 1}

    def resolve(func) -> Optional[str]:
        if isinstance(func, ast.Name):
            return fn_alias.get(func.id) or local_fns.get(func.id)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            m = mod_alias.get(func.value.id)
            if m:
                cand = f"{m}.{func.attr}"
                if cand in sigs:
                    return cand
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        q = resolve(node.func)
        if q is None:
            continue
        s = sigs[q]
        # 动态实参 (*args / **kwargs 调用) 跳过
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue
        if any(k.arg is None for k in node.keywords):
            continue
        n_pos = len(node.args)
        kw_names = [k.arg for k in node.keywords]

        def emit(kind, detail):
            findings.append({
                "kind": kind, "callee": q, "callee_at": f"{s.file}:{s.line}",
                "call_at": f"{rel}:{node.lineno}", "detail": detail})

        if n_pos > len(s.pos_params) and not s.vararg:
            emit("TOO_MANY_ARGS",
                 f"{n_pos} 个位置实参 > 形参上限 {len(s.pos_params)}")
            continue
        unknown = [k for k in kw_names
                   if k not in s.all_names and not s.kwarg]
        if unknown:
            emit("UNKNOWN_KWARG",
                 f"未知关键字 {unknown}, 形参只有 {sorted(s.all_names)[:8]}")
            continue
        if not s.kwarg:
            filled = set(s.pos_params[:n_pos]) | set(kw_names)
            missing = [p for p in s.pos_params[:s.req_count] if p not in filled]
            miss_ko = [k for k in s.kwonly_req if k not in kw_names]
            if missing or miss_ko:
                emit("MISSING_REQUIRED",
                     f"缺必填参数 {missing + miss_ko}")


def main():
    out_json = None
    if "--json" in sys.argv:
        out_json = sys.argv[sys.argv.index("--json") + 1]
    sigs = collect_sigs()
    findings: List[dict] = []
    for rel in _iter_py():
        analyze_file(ROOT / rel, rel, sigs, findings)
    findings.sort(key=lambda f: (f["kind"], f["call_at"]))
    print(f"# 已登记签名 {len(sigs)} 个; 发现可疑调用 {len(findings)} 处\n")
    cur = None
    for f in findings:
        if f["kind"] != cur:
            cur = f["kind"]
            print(f"\n== {cur} ==")
        print(f"  {f['call_at']}  → {f['callee']}  ({f['detail']})"
              f"\n      定义于 {f['callee_at']}")
    if out_json:
        Path(out_json).write_text(json.dumps(findings, ensure_ascii=False, indent=2))
        print(f"\nJSON 已写 {out_json}")


if __name__ == "__main__":
    main()
