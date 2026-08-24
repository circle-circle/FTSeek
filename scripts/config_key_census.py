#!/usr/bin/env python3
"""配置键消费普查 + 死键门禁 (2026-08-22 普查 / 2026-08-24 A3 门禁化, 只读).

背景: C批人工 grep 出 unified_config.yaml 9 个死键/影子键。本工具把该排查
固化为可重跑的普查: 全量扫 config/*.yaml → 展平叶子键 → 在运行时代码里找
消费点, 死键从此自动暴露, 不靠人肉审计。

分类 (按叶子键名 word-boundary 匹配, 与 C批人工审计同一口径):
  referenced      — 叶子名出现在运行时代码且非纯字典字面量定义行 (.get("k") /
                    get_dotted("a.b.k") / 变量赋值引用等)
  definition_only — 仅出现在字典字面量定义行 (注册表形态, 如 PlaybookConfig.
                    DEFAULTS 的 "k": default) — 注册≠消费, 死键候选
  orphan          — 运行时代码零出现 — 死键候选 (最强信号)

判定规则: 行以 ["']leaf["']: 开头且该词在本行只出现一次 → 定义行; 其余出现
(含 .get 参数、get_dotted 路径、赋值 RHS) → 消费。注释整行跳过 (安全方向:
只会把死键漏报成活, 不会把活键误报成死)。

A3 门禁化 (2026-08-24):
  - 语料补盲区: 根目录 main.py 纳入扫描 (原只扫 app/+scripts/, 入口文件的
    消费点不可见 → 活键误判死键)。
  - 倒排索引提速: 原 O(叶子×语料) 全量跑要数分钟; 先建 token→行 索引再按叶
    子查候选行, 数秒内完成 → 门禁可直接进 pytest 实时跑, 无过期基线问题。
    候选行最终仍用原 word-boundary 正则复判, 分类语义与旧版逐位一致。
  - --gate 模式: 所有非 referenced 叶子必须落在显式豁免注册表里, 否则退出
    码 1。新增配置键而不消费 (或不登记) → 测试红。冻结期纪律: 只登记不删除,
    十月解冻后按注册表清理。

已知局限 (结论供人工复核, 不构成删除依据):
  1. 同名叶子跨节/跨文件互参 (影子键): 叶子级分类可能把影子标成 referenced
     (漏报, 安全向)。
  2. 泛型迭代消费: factor_profiles/strategy_variants 被 assembly.py 以
     for s in slots 形态迭代, 内层键名永不出现在 py — 这类文件的
     orphan/definition_only 只作候选, 文件级标 iteration_consumption_likely。
  3. f-string 动态构造键名无法静态发现。
  4. 非 python 消费方 (shell/外部脚本) 不在语料内。

只读: 输出 data/config_key_census_latest.json + 控制台摘要; --gate 非零退出。
用法:
  PYTHONPATH=. venv/bin/python scripts/config_key_census.py                 # 普查
  PYTHONPATH=. venv/bin/python scripts/config_key_census.py --gate          # 门禁
  PYTHONPATH=. venv/bin/python scripts/config_key_census.py --update-baseline  # 缩基线(随清理commit)
"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE / "config"
OUT = BASE / "data" / "config_key_census_latest.json"

# 扫描语料: 运行时代码. 排除 archive (历史脚本不算活消费)、本脚本自身
# (docstring 提及的死键名会自污染语料)、tests (测试钉死≠运行时消费)。
# A3 修复: 根目录 *.py (main.py 等) 也是运行时消费大户, 必须入语料。
SCAN_DIRS = [BASE / "app", BASE / "scripts"]
ROOT_PY = sorted(BASE.glob("*.py"))
EXCLUDE_PARTS = {"archive"}
EXCLUDE_FILES = {Path(__file__).resolve()}

# 已确认走泛型迭代消费的文件 (零命名出现≠死)
ITERATION_CONSUMED = {"factor_profiles.yaml", "strategy_variants.yaml"}

# C批人工已核的死键 (unified_config.yaml) — 口径漂移哨兵兼 LEAF_EXEMPT 数据源
KNOWN_DEAD = {
    "buying_power_fallback_alert",
    "intraday_breakout_pct",
    "breakeven_ratchet_enabled",
    "time_exit_enabled",
}

# ── 门禁豁免注册表 (A3, 2026-08-24) ────────────────────────────────────── #
# 门禁三层模型:
#   1) FILE_EXEMPT / ITERATION_CONSUMED / LEAF_EXEMPT — 显式实证豁免;
#   2) 基线快照 tests/fixtures/config_key_census_baseline.json — 存量非
#      referenced 叶子冻结集: 只许减少 (十月解冻后清理时缩基线), 不许新增
#      (新死键 → 测试红)。重新生成 = scripts/...py --update-baseline,
#      是进 diff 的显式动作。
BASELINE = BASE / "tests" / "fixtures" / "config_key_census_baseline.json"

# 文件级豁免: 整文件非运行时配置或整文件已死
FILE_EXEMPT = {
    "version.yaml": (
        "纯文档元数据 (升级指南/deprecation 标注), 无运行时消费方, 非配置"
    ),
    "position_protection.yaml": (
        "整文件无加载点 (2026-08-24 实证: 全仓唯一同名命中是 DB 表 "
        "position_protection_log) — 叶子全部为死键, 十月解冻后整文件删除"
    ),
}

# 键级豁免: "文件名:叶子名" → 实证理由 (只收编需特别标注的项, 其余走基线)
LEAF_EXEMPT = {
    # C批人工核定 (原 KNOWN_DEAD, 兼口径漂移哨兵数据源)
    "unified_config.yaml:buying_power_fallback_alert": "C批核定死键",
    "unified_config.yaml:intraday_breakout_pct": "C批核定死键",
    "unified_config.yaml:breakeven_ratchet_enabled": "C批核定死键",
    "unified_config.yaml:time_exit_enabled": "C批核定死键",
    # 2026-08-24 A3 分诊实证 (grep 零命中 + 消费方改道证据)
    "unified_config.yaml:battle_plan_bonus": (
        "8/23 审计 BATTLE_PLAN_BONUS 常量已删 (commit 0e8990a8), 配置键残留"
    ),
    "unified_config.yaml:battle_plan_bonus_enabled": (
        "8/23 审计 BATTLE_PLAN_BONUS 常量已删, 配置键残留"
    ),
    "unified_config.yaml:login_password": (
        "解锁密码实际走环境变量 FUTU_UNLOCK_PWD_MD5 (dual_broker.py:53), "
        "yaml 字段死 — 含敏感形字段, 十月解冻后优先清除"
    ),
}

# 自验证集 (普查口径漂移哨兵): 哨兵死键必须落非 referenced, 否则规则坏了
SELF_CHECK_DEAD = sorted(f"unified_config.yaml:{k}" for k in KNOWN_DEAD)

_DEF_TEMPLATE = r'^\s*["\']{}["\']\s*:'

# ── 门禁核心 ──────────────────────────────────────────────────────────── #
def exemption_for(fname: str, leaf: str):
    """返回 (exempt?, reason). 文件级 > 泛型迭代 > 键级."""
    if fname in FILE_EXEMPT:
        return True, f"文件级: {FILE_EXEMPT[fname]}"
    if fname in ITERATION_CONSUMED:
        return True, "泛型迭代消费文件 (零命名出现≠死)"
    key = f"{fname}:{leaf}"
    if key in LEAF_EXEMPT:
        return True, LEAF_EXEMPT[key]
    return False, ""


def dead_snapshot(census: dict) -> dict:
    """提取全部非 referenced 叶子 → {file: {leaf: status}} (基线/对比形态)."""
    out = {}
    for fname, fr in census["files"].items():
        if "error" in fr:
            continue
        dead = {leaf: v["status"] for leaf, v in fr["leaves"].items()
                if v["status"] != "referenced"}
        if dead:
            out[fname] = dead
    return out


def gate_violations(census: dict):
    """门禁违规 = 未豁免 且 不在基线快照内的非 referenced 叶子 (只堵新增)."""
    baseline = {}
    if BASELINE.exists():
        try:
            baseline = json.loads(BASELINE.read_text(encoding="utf-8")).get(
                "non_referenced", {})
        except Exception:
            baseline = {}
    violations = []
    for fname, fr in census["files"].items():
        if "error" in fr:
            violations.append((fname, "<解析失败>", fr["error"]))
            continue
        for leaf, v in fr["leaves"].items():
            if v["status"] == "referenced":
                continue
            ok, reason = exemption_for(fname, leaf)
            if ok:
                continue
            if leaf in baseline.get(fname, {}):
                continue  # 存量死键: 基线冻结, 十月解冻后清理
            violations.append(
                (fname, leaf,
                 f"{v['status']} ← {(v.get('dotted_paths') or ['?'])[0]}")
            )
    return violations


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
    """收集运行时代码行. 返回 [(path_str, line_str)]; 整行注释不入索引."""
    corpus = []
    files = []
    for d in SCAN_DIRS:
        files.extend(sorted(d.rglob("*.py")))
    files.extend(ROOT_PY)
    for p in files:
        if EXCLUDE_PARTS & set(p.parts) or p in EXCLUDE_FILES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rp = str(p.relative_to(BASE))
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            corpus.append((rp, line))
    return corpus


def build_index(corpus):
    """倒排索引: \w+ 词元 → [(path, line)]. 只作候选生成器, 终判仍用
    word-boundary 正则复核 (见 classify_leaf), 保证与逐行扫描语义一致."""
    idx = defaultdict(list)
    tok = re.compile(r"\w+")
    for path, line in corpus:
        for t in set(tok.findall(line)):
            idx[t].append((path, line))
    return idx


def _candidates_from_corpus(leaf, corpus):
    """兼容旧签名: 直接在原始行列表上找候选 (单测合成小语料用)."""
    word = re.compile(r"\b" + re.escape(leaf) + r"\b")
    return [(p, l) for p, l in corpus if word.search(l)]


def classify_leaf(leaf, corpus_or_index):
    """在语料中找 leaf 的出现并分类. 返回 (status, matched_files).

    corpus_or_index 两种形态:
      - dict (build_index 产物): O(候选) 查询, 全量跑用
      - list[(path, line)]: 线性扫, 单测合成语料用
    """
    if isinstance(corpus_or_index, dict):
        # 候选生成: 叶子含非 \w 字符时索引无法命中, 回退线性扫
        if leaf and re.fullmatch(r"\w+", leaf):
            candidates = corpus_or_index.get(leaf, [])
        else:
            candidates = _candidates_from_corpus(leaf, _flatten_index(corpus_or_index))
    else:
        candidates = _candidates_from_corpus(leaf, corpus_or_index)

    word = re.compile(r"\b" + re.escape(leaf) + r"\b")
    def_line = re.compile(_DEF_TEMPLATE.format(re.escape(leaf)))
    files = set()
    referenced = False
    for path, line in candidates:
        if line.strip().startswith("#"):
            continue  # 整行注释跳过 (列表语料路径; 索引语料已在 load_corpus 滤过)
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


def _flatten_index(idx):
    for pairs in idx.values():
        yield from pairs


def run_census():
    """全量普查. 返回 (result_dict, corpus_lines_count)."""
    corpus = load_corpus()
    index = build_index(corpus)

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
        "gate_registries": {
            "file_exempt": dict(FILE_EXEMPT),
            "leaf_exempt": sorted(LEAF_EXEMPT),
        },
    }

    self_check_hits = {}
    for yf_name, info in all_files.items():
        if "error" in info:
            result["files"][yf_name] = info
            continue
        leaf_status = {}
        for leaf in info["leaves"]:
            status, files = classify_leaf(leaf, index)
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
            if f"{yf_name}:{leaf}" in SELF_CHECK_DEAD and status == "referenced":
                self_check_hits[f"{yf_name}:{leaf}"] = status

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
        "self_check_dead_expected": sorted(SELF_CHECK_DEAD),
        "unexpected_referenced": self_check_hits,
        "note": "哨兵死键应全部落非referenced; 若出现 referenced, 说明普查口径"
                "漂移, 先查规则再信结论.",
    }
    return result


def print_summary(result):
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
    bad = sc.get("unexpected_referenced") or {}
    print(f"\n自验证: 哨兵死键异常翻活 {len(bad)}/{len(sc.get('self_check_dead_expected', []))}"
          + (f" ({', '.join(sorted(bad))})" if bad else " (口径正常)"))


def main():
    result = run_census()
    print_summary(result)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"输出: {OUT}")

    if "--update-baseline" in sys.argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        snap = {"_note": ("存量非referenced叶子冻结基线 (A3门禁): 只许减少不许新增; "
                          "缩基线须随十月清理 commit 一起改"),
                "generated_at": result["generated_at"],
                "non_referenced": dead_snapshot(result)}
        BASELINE.write_text(json.dumps(snap, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        n = sum(len(v) for v in snap["non_referenced"].values())
        print(f"基线更新: {BASELINE} ({n} 个存量死键冻结)")

    if "--gate" in sys.argv:
        violations = gate_violations(result)
        if violations:
            print(f"\n═══ 门禁失败: {len(violations)} 个新增未登记死键 ═══")
            for fname, leaf, detail in violations:
                print(f"  ✗ {fname}:{leaf}  [{detail}]")
            print("\n处置: 消费该键; 或确属死键 → LEAF_EXEMPT/FILE_EXEMPT 登记"
                  "实证理由 (冻结期不删除); 存量清理后用 --update-baseline 缩基线.")
            return 1
        print("\n✅ 门禁通过: 无新增死键 (存量以基线冻结).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
