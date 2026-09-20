#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元环 findings 生命周期环 (meta_audit) — Loop Engineering 试点第 1 个 loop
==================================================================================
立起: 2026-09-20 用户令「按建议执行」(候选 B: 纯只读审计/守卫环) ⇒ 落成本环。

## 它解决什么问题 (先摆事实)
本仓的「测量免疫系统」(docs/CONTROL-LOOP-LEDGER.md §2.5) 已有探测器: 门活性探针、
data_freshness_watchdog(D1/D2)、daily_invariant_check(I11~I14/W7)、反向配置普查(D3)。
2026-09-19 实测门活性探针 `dead=4` —— **数字一直在产出, 但没有任何地方记录"这 4 项谁裁、
何时裁、裁成什么"**: 探测器只负责"发现", 发现之后的**归宿**不存在载体。

⇒ 本环不复做任何检测 (零新探测器, 做减法), 只做三件事:
  ① **消费**: 读既有探测器产物 (只读 JSON, 不连 DB / 不连富途 / 不碰执行链);
  ② **对账**: 把 finding 与**持久状态文件** (data/loops/meta_audit/) 对账 — 新增落 OPEN,
     源侧回读确认消失才落 CLOSED, 源产物不可用**绝不自动关闭** (改落 UNVERIFIABLE);
  ③ **收敛可见**: 每个 OPEN finding 带 owner + decide_by, 逾期在摘要里显式点名。

## 停止条件 (外部可验 — 取代"agent 说完成")
    LOOP_DONE ⇔ ①每个 OPEN finding 都有 owner 且 decide_by 非空
               ∧ ②无逾期未裁 (decide_by 未过期)
               ∧ ③每个 CLOSED finding 都有源侧回读证据 (close_evidence 非空)
               ∧ ④所有探测器产物新鲜 (age ≤ 各自阈值)
    四条全真才打印 True; 任一为假即打印具体为假的那条。判据由**文件**承载 (STATE.md),
    任何 agent 可以只读复算, 不需要信任本环的自述。

## 权限层 (L1, 与交易纪律对齐)
本环是 **L1 纯只读**: 只写自己 loop 目录下的 STATE.md / findings.json / findings_history.jsonl,
不写任何其他文件、不改配置、不改参数、不重启服务、不下单、不删产出。
禁止本环修改自身之外的代码 (「loop 不得自我进化」)。

## 输入 (既有产物, 阈值=该产物应有的更新节奏)
    gate_liveness   data/gate_liveness_latest.json         96h  (15 7 * * 2-6)
    watchdog        data/watchdog_liveness_latest.json      96h  (25 6 * * 2-6)
    invariant       data/invariant_check_latest.json        96h  (5 5 * * 2-6)
    config_census   data/config_key_reverse_census_latest.json 192h (周频)
  阈值不是"新鲜度判据"的重新预注册: 它们是各载体自己声称的节奏, 超期=该探测器没在跑。

## 用法
    python scripts/meta_audit_loop.py             # 跑一轮; 有状态变化才打印摘要 (无变化=静默成功)
    python scripts/meta_audit_loop.py --force-print  # 强制打印摘要 (人工查看, 载体不用)
    python scripts/meta_audit_loop.py --status    # 只读 STATE 摘要, 不采集不对账

## 输出契约 (给载体)
    rc 0 = 本环跑完 (stdout 可能为空 = 无变化静默; 有内容 = 状态变化摘要)
    rc 2 = 本环自身崩溃 (载体必须报"环自身异常", 不得读成"零 finding")
    rc 3 = 仓根/loop 目录不可用
回滚: 删 scripts/meta_audit_loop.py + data/loops/meta_audit/ + 载体 job + 台账行。
"""
from __future__ import annotations

import json
import os
import sys
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(os.environ.get("FTSEEK_REPO", Path(__file__).resolve().parent.parent))
LOOP_DIR = BASE / "data" / "loops" / "meta_audit"
STATE_MD = LOOP_DIR / "STATE.md"
FINDINGS = LOOP_DIR / "findings.json"
HISTORY = LOOP_DIR / "findings_history.jsonl"
LAST_DIGEST = LOOP_DIR / "last_digest.json"

DECIDE_BY_DAYS = 14  # 提醒期限 (自然日; 不用交易日历 — 该 accessor 会开 OpenD 连接)

# 路由表: 哪些 finding 归"维护批"(可自动修), 哪些必须"用户裁决"(= 不自动执行)。
# 未命中路由表者取该源的默认 owner。
DEFAULT_OWNER = {
    "gate_liveness": "用户(裁决)",      # 死门退役/修复属行为变更, 须人工裁 (E5 待裁)
    "watchdog": "维护批",               # 生产者没喂数 = 修接线, 不改交易行为
    "invariant": "维护批",
    "config_census": "维护批",
    "detector": "维护批",               # 探测器产物停更 = 把尺子修回来
}
KEY_OWNER = {
    "invariant:W7_chg_denominator_divergence": "用户(裁决)",  # G8: 生产修复被列为 P2, 须另开预注册
}

MAX_DIGEST_ITEMS = 8


def _now() -> datetime:
    return datetime.now()


def _age_h(ts: float) -> float:
    return (datetime.now().timestamp() - ts) / 3600.0


# ── 采集: 从既有产物抽 finding (零新检测逻辑) ────────────────────────────────

def _extract_gate_liveness(doc: dict):
    out = []
    for gate, info in (doc.get("gates") or {}).items():
        status = (info or {}).get("status")
        if status in ("dead", "stale"):
            out.append((gate, "死门/陈旧门: %s" % gate,
                        "status=%s | %s" % (status, str((info or {}).get("detail") or "")[:220])))
    return out


def _extract_watchdog(doc: dict):
    out = []
    for a in (doc.get("alarms") or []):
        kind = str(a.get("kind") or "")
        key = str(a.get("key") or "")
        # watchdog 的 key 字段自带 kind 前缀 (如 "D1:open_positions:never_fed") ⇒ 不重复拼
        check = key if key.startswith(kind + ":") else "%s:%s" % (kind, key)
        out.append((check, "载体异常 %s" % check,
                    "%s | %s" % (str(a.get("detail") or "")[:180], str(a.get("action") or "")[:120])))
    return out


def _extract_invariant(doc: dict):
    out = []
    ex = doc.get("examples") or {}
    for check, n in (doc.get("violations") or {}).items():
        if not n:
            continue
        out.append((check, "不变量违反 %s ×%s" % (check, n),
                    "%s | 例: %s" % (n, str(ex.get(check) or "")[:220])))
    return out


def _extract_config_census(doc: dict):
    out = []
    for kind in ("undeclared_literal", "undeclared_second_surface"):
        for item in (doc.get(kind) or []):
            path = item.get("path") or "?"
            out.append(("%s:%s" % (kind, path), "配置键未声明/第二源 (%s)" % kind, str(path)[:200]))
    return out


SOURCES = [
    # (源名, 产物路径, 阈值h, 抽取函数, 消费者路径 | None)
    # 阈值口径 = 该载体**调度面自带的最长自然间隔 + 1 个 tick 容差**, 不是"新鲜度判据"的重新预注册:
    #   gate_liveness / watchdog / invariant 均 `* * 2-6` (周二~周六 CST) ⇒ 最长间隔 = 周六跑 → 周二跑
    #   = 72h (周日/周一 CST 不在调度面) ⇒ 阈值 96h。**首版用 26h 在周日实测误报 3 源**
    #   (30h/31h 是周末的正常间隔, 不是停更) — 误报会把真信号淹掉, 故按调度面而非"日频直觉"定阈值。
    #
    # 第 5 列为 **消费者路径** ⇒ 该源是**按需工具**而非周期传感器 (2026-09-20 用户令「如果无用就退役」实测后的处置):
    #   `scripts/config_key_reverse_census.py` 无周期载体 (台账原声称的 cron 不存在), 但判定**有活消费者**
    #   = `tests/test_config_key_reverse_census.py::test_real_repo_has_no_new_invisible_switch`
    #   (每次全量回归在真仓上跑门禁: 新增隐形开关即红)。⇒ 判定不退役 (退役会删掉一条在役守卫),
    #   **退役的是"把它当周期传感器"这个错误假设**: 产物陈旧**不是失明**, 故不产 `detector:*` 停更 finding;
    #   只有**消费者消失**(测试被删)才算失明。产物 fresh 时它的读数仍可作 live finding。
    ("gate_liveness", "data/gate_liveness_latest.json", 96, _extract_gate_liveness, None),
    ("watchdog", "data/watchdog_liveness_latest.json", 96, _extract_watchdog, None),
    ("invariant", "data/invariant_check_latest.json", 96, _extract_invariant, None),
    ("config_census", "data/config_key_reverse_census_latest.json", 192, _extract_config_census,
     "tests/test_config_key_reverse_census.py"),
]


def collect():
    """返回 (findings, source_state)。findings: [(key,title,evidence,source)]。
    source_state[source] = {"fresh": bool, "why": str, "n": int}"""
    findings, src_state = [], {}
    for name, rel, max_age_h, extract, consumer in SOURCES:
        p = BASE / rel
        on_demand = bool(consumer)
        consumer_alive = (not on_demand) or (BASE / consumer).exists()
        if on_demand and not consumer_alive:
            src_state[name] = {"fresh": False, "on_demand": True, "n": 0, "consumer": consumer,
                               "why": "按需源的消费者缺失 (%s)" % consumer}
            findings.append(("detector:%s" % name, "按需源的消费者消失: %s" % name,
                             "判定载体 %s 不存在 ⇒ 该源的判定已无执行者 (产物的新旧不代表判定在跑)" % consumer,
                             "detector"))
            continue
        if not p.exists():
            if on_demand:
                # 按需源无周期载体: 产物缺失不是失明 (判定在消费者), 不产 detector finding
                src_state[name] = {"fresh": True, "on_demand": True, "n": 0, "consumer": consumer,
                                   "why": "按需源: 判定由 %s 承载 (无产物, 不判停更)" % consumer}
                continue
            src_state[name] = {"fresh": False, "on_demand": False, "n": 0,
                               "why": "产物不存在 (%s)" % rel}
            findings.append(("detector:%s" % name, "探测器产物缺失: %s" % name,
                             "期望产物 %s 不存在 ⇒ 该探测器读数不可用 (不得读成零 finding)" % rel,
                             "detector"))
            continue
        age = _age_h(p.stat().st_mtime)
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:                                   # noqa: BLE001
            src_state[name] = {"fresh": False, "on_demand": on_demand, "n": 0,
                               "why": "产物解析失败 (%s)" % type(e).__name__}
            findings.append(("detector:%s" % name, "探测器产物不可解析: %s" % name,
                             "%s 解析失败: %s" % (rel, e), "detector"))
            continue
        fresh = age <= max_age_h
        # 按需源: 产物陈旧≠失明; 陈旧读数是**过期读数**, 不作为 live finding (避免拿 22 天前的读点当今天的问题)
        take_items = fresh or not on_demand
        items = extract(doc) if take_items else []
        if on_demand:
            src_state[name] = {"fresh": True, "on_demand": True, "n": len(items), "consumer": consumer,
                               "why": "按需源: 消费者 %s 存在 | 产物 age=%.1fh%s"
                                      % (consumer, age, "" if fresh else " (>阈值 ⇒ 过期读数不取)")}
        else:
            src_state[name] = {"fresh": fresh, "on_demand": False, "n": len(items),
                               "why": "age=%.1fh (阈值 %sh)" % (age, max_age_h)}
        for key, title, ev in items:
            findings.append(("%s:%s" % (name, key), title, ev, name))
        if not fresh and not on_demand:
            findings.append(("detector:%s" % name, "探测器产物停更: %s (%.0fh > %sh)" % (name, age, max_age_h),
                             "%s mtime 距今 %.0f 小时 (阈值 %sh) ⇒ 该探测器本轮读数不可用; 停更不等于零 finding"
                             % (rel, age, max_age_h), "detector"))
    return findings, src_state


# ── 状态文件读写 ────────────────────────────────────────────────────────────

def _load():
    if FINDINGS.exists():
        try:
            return json.loads(FINDINGS.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            pass
    return {"findings": {}}


def _atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _append_history(rows):
    if not rows:
        return
    with HISTORY.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def reconcile(state, collected, src_state):
    """把本轮采集结果对账进持久状态。返回 (new, closed, unverifiable, overdue)。"""
    today = _now().strftime("%Y-%m-%d")
    ts = _now().isoformat(timespec="seconds")
    store = state["findings"]
    seen_keys = {k for k, _, _, _ in collected}
    new, closed, unverifiable = [], [], []
    hist = []

    for key, title, ev, src in collected:
        if key not in store:
            owner = KEY_OWNER.get(key) or DEFAULT_OWNER.get(src, "维护批")
            store[key] = {
                "key": key, "source": src, "title": title, "evidence": ev,
                "status": "OPEN", "owner": owner,
                "first_seen": today, "last_seen": today,
                "decide_by": (_now() + timedelta(days=DECIDE_BY_DAYS)).strftime("%Y-%m-%d"),
                "closed_at": None, "close_evidence": None, "history": [],
            }
            hist.append({"ts": ts, "key": key, "event": "OPEN", "title": title, "evidence": ev})
            new.append(store[key])
        else:
            f = store[key]
            f["last_seen"] = today
            f["title"] = title
            f["evidence"] = ev
            if f.get("status") != "OPEN":
                # 曾经关闭/不可判的项再次出现 ⇒ 重新打开 (复发必须可见, 不许静默)
                f["status"] = "OPEN"
                f["first_seen"] = today
                f["decide_by"] = (_now() + timedelta(days=DECIDE_BY_DAYS)).strftime("%Y-%m-%d")
                f["closed_at"] = None
                f["close_evidence"] = None
                hist.append({"ts": ts, "key": key, "event": "REOPEN", "title": title})
                new.append(f)

    for key, f in list(store.items()):
        if key in seen_keys or f.get("status") not in ("OPEN", "UNVERIFIABLE"):
            continue
        src = f.get("source")
        if src == "detector":
            # 探测器级 finding 的关闭条件 = 它所报的那个源恢复"可在册":
            #   · 周期源: 产物恢复新鲜 ⇒ 下轮 detector:* key 不再出现 (走上面 seen_keys 分支), 此处只管按需源;
            #   · 按需源: 消费者复活 ⇒ 关闭 (2026-09-20 实测: 首版只认"产物新鲜", 会把按需源的
            #     detector:config_census 永久钉成 OPEN — 一个**永不收敛**的 finding, 正是本环要防的病)。
            tgt = key.split("detector:", 1)[1] if "detector:" in key else ""
            st = src_state.get(tgt) or {}
            if st.get("on_demand") and st.get("fresh"):
                f["status"] = "CLOSED"
                f["closed_at"] = today
                f["close_evidence"] = ("按需源语义: 判定由 %s 承载且消费者存在 ⇒ '产物停更'判定不适用 "
                                       "(非周期传感器) | 关闭日 %s" % (st.get("consumer", "?"), today))
                hist.append({"ts": ts, "key": key, "event": "CLOSED", "evidence": f["close_evidence"]})
                closed.append(f)
            continue
        if not (src_state.get(src) or {}).get("fresh"):
            if f.get("status") != "UNVERIFIABLE":
                f["status"] = "UNVERIFIABLE"
                hist.append({"ts": ts, "key": key, "event": "UNVERIFIABLE",
                             "why": "源产物不可用/不新鲜 ⇒ 禁止自动关闭 (关=把尺子坏掉读成修好)"})
            unverifiable.append(f)
            continue
        st = src_state.get(src) or {}
        if st.get("on_demand"):
            ev = ("按需源语义: 判定由 %s 承载 (非周期传感器), 陈旧产物不作 live 读数 ⇒ "
                  "关闭不再报该项**不是**'已修好', 而是该源不承载周期性归宿 | 关闭日 %s"
                  % (st.get("consumer", "?"), today))
        else:
            ev = ("源侧回读: %s 当日产物不再报该项 (age %.1fh, 阈值内) | 关闭日 %s"
                  % (src, st.get("_age_h", -1), today))
        f["status"] = "CLOSED"
        f["closed_at"] = today
        f["close_evidence"] = ev
        hist.append({"ts": ts, "key": key, "event": "CLOSED", "evidence": f["close_evidence"]})
        closed.append(f)

    overdue = [f for f in store.values()
               if f.get("status") == "OPEN" and (f.get("decide_by") or "9999") < today]
    state["last_run"] = ts
    state["findings"] = store
    return new, closed, unverifiable, overdue, hist


# ── 呈现 ────────────────────────────────────────────────────────────────────

def _stop_condition(store, src_state):
    open_ = [f for f in store.values() if f.get("status") == "OPEN"]
    closed = [f for f in store.values() if f.get("status") == "CLOSED"]
    unver = [f for f in store.values() if f.get("status") == "UNVERIFIABLE"]
    today = _now().strftime("%Y-%m-%d")
    overdue = [f for f in open_ if (f.get("decide_by") or "9999") < today]
    stale = [s for s, v in src_state.items() if not v.get("fresh")]
    # 判据一律以 (标签, 布尔) 对呈现, 禁止 c1/c2/... 位置对齐 —— 首版 digest 的标签与取值错位
    # (②标签配 c2=关闭证据), 属"看起来有断言、实际说反"的静默缺陷, 与 §6 判据同源纪律同族。
    crit = [
        ("①owner+decide_by 齐备", all(f.get("owner") and f.get("decide_by") for f in open_)),
        ("②无逾期未裁", not overdue),           # 逾期未裁必须能让 LOOP_DONE 变假 (首版漏此条)
        ("③CLOSED 全带回读证据", all(f.get("close_evidence") for f in closed)),
        ("④探测器产物全新鲜", not stale),
    ]
    return {
        "done": all(ok for _, ok in crit),
        "crit": crit,
        "open": len(open_), "closed": len(closed), "unverifiable": len(unver),
        "overdue": len(overdue), "stale_sources": stale,
    }


def _render_state_md(store, src_state, sc, prev_run="-"):
    lines = []
    lines.append("# meta_audit loop — STATE")
    lines.append("")
    lines.append("> 本文件由 `scripts/meta_audit_loop.py` 生成 (L1 只读环, 只写本目录)。"
                 "**开工先读、收工先写、跨轮存活**。")
    lines.append("> 停止条件 (外部可验, 禁止用'agent 说完成'): "
                 "①OPEN 项都有 owner+decide_by ②无逾期未裁 ③CLOSED 项都有源侧回读证据 ④探测器产物全新鲜。")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|----|----|")
    lines.append("| 上轮运行 | %s |" % prev_run)
    lines.append("| LOOP_DONE | **%s** |" % ("TRUE" if sc["done"] else "FALSE"))
    lines.append("| 判据 | %s |" % " / ".join("%s=%s" % (lab, ok) for lab, ok in sc["crit"]))
    lines.append("| OPEN / UNVERIFIABLE / CLOSED | %d / %d / %d |"
                 % (sc["open"], sc["unverifiable"], sc["closed"]))
    lines.append("")
    lines.append("## 探测器产物状态")
    lines.append("")
    lines.append("| 源 | 新鲜 | 本轮读数 | 说明 |")
    lines.append("|----|------|---------|------|")
    for s in sorted(src_state):
        v = src_state[s]
        lines.append("| %s | %s | %d | %s |" % (s, "✅" if v["fresh"] else "❌", v["n"], v["why"]))
    lines.append("")
    lines.append("## 未决 findings (OPEN / UNVERIFIABLE)")
    lines.append("")
    lines.append("| key | 状态 | owner | decide_by | 首见 | 末见 | 证据 |")
    lines.append("|-----|------|-------|-----------|------|------|------|")
    rows = [f for f in store.values() if f.get("status") in ("OPEN", "UNVERIFIABLE")]
    for f in sorted(rows, key=lambda x: (x.get("decide_by") or "", x["key"])):
        overdue = " ⏰逾期" if (f.get("decide_by") or "9999") < _now().strftime("%Y-%m-%d") else ""
        lines.append("| %s | %s%s | %s | %s | %s | %s | %s |"
                     % (f["key"], f.get("status"), overdue, f.get("owner"), f.get("decide_by"),
                        f.get("first_seen"), f.get("last_seen"),
                        str(f.get("evidence") or "").replace("|", "/")[:160]))
    if not rows:
        lines.append("| (无) | — | — | — | — | — | — |")
    lines.append("")
    lines.append("## 已关闭 (CLOSED, 附源侧回读证据)")
    lines.append("")
    lines.append("| key | 关闭日 | 关闭证据 |")
    lines.append("|-----|--------|---------|")
    cl = [f for f in store.values() if f.get("status") == "CLOSED"]
    for f in sorted(cl, key=lambda x: x["key"]):
        lines.append("| %s | %s | %s |" % (f["key"], f.get("closed_at"),
                                           str(f.get("close_evidence") or "").replace("|", "/")[:160]))
    if not cl:
        lines.append("| (无) | — | — |")
    lines.append("")
    lines.append("## 边界 (刻意不做的事)")
    lines.append("")
    lines.append("- 不做任何检测 (零新探测器): 只消费既有产物, 检测逻辑留在各探测器本体。")
    lines.append("- 不碰影子裁决 (RFC-088 `pending_adjudications` / `criteria_status` 是那半边消费者),"
                 " 本环只承载**探测器级 finding** 的归宿。")
    lines.append("- 不自动改参/改门/重启/下单; L1 只读, 输出是「该谁裁」而不是「已改好」。")
    lines.append("- 不自动关闭: 源产物不可用/不新鲜时落 UNVERIFIABLE 并保留, 关闭必须有源侧回读证据。")
    lines.append("")
    return "\n".join(lines) + "\n"


def _digest(new, closed, unverifiable, overdue, src_state, sc, force=False):
    lines = []
    lines.append("[meta_audit] %s | 探测器 %d/%d 新鲜 | 未决 %d (新 %d / 关闭 %d / 逾期 %d)"
                 % (_now().strftime("%Y-%m-%d %H:%M"), sum(1 for v in src_state.values() if v["fresh"]),
                    len(src_state), sc["open"] + sc["unverifiable"], len(new), len(closed), len(overdue)))
    lines.append("[meta_audit] LOOP_DONE=%s | %s"
                 % (sc["done"], " ".join("%s=%s" % (lab, ok) for lab, ok in sc["crit"])))
    if sc["stale_sources"]:
        lines.append("[meta_audit] ⚠️ 不新鲜探测器: %s (读数不可用, 不等于零 finding)"
                     % ", ".join(sc["stale_sources"]))
    shown = 0
    for f in new[:MAX_DIGEST_ITEMS]:
        lines.append("[meta_audit] 🆕 %s | %s | owner=%s decide_by=%s"
                     % (f["key"], f["title"], f["owner"], f["decide_by"]))
        shown += 1
    for f in overdue[:max(0, MAX_DIGEST_ITEMS - shown)]:
        lines.append("[meta_audit] ⏰ 逾期未裁 %s | decide_by=%s | owner=%s"
                     % (f["key"], f["decide_by"], f["owner"]))
        shown += 1
    rest = (len(new) + len(overdue)) - shown
    if rest > 0:
        lines.append("[meta_audit] …其余 %d 项见 STATE.md" % rest)
    for f in closed[:3]:
        lines.append("[meta_audit] ✅ 关闭 %s | %s" % (f["key"], f["close_evidence"]))
    lines.append("[meta_audit] 状态: %s" % STATE_MD)
    return "\n".join(lines)


def main(argv):
    if "--status" in argv:
        state = _load()
        store = state.get("findings", {})
        rows = [f for f in store.values() if f.get("status") in ("OPEN", "UNVERIFIABLE")]
        print("[meta_audit] --status: 上轮 %s | 未决 %d" % (state.get("last_run", "-"), len(rows)))
        for f in sorted(rows, key=lambda x: x["key"]):
            print("  %-58s %-13s owner=%s decide_by=%s" % (f["key"], f.get("status"),
                                                          f.get("owner"), f.get("decide_by")))
        return 0

    if not BASE.exists():
        print("[meta_audit] 仓根不存在: %s" % BASE)
        return 3
    LOOP_DIR.mkdir(parents=True, exist_ok=True)

    collected, src_state = collect()
    for name, rel, max_age_h, _extract, _consumer in SOURCES:
        p = BASE / rel
        if p.exists():
            src_state[name]["_age_h"] = _age_h(p.stat().st_mtime)
    state = _load()
    prev_run = state.get("last_run") or "-"
    new, closed, unverifiable, overdue, hist = reconcile(state, collected, src_state)
    sc = _stop_condition(state["findings"], src_state)

    _atomic_write(STATE_MD, _render_state_md(state["findings"], src_state, sc, prev_run))
    _atomic_write(FINDINGS, json.dumps(state, ensure_ascii=False, indent=1))
    _append_history(hist)

    text = _digest(new, closed, unverifiable, overdue, src_state, sc)
    # 静默契约的判定必须**不含时间戳** (首版把 2026-09-20 12:26 写进正文 ⇒ 每轮 hash 都变 ⇒ 每轮都推,
    # 静默契约形同虚设)。签名只取"状态变化"维度。
    sig = {
        "open": sorted("%s|%s|%s" % (f["key"], f.get("status"), f.get("decide_by"))
                       for f in state["findings"].values()
                       if f.get("status") in ("OPEN", "UNVERIFIABLE")),
        "closed": sorted("%s|%s" % (f["key"], f.get("closed_at"))
                         for f in state["findings"].values() if f.get("status") == "CLOSED"),
        "overdue": sorted(f["key"] for f in overdue),
        "stale_sources": sorted(sc["stale_sources"]),
        "done": sc["done"],
    }
    h = hashlib.sha256(json.dumps(sig, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    prev = {}
    if LAST_DIGEST.exists():
        try:
            prev = json.loads(LAST_DIGEST.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            prev = {}
    changed = prev.get("hash") != h
    _atomic_write(LAST_DIGEST, json.dumps({"hash": h, "ts": _now().isoformat(timespec="seconds"),
                                           "n_open": sc["open"] + sc["unverifiable"]},
                                          ensure_ascii=False))

    # 静默契约: 无状态变化 = 不打印 (载体不推送); 有变化/强制 = 打印摘要
    if changed or "--force-print" in argv:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:                                       # noqa: BLE001
        print("[meta_audit] 环自身异常 (本环崩溃必须与'零 finding'可区分): %s: %s"
              % (type(e).__name__, e))
        sys.exit(2)