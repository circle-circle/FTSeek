"""L5 影子健康度 v2 — 修正 v1/probe_shadow_health 的假警报 (读 verdict + 搜 scripts/)。

v1 假警报成因 (实测 3 例):
  · `eth_rth_fade`            写入方在 scripts/shadow_board.py         (v1 只搜 app/)
  · `ext_entry_exec_price_shadow` 写入方在 extended_hours_entry_executor.py (同上)
  · `p1_1_vwap_slope`         verdict=GRADUATED_BY_USER_OVERRIDE       (已毕业, v1 未读 verdict)
⇒ v1 的「僵尸候选」清单不可直接采信; 本版按三证判定:
  ① 注册表 status/verdict (已毕业/已裁否 ⇒ 停更合理, 不是僵尸)
  ② 写入方存在性 (搜 app/ + scripts/ 全域, 排除 shadow_registry 自身与 tests)
  ③ 产物龄 (最后写入距今 >150h 才算停滞; 阈值沿用 v1 便于对照)

输出: 三档 — 已裁结(status/verdict 终态) / 真僵尸(无终态 ∧ 无写入方 ∧ 产物停滞) /
      需人工核(有写入方但产物停滞, 可能是上游断供而非影子死)
"""
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

R = pathlib.Path("/opt/ftseek/FTSeek-US")
sys.path.insert(0, str(R))
out = []


def P(s=""):
    out.append(s)
    print(s)


REG = (R / "app/playbook/shadow_registry.py").read_text(encoding="utf-8")

# 按 id= 切块 (每块到下一个 id= 或文件尾)
# ⚠️ 勿在正则里重复 (?m) 内联 flag (Python 3.11: "global flags not at the start")
entries = []
ids = [(mm.start(), mm.group(1)) for mm in re.finditer(r'id\s*=\s*"([^"]+)"', REG)]
for k, (pos, sid) in enumerate(ids):
    end = ids[k + 1][0] if k + 1 < len(ids) else len(REG)
    blk = REG[pos:end]

    def field(name):
        # ⚠️ status 是**枚举**写法 `status=ShadowStatus.GRADUATED`, 不是字符串 ⇒
        #   v2 首版正则 `name\s*=\s*f?"([^"]*)"` 恒不匹配 ⇒ 终态档 n=0 是假读数,
        #   p1_1_vwap_slope 明明 GRADUATED_BY_USER_OVERRIDE (:499) 却被判「无终态」。
        fm = re.search(name + r'\s*=\s*ShadowStatus\.([A-Z_]+)', blk)
        if fm:
            return fm.group(1)
        fm = re.search(name + r'\s*=\s*[f]?"([^"]*)"', blk)
        if fm:
            return fm.group(1)
        # verdict 常写在 history 字典里: {"date": ..., "verdict": "GRADUATED_BY_USER_OVERRIDE"}
        fm = re.search(r'"' + name + r'"\s*:\s*"([^"]*)"', blk)
        return fm.group(1) if fm else None

    entries.append({
        "id": sid,
        "status": field("status") or "",
        "verdict": field("verdict") or "",
        "data_path": field("data_path") or "",
        "eval_script": field("eval_script") or "",
    })

P(f"注册表影子条目 n={len(entries)}")
NOW = datetime.now(timezone.utc)
# 终态集合 = 注册表 :17 生命周期定义的终局态 (GRADUATED/KILLED/SUPERSEDED)
#   + history verdict 常见写法; OBSERVING / READY_TO_EVALUATE **不是**终态 (仍在跑)。
TERMINAL = re.compile(r"GRADUATED|KILLED|SUPERSEDED|REJECTED|RETIRED|CLOSED|"
                      r"DEPRECATED|ABANDONED|WONTFIX|DISABLED_BY_USER|OBSOLETE", re.I)

buckets = {"terminal": [], "zombie": [], "manual": [], "healthy": [], "nopath": []}

for e in entries:
    sid = e["id"]
    # ① 终态判定 (已毕业/已裁否/已退役 ⇒ 停更合理)
    term_hit = None
    for fld in ("verdict", "status"):
        v = e[fld]
        if v and TERMINAL.search(v):
            term_hit = f"{fld}={v}"
            break
    # ② 产物龄
    age_h = None
    dp = e["data_path"].split("?")[0].split("#")[0].strip()
    if dp:
        cand = list(R.glob(dp)) if ("*" in dp or not dp.startswith("/")) else [pathlib.Path(dp)]
        cand = [c for c in cand if c.exists() and c.is_file()]
        if not cand and dp.startswith("data/"):
            cand = [p for p in (R / dp).parent.glob((R / dp).name) if p.exists()]
        if cand:
            newest = max(c.stat().st_mtime for c in cand)
            age_h = (NOW.timestamp() - newest) / 3600.0
    else:
        age_h = None
    if dp and age_h is None:
        buckets["nopath"].append((sid, dp))
    # ③ 写入方 (搜 app/ + scripts/, 排除注册表自身与测试)
    # ⚠️ 必须排除 app/review/shadow_lexicon.py — 它是**白话词典 SSOT** (实测写入产物
    #   计数 = 0, 文件 docstring 自述「判决书与影子验收清单共用的单一真源」),
    #   但词典里**必然提到每个影子的 id** ⇒ grep -rl 对它恒假阳性,
    #   会把真僵尸误判成「有写入方 ⇒ 需人工核」。同理排除其他纯文档/词典/推送渲染层。
    NON_WRITER = ("shadow_registry", "shadow_lexicon", "/test", "tests/",
                  "bundle_render", "actionability_diagnostics", "pending_adjudication_report",
                  # ⚠️ 必须排除**本 DEB 家族的分析脚本**: 本脚本「v1 假警报复核」段逐字提到
                  #   eth_rth_fade / ext_entry_exec_price_shadow / p1_1_vwap_slope 三个 id ⇒
                  #   入库到 scripts/ 后 grep -rl 会把**它自己**算成写入方 (实测已发生:
                  #   写入方=['shadow_board.py','deb20261002_shadow_health.py'])。
                  #   后果是真僵尸被误判「有写入方 ⇒ 需人工核」= 假绿。
                  # ⚠️ 但**不可**顺手排除 `probe_`/`audit_` 前缀: `probe_eth_short_open.py`
                  #   是 eth_short_probe 的**真写入方**, 排掉它会把活影子误判成真僵尸
                  #   (制造新假警报)。判据不是文件名, 而是「该文件是否产生产物」—
                  #   deb20261002_* 是本 DEB 的一次性分析脚本, 确定不产影子产物。
                  "deb20261002_")
    wr = subprocess.run(
        ["grep", "-rl", "--include=*.py", "-F", sid, "app", "scripts"],
        cwd=R, capture_output=True, text=True)
    writers = [w for w in (wr.stdout or "").split("\n")
               if w and not any(nw in w for nw in NON_WRITER)]
    e["age_h"] = age_h
    e["writers"] = writers
    e["term_hit"] = term_hit

    if term_hit:
        buckets["terminal"].append(e)
    elif age_h is not None and age_h > 150:
        if writers:
            buckets["manual"].append(e)      # 有写入方但停滞 ⇒ 疑上游断供, 非影子死
        else:
            buckets["zombie"].append(e)      # 无终态 ∧ 无写入方 ∧ 停滞 ⇒ 真僵尸
    else:
        buckets["healthy"].append(e)


def dump(title, lst, show_writers=True):
    P(f"\n{'═' * 4}\n{title}  n={len(lst)}\n{'═' * 4}")
    for e in sorted(lst, key=lambda x: -(x["age_h"] or 0)):
        a = f"{e['age_h']:.0f}h" if e["age_h"] is not None else "?"
        w = ",".join(pathlib.Path(x).name for x in e["writers"][:2]) if show_writers else ""
        P(f"  {e['id']:<46} 产物龄={a:<8} 终态={e['term_hit'] or '-':<28} 写入方={w or '(无)'}")


dump("① 已裁结 (终态, 停更合理 ⇒ 不是僵尸)", buckets["terminal"], show_writers=False)
dump("② 真僵尸 (无终态 ∧ 无写入方 ∧ 产物停滞>150h) ⇒ 可停采集", buckets["zombie"])
dump("③ 需人工核 (有写入方但产物停滞 ⇒ 疑上游断供, 停采集会掩盖断供)", buckets["manual"])
P(f"\n④ 健康 (产物龄≤150h 或有终态): n={len(buckets['healthy'])}")
P(f"⑤ data_path 无法定位产物 (口径问题, 非影子死): n={len(buckets['nopath'])}")
for sid, dp in buckets["nopath"][:12]:
    P(f"     {sid:<44} data_path={dp[:60]}")

P("\n═══ v1 假警报复核 (三例是否已被本版正确归类) ═══")
for sid in ("eth_rth_fade", "ext_entry_exec_price_shadow", "p1_1_vwap_slope"):
    e = next((x for x in entries if x["id"] == sid), None)
    if not e:
        P(f"  🔴 {sid}: 注册表未找到")
        continue
    got = [b for b, l in buckets.items() if e in l]
    P(f"  {sid:<40} 归入={got[0] if got else '?'}  写入方={[pathlib.Path(w).name for w in e['writers']][:2]}  终态={e['term_hit'] or '-'}")

# 输出走 stdout (与其他 deb20261002_* 一致): verify_claims 靠 stdout 逐字比对,
#   写死 scratch 绝对路径会让复查者拿不到产物 ⇒ 已改为不落盘。
print("\n[影子健康度审计结束]")
