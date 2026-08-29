"""T4 双轨契约守卫 (DEB-20260829-entry-zone 第 4 轮, 2026-08-29).

契约: `_check_intraday_trigger` 的触发几何只准以 `entry_price` 为锚按
entry_band.py 单一真源现算 (3%/5% 或 vol_widen), **永不读指令的
entry_low / entry_high** —— 落库区间是 DA 链 LLM 散文技术位, 仅作
展示/归因, 两者双轨运行是有意设计 (T1/T2/T4).

违反本契约 = 未经裁决把 LLM 未经验证的区间接入执行路径 (风险面变更,
须走 ADAPTATION-CONTROL-LAW 转移函数), 典型事故形态是有人"顺手统一
两套数值"修掉一个看似冗余的几何计算.

回滚: 删本文件即可, 零生产依赖.
"""
import inspect
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent / "app" / "playbook" / "pipeline.py"


def test_trigger_never_reads_stored_entry_band():
    src = inspect.getsource(__import__(
        "app.playbook.pipeline", fromlist=["_check_intraday_trigger"
                                           ""])._check_intraday_trigger)
    # 遥测透传 (锚限价影子落盘 "entry_low": inst.get(...)) 不算读 — 契约只禁
    # 触发几何消费. 断言收窄到判定段: 出现在比较/赋值右侧才算违约.
    import re
    _telemetry_passthrough = re.compile(r'"entry_(low|high)":\s*inst\.get\(')
    for line in src.splitlines():
        if ("entry_low" in line or "entry_high" in line) and not _telemetry_passthrough.search(line):
            raise AssertionError(
                f"_check_intraday_trigger 违反双轨契约 (DEB-20260829 T4): 触发几何开始\n"
                f"消费落库区间: {line.strip()}\n"
                "落库区间是 LLM 散文技术位, 不是执行几何; 收敛两套数值属风险面变更, "
                "须走适应律转移函数并修订本契约")


def test_entry_band_single_source_constants_intact():
    from app.playbook import entry_band
    assert entry_band.KNIFE_FLOOR_PCT == 0.03, (
        "knife floor 被改动 — 触发几何变更须走适应律 (DEB-20260829 T4 契约)")
    assert entry_band.CHASE_ZONE_PCT == 0.05, (
        "chase zone 被改动 — 触发几何变更须走适应律 (DEB-20260829 T4 契约)")
