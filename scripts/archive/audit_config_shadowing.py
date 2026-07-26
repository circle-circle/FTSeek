#!/usr/bin/env python3
"""PlaybookConfig 遮蔽审计脚本 (DEB-20260721 F-1, 只读).

背景: PlaybookConfig.get(key, default) 的 fallback 链 = YAML > 调用方显式默认 > DEFAULTS.
任何调用点传了显式 default 就绕过 DEFAULTS; 若 playbook 节缺失/损坏, get_dotted("playbook")
静默返回 {}, 则所有 pb_cfg.get 返回字面量第二参 — "静默遮蔽机".

本脚本 AST 扫描全部调用点, 对照 YAML(扁平合并 risk_parameters+execution_parameters)
与 DEFAULTS, 报告:
  [P1-遮蔽]   YAML 无此键 且 显式默认 != DEFAULTS 值 → DEFAULTS 被绕过, 生效值=调用点字面量
  [P2-裸键]   YAML 无此键 且 DEFAULTS 也无 → 生效值完全由调用点字面量决定 (SSOT 外)
  [P3-不一致] 同一键在多个调用点使用不同的显式默认 → YAML 节丢失时行为随调用点漂移
  [OK]        YAML 有此键 (声明值生效) 或 调用点默认与 DEFAULTS 一致

用法: ./venv/bin/python scripts/audit_config_shadowing.py [--ci]
  --ci: 存在 P1/P3 时退出码 1 (可挂 CI 防复发)
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _literal(node):
    """尽力求值字面量默认参数; 失败返回哨兵."""
    try:
        return ast.literal_eval(node)
    except Exception:
        return "<非字面量>"


def collect_callsites():
    """扫描 app/ 下所有 PlaybookConfig 接收者的 .get(key, default) 调用点."""
    sites = []  # (file, lineno, key, default_value, has_default)
    for py in sorted((ROOT / "app").rglob("*.py")):
        try:
            src = py.read_text(encoding="utf-8")
        except Exception:
            continue
        if "get_playbook_config" not in src and "PlaybookConfig" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        # 1) 收集 "x = get_playbook_config()" / "x = PlaybookConfig()" 的变量名 (含别名导入)
        pb_names = set()
        factory_aliases = {"get_playbook_config"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "playbook" in node.module:
                for a in node.names:
                    if a.name == "get_playbook_config":
                        factory_aliases.add(a.asname or a.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = node.value.func
                fname = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else "")
                if fname in factory_aliases or fname == "PlaybookConfig":
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            pb_names.add(t.id)

        # 2) 找 .get("key"[, default]) 调用: 接收者是 pb 变量 / 链式工厂调用
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                continue
            recv = node.func.value
            is_pb = False
            if isinstance(recv, ast.Name) and recv.id in pb_names:
                is_pb = True
            elif isinstance(recv, ast.Call):
                rf = recv.func
                rname = rf.id if isinstance(rf, ast.Name) else (
                    rf.attr if isinstance(rf, ast.Attribute) else "")
                if rname in factory_aliases:
                    is_pb = True
            if not is_pb:
                continue
            key = node.args[0].value
            has_default = len(node.args) > 1
            default_val = _literal(node.args[1]) if has_default else None
            sites.append((str(py.relative_to(ROOT)), node.lineno, key, default_val, has_default))
    return sites


def main():
    from app.playbook.config import PlaybookConfig
    # 复用生产加载逻辑取 YAML 扁平字典 (只读)
    yaml_flat = PlaybookConfig._load()
    defaults = PlaybookConfig.DEFAULTS

    sites = collect_callsites()
    by_key = defaultdict(list)
    for s in sites:
        by_key[s[2]].append(s)

    p1, p2, p3, ok = [], [], [], []
    for key, ss in sorted(by_key.items()):
        in_yaml = key in yaml_flat
        in_def = key in defaults
        defaults_seen = {repr(s[3]) for s in ss if s[4]}
        if len(defaults_seen) > 1:
            p3.append((key, ss, defaults_seen))
        for s in ss:
            f, ln, _, dv, hd = s
            if in_yaml:
                ok.append((key, f, ln, f"YAML={yaml_flat[key]!r} 生效"))
            elif hd and in_def and dv != defaults[key]:
                p1.append((key, f, ln, f"调用点默认={dv!r} 遮蔽 DEFAULTS={defaults[key]!r}"))
            elif hd and not in_def:
                p2.append((key, f, ln, f"生效值=调用点字面量 {dv!r} (YAML/DEFAULTS 均无)"))
            else:
                ok.append((key, f, ln, f"默认={dv!r} 与 DEFAULTS 一致或走 DEFAULTS"))

    print(f"扫描: {len(sites)} 个调用点 / {len(by_key)} 个键 | YAML 键 {len(yaml_flat)} | DEFAULTS 键 {len(defaults)}")
    print(f"\n[P1-遮蔽] DEFAULTS 被调用点默认绕过 ({len(p1)}):")
    for key, f, ln, msg in p1:
        print(f"  {key:40s} {f}:{ln}  {msg}")
    print(f"\n[P2-裸键] SSOT 外字面量 ({len(p2)}):")
    for key, f, ln, msg in p2:
        print(f"  {key:40s} {f}:{ln}  {msg}")
    print(f"\n[P3-不一致] 同键多默认 ({len(p3)}):")
    for key, ss, dvs in p3:
        locs = ", ".join(f"{s[0]}:{s[1]}={s[3]!r}" for s in ss if s[4])
        print(f"  {key:40s} {locs}")
    print(f"\n[OK] {len(ok)} 个调用点无遮蔽风险")

    if "--ci" in sys.argv and (p1 or p3):
        print("\nCI 模式: 存在 P1/P3 → exit 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
