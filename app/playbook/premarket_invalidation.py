"""盘前失效评估器 (record-only shadow, 2026-08-09 用户批准方案1).

问题: 盘前指令基于昨日数据生成, 隔夜+盘前的真实变化 (跳空/逆向走) 目前要到
触发时刻才被门感知 — 盘前 09:30 前的信息完全没被用于指令侧校验。

机制: 每个交易日 09:35-09:59 ET 评估一次 — 对当日全部可执行指令取快照
(futu get_snapshot, 历史连接路径), 计算涨跌幅 vs prev_close, 按候选规则打
"若启用将失效"标记, 落 data/premarket_invalidation_shadow.jsonl。
零行为变更; 攒样本后与 trade_records 对照判定哪条规则有真价值。

字段语义正名 (2026-09-11, DEB-20260911 Q1): `premarket_price` 是 **09:35-09:59 ET
常规时段**的 snapshot.last_price, 不是盘前 pre_price; `chg_pct` 的分母是 snapshot
的 prev_close (昨日正式收盘)。命名系 8/9 立项遗留, 读该字段时勿按"盘前价"推理。
**评估窗不得前移到盘前**: 快照 prev_close_price 在盘前窗有 62% 概率返回两天前收盘
(market_data_router.py:509-534 注释实测) — 前移即污染本影子全部分母。

现役规则 (2026-09-11 起仅存 B; A/C 已裁退役, 理由见 _RULES 注):
  B: LONG 且跌<-5% — 接飞刀区 (DEB §5.3 / DA prompt v2.4 已禁产;
     执行时刻口径硬门 RFC-068 已在线, 本影子测的是盘前快照口径的增量)
  注: LONG 跌 -2%~0 (回调日) 是赢家模式 (+$103), 明确不打标。

回滚: playbook.premarket_invalidation_observe 置 false + 重启 (会连 B 采集一起停)。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_OUTPUT_PATH = Path("data/premarket_invalidation_shadow.jsonl")
_WINDOW = (935, 959)          # 09:35-09:59 ET 评估窗
_done_date = None             # 进程内每日一次

_RULES = (
    # (rule_id, direction, chg阈值方向, 阈值, 说明)
    # direction 用指令表口径 (2026-08-09 DB 实查): playbook_instructions.direction =
    #   BUY/SELL_SHORT/SKIP — "SHORT" 枚举仅存在于 trade_records; evaluate_rules 做双向归一。
    #
    # 2026-09-11 退役 A_short_chase / C_long_chase 采集 (DEB-20260911 两轮多模型辩论,
    # 用户裁「按建议执行」; 共识 C2/C4/C5/C8):
    #   A — 旧否决前提已失效 (禁触窗 8/15 由 1015→0935, git 1d2b909d, DEB-20260808 的
    #       -$19.80 残存效应不可直接引用), 但 09:35 快照相位本身被证据否定; 其假设
    #       「逆势追空差」在 310 行 IC 上成立 (SELL_SHORT rho=-0.378, p≈0.005) ⇒ 转入
    #       short_chase_ban_shadow (触发时刻相位 = B 转正先例) 作单一证据源重测,
    #       **快照行不得独立转正** (防双影子双转正打架)。
    #   C — chg 轴方向用反: BUY rho=+0.151 (p≈0.018), C 命中集 (chg>+3%, n=32) 指令口径
    #       fwd_return_t1_adj 均 +4.90% vs 阈下 +1.60%; 注册口径 executed 净 -$152.49 /
    #       误杀 68.2% 超预注册门 40%; chg>10% 桶净 -$319.6 全由单笔过夜 GAP -$380.3 驱动
    #       (同桶其余 7 笔日内 +$60.6) ⇒ 尾部/过夜问题, 非入场门可解; 追高侧已有四道
    #       anchor 轴硬门 (extreme_gap 8% / thesis 漂移 5% / chase_cap / entry band)。
    #   历史行留在 data/premarket_invalidation_shadow.jsonl (退役=停采集, 不删数据);
    #   结算器对 A/C 恒判 RETIRED_NO_PROMOTION (settle_premarket_invalidation.RETIRED_RULES)。
    #   回滚: 恢复本元组两行 + 重启 (行为等价, 采集侧无交易面)。
    ("B_long_knife", "BUY", "<", -5.0, "接飞刀区 (执行时刻硬门 RFC-068 已在线)"),
)


def _canon_direction(direction: str) -> str:
    """方向枚举归一: SHORT/SELL_SHORT/SELL → SELL_SHORT (指令表口径)."""
    d = str(direction or "").upper()
    return "SELL_SHORT" if d in ("SHORT", "SELL_SHORT", "SELL") else d


def evaluate_rules(direction: str, chg_pct: float):
    """纯函数: 返回命中的 rule_id 列表 (可单测)."""
    direction = _canon_direction(direction)
    hits = []
    for rid, d, op, th, _ in _RULES:
        if direction != d:
            continue
        if op == ">" and chg_pct > th:
            hits.append(rid)
        elif op == "<" and chg_pct < th:
            hits.append(rid)
    return hits


def _observe_enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted(
            "playbook.premarket_invalidation_observe", True))
    except Exception as _cfg_err:
        # 2026-11-03 (全链审计复核): 配置读取失败 ⇒ 影子**静默停采** ⇒ 证据流干涸而
        # 无人知情。按 R5「新增异常处理必须 warn 留痕」提级 (原为无声 return False)。
        # WARNING 不被 error_alert 计数 (只扫 ERROR/CRITICAL) ⇒ 不产生推送噪音。
        logger.warning("[盘前失效] 观察开关读取失败 ⇒ 本次不采集 (影子停采): %s", _cfg_err, exc_info=True)
        return False


def run_check(scheduler=None) -> int:
    """调度器钩子: 每日 09:35-09:59 ET 评估一次, 返回落盘行数. 异常静默."""
    global _done_date
    try:
        import pytz
        from datetime import datetime
        now_et = datetime.now(pytz.timezone("America/New_York"))
        today = now_et.strftime("%Y-%m-%d")
        hm = now_et.hour * 100 + now_et.minute
        if not (_WINDOW[0] <= hm <= _WINDOW[1]) or _done_date == today:
            return 0
        if not _observe_enabled():
            return 0

        from app.playbook.db import PlaybookDB
        rows = PlaybookDB().get_instructions_by_date(today)
        insts = [r for r in rows
                 if r.get("direction") in ("BUY", "SELL_SHORT") and not r.get("skip_reason")]
        if not insts:
            return 0

        router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        futu = getattr(router, "futu", None) if router is not None else None
        if futu is None or not hasattr(futu, "get_snapshot"):
            return 0

        symbols = sorted({r["symbol"] for r in insts})
        snaps = futu.get_snapshot(symbols)
        if not snaps:
            return 0
        snap_by = {r["code"].replace("US.", "", 1): r for r in snaps if r.get("last_price", 0) > 0}

        _done_date = today
        written = 0
        flagged = 0
        _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _OUTPUT_PATH.open("a", encoding="utf-8") as f:
            for r in insts:
                sym = r["symbol"].replace("US.US.", "US.").replace("US.", "", 1)
                s = snap_by.get(sym) or snap_by.get(r["symbol"])
                if not s or not s.get("prev_close"):
                    continue
                chg = (s["last_price"] - s["prev_close"]) / s["prev_close"] * 100.0
                hits = evaluate_rules(r["direction"], chg)
                if hits:
                    flagged += 1
                rec = {
                    "ts": now_et.isoformat(timespec="seconds"),
                    "date": today, "symbol": r["symbol"], "direction": r["direction"],
                    "inst_id": r.get("id"), "decision_id": r.get("decision_id"),
                    "premarket_price": s["last_price"], "prev_close": s["prev_close"],
                    "chg_pct": round(chg, 3), "anchor": r.get("entry_price"),
                    "would_invalidate": hits, "schema": "v1",
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
        logger.info(f"[盘前失效] 评估落盘 {written} 行 (命中规则 {flagged} 条) → {_OUTPUT_PATH.name}")
        return written
    except Exception as e:
        # 2026-11-03: 评估失败 ⇒ 本日证据断档。静默死亡族 (R5: 异常必须 warn 留痕)。
        # WARNING 不被 error_alert 计数 ⇒ 无推送噪音。
        logger.warning(f"[盘前失效] 评估异常 (已隔离, 本日证据断档): {e}", exc_info=True)
        return 0
