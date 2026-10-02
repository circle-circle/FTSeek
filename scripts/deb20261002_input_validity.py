"""输入有效性五层审计 — 每层「既有守卫 vs 真缺口」+ 死门四条件判定

用户要求 (逐字): 「现有的机制需要确保输入的信息新鲜度, 及时性, 准确率,
价值识别层面优化, 泛化以及可迭代, 在这一方面**不做预测**, 但确保输入的有效性」
⇒ 约束: 只审计**卫生层**(输入本身是否有效), 不提议任何预测性门 (与 C12 同源)。

五层与预注册判据 (读数前写死):
  L1 新鲜度: 输入的时间龄是否有守卫 ∧ 守卫是否活着
  L2 及时性: 采集→消费链路延迟是否可观测
  L3 准确率: 输入值是否有 sanity/交叉验/几何一致性
  L4 价值识别: 输入是否按相关度/重要度筛选排序 (vs 只截条数)
  L5 泛化+可迭代: 测量系统变更是否触发样本重置 ∧ 影子是否有结算器 (非僵尸)

死门四条件 (MEMORY 铁律, 全中 ⇒ 删门非加守卫):
  D1 比较值同源同快照  D2 错相位陈旧=假阳  D3 产物被仲裁丢弃  D4 判据字面量≠生产值域
"""
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

R = Path("/opt/ftseek/FTSeek-US")
SK = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
out = []
P = out.append


def head(s):
    P(f"\n{'═'*72}\n{s}\n{'═'*72}")


def run(cmd, timeout=120):
    r = subprocess.run(cmd, shell=True, cwd=R, capture_output=True, text=True, timeout=timeout)
    return ((r.stdout or "") + (r.stderr or "")).strip()


def rg(pattern, paths, flags="-rn", limit=6, fixed=False):
    """grep 包装 — 用 list 形式传参, 彻底避开 shell 引号/repr 陷阱。
    ⚠️ 教训 (本脚本 v1 三处假读数): 用 `repr(pat)` 拼进 shell 串会给 grep 传带引号模式
       ⇒ 恒不命中 = 假红; 子串计数 (str.count) 过宽 ⇒ 把别的同名前缀脚本算进来 = 假绿。
    """
    args = ["grep", flags] + (["-F"] if fixed else []) + [pattern] + paths
    r = subprocess.run(args, cwd=R, capture_output=True, text=True, timeout=120)
    ls = [l for l in (r.stdout or "").split("\n") if l.strip()]
    return "\n".join(ls[:limit])


def count_in(haystack, needle, exact_line_token=None):
    """精确计数: 默认子串; 给 exact_line_token 时要求整行含该 token (防前缀污染)。"""
    if exact_line_token is None:
        return haystack.count(needle)
    return sum(1 for l in haystack.split("\n") if needle in l and exact_line_token in l)


def read(p, lo=None, hi=None):
    fp = Path(p) if Path(p).is_absolute() else R / p
    if not fp.exists():
        return ""
    ls = fp.read_text(errors="replace").split("\n")
    return "\n".join(ls[(lo or 1) - 1:hi])


# ══════════ L1 新鲜度 ══════════
head("L1 新鲜度 — 输入龄期守卫现状")

P("\n[1.1] 现役龄期守卫 (config 实值, 非代码默认)")
# ⚠️ v1 用 repr(_pat) 拼 shell 串 ⇒ grep 收到带引号模式 ⇒ 8 键全报「无此键」= 假红。
#    改用 rg() list 传参; 并保留 v1 的错误读数作对照, 证明修复有效 (阳性对照纪律)。
_CFG = ["config/unified_config.yaml"]
for k in ("pretrade_quote_gate_enabled", "pretrade_max_quote_age_sec", "pretrade_max_spread_pct",
          "entry_freshness_gate_enabled", "entry_max_quote_age_sec", "max_age_minutes",
          "candidate_scan_max_age_hours", "entry_archetype_ttl_seconds"):
    h = rg(k + ":", _CFG, flags="-n", limit=2)
    P(f"  {k:<32} {h.replace(chr(10), ' | ') if h else '🔴 yaml 无此键'}")

P("\n[1.2] 死门签名核实 (config:1192-1200 自述 + 代码三点连线)")
P("  ① timestamp 写入点 (futu_lv3_provider):")
for ln in run("grep -n '\"timestamp\": time.time()' app/data/futu_lv3_provider.py | head -6").split("\n"):
    P(f"     {ln.strip()}")
P("  ② 设计意图 (provider:243-246 注释, 说明为何用 time.time()):")
for l in read("app/data/futu_lv3_provider.py", 243, 246).split("\n"):
    if l.strip():
        P(f"     {l.strip()[:110]}")
P("  ③ push 缓存 TTL (market_data_router) — v1 用 head -3 截掉了真行, 改搜字面量 '=5':")
_ttl = rg("cache_ttl_seconds=5", ["app/data/market_data_router.py"], flags="-n", limit=4)
P("     " + (_ttl.replace("\n", "\n     ") if _ttl else "🔴 未找到 TTL=5 字面量"))
P("  ④ check_quote_freshness 是否提前返回 (黑名单先判 ⇒ 龄期分支走不到):")
h = run("grep -rn 'def check_quote_freshness' app/ | head -2")
P(f"     定义: {h}")
if h:
    fp = h.split(":")[0].strip()
    ln0 = int(h.split(":")[1])
    body = read(fp, ln0, ln0 + 34)
    P(f"     函数体前 34 行里的 return 位置:")
    for i, l in enumerate(body.split("\n")):
        if re.search(r"\breturn\b|blacklist|降级|if .*stale|age", l):
            P(f"       +{i:<3} {l.strip()[:96]}")

P("\n[1.3] 影子实测 (ue_quote_freshness_shadow, 我本轮重跑 eval 的真实裁决)")
ev = R / "data/shadow/ue_quote_freshness_shadow_eval_latest.json"
if ev.exists():
    d = json.loads(ev.read_text())
    for k in ("n_rounds", "min_rounds", "verdict", "age_genuine_p50", "age_genuine_p95",
              "age_genuine_max", "n_exchange_stamp_available", "n_rows", "generated_at",
              "threshold_used", "n_stale_genuine", "n_newly_rejected_genuine"):
        if k in d:
            P(f"  {k:<30} {d[k]}")
    P(f"  verdict_text: {d.get('verdict_text','')}")
jl = R / "data/shadow/ue_quote_freshness_shadow.jsonl"
if jl.exists():
    ls = [json.loads(l) for l in jl.read_text().split("\n") if l.strip()]
    ds = sorted({(x.get("date") or "")[:10] for x in ls})
    P(f"  jsonl 行数={len(ls)} 去重轮次={len(ds)} 跨度={ds[0]}→{ds[-1]}")
    P(f"  ⇒ eval generated_at 若早于 jsonl 末日 = 结算器停更 (规则55 僵尸形态)")

P("\n[1.4] 结算器 cron 挂载 (影子采集 vs 结算器, 二者可分离)")
_cron_all = run("crontab -l 2>/dev/null")
# ⚠️ v1 用 _cron_all.count("shadow_eval") ⇒ 把 ue_tier_shadow_eval / ue_tier_gate_shadow_eval
#    等【别的影子】的结算器也算进来, 得 4 = 假绿, 掩盖了真缺口。
#    正确口径: 只数含完整脚本名 ue_quote_freshness_shadow_eval 的行, 且排除注释行。
def _cron_hits(token):
    return [l for l in _cron_all.split("\n")
            if token in l and not l.lstrip().startswith("#")]
_collect = _cron_hits("ue_quote_freshness_shadow.py")
_eval = _cron_hits("ue_quote_freshness_shadow_eval")
P(f"  采集器 ue_quote_freshness_shadow.py       cron 有效行: {len(_collect)}")
# ⚠️⚠️ 结算器**没有 cron 是正确的**, 不是缺口 (2026-10-02 自查更正):
#   注册项 `eval_on_demand=True` (shadow_registry.py:1798) + notes「届时手工/会话跑 eval_script 呈裁」
#   我 R4 首版给它挂了 `55 5 * * 6` = 与该字段语义相悖的多余载体 + 违反台账纪律1(新增 cron 须入账,
#   基线里没有我这条), 已撤销。**权威依据不是我的推断, 是注册表字段语义本身**
#   (shadow_registry.py:99-100 逐字): 「False=应有 cron/调度挂载 (挂载核对话语权);
#   True=按需手工/裁决日触发 (哨兵跳过挂载核对, 2026-09-01 挂载核对哨兵引入)」。
#   同批对照实证: `ue_tier_gate` eval_on_demand=**False** ∧ 有周六 05:52 cron ⇒ 一致;
#   本影子 eval_on_demand=**True** ∧ notes 逐字「届时**手工/会话跑** eval_script 呈裁」⇒ 不该有 cron。
#   原「cron 0 命中 ⇒ 无载体 ⇒ 僵尸」的推断是**错的**: 判「有没有人裁」要看
#   ①注册项 eval_on_demand 字段语义 ②notes 是否点名到期触发方式 ③裁决产物 generated_at,
#   不是看 cron 有没有挂 (与 ⑬⑨「判进程活着不得只看 cron」同族, 这次是反向的同一错误)。
P(f"  结算器 ue_quote_freshness_shadow_eval.py  cron 有效行: {len(_eval)}"
  + ("  ✅ 无 cron = 正确 (注册项 eval_on_demand=True ⇒ 按需手工/裁决日触发)" if not _eval else ""))
_on_demand = run(f"grep -c 'eval_on_demand=True' {R}/app/playbook/shadow_registry.py")
P(f"  注册表 eval_on_demand=True 登记数: {_on_demand}")
# E48 的权威依据: 从注册表**运行时对象**取 (不 grep 源码文本 —— notes 是 Python 字符串
#   跨行拼接, `grep -o '届时.*eval_script 呈裁'` 实测**匹配不到** = 空输出; 且 sed 取行会
#   把注释换行切碎。运行时对象是唯一不会说谎的形态)。
# ⚠️ 不用子进程: 首版写 `run(f"... -c {_shq(_py)}")` 而 `_shq` 在本脚本**根本没定义**
#   ⇒ NameError 被下面的 try/except 吞成「🔴 提取失败」(自己给自己造假红)。
#   本脚本本就在 `PYTHONPATH=.` 下跑 ⇒ 直接 import 即可, 少一层 shell 就少一层引号地狱。
try:
    from app.playbook.shadow_registry import REGISTRY as _REG
    _e = [x for x in _REG if x.id == "ue_quote_freshness_shadow"][0]
    P(f"  注册项真值: id={_e.id} eval_on_demand={_e.eval_on_demand} status={_e.status.name}")
    P("  挂载哨兵语义 (registry 字段注释): eval_on_demand=False⇒应有 cron 挂载; "
      "True⇒按需手工/裁决日触发, 哨兵跳过挂载核对")
    _n = " ".join((_e.notes or "").split())
    # ⚠️ 锚点必须是「届时」而非「到期触发」: notes 里「到期触发」出现两次,
    #   第一处是「②裁决器 + **到期触发人** — 本条注册 + eval 脚本…」(讲注册动机),
    #   我引用的是第二处「**到期触发**: …届时手工/会话跑 eval_script 呈裁」(讲触发方式)
    #   ⇒ 用 find('到期触发') 会取到无关那段 = 引用了原文但答非所问。
    _i = _n.find("届时")
    P(f"  notes 到期触发方式原话: …{_n[max(0, _i - 46):_i + 60]}" if _i >= 0
      else "  🔴 notes 未找到「届时…eval_script 呈裁」段")
except Exception as _exc:  # noqa: BLE001 — 审计脚本不得因单段失败整体崩
    # ⚠️ 变量名不得复用 `_e` (注册项对象): 否则 except 分支会把注册项覆盖成异常对象,
    #   后续任何引用都变成异常 —— 这类 shadowing 在审计脚本里最难查 (只在失败路径出现)。
    P(f"  🔴 注册项真值提取失败: {type(_exc).__name__}: {_exc}")
_ev = R / "data/shadow/ue_quote_freshness_shadow_eval_latest.json"
if _ev.exists():
    _d = json.loads(_ev.read_text(encoding="utf-8"))
    P(f"  裁决产物: verdict={_d.get('verdict')}  n_rows={_d.get('n_rows')}"
      f"  n_rounds={_d.get('n_rounds')}/{_d.get('min_rounds')}")
    P(f"  预注册判据 (钉死, 事后改阈值=重新预注册): threshold_used={_d.get('threshold_used')}"
      f"  min_rounds={_d.get('min_rounds')}")
    P(f"  会漂的读数 (不进期望列): age_genuine_p95={_d.get('age_genuine_p95')}"
      f"  p50={_d.get('age_genuine_p50')}  会新拒={_d.get('n_newly_rejected_genuine')}")
    P(f"  generated_at={_d.get('generated_at')}")
else:
    P("  🔴 裁决产物不存在 ⇒ 从未裁过 (这才是真僵尸判据)")
P(f"  [对照] v1 宽子串 'shadow_eval' 命中 = {_cron_all.count('shadow_eval')} (含别的影子 ⇒ 假绿, 已弃用)")
_other = [l.strip()[:70] for l in _cron_all.split("\n")
          if "shadow_eval" in l and "ue_quote_freshness" not in l and not l.lstrip().startswith("#")]
for o in _other[:4]:
    P(f"      别的影子结算器: {o}")

P("\n[1.4b] 死门**行为级对照** (代码级之外必须有这一条: 门恒 False ⇒ 日志零命中)")
# ⚠️ 本段是 E47 的行为级证据。MEMORY 死门四条件的 D4 (判据字面量 ≠ 生产值域 ⇒ 恒 False)
#   只能由「代码结构」推出, 但**结论**必须由「行为」证伪: 门若真在工作, 日志必有命中。
#   首版漏了本段 ⇒ E47 只有代码级证据, 复查者无法复算结论 (我在聊天里手工查过但没入库,
#   等于让复查者拿不到我的证据 = 违反「E 表每行可复算」纪律)。
# 阳性对照 (chase_cap拦截) 必须有命中: 证明日志文件本身在写、grep 口径没错,
#   否则「0 命中」可能只是日志被轮转/文件不存在造成的假证 (阴性结果必须有阳性对照)。
# ⚠️ 用 Path.glob 而非 glob.glob: 本脚本 import 区没有 glob (只有 json/re/subprocess/
#   collections/pathlib) ⇒ 首版写 glob.glob 会 NameError。
_log_glob = sorted(str(p) for p in (R / "logs").glob("ftseek_us.log*"))
P(f"  日志文件数 = {len(_log_glob)}")
if not _log_glob:
    P("  🔴 日志文件不存在 ⇒ 本段无法判定 (阴性结果不可用, 须先确认日志在写)")
else:
    _joined = " ".join(_log_glob)
    _pos = run(f"grep -hF 'chase_cap拦截' {_joined} 2>/dev/null | wc -l")
    # ⚠️ 行数会**单调增长**(拦截一直在发生) ⇒ 不可写进 E 表期望列 (本轮真踩过:
    #   期望写 9 行, 数小时后变 16 行 ⇒ verify_claims 假红 E47)。
    #   故拆两行: 含数字的行只作现场参考, 稳定断言行(只说「有命中/零命中」)才进期望。
    P(f"  [阳性对照] chase_cap拦截 = {_pos} 行 (计数单调增长, 不作期望值)")
    _pos_ok = _pos.strip() not in ("", "0")
    P("  [阳性对照] " + ("✅ 有命中 ⇒ 日志在写且 grep 口径正确 (阴性结果可用)"
                          if _pos_ok else
                          "🔴 零命中 ⇒ 日志缺失/轮转, 下面的 0 命中不可信 (阴性结果不可用)"))
    for _kw in ("数据新鲜度门拒绝开仓", "报价健康门拒绝下单"):
        _n = run(f"grep -hF '{_kw}' {_joined} 2>/dev/null | wc -l")
        _n_zero = _n.strip() == "0"
        P(f"  {_kw} = {_n} 行 (计数)")
        P(f"  {_kw} " + ("✅ 零命中 ⇒ 门从未拦过任何单 (恒 False 的行为级证据)"
                          if _n_zero else f"🔴 有 {_n} 命中 ⇒ 门在工作, 死门结论作废"))
    P("  结论: 阳性对照有命中 ∧ 两个新鲜度门零命中 ⇒ 门结构性恒 False (D4 成立), "
      "非「恰好没触发过」")

P("\n[1.5] data_freshness_watchdog WATCHLIST 覆盖面 (人工登记 ⇒ 登记不到的源死了无人知)")
wl = read("scripts/data_freshness_watchdog.py", 56, 88)
n_wl = len([l for l in wl.split("\n") if re.match(r'\s*\("', l)])
P(f"  WATCHLIST 条目数 = {n_wl}")
for l in wl.split("\n"):
    m = re.match(r'\s*\("([^"]+)",\s*(\d+),\s*"([^"]*)"', l)
    if m:
        P(f"    {m.group(1):<50} 阈={m.group(2):<4}h {m.group(3)[:34]}")
P("  脚本自述的局限 (逐字):")
for l in read("scripts/data_freshness_watchdog.py", 1, 20).split("\n"):
    if "登记不到" in l or "人工登记" in l or "降级不报警" in l:
        P(f"    {l.strip()[:112]}")

P("\n[1.6] DA 新闻/研报输入是否按龄过滤 (vs 只渲染时间戳给 LLM 自己判)")
syn = SK / "llm_synth.py"
if syn.exists():
    s = syn.read_text(errors="replace")
    for kw, lbl in ((r"news\[:\d+\]", "新闻截断"), (r"research\[:\d+\]", "研报截断"),
                    (r"sentiment\[:\d+\]", "社区截断"), (r"peers\[:\d+\]", "peer截断")):
        m = re.findall(kw, s)
        P(f"  {lbl:<10} 命中 {sorted(set(m)) if m else '无'}")
    # ⚠️ 正则必须提到 f-string 外: Python <3.12 禁止表达式部分含反斜杠
    _age_re = re.compile(r"(now|time\.time\(\)|_ts)\s*-\s*.*publish_time"
                         r"|publish_time.*(max_age|>\s*\d+\s*\*)")
    P(f"  按龄过滤 (publish_time 与 now 比较后丢弃): {len(_age_re.findall(s))} 处")
    P(f"  low_relevance 标记消费: {len(re.findall(r'low_relevance', s))} 处")
    P("  v0.86 时间戳修复注释 (说明此前 LLM 无法分辨新闻龄):")
    for l in s.split("\n"):
        if "91 秒前还是 3 天前" in l or "延迟感知为零" in l:
            P(f"    {l.strip()[:118]}")

# ══════════ L2 及时性 ══════════
head("L2 及时性 — 采集→消费链路延迟可观测性")
P("\n[2.1] delay_ms 字段 (provider 侧自报延迟)")
# ⚠️ 首版 `head -6` 被 P0-2 新增的**注释块**(provider.py:185-191)占满 ⇒ 真赋值行全被截掉,
#   复查者只看得到病灶描述、看不到修复是否落地 (E50 锚串 quote_age_ms 必 0 命中 = 假红)。
#   改为: 排注释行后只看真赋值/真消费, 并分别计数三个可验证断言。
_d_assign = run("grep -rn 'delay_ms' app/data/*.py | grep -v '^ *#' | grep -vE ':[0-9]+: *#' | head -8")
P("  真赋值/消费行 (排除注释):")
P("  " + _d_assign.replace("\n", "\n  ")[:900])
# ⚠️ 期望值必须用**精确口径**实测得到, 不能凭印象写 (MEMORY: 判据字面量≠生产值域)。
#   首版两个 grep 口径错: `grep -c unmeasured` 把 provider 注释块也算 ⇒ 报 4 (真值 3);
#   子串 `quote_age_ms` 把 helper 名 `_measured_quote_age_ms` + router 读取行都算 ⇒ 报 6。
#   修正: unmeasured 排注释; quote_age_ms 只数 provider 里的**字典键**(带引号)。
_n_literal = run("grep -rnE '\"delay_ms\" *(:|=) *\"<[0-9]+\"' app/data/*.py | wc -l")
_n_unmeasured = run("grep -rn 'delay_ms.*unmeasured' app/data/futu_lv3_provider.py | grep -vE ':[0-9]+: *#' | wc -l")
_n_age = run("grep -rnE '\"quote_age_ms\"' app/data/futu_lv3_provider.py | wc -l")
_n_router = run("grep -rn 'lv3_quote_age_ms' app/data/market_data_router.py | grep -vE ':[0-9]+: *#' | wc -l")
P(f"  [断言] provider 硬编码字面量残留          = {_n_literal} 处 (期望 0)")
P(f"  [断言] provider delay_ms 赋值 unmeasured = {_n_unmeasured} 处 (期望 3, 排注释)")
P(f"  [断言] provider 写 quote_age_ms 字典键   = {_n_age} 处 (期望 3)")
P(f"  [断言] router 透传 lv3_quote_age_ms      = {_n_router} 处 (期望 2: 写 ts_ctx + 防覆盖集合)")
P("\n[2.2] 链路时戳是否可端到端还原 (采集→桥→DB→执行)")
for kw, lbl in (("observed_at", "影子采集时刻"), ("created_at", "指令创建"),
                ("entry_time", "成交时刻"), ("scored_at", "评分时刻")):
    n = run(f"grep -rl '{kw}' app/ --include='*.py' 2>/dev/null | wc -l")
    P(f"  {lbl:<14} ({kw}) 出现于 {n} 个文件")
P("\n[2.3] 既有延迟类影子/台账")
P("  " + (run("ls data/shadow/*.jsonl 2>/dev/null | head -20").replace("\n", "\n  ")))

# ══════════ L3 准确率 ══════════
head("L3 准确率 — 输入值的 sanity / 交叉验 / 几何一致性")
P("\n[3.1] 现役校验器清单")
for pat, lbl in (("def _?validate", "validate*"), ("sanity", "sanity*"),
                 ("cross_check", "cross_check"), ("geometry_conflict", "几何一致性"),
                 ("_price_in_range", "价格区间闸"), ("plausib", "plausibility")):
    # ⚠️ 必须 -rnE: v1 用 BRE (`grep -rn`), `def _?validate` 的 `?` 被当**字面量**
    #   ⇒ 恒不命中报 0 = 假红 (真值 41)。ERE 下 `_?`=可选下划线才正确。
    h = run(f"grep -rnE '{pat}' app/ ~/.hermes/skills/deep-stock-analysis/scripts/ --include='*.py' 2>/dev/null | wc -l")
    P(f"  {lbl:<14} 命中 {h} 处")
P("\n[3.2] 本轮已落地的准确率守卫 (RFC-120 系列)")
for pat in ("chase_cap_inverted_short", "knife_geometry_conflict", "EXT_ENTRY_MIN_PRICE"):
    h = run(f"grep -rn '{pat}' app/ ~/.hermes/skills/deep-stock-analysis/scripts/ --include='*.py' 2>/dev/null | head -2")
    P(f"  {pat}:")
    for l in h.split("\n")[:2]:
        if l.strip():
            P(f"    {l.strip()[:112]}")
P("\n[3.3] DA 溯源器 (规则21: 检测器全量命中=怀疑检测器) 现状")
P("  " + run("grep -rn 'DA_SOURCE_VERIFY_MODE\\|source_verify\\|未溯源' ~/.hermes/skills/deep-stock-analysis/scripts/*.py 2>/dev/null | head -5").replace("\n", "\n  ")[:600])
P("\n[3.4] 双源交叉验 (富途 vs Finnhub vs AlphaVantage)")
P("  providers: " + run("ls app/data/*provider*.py 2>/dev/null | xargs -n1 basename | tr '\\n' ' '"))
P("  交叉验实现: " + (run("grep -rln 'cross.*valid\\|双源\\|两源' app/data/*.py | head -3") or "🔴 无"))

# ══════════ L4 价值识别 ══════════
head("L4 价值识别 — 输入是否按相关度/重要度筛选 (vs 只截条数)")
if syn.exists():
    s = syn.read_text(errors="replace")
    P("\n[4.1] 各输入段落的筛选机制 (逐段)")
    # ⚠️ 引号形态必须三态兼容: v1 用 `f?"""?` **要求≥2个引号** ⇒ 只匹配三引号段落,
    #   漏掉 `sections.append(f"## 最新新闻...")` 这类单引号段落 ⇒ 报 12 段且误报
    #   「大盘环境不存在」(实际存在)。真值 19 段。
    #   `["']{1,3}` = 1~3 个引号(单/双/三引号皆可), f 前缀可选。
    segs = re.findall(r'sections\.append\(f?["\']{1,3}\s*##\s*([^\n"\'\\]{2,28})', s)
    P(f"  prompt 注入段落数 = {len(segs)}")
    for nm in segs[:26]:
        P(f"    · {nm.strip()}")
    P("\n[4.2] 排序/相关度机制命中")
    for pat, lbl in ((r"sort(ed)?\(.*key=", "排序"), (r"relevan", "相关度"),
                     (r"importan|salien|重要", "重要度"), (r"score.*>|阈值", "打分门"),
                     (r"\[:\d+\]", "截断(无筛选)")):
        n = len(re.findall(pat, s))
        P(f"  {lbl:<16} {n} 处")
    P("\n[4.3] news_v2 质量层 (仓内独立模块)")
    P("  " + run("ls app/news_v2/quality/*.py 2>/dev/null | xargs -n1 basename | tr '\\n' ' '"))
    P("  relevance_filter 判据:")
    for l in read("app/news_v2/quality/relevance_filter.py", 1, 40).split("\n"):
        if re.search(r"def |THRESH|阈值|score|reject|low_relevance", l):
            P(f"    {l.strip()[:104]}")
    P("\n[4.4] 未被价值层覆盖的输入 (缺口候选)")
    for kw in ("大盘环境", "基本面指标", "资金面", "技术面", "异动", "内部人"):
        seg_hit = any(kw in x for x in segs)
        filt = len(re.findall(rf"{kw}.*(relevan|sort|score|filter)", s))
        P(f"  {kw:<10} 段落存在={seg_hit}  同段价值过滤命中={filt}")

# ══════════ L5 泛化 + 可迭代 ══════════
head("L5 泛化 + 可迭代 — 测量系统变更样本重置 ∧ 影子有结算器")
P("\n[5.1] 影子注册表: 有采集器但无结算器 = 死影子 (skill 铁律)")
reg = read("app/playbook/shadow_registry.py")
ids = re.findall(r'id="([a-z_0-9]+)"', reg)
P(f"  注册影子数 = {len(ids)}")
P("  " + ", ".join(ids[:40]))
P("\n[5.2] 每个影子的 eval/结算脚本是否入库 (死影子检测)")
ev_files = set(p.name for p in (R / "scripts").glob("*eval*.py")) | \
           set(p.name for p in (R / "scripts").glob("*settle*.py")) | \
           set(p.name for p in (R / "scripts").glob("*report*.py"))
P(f"  scripts/ 下 eval|settle|report 脚本数 = {len(ev_files)}")
miss = []
for i in ids:
    base = i.replace("_shadow", "").replace("_gate", "")
    if not any(base[:12] in f for f in ev_files):
        miss.append(i)
P(f"  未匹配到结算脚本的影子 ({len(miss)}): {', '.join(miss[:24])}")
P("\n[5.3] cron 挂载: 影子采集 vs 结算器 (逐个核)")
cron = run("crontab -l 2>/dev/null")
_cron_lines = [l for l in cron.split("\n") if not l.lstrip().startswith("#")]
P("  ⚠️ 口径: 只数非注释行, 且要求命中【完整影子 id】(v1 用 re.escape(id) 子串匹配,")
P("     会把 id 互为前缀的影子算重 ⇒ 假绿; 这里额外报出该行实际脚本名供人工核)")
for i in ids[:26]:
    hit = [l for l in _cron_lines if i in l]
    if not hit:
        P(f"  {i:<44} cron 命中=0  🔴 无载体")
    else:
        # 提取该行里的脚本名, 便于核是采集器还是结算器
        names = sorted({m for l in hit for m in re.findall(r"([\w./-]+\.py)", l)})
        P(f"  {i:<44} cron 命中={len(hit)}  脚本={', '.join(n.split('/')[-1] for n in names)[:60]}")
P("\n[5.4] 测量系统变更 ⇒ 样本重置 (MIN_TRADE_DATE 类闸)")
P("  " + run("grep -rn 'MIN_TRADE_DATE\\|样本重置\\|measurement.*reset' scripts/ app/ --include='*.py' 2>/dev/null | head -6").replace("\n", "\n  ")[:700])
P("\n[5.5] 归因字段可用性 (本轮 F7 缺口: model_used 被兜底污染 ⇒ 按模型迭代失真)")
P("  " + run("grep -rn 'or \"deterministic\"' app/playbook/pipeline.py | head -2").replace("\n", "\n  "))
P("  " + run("grep -rn \"model_used='deterministic'\" app/playbook/db.py | head -2").replace("\n", "\n  "))
# ⚠️ 进程内直查 (不 shell out — 三层嵌套引号必炸)
import sqlite3 as _sq
try:
    _c = _sq.connect("file:data/ftseek_us.db?mode=ro", uri=True)
    _tot = _c.execute("SELECT COUNT(*) FROM playbook_instructions "
                      "WHERE decision_id LIKE 'DA-%' AND date>='2026-09-01' AND date < '2026-10-02 00:00:00'").fetchone()[0]
    _det = _c.execute("SELECT COUNT(*) FROM playbook_instructions "
                      "WHERE decision_id LIKE 'DA-%' AND date>='2026-09-01' AND date < '2026-10-02 00:00:00' "
                      "AND model_used='deterministic'").fetchone()[0]
    P(f"  9月起 DA- 行 = {_tot}, 被错标 deterministic = {_det} "
      f"({100*_det/_tot:.1f}%)" if _tot else "  (无 DA- 行)")
    _c.close()
except Exception as _e:
    P(f"  🔴 DB 查询失败: {_e}")

# ⚠️ 必须把内容打到 stdout: 原脚本的 P() 只 append 到 out 列表、靠末尾 write_text 落盘;
#   改成不落盘后若不显式输出, stdout 只剩「[审计结束]」1 行 ⇒ 191 行审计内容全丢
#   (本次真踩过: rc=0 但 verify_claims 无从逐字比对 = 假绿)。
print("\n".join(out))
print(f"\n[审计结束] 共 {len(out)} 行")
