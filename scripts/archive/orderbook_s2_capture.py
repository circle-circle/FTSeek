#!/usr/bin/env python3
"""S2 窄校准实验 · 订单簿影子采集 (2026-08-04, 用户批准, DEB-ruler 6.3④/§7.4-5)。

目的: 校准"代理因子失真" — 主链路 QUOTE push 无 bid/ask, 用 last_price 兜底
(futu_lv3_provider.py L426-431/L791-795), 导致 order_flow_imbalance 恒 0、
spread 门形同虚设。本实验对 4 只标的临时订阅 ORDER_BOOK, 记录真值盘口,
期末 (2026-08-18) 测: ①真值 spread/深度失衡的分布 (代理恒报 0 的量级);
②真值因子对 5/15min 前瞻收益的 IC — IC≈0 则代理失真无害 (合法关门),
IC 显著则重开扩面议。

record-only: 独立 OpenQuoteContext, 只订阅实验标的 ORDER_BOOK (4 槽/1000),
不动 config/unified_config.yaml stock_sub_types (全池恢复 = ADR-013 明令禁止),
不挂主进程 (避免 ob_* 复活改变实验标的信号 → 纯影子), 零交易影响。

标的 (era 高频 × 价格带/板块分散): NVDA / AMZN / ORCL / MRK。
窗口: 2026-08-04 → 08-18 (2 周), 到期摘 cron 并跑校准分析。

cron: */30 21-23 * * 1-5 + */30 0-4 * * 2-6 (盘中, timeout 180) —
同 spread_depth_capture 双段模式。
"""
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "shadow" / "orderbook_s2.jsonl"
LOG = ROOT / "logs" / "orderbook_s2_capture.log"

SYMBOLS = ["US.NVDA", "US.AMZN", "US.ORCL", "US.MRK"]
EXP_END = "2026-08-18"  # 到期后 cron 摘除 + 校准分析

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ob-s2] %(levelname)s: %(message)s",
    handlers=[logging.FileHandler(LOG), logging.StreamHandler()],
)
logger = logging.getLogger("orderbook_s2")


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
    # spread_depth_capture 同款: OpenD 负载高时握手偶发超时返回坏 context,
    # get_global_state 探活 + 3 次重试。
    last_err = None
    for attempt in range(3):
        try:
            ctx = OpenQuoteContext(host=host, port=port)
            ret, _ = ctx.get_global_state()
            if ret == 0:
                return ctx
            last_err = f"get_global_state ret={ret}"
            try:
                ctx.close()
            except Exception:
                pass
        except Exception as e:
            last_err = e
        time.sleep(3)
    logger.warning(f"OpenQuoteContext 连接重试 3 次仍失败: {last_err}")
    return None


def _fetch_book(ctx, symbol: str):
    ret, data = ctx.get_order_book(symbol, num=10)
    if ret != 0 or data is None:
        return None
    # 实测 (2026-08-04): 本 futu 版本返回 dict —
    # {"Bid": [(price, vol, order_num, {}), ...], "Ask": [...]}
    try:
        if isinstance(data, dict):
            bids = list(data.get("Bid") or [])[:10]
            asks = list(data.get("Ask") or [])[:10]
        else:  # DataFrame 兜底
            row = data.iloc[0]
            bids = list(row.get("Bid") or [])[:10]
            asks = list(row.get("Ask") or [])[:10]
        if not bids or not asks:
            return None
        bp = [float(x[0]) for x in bids]
        bv = [float(x[1]) for x in bids]
        ap = [float(x[0]) for x in asks]
        av = [float(x[1]) for x in asks]
    except Exception:
        return None
    if bp[0] > 0 and ap[0] > 0:
        return {"bid_prices": bp, "bid_volumes": bv,
                "ask_prices": ap, "ask_volumes": av}
    return None


def main() -> int:
    from app.utils.timezone import now_et
    if now_et().strftime("%Y-%m-%d") > EXP_END:
        logger.info("实验窗口已过 (08-18), 请摘除 cron")
        return 0
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    ctx = _make_quote_ctx(host, port)
    if ctx is None:
        logger.warning(f"OpenD {host}:{port} 不可达, 本轮跳过")
        return 0
    ts = now_et().isoformat()
    rows = []
    try:
        from futu import SubType
        ret, msg = ctx.subscribe(SYMBOLS, [SubType.ORDER_BOOK], subscribe_push=False)
        if ret != 0:
            logger.warning(f"ORDER_BOOK 订阅失败: {msg}")
            return 0
        time.sleep(2)
        for sym in SYMBOLS:
            try:
                book = _fetch_book(ctx, sym)
                snap_row = {}
                try:
                    ret2, df2 = ctx.get_market_snapshot([sym])
                    if ret2 == 0 and df2 is not None and not df2.empty:
                        r2 = df2.iloc[0]
                        snap_row = {"snap_bid": float(r2.get("bid_price") or 0),
                                    "snap_ask": float(r2.get("ask_price") or 0),
                                    "last": float(r2.get("last_price") or 0)}
                except Exception:
                    pass
                if not book:
                    logger.warning(f"{sym}: 盘口拉取失败")
                    continue
                b1, a1 = book["bid_prices"][0], book["ask_prices"][0]
                mid = (b1 + a1) / 2
                bt = sum(v for v in book["bid_volumes"] if v > 0)
                at = sum(v for v in book["ask_volumes"] if v > 0)
                rows.append({
                    "ts_et": ts, "symbol": sym,
                    "exp": "S2-orderbook-calibration",
                    **book,
                    "spread_bps_true": round((a1 - b1) / mid * 10000, 2) if mid > 0 else None,
                    "bid_ask_ratio_l1": round(book["bid_volumes"][0] / book["ask_volumes"][0], 3)
                    if book["ask_volumes"][0] > 0 else None,
                    "depth_imbalance_10": round((bt - at) / (bt + at), 4) if (bt + at) > 0 else None,
                    **snap_row,
                    # 代理口径 (主链路 push 路径实际所见, 恒退化):
                    "proxy_spread_bps": 0.0, "proxy_ofi": 0.0,
                    "proxy_note": "push path bid=ask=last_price, bid_vol=ask_vol=0",
                })
            except Exception as e:
                logger.warning(f"{sym}: 异常 {e}")
        try:
            ctx.unsubscribe(SYMBOLS, [SubType.ORDER_BOOK])
        except Exception:
            pass
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    if rows:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info(f"快照 {ts[:16]}: {len(rows)}/{len(SYMBOLS)} 只落盘 → {OUT.name}")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
