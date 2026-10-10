"""从探针实跑输出程序化生成 §27.7 E 表期望值 (禁手抄)。"""
import json
import subprocess

REPO = "/opt/ftseek/FTSeek-US"
PROBE = "PYTHONPATH=. ./venv/bin/python scripts/deb20261009_strategy_opt_probe.py"
PYJ = "./venv/bin/python -c"


def j(expr: str) -> str:
    """探针 JSON → python 抽字段, 输出确定性字面量。"""
    pre = "import json,sys; j=json.load(sys.stdin); S={s['segment']:s for s in j['segments']}; "
    return f'{PROBE} | {PYJ} "{pre}{expr}"'


ROWS = [
    ("E53", "探针八段结构完整 (A–H)",
     j("print(len(S), ''.join(sorted(S)))")),
    ("E54", "KR1 冻结窗: status / 哈希数 / 已结算 / 目标",
     "./venv/bin/python -c \"import json;"
     "oc=json.load(open('data/invariant_check_latest.json'))['config_freeze']['oct_cohort'];"
     "print(oc['status'],oc['n_unique'],oc['n_closed'],oc['n_target'])\""),
    ("E55", "meta_audit P0 逾期条数",
     'grep -c "P0 OPEN" logs/meta_audit.log'),
    ("E56", "410297295 新增键行数 (自称「零行为变更」)",
     'git show 410297295 -- config/playbook_horizon.yaml '
     '| grep -c "^+  overnight_gap_stop_pct: 3.0"'),
    ("E57", "horizon_config 进 exit hash _snapshot 的命中数",
     "grep -c '\"horizon_config\"' app/playbook/config.py"),
    ("E58", "§25.2 因果检验: SL_INITIAL / GAP_STOP 的 n·均持·跨日",
     j("f={x['exit_reason']:x for x in S['C']['families']};a=f['SL_INITIAL'];"
       "b=f['OVERNIGHT_GAP_STOP'];"
       "print(a['n'],a['avg_hold_h'],a['n_cross_day'],b['n'],b['avg_hold_h'],b['n_cross_day'])")),
    ("E59", "仙股剔除敏感性: n·净·剔除后 n·净·符号翻转·费用占比",
     j("d=S['D'];print(d['n_total'],d['all_net'],d['after_exclude_n'],"
       "d['after_exclude_net'],d['net_sign_flips'],d['high_fee_share_of_all_fees_pct'])")),
    ("E60", "墓地桶 + 全样本净 + 费用/毛利比",
     j("e=S['E'];g=e['buckets'][0];"
       "print(g['n'],g['net'],g['wr'],e['sample_net'],e['graveyard_net'],e['fee_to_gross_ratio'])")),
    ("E61", "candidate A 可拦截面: 拦/净/gap/非gap/误杀候选/放行",
     j("f=S['F'];print(f['blocked_n'],f['blocked_net'],f['blocked_gap_n'],f['blocked_gap_net'],"
       "f['blocked_nongap_n'],f['blocked_nongap_net'],"
       "f['blocked_winners_false_kill_candidates'],f['passed_n'],f['passed_net'])")),
    ("E62", "candidate D 第3条命中已否决轴 (DEB-20261008 + DEAD RULE)",
     j("g=S['G'];print(g['deb_20261008_exists'],g['mentions_partial_take_profit'],"
       "len(g['verdict_lines']),g['scale_out_dead_rule'].split()[0])")),
    ("E63", "adaptive_trailing 在 exit hash 白名单的命中数 (0=灰区)",
     'grep -c "adaptive_trailing" app/playbook/config.py'),
    ("E64", "adaptive_trailing 参数实际所在行 (unified_config)",
     'grep -n "adaptive_trailing_target_capture_pct\\|adaptive_trailing_max_drawdown_pct" '
     'config/unified_config.yaml | cut -d: -f1 | tr "\\n" " "'),
]


def main() -> int:
    out = []
    for eid, title, cmd in ROWS:
        r = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True,
                           text=True, cwd=REPO, timeout=420)
        got = (r.stdout or "").strip().replace("\n", "\\n")
        out.append({"id": eid, "title": title, "cmd": cmd,
                    "got": got, "rc": r.returncode,
                    "err": (r.stderr or "").strip()[:120]})
        flag = "✅" if r.returncode == 0 and got else "❌"
        print(f"{flag} {eid} rc={r.returncode}\n   cmd={cmd[:100]}\n   got={got[:120]!r}")
        if r.returncode != 0:
            print(f"   err={(r.stderr or '')[:200]!r}")
    json.dump(out, open("/root/.hermes/cache/scratch/e27.json", "w"),
              ensure_ascii=False, indent=1)
    bad = [o["id"] for o in out if o["rc"] != 0 or not o["got"]]
    print(f"\n总计 {len(out)} 条, 不可跑/空输出: {bad or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
