#!/usr/bin/env python3
"""配置键消费普查 (2026-08-22, 只读) — 配置层的"活性探针".

背景: C批人工 grep 出 unified_config.yaml 9 个死键/影子键。本工具把该排查
固化为可重跑的普查: 全量扫 config/*.yaml → 展平叶子键 → 在 app/+scripts/
运行时代码里找消费点, 死键从此自动暴露, 不靠人肉审计。

分类 (按叶子键名 word-boundary 匹配, 与 C批人工审计同一口径):
  referenced      — 叶子名出现在运行时代码且非纯字典字面量定义行 (.get("k") /
                    get_dotted("a.b.k") / 变量赋值引用等)
  definition_only — 仅出现在字典字面量定义行 (注册表形态, 如 PlaybookConfig.
                    DEFAULTS 的 "k": default) — 注册≠消费, 死键候选
  orphan          — 运行时代码零出现 — 死键候选 (最强信号)

判定规则: 行以 ["']leaf["']: 开头且该词在本行只出现一次 → 定义行; 其余出现
(含 .get 参数、get_dotted 路径、赋值 RHS) → 消费。注释整行跳过 (安全方向:
只会把死键漏报成活, 不会把活键误报成死)。

已知局限 (结论供人工复核, 不构成删除依据):
  1. 同名叶子跨节/跨文件互参 (影子键): unified 的 rr_ratio 是影子, horizon
     文件的同名键才活 — 叶子级分类会把影子键标成 referenced (漏报, 安全向)。
  2. 泛型迭代消费: factor_profiles/strategy_variants 被 assembly.py 以
     for s in slots 形态迭代, 内层键名永不出现在 py — 这类文件的
     orphan/definition_only 只作候选, 文件级标 iteration_consumption_likely。
  3. f-string 动态构造键名无法静态发现。

只读: 输出 data/config_key_census_latest.json + 控制台摘要。
"""
import json
import re
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE / "config"
OUT = BASE / "data" / "config_key_census_latest.json"

# 扫描语料: 运行时代码. 排除 archive (历史脚本不算活消费)、本脚本自身
# (docstring 提及的死键名会自污染语料)、tests (测试钉死≠运行时消费)。
SCAN_DIRS = [BASE / "app", BASE / "scripts"]
EXCLUDE_PARTS = {"archive"}
EXCLUDE_FILES = {Path(__file__).resolve()}

# 已确认走泛型迭代消费的文件 (零命名出现≠死)
ITERATION_CONSUMED = {"factor_profiles.yaml", "strategy_variants.yaml"}

# C批人工已核的死键 — 用于自验证普查口径没有漂移
KNOWN_DEAD = {
    "buying_power_fallback_alert",
    "intraday_breakout_pct",
    "breakeven_ratchet_enabled",
    "time_exit_enabled",
}

_DEF_TEMPLATE = r'^\s*["\']{}["\']\s*:'


def flatten(node, prefix, out):
    """yaml 树 → [(dotted_path, leaf_name)]; list 内 dict 也展平 (路径加 [i])."""
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                flatten(v, path, out)
            else:
                out.append((path, str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            flatten(v, f"{prefix}[{i}]", out)


def load_corpus():
    """收集运行时代码行. 返回 [(path_str, line_str, is_comment_line)]."""
    corpus = []
    for d in SCAN_DIRS:
        for p in sorted(d.rglob("*.py")):
            if EXCLUDE_PARTS & set(p.parts) or p in EXCLUDE_FILES:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for line in text.splitlines():
                corpus.append((str(p.relative_to(BASE)), line))
    return corpus


def classify_leaf(leaf, corpus):
    """在语料中找 leaf 的出现并分类. 返回 (status, matched_files)."""
    word = re.compile(r"\b" + re.escape(leaf) + r"\b")
    def_line = re.compile(_DEF_TEMPLATE.format(re.escape(leaf)))
    files = set()
    referenced = False
    for path, line in corpus:
        if line.strip().startswith("#"):
            continue
        hits = word.findall(line)
        if not hits:
            continue
        files.add(path)
        # 定义行 = 行首 "leaf": 且本行仅一次出现; 两次以上必含 .get/取值引用
        if len(hits) == 1 and def_line.match(line):
            continue
        referenced = True
    if referenced:
        return "referenced", files
    if files:
        return "definition_only", files
    return "orphan", files


def main():
    corpus = load_corpus()

    # 其他 yaml 的叶子集 (影子键互参提示)
    yaml_leaves_by_file = {}
    all_files = {}
    for yf in sorted(CONFIG_DIR.glob("*.yaml")):
        try:
            tree = yaml.safe_load(yf.read_text(encoding="utf-8"))
        except Exception as e:
            all_files[yf.name] = {"error": str(e)}
            continue
        pairs = []
        flatten(tree, "", pairs)
        leaves = sorted({leaf for _, leaf in pairs})
        yaml_leaves_by_file[yf.name] = set(leaves)
        all_files[yf.name] = {"paths": pairs, "leaves": leaves}

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus_files": len({p for p, _ in corpus}),
        "corpus_lines": len(corpus),
        "classification_rule": (
            "referenced=运行时代码出现且非纯字典定义行; definition_only=仅注册表形态定义"
            "(注册≠消费); orphan=零出现. 注释行跳过, 同名影子键会漏报(安全向)."
        ),
        "files": {},
        "self_check": {},
    }

    self_check_hits = {}
    for yf_name, info in all_files.items():
        if "error" in info:
            result["files"][yf_name] = info
            continue
        own_leaves = yaml_leaves_by_file[yf_name]
        leaf_status = {}
        for leaf in info["leaves"]:
            status, files = classify_leaf(leaf, corpus)
            # 影子提示: 同名叶出现在其他 yaml (那边可能才是活的)
            shadow_in = sorted(
                other for other, s in yaml_leaves_by_file.items()
                if other != yf_name and leaf in s
            )
            entry = {
                "status": status,
                "dotted_paths": [p for p, l in info["paths"] if l == leaf],
            }
            if status != "referenced":
                entry["match_files"] = sorted(files)
                if shadow_in:
                    entry["shadow_in_other_yaml"] = shadow_in
            leaf_status[leaf] = entry
            if leaf in KNOWN_DEAD and status != "referenced":
                self_check_hits[leaf] = status

        fresult = {
            "leaf_total": len(info["leaves"]),
            "referenced": sum(1 for v in leaf_status.values() if v["status"] == "referenced"),
            "definition_only": sum(1 for v in leaf_status.values() if v["status"] == "definition_only"),
            "orphan": sum(1 for v in leaf_status.values() if v["status"] == "orphan"),
        }
        if yf_name in ITERATION_CONSUMED:
            fresult["caveat"] = (
                "本文件经泛型迭代消费 (assembly.py slots/variants 循环), "
                "orphan/definition_only 仅作候选不作死键结论"
            )
        fresult["leaves"] = leaf_status
        result["files"][yf_name] = fresult

    result["self_check"] = {
        "known_dead_expected": sorted(KNOWN_DEAD),
        "known_dead_found_non_referenced": self_check_hits,
        "note": "C批人工核定的死键应全部落 orphan/definition_only; 若有 referenced "
                "或缺失, 说明普查口径漂移, 先查规则再信结论.",
    }

    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    # 控制台摘要
    print(f"═══ 配置键消费普查 (语料 {result['corpus_files']} 文件/"
          f"{result['corpus_lines']} 行) ═══")
    for name, fr in result["files"].items():
        if "error" in fr:
            print(f"\n{name}: 解析失败 {fr['error']}")
            continue
        flag = " [迭代消费caveat]" if "caveat" in fr else ""
        print(f"\n{name}{flag}: {fr['leaf_total']} 叶子键 → "
              f"referenced={fr['referenced']} "
              f"definition_only={fr['definition_only']} orphan={fr['orphan']}")
        for leaf, v in fr["leaves"].items():
            if v["status"] == "referenced":
                continue
            shadow = ""
            if "shadow_in_other_yaml" in v:
                shadow = f" (同名见: {','.join(v['shadow_in_other_yaml'])})"
            print(f"   [{v['status']}] {leaf}{shadow} ← {v['dotted_paths'][:3]}")
    sc = result["self_check"]
    print(f"\n自验证: 已知死键 {len(sc['known_dead_found_non_referenced'])}/"
          f"{len(KNOWN_DEAD)} 命中非referenced "
          f"({', '.join(sorted(sc['known_dead_found_non_referenced']))})")
    print(f"输出: {OUT}")


if __name__ == "__main__":
    main()
