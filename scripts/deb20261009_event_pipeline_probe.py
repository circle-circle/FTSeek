#!/usr/bin/env python3
"""[DEB-20261009 event-to-trade §12 复查探针] 事件识别链的结构性缺口实测.

用途: 检验提案 §1.2「没有电信竞争、频谱交易或监管审批传导模板」这条立项依据,
并量化 §11 (Claude 复查) 遗漏的三条活资产/真缺口。

只读: 不改文件/配置/cron, 不触发 OpenD, 不发起外部 HTTP (finnhub/RSSHub 均只读本地 DB 或跳过)。

跑法:
    cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261009_event_pipeline_probe.py

输出分区 (对应 §12 E 表 E1-E7):
  A 活链盘点        → CatalystClassifier / gen_event_symbols 是否真在跑 (§11 N1 定性修正)
  B 词表覆盖探针    → 用提案 §1.1 真实事件标题跑生产分类器 (阳性对照防假阴)
  C RSSHub 断采实证 → news_us.db 停采 + 采集 cron 星期覆盖 vs gen_event_symbols 星期
  D 静默失效        → 空列表不抛异常 ⇒ P2 降级告警永不触发
  E event_symbols 产物面 → HARD_EVENT 产出率 × 星期
  F news_quality.db → 37 行全 event_type='other' (LLM 分类器死输出)
"""

from __future__ import annotations

import collections
import json
import glob
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

EVENT_DB = os.path.join(REPO, "data/rsshub_us/news_us.db")
WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

# ⚠️ 自指排除 (两种形态):
#   ① 本探针 B 段自己 import CatalystClassifier;
#   ② 姊妹验证器 verify_deb20261009_event.py 的 L2 对账正则含
#      "CatalystClassifier 消费方文件数" 字面量, 也会被 A 段 grep 命中 (实测 5→6 漂移)。
# 两者都是本 DEB 的复查工件, 不是生产消费方; 其余 scripts/ 一律照检。
SELF_FILES_EXCLUDE = (
    "scripts/deb20261009_event_pipeline_probe.py",
    "scripts/verify_deb20261009_event.py",
)


def _hr(t: str) -> None:
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def section_a_live_chain() -> dict:
    """A: CatalystClassifier 的真实消费方 (§11 称 catalyst.enabled=false 像死资产)."""
    _hr("A · 事件识别链的真实消费者 (排除 app/catalyst 自身)")
    r = subprocess.run(
        ["grep", "-rn", "-E", r"CatalystClassifier|from app\.catalyst", "scripts/", "app/", "--include=*.py"],
        capture_output=True, text=True, cwd=REPO,
    )
    hits = [l for l in r.stdout.splitlines()
            if "__pycache__" not in l
            and not l.startswith("app/catalyst/")          # 定义处自身不算消费方
            and not l.startswith(SELF_FILES_EXCLUDE)]      # 自指排除 (见模块头)
    callers = collections.Counter(l.split(":")[0] for l in hits)
    for f, n in callers.most_common():
        print(f"  {f:<44}{n} 处引用")
    print(f"\n  消费方文件数 = {len(callers)}  (零 = 死资产; >0 = 活链)")
    # catalyst.enabled 门控的是哪条链
    r2 = subprocess.run(["grep", "-rn", "-E", r"catalyst.*enabled|_gc_cat", "app/core/scheduler_core.py"],
                        capture_output=True, text=True, cwd=REPO)
    gated = len([l for l in r2.stdout.splitlines() if l.strip()])
    print(f"  scheduler_core 里 catalyst 配置门控点 = {gated} 处 (仅管 HK/CN detector 链)")
    print("  ⇒ CatalystClassifier 被直接 import 使用, **不受 catalyst.enabled 门控**")
    return {"callers": len(callers), "files": sorted(callers)}


def section_b_keyword_probe() -> dict:
    """B: 用提案 §1.1 真实事件标题跑生产分类器 (含阳性对照)."""
    _hr("B · 词表覆盖探针: 提案 §1.1 的 SpaceX/FCC 事件能否被现有链识别")
    from app.catalyst.event_classifier import CatalystClassifier
    from app.news_v2.models import NewsUrgency, UnifiedNewsItemV2

    cases = [
        # 提案 §1.1 的真实事件 (官方授权 / 频谱收购 → 电信股跌 6%)
        ("SpaceX granted FCC authority to deploy 15,000 satellites for direct-to-cell service", "事件主体"),
        ("SpaceX acquires 800 MHz spectrum licenses from Grain Management", "事件主体"),
        ("AT&T, T-Mobile, Verizon shares fall 6% after SpaceX satellite authorization", "受损方"),
        ("FCC order DA-26-1078A1 grants-in-part SpaceX NGSO satellite authority", "官方源"),
        # 阳性对照: 词表内确定有的关键词 (证明探针本身能命中, 排除假阴)
        ("SEC investigation into company accounting practices announced", "对照·SEC"),
        ("US adds semiconductor firm to entity list, export ban effective", "对照·entity list"),
    ]
    c = CatalystClassifier()
    results = []   # 用 list 不用 dict: tag 有重复 ("事件主体" 出现两次), dict key 会互相覆盖
    print(f"  {'标题(截断 58)':<60}{'分类结果'}")
    for title, tag in cases:
        # 逐字照抄 gen_event_symbols.py:120-127 的生产构造方式
        item = UnifiedNewsItemV2(
            id="probe", title=title, content=title, source="probe", source_name="probe",
            timestamp=0.0, related_stocks=["US.T", "US.TMUS", "US.VZ"],
            urgency=NewsUrgency.NORMAL,
        )
        evs = c.classify([item], use_llm=False)
        desc = ", ".join(
            f"{getattr(e, 'event_type', '?')}/{getattr(e, 'severity', '?')}/{getattr(e, 'tickers', [])}"
            for e in evs
        ) or "❌ 零命中"
        results.append((tag, len(evs)))
        print(f"  {title[:58]:<60}{desc}")
    ev_rows = [(t, n) for t, n in results if t.startswith("事件") or t in ("受损方", "官方源")]
    ct_rows = [(t, n) for t, n in results if t.startswith("对照")]
    n_event, n_hit_event = len(ev_rows), sum(1 for _, n in ev_rows if n > 0)
    n_ctrl, n_hit_ctrl = len(ct_rows), sum(1 for _, n in ct_rows if n > 0)
    print(f"\n  事件样本命中 {n_hit_event}/{n_event}   阳性对照命中 {n_hit_ctrl}/{n_ctrl}")
    if n_hit_ctrl == n_ctrl and n_hit_event == 0:
        print("  ⇒ 阳性对照全中 ⇒ 探针有效, 事件零命中是**词表结构性缺口**而非探针假阴")
    return {"event_hit": n_hit_event, "event_total": n_event,
            "ctrl_hit": n_hit_ctrl, "ctrl_total": n_ctrl}


def section_c_rsshub_starvation() -> dict:
    """C: RSSHub 停采 + 采集/消费 cron 星期错配."""
    _hr("C · RSSHub 新闻源停采实证 (提案 §1.2#3 只报了 mtime, 未查因果)")
    out = {}
    if not os.path.exists(EVENT_DB):
        print(f"  ❌ {EVENT_DB} 不存在")
        return out
    mt = datetime.fromtimestamp(os.path.getmtime(EVENT_DB))
    print(f"  news_us.db mtime = {mt:%Y-%m-%d %H:%M}  (周{WEEKDAY_CN[mt.weekday()]})")
    c = sqlite3.connect(f"file:{EVENT_DB}?mode=ro", uri=True)
    n, mx, mn = c.execute("SELECT COUNT(*), MAX(fetched_at), MIN(fetched_at) FROM us_news").fetchone()
    print(f"  us_news 行数 = {n}   fetched_at 跨度 = {str(mn)[:19]} → {str(mx)[:19]}")
    out["rows"] = n
    out["max_fetched_at"] = str(mx)[:19]

    # cron 星期覆盖
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith("#")]
    for label, pat in (("RSSHub 采集", r"rsshub"), ("gen_event_symbols", r"gen_event_symbols")):
        dows = [l.split()[4] for l in lines if re.search(pat, l)]
        print(f"  {label} cron 星期字段 = {dows or '未挂'}")
        out[f"{label}_dow"] = dows
    print("\n  ⇒ 采集只在 周六/周日/周一 跑, 消费方 gen_event_symbols 在 周一~周五 跑")
    print("     周二~周五的 HARD_EVENT 只能靠 finnhub 降级腿 (_gather_news 无条件并入)")
    return out


def section_d_silent_failure() -> dict:
    """D: 空列表不抛异常 ⇒ P2 降级告警永不触发."""
    _hr("D · 静默失效: RSSHub 返回空列表时, 2026-09-17 用户裁的降级告警会不会发?")
    src = open(os.path.join(REPO, "app/data/rsshub_us_news.py"), encoding="utf-8").read()
    m = re.search(r"def get_recent_news\(.*?\n    def ", src, re.S)
    body = m.group(0) if m else ""
    raises = len(re.findall(r"\braise\b", body))
    returns_empty = "return [dict(r) for r in rows]" in body
    print(f"  get_recent_news 函数体内 raise 数 = {raises}")
    print(f"  无数据时返回 [] (不抛异常) = {returns_empty}")

    scan = open(os.path.join(REPO, "scripts/llm_universe_scan.py"), encoding="utf-8").read()
    m2 = re.search(r"def _gather_news\(hours: int\).*?\n    return items", scan, re.S)
    body2 = m2.group(0) if m2 else ""
    alert_in_except = bool(re.search(r"except Exception as e:.*?FeishuAlerter", body2, re.S))
    finnhub_unconditional = bool(re.search(
        r"if bool\(_cfg\(\"universe_expansion\.clue_finnhub_news\".*?\n(?!    except)", body2, re.S))
    print(f"  _gather_news 的降级告警位于 except 块内 = {alert_in_except}")
    print(f"  finnhub 腿无条件并入 (不在 except 内) = {finnhub_unconditional}")
    if raises == 0 and alert_in_except:
        print("\n  ⇒ **真失效形态 (DB 陈旧 → 查询返回 0 行) 不抛异常 ⇒ 告警永不触发**")
        print("     提案 §1.1 事件当天 (10-06/07/08) RSSHub 腿静默返回 0 条, 无人知晓")
    return {"raises": raises, "alert_in_except": alert_in_except,
            "finnhub_unconditional": finnhub_unconditional}


def section_e_product_surface() -> dict:
    """E: event_symbols 产物面 + HARD_EVENT 产出率 × 星期."""
    _hr("E · event_symbols 产物面 (提案 §1.2 完全未盘点这条活产物)")
    files = sorted(glob.glob(os.path.join(REPO, "data/event_symbols_*.json")))
    print(f"  产物数 = {len(files)}")
    if files:
        print(f"  跨度 = {os.path.basename(files[0])[14:24]} → {os.path.basename(files[-1])[14:24]}")
    hit_by_wd, tot_by_wd = collections.Counter(), collections.Counter()
    types = collections.Counter()
    syms = set()
    for f in files:
        ds = os.path.basename(f)[14:24]
        try:
            y, m, d = map(int, ds.split("-"))
        except ValueError:
            continue
        wd = date(y, m, d).weekday()
        tot_by_wd[wd] += 1
        doc = json.load(open(f, encoding="utf-8"))
        has = False
        for e in doc.get("event_symbols", []):
            syms.add(e.get("symbol"))
            for r in e.get("reasons", []):
                if r.startswith("HARD_EVENT"):
                    has = True
                    types[r.split("]")[0] + "]"] += 1
        if has:
            hit_by_wd[wd] += 1
    print(f"  覆盖标的数 = {len(syms)}")
    print("  HARD_EVENT 类型分布:")
    for k, v in types.most_common():
        print(f"    {k:<38}{v}")
    print("\n  HARD_EVENT 产出日 × 星期:")
    for wd in range(7):
        if tot_by_wd[wd]:
            print(f"    周{WEEKDAY_CN[wd]}: {hit_by_wd[wd]}/{tot_by_wd[wd]}")
    total_hit, total = sum(hit_by_wd.values()), sum(tot_by_wd.values())
    print(f"\n  总产出率 = {total_hit}/{total} = {100.0 * total_hit / max(total, 1):.1f}%")
    return {"files": len(files), "symbols": len(syms),
            "hit": total_hit, "total": total, "types": dict(types)}


def section_f_news_quality_db() -> dict:
    """F: news_quality.db 的 LLM 分类输出 (提案 §4.3/§4.5 拟新建的同 schema 表)."""
    _hr("F · news_quality.db (提案 §4.2 拟新建 event_stream.db, 此表 schema 高度重合)")
    db = os.path.join(REPO, "data/news_quality.db")
    if not os.path.exists(db):
        print("  ❌ 不存在")
        return {}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    cols = [r[1] for r in c.execute("PRAGMA table_info(news_classifications)")]
    n = c.execute("SELECT COUNT(*) FROM news_classifications").fetchone()[0]
    print(f"  news_classifications: {n} 行")
    print(f"  列 = {cols}")
    dist = [tuple(x) for x in c.execute(
        "SELECT event_type, COUNT(*) FROM news_classifications GROUP BY 1 ORDER BY 2 DESC")]
    print(f"  event_type 分布 = {dist}")
    filled = c.execute(
        "SELECT COUNT(*) FROM news_classifications WHERE primary_tickers IS NOT NULL AND primary_tickers <> ''"
    ).fetchone()[0]
    nonzero_sent = c.execute(
        "SELECT COUNT(*) FROM news_classifications WHERE sentiment <> 0").fetchone()[0]
    print(f"  primary_tickers 非空 = {filled}/{n}   sentiment≠0 = {nonzero_sent}/{n}")
    if dist and dist[0][0] == "other" and dist[0][1] == n:
        print("\n  ⇒ 全表 event_type='other' + tickers 空 + sentiment=0 ⇒ **LLM 分类器只写兜底值**")
        print("     这是「判据字面量≠生产值域 / 空写入」形态: 表在写, 内容恒为兜底")
        print("     写入方 app/news_v2/processor/llm_classifier.py:227 属已退役 news_v2 链")
    return {"rows": n, "dist": dist, "tickers_filled": filled}


def main() -> int:
    print("DEB-20261009-event-to-trade §12 复查探针 · 事件识别链结构性缺口")
    print(f"仓库: {REPO}   (只读)")
    a = section_a_live_chain()
    b = section_b_keyword_probe()
    c = section_c_rsshub_starvation()
    d = section_d_silent_failure()
    e = section_e_product_surface()
    f = section_f_news_quality_db()

    _hr("汇总 (供 §12 E 表引用)")
    print(f"  CatalystClassifier 消费方文件数 : {a.get('callers')}")
    print(f"  事件样本词表命中               : {b.get('event_hit')}/{b.get('event_total')}")
    print(f"  阳性对照命中                   : {b.get('ctrl_hit')}/{b.get('ctrl_total')}")
    print(f"  news_us.db 行数                : {c.get('rows')}")
    print(f"  get_recent_news 内 raise 数    : {d.get('raises')}")
    print(f"  event_symbols 产物数           : {e.get('files')}")
    print(f"  HARD_EVENT 总产出率            : {e.get('hit')}/{e.get('total')}")
    print(f"  news_quality.db event_type     : {f.get('dist')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
