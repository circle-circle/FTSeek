#!/usr/bin/env python3
"""配置键 **反向** 普查 + 隐形开关门禁 (2026-08-28, D3)。

为什么需要它: config_key_census.py 只查一个方向 —— "yaml 里写了的键有没有人读"
(死键)。另一个方向无人守: "代码读的键有没有人写"。缺这一侧的后果是**隐形开关**:
代码 get_dotted("a.b", <字面量默认>) 而 yaml 根本没这个键 → 行为整个扛在代码
默认值上, 配置面看不见, 人工审计看不见, 死键普查也看不见 (它压根不在 yaml 里)。

实案 (本工具首跑抓出, 2026-08-28):
  app/risk/position_exit_monitor.py:353-354 读 exit_policy.use_exit_registry /
  registry_fallback_on_error, 默认 True/True 且 config/*.yaml + app/risk/exit_policy.yaml
  **两处配置面都是 0 命中** → 全部持仓用哪套退出引擎挂在代码默认值上; 任一次 Registry
  异常静默降级 legacy (legacy 含 Registry 没有的 time_exit), 降级只 logger.debug 而
  LOG_LEVEL=INFO → 生产日志永久不可见。

判据 (安全方向: 宁可漏报不误报):
  - 只算**字面量默认值** (数字/布尔/字符串/{}/[]/None)。默认值是另一个已声明键或
    变量 (如 risk.position.long_max_per_direction 默认取 limits["max_per_direction"])
    时不告警 —— 那是对**可见旋钮**的回退, 不是藏起来的常数; 归 TRANSITIVE_EXEMPT 留痕。
  - 注释/docstring 里的 get_dotted 不算读点 (tokenize 剔除, 否则 scale_out.py:68 的
    "The previous get_dotted(...) approach" 说明文字会误报)。
  - 声明判定按**完整点号路径**精确匹配 (父级 section 存在即视为 section 型读点已声明,
    因为 section 型默认 {} 的语义就是"用代码内 DEFAULTS", 键在不在都一样)。

只读。用法:
  PYTHONPATH=. venv/bin/python scripts/config_key_reverse_census.py                 # 普查
  PYTHONPATH=. venv/bin/python scripts/config_key_reverse_census.py --gate          # 门禁
  PYTHONPATH=. venv/bin/python scripts/config_key_reverse_census.py --update-baseline  # 缩基线
"""
import io
import json
import re
import sys
import tokenize
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "data" / "config_key_reverse_census_latest.json"
BASELINE = BASE / "data" / "config_key_reverse_census_baseline.json"

# 运行时语料 (与正向普查同口径: 主入口 main.py 必须在内, archive/tests 不算活读点)
SCAN_DIRS = ["app", "scripts"]
SCAN_FILES = ["main.py"]

READ_RE = re.compile(r'get_dotted\(\s*["\']([a-zA-Z0-9_.]+)["\']\s*(?:,\s*([^)\n]{0,120}))?')

# 字面量默认值 = 真隐形开关; 其余 (标识符/下标/属性链) = 回退到别处已声明的值
LITERAL_RE = re.compile(
    r"^(?:-?\d+(?:\.\d+)?$|True|False|None$"
    r"|\"[^\"]*\"$|'[^']*'$|\{\}$|\[\]$|\{\s*$|\[\s*$)")

# 2026-08-28 补漏 (回归测试抓出): LITERAL_RE 的数字分支不认 Python 下划线分隔写法,
# 而仓内确有 get_dotted("capital.fixed_amount", 82_000) 这类读点 (4 处) —— 走正则会把
# 它们判成"默认值非字面量"→ 归豁免, 于是**未声明**的键也能被静默放过去。改用 AST 判定。
def _is_literal_default(default: str) -> bool:
    """默认值文本是否为字面量 (含下划线数字/多行 dict 起始)。"""
    d = (default or "").strip()
    if not d or d == "<无默认=None>":
        return True  # 无默认 = None, 同样是藏在代码里的常量
    if LITERAL_RE.match(d):
        return True
    if d.startswith(("{", "[")):  # 跨行 dict/list 字面量起始
        return True
    try:
        import ast
        val = ast.literal_eval(d)
    except Exception:
        return False
    return isinstance(val, (bool, int, float, str, bytes, tuple, list, dict, type(None)))


# 二级配置面 (不经 get_dotted, 由模块自己读文件): 代码文件 → (yaml文件, yaml内section路径, section变量名)
SECOND_SURFACE = {
    # _load_scale_out_config() 返回 policy["exit_rules"]["scale_out"], 局部变量名 cfg
    "app/risk/exit_rules/scale_out.py": ("app/risk/exit_policy.yaml", "exit_rules.scale_out", "cfg"),
}

# 非字面量默认 → 有意豁免, 留实证理由 (与正向普查 LEAF_EXEMPT 同法: 只登记不删除)
TRANSITIVE_EXEMPT = {
    "risk.position.long_max_per_direction":
        "默认取 limits['max_per_direction'] (yaml risk.position.max_per_direction 已声明) — "
        "对可见旋钮的回退, 非隐藏常数",
}


def _strip_comments(src: str) -> str:
    """tokenize 剔除注释与 docstring, 保留行号 (换行填充) 以便报出原始 file:line。"""
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 解析失败 → 退回"整行注释剔除"保守方案 (只会漏报, 不会因注释误报)
        return "\n".join("" if ln.lstrip().startswith("#") else ln
                         for ln in src.split("\n"))
    lines = src.split("\n")
    keep = [True] * (len(lines) + 2)
    for tok in toks:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            # STRING 只在"作为语句出现"时是 docstring; 正向普查同向安全: 多删不误报读点,
            # 但普通字符串里也不含 get_dotted( 调用, 故整体当 docstring 处理是安全的
            if tok.type == tokenize.COMMENT or tok.string.startswith(('"""', "'''")):
                for ln in range(tok.start[0], tok.end[0] + 1):
                    keep[ln] = False
    return "\n".join("" if not keep[i] else lines[i - 1]
                     for i in range(1, len(lines) + 1))


def _code_reads():
    """→ {dotted_path: [(file, line, default_text)]}"""
    reads = {}
    files = []
    for d in SCAN_DIRS:
        if (BASE / d).is_dir():
            files += [p for p in sorted((BASE / d).rglob("*.py"))
                      if "__pycache__" not in str(p) and "/archive/" not in str(p)]
    files += [BASE / f for f in SCAN_FILES if (BASE / f).exists()]
    for p in files:
        try:
            src = _strip_comments(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for n, line in enumerate(src.split("\n"), start=1):
            for m in READ_RE.finditer(line):
                path = m.group(1).lower()
                default = (m.group(2) or "").strip().rstrip(",)")
                reads.setdefault(path, []).append(
                    (str(p.relative_to(BASE)), n, default or "<无默认=None>")
                )
    return reads


def _declared_paths():
    """config/*.yaml (get_dotted 唯一来源) → 全部点号路径 (含中间 section)。"""
    paths = set()

    def walk(node, prefix):
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            p = f"{prefix}.{k}".lower() if prefix else str(k).lower()
            paths.add(p)
            walk(v, p)

    for f in sorted((BASE / "config").glob("*.yaml")):
        try:
            walk(yaml.safe_load(f.read_text(encoding="utf-8", errors="replace")) or {}, "")
        except Exception as e:
            print(f"[warn] yaml 解析失败 {f.name}: {e}")
    return paths


def _second_surface_reads():
    """二级配置面的 section 读点 → {yaml内路径: [(file,line,default)]}

    只认 section 变量名上的 .get("字面量键", 字面量默认) — 同文件里对逐条数据字典
    (如 tier dict t) 的 t.get("close_pct", 0) 不是配置读点, 按接收者名过滤掉。
    """
    out = {}
    for rel, (yaml_file, section, sec_var) in SECOND_SURFACE.items():
        p = BASE / rel
        if not p.exists():
            continue
        try:
            declared = yaml.safe_load((BASE / yaml_file).read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        sec = declared
        for part in section.split("."):
            sec = sec.get(part) if isinstance(sec, dict) else None
        have = set()
        if isinstance(sec, dict):
            def w(node, pre):
                for k, v in (node or {}).items():
                    q = f"{pre}.{k}" if pre else str(k)
                    have.add(q.lower())
                    if isinstance(v, dict):
                        w(v, q)
            w(sec, "")
        src = _strip_comments(p.read_text(encoding="utf-8", errors="replace"))
        pat = re.compile(
            r'\b' + re.escape(sec_var) + r'\.get\(\s*["\']([a-zA-Z0-9_]+)["\']\s*,\s*([^)\n]{0,60})')
        for n, line in enumerate(src.split("\n"), start=1):
            for m in pat.finditer(line):
                leaf, default = m.group(1), m.group(2).strip().rstrip(",)")
                # have 是**相对 sec 子树**的路径集 (sec 已是 exit_rules.scale_out),
                # 故按 leaf 比对; 早先用绝对 section 前缀比对会把已声明的 tier_mode 误报
                if leaf.lower() in have:
                    continue
                if not _is_literal_default(default):
                    continue
                out.setdefault(f"{yaml_file}::{section}.{leaf}", []).append(
                    (rel, n, default))
    return out


def run():
    reads = _code_reads()
    declared = _declared_paths()
    undeclared, transitive = [], []
    for path, sites in sorted(reads.items()):
        if path in declared:
            continue
        defaults = [d for _, _, d in sites]
        # section 型读点 (默认值全是 dict 字面量) 的语义是"整节没配就用代码内 DEFAULTS",
        # 故父 section 存在即算已声明; 叶子型读点必须精确命中, 否则父节点存在也会漏判
        # (capital: 存在不代表 capital.initial 已声明 —— 后者正是本工具要找的隐形常数)
        is_section = all(d is None or d.strip().startswith(("{", "None")) for d in defaults) \
            and all(_is_literal_default(d) for d in defaults)
        if is_section and any(path.startswith(p + ".") for p in declared):
            continue
        literal = all(_is_literal_default(d) for _, _, d in sites)
        rec = {"path": path, "sites": [f"{f}:{l}" for f, l, _ in sites[:3]],
               "defaults": sorted({d for _, _, d in sites})[:3]}
        if literal and path not in TRANSITIVE_EXEMPT:
            undeclared.append(rec)
        else:
            transitive.append(rec)
    second = _second_surface_reads()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reads_total": len(reads),
        "undeclared_literal": undeclared,
        "undeclared_second_surface": [
            {"path": k, "sites": [f"{f}:{l}" for f, l, _ in v[:3]],
             "defaults": sorted({d for _, _, d in v})[:3]} for k, v in sorted(second.items())
        ],
        "transitive_exempt": transitive,
        "exempt_registry": TRANSITIVE_EXEMPT,
    }


def _baseline_set():
    if not BASELINE.exists():
        return set()
    try:
        snap = json.loads(BASELINE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return set(snap.get("undeclared", []))


def main():
    r = run()
    viol = [x["path"] for x in r["undeclared_literal"]] + \
           [x["path"] for x in r["undeclared_second_surface"]]
    print("=" * 66)
    print(f"配置键反向普查 {r['generated_at']} | 代码读点 {r['reads_total']} 个")
    print(f"  隐形开关 (字面量默认 + 配置无声明): {len(viol)}")
    for x in r["undeclared_literal"]:
        print(f"    ✗ {x['path']:48s} 默认={x['defaults']}  {x['sites']}")
    for x in r["undeclared_second_surface"]:
        print(f"    ✗ {x['path']:48s} 默认={x['defaults']}  {x['sites']}")
    print(f"  回退到已声明值 (豁免留痕): {len(r['transitive_exempt'])}")
    for x in r["transitive_exempt"]:
        why = TRANSITIVE_EXEMPT.get(x["path"], "默认值非字面量")
        print(f"    · {x['path']:48s} {why[:60]}")
    print("=" * 66)
    OUT.write_text(json.dumps(r, indent=2, ensure_ascii=False))
    print(f"输出: {OUT}")

    if "--update-baseline" in sys.argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps({
            "_note": ("隐形开关冻结基线 (D3): 只许减少不许新增; 新增读点必须先在配置面写键。"
                      "缩基线随声明 commit 一起 --update-baseline"),
            "generated_at": r["generated_at"], "undeclared": sorted(viol)},
            indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"基线更新: {BASELINE} ({len(viol)} 条存量冻结)")

    if "--gate" in sys.argv:
        frozen = _baseline_set()
        new = [v for v in viol if v not in frozen]
        gone = sorted(frozen - set(viol))
        if gone:
            print(f"\n✅ 基线可缩 {len(gone)} 条 (已声明, 请随手 --update-baseline): {gone}")
        if new:
            print(f"\n═══ 门禁失败: 新增 {len(new)} 个隐形开关 ═══")
            for v in new:
                print(f"  ✗ {v}")
            print("\n处置: 在配置面显式写出该键并赋当前代码默认值 (零行为变更), "
                  "或确属有意隐藏 → TRANSITIVE_EXEMPT 登记实证理由。")
            return 1
        print(f"\n✅ 门禁通过: 无新增隐形开关 (基线存量 {len(frozen)} 条)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
