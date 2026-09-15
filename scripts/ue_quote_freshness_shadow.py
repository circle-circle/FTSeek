#!/usr/bin/env python3
"""UE 报价新鲜度门「哑弹」量化影子 — 只读、只度量、**永不改变任何指令流**。

════════ 为什么存在 (2026-09-15, DEB-20260910 §22, 用户裁「按照建议执行」) ════════

P2.2 修复批 (commit 3646e0b36) 在**扫描期**把供应商的 `timestamp` 归一成 `source_asof`,
让初筛的 PASS 态首次可达。修的过程中查出三件比原缺陷更根本的事, 都指向**同一个门**:

1. `app/playbook/candidate_eligibility.py:134 _source_time` 只认 `source_asof`/`update_time`
   两个键, 而**全仓没有任何生产代码给报价写过这两个键** (唯一另一处
   `app/data/adaptive_polling.py:151` 写的 `update_time` 也是 `time.time()`, 即抓取时刻)。
   ⇒ SSOT 的新鲜度门 `stale_quote` (`quote_age_sec > max_quote_age_sec=90`) **全系统哑火**:
   `quote_age_sec` 恒为 None ⇒ 恒落 `missing_fields=['source_asof']` ⇒ status 恒为
   UNKNOWN, 而 inject 侧 (`scripts/llm_universe_scan.py:3306`) 只拦 `REJECT` ⇒ 全放行。
   **这不是"键名错配"这么简单: 门是装了的, 但从来没打过响。**

2. 交易所侧的真时戳**其实一直到手边**: `futu_lv3_provider.get_quote` 的快照路径
   (`:983`) 取的就是 `get_market_snapshot` 的同一行, 该行含 `update_time`
   (`get_snapshot:1733` 已在用), 但 `get_quote` **把它丢了**, 改写
   `"timestamp": time.time()` (`:970`/`:1024`)。
   ⇒ 诚实修法几乎零成本 (透出既有字段), **且 SSOT 一行都不用改**: `_source_time` 对
   naive 串本就假定 ET (`:141-142`), 而 futu `update_time` 正是 ET-naive
   (实测 `'2026-09-15 08:03:50.057'` 对齐 08:03:50 ET)。

3. 但"透出真时戳"= **激活一道从未响过的门** = 交易路径行为变更 (会真丢候选),
   按 AGENT.md C4「行为变更一律影子先行」必须先量化。本脚本就是那把尺子。

════════ 量什么 ════════

对同一批标的、同一时刻, 用 **SSOT 自己的判据** (`evaluate_candidate` + `_source_time`,
本脚本一行判据都不抄) 算三个假设下的资格判定:

  `as_is`      现状 —— 报价无 `source_asof`/`update_time` ⇒ 门哑火 ⇒ UNKNOWN
  `alias`      别名假设 —— 把我方**抓取时刻** `timestamp` 当 `source_asof`
               (= 扫描期那批修的做法; 扫描期新鲜度门是关的, 故那里安全)
  `genuine`    诚实假设 —— 把交易所侧 `update_time` 当 `source_asof` (= 候选修法)

关键读数 (决定要不要激活门、以及 90s 阈值在盘前是否成立):
  `age_sec_genuine` / `age_sec_alias`      两种口径各自的报价龄
  `newly_rejected_genuine`                 诚实修法会**新拒**的标的 (= 真行为变更面)
  `alias_washes_stale`                     **别名会把真陈旧洗白**: genuine 已超阈而 alias
                                           未超 ⇒ 正是 `futu_lv3_provider.py:1052` B4
                                           注释警告的"15min 延迟报价洗白成新鲜"
  `threshold_sec`                          本次用的 `max_quote_age_sec` (读活配置, 不硬编码)

════════ 边界 (刻意不做的事) ════════

* **不进 inject/scan 生产路径**: 真时戳要走 `FutuLV3Provider.get_snapshot`, 它按
  `_connect_for_historical()` 建历史连接, 实测冷启可达 ~150s ⇒ 绝不能塞进 21:10 那轮
  inject (cron 给整段 scan+inject+push 只有 1800s)。故本脚本是**独立只读探针**。
* **不改 `app/`**: 落 `app/*.py` 会触发部署守卫预约一次 09:00-12:00 自动重启
  (DEB-20260910 §20.10), 影子阶段不该有这个副作用。
* **不落库、不下单、不改配置、不改退出参数** (OKR-2026-09 全月冻结)。
* 只追加一个 jsonl: `data/shadow/ue_quote_freshness_shadow.jsonl`。
* 全程 fail-open 且**降级要响亮** (仓内 except-pass 棘轮 + 禁静默纪律): 任何异常都
  打印 `[warn]`, 不静默吞。

回滚: 删本文件 + 删 jsonl (零生产耦合, 无调用方)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ET = ZoneInfo("America/New_York")
SHADOW_PATH = _ROOT / "data" / "shadow" / "ue_quote_freshness_shadow.jsonl"
SCAN_PATH = _ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"

# 三假设的名字即口径, 落盘时一并写死, 便于日后分段 (不靠列顺序猜)。
HYPOTHESES = ("as_is", "alias", "genuine")


def _now_et() -> datetime:
    return datetime.now(_ET)


def _age_sec(stamp: Any, now: datetime) -> Optional[float]:
    """报价龄 — **直接复用 SSOT 的 `_source_time`**, 不自己解析时区/格式。

    为什么不自己写: `_source_time` 里藏着两条容易抄错的约定 ——
      ① int/float 按 unix 秒走 `fromtimestamp(raw, _ET)`;
      ② 字符串走 `fromisoformat`, 且 **naive 串假定为 ET** (futu `update_time` 正是
        ET-naive, 若按本机 CST 解析会凭空多算 12h ⇒ 全批假陈旧)。
    抄一份就是造第二个真源; 复用 SSOT 才能保证"影子量的门 == 将来激活的门"。
    """
    from app.playbook.candidate_eligibility import _source_time

    if stamp in (None, ""):
        return None
    _, age = _source_time({"source_asof": stamp}, now)
    return age


def quote_freshness_counterfactual(base_facts: Dict[str, Any], *, policy: Dict[str, Any],
                                   now: datetime, fetch_stamp: Any = None,
                                   exchange_stamp: Any = None,
                                   naive_now: Optional[datetime] = None) -> Dict[str, Any]:
    """纯函数: 同一份非时间事实 + 三种时戳口径 ⇒ 三套资格判定与差异。

    Args:
        base_facts: 除 `quote_age_sec` 之外的判定输入 (symbol/price/screening_atr_ratio/
                    bid/ask/direction_hint/shortable)。**原样喂给 SSOT**, 不做任何加工 —
                    否则影子量的就不是生产那道门了。
        policy:     `load_candidate_policy()` 结果 (inject 侧真口径, 含
                    `max_quote_age_sec=90`); 可注入测试。
        now:        带时区观测时点 (**取数之后**定型 = 主口径, 模拟"正确实现")。
        fetch_stamp:   供应商 `timestamp` (我方抓取时刻, unix float)。
        exchange_stamp: 交易所侧 `update_time` (futu ET-naive 串)。
        naive_now:  可选的**取数之前**时点。给了就额外算一遍"朴素实现"下 genuine 的
                    判定 —— 用来量化一个反直觉的失效模式: `now` 若在取数前定型, 活跃
                    标的的交易所时戳会**晚于** `now` ⇒ 报价龄为负 ⇒ 撞上
                    `quote_age < -5 ⇒ future_quote` 判据 ⇒ **越新鲜的报价越先被拒**。
                    2026-09-15 第二次实跑实测到 3/5 标的为负龄 (TRMD -6.5s / COIN -7.2s),
                    与扫描期同一陷阱同源 (DEB-20260910 §21.7), 故必须在影子里分开量。

    Returns:
        每假设的 status/reasons/missing_fields + 差异标志位。零 IO。
    """
    from app.playbook.candidate_eligibility import evaluate_candidate

    ages = {"as_is": None,
            "alias": _age_sec(fetch_stamp, now),
            "genuine": _age_sec(exchange_stamp, now)}
    verdicts: Dict[str, Any] = {}
    for name in HYPOTHESES:
        facts = dict(base_facts)
        facts["quote_age_sec"] = ages[name]
        v = evaluate_candidate(facts, policy)
        verdicts[name] = {"status": v["status"], "reasons": list(v["reasons"]),
                          "missing_fields": list(v["missing_fields"]),
                          "age_sec": (round(ages[name], 3)
                                      if isinstance(ages[name], (int, float)) else None)}

    def _stale(name: str) -> bool:
        return "stale_quote" in verdicts[name]["reasons"]

    threshold = policy.get("max_quote_age_sec")
    row = {
        "symbol": str(base_facts.get("symbol") or ""),
        "threshold_sec": threshold,
        "age_sec_alias": verdicts["alias"]["age_sec"],
        "age_sec_genuine": verdicts["genuine"]["age_sec"],
        "verdicts": verdicts,
        # 真行为变更面: 现状不拒, 诚实修法拒。
        "newly_rejected_genuine": (_stale("genuine")
                                   and verdicts["as_is"]["status"] != "REJECT"),
        # B4 洗白风险: 别名口径判新鲜, 交易所口径已超阈 ⇒ 用抓取时刻当行情时点会掩盖真陈旧。
        "alias_washes_stale": bool(_stale("genuine") and not _stale("alias")),
        # 反向: 别名口径反而更严 (时钟偏差/缓存回放), 说明 timestamp 不是可靠替代。
        "alias_stricter_than_genuine": bool(_stale("alias") and not _stale("genuine")),
        "future_quote_genuine": "future_quote" in verdicts["genuine"]["reasons"],
        "exchange_stamp_available": exchange_stamp not in (None, ""),
        "fetch_stamp_available": fetch_stamp not in (None, ""),
    }
    if naive_now is not None:
        naive_age = _age_sec(exchange_stamp, naive_now)
        naive_facts = dict(base_facts)
        naive_facts["quote_age_sec"] = naive_age
        naive_v = evaluate_candidate(naive_facts, policy)
        row["age_sec_genuine_naive_now"] = (round(naive_age, 3)
                                            if isinstance(naive_age, (int, float)) else None)
        row["future_quote_under_naive_now"] = "future_quote" in naive_v["reasons"]
        row["stale_quote_under_naive_now"] = "stale_quote" in naive_v["reasons"]
    return row


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """纯函数: 把逐标的差异汇成一句话能读完的读数 (毕业/裁决就看这几个数)。"""
    n = len(rows)
    have_ex = [r for r in rows if r["exchange_stamp_available"]]
    return {
        "n": n,
        "n_exchange_stamp_available": len(have_ex),
        "n_newly_rejected_genuine": sum(1 for r in rows if r["newly_rejected_genuine"]),
        "n_alias_washes_stale": sum(1 for r in rows if r["alias_washes_stale"]),
        "n_alias_stricter": sum(1 for r in rows if r["alias_stricter_than_genuine"]),
        "n_stale_genuine": sum(1 for r in rows
                               if "stale_quote" in r["verdicts"]["genuine"]["reasons"]),
        "n_future_quote_genuine": sum(1 for r in rows if r.get("future_quote_genuine")),
        # 朴素实现 (now 在取数前定型) 额外会误拒多少个 — 这是"激活门的正确姿势"证据。
        "n_future_quote_under_naive_now": sum(1 for r in rows
                                              if r.get("future_quote_under_naive_now")),
        "age_genuine_max_sec": max([r["age_sec_genuine"] for r in rows
                                    if r["age_sec_genuine"] is not None], default=None),
        "age_genuine_median_sec": _median([r["age_sec_genuine"] for r in rows
                                           if r["age_sec_genuine"] is not None]),
        "threshold_sec": rows[0]["threshold_sec"] if rows else None,
    }


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(float(ordered[mid]), 3)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 3)


def latest_ue_symbols(limit: int = 12) -> List[str]:
    """从当日最后一条 scan 记录取 surfaced 标的 —— 正是"门若激活会影响谁"的那批。

    只读 jsonl, 不连库、不 import 生产模块 ⇒ 与生产链零耦合。
    """
    try:
        lines = SCAN_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"  [warn] 读 scan jsonl 失败 (无标的可测): {exc}")
        return []
    bad_lines = 0
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            # 不静默跳行 (except-pass 棘轮 + 禁静默纪律): 半截行/坏行会让"当日最后一条
            #   scan"被悄悄换成更早一条 ⇒ 影子量的不是今晚那批标的, 读数失真却无人知。
            bad_lines += 1
            print(f"  [warn] scan jsonl 有不可解析行, 已跳过 {bad_lines} 行 (继续向前找): {exc}")
            continue
        if rec.get("type") != "scan":
            continue
        syms = [str(s.get("symbol") or "").strip().upper()
                for s in (rec.get("surfaced") or [])]
        syms = list(dict.fromkeys(s for s in syms if s))
        return syms[:limit]
    if bad_lines:
        print(f"  [warn] scan jsonl 全文无 type=scan 记录 (坏行 {bad_lines}) — 无标的可测")
    return []


_release_note = ("显式释放 OpenD 槽位: 实测本脚本跑完后 futu SDK 的非守护线程会让进程"
                 "挂着不自退 (需外部 SIGTERM), 而 OpenD 槽位是**与生产链路共享**的资源 —"
                 "影子占着槽位就可能挤掉 21:10 inject 的取价。")


def _release(*closers) -> None:
    """逐个关闭连接, 每个都独立兜错 (一个关不掉不能连累另一个)。"""
    for label, fn in closers:
        if fn is None:
            continue
        try:
            fn()
        except Exception as exc:
            print(f"  [warn] {label} 释放失败 (槽位可能残留, 下次取价会重建): {exc}")


def _collect_stamps(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """取两种时戳: 交易所侧 `update_time` (真) + 我方 `timestamp` (抓取时刻)。

    ⚠ `get_snapshot` 走历史连接, 冷启实测可达 ~150s ⇒ 本函数只属独立探针, 严禁进
      inject/scan 生产路径 (cron 给 scan+inject+push 整段只有 1800s)。

    连接**必须**在 finally 里释放 (理由见 `_release_note`)。
    """
    out: Dict[str, Dict[str, Any]] = {}
    provider = None
    router = None
    rows: List[Dict[str, Any]] = []
    quotes: Dict[str, Any] = {}
    try:
        try:
            from app.data.futu_lv3_provider import FutuLV3Provider
            provider = FutuLV3Provider()
            rows = provider.get_snapshot([f"US.{s}" for s in symbols])
        except Exception as exc:
            print(f"  [warn] 交易所侧快照取数失败 (本批 genuine 假设不可测, 只记 as_is/alias): {exc}")
            rows = []
        for row in rows or []:
            code = str(row.get("code") or "").replace("US.", "")
            if not code:
                continue
            out[code] = {"exchange_stamp": str(row.get("update_time") or "") or None,
                         "price": row.get("last_price"),
                         "bid": row.get("bid"), "ask": row.get("ask")}
        # 我方抓取时刻: 走生产同一个 router, 保证 alias 假设量的就是生产那条路。
        try:
            from scripts.llm_universe_scan import _build_router
            router = _build_router()
            quotes = router.get_batch_quotes([f"US.{s}" for s in symbols]) or {}
        except Exception as exc:
            print(f"  [warn] router 报价取数失败 (alias 假设不可测): {exc}")
            quotes = {}
        for key, quote in quotes.items():
            if not isinstance(quote, dict):
                continue
            bare = str(key).replace("US.", "")
            slot = out.setdefault(bare, {"exchange_stamp": None})
            slot["fetch_stamp"] = quote.get("timestamp")
            slot.setdefault("price", quote.get("price"))
            slot.setdefault("bid", quote.get("bid") or quote.get("bid_price"))
            slot.setdefault("ask", quote.get("ask") or quote.get("ask_price"))
    finally:
        _release(("provider", getattr(provider, "disconnect", None)),
                 ("router", getattr(router, "close", None)))
    return out


def run(symbols: Optional[List[str]] = None, *, limit: int = 12,
        write: bool = True) -> Dict[str, Any]:
    from app.playbook.candidate_eligibility import canonical_symbol, load_candidate_policy

    syms = [s for s in (symbols or []) if s] or latest_ue_symbols(limit)
    now_before = _now_et()
    now = now_before
    record: Dict[str, Any] = {
        "type": "quote_freshness_shadow", "date": now.astimezone(_ET).date().isoformat(),
        "observed_at": now.isoformat(), "observed_at_et": now.astimezone(_ET).isoformat(),
        "schema": "ue_quote_freshness_shadow_v1", "n_symbols_requested": len(syms),
        "rows": [], "summary": {},
    }
    if not syms:
        record["status"] = "no_symbols"
        print("[shadow] 无可测标的 (当日 scan 无 surfaced?) — 不落盘")
        return record
    try:
        policy = load_candidate_policy()
    except Exception as exc:
        print(f"  [warn] 资格策略读取失败, 影子整体跳过 (不猜阈值): {exc}")
        record["status"] = "policy_unavailable"
        return record
    record["policy_version"] = policy.get("version")
    record["threshold_sec"] = policy.get("max_quote_age_sec")
    stamps = _collect_stamps(syms)
    # 主口径的 `now` **在取数之后**定型 (与扫描期修法③同一原则, §21.7): 否则活跃标的的
    #   交易所时戳晚于 now ⇒ 负龄 ⇒ 撞 future_quote ⇒ "越新鲜越先被拒"的假读数。
    #   取数前的时点仍保留为 naive_now, 用来**量化**朴素实现会多误拒多少 (不靠猜)。
    now = _now_et()
    record["observed_at"] = now.isoformat()
    record["observed_at_et"] = now.astimezone(_ET).isoformat()
    record["now_before_collect_et"] = now_before.isoformat()
    record["collect_latency_sec"] = round((now - now_before).total_seconds(), 3)
    rows: List[Dict[str, Any]] = []
    for sym in syms:
        got = stamps.get(sym) or {}
        facts = {"symbol": canonical_symbol(sym), "price": got.get("price"),
                 # 非时间事实刻意留空: 本影子只问"时戳口径改变会不会新拒", 不去复刻
                 # ATR/借券等其它门 (那些由生产初筛自己判)。缺 ATR ⇒ 三假设同样落
                 # missing, 对**差异**无影响, 差异全部来自 quote_age_sec。
                 "screening_atr_ratio": None, "bid": got.get("bid"), "ask": got.get("ask"),
                 "direction_hint": None, "shortable": None}
        rows.append(quote_freshness_counterfactual(
            facts, policy=policy, now=now,
            fetch_stamp=got.get("fetch_stamp"), exchange_stamp=got.get("exchange_stamp"),
            naive_now=now_before))
    record["rows"] = rows
    record["summary"] = summarize(rows)
    record["status"] = "ok"
    if write:
        try:
            SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
            with SHADOW_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            print(f"  [warn] 影子落盘失败 (度量已算出, 仅未持久化): {exc}")
            record["status"] = "write_failed"
    s = record["summary"]
    print(f"[shadow] 报价新鲜度门哑弹量化 n={s.get('n')} "
          f"交易所时戳可得={s.get('n_exchange_stamp_available')} "
          f"阈值={s.get('threshold_sec')}s")
    print(f"  genuine 口径超阈={s.get('n_stale_genuine')} "
          f"⇒ 若诚实激活会**新拒**={s.get('n_newly_rejected_genuine')}")
    print(f"  别名(抓取时刻)洗白真陈旧={s.get('n_alias_washes_stale')} "
          f"反向更严={s.get('n_alias_stricter')} "
          f"genuine 龄 中位={s.get('age_genuine_median_sec')}s 最大={s.get('age_genuine_max_sec')}s")
    print(f"  genuine future_quote={s.get('n_future_quote_genuine')} "
          f"(朴素 now 口径下会多误拒 {s.get('n_future_quote_under_naive_now')}) "
          f"取数耗时={record.get('collect_latency_sec')}s")
    for r in rows:
        flag = " ⚠新拒" if r["newly_rejected_genuine"] else ""
        wash = " ⚠别名洗白" if r["alias_washes_stale"] else ""
        naive = (" ⚠朴素now会误拒" if r.get("future_quote_under_naive_now") else "")
        print(f"    {r['symbol']:8s} genuine={r['age_sec_genuine']}s "
              f"alias={r['age_sec_alias']}s "
              f"{r['verdicts']['as_is']['status']}→{r['verdicts']['genuine']['status']}"
              f"{flag}{wash}{naive}")
    if write:
        print(f"  → {SHADOW_PATH.relative_to(_ROOT)}")
    return record


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", nargs="*", default=None,
                        help="指定标的 (裸代码); 缺省=当日最后一条 scan 的 surfaced")
    parser.add_argument("--limit", type=int, default=12, help="缺省取样上限")
    parser.add_argument("--dry", action="store_true", help="只算不落盘")
    args = parser.parse_args(argv)
    rec = run(args.symbols, limit=args.limit, write=not args.dry)
    return 0 if rec.get("status") in ("ok", "no_symbols") else 1


if __name__ == "__main__":
    raise SystemExit(main())
