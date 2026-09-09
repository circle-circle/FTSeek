r"""实盘成交实时推送订阅器 (RFC-078 批E push 层, 2026-09-09, record-only).

## 为什么需要 (用户 9/9 问"实盘成交能否直接获取, 不用事后定期同步")

RFC-078 批B 的券商真值层靠 cron 定期回补 (`live_ledger_sync.py`, 每日 17:10 CST +
周六 09:10) ⇒ 实盘成交可见性最差 T+1。9/8 TSLA 手动平仓 (富途 APP 直接操作,
本地台账层完全看不见) 直到次日手动 sync 才落库 — 真钱交易长时间静默。

本模块挂 Futu 原生成交推送 `TradeDealHandlerBase` (秒级, 零轮询), 成交当场:
  ① 幂等落 `live_deal_records` (复用 live_ledger.record_deal_push, 与每日 sync
     共用同一 upsert SSOT `_upsert_deal_row` — 两链路字段映射永不漂移);
  ② 可选飞书即时提醒 (env FTSEEK_LIVE_DEAL_PUSH_NOTIFY, 默认开)。
每日 sync cron **保留**, 降级为对账兜底 (推送可能漏: 服务重启窗口 / OpenD 断线)。

## 三个实测确认的正确性要点 (写码前逐一探针验证)

1. **必须过滤 trd_env == 'REAL'**: 模拟盘 playbook 用**同一实盘账户** +
   TrdEnv.SIMULATE 成交 (dual_broker.py:75 "美股模拟用实盘账户+TrdEnv.SIMULATE"),
   push 行 `trd_env` 经 `TrdEnv.to_string2` 为字符串 'REAL'/'SIMULATE'
   (futu/trade/trade_query.py UpdateDealPush.unpack_rsp 实测) — 不过滤会把
   sim 高频成交灌进实盘账本 = 跨环境污染。
2. **push 行市场字段是 `trd_market`** (query 行是 `deal_market`) —
   `_upsert_deal_row` 已兼容两者; market/currency 仍由行自带字段推导。
3. **deal_id 19 位 TEXT**: push dict 的 deal_id 经 pandas DataFrame 可能变
   numpy int64/str — `_s()` 统一 str(), 幂等键不失效。

## 连接拓扑 (刻意不复用 DualBroker/hk_order_service 的 ctx)

- 推送是**被动接收**, 不需要 unlock_trade (unlock 仅下单需要) ⇒ 自建常驻
  OpenSecTradeContext(US) + (HK) 各一, **完全不碰下单 ctx = 零交易风险**。
- 综合账户下"单 ctx 是否收到另一市场成交的推送"无法离线验证 ⇒ 双 ctx 都挂
  handler, 同一笔可能双收 — 靠 deal_id 幂等去重 (sync_deals 两腿重叠同模式,
  已生产验证), 对"过滤/不过滤"两种 SDK 行为都正确。
- hk_order_service 的 ctx 是 ephemeral (每次操作 connect/disconnect), 不能挂推送。
- Futu SDK conn_mng 自带断线重连, handler 注册在 ctx 生命周期内持续有效;
  OpenD 重启窗口的漏推由每日 sync cron 兜底 (设计如此, 不做双保险轮询)。

## 纪律

- record-only: 只写台账 + 发通知, **不触发任何交易动作**。
- 回调线程纪律: SDK recv 线程里只做落库 (本地 sqlite, ms 级); 飞书 HTTP 推送
  派生 daemon 线程, 不阻塞 recv。
- 异常全收口: 订阅器启动失败只 warning, 绝不阻断主服务启动; handler 内异常
  不上抛 (上抛会打断 SDK recv 循环)。
- 开关: `.env.us` FTSEEK_LIVE_DEAL_PUSH (默认 1=开; 0=完全跳过订阅, 行为
  等同 RFC-078 纯 cron 模式)。通知独立开关 FTSEEK_LIVE_DEAL_PUSH_NOTIFY (默认 1)。
  回滚 = 置 0 + 重启 (连接类变更无法热插拔)。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("ftseek_us.live_deal_push")

_instance: Optional["LiveDealPushSubscriber"] = None
_instance_lock = threading.Lock()


def push_enabled() -> bool:
    """`.env.us` FTSEEK_LIVE_DEAL_PUSH (默认 1). main.py 启动已 load_dotenv."""
    return os.getenv("FTSEEK_LIVE_DEAL_PUSH", "1").strip() != "0"


def notify_enabled() -> bool:
    return os.getenv("FTSEEK_LIVE_DEAL_PUSH_NOTIFY", "1").strip() != "0"


class LiveDealPushSubscriber:
    """常驻成交推送订阅器: US+HK 双 ctx, REAL-only, 幂等落库 + 可选飞书提醒."""

    def __init__(self):
        self.host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        self.port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        self._ctxs = []          # [(market, ctx)]
        self._started = False
        self.stats: Dict[str, int] = {"received": 0, "real_inserted": 0,
                                      "real_updated": 0, "sim_skipped": 0,
                                      "errors": 0}

    # ── handler ──────────────────────────────────────────────
    def _make_handler(self, ctx_market: str):
        from futu import RET_OK, TradeDealHandlerBase

        subscriber = self

        class _DealHandler(TradeDealHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret_code, ret_data = super().on_recv_rsp(rsp_pb)
                if ret_code != RET_OK or ret_data is None:
                    return ret_code, ret_data
                try:
                    subscriber._handle_push(ctx_market, ret_data)
                except Exception as exc:  # noqa: BLE001 — 不上抛, 上抛打断 SDK recv 循环
                    subscriber.stats["errors"] += 1
                    logger.warning(f"[实盘推送] handler 异常 (ctx={ctx_market}): {exc}")
                return ret_code, ret_data

        return _DealHandler()

    def _handle_push(self, ctx_market: str, df) -> None:
        """ret_data = DataFrame (TradeDealHandlerBase.unpack_rsp 实测, 通常单行可多行).

        每行独立 try/except: 一笔坏行/通知失败不得丢弃同一推送里的其余行
        (handler on_recv_rsp 的外层 try 只防 recv 循环被打断, 不防行间连带)。
        """
        for i in range(len(df)):
            try:
                row = df.iloc[i]
                raw = {k: row.get(k) for k in row.index}
                self.stats["received"] += 1
                env = str(raw.get("trd_env") or "").upper()
                if env != "REAL":
                    # sim playbook 同账户 SIMULATE 成交 — 实盘账本只收 REAL (要点1)
                    self.stats["sim_skipped"] += 1
                    continue
                from app.web_dashboard import live_ledger
                result = live_ledger.record_deal_push(raw)
                if result is None:
                    self.stats["errors"] += 1
                    continue
                outcome, deal_id = result
                if outcome == "inserted":
                    self.stats["real_inserted"] += 1
                    logger.info(
                        f"[实盘推送] ✅ 真钱成交落账 {raw.get('code')} "
                        f"{raw.get('trd_side')} qty={raw.get('qty')} @{raw.get('price')} "
                        f"deal_id={deal_id} (ctx={ctx_market})")
                    if notify_enabled():
                        try:
                            self._notify_async(raw, deal_id)
                        except Exception as exc:  # noqa: BLE001 — 通知旁路, 落账已完成
                            logger.warning(f"[实盘推送] 通知派发失败 (落账不受影响): {exc}")
                elif outcome == "updated":
                    # 双 ctx 双收 / sync 已先落 — 幂等去重生效, 不重复通知
                    self.stats["real_updated"] += 1
            except Exception as exc:  # noqa: BLE001 — 单行兜底, 续处理下一行
                self.stats["errors"] += 1
                logger.warning(f"[实盘推送] 处理成交行失败 (ctx={ctx_market}, row={i}): {exc}")

    # ── 飞书即时提醒 (daemon 线程, 不阻塞 SDK recv) ──────────────
    def _notify_async(self, raw: Dict[str, Any], deal_id: str) -> None:
        def _send():
            try:
                from app.notifier.feishu import send_text
                from app.web_dashboard.live_ledger import market_of, _s
                symbol = _s(raw, "code") or "?"
                market = market_of(_s(raw, "deal_market") or _s(raw, "trd_market"), symbol)
                cur = "HK$" if market == "HK" else "$"
                qty = raw.get("qty")
                price = raw.get("price")
                amount = f"{cur}{float(qty) * float(price):,.0f}" if qty and price else "?"
                side = str(raw.get("trd_side") or "?")
                side_cn = {"BUY": "买入", "SELL": "卖出", "SELL_SHORT": "卖空",
                           "BUY_BACK": "买回"}.get(side, side)
                t = str(raw.get("create_time") or "")[5:16]
                send_text(f"💰 实盘成交 {symbol} {side_cn} {qty}@{price} ({amount}) {t} [{market}·真钱]")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[实盘推送] 飞书通知失败 (落账不受影响): {exc}")
        threading.Thread(target=_send, name="LiveDealNotify", daemon=True).start()

    # ── 生命周期 ──────────────────────────────────────────────
    def start(self) -> bool:
        """建 US+HK 双常驻 ctx 并挂 handler. 任何失败只 warning, 返回 False."""
        if self._started:
            return True
        try:
            from futu import OpenSecTradeContext, TrdMarket
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[实盘推送] futu SDK 导入失败, 推送订阅跳过: {exc}")
            return False
        ok_any = False
        for market, trd in (("US", TrdMarket.US), ("HK", TrdMarket.HK)):
            try:
                ctx = OpenSecTradeContext(filter_trdmarket=trd, host=self.host,
                                          port=self.port, is_encrypt=False)
                ctx.set_handler(self._make_handler(market))
                self._ctxs.append((market, ctx))
                ok_any = True
                logger.info(f"[实盘推送] {market} 成交推送订阅已挂载 (被动接收, 无需解锁)")
            except Exception as exc:  # noqa: BLE001 — 单腿失败不拖累另一腿
                logger.warning(f"[实盘推送] {market} ctx 建立失败 (该腿无推送, 每日 sync 兜底): {exc}")
        self._started = ok_any
        return ok_any

    def stop(self) -> None:
        for market, ctx in self._ctxs:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001 — 清理路径不抛
                pass
        self._ctxs = []
        self._started = False


def start_live_deal_push() -> bool:
    """进程级单例启动入口 (scheduler_core 启动序列调用, 异常全收口)."""
    if not push_enabled():
        logger.info("[实盘推送] FTSEEK_LIVE_DEAL_PUSH=0, 跳过订阅 (纯 cron 回补模式)")
        return False
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = LiveDealPushSubscriber()
        return _instance.start()


def get_live_deal_push() -> Optional[LiveDealPushSubscriber]:
    return _instance
