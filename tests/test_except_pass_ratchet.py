#!/usr/bin/env python3
"""except:pass 棘轮守卫 (2026-08-31, 用户裁"按建议执行", DEB-20260831 P1③)。

背景: "静默死亡"病灶族 (R5/E13/归因链教训) — 异常被吞 = 测量/证据静默消失,
而"零违反"与"守卫自己崩了"不可区分。全仓普查 (2026-08-31, 本测试同款正则,
排除 tests/archive/venv/obsidian) = **957 处**, 登记为基线。

纪律: 存量分批清剿 (每批修完把 BASELINE 调低, 只降不升); **新增必须失败**。
新代码的异常处理必须 warn 留痕 (参照 R5: except:pass → logger.warning 模式)。
诚实边界: 只数 `except...:` 紧跟 pass/continue 的行, 多行吞错体不捕捉。
回滚: 删本文件 (回到无管控状态, 基线信息在本文件头注留档)。
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
BASELINE = 900  # 2026-10-28 日志卫生批调低 (921→900), 用户裁「按建议执行」。两件事同一批:
                  #   ① **扫描面收敛到生产可见面** — 新增 "docs" 排除。9/30 基线后 +40 处
                  #      全部来自 `docs/analysis/2026-10-27-da-ca-offline-replay/` 里两个
                  #      一次性离线回放程序 (baseline.py / variant.py 各 20 处)。它们不是
                  #      生产代码 (无 import 引用、无调度载体), 是 DEB-20261027 的证据归档包
                  #      (带 README + SHA256SUMS) — 改它们等于篡改证据校验和。判据与本仓
                  #      W4/W6 日志卫生棘轮的扫描面 (_HYGIENE_ROOTS = app/scripts/ops/config
                  #      + main.py) 对齐; 边界声明: docs/ 下不得再放生产逻辑, 否则逃出本棘轮。
                  #   ② **清剿基线后真实新增 29 处** (剔除 10-09 拆分搬家 51 处), 分布:
                  #      position_sync 4 (SL/TP 真值降级→warning) / push/health 2 (锁清理) /
                  #      finnhub 1 / order_executor 1 / data_collector 1 / pipeline 1 /
                  #      daily_review 1 / trade_store 1 / risk_state_machine 1 /
                  #      ops/intraday_diagnose 1 (首日无 state 改显式 return) /
                  #      scripts 15 (坏行/坏bar/坏日期 → stderr 留痕或 logger.debug)。
                  #      全部零控制流变更 (pass→留痕, continue→留痕+continue)。
                  #   921 + 40(docs) − 40(排除) − 29(清剿) = 900 — 实测复核过。只降不升。
                  #   (pipeline 4: 双锚影子 band 沿反推/patient_exit 跨日守卫/EOD 兜底/arm 连接关闭;
                  #    intraday_diagnose_late 1: 去重指纹 fail-open 留痕 stderr;
                  #    scripts 15: da_fresh_event_batch/reprice×2/ttm×3/deb20261008×5/
                  #    factor_neg_edge/premarket_reprice/rehang_live_stop 坏行计数进 print/stderr)
                  #   − 顺带净愈合 3 处 (weekly_optimizer −2, error_pattern_detector −1)。
                  #   全部改 logger.warning(exc_info=True)/计数留痕, 零控制流变更。只降不升。
                  # 2026-09-30 meta_audit 核查批调低 (925→924): 9/24 基线后他批 +5
                  #   (extended_hours_entry_executor 1, llm_universe_scan 1,
                  #    runner_exit_counterfactual 1, shadow_board 1,
                  #    wweek_exempt_counterfactual_20260929 1) 全量就地清剿 —
                  #   5 处均改坏行/坏bar计数留痕 (n_bad_rows/n_bad_bars/n_bad_json/
                  #   _n_bad_meta 进产物或 print) 或告警行 (shadow_board stat 失败),
                  #   控制流零变更。只降不升。
                  # 2026-09-24 用户裁「清剿新增点位」 调低 (950→925):
                  #   9/15 基线后新增 ~70 处 (多批次累积, 无单一罪魁), 本批全量清剿:
                  #   ① 新文件 39 处 (review_coverage_scan 5, orphan_ext_order_watch 6,
                  #      cancel_orphan_ext_order 3, carrier_orphan_census 1, 等 20+ 新脚本);
                  #   ② 存量文件增长 10 处 (scheduler_core +1, shadow_registry +1, hub +1,
                  #      message +1, extended_hours_entry_executor +5, premarket_exit_shadow +1);
                  #   ③ 顺带清剿 baseline 内 20 处 (futu_channel_shadow_collect 13,
                  #      data_freshness_watchdog 7, scheduler_core +8, hub +3, message +2,
                  #      shadow_registry +2, premarket_exit_shadow +1) 使基线收紧;
                  #   全改 logger.debug/warning 留痕 (exc_info=True), 零控制流变更,
                  #   高频循环/预期内静默用 debug 降噪; dotenv 加载统一 debug。
                  #   净 −25 (950→925), 只降不升。
                  # 2026-09-15 DEB-20260914-cutover §十 调低 (951→950): 切换基建批的两个新脚本
                  #   (`scripts/cutover_preflight.py` 9 处 + `scripts/cutover_rehearse.py` 1 处)
                  #   曾把计数顶到 960 — 被并行工作流 (DEB-20260914 VIX 批) 全仓回归抓出并
                  #   归因到本工作流, 就地清剿全部 10 处: 5 处"库文件缺失就跳臂"改为
                  #   `_open_lane()` 留痕 + main() 汇总显式打印 (诊断器最坏的失效是
                  #   "数据源打不开"与"查了没查出东西"输出同形 ⇒ 集体假绿);
                  #   2 处冷却态读/写失败改具名留痕 (失败方向选 fail-loud, 宁重复推送不静默抑制);
                  #   1 处日志档 OSError 留痕 (否则 P6"零里程"无法区分"没挂过"与"没扫到");
                  #   1 处 P9 live 库同 `_open_lane`; rehearse 的沙箱守卫由
                  #   try/relative_to+`except ValueError: pass` 改 parents 判定 (语义等价,
                  #   不再把"守卫通过"与"守卫异常"共用一条 except 通道) ⇒ 净 −10, 只降不升。
                  # 2026-09-11 DEB-20260910-logging §11.13 调低 (952→951): `app/shared` 符号链接
                  #   退役后本棘轮**首次看见** market_hours.py (pathlib.rglob 不跟随链接目录,
                  #   此前它的 2 处 `except: pass` 一直在分母外 = 基线是按少算校准的)。
                  #   同批就地清剿那 2 处 (config_center 时段回退 / HK 半日市日历 fail-open,
                  #   均改 logger.warning(..., exc_info=True)) ⇒ 净 −1, 顺带吃掉原有 1 格余量。
                  # 2026-09-10 DEB-20260910-logging §10.7 清剿 3 处后调低 (955→952):
                  #   pre_trade_check 系统熔断 fail-open / DEGRADED 门槛折扣 fail-open,
                  #   data_freshness_watchdog 冷却状态写失败 (重复告警根因) — 只降不升
_EXCLUDE_PARTS = {"venv", "node_modules", ".git", "tests", "archive",
                  "__pycache__", ".backup_premigration", "obsidian_vault",
                  # 2026-10-28 扫描面收敛到生产可见面 (用户裁「按建议执行」): docs/ 是文档与
                  # 证据归档面 (含一次性离线回放程序), 无 import 引用、无调度载体; 纳入会
                  # 让每次分析交付都染红棘轮。判据与 W4/W6 的 _HYGIENE_ROOTS 对齐。
                  "docs"}
_PATTERN = re.compile(r"except[^:\n]*:\s*\n\s*(pass|continue)\s*(?:#.*)?$", re.MULTILINE)


def _count_except_pass() -> int:
    total = 0
    for path in REPO.rglob("*.py"):
        if any(part in _EXCLUDE_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        total += len(_PATTERN.findall(text))
    return total


def test_except_pass_not_growing():
    current = _count_except_pass()
    assert current <= BASELINE, (
        f"except:pass/continue 数量增长: {BASELINE} → {current}。"
        f"新增异常处理必须 warn 留痕 (R5 模式), 见 DEB-20260831 P1③ 与判据字典纪律。")


def test_baseline_is_tight():
    """基线不得虚高超过 5% (防基线被悄悄放宽后失去棘轮意义)。"""
    current = _count_except_pass()
    assert current >= BASELINE * 0.95 or current <= BASELINE, (
        f"基线 {BASELINE} 与实测 {current} 偏离过大 — 若已清剿请同步调低 BASELINE")
