#!/usr/bin/env python3
"""markout 成交质量采集 (2026-07-30, P0-2 切实盘数据准备, 用户裁"要")。

是什么: 每笔入场成交后 1/5/15 min, 记录当时价与入场价的方向感知漂移
(drift>0 = 成交后朝有利方向走)。这是 sim 环境里最接近实盘滑点/逆向选择
(adverse selection) 的代理指标 — 若 1min drift 系统性为负, 说明触发即追高,
live 后冲击成本会进一步放大该效应。攒样本供 08-29 终裁"sim→live 兑现率"。

口径: 方向感知 — LONG: (px-entry)/entry; SHORT: (entry-px)/entry。只处理
当日 (ET) 有 entry_price 的 trade_records; 幂等 — (trade_id,horizon) 已记跳过。
record-only: 只读行情, 零交易影响; 结束 os._exit (futu 非 daemon 线程教训)。

cron: */5 21-23 * * 1-5 + */5 0-4 * * 2-6 (盘中, 建议 timeout 180)。
"""
import json
import logging
import os
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "shadow" / "markout.jsonl"
SENTINEL = ROOT / "data" / ".markout_done"
LOG = ROOT / "logs" / "markout_capture.log"
HORIZONS = (60, 300, 900)  # 秒

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [markout] %(levelname)s: %(message)s",
    handlers=[logging.FileHandler(LOG), logging.StreamHandler()],
)
logger = logging.getLogger("markout_capture")


def _load_recorded() -> set:
    keys = set()
    if OUT.exists():
        try:
            with open(OUT, encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        keys.add((d.get("trade_id"), d.get("horizon_s")))
                    except Exception:
                        continue
        except Exception:
            pass
    return keys


def _make_quote_ctx(host: str, port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    if sock.connect_ex((host, port)) != 0:
        sock.close()
        return None
    sock.close()
    try:
        import futu.common.ft_logger as _ftl
        _ftl.logger.file_level = logging.WARNING
        _ftl.logger.console_level = logging.WARNING
    except Exception:
        pass
    from futu import OpenQuoteContext, SysConfig
    SysConfig.enable_proto_encrypt(False)
    return OpenQuoteContext(host=host, port=port)


def main() -> int:
    from app.utils.timezone import now_et
    now = now_et()
    today = now.strftime("%Y-%m-%d")
    if not DB.exists():
        logger.warning(f"DB 不存在: {DB}")
        return 0
    conn = sqlite3.connect(str(DB))
    try:
        rows = conn.execute(
            "SELECT id, symbol, direction, entry_price, entry_time "
            "FROM trade_records WHERE substr(entry_time,1,10)=? AND entry_price>0",
            (today,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        logger.info("当日无入场记录")
        return 0
    if SENTINEL.exists():
        try:
            d = json.loads(SENTINEL.read_text())
            if d.get("date") == today and d.get("trades") == len(rows):
                return 0  # 当日标记已采齐且无新入场 — 静默退出 (省 ~80 次/日空醒)
        except Exception:
            pass
    recorded = _load_recorded()
    # 计算待采标记: (trade_id, symbol, direction, entry, horizon)
    pending = []
    for tid, sym, direction, entry, etime in rows:
        try:
            t0 = datetime.fromisoformat(str(etime).replace("Z", "+00:00"))
            if t0.tzinfo is None:
                from app.utils.timezone import ET as _ET
                t0 = t0.replace(tzinfo=_ET)
        except Exception:
            continue
        age = (now - t0).total_seconds()
        for h in HORIZONS:
            if age >= h and (tid, h) not in recorded:
                pending.append((tid, sym, str(direction or "LONG").upper(),
                                float(entry), h))
    if not pending:
        all_keys = {(r[0], h) for r in rows for h in HORIZONS}
        if all_keys and all_keys.issubset(recorded):
            SENTINEL.write_text(json.dumps({"date": today, "trades": len(rows)}))
            logger.info(f"当日 {len(rows)} 笔 ×{len(HORIZONS)} horizons 全部采齐 — 后续静默")
        else:
            logger.info(f"无待采标记 (当日 {len(rows)} 笔入场)")
        return 0
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    ctx = _make_quote_ctx(host, port)
    if ctx is None:
        logger.warning(f"OpenD {host}:{port} 不可达, 本轮跳过 ({len(pending)} 待采)")
        return 0
    symbols = sorted({p[1] for p in pending})
    prices = {}
    try:
        ret, snap = ctx.get_market_snapshot(symbols)
        if ret == 0 and snap is not None and not snap.empty:
            for _, r in snap.iterrows():
                lp = r.get("last_price")
                if lp and float(lp) > 0:
                    prices[str(r.get("code"))] = float(lp)
    except Exception as e:
        logger.warning(f"snapshot 失败: {e}")
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    ts = now.isoformat()
    n = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        for tid, sym, direction, entry, h in pending:
            px = prices.get(sym)
            if not px:
                continue
            drift = (px - entry) / entry * 100 if direction != "SHORT" \
                else (entry - px) / entry * 100
            f.write(json.dumps({
                "trade_id": tid, "symbol": sym, "direction": direction,
                "entry_price": entry, "horizon_s": h,
                "price_after": round(px, 4),
                "drift_pct": round(drift, 4), "marked_at": ts,
            }, ensure_ascii=False) + "\n")
            n += 1
    logger.info(f"标记 {n}/{len(pending)} 笔 (当日入场 {len(rows)}, 报价 {len(prices)}/{len(symbols)})")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
