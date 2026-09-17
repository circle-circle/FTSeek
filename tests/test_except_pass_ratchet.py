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
BASELINE = 950  # 2026-09-15 DEB-20260914-cutover §十 调低 (951→950): 切换基建批的两个新脚本
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
                  "__pycache__", ".backup_premigration", "obsidian_vault"}
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
