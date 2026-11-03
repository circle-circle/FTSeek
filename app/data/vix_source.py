#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VIX 单一真源 (Batch 2026-09-14, 用户裁「全部处理」P0-1)
================================================================
背景 (为什么需要这个模块):
  盘前数据有效性审计实测出两处独立病灶, 根因都是"VIX 取值口径在多处各写一份":

  ① 执行侧 VIX 门的 fail-close 是纸面的 —
     app/analysis/market_state.py::_fetch_vix_data 全源失败时 `return 20.0`
     (不是 None), 于是 order_executor.py:3418 / playbook_service.py:932 的
     `if vix is None: 拒绝新仓` 分支**永不触发** (同型: 回撤熔断静默失效案)。
  ② 同一天的 VIX 门读两种口径 —
     regime 侧 (app/playbook/market_regime.py) 走 MarketDataRouter 的
     Futu VIXY×0.85 实时代理 (2026-08-03 修复), 而执行侧硬拒门走 FRED VIXCLS
     日频 (T-1/T-2, 实测 2026-09-14 序列最新仅到 09-10) — 盘中 VIX 冲高
     在执行门上不可见, 与 "VIX 新鲜度 fail-close" 的注释意图相悖。
  ③ VIXY→VIX 换算系数 0.85 此前复制在 4 处 (router / alphavantage_provider /
     vix_monitor / 本次新增), 任一漂移即口径分裂。
  ④ (2026-09-14 追加, 用户裁「修复再提交」)"**哪些市场阶段该走 VIXY 代理解析**"
     这条规则此前同样在 4 处各写一份, 且**分成了两派**:
        A 派 {PRE_MARKET, TRADING} — market_data_router / market_state (本批新增)
        B 派 {TRADING}           — alphavantage_provider / risk.vix_monitor
     后果 (2026-09-14 17:44 ET 实测, 同进程同一时刻): 执行门+regime 读
     **14.71** (VIXY 代理), 而 vix_monitor 写的 data/vix_cache/vix_data.json
     读 **17.84** (FRED T-1) → 看板 data_service / day_score / order.thesis /
     data_health_check 与执行门各看一个 VIX。修 ③ 时把系数收敛了, 却漏了这
     条同等重要的规则 ⇒ 本模块补上唯一判据 `is_vixy_proxy_window`。

本模块职责 (只做这三件事, 不持有缓存/不持连接):
  1. VIXY 价格 → VIX 估算 的唯一换算入口 (`vixy_to_vix`) — 消除系数漂移。
  2. Futu OpenD 直连取 VIXY 快照 (`fetch_vixy_via_opend`) — 供无 Router 的
     调用方 (如 MarketStateChecker 直连 OpenD 的既定模式) 复用同一链路。
  3. 代理窗口的唯一判据 (`is_vixy_proxy_window` + `VIXY_PROXY_PHASES`) —
     消除"哪些阶段走代理"的四处分派。

口径依据:
  - **VIX 现货无盘前交易** (CBOE 现金指数不撮合), 故盘前/盘中只能取代理值;
    可用代理 = VIXY (短期期货 ETF) 实时报价 × contango 校准系数 0.85。
  - **富途不供指数快照**: 实测 US.VIX / US.IXIC / US.DJI / US.SPX 全部
    `{"_error": "empty"}`, 而 US.VIXY / US.SPY / US.QQQ 正常
    (与 docs/debates/DEB-20260818-futu-capability-audit.md:89,144
    「US.VIX 直查不可用」一致) ⇒ 禁止再直查 US.VIX。

不做的事 (刻意):
  - 不在这里做缓存/TTL — 各调用方缓存语义不同 (router 无缓存 / AV 60-600s /
    vix_monitor 磁盘 180s / market_state 30min), 强行统一会改变既有行为与
    测试面; 本次只统一"换算与取数"这一层。
  - 不改变任何调用方的默认/兜底策略 — 兜底由调用方按各自语义声明
    (vix_below_25 显式 fail-open / get_vix_regime 显式 20.0 / fail-close 侧显式 None)。

回滚: 删本文件 + 把 VIXY_CONTANGO_FACTOR 引用还原为字面量 0.85。
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# VIXY (iPath VIX Short-Term Futures ETF) → VIX spot 的 contango 校准系数。
# 正常 contango 下 VIXY ≈ VIX × 1.15~1.20; 反推取 0.85 (与 2026-06-21 起的
# router / alphavantage_provider / vix_monitor 三处现值逐字一致, 本次为收敛而非变更)。
# ⚠️ backwardation 时该系数偏差会变大 (期货贴水), 属已知代理误差, 非本模块可解。
VIXY_CONTANGO_FACTOR = 0.85


def vixy_to_vix(vixy_price: Optional[float]) -> Optional[float]:
    """VIXY 价格 → VIX 估算值 (系数唯一入口). 无效输入返回 None, 绝不兜底。"""
    try:
        _p = float(vixy_price) if vixy_price is not None else 0.0
    except (TypeError, ValueError):
        return None
    if _p <= 0:
        return None
    return round(_p * VIXY_CONTANGO_FACTOR, 2)


# VIXY 实时代理**有效窗口**的唯一判据 (2026-09-14, 用户裁「修复再提交」)。
#
# 依据 (仓内既有实测与事故记录, 非偏好):
#   - 2026-08-03 regime 精准性验证 (market_data_router.py 注释 + obsidian
#     2026-08-03_regime_validation): 盘前用 FRED VIXCLS 日频 → FRED 更新滞后
#     2-3 天, 7/31 盘前生成的 23 条指令**全用 7/29 的 20.66**, 与 SPY 实际波动
#     12.4% 脱节, **污染 regime**。当事修复即"盘前/交易时段优先取 VIXY 实时代理"。
#   - 事实基础: VIX 现货 (CBOE 现金指数) **盘前不撮合**, 而 VIXY 是 ETF、有盘前
#     成交 (实测 04:00 ET 起有报价) ⇒ 盘前唯一**可用且新鲜**的 VIX 值就是代理值。
#
# 为何**不含** AFTER_HOURS / OVERNIGHT / WEEKEND: 盘后 VIXY 报价稀疏易失真, 且
#   此刻 FRED 的 T-1 收盘是**只欠几小时的真值** ⇒ 维持 FRED 优先 (与 router 原
#   `OVERNIGHT/WEEKEND 用 FRED 日频可接受` 注释一致)。改这条边界属消费者可见的
#   行为变更, 须另开辩论。
VIXY_PROXY_PHASES = ("PRE_MARKET", "TRADING")

# 盘中阶段 (2026-11-03, 全链审计 P0-2 续) —— 判据同样收敛到本模块
TRADING_PHASES = ("TRADING",)


def is_trading_phase(phase=None) -> bool:
    """当前 (或给定) 阶段是否 TRADING —— 单一真源入口 (2026-11-03 P0-2 续)。

    为什么需要: `app/analysis/market_state.py` 的 VIX 缓存 TTL 要按"盘中/盘外"分档
    (盘中 60s 才能捕捉 VIX 急变; 盘外 30min 少打 API), 但生产者**不得**在体内直接
    比较 `MarketPhase.TRADING` —— `tests/test_vix_proxy_window_single_source.py` 的
    AST 契约要求阶段判据一律走本模块。故判据落在这里, 生产者只调不写。

    fail-closed 方向: 取不到 phase ⇒ 回落 False (用盘外的 30min TTL) ——
    宁可多缓存一会儿, 也不要在"时钟坏了"时把取数频率抬到每分钟。
    """
    if phase is None:
        try:
            from app.core.scheduler_triggers import get_market_phase
            phase = get_market_phase()
        except Exception as _e:
            logger.warning("[vix_source] 市场阶段判定失败 → 盘中判定回落 False (用盘外 TTL): %s",
                           _e, exc_info=True)
            return False
    return getattr(phase, "name", str(phase)) in TRADING_PHASES


def is_vixy_proxy_window(phase=None) -> bool:
    """当前 (或给定) 市场阶段是否处于 **VIXY 实时代理优先** 窗口 — 唯一判据入口。

    修前这条规则散在 4 个 VIX 生产者里且分成两派 (见模块 docstring ④):
    `market_data_router` / `market_state` 认 {PRE_MARKET, TRADING}, 而
    `alphavantage_provider` / `risk.vix_monitor` 只认 {TRADING} ⇒ 同一进程盘前
    会同时存在两个 VIX (2026-09-14 实测 14.71 vs 17.84)。方向按 2026-08-03
    事故证据统一为 A 派 (盘前也走代理)。

    本函数只决定**尝试顺序**, 不取数、不兜底: 代理腿取不到时各调用方仍会各自
    回落 FRED/Old 链。

    fail-open 方向 (取不到 phase ⇒ 返回 True): 只影响尝试顺序, 而代理失败必然
    继续回落 ⇒ **无害**; 反之 fail-closed 到 False 会在"时钟坏了"时静默退回
    T-1 陈旧值 —— 本模块立项要消灭的正是这种静默 (2026-08-03 事故同型)。
    """
    if phase is None:
        try:
            from app.core.scheduler_triggers import get_market_phase
            phase = get_market_phase()
        except Exception as _e:
            logger.warning("[vix_source] 市场阶段判定失败 → 代理窗口 fail-open 为 True: %s",
                           _e, exc_info=True)
            return True
    return getattr(phase, "name", str(phase)) in VIXY_PROXY_PHASES


def fetch_vixy_via_opend(host: Optional[str] = None,
                         port: Optional[int] = None,
                         timeout: int = 8) -> Optional[float]:
    """直连 Futu OpenD 取 US.VIXY 快照 → VIX 估算; 任何失败返回 None (不兜底)。

    用途: 供已按"直连 OpenD"模式实现的调用方 (MarketStateChecker) 复用同一
    链路, 而不必新建 MarketDataRouter (避免重复连接冲突, 见其 _fetch_spy_data 注释)。

    时点语义 (实测 2026-09-14):
      - 盘中: VIXY 实时报价 → 当日有效值 (与 regime 侧同源);
      - 盘前: 盘前报价有效 (VIXY 是 ETF, 有盘前成交);
      - 盘后/周末: 快照返回上一交易日收盘 (与 FRED 日频同日), 无害。
    """
    _host = host or os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    try:
        _port = int(port if port is not None else os.getenv("FUTU_OPEND_PORT", "11111"))
    except (TypeError, ValueError):
        _port = 11111
    try:
        from futu import OpenQuoteContext
        ctx = OpenQuoteContext(host=_host, port=_port)
        try:
            ret, snap = ctx.get_market_snapshot(["US.VIXY"])
            if ret != 0 or snap is None or len(snap) == 0:
                logger.debug("[vix_source] VIXY 快照失败: ret=%s", ret)
                return None
            vix = vixy_to_vix(snap.iloc[0].get("last_price", 0))
            if vix is None:
                return None
            logger.info("[vix_source] VIX (Futu VIXY 实时): VIXY=$%.2f → VIX≈%.2f",
                        float(snap.iloc[0].get("last_price", 0) or 0), vix)
            return vix
        finally:
            try:
                ctx.close()
            except Exception as _ce:
                # 不留静默吞错 (except:pass 与 W4 debug-swallow 两道棘轮都禁):
                #   连接关闭失败属真异常, 必须可见且带栈。
                logger.warning("[vix_source] Futu context 关闭失败: %s", _ce, exc_info=True)
    except Exception as e:
        # ⚠️ 全链路失败**必须可见** — 本模块的立项理由就是"VIX 缺失长期不可见"
        #   (DA 侧结构性恒缺 + 执行门 fail-close 纸面化), 故这里不能用 debug 藏起来。
        #   调用方 (MarketStateChecker/VixGate) 会据此降级到 FRED 日频 —— 那是**口径
        #   退化** (T-1), 值班人必须能从日志看出"今天 VIXY 代理没取到"。
        logger.warning("[vix_source] VIXY 直连取数失败 (调用方将降级到日频口径): %s",
                       e, exc_info=True)
        return None