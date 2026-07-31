#!/usr/bin/env python3
"""买卖价差/盘口深度快照采集 (2026-07-30, P0-1 切实盘数据准备, 用户裁"要")。

为什么: sim 按盘口快照成交, 低流动/宽价差标的盈利含虚高成分; quote 无 bid/ask
字段 (liquidity gate 的 spread 门因此如实放弃)。本脚本盘中每 30min 对活跃池
抓 Level1 盘口落 jsonl — 攒 2-4 周后可建真实成本模型 (spread+冲击) 替换 est,
并为 08-29 终裁提供"sim→live 兑现率"量化依据。

record-only: 只读行情, 零交易影响; 单标的失败跳过; 结束 os._exit
(futu SDK 非 daemon 线程会卡解释器退出 — regime_divergence 挂死同款教训)。

cron: */30 21-23 * * 1-5 + */30 0-4 * * 2-6 (盘中, 建议 timeout 180)。
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
OUT = ROOT / "data" / "shadow" / "spread_depth.jsonl"
LOG = ROOT / "logs" / "spread_depth_capture.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [spread-cap] %(levelname)s: %(message)s",
    handlers=[logging.FileHandler(LOG), logging.StreamHandler()],
)
logger = logging.getLogger("spread_capture")


def _pool_symbols() -> list:
    try:
        data = json.loads((ROOT / "data" / "daily_active_pool.json").read_text())
        out = []
        for e in data.get("entries", []) or []:
            c = str(e.get("code", ""))
            if c.startswith("US.") and len(c) <= 9:  # 排除期权码
                out.append(c)
        return sorted(set(out))
    except Exception as e:
        logger.warning(f"活跃池读取失败: {e}")
        return []


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
    # 2026-07-30 实测: 盘中 OpenD 负载高时握手偶发超时, 且 SDK 不抛异常而是返回
    #   坏 context (后续查询永久挂死)。用 get_global_state 探活 (有内部超时),
    #   失败关连接重试, 最多 3 次。
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


def main() -> int:
    from app.utils.timezone import now_et
    symbols = _pool_symbols()
    if not symbols:
        logger.warning("活跃池为空, 本轮跳过")
        return 0
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    ctx = _make_quote_ctx(host, port)
    if ctx is None:
        logger.warning(f"OpenD {host}:{port} 不可达, 本轮跳过")
        return 0
    ts = now_et().isoformat()
    n_ok = 0
    rows = []
    try:
        # 2026-07-30 实测: snapshot 自带 bid/ask+量 (Nasdaq Basic 权限即有),
        #   单次批量调用, 无需 ORDER_BOOK 订阅 (订阅路径配额受限且坏连接会挂死)。
        for i in range(0, len(symbols), 30):
            batch = symbols[i:i + 30]
            try:
                ret, df = ctx.get_market_snapshot(batch)
                if ret != 0 or df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    try:
                        bp, ap = float(row.get("bid_price") or 0), float(row.get("ask_price") or 0)
                        if bp <= 0 or ap <= 0:
                            continue
                        mid = (bp + ap) / 2
                        rows.append({
                            "ts_et": ts, "symbol": str(row.get("code", "")),
                            "bid": bp, "ask": ap,
                            "spread_pct": round((ap - bp) / mid * 100, 4),
                            "bid_sz1": int(row.get("bid_vol") or 0),
                            "ask_sz1": int(row.get("ask_vol") or 0),
                        })
                        n_ok += 1
                    except Exception:
                        continue
            except Exception:
                continue
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
    logger.info(f"快照 {ts[:16]}: {n_ok}/{len(symbols)} 只落盘 → {OUT.name}")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
