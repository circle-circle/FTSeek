#!/usr/bin/env python3
"""交易日检查脚本 — 被 cron 任务的 Step0 与监控脚本的守卫调用。

用法:
  python scripts/trading_day_check.py              # 检查美股 (默认)
  python scripts/trading_day_check.py --market US  # 显式指定美股

输出契约 (2026-09-07 修订):
  stdout:    "TRADING_DAY" 或 "NON_TRADING_DAY" (逐字不变, 兼容按 stdout 判断的调用方)
  exit code: **0 = 交易日 / 1 = 非交易日**
  ⚠️ 不确定一律 fail-open ⇒ stdout `TRADING_DAY` + exit 0。**exit 1 只在确定是非交易日时
     出现**, 因此调用方可安全把「非 0」当「非交易日」, 不会把依赖故障误读成假日。

为什么改 exit code (2026-09-07 根因核查, 全仓 grep 实证)
------------------------------------------------------
旧 docstring 写的是「exit code 始终 0, 调用方按 stdout 内容判断, 不按 exit code」,
但实测**全仓 9 个消费方无一按 stdout 判断, 全部读 exit code**:
  - 4 个 hermes cron prompt 的 Step 0 (`echo "EXIT=$?"` + 「如果 EXIT=1(非交易日)
    回复 [SILENT] 并停止」): `7a40dd7c9e94` 盘后综合复盘 / `2d69370d5c91` 盘前综合预检 /
    `9ac3efd8c5e7` Deepulse 科技情报日报 / `3a6c83bbd84c` deep-analysis 盘后复盘+记忆更新
  - `scripts/da_supply_reconcile_check.py:99`        `return r.returncode == 0`
  - `scripts/premarket_reprice_shadow.py:73`         `return r.returncode == 0`
  - `scripts/business_heartbeat.py:95`               `if r.returncode != 0: return 0`
  - `ops/hermes/intraday_diagnose.py:45`             `if r.returncode != 0: sys.exit(0)`
  - `ops/hermes/intraday_diagnose_late.py:53`        `if result.returncode != 0: is_trading = False`
⇒ exit code 恒 0 使**这 9 处的假日分支全是死代码**, 全系统交易日守卫实际退化为「只排周末」
  (而 cron 表达式本就排周末 ⇒ 等于零守卫): 美国假日照跑盘后复盘/盘前预检/盘中诊断/
  shadow 记账, 并向飞书推送基于陈旧数据的报告。改这 1 个文件即同时修好 9 个消费方,
  优于逐个手改 4 段 prompt 文本 + 5 处代码 (prompt 改动面大、易错、无测试保护)。
两条 intraday 脚本已同批改为**按 stdout 判定**(语义更严格: 子进程崩溃/超时时 fail-open
  照报而非静默), 与本文件新的 exit code 语义在正向路径上结论一致。

stdout 洁净度 (2026-09-07 追加修正, DEB-20260907 §8.2 #7)
--------------------------------------------------------
`is_trading_day()` 的权威层会连 Futu OpenD, 而 futu SDK 的 `FTLog` 单例在**首次构造时**
把 console handler 绑到当时的 `sys.stdout` (`futu/common/ft_logger.py:102`
`logging.StreamHandler(sys.stdout)`), 于是 `_init_connect_sync` / `on_disconnect` 两行
连接日志混进本脚本 stdout ⇒ 按 stdout 逐字比对的消费方读到的是三行噪声而不是一个 token
(`tests/test_trading_day_check_contract.py::test_stdout_token_and_exit_code_agree` 因此红)。
治根 = **把 SDK 日志引到 stderr, 而不是放宽测试**:
  ① 计算判定期间把 `sys.stdout` 指向 `sys.stderr` ⇒ 此后构造的 handler 天然绑 stderr;
  ② 再把已绑到真 stdout 的 FT*Log handler 逐个改流 (防 SDK 早于本函数构造单例);
  ③ 最后只在**入口处取得的那个真 stdout** 上打那一个 token (不做模块级快照, 否则
     会绕过调用方的 `redirect_stdout` 替身)。
`on_disconnect` 是异步/延迟发出的 (实测落在 token 之后), 但 handler 绑的是 stderr **对象**,
故 restore `sys.stdout` 之后它仍然走 stderr — 这也是不能只靠"临时 swap"的原因。
token 逐字与 exit code 语义均**不变** (他批 `bace63a6` 的字节形态纪律仍然成立):
改变的只有"噪声去哪"。

判定真源 (2026-09-07 同批修正)
------------------------------
旧实现直接读 `app.utils.holiday_calendar.US_HOLIDAYS` **常量**做集合判断, 绕过了同模块
`is_trading_day()` 的 Futu 权威层 ⇒ 临时全国哀悼日那类只有交易所知道的休市判不出来,
且会继承离线表在 2026-12-24 上的保守误标(该日 NYSE 提前收盘 13:00 ET, 是交易日)。
现统一走 SSOT 方法 `get_holiday_calendar().is_trading_day("US", d)`
(Futu API 权威 → 2026-2028 硬编码降级 → 仅周末兜底), 与 UE `llm_universe_scan.py:152` /
FTSeek `app/playbook/pipeline.py:523` / DA skill `trading_calendar.py` L1 同源。
港股维持原简化口径(仅周末) — 本仓无港股假日表, 不假装有。
"""
import sys
import os

# 确保能 import app 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _today(market: str):
    """取「当日」日期 (US→ET, HK→HKT); 取不到 → None 由调用方兜底。"""
    try:
        if market == "HK":
            from datetime import datetime, timezone, timedelta
            return datetime.now(timezone(timedelta(hours=8))).date()
        from app.utils.timezone import now_et
        return now_et().date()
    except Exception:
        return None


def _is_trading(market: str) -> bool:
    d = _today(market)
    if d is None:
        # 连当日日期都取不到 → 本地日期兜底 (原实现的最终兜底口径)
        from datetime import date
        d = date.today()

    if d.weekday() >= 5:
        # 周末: 零依赖即可判定, 优先于一切 (SSOT 故障时这条仍然成立)
        return False

    if market == "HK":
        return True                      # 本仓无港股假日表, 不假装有

    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return bool(get_holiday_calendar().is_trading_day("US", d))
    except Exception:
        # SSOT 不可用 → fail-open: 宁可假日白跑一轮, 不可让 9 个消费方在依赖故障日集体静默
        return True


def _repoint_sdk_log_handlers(real_stdout) -> None:
    """把 futu SDK 的 FT*Log console handler 从真 stdout 改流到 stderr。

    handler 在**构造那一刻**绑定 stream 对象, 故仅靠临时替换 `sys.stdout` 只能覆盖
    "构造发生在替换期间"的情形; 若单例已被更早的 import 构造出来, 就必须显式改流。
    `real_stdout` 由 main() 在**入口时**取 (不是模块级常量) — 模块级快照会把 token
    打到进程原始 stdout 上, 绕过调用方的 `contextlib.redirect_stdout` 替身, 消费方
    (含本仓契约测试) 就会读到空串。
    改流失败**必须留痕**(R5 纪律 / except:pass 棘轮): 否则"stdout 干净"与"治理根本没跑"
    不可区分。判定本身绝不能因日志治理而失败 ⇒ 只 warn 到 stderr, 不抛。
    """
    import logging
    for name in ("FTConsoleLog", "FTFileLog", "FT"):
        for h in list(logging.getLogger(name).handlers):
            try:
                if getattr(h, "stream", None) in (real_stdout, sys.__stdout__):
                    h.setStream(sys.stderr)
            except Exception as e:
                print(f"[trading_day_check] SDK 日志改流失败 logger={name} "
                      f"{type(e).__name__}: {e}", file=sys.stderr)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="US trading day check")
    parser.add_argument("--market", default="US", help="Market (US/HK, default=US)")
    args = parser.parse_args()

    market = (args.market or "US").upper()
    real_stdout = sys.stdout             # 入口时的真 stdout (cron 管道 / 测试替身)
    # 判定期间 stdout 让位给 stderr: 此后 futu SDK 构造的 console handler 绑的就是 stderr
    sys.stdout = sys.stderr
    try:
        _repoint_sdk_log_handlers(real_stdout)   # 覆盖"单例早于本函数已构造"的情形
        try:
            is_trading = _is_trading(market)
        except Exception:
            is_trading = True            # 兜底 fail-open (与上方 except 同口径)
    finally:
        sys.stdout = real_stdout
        _repoint_sdk_log_handlers(real_stdout)   # 覆盖"判定过程中才构造单例"的情形

    print("TRADING_DAY" if is_trading else "NON_TRADING_DAY")
    return 0 if is_trading else 1


if __name__ == "__main__":
    sys.exit(main())
