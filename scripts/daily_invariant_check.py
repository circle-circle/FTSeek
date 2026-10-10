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
  W2 字段基数棘轮 (离散字段塌缩为常量 → 下游切片零区分度; ADR-038 P7 军规自动化, 2026-09-06)
  W4 except 块内 logger.debug 棘轮 (失败被 LOG_LEVEL=INFO 吞掉; DEB-20260910 §8.2, 2026-09-10)
  W5 裸名 logger 棘轮 (无点号 ⇒ 按子系统设级/过滤覆盖不到; DEB-20260910 §5, 2026-09-10)
  W6 except 块内告警无 exc_info 棘轮 (有告警无栈 ⇒ 根因靠猜; DEB-20260910 §8.2, 2026-09-10)
  W7 昨收分母哨兵: 两影子 jsonl 同标的同窗 chg 差>2pp ⇒ 昨收分母口径分叉
     (DEB-20260911 共识 N1/N3, BIDU 2026-09-08 实锤 11.72pp; 基线棘轮锁存量, 新增才告警)
  豁免: data_quality 前缀命中 duplicate/outlier/invalid/void/excluded 的行 (写入方
        声明"非真实了结", 如 id=1327 duplicate_external_close 故意留 NULL 使净额求和
        自动排除) 不告; 数量落 JSON (settled_exempt_marked) 防豁免面无声扩大。
  自检的自检: I9镜像/I10/I11~I14 段内部异常不再 `except: pass` — 会产出
        *_detector_error 违反 (2026-08-28 D1/D2 同课: "零违反"与"守卫自己崩了"必须可区分,
        否则一个坏掉的看门狗就是最会撒谎的看门狗)。表/列不存在属旧库形态, 仍静默跳过。

输出: 违反数 >0 → logger.error (被 error_alert.py 15min 扫描拾取推钉钉 — 2026-09-10 起本脚本的
     logs/invariant_check.log 才真进了告警扫描面; 此前 error_alert.sh 只读 logs/ftseek_us.log,
     「违反 → 推钉钉」自 7/28 上线起是纸面的, 见 DEB-20260910 §4) + exit 1;
     **W1_* 关键违反另直走 app/push hub** (2026-09-11, DEB-20260909 §7.7⑤(i)): 本脚本单次运行
     无论多少违反恒只输出 1 行汇总 ERROR, 永远够不着 error_alert 的行数阈值地板 3 — 速率型
     判据与"判据破"这类事件型告警结构性不兼容 (§7.2: 9/11 实跑 1 条 < 3 未推送) ⇒ W1_*
     (KR2 冻结守卫) 直推 hub sync=True 必达, 不经行数阈值; 同键集已成功送达则抑制重推;
     其余违反仍走原 logger.error → error_alert 速率通道 (不动别人的判据)。
     全过 → 一行 OK + exit 0。结果落 data/invariant_check_latest.json。
cron: 每交易日盘后 (见 crontab '05 5 * * 2-6')。
"""
from __future__ import annotations

import ast
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


def _is_neutral_data_quality(value) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized.startswith(_DQ_NEUTRAL_PREFIXES)


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
# 主 cohort 窗起点 (2026-09-06 OKR-2026-09 KR2 判分单位修订, 用户授权代裁「交给你来做决策」):
# = 现行退出参数版本 exit-de49b1299290 上线日 (DEB-20260903 议题二方案A, 9/3 夜首笔)。
# 为什么要 cohort 维: 全窗 (8/24 起) 事实上已有 4 个哈希且**不可逆** ⇒ 按全窗字面判 KR2
# 永远 BROKEN, 会把"9/3 起硬冻结"创造出的单版本样本 (OKR 自算 ≈215 笔) 一起判作废 —
# 那是自毁项目史上第一个统计可用窗口。判分单位改 cohort 后判据重新**可达成且可证伪**
# (窗内出现第 2 个哈希即 BROKEN), 同时 8/24→9/2 多版本期如实留痕 (只可作跨版本对照)。
# 变更预算 (同批写死): 2026-09-06 → 09-30 **零次**退出参数变更, P0 豁免通道对本维**关闭**
# (前 4 版全走了豁免清单; 再开一次口子 = 判据名存实亡)。
_FREEZE_COHORT_START = "2026-09-03"
# ── 子窗观测面 (2026-09-11, DEB-20260909 §7.6 选项(d), 用户裁"按建议执行") ──
# 背景: 9/9 行动项C (breakeven_fee_cushion_pct 0.4→0.15) 换了退出参数哈希 ⇒ 主 cohort 窗
# 出现第 2 个哈希 ⇒ W1_kr2_cohort_broken (2026-09-11 05:05 实测已报)。选项(d) = **双口径并列**:
# 主 cohort 判分窗与 BROKEN 判定**一字不动** (不放宽预注册判据), 另加本观测面如实报告
# "破判之后仍有一个单版本窗在攒样", 两份事实并存、不得互相冒充 (沿用 OKR-2026-09 KR2 边界注
# 的既有惯例)。本块**只观测不判分**: 不调 rec()、不产生违反、不参与 status 取劣。
#
# 为什么起点是**显式常量**而不是自动取"最新哈希的首个 trade_date": 自动推导会让**任何**未来
# 换版都静默把窗口重置到自己上线那天 ⇒ 判据永远"单版本"= 比 9/3 勘误修掉的"读错列恒 v2"
# 更隐蔽的结构性假绿。移动本常量 = 改判据面, 必须先落 DEB + 改 OKR (与 _FREEZE_COHORT_START
# 同纪律), 并由 tests/test_w1_exit_version_guard.py 字面钉死强制留痕。
# 为什么是 9/10 而不是 9/9: 9/9 含唯一骑墙笔 (trade 1473 US.SNDK, 开仓 9/9=变更前、
# 退出 9/10T09:38=变更后), 把混源交易放进"单版本窗"会重新制造歧义; 实测两段退出时刻
# 间隔 ≈20h 零交错 (de49 末 9/9T13:33 ET → 13f6 首 9/10T09:38 ET), 9/10 是干净边界。
_SUB_COHORT_START = "2026-09-10"
# ── oct_cohort 判分维 (2026-10-04, OKR-2026-10 KR1, 10 月开跑会裁决 A1, 用户裁「按建议执行」) ──
# OKR-2026-09 KR2 BROKEN 终局 (9 月三段哈希只作跨版本对照) 后, 10 月功效窗的判分对象:
# exit-34c7153088a1 (DEB-20260917 §20–§22 延长时段退出链重构, **用户裁产生 + 已入豁免清单**)
# 的首个干净退出日 = 9/17。边界实测: 13f6 末笔退出 9/16T14:37 ET → 34c7 首笔 9/17T09:48 ET
# (~19h 零交错); 唯一骑墙笔 (trade_date=9/16 入场、34c7 隔夜退出) 按 sub_cohort 骑墙笔先例
# (trade 1473) 排除窗外。**与 sub_cohort 的本质区别**: sub_cohort 是观测面 (不判分),
# 本块是 OKR-2026-10 KR1 的**判分维** (窗内出现第 2 个哈希 ⇒ W1_oct_cohort_broken 当天直推)。
# 移动本常量 = 改判据面, 必须先落 DEB + 改 OKR (与 _FREEZE_COHORT_START 同纪律),
# 并由 tests/test_w1_exit_version_guard.py 字面钉死强制留痕。
_OCT_COHORT_START = "2026-09-17"
_POST_CA4487_START = "2026-10-09"
_POST_CA4487_HASH = "exit-ca4487172a6f"
# ── W1_* 关键违反直推 (2026-09-11, DEB-20260909 §7.7 清单⑤(i), 用户裁选项(d) 同批落地) ──
#    断链根因 (§7.2): 事件型告警 (判据破, 一违反即关键) vs error_alert 的速率型行数阈值
#    (地板 3) 结构性不兼容 — 调低阈值只会让日常噪声刷屏, 不是解法。W1_* 是"零违反=判据
#    在轨, 一违反=判据破"的二元关键事件, 直走 app/push hub (sync=True: 短命 cron 进程要
#    真实送达结果, 与 trigger_gate_report / live_reconcile_check 的直推模式同族)。
#    防刷屏 (同 _I15_SINCE 的"告警必须可动作"纪律): KR2 已破是**持续状态**, 与上次已成功
#    送达的 W1 键集相同则不重推 (否则每晚重推同一事实 = 不可动作噪声, 假告警一多守卫就被
#    无视); 首次 / 上次推送失败 / 键集变化 (如子窗内出现第 3 个哈希的 W1_exit_version_drift)
#    必推。推送结果落 result.w1_direct_push, 失败显性化不上抛 (JSON/exit 码照落)。
_W1_KEY_PREFIX = "W1_"


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
            # cohort 维: 每版本笔数 + 首末成交日 (不依赖 exit_time 列 — 与既有两维同款最小
            # schema 面, 库形态差异时整段走 skipped_old_schema 而非半截假绿)
            cohort_pairs = conn.execute(
                "SELECT COALESCE(json_extract(metadata_json,'$.config_version'),''), COUNT(*), "
                "MIN(trade_date), MAX(trade_date) "
                "FROM trade_records WHERE trade_date >= ? AND source_type='PLAYBOOK_SIM' "
                "GROUP BY 1", (_FREEZE_COHORT_START,)).fetchall()
            # 子窗观测面 (DEB-20260909 §7.6 选项(d)): 同口径, 但只数**已闭合**行 —
            # 本块的用途是"破判后还有多少可测样本", 未平仓行不构成样本 (与主 cohort 维不同,
            # 主维必须含未平仓行才能发现 hash_missing_rows)。同样不依赖 exit_time 列。
            # 独立容错 (2026-09-11 落地时实测教训): 本查询比主维多依赖 exit_price 列 —
            # 旧库/最小测试夹具缺该列时只降级**本观测面** (skipped_old_schema), 不得拖垮
            # 主 cohort 维 (W1 主判据); 若无此隔离, 一次观测面查询就把既有钉子测试整批
            # 打红 = 主判据被观测面绑架。非结构异常照旧上抛走 detector_error。
            try:
                sub_cohort_pairs = conn.execute(
                    "SELECT COALESCE(json_extract(metadata_json,'$.config_version'),''), COUNT(*), "
                    "MIN(trade_date), MAX(trade_date) "
                    "FROM trade_records WHERE trade_date >= ? AND source_type='PLAYBOOK_SIM' "
                    "AND exit_price > 0 GROUP BY 1", (_SUB_COHORT_START,)).fetchall()
            except Exception as _se:
                if not _is_structural_error(_se):
                    raise
                sub_cohort_pairs = None
            # oct_cohort 判分维 (2026-10-04, OKR-2026-10 KR1): 同款最小 schema 面 + 独立容错 —
            # 观测面/判分维的查询失败都不得拖垮主 cohort 维 (W1 主判据), 与 sub_cohort 同一纪律。
            try:
                oct_cohort_pairs = conn.execute(
                    "SELECT COALESCE(json_extract(metadata_json,'$.config_version'),''), COUNT(*), "
                    "MIN(trade_date), MAX(trade_date) "
                    "FROM trade_records WHERE trade_date >= ? AND source_type='PLAYBOOK_SIM' "
                    "AND exit_price > 0 GROUP BY 1", (_OCT_COHORT_START,)).fetchall()
            except Exception as _oe:
                if not _is_structural_error(_oe):
                    raise
                oct_cohort_pairs = None
            try:
                post_ca4487_pairs = conn.execute(
                    "SELECT COALESCE(json_extract(metadata_json,'$.config_version'),''), COUNT(*), "
                    "MIN(trade_date), MAX(trade_date) "
                    "FROM trade_records WHERE trade_date >= ? AND source_type='PLAYBOOK_SIM' "
                    "AND exit_price > 0 GROUP BY 1", (_POST_CA4487_START,)).fetchall()
            except Exception as _pe:
                if not _is_structural_error(_pe):
                    raise
                post_ca4487_pairs = None
        finally:
            conn.close()
        versions = {str(v): n for v, n in pairs}
        # 无哈希行 (旧行/写入侧未落 metadata) 单列计数, 不当作版本 — 否则首跑即假漂移
        exit_versions = {str(v): n for v, n in exit_pairs if str(v).startswith("exit-")}
        info["exit_hash_missing_rows"] = sum(
            n for v, n in exit_pairs if not str(v).startswith("exit-"))
        info["versions"] = versions
        info["exit_versions"] = exit_versions
        cohort_versions = {str(v): {"n": n, "first": f, "last": la}
                           for v, n, f, la in cohort_pairs if str(v).startswith("exit-")}
        info["cohort"] = {
            "window_start": _FREEZE_COHORT_START,
            "versions": cohort_versions,
            "n_unique": len(cohort_versions),
            # 无哈希行同样只计数不当版本 (未平仓行 metadata 未落哈希 — 与全窗维同一纪律)
            "hash_missing_rows": sum(n for v, n, _, _ in cohort_pairs
                                     if not str(v).startswith("exit-")),
        }
        if len(cohort_versions) > 1:
            info["cohort"]["status"] = "broken"
            rec("W1_kr2_cohort_broken", "-", ",".join(sorted(cohort_versions)),
                f"KR2 主 cohort 窗 ({_FREEZE_COHORT_START}→2026-09-30) 出现 "
                f"{len(cohort_versions)} 个退出哈希 ⇒ 判据 **BROKEN** (2026-09-06 修订: 变更预算"
                f"归零, P0 豁免通道对本维关闭 — config_freeze_baseline.json 的豁免**不适用**); "
                f"9 月单版本样本作废, 须 DEB 落档 + 10 月开跑会项目级裁决 | 2026-09-11 追加: "
                f"起因=9/9 行动项C 换版 (DEB-20260909 §6), 已按 §7.6 选项(d) 双口径并列落档 — "
                f"本判定维持 BROKEN 不放宽, 另见 sub_cohort 观测面 (非 KR2 判分对象)")
        else:
            info["cohort"]["status"] = "single_version" if cohort_versions else "no_rows"
        # ── 子窗观测面: 只报告, 不判分 (不调 rec / 不参与 status 取劣) ──
        if sub_cohort_pairs is None:
            # 缺 exit_price 列的旧库形态: 观测面跳过, 主 cohort 维结果不受影响
            info["sub_cohort"] = {
                "window_start": _SUB_COHORT_START,
                "status": "skipped_old_schema",
                "not_kr2_verdict": True,
                "note": "exit_price 列缺失 (旧库形态) — 观测面跳过, 主 cohort 判分维不受影响",
            }
        else:
            sub_versions = {str(v): {"n": n, "first": f, "last": la}
                            for v, n, f, la in sub_cohort_pairs if str(v).startswith("exit-")}
            info["sub_cohort"] = {
                "window_start": _SUB_COHORT_START,
                "versions": sub_versions,
                "n_unique": len(sub_versions),
                "n_closed": sum(d["n"] for d in sub_versions.values()),
                "status": ("single_version" if len(sub_versions) == 1 else
                           "broken" if len(sub_versions) > 1 else "no_rows"),
                # 显式防误读: 本块不是判据, 不得被任何面板/结算当成 KR2 达标证据
                "not_kr2_verdict": True,
                "note": ("观测面 (DEB-20260909 §7.6 选项(d), 2026-09-11 用户裁'按建议执行'): "
                         "主 cohort 窗 BROKEN 后仍在攒样的单版本子窗, 只报已闭合笔数与唯一性, "
                         "**不是** KR2 判分对象, 不得据此宣称 KR2 达标或 9 月样本有效"),
            }
        # ── oct_cohort: OKR-2026-10 KR1 判分维 (2026-10-04 开跑会 A1) ──
        # 与 sub_cohort (纯观测, 不 rec) 不同: 本块**判分并告警** — 窗内 ≥2 个退出哈希
        # ⇒ W1_oct_cohort_broken (走 W1_* 直推通道, 当天送达用户)。
        # 刻意**不参与 info["status"] 取劣**: status 的既有语义 = 全窗维 (档案列 + 退出哈希豁免对账)
        # 的漂移状态, 消费方 (meta_audit / 周报) 按该语义读; 混入 oct 维会改变既有键语义
        # (改判据面之外的口径)。本维的告警出口是 W1 key, 不是 status。
        if oct_cohort_pairs is None:
            info["oct_cohort"] = {
                "okr": "2026-10", "window_start": _OCT_COHORT_START,
                "status": "skipped_old_schema", "not_kr2_09_verdict": True,
                "note": "exit_price 列缺失 (旧库形态) — 判分维跳过, 主 cohort 维不受影响",
            }
        else:
            oct_versions = {str(v): {"n": n, "first": f, "last": la}
                            for v, n, f, la in oct_cohort_pairs if str(v).startswith("exit-")}
            oct_n_unique = len(oct_versions)
            info["oct_cohort"] = {
                "okr": "2026-10",
                "window_start": _OCT_COHORT_START,
                "versions": oct_versions,
                "n_unique": oct_n_unique,
                "n_closed": sum(d["n"] for d in oct_versions.values()),
                "n_target": 200,      # OKR-2026-10 KR1 功效目标 (非判据, 只作进度提示)
                "hash_missing_rows": sum(n for v, n, _, _ in oct_cohort_pairs
                                         if not str(v).startswith("exit-")),
                "status": ("single_version" if oct_n_unique == 1 else
                           "broken" if oct_n_unique > 1 else "no_rows"),
                # 显式防误读: 本块判的是 OKR-2026-10 KR1, 与 9 月 KR2 (BROKEN 终局) 不是同一判据
                "not_kr2_09_verdict": True,
                "note": ("OKR-2026-10 KR1 判分维 (开跑会 A1, 2026-10-04 用户裁「按建议执行」): "
                         "34c7 单版本功效窗, 起点=9/17 (骑墙笔按 trade 1473 先例排除窗外)。"
                         "**不是** OKR-2026-09 KR2 的判分对象 — 后者 BROKEN 终局不改写, "
                         "本块 in_track 不得被读成 9 月达标"),
            }
            if oct_n_unique > 1:
                rec("W1_oct_cohort_broken", "-", ",".join(sorted(oct_versions)),
                    f"OKR-2026-10 KR1 功效窗 ({_OCT_COHORT_START}→) 出现 {oct_n_unique} 个退出哈希 "
                    f"⇒ 判据 **BROKEN** (变更预算 0, P0 豁免通道对本维关闭); 按预注册回滚线须 "
                    f"DEB 落档 + 开跑会级裁决, **不得静默移窗** (移动 _OCT_COHORT_START = 改判据面)")
            if post_ca4487_pairs is None:
                info["post_ca4487"] = {
                    "window_start": _POST_CA4487_START,
                    "expected_hash": _POST_CA4487_HASH,
                    "status": "skipped_old_schema",
                    "not_okr_2026_10_kr1_verdict": True,
                }
            else:
                post_versions = {str(v): {"n": n, "first": f, "last": la}
                                 for v, n, f, la in post_ca4487_pairs
                                 if str(v).startswith("exit-")}
                post_n_unique = len(post_versions)
                expected_only = list(post_versions) == [_POST_CA4487_HASH]
                info["post_ca4487"] = {
                    "window_start": _POST_CA4487_START,
                    "expected_hash": _POST_CA4487_HASH,
                    "versions": post_versions,
                    "n_unique": post_n_unique,
                    "n_closed": sum(d["n"] for d in post_versions.values()),
                    "n_target": 200,
                    "hash_missing_rows": sum(n for v, n, _, _ in post_ca4487_pairs
                                             if not str(v).startswith("exit-")),
                    "expected_hash_only": expected_only,
                    "status": ("single_version" if expected_only else
                               "hash_drift" if post_n_unique > 1 else
                               "unexpected_single_version" if post_n_unique == 1 else "no_rows"),
                    "not_okr_2026_10_kr1_verdict": True,
                    "note": ("DEB-20261009 §28.10 方案A清洁观测面: 2026-10-08新旧哈希同日交错, "
                             "故从2026-10-09起观测ca4487; 只报告不判分不告警, "
                             "不得替代或改写oct_cohort BROKEN终局"),
                }
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


def _push_w1_critical(viol, examples: dict, checked_at: str) -> dict:
    """W1_* 关键违反直推 app/push hub (sync=True) — 不经 error_alert 行数阈值。

    防刷屏判据 = 与**上一次已成功送达**的 W1 键集比较 (读旧 OUT 文件, 须在覆写前调用):
    相同 → suppressed; 上次失败 / 无记录 (含旧代码时代的产物, 即"从未送达") / 键集变化 →
    必推。读取上次状态失败按"未送达"处理 (宁可重推不可漏推)。返回 dict 落
    result.w1_direct_push: {keys, attempted, ok, error, suppressed_reason}。
    """
    w1_keys = sorted(str(k) for k in viol if str(k).startswith(_W1_KEY_PREFIX))
    out = {"keys": w1_keys, "attempted": False, "ok": None, "error": None,
           "suppressed_reason": None}
    if not w1_keys:
        return out
    prev_notified: list = []
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
        prev_push = (prev or {}).get("w1_direct_push") or {}
        if prev_push.get("attempted") and prev_push.get("ok"):
            prev_notified = sorted(prev_push.get("keys") or [])
    except Exception:
        prev_notified = []
    if w1_keys == prev_notified:
        out["suppressed_reason"] = "与上次已成功送达的 W1 键集相同 (持续状态不每晚重推)"
        return out
    out["attempted"] = True
    lines = [f"· {k}×{viol[k]} — 例: {examples.get(k, '')}" for k in w1_keys]
    hub = None
    try:
        from app.push import get_push_hub, PushMessage, Level, Category
        hub = get_push_hub()
        ok = hub.push(PushMessage(
            title=f"⚠️ 不变量自检 W1 关键违反 {len(w1_keys)} 类: {', '.join(w1_keys)}",
            body="\n".join(lines) + (
                f"\n产物 data/invariant_check_latest.json (checked_at {checked_at})。"
                "W1 = OKR-2026-09 KR2 参数冻结守卫; 判分口径见 DEB-20260909-exit-params §7 "
                "(2026-09-11 已裁选项(d): 主 cohort BROKEN 不放宽, sub_cohort 观测面非判分对象)。"
                "本推送为事件型直推, 不经 error_alert 行数阈值。"),
            level=Level.HIGH,
            category=Category.RISK_ALERT,
            dedup_key=f"invariant_w1:{str(checked_at)[:16]}",
        ), sync=True)
        out["ok"] = bool(ok)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        logger.error(f"W1 直推失败 (违反已落 JSON/日志, 但主动通知链路断): {e}", exc_info=True)
    finally:
        # sync=True 时消息已发出, stop() 只做聚合 flush + 关线程池; 不关会让短命 cron 进程
        # 挂在 hub 的非 daemon worker 上 (error_alert.py:360-384 同课)。关失败只 WARNING 带栈,
        # 不改 out["ok"] — 推送成败以 hub.push 返回为准。
        try:
            if hub is not None:
                hub.stop()
        except Exception as e:
            logger.warning("W1 直推后 push hub 关闭异常 (推送结果不受影响): %s", e, exc_info=True)
    return out


# ── W2 字段基数棘轮 (2026-09-06, ADR-038 P7 军规自动化, 用户裁"按建议执行") ──
#    病灶: "字段在生产 ≠ 有信息" — 常量退化已六例 (da_score 75.9% 塌在 6.0 /
#    invalidation_trigger_count 100%=2 / tripwire_count 99.6%=2 / ue_instruction
#    confidence 100%=50 / da_instruction score 53.2% 塌在 7.0 / challenger_score),
#    全部靠事后人工复盘才发现。工具 scripts/shadow_field_cardinality.py (W0) 已能机械
#    判 DEGEN 并 exit 1, 但此前无任何调度方 → P7 军规停在纸面。本守卫把它接进每晚
#    不变量自检, 与 W1 同款棘轮纪律:
#      基线外新增 DEGEN  → 告警 (损失封顶当天, 经 error_alert.py 推钉钉);
#      基线内已知 DEGEN  → silenced_known 计数, 不刷不可动作噪声 (存量病灶在修);
#      基线内已愈合      → healed 列出, 提示人工收缩基线 (棘轮只降不升, 不自动改写);
#      采集器/检测器异常 → W2_detector_error ("零违反"与"守卫自己崩了"必须可区分);
#      表/列不存在       → skipped_old_schema (库形态差异, 与 W1/I9/I10 同款静默跳过)。
#    豁免通道: data/field_cardinality_baseline.json 的 degen 追加 "source.field" + refs
#    说明 (改本文件 = 改判据, 须先落档 ADR/DEB — 与 config_freeze_baseline.json 同纪律)。
#    回滚: 删本段 + main() 内两行接线 + 基线文件; 零生产代码依赖。
_FIELD_CARD_SCRIPT = REPO / "scripts" / "shadow_field_cardinality.py"
_FIELD_CARD_BASELINE = REPO / "data" / "field_cardinality_baseline.json"
_FIELD_CARD_DAYS = 30  # 与工具默认口径一致 (n<30 记 INSUFFICIENT, 不判 DEGEN)


def _load_field_cardinality_module():
    """按路径加载 W0 工具 (scripts/ 非包, 与 tests 侧 importlib 加载本脚本同款做法)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "shadow_field_cardinality", _FIELD_CARD_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check_env_demix(rec) -> dict:
    """W3: sim/live 账本混同守卫 (RFC-080 demix 2026-09-09, 用户裁决"不要混为一谈包括历史数据").

    双向检测 (只读):
      · sim 库出现 trade_mode='live' 行 = 真钱记录误进研究账本
      · live 库出现 trade_mode!='live' 行 = 模拟记录误进实盘账本
    根因已修: position_sync external_close 原硬编码 trade_mode='live' 无视 env
    (broker REAL 成交史实锤 MSFT/AVGO/TTE/BULL 4 行零真钱证据); 现跟随
    AUTO_TRADE_ACCOUNT。本守卫防任何新写入方复发。
    单库翻转日后 (TRADE_ENV=0): 权威库=live 库, sim 库停止生长 — 届时 sim 侧检测
    自动空转, live 侧检测继续有效 (auto-live 与 manual 都属 live, trade_mode='live'
    恒真; PLAYBOOK_SHADOW 等模拟行混入即告警)。
    """
    out = {"sim_live_rows": -1, "live_sim_rows": -1}
    sim_db = REPO / "data" / "ftseek_us.db"
    live_db = REPO / "data" / "ftseek_us_live.db"
    try:
        if sim_db.exists():
            c = sqlite3.connect(f"file:{sim_db}?mode=ro", uri=True)
            out["sim_live_rows"] = c.execute(
                "SELECT COUNT(*) FROM trade_records WHERE trade_mode='live'").fetchone()[0]
            c.close()
        if live_db.exists():
            c = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
            out["live_sim_rows"] = c.execute(
                "SELECT COUNT(*) FROM trade_records WHERE trade_mode!='live'").fetchone()[0]
            c.close()
        if out["sim_live_rows"] > 0:
            rec("W3_sim_db_live_rows", "-", "trade_records",
                f"sim 库出现 {out['sim_live_rows']} 行 trade_mode='live' (真钱记录误进研究账本)")
        if out["live_sim_rows"] > 0:
            rec("W3_live_db_sim_rows", "-", "trade_records",
                f"live 库出现 {out['live_sim_rows']} 行 trade_mode!='live' (模拟记录误进实盘账本)")
    except Exception as e:
        if not _is_structural_error(e):
            rec("W3_demix_check_error", "-", "-", f"{type(e).__name__}: {e}")
    return out


# ── W8 费用异常单守卫 (2026-10-10, DEB-20261009-event §30, 用户裁「费用护栏同意」) ──
#   形态 = 监控非硬门: 损伤路径 (EXT) 已于 9/17 当日由 $5 地板 + 100bps 闸收口
#   (ce54f64eb), playbook 准入 $5/$2500 双地板算术封顶 ~50bps ⇒ 新硬门是死规则。
_FEE_BPS_WATCH_START = "2026-10-10"   # 裁决日起生效; 存量 CTNT/GIPR (9/17) 不追溯
_FEE_BPS_MAX = 200.0                  # 预注册阈值 (§28.3 回放: 全历史拦 2 误杀 0)


def _check_fee_bps_anomaly(rec, db_path=None) -> dict:
    """W8: 仙股/最低佣金异常单守卫 (只读, 次日发现, 不拦单).

    对裁决日起所有已平仓单用 fee_model.estimate_round_trip 重算**事前** fee_bps,
    >_FEE_BPS_MAX 即告警 — 防未来新入场路径绕过既有价格/费用地板 (CTNT/GIPR 同型)。
    与 §28.3 回放判据同源同阈值; 预注册升级条件: 任一 30 日内新增触发 ≥1 笔
    ⇒ 自动升级为「硬门提案」重新裁决。回滚: 删 main() 注册行 + 本函数。
    """
    out = {"scanned": -1, "anomalies": -1, "watch_start": _FEE_BPS_WATCH_START,
           "max_fee_bps": _FEE_BPS_MAX}
    db = Path(db_path) if db_path else (REPO / "data" / "ftseek_us.db")
    try:
        from app.execution.fee_model import estimate_round_trip
        if not db.exists():
            return out
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = c.execute(
            "SELECT symbol, direction, entry_price, quantity, trade_date "
            "FROM trade_records "
            "WHERE exit_time IS NOT NULL AND exit_reason <> '' "
            "AND quantity > 0 AND entry_price > 0 AND trade_date >= ?",
            (_FEE_BPS_WATCH_START,)).fetchall()
        c.close()
        out["scanned"] = len(rows)
        hits = []
        for symbol, direction, ep, qty, td in rows:
            fee = estimate_round_trip(qty, ep, ep, direction, td)
            notional = qty * ep
            fee_bps = 10000.0 * fee["total"] / notional if notional > 0 else None
            if fee_bps is not None and fee_bps > _FEE_BPS_MAX:
                hits.append(f"{symbol}@{td}({fee_bps:.0f}bps)")
        out["anomalies"] = len(hits)
        if hits:
            rec("W8_fee_bps_anomaly", "-", ",".join(hits[:5]),
                f"裁决日({_FEE_BPS_WATCH_START})后已平仓单出现 {len(hits)} 笔 "
                f"fee_bps>{_FEE_BPS_MAX:g} (仙股/最低佣金异常, CTNT/GIPR 同型)")
    except Exception as e:
        if not _is_structural_error(e):
            rec("W8_detector_error", "-", "-", f"{type(e).__name__}: {e}")
    return out


def _check_field_cardinality(rec, _collect=None) -> dict:
    """W2: 离散字段基数棘轮 — 新塌缩当天告警, 已知病灶静默计数。全程只读。

    _collect: 可注入采集器 (days -> [check_field 结果 dict]); 默认加载
    scripts/shadow_field_cardinality.py 的 collect, 并把其模块级 _DB 覆写为本脚本
    按 TRADE_ENV 解析出的 DB — 工具默认硬编码 data/ftseek_us.db, 切实盘那天会变成
    "守卫在读陈旧 sim 名单" (与 _resolve_db 注释同一族病灶)。单测传桩, 不碰生产库/日志。
    """
    info = {"days": _FIELD_CARD_DAYS}
    try:
        if _collect is None:
            mod = _load_field_cardinality_module()
            mod._DB = DB
            _collect = mod.collect
        results = _collect(_FIELD_CARD_DAYS) or []
    except Exception as e:
        # 表/列不存在 = 库形态差异 (旧库/最小夹具), 与 W1/I9/I10 同款静默跳过 —
        # 否则凡是用最小夹具跑 main() 的既有测试都会被 W2 染红 (2026-09-06 实测 8 例)。
        if _is_structural_error(e):
            info["status"] = "skipped_old_schema"
            return info
        rec("W2_detector_error", "-", type(e).__name__,
            f"W2 基数自检采集失败: {e} → 常量退化守卫失明, 本次'零违反'不可信")
        info["status"] = "detector_error"
        return info
    try:
        def _key(row: dict) -> str:
            return f'{row["source"]}.{row["field"]}'

        degen = sorted(_key(r) for r in results if r.get("verdict") == "DEGEN")
        info["fields_checked"] = len(results)
        info["degen"] = degen
        info["insufficient"] = sorted(
            _key(r) for r in results if r.get("verdict") == "INSUFFICIENT")
        if not _FIELD_CARD_BASELINE.exists():
            baseline = {
                "degen": degen,
                "established_at": now_et().isoformat(timespec="seconds"),
                "note": ("已知常量退化字段基线 (ADR-038 P7 军规); 豁免=degen 追加 "
                         "\"source.field\" 并在 refs 写裁决出处。改本文件=改判据, 须先落档; "
                         "棘轮只降不升 — 字段愈合后由人工收缩, 守卫不自动改写。"),
                "refs": {},
            }
            _FIELD_CARD_BASELINE.write_text(
                json.dumps(baseline, ensure_ascii=False, indent=1), encoding="utf-8")
            info["status"] = "baseline_established"
            return info
        baseline = json.loads(_FIELD_CARD_BASELINE.read_text(encoding="utf-8"))
        known = set(baseline.get("degen") or [])
        new = sorted(set(degen) - known)
        info["silenced_known"] = len(set(degen) & known)
        info["healed"] = sorted(known - set(degen))
        for f in new:
            rec("W2_new_field_degen", "-", f,
                f"字段取值塌缩 (基线外新增 DEGEN, 近 {_FIELD_CARD_DAYS} 天 n≥30): 落了库但"
                f"零区分度, 一切下游切片表面无恙实则失明; 处置=查写入方是否退化/是否 "
                f"write-only (P7 消费方登记), 或经 ADR/DEB 落档后登记 {_FIELD_CARD_BASELINE.name}")
        info["status"] = "drift" if new else "ok"
        return info
    except Exception as e:
        rec("W2_detector_error", "-", type(e).__name__, f"W2 检测器自身异常: {e}")
        info["status"] = "detector_error"
        return info


# ── W4/W5/W6 日志卫生棘轮 (2026-09-10, DEB-20260910 §8.2 + §5, 用户裁「按照建议执行优化」) ──
#    病灶 (全仓 AST 实测, 非 grep 估算; 数字见三个基线文件的 counts 合计):
#      W4 `except` 块内 logger.debug 记失败/异常 ≈ 711 处 / 164 文件 — LOG_LEVEL=INFO 下**全不
#         可见**。已逐案修 4 轮仍在漏: scheduler_core.py:2375/:2450 (2026-07-04)、
#         playbook_service.py:1466/:1486 (DEB-20260721 R4)、pipeline.py:800 (2026-07-27)、
#         DEB-20260904 #148, 本批又抓 4 处, 其中 :2461 与已修的 :2450 **相隔 11 行**
#         ⇒ 逐案修不收敛, 必须立规则 (DEB §8.2 的立法理由)。
#      W5 裸名 logger (`logging.getLogger("无点号字面量")`) ≈ 17 处 — 无法按子系统 setLevel/
#         过滤, 且迫使 error_alert / daily_alert_summary 退化为全行子串匹配 (DEB §5 的成因链)。
#      W6 `except` 块内 warning/error/critical **不带 exc_info** ≈ 1142 处 / 243 文件 (全仓
#         4230 个 except 处理器只有 103 处带栈, 98% 无栈) ⇒ 有告警无栈, 根因只能靠猜。
#         ②批已在 scheduler_core 逐处补 19 处; 本棘轮防新增。
#    纪律 (照 W2 棘轮形态原样沿用):
#      基线外新增文件 / 基线内计数增长 → 当晚告警 + exit 1 (经 error_alert.py 推钉钉);
#      基线内存量 → silenced_known 计数, 不刷不可动作噪声 (存量在修);
#      计数下降 → healed 列出, 提示人工收缩基线 (**守卫绝不自动改写基线**, 棘轮只降不升);
#      解析失败 → W456_parse_error; 检测器异常 → *_detector_error ("零违反"与"守卫自己崩了"
#      必须可区分, 8/28 D1/D2 同课)。
#    判据口径 = **per-file 计数**, 不是行号: 行号随任何编辑漂移 ⇒ 行号基线会天天假告警,
#      假告警一多守卫就被无视 (比没有守卫更糟)。refs 只是诊断样本, 判据不用它。
#      已知弱点 (诚实登记): 同一文件"修一处 + 新增一处"净计数不变 ⇒ 抓不到; 跨文件不掩盖。
#    豁免通道: 改对应 data/*_baseline.json 的 counts + 在 refs 写裁决出处 (改基线 = 改判据,
#      须先落档 ADR/DEB — 与 config_freeze_baseline.json / field_cardinality_baseline.json 同纪律)。
#    扫描面: main.py + app/ scripts/ ops/ config/ 的 *.py; 排除 tests/ venv/ archive/
#      __pycache__/ (tests 非生产可见面; archive 是墓地, 军规要求零 import 也就不必守它)。
#    回滚: 删本段 + main() 内两行接线 + 三个基线文件; 零生产代码依赖。
_HYGIENE_ROOTS = ("app", "scripts", "ops", "config")
_HYGIENE_EXCLUDE = {"archive", "__pycache__", "venv", "tests", ".git", "node_modules"}
_HYGIENE_DIMS = ("debug_swallow", "bare_logger", "except_stack")
_HYGIENE_CODES = {"debug_swallow": "W4_debug_swallow", "bare_logger": "W5_bare_logger",
                  "except_stack": "W6_except_stack"}
_HYGIENE_BASELINES = {
    "debug_swallow": REPO / "data" / "debug_swallow_baseline.json",
    "bare_logger": REPO / "data" / "bare_logger_baseline.json",
    "except_stack": REPO / "data" / "except_stack_baseline.json",
}
_NO_STACK_METHODS = {"warning", "warn", "error", "critical"}
_HYGIENE_FIX = {
    "debug_swallow": ("except 块内改 logger.warning(..., exc_info=True) — 失败必须可见且带栈; "
                      "确属可忽略噪声就删掉这行 debug, 别靠 LOG_LEVEL=INFO 把它藏起来"),
    "bare_logger": ("logger 名改 `ftseek.<subsystem>.<module>` (DEB-20260910 §5 建议的 SSOT "
                    "约定) — 裸名无法按子系统设级/过滤"),
    "except_stack": ("补 exc_info=True 或改用 logger.exception(...) — 有告警无栈等于让值班人猜根因"),
}
_HYGIENE_NOTE = (
    "日志卫生棘轮基线 (DEB-20260910 §8.2/§5, W2 同款纪律)。counts = per-file 计数, 判据只用它; "
    "refs = 诊断样本 (带行号, 会随编辑漂移, **不参与判定**)。只降不升: 计数下降由人工收缩本文件, "
    "守卫绝不自动改写。改 counts = 改判据, 须先落档 ADR/DEB 并在 refs 写裁决出处。"
)


def _hygiene_py_files(repo: Path = None) -> list:
    """扫描面 (确定性排序, 与运行目录无关)。

    repo 不用默认参数绑 REPO —— 默认参数在 def 时求值, 单测 monkeypatch `dic.REPO`
    就换不掉扫描面 (与 _check_field_cardinality 覆写 mod._DB 同一族接线坑)。
    """
    repo = repo or REPO
    files = [repo / "main.py"]
    for root in _HYGIENE_ROOTS:
        base = repo / root
        if not base.is_dir():
            continue
        for f in base.rglob("*.py"):
            if any(part in _HYGIENE_EXCLUDE for part in f.relative_to(repo).parts):
                continue
            files.append(f)
    return sorted({f for f in files if f.is_file()})


def _scan_log_hygiene(path: Path) -> dict:
    """AST 单遍量三维 (只读)。嵌套 except 只计一次 (按 Call 节点身份去重)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    in_handler = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            in_handler.update(sub for sub in ast.walk(node) if isinstance(sub, ast.Call))
    out = {d: 0 for d in _HYGIENE_DIMS}
    refs = {d: [] for d in _HYGIENE_DIMS}

    def _hit(dim: str, node) -> None:
        out[dim] += 1
        if len(refs[dim]) < 3:
            try:
                code = ast.unparse(node)
            except Exception:                     # 极端形态 (py<3.9 语法) 不影响计数
                code = "<unparse 失败>"
            refs[dim].append({"line": getattr(node, "lineno", -1), "code": code[:110]})

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if node in in_handler:
            if method == "debug":
                _hit("debug_swallow", node)
            elif (method in _NO_STACK_METHODS
                  and "exc_info" not in {kw.arg for kw in node.keywords}):
                _hit("except_stack", node)
        if (method == "getLogger" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value and "." not in node.args[0].value):
            _hit("bare_logger", node)
    out["refs"] = refs
    return out


def _hygiene_ratchet(rec, dim: str, counts: dict, refs: dict) -> dict:
    """单维棘轮 (照 _check_field_cardinality 形态): 新增/增长当天告警, 存量静默计数, 愈合提示。"""
    code = _HYGIENE_CODES[dim]
    path = _HYGIENE_BASELINES[dim]
    info = {"total": sum(counts.values()),
            "files": sum(1 for v in counts.values() if v)}
    try:
        if not path.exists():
            path.write_text(json.dumps(
                {"counts": counts, "refs": refs,
                 "established_at": now_et().isoformat(timespec="seconds"),
                 "note": _HYGIENE_NOTE, "fix": _HYGIENE_FIX[dim],
                 "deb_ref": "DEB-20260910-logging-observability-batch §8.2/§5"},
                ensure_ascii=False, indent=1), encoding="utf-8")
            info["status"] = "baseline_established"
            return info
        known = (json.loads(path.read_text(encoding="utf-8")) or {}).get("counts") or {}
        new_files = sorted(k for k, v in counts.items() if v and k not in known)
        grew = sorted((k, known[k], counts[k]) for k in counts
                      if k in known and counts[k] > known[k])
        healed = sorted((k, known[k], counts.get(k, 0)) for k in known
                        if counts.get(k, 0) < known[k])
        info.update({
            "silenced_known": sum(1 for k, v in counts.items()
                                  if v and k in known and v <= known[k]),
            "new_files": new_files,
            "grew": [f"{k}: {a}→{b} (+{b - a})" for k, a, b in grew],
            "healed": [f"{k}: {a}→{b}" for k, a, b in healed],
        })
        for k in new_files:
            sample = (refs.get(k) or [{}])[0]
            rec(f"{code}_new_file", "-", k,
                f"基线外新增 {counts[k]} 处 (例 L{sample.get('line')}: {sample.get('code', '')}) "
                f"→ 处置: {_HYGIENE_FIX[dim]}; 或经 ADR/DEB 落档后登记 {path.name}")
        for k, a, b in grew:
            sample = (refs.get(k) or [{}])[0]
            rec(f"{code}_grew", "-", k,
                f"{a}→{b} (+{b - a}, 例 L{sample.get('line')}: {sample.get('code', '')}) "
                f"→ 处置: {_HYGIENE_FIX[dim]}; 或经 ADR/DEB 落档后登记 {path.name}")
        info["status"] = "drift" if (new_files or grew) else "ok"
        return info
    except Exception as e:
        rec(f"{code}_detector_error", "-", dim,
            f"{type(e).__name__}: {e} → 日志卫生棘轮失明, 本次'零违反'不可信")
        info["status"] = "detector_error"
        return info


def _check_log_hygiene(rec) -> dict:
    """W4/W5/W6: 一次 AST 遍历, 三份棘轮。全程只读, 不碰生产库/日志。"""
    counts = {d: {} for d in _HYGIENE_DIMS}
    refs = {d: {} for d in _HYGIENE_DIMS}
    parse_errors = []
    files = _hygiene_py_files()
    for f in files:
        rel = str(f.relative_to(REPO))
        try:
            got = _scan_log_hygiene(f)
        except Exception as e:
            # 解析不了 = 该文件对本棘轮不可见 ⇒ 必须显性化, 不得当"零违反"
            parse_errors.append(f"{rel}: {type(e).__name__}: {e}")
            continue
        for dim in _HYGIENE_DIMS:
            if got[dim]:
                counts[dim][rel] = got[dim]
                refs[dim][rel] = got["refs"][dim]
    for pe in parse_errors:
        rec("W456_parse_error", "-", pe.split(":")[0],
            f"日志卫生扫描无法解析该文件 ⇒ 三维棘轮对它失明: {pe}")
    out = {"files_scanned": len(files), "parse_errors": len(parse_errors)}
    for dim in _HYGIENE_DIMS:
        out[_HYGIENE_CODES[dim]] = _hygiene_ratchet(rec, dim, counts[dim], refs[dim])
    return out


# ── W7 昨收分母哨兵 (2026-09-11, DEB-20260911 共识 N1/N3, 用户裁「按建议执行」) ──
#    病灶 (BIDU 2026-09-08 实锤, DEB C6): 同一标的同一时刻, 两条取数路径对「chg vs 昨收」
#    给出分叉读数 — premarket_invalidation 走 futu get_market_snapshot 原始 prev_close_price
#    (干净侧, SMTC 8/26 真值对照已证), ctx 走 get_quote push 缓存 prev_close (陈旧侧, 9/8 BIDU
#    快照 -7.65% vs ctx +4.07%, 7 秒内差 11.72pp 且 change_percent 全日冻结) ⇒ 所有
#    ctx["change_percent"] 消费者 (short_chase_ban_shadow / entry_timing_shadow / 记分牌)
#    当日读数全部不可信, 但此前无守卫盯「分母口径」维度 — 两个影子各自零违反 ≠ 口径一致。
#    机制: 两影子 jsonl 按 inst_id (缺失退 (symbol,date)) join, 可比窗 ±10min 内取最小
#    |Δchg| (两读数最接近的时刻才是同一次测量; 窗外行含日内价格漂移, 不可比不判)。
#    纪律 (照 W2/W456 棘轮形态):
#      |Δchg|>2pp 基线外新增 → 当晚 W7_chg_denominator_divergence (损失封顶当天);
#      基线内存量 (BIDU 9/8 等 90 例) → silenced_known 计数, 不刷不可动作噪声 (分母修复前排期中);
#      愈合 → healed 列出提示人工收缩基线 (棘轮只降不升, 守卫绝不自动改写);
#      影子档缺失 = 部署形态差异 → skipped_no_shadow 静默 (与 W1 skipped_old_schema 同纪律);
#      坏行跳过计数, 整档不可读 → W7_detector_error ("零违反"与"守卫崩了"必须可区分, 8/28 D1/D2 同课)。
#    豁免通道: data/chg_denominator_baseline.json 的 divergent 追加键 + refs 裁决出处
#    (改基线 = 改判据, 须先落档 ADR/DEB — 与 config_freeze / field_cardinality 同纪律)。
#    回滚: 删本段 + main() 内两行接线 + 基线文件; 零生产代码依赖。
_W7_THRESHOLD_PP = 2.0
_W7_WINDOW_MIN = 10
_W7_PMI = REPO / "data" / "premarket_invalidation_shadow.jsonl"   # 快照路径 (干净昨收)
_W7_ETS = REPO / "data" / "entry_timing_shadow.jsonl"             # ctx 路径 (push 缓存昨收)
_W7_BASELINE = REPO / "data" / "chg_denominator_baseline.json"


def _w7_ts(value):
    """ISO 时间戳 → aware datetime; 不可解析/naive → None (该行不可比, 跳过但不灭灯)。"""
    from datetime import datetime
    try:
        t = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return t if t.tzinfo is not None else None


def _load_jsonl_quiet(path: Path) -> tuple:
    """读 jsonl: (好行列表, 坏行数)。坏行跳过计数 — 单行损坏不灭整个哨兵。"""
    rows, bad = [], 0
    for ln in (path.read_text(encoding="utf-8") or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            bad += 1
    return rows, bad


def _check_chg_denominator(rec) -> dict:
    """W7: 昨收分母哨兵 — 两条取数路径的 chg 读数分叉即测量面口径分裂。全程只读 (除首跑建基线)。"""
    info = {"threshold_pp": _W7_THRESHOLD_PP, "window_min": _W7_WINDOW_MIN}
    try:
        if not (_W7_PMI.exists() and _W7_ETS.exists()):
            info["status"] = "skipped_no_shadow"
            return info
        pmi_rows, pmi_bad = _load_jsonl_quiet(_W7_PMI)
        ets_rows, ets_bad = _load_jsonl_quiet(_W7_ETS)
        info["bad_lines"] = {"pmi": pmi_bad, "ets": ets_bad}
        # 整档坏 = 哨兵失明面 100%, 不得读作"零分叉" (单行坏只计数)
        for label, rows, bad in (("pmi", pmi_rows, pmi_bad), ("ets", ets_rows, ets_bad)):
            if bad and not rows:
                rec("W7_detector_error", "-", label,
                    f"{_W7_ETS.name if label == 'ets' else _W7_PMI.name} 全部 {bad} 行不可解析"
                    f" → 昨收分母哨兵失明, 本次'零违反'不可信")
                info["status"] = "detector_error"
                return info
        # ets 双索引: inst_id 精确 (指令表唯一键) + (symbol,date) 兜底 (供 pmi 侧无 inst_id 的行)
        idx_inst, idx_sym = {}, {}
        for r in ets_rows:
            cp = (r.get("features") or {}).get("change_percent")
            if cp is None:
                continue
            t = _w7_ts(r.get("ts"))
            if t is None:
                continue
            date = str(r.get("ts"))[:10]
            idx_sym.setdefault((str(r.get("symbol") or ""), date), []).append((t, float(cp)))
            if r.get("inst_id") is not None:
                idx_inst.setdefault((r["inst_id"], date), []).append((t, float(cp)))
        divergent, paired, unpaired = {}, 0, 0
        for r in pmi_rows:
            chg = r.get("chg_pct")
            if chg is None:
                continue
            t0 = _w7_ts(r.get("ts"))
            date = str(r.get("date") or str(r.get("ts"))[:10])
            if t0 is None:
                unpaired += 1
                continue
            if r.get("inst_id") is not None:
                cands = idx_inst.get((r["inst_id"], date), [])
            else:
                cands = idx_sym.get((str(r.get("symbol") or ""), date), [])
            # 可比窗 ±10min: 触发评估与 09:35 快照的时差; 窗外行含日内价格漂移 → 不可比不判。
            # 窗内取最小 |Δchg|: 分母若一致, 最接近时刻的两次读数必 ≈0; 分母若分叉, 全日冻结必全发散。
            deltas = [abs(cp - float(chg)) for t, cp in cands
                      if abs((t - t0).total_seconds()) <= _W7_WINDOW_MIN * 60]
            if not deltas:
                unpaired += 1
                continue
            paired += 1
            dmin = min(deltas)
            if dmin > _W7_THRESHOLD_PP:
                inst = r.get("inst_id")
                vk = f"{date} {r.get('symbol')}#{inst if inst is not None else 'noinst'}"
                if dmin > divergent.get(vk, -1.0):
                    divergent[vk] = dmin
        keys = sorted(divergent)
        info.update({"paired": paired, "unpaired": unpaired,
                     "divergent_n": len(keys), "divergent": keys})
        if not _W7_BASELINE.exists():
            _W7_BASELINE.write_text(json.dumps({
                "divergent": keys,
                "established_at": now_et().isoformat(timespec="seconds"),
                "note": ("昨收分母哨兵基线 (DEB-20260911 共识 N1/N3, W2 同款棘轮纪律)。"
                         "divergent = 已知分叉存量 (date symbol#inst_id), 判据只用它; "
                         "refs = 诊断样本。只降不升: 分叉愈合后由人工收缩本文件, 守卫绝不自动改写。"
                         "改 divergent = 改判据, 须先落档 ADR/DEB 并在 refs 写裁决出处。"
                         "判据: |Δchg|>2pp, 可比窗 ±10min 窗内取最小差。"),
                "refs": {"2026-09-08 US.BIDU#2353": (
                    "DEB-20260911 C6: push 缓存昨收跨日不更新 (pmi 快照 -7.65% vs ctx "
                    "+4.07%, 7 秒内差 11.72pp, change_percent 全日冻结) — 立档实锤")},
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            info["status"] = "baseline_established"
            return info
        baseline = json.loads(_W7_BASELINE.read_text(encoding="utf-8"))
        known = set(baseline.get("divergent") or [])
        new = [k for k in keys if k not in known]
        info["silenced_known"] = len([k for k in keys if k in known])
        info["healed"] = sorted(known - set(keys))
        for k in new:
            rec("W7_chg_denominator_divergence", "-", k,
                f"两影子同标的同窗 chg 差 {divergent[k]:.2f}pp > {_W7_THRESHOLD_PP}pp "
                f"(pmi 快照昨收 vs ctx push 缓存昨收) → 昨收分母口径分叉, 该日所有 "
                f"ctx[change_percent] 消费者读数不可信; 已知存量登记 {_W7_BASELINE.name} "
                f"(改基线=改判据, 须先落档 DEB)")
        info["status"] = "drift" if new else "ok"
        return info
    except Exception as e:
        rec("W7_detector_error", "-", type(e).__name__,
            f"W7 分母哨兵自身异常: {e} → 昨收口径守卫失明, 本次'零违反'不可信")
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


def main(push_enabled: bool = True) -> int:
    """push_enabled=False 供测试/演练走完整违反路径而不真发钉钉 (直推是外部副作用)。"""
    since = (now_et().date() - timedelta(days=7)).isoformat()
    checked_at = now_et().isoformat()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM trade_records WHERE trade_date>=? AND source_type LIKE 'PLAYBOOK%' "
        "AND exit_time IS NOT NULL", (since,)).fetchall()
    c.close()

    viol: Counter = Counter()
    examples: dict = {}
    sample_exempt = 0

    def rec(k: str, rid, sym, msg: str):
        viol[k] += 1
        examples.setdefault(k, f"id={rid} {sym} {msg}")

    for r in rows:
        m = json.loads(r["metadata_json"] or "{}")
        if _is_neutral_data_quality(_col(r, "data_quality")):
            sample_exempt += 1
            continue
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
            if _is_neutral_data_quality(dq):
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

    # ── W2 字段基数棘轮 (2026-09-06, ADR-038 P7 军规自动化): 新塌缩字段当天告警 ──
    field_cardinality = _check_field_cardinality(rec)

    # ── W3 sim/live 账本混同守卫 (RFC-080 demix 2026-09-09): 双向检测, 复发当天告警 ──
    env_demix = _check_env_demix(rec)

    # ── W4/W5/W6 日志卫生棘轮 (2026-09-10, DEB-20260910 §8.2/§5): 基线外新增当天告警 ──
    log_hygiene = _check_log_hygiene(rec)

    # ── W7 昨收分母哨兵 (2026-09-11, DEB-20260911 共识 N1/N3): 分母口径分叉当天告警 ──
    chg_denominator = _check_chg_denominator(rec)

    # ── W8 费用异常单守卫 (2026-10-10, DEB-20261009-event §30, 用户裁「费用护栏同意」):
    #    仙股/最低佣金异常 (>200bps) 次日告警; 只读监控, 不拦单 ──
    fee_bps_anomaly = _check_fee_bps_anomaly(rec)

    # ── W1_* 关键违反直推: **必须在覆写 OUT 之前** (防刷屏判据读的是上一轮 OUT 里的
    #    w1_direct_push 状态; 先覆写就永远读到本轮自己 ⇒ 首次告警即被自我抑制)。──
    if push_enabled:
        w1_push = _push_w1_critical(viol, examples, checked_at)
    else:
        w1_push = {"keys": sorted(str(k) for k in viol if str(k).startswith(_W1_KEY_PREFIX)),
                   "attempted": False, "ok": None, "error": None,
                   "suppressed_reason": "push_enabled=False (测试/演练路径, 不发外部通知)"}

    result = {
        "checked_at": checked_at, "window_since": since, "sample": len(rows),
        "sample_exempt_marked": sample_exempt,
        "violations": dict(viol), "examples": examples, "field_health": field_health,
        "caliber_diff": caliber_diff,
        "config_freeze": config_freeze,
        "field_cardinality": field_cardinality,
        "env_demix": env_demix,
        "log_hygiene": log_hygiene,           # W4 debug吞异常 / W5 裸名logger / W6 无栈告警
        "chg_denominator": chg_denominator,   # W7 两影子昨收分母口径分叉 (pmi 快照 vs ctx 缓存)
        "fee_bps_anomaly": fee_bps_anomaly,   # W8 仙股/最低佣金异常 (>200bps, DEB-20261009-event §30)
        "w1_direct_push": w1_push,            # W1 事件型直推回执 (DEB-20260909 §7.7⑤)
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
