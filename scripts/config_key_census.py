#!/usr/bin/env python3
"""配置键消费普查 + 死键门禁 (2026-08-22 普查 / 2026-08-24 A3 门禁化, 只读).

背景: C批人工 grep 出 unified_config.yaml 9 个死键/影子键。本工具把该排查
固化为可重跑的普查: 全量扫 config/*.yaml + EXTRA_YAML 登记的仓外活体配置
(app/risk/exit_policy.yaml 等, ConfigWriter 有写盘通道故必须在内) → 展平叶子键
→ 在运行时代码里找消费点, 死键从此自动暴露, 不靠人肉审计。

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

# config/ 之外的**活体** yaml (2026-08-28): 普查原先只 glob config/*.yaml, 于是这个文件
# 全程裸奔 — 而它是 exit_policy_guardian 的出场参数 SSOT, 且 ConfigWriter 有条目
# ("yaml_file": "exit_policy") 直接往里写。写盘通道有、死键门禁无 = 只进不出没人查。
# 新增此类仓外活体配置时往这里加, 不要靠"它在 config/ 下"的假设。
EXTRA_YAML = [BASE / "app" / "risk" / "exit_policy.yaml"]

# 已确认走泛型迭代消费的文件 (零命名出现≠死)
ITERATION_CONSUMED = {"factor_profiles.yaml", "strategy_variants.yaml"}

# 仓外运行时消费键 (2026-08-28): 普查语料只有仓内 *.py, 这些键按语料判 orphan,
# 但仓外活体脚本真读它们 → 不是死键, 而是"本仓 grep 看不见的开关"。逐个实证见值。
EXTERNAL_CONSUMED = {
    "unified_config.yaml:da_skip_leveraged_etf": (
        "/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py:835 "
        "_da_skip_leveraged_etf 读 universe_expansion.da_skip_leveraged_etf "
        "作杠杆/反向 ETF 前置过滤 (RFC-159; 仓内语料看不到该读者)"
    ),
    "unified_config.yaml:da_candidate_feed": (
        "/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py:168 读 "
        "universe_expansion.da_candidate_feed 作 UE→DA 注入总开关 (false 即跳过)"
    ),
    # [RFC-117 2026-09-29] UE LONG 接入 DA feed 的独立回滚开关 (仓外 skill 消费)
    "unified_config.yaml:da_candidate_feed_long": (
        "/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py:799 读 "
        "universe_expansion.da_candidate_feed_long 作 UE LONG 腿进 DA 深分析池的"
        "独立开关 (false 恢复 2026-08-03 只取 SHORT 语义)"
    ),
    "unified_config.yaml:symbol_card_prompt_enabled": (
        "/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:1169 读 "
        "playbook.symbol_card_prompt_enabled 作股性卡进 prompt 开关"
    ),
    # DEB-20260831 §四 (2026-09-01): 成交口径股性档案注入开关
    "unified_config.yaml:trading_card_prompt_enabled": (
        "/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py "
        "_load_trading_card_for_prompt 读 playbook.trading_card_prompt_enabled "
        "作成交口径档案进 prompt 开关 (false 即停注)"
    ),
    # 2026-09-01 信息供应链审计建议② (PE 审阅执行批): 负向论据警示前置注入开关
    "unified_config.yaml:claim_negative_prompt_enabled": (
        "/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:1307 "
        "_load_negative_claims_warning_for_prompt 读 playbook.claim_negative_prompt_enabled "
        "作负向论据警示进 prompt 开关 (false 即停注; 代码默认 True, 键为纯回滚开关)"
    ),
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
}

# 键级豁免: "文件名:叶子名" → 实证理由 (只收编需特别标注的项, 其余走基线)
# 2026-08-28: battle_plan_bonus / battle_plan_bonus_enabled / login_password
#   三键已按各自豁免理由的处方清除 (前两键"配置键残留"→删; login_password
#   "含敏感形字段, 建议随下批死键清除"→删), 豁免条目随之注销 — 哨兵口径:
#   键消失而豁免还挂着 = 注册表与配置脱节, 必须同步。
LEAF_EXEMPT = {
    # 2026-09-24 登记 (982895f21, DEB-20260918 质检口径批引入, 当时漏登记):
    # reasoning_effort 是**嵌套消费** — 它位于 llm.features.universe_expansion_reasoning
    # .extra_body 内, extra_body 整字典经 app/llm/unified_service.py 原样透传给 LLM API
    # (DashScope/GLM thinking 参数), 普查扫描器按叶子名找 get()/[] 直接消费看不到它。
    # 实证: unified_config.yaml:512-517 (extra_body.enable_thinking+reasoning_effort),
    # unified_service.py:50/77/94/111 (extra_body 透传), daily_llm_token_report.py:275
    # (以该键落库时刻为锚)。勿删该键 (GLM-5.3 未传时默认 max ⇒ UE brief 烧满输出预算
    # schema_failed, 见 unified_config 行内注释与 [[ue-thinking-ab-control-arm]] 教训)。
    "unified_config.yaml:reasoning_effort": (
        "extra_body 嵌套透传消费 (unified_service.py → LLM API), 扫描器盲区非死键; "
        "982895f21 DEB-20260918 质检口径批引入"
    ),
}

# 自验证集 (普查口径漂移哨兵):
#   哨兵必须"仍在 yaml 且仍非 referenced"。两个方向都会响:
#     referenced → 普查规则漂了 (死键被判活), 先查规则再信结论;
#     整个键消失 → 注册表与配置脱节 (豁免还挂着, 键已被删/改名)。
#   派生自注册表本身, 不再手写名单: 2026-08-28 前是硬编码 KNOWN_DEAD 四键,
#   那四键当天被删后哨兵永不再命中 (= 静默常绿), 故改为派生。
SELF_CHECK_DEAD = sorted(LEAF_EXEMPT)
# 仓外开关哨兵: 语料看不见→必然判 orphan (口径已知盲区), 但键必须还在,
# 消失 = 仓外消费方读的配置被误删。
SELF_CHECK_EXTERNAL = sorted(EXTERNAL_CONSUMED)

_DEF_TEMPLATE = r'^\s*["\']{}["\']\s*:'

# ── 门禁核心 ──────────────────────────────────────────────────────────── #
def exemption_for(fname: str, leaf: str):
    """返回 (exempt?, reason). 文件级 > 泛型迭代 > 键级."""
    if fname in FILE_EXEMPT:
        return True, f"文件级: {FILE_EXEMPT[fname]}"
    if fname in ITERATION_CONSUMED:
        return True, "泛型迭代消费文件 (零命名出现≠死)"
    key = f"{fname}:{leaf}"
    if key in EXTERNAL_CONSUMED:
        return True, f"仓外运行时消费: {EXTERNAL_CONSUMED[key]}"
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
                continue  # 存量死键: 基线冻结, 随各批"清理"逐步缩 (无固定解冻日)
            violations.append(
                (fname, leaf,
                 f"{v['status']} ← {(v.get('dotted_paths') or ['?'])[0]}")
            )
    # 注册表脱节也算违规: 豁免/仓外开关名单必须与配置实际键集一致
    sc = census.get("self_check", {})
    for key, status in sorted((sc.get("unexpected_referenced") or {}).items()):
        violations.append(("<self_check>", key, f"哨兵死键被判 {status} — 普查口径漂移"))
    for key in sorted(sc.get("sentinel_missing") or []):
        violations.append(
            ("<self_check>", key, "注册表挂豁免/仓外开关, 但键已从 yaml 消失 — 名单脱节"))
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
    r"""倒排索引: \w+ 词元 → [(path, line)]. 只作候选生成器, 终判仍用
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
    """全量普查. 返回 result_dict (corpus_files/corpus_lines 均在 dict 内)."""
    corpus = load_corpus()
    index = build_index(corpus)

    # 其他 yaml 的叶子集 (影子键互参提示)
    yaml_leaves_by_file = {}
    all_files = {}
    sweep = sorted(CONFIG_DIR.glob("*.yaml")) + [p for p in EXTRA_YAML if p.exists()]
    for yf in sweep:
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
            "(注册≠消费); orphan=零出现. 注释行跳过, 同名影子键会漏报(安全向). "
            "⚠️语料=仓内 *.py 单一载体: orphan 只证明\"仓内 py 不读\", 不证明\"无人读\" "
            "— .sh/.service/html/js 与仓外 /root/.hermes/** 运行时脚本均在盲区 "
            "(2026-08-28 实证: da_candidate_feed / symbol_card_prompt_enabled 判 orphan "
            "却被 Hermes 脚本当真开关读). 删除前必须跨载体+仓外复核, 见 external_consumed."
        ),
        "files": {},
        "self_check": {},
        "gate_registries": {
            "file_exempt": dict(FILE_EXEMPT),
            "leaf_exempt": sorted(LEAF_EXEMPT),
            "external_consumed": dict(EXTERNAL_CONSUMED),
        },
    }

    self_check_hits = {}
    seen_sentinels = set()
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
            leaf_key = f"{yf_name}:{leaf}"
            if leaf_key in SELF_CHECK_DEAD:
                seen_sentinels.add(leaf_key)
                if status == "referenced":
                    self_check_hits[leaf_key] = status
            if leaf_key in SELF_CHECK_EXTERNAL:
                seen_sentinels.add(leaf_key)

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
        "external_switch_expected": sorted(SELF_CHECK_EXTERNAL),
        "unexpected_referenced": self_check_hits,
        "sentinel_missing": sorted(
            (set(SELF_CHECK_DEAD) | set(SELF_CHECK_EXTERNAL)) - seen_sentinels
        ),
        "note": "哨兵双向: 键在且被判 referenced=普查口径漂移 (先查规则再信结论); "
                "键整个消失=注册表与配置脱节 (豁免/仓外开关悬空, 必须同步清理或还原)。",
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
    missing = sc.get("sentinel_missing") or []
    n_exp = len(sc.get("self_check_dead_expected", [])) + len(
        sc.get("external_switch_expected", []))
    verdict = " (口径正常)" if not bad and not missing else ""
    print(f"\n自验证: 哨兵 {n_exp} 键 → 异常翻活 {len(bad)}"
          + (f" ({', '.join(sorted(bad))})" if bad else "")
          + f" / 注册表悬空(键已消失) {len(missing)}"
          + (f" ({', '.join(missing)})" if missing else "")
          + verdict)


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
