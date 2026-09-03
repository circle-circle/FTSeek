#!/usr/bin/env python3
"""每日测量不变量自检 (2026-07-28, 用户拷问"为什么修尺子问题都是我发现"的制度性回答)。

原理: 本周修掉的每个尺子问题事后看都违反某个跨字段物理不变量 (盈亏≠价差 /
bar峰<成交价 / 负MFE / 时间乱序 / 审计桶加总≠skips), 但此前无人每天检查 —
用户看"钱对不对"这个终极不变量所以总是先发现。本脚本把人肉对账变成机器每晚对账。

检查面 (近 7 天已平仓, 对齐判分窗):
  I1 realized_pnl ≈ (exit-entry)*qty*sign     I5 mfe/mae ≥ 0
     (+价格/数量 ≤0 即数据腐败单独告警)
  I2 pnl_pct 符号 = pnl 符号                   I6 bar 峰谷物理性 (LONG bar_peak ≥ 0.995*max(e,x);
                                                SHORT 镜像 bar_peak ≤ 1.005*min(e,x))
  I3 peak/trough 最有利/最不利语义 (±0.5%)   I7 exit_time > entry_time
  I4 mfe 与 peak 重算一致 (±0.05pp)            F  字段健康度 (关键测量字段非空率骤降/常量化)
  I8 pnl_net = realized_pnl - fees_est (±0.02, 2026-07-30 费用净额批)
  I9 conf 量纲 (成交侧 0-1 / 指令侧 0-100)
  I10 已了结行必须有 exit_time (出场日口径前提, 2026-08-28)
  ── 以下覆盖全部 source_type 的了结行 (2026-08-28 D4, "字段级消失"族) ──
  I11 了结行 realized_pnl 不得为 NULL (否则从净额/胜率两处聚合同时消失)
  I12 realized_pnl=0 而 价差×数量 明显非零 → 占位符伪装盈亏 (8/28 占位链常驻守卫)
  I13 费用上线日后缺 fees_est/pnl_net → COALESCE 静默回落毛口径
  I14 了结行缺 exit_reason (空串或 NULL) → 出场归因分类丢行
  I15 指令 decision_id 决策日 == 指令交易日 (T-1 bridge 陈旧供单守卫, 2026-09-02)
  W1 参数冻结窗 config_version 唯一性 (OKR-2026-09 KR2, 2026-08-31 用户裁"按建议执行")
  豁免: data_quality 前缀命中 duplicate/outlier/invalid/void/excluded 的行 (写入方
        声明"非真实了结", 如 id=1327 duplicate_external_close 故意留 NULL 使净额求和
        自动排除) 不告; 数量落 JSON (settled_exempt_marked) 防豁免面无声扩大。
  自检的自检: I9镜像/I10/I11~I14 段内部异常不再 `except: pass` — 会产出
        *_detector_error 违反 (2026-08-28 D1/D2 同课: "零违反"与"守卫自己崩了"必须可区分,
        否则一个坏掉的看门狗就是最会撒谎的看门狗)。表/列不存在属旧库形态, 仍静默跳过。

输出: 违反数 >0 → logger.error (被 error_alert.sh 15min 扫描拾取推钉钉) + exit 1;
     全过 → 一行 OK + exit 0。结果落 data/invariant_check_latest.json。
cron: 每交易日盘后 (见 crontab '05 5 * * 2-6')。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.utils.timezone import now_et  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("invariant_check")

# I15 口径起点 (2026-09-02, DEB-20260902-stale-bridge): 陈旧 did 守卫的 go-forward 起点
#   = P1-①/P1-② 修复部署日。9/1 的 5 行 (BABA/BWXT/EIX/LLY/META) 是立档证据, 已裁决留痕,
#   历史行不自愈 → 纳入窗口会连告 7 天 (不可动作噪声)。改本常量 = 改判据, 须先改 DEB。
_I15_SINCE = "2026-09-02"


def _resolve_db() -> Path:
    """权威库必须按 TRADE_ENV 解析 — 硬编码文件名会在切实盘那一刻静默变成
    "监控在读陈旧同名单" (与 2026-08-28 发现的 ftseek_us_live.db 死库同族)。"""
    for mod, fn in (("app.core.paths", "get_trade_records_db"),
                    ("app.core.db_utils", "get_main_db")):
        try:
            import importlib
            return Path(str(getattr(importlib.import_module(mod), fn)()))
        except Exception:
            continue
    return REPO / "data" / "ftseek_us.db"


DB = _resolve_db()
OUT = REPO / "data" / "invariant_check_latest.json"
# 净额三字段 (fees_est/pnl_net) 上线日 — 此日后了结行缺费用/净额=口径静默回落毛额
_FEE_ROLLOUT_DATE = "2026-07-30"
# 已知"非真实了结"标记词表 (2026-08-28 实测全库 data_quality 分布:
#   real 457 / clean 265 / outlier_external 1 / manual 1 / duplicate_external_close 1 /
#   direction_mismatch 1) — 只有前缀命中本表才豁免, 'real'/'clean'/'manual' 一律照判。
_DQ_NEUTRAL_PREFIXES = ("duplicate", "outlier", "invalid", "void", "excluded")


# ── W1 参数冻结窗守卫 (2026-08-31): KR2 判分公式原为周六周检人工 SQL — 换版只能等
#    月底结算才作废整月; 现每晚机器对账, 版本分裂当天告警 (损失封顶当天)。
#    豁免通道 (KR2 例外): P0 bug + DEB 落档 + 用户知会 → 人工把新版本加入
#    data/config_freeze_baseline.json 的 versions 并附 deb_ref, 本检查即放行。
#    两维口径 (2026-09-03 复盘修复, REVIEW-20260903 §四.1): 名字 config_version 在本仓有
#    两个语义 — ①trade_records.config_version **列** = 配置档案版本 (现恒 v2);
#    ②metadata_json.config_version = **退出参数哈希** (exit-*, app/playbook/config.py:188)。
#    KR2 判的是 ②, 而本守卫原先只查 ① → 8/24 起退出参数换 4 版此处恒 "ok" (读错列 =
#    结构性失明; 只有周六周检 ops/hermes/ftseek_weekly_checkpoint.py 读对)。
#    OKR-2026-09 KR2 判分 SQL 同病, 已同步勘误。①维保留 (档案版本分裂同样有意义)。
_FREEZE_WINDOW_START = "2026-08-24"  # OKR-2026-09 KR2 判分窗口起点 (判据冻结, 改须先改 OKR)
_FREEZE_BASELINE = REPO / "data" / "config_freeze_baseline.json"


def _check_config_freeze(rec) -> dict:
    """W1: 冻结窗内 PLAYBOOK_SIM 参数版本唯一性 (档案列 + 退出哈希两维)。只读; 异常显性化。

    输出: versions/profile_status (档案列维) + exit_versions/exit_hash_missing_rows/
    exit_status (退出哈希维=KR2 真口径, 每哈希笔数即 cohort 样本量) + status (两维取劣)。
    """
    info = {"window_start": _FREEZE_WINDOW_START}
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            pairs = conn.execute(
                "SELECT config_version, COUNT(*) FROM trade_records "
                "WHERE trade_date >= ? AND source_type='PLAYBOOK_SIM' GROUP BY 1",
                (_FREEZE_WINDOW_START,)).fetchall()
            exit_pairs = conn.execute(
                "SELECT COALESCE(json_extract(metadata_json,'$.config_version'),''), COUNT(*) "
                "FROM trade_records WHERE trade_date >= ? AND source_type='PLAYBOOK_SIM' "
                "GROUP BY 1", (_FREEZE_WINDOW_START,)).fetchall()
        finally:
            conn.close()
        versions = {str(v): n for v, n in pairs}
        # 无哈希行 (旧行/写入侧未落 metadata) 单列计数, 不当作版本 — 否则首跑即假漂移
        exit_versions = {str(v): n for v, n in exit_pairs if str(v).startswith("exit-")}
        info["exit_hash_missing_rows"] = sum(
            n for v, n in exit_pairs if not str(v).startswith("exit-"))
        info["versions"] = versions
        info["exit_versions"] = exit_versions
        if not versions:
            info["status"] = "no_rows_in_window"
            return info
        if _FREEZE_BASELINE.exists():
            baseline = json.loads(_FREEZE_BASELINE.read_text(encoding="utf-8"))
        else:
            # 首跑自建基线: 当下唯一版本即冻结基线 (诚实声明: 基线日之后才可判漂移)
            baseline = {"versions": sorted(versions),
                        "established_at": now_et().isoformat(timespec="seconds"),
                        "note": "OKR-2026-09 KR2 冻结基线; 豁免=本文件 versions 追加新版本+deb_ref"}
            _FREEZE_BASELINE.write_text(json.dumps(baseline, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
            info["status"] = "baseline_established"
            return info
        extra = sorted(set(versions) - set(baseline.get("versions", [])))
        if extra:
            for v in extra:
                rec("W1_config_version_drift", "-", v,
                    f"冻结窗版本分裂: 基线={baseline.get('versions')} 新增={extra}; "
                    f"若属批准的 P0 修复, 更新 {_FREEZE_BASELINE.name} 并补 DEB 落档 (KR2 例外通道)")
            info["profile_status"] = "drift"
        else:
            info["profile_status"] = "ok"
        # 退出哈希维 (KR2 真口径): 豁免清单 = baseline.exit_versions 键集 (与周六周检同源;
        # 键集被 tests/test_kr2_freeze_exemption.py 钉死 — 新豁免必须改测试 = 强制留痕)
        exempt = set((baseline.get("exit_versions") or {}).keys())
        extra_exit = sorted(set(exit_versions) - exempt)
        # 反向可见性 (信息级, 不告警): 豁免在册而窗内零笔 = 幽灵版本 (如 exit-d1120a105711,
        # 登记于 9/1 但从未出现在任何成交行)。不告警是因为"先登记后上线"是合法顺序
        # (exit-de49b1299290 于 9/3 登记, 当晚才首笔) — 同台账"幽灵环路"只作人工审计面。
        info["exit_exempt_unobserved"] = sorted(exempt - set(exit_versions))
        if extra_exit:
            for v in extra_exit:
                rec("W1_exit_version_drift", "-", v,
                    f"退出参数哈希未在豁免清单 (窗内 {exit_versions[v]} 笔): 冻结期换版必须"
                    f"先 DEB 落档 + 用户裁, 再登记 {_FREEZE_BASELINE.name} 的 exit_versions; "
                    f"已豁免={sorted(exempt)} — 未裁先换 = KR2 破 (OKR-2026-09 结算判据)")
            info["exit_status"] = "drift"
        else:
            info["exit_status"] = "ok"
        info["status"] = ("drift" if "drift" in (info["profile_status"], info["exit_status"])
                          else "ok")
        return info
    except Exception as e:
        if _is_structural_error(e):
            info["status"] = "skipped_old_schema"
            return info
        rec("W1_detector_error", "-", type(e).__name__, f"W1 检测器自身异常: {e}")
        info["status"] = "detector_error"
        return info


def _col(row, name, default=None):
    """ sqlite3.Row 取不存在的列会 IndexError — 旧库/测试夹具列集小于生产库, 缺列按 default 处理。"""
    try:
        v = row[name]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _is_structural_error(exc: Exception) -> bool:
    """表/列不存在 = 库形态差异 (可静默跳过); 其余异常 = 检测器自身故障, 必须显性化。"""
    m = str(exc).lower()
    return "no such table" in m or "no such column" in m


def _i7_time_violated(entry_s, exit_s) -> bool:
    """I7 时间序判据 (2026-08-27 收盘审计③ 修复): 旧实现用字符串比较混合时区时间戳 —
    id=1320 假阳性 ('...T10:14...-04:00' < '...T21:47...+08:00' 按字典序成立, 绝对时间
    出场在入场 27 分钟后); 且 DST 边界日 -04/-05 混排可反向掩盖真违反 (假阴性)。
    改 fromisoformat 解析后按绝对时间比较; naive = 服务器本地挂钟 (历史 naive 均为
    CST 写入); 不可解析退回旧字符串比较 (保守多报不漏报)。"""
    from datetime import datetime

    def _p(s):
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.astimezone()  # naive → 系统本地时区
    try:
        return _p(exit_s) <= _p(entry_s)
    except (ValueError, TypeError):
        return str(exit_s) <= str(entry_s)


def main() -> int:
    since = (now_et().date() - timedelta(days=7)).isoformat()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM trade_records WHERE trade_date>=? AND source_type LIKE 'PLAYBOOK%' "
        "AND exit_time IS NOT NULL", (since,)).fetchall()
    c.close()

    viol: Counter = Counter()
    examples: dict = {}

    def rec(k: str, rid, sym, msg: str):
        viol[k] += 1
        examples.setdefault(k, f"id={rid} {sym} {msg}")

    for r in rows:
        m = json.loads(r["metadata_json"] or "{}")
        e = float(r["entry_price"] or 0)
        x = float(r["exit_price"] or 0)
        q = float(r["quantity"] or 0)
        pnl = float(r["realized_pnl"] or 0)
        pct, mfe, mae = r["pnl_pct"], r["mfe_pct"], r["mae_pct"]
        pk, tr = m.get("peak_price"), m.get("trough_price")
        short = str(r["direction"]).upper() in ("SHORT", "SELL_SHORT")
        sign = -1 if short else 1
        sym = str(r["symbol"])[3:]
        if e <= 0 or x <= 0 or q <= 0:
            rec("I1_price_qty_invalid", r["id"], sym, f"e={e} x={x} q={q}")
        elif abs(pnl - (x - e) * q * sign) > max(0.02, abs(pnl) * 0.02):
            rec("I1_pnl_vs_prices", r["id"], sym, f"pnl={pnl} calc={(x-e)*q*sign:.2f}")
        if pct is not None and pnl != 0 and (float(pct) > 0) != (pnl > 0):
            rec("I2_pct_sign", r["id"], sym, f"pnl={pnl} pct={pct}")
        if pk:
            pkf = float(pk)
            # 容差对齐 I6 (0.5%, 2026-08-01 用户裁): quote 采样在退出瞬间可滞后数 tick
            # (id=1189 PDD exit 88.37 vs quote-peak 88.36, bar 版 88.4 证实物理合法)
            if not short and pkf < max(e, x) * 0.995:
                rec("I3_peak_semantics", r["id"], sym, f"peak={pkf} e={e} x={x}")
            if short and pkf > min(e, x) * 1.005:
                rec("I3_peak_semantics", r["id"], sym, f"SHORT peak={pkf} e={e} x={x}")
        if pk and e > 0 and mfe is not None:
            calc = max(0.0, (float(pk) - e) * sign / e * 100)
            if abs(calc - float(mfe)) > 0.05:
                rec("I4_mfe_recalc", r["id"], sym, f"mfe={mfe} recalc={calc:.3f}")
        if (mfe is not None and float(mfe) < -1e-6) or (mae is not None and float(mae) < -1e-6):
            rec("I5_neg_mfe_mae", r["id"], sym, f"mfe={mfe} mae={mae}")
        pkb = m.get("peak_price_bar")
        if pkb and not short and float(pkb) < max(e, x) * 0.995:
            rec("I6_bar_peak_impossible", r["id"], sym, f"bar_peak={pkb} e={e} x={x}")
        # SHORT 镜像: peak_price_bar = min(lows) (pipeline.py:4933), 物理上 ≤ min(e,x)
        if pkb and short and float(pkb) > min(e, x) * 1.005:
            rec("I6_bar_peak_impossible", r["id"], sym, f"SHORT bar_peak={pkb} e={e} x={x}")
        if r["entry_time"] and r["exit_time"] and _i7_time_violated(r["entry_time"], r["exit_time"]):
            rec("I7_time_order", r["id"], sym, f"{r['entry_time']} → {r['exit_time']}")
        # I8 (2026-07-30 DEB-fee-net-pnl): 净额三字段自洽 — 毛−费=净 (列可空, 有值才查)
        try:
            fees, net = r["fees_est"], r["pnl_net"]
        except (IndexError, KeyError):
            fees = net = None  # 旧库无列
        if fees is not None and net is not None and abs(pnl - float(fees) - float(net)) > 0.02:
            rec("I8_fee_net_identity", r["id"], sym,
                f"pnl={pnl} fees={fees} net={net} (毛−费≠净)")
        # I9 (2026-08-01, P0.2): conf 量纲守卫 — 成交侧必须 0-1 (playbook_service.py:1233 /100),
        #   越界=写入链退化或新旧量纲混入
        try:
            sc = r["signal_confidence"]
        except (IndexError, KeyError):
            sc = None
        if sc is not None and not (0.0 <= float(sc) <= 1.0):
            rec("I9_conf_unit_out_of_range", r["id"], sym, f"signal_confidence={sc} (应 0-1)")

    # I9 镜像: 指令侧 confidence 必须 0-100 (两量纲并存系设计, 越界即事故)
    try:
        _c9 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        _bad = _c9.execute(
            "SELECT COUNT(*) FROM playbook_instructions WHERE date>=? "
            "AND confidence IS NOT NULL AND (confidence < 0 OR confidence > 100)",
            (since,)).fetchone()[0]
        _c9.close()
        if _bad:
            rec("I9_conf_unit_out_of_range", "-", "instructions", f"{_bad} 条 confidence 越界 [0,100]")
    except Exception as _e9:
        # 表不存在 = 夹具/旧库形态可静默; 其余异常必须显性化, 否则"查询坏了"读成"零违反"
        if not _is_structural_error(_e9):
            rec("I9_mirror_detector_error", "-", "instructions",
                f"{type(_e9).__name__}: {_e9} → 指令侧 conf 守卫失明")

    # I15 (2026-09-02, DEB-20260902-stale-bridge): 指令 did 的决策日必须等于指令交易日 —
    #   服务在 ET 日界前启动时内存载 T-1 bridge, 当日 bridge 落盘后 P0-2 守卫不重载
    #   ("已是 Bridge 就不重载"), 生成批于是用昨日决策号覆写/新增当日指令。9/1 实锤
    #   5 行 (BABA/BWXT/EIX/LLY/META, did=DA-202608311* 落 date=2026-09-01), 其中 4 行
    #   成交 = 当日净利 72%, 且 BABA/BWXT 当日 DA 门已 BLOCK、EIX 当日 DA 为 SKIP。
    #   历史面: 8/1 起 274 条 DA- 指令仅这 5 行越界 → 本尺子零误报史。
    #   修复面在 pipeline._reload_bridge_if_landed (P1-①) + db.save_instruction 仲裁
    #   (P1-②); 本项是次日晨兜底 (盘后 05:05 即可见, 不依赖人工复盘发现)。
    #   口径起点 _I15_SINCE = 修复部署日: 9/1 那 5 行是已裁决留档的立档证据
    #   (DEB-20260902-stale-bridge §二), 历史行不自愈 → 不设起点会连告 7 天变成
    #   不可动作的噪声 (告警必须可动作)。本尺子只守 go-forward。
    try:
        _c15 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        _c15.row_factory = sqlite3.Row
        _rows15 = _c15.execute(
            "SELECT id, symbol, date, decision_id, executed, outcome, pnl "
            "FROM playbook_instructions WHERE date >= ? AND date >= ? "
            "AND decision_id LIKE 'DA-%' ORDER BY id",
            (since, _I15_SINCE)).fetchall()
        _c15.close()
        for _r15 in _rows15:
            _did15 = str(_r15["decision_id"] or "")
            if not re.match(r"^DA-\d{8}", _did15):
                continue  # 非标准 DA 决策号 (不可判日期) → 不告, 避免尺子自身造假阳
            _did_d = f"{_did15[3:7]}-{_did15[7:9]}-{_did15[9:11]}"
            if _did_d != str(_r15["date"]):
                rec("I15_stale_decision_id", _r15["id"], str(_r15["symbol"])[3:],
                    f"did 决策日={_did_d} ≠ 指令日={_r15['date']} "
                    f"(executed={_r15['executed']} outcome={_r15['outcome']} "
                    f"pnl={_r15['pnl']}) → T-1 决策供单")
    except Exception as _e15:
        if not _is_structural_error(_e15):
            rec("I15_detector_error", "-", "instructions",
                f"{type(_e15).__name__}: {_e15} → 陈旧did守卫失明")

    # I10 (2026-08-28): 已了结行必须有 exit_time — 全仓"当日"口径自本日起一律按
    #   substr(exit_time,1,10) 归集 (熔断/日亏损门/日报/复盘验证/面板/心跳),
    #   exit_time 缺失的行不再是"归错日"而是从所有当日数字里静默消失。
    #   上方主循环 SELECT 带 exit_time IS NOT NULL, 故必须独立查询。
    try:
        _c10 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        _c10.row_factory = sqlite3.Row
        _no_exit = _c10.execute(
            "SELECT id, symbol, trade_date FROM trade_records "
            "WHERE exit_price > 0 AND (exit_time IS NULL OR exit_time = '') "
            "ORDER BY id").fetchall()
        _c10.close()
        for _r in _no_exit:
            rec("I10_closed_missing_exit_time", _r["id"], _r["symbol"],
                f"exit_price>0 但 exit_time 空 (入场日={_r['trade_date']}) → 出场日口径下整笔消失")
    except Exception as _e10:
        if not _is_structural_error(_e10):
            rec("I10_detector_error", "-", "closed_rows",
                f"{type(_e10).__name__}: {_e10} → exit_time 守卫失明")

    # ── I11~I14 (2026-08-28 D4): 了结行"字段级消失"守卫 ──────────────────
    # 上方主循环只看得到 source_type LIKE 'PLAYBOOK%' 且 exit_time 非空的行;
    # 而当日口径全部按 substr(exit_time,1,10) + COALESCE(pnl_net, realized_pnl) 聚合
    # ⇒ 了结行缺 pnl_net / 缺 realized_pnl / 缺 exit_reason 时不是"算错"而是从数字里
    # 整笔消失 (净口径静默回落毛口径、归因链丢行)。本段覆盖全部来源的了结行。
    # data_quality 前缀命中 _DQ_NEUTRAL_PREFIXES = 写入方声明"非真实了结" (如 id=1327
    #   duplicate_external_close, 故意留 NULL 使净额求和自动排除) → 豁免告警但计数。
    #   注: 首版曾用"data_quality 非空即豁免", 实测 62/62 行全部命中 (常态值是
    #   'real'/'clean' 来源标签) → 守卫直接失效, 故收敛为词表前缀。
    settled_all, exempt = 0, 0
    try:
        _c11 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        _c11.row_factory = sqlite3.Row
        _rows11 = _c11.execute(
            "SELECT * FROM trade_records WHERE exit_price > 0 "
            "AND substr(COALESCE(exit_time, trade_date), 1, 10) >= ?", (since,)).fetchall()
        _c11.close()
        for r in _rows11:
            settled_all += 1
            _keys = set(r.keys())
            dq = str(_col(r, "data_quality") or "").strip().lower()
            if dq and dq.startswith(_DQ_NEUTRAL_PREFIXES):
                exempt += 1
                continue  # 已知坏行豁免 (数量落 settled_exempt_marked)
            e = float(r["entry_price"] or 0)
            x = float(r["exit_price"] or 0)
            q = float(r["quantity"] or 0)
            short = str(r["direction"] or "").upper() in ("SHORT", "SELL_SHORT")
            rid, sym = r["id"], str(r["symbol"])[3:]
            rp = r["realized_pnl"]
            # I11: 了结却无盈亏记账 → 净额/胜率两处聚合同时丢这一笔
            if rp is None:
                rec("I11_closed_null_pnl", rid, sym,
                    f"realized_pnl=NULL (exit {x}) → 出场日口径下整笔消失")
            else:
                # I12: 0 占位伪装真实盈亏 (2026-08-28 占位符链的常驻守卫) —
                #   价差×数量明显非零却记 0, 只可能是写入方拿 NULL/默认值凑数
                _recompute = abs((x - e) * q * (-1 if short else 1))
                if abs(float(rp)) < 1e-9 and _recompute > 1.0:
                    rec("I12_zero_as_proxy_pnl", rid, sym,
                        f"realized_pnl=0 但价差×量={_recompute:.2f} → 占位符伪装盈亏")
            # I13: 净额口径静默回落毛额 — COALESCE(pnl_net, realized_pnl) 的兜底分支
            #   在费用上线日之后仍被走到, 意味着该笔以毛额混进"净额"数字
            #   (列不存在 = 旧库形态, 不判; 存在但为 NULL = 写入链退化, 判)
            if "fees_est" in _keys and "pnl_net" in _keys:
                fees, net = _col(r, "fees_est"), _col(r, "pnl_net")
                _created = str(_col(r, "created_at") or r["exit_time"] or "")[:10]
                if _created >= _FEE_ROLLOUT_DATE and (fees is None or net is None):
                    rec("I13_net_caliber_degraded", rid, sym,
                        f"fees_est={fees} pnl_net={net} (费用净额已上线) → 该笔回落毛口径")
            # I14: 了结无出场原因 → 出场归因链 (SL/TP/时间退出分类) 丢这一笔
            #   NULL 也算: 全库 725 条了结行零 NULL (2026-08-28 实测), 出现 NULL 即写入方停止赋值
            if "exit_reason" in _keys and not str(_col(r, "exit_reason") or "").strip():
                rec("I14_closed_no_exit_reason", rid, sym,
                    "exit_reason 空 → 出场原因分类丢行")
    except Exception as _e11:
        # 检测器自身异常不得读成"全部了结行健康" (D1/D2 同课: 静默 return [] 即二次失明)
        if not _is_structural_error(_e11):
            rec("D4_detector_error", "-", "I11~I14",
                f"{type(_e11).__name__}: {_e11} → 了结行字段守卫失明")
        settled_all = -1

    # F: 字段健康度 — 非空率骤降即测量链断线 (常量退化族预警)
    field_health = {}
    if rows:
        checks = {
            "slippage_bps": lambda r, m: (m.get("execution") or {}).get("slippage_bps"),
            "exit_telemetry": lambda r, m: m.get("exit_telemetry"),
            "entry_price_source": lambda r, m: m.get("entry_price_source"),
            "attribution": lambda r, m: m.get("attribution"),  # 2026-07-28 上线, 逐步爬坡
            "fees_est": lambda r, m: (r["fees_est"] if "fees_est" in r.keys() else None),  # 2026-07-30 上线
        }
        for name, fn in checks.items():
            nn = sum(1 for r in rows if fn(r, json.loads(r["metadata_json"] or "{}")) not in (None, "", {}))
            field_health[name] = f"{nn}/{len(rows)}"
            # attribution/fees_est 是新字段, 首周不告警; 其余跌破 50% 告警
            if name not in ("attribution", "fees_est") and len(rows) >= 10 and nn < len(rows) * 0.5:
                rec("F_field_dropout", "-", name, f"非空率 {nn}/{len(rows)}")

    # ── J2 三口径盈亏对账 (2026-07-31 议题⑩, 用户裁"整体执行"): trade_records (SSOT)
    #    vs trade_attribution vs daily_review 同窗口净额差值 — >5% (且绝对差 >$20) 告警.
    #    2026-09-03 (用户裁"按建议执行", DEB-20260903-trade-attribution-zombie-watch 方案A):
    #    attribution 侧退役 — trade_attribution 表止于 2026-07-14 (ADR-015 策略隔离砍唯一
    #    写入方, 8/27 退役⑦ 裁停 review_loop 规则), 本对账自 7/15 起恒 not_judgeable 属
    #    僵尸观察而非待收敛项. 保留 trade_records_net (SSOT 净额快照), 死表查询移除.
    #    现代归因载体: trade_records.attribution + f1_trend~f5_lifecycle.
    caliber_diff = {}
    try:
        _c2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)  # 主连接 L48 已关, 自开
        _tr_net = _c2.execute(
            "SELECT COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0) FROM trade_records "
            "WHERE trade_date >= ? AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
            "AND exit_time IS NOT NULL", (since,)).fetchone()[0]
        _c2.close()
        caliber_diff = {"trade_records_net": round(_tr_net, 2),
                        "status": "retired_attribution_20260827",
                        "note": ("trade_attribution 表止于 2026-07-14 (ADR-015 策略隔离, "
                                 "8/27 退役⑦); 现代载体 trade_records.attribution+f1~f5; "
                                 "见 DEB-20260903-trade-attribution-zombie-watch")}
    except Exception as _cd_e:
        caliber_diff = {"error": type(_cd_e).__name__}

    # ── W1 参数冻结窗守卫 (2026-08-31, 用户裁"按建议执行"): 版本分裂当天告警 ──
    config_freeze = _check_config_freeze(rec)

    result = {
        "checked_at": now_et().isoformat(), "window_since": since, "sample": len(rows),
        "violations": dict(viol), "examples": examples, "field_health": field_health,
        "caliber_diff": caliber_diff,
        "config_freeze": config_freeze,
        "settled_all_sources": settled_all,   # I11~I14 覆盖面 (全部 source_type)
        "settled_exempt_marked": exempt,      # data_quality 已标记坏行 (豁免告警, 数量增长需人工看)
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1))

    if viol:
        logger.error(f"[不变量自检] {sum(viol.values())} 处违反: "
                     + "; ".join(f"{k}×{n} (例 {examples[k]})" for k, n in viol.most_common()))
        return 1
    logger.info(f"[不变量自检] 全过 ✓ (样本 {len(rows)} 笔, 字段健康 {field_health})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
