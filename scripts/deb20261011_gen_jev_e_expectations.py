"""追加 §8 独立复查节到 DEB-20261011 (E 表期望值程序化取自探针实跑输出, 禁手抄)。

纪律:
  - 只追加, 不碰 §1–§7 (作者 = Codex, 今日 11:30 停笔, 已备份)。
  - E 表遵守 verify_deb20261009_event.py 的 **3 列契约** (| # | 命令 | 期望输出 |)。
  - 期望值全部从 /root/.hermes/cache/scratch/jev_probe2.json 程序化提取。
"""
import json
import re
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DEB = REPO / "docs/debates/DEB-20261011-jev-structured-decision-automation-discipline.md"
BAK = Path("/root/.hermes/cache/scratch/DEB-jev-20261011.bak.md")
J = json.load(open("/root/.hermes/cache/scratch/jev_probe2.json", encoding="utf-8"))
S = {s["segment"]: s for s in J["segments"]}
PROBE = "PYTHONPATH=. ./venv/bin/python scripts/deb20261011_jev_conflict_probe.py"
PYJ = "./venv/bin/python -c"


def j(expr: str) -> str:
    pre = ("import json,sys; j=json.load(sys.stdin); "
           "S={s['segment']:s for s in j['segments']}; ")
    return f'{PROBE} | {PYJ} "{pre}{expr}"'


# ── E 表: 命令 + 实跑期望值 (程序化) ──
A, B, C, D, E, F, G = (S[k] for k in "ABCDEFG")
ROWS = [
    ("E1", "两 DEB 交叉引用: 新 DEB 是否提及前案",
     j("g=S['G']['new_20261011'];print(g['lines'],g['git_tracked'],g['mentions_20260924'],"
       "g['mentions_AUC_0403'],g['mentions_Q3_voided'],g['claims_no_local_validation'])")),
    ("E2", "候选① 真值面: failures 近30天/recovered 填充/类型分布",
     j("a=S['A'];print(a['failures']['n_total'],a['failures']['n_last_30d'],a['failures']['span'][1],"
       "a['failures_recovered_fill']['nonzero'],a['failures_type_dist'][0]['t'],"
       "a['failures_type_dist'][0]['n'])")),
    ("E3", "候选① 其余真值面: alerts / issue_registry / cooldown",
     j("a=S['A'];print(a['alerts']['n_total'],a['issue_registry'],a['alert_state_cooldown']['exists'])")),
    ("E4", "候选① 输入体量: 日志 ERROR/CRITICAL (字面量契约)",
     j("v=S['A']['log_volume'];print(v['logs/ftseek_us.log']['lines'],v['logs/ftseek_us.log']['ERROR'],"
       "v['logs/ftseek_us.log']['CRITICAL'],v['logs/error_alert.log']['lines'])")),
    ("E5", "候选① 无决策空间: error_alert.py 判据已是「有就报」",
     j("e=S['A']['error_alert_py'];print(e['MIN_THRESHOLD'],e['THRESHOLD_MULT'],e['level_markers_line'])")),
    ("E6", "候选① 载体误读: error_alert_carrier 是包装器非分诊器",
     j("c=S['A']['carrier_role'];print(c['is_wrapper_not_triager'],c['delegates_to'],"
       "c['zero_output_when_normal'],c['has_severity_grading_logic'])")),
    ("E7", "复盘归因面 (旧 DEB 场景2): 四个真值字段填充率 + 停更天数",
     j("b=S['B'];print(b['n_total'],b['root_cause_category_nonzero'],b['llm_model_nonzero'],"
       "b['human_feedback_nonzero'],b['key_decision_factor_nonzero'],b['last_update'],b['days_stale'])")),
    ("E8", "候选② 价值上限: LLM 花销 7d/30d/全时",
     j("c=S['C'];print(c['last_7d']['n_calls'],c['last_7d']['cost_usd'],c['last_30d']['n_calls'],"
       "c['last_30d']['cost_usd'],c['all_time_cost_usd'])")),
    ("E9", "候选② 真要省钱该动谁: 30 天按模型成本 Top3",
     j("c=S['C']['by_model_30d'];print([(x['model'],x['n'],x['cost']) for x in c[:3]])")),
    ("E10", "唯一活口 F14 现状: 体量翻倍但少数类更薄",
     j("e=S['E'];print(e['n_lines'],e['n_labeled'],e['n_distinct_days'],e['per_day'],e['date_span'])")),
    ("E11", "F14 判据可行性: 各真值字段少数类 n 与失衡比",
     j("i=S['E']['class_imbalance'];print([(k,v['minority_n'],v['ratio']) for k,v in sorted(i.items())])")),
    ("E12", "D5 前提核验: KR1 冻结窗是否仍「恒为 1」",
     j("f=S['F'];print(f['status'],f['n_unique'],f['n_closed'],f['n_target'],f['claim_holds'])")),
    ("E13", "新 DEB §1.3 六个代码锚点是否真实",
     j("d=S['D']['anchors'];print(len(d),sum(1 for x in d if x['exists'] and x['line_in_range']))")),
    ("E14", "前案 F12/F13/F14 原文是否含「整体关闭」",
     "grep -c \"Jev 议题应整体关闭\" docs/debates/DEB-20260924-jev-bounded-judgment-layer-feasibility.md"),
    ("E15", "前案 Q3 作废原文 (候选① 与之同轴)",
     "grep -c \"Q3.*作废\\|作废（§4.5.3 场景 1）\" docs/debates/DEB-20260924-jev-bounded-judgment-layer-feasibility.md"),
]

# 实跑取值
e_rows = []
for eid, title, cmd in ROWS:
    import subprocess
    r = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True,
                       cwd=REPO, timeout=420)
    got = (r.stdout or "").strip().replace("\n", "\\n")
    err = (r.stderr or "").strip()[:100]
    e_rows.append((eid, title, cmd, got, r.returncode, err))
    print(f"{'✅' if r.returncode == 0 and got else '❌'} {eid} rc={r.returncode} got={got[:88]!r}")
    if r.returncode != 0:
        print(f"     err={err!r}")

json.dump([{"id": a, "title": b, "cmd": c, "got": d, "rc": e} for a, b, c, d, e, _ in e_rows],
          open("/root/.hermes/cache/scratch/e_jev.json", "w"), ensure_ascii=False, indent=1)
bad = [a for a, b, c, d, e, _ in e_rows if e != 0 or not d]
print(f"\nE 表 {len(e_rows)} 条, 不可跑/空: {bad or '无'}")
