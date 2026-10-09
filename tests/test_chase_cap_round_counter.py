"""chase_cap 同指令累计拦截轮次计数 钉子 (DEB-20260910-logging §11.8-3, 2026-09-11)

动机 (实测): 9/10 夜 `US.AMD SELL_SHORT` 被 chase_cap 连拦 **69 轮** (21:45→03:58, 每~5min 一轮),
每轮文案除价格外完全相同 ⇒ 值班人无法从单行分清"首次拦截"与"整夜拦了 69 次", 只能 grep 数行;
那 138 行 WARNING 占当晚 237 条的 58%。**门本身判对了** (SELL_SHORT $504-509 低于禁追线 $525,
做空不许追跌), 重评也是刻意设计 (9/8 SNDK 正是靠重评成交) ⇒ 本批**只动观测面, 零行为变更**。

三向钉住:
  ① 计数器自身语义 (按指令累计 / 互不串号 / 有界不无界增长)
  ② **消费侧不得炸组** — `daily_alert_summary._dedup_alerts` 的归一化必须吃掉裸整数轮次,
     否则日报里同一模板的 69 轮会从 1 行炸成 69 行 (这是本改动最大的连带风险)
  ③ 源级纪律 — 计数必须在 enforce 动作**之后**取 (不得影响判定), 且助手**不含 try/except
     也不碰 DB/文件/网络**: 调用点在 G-06 fail-close 的 try 内, 一旦抛异常 `_cc_gate_ok`
     停在 False ⇒ "本轮跳过"被升级成"终态跳过", 那就是真行为变更了。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.playbook import pipeline as pl  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "daily_alert_summary_for_cc", PROJECT_ROOT / "scripts" / "daily_alert_summary.py"
)
das = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(das)

_SRC_PIPE = (PROJECT_ROOT / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
# [RFC-060 phase1] pipeline 重构后 enforce 块/遥测文案迁入 pipeline_parts/auto_execute_flow.py;
# 本轮测试的计数/文案/分流锚字符串全在新宿主, 单独拼一份给源级纪律断言用。
_SRC_AEF = (
    PROJECT_ROOT / "app" / "playbook" / "pipeline_parts" / "auto_execute_flow.py"
).read_text(encoding="utf-8")
_SRC_ENFORCE = _SRC_PIPE + "\n" + _SRC_AEF


@pytest.fixture(autouse=True)
def _reset_counter():
    """每例前后清空进程内计数, 免用例间串号。"""
    pl._CHASE_CAP_ROUNDS.clear()
    yield
    pl._CHASE_CAP_ROUNDS.clear()


# ── ① 计数器语义 ─────────────────────────────────────────────────
def test_rounds_accumulate_per_instruction():
    assert pl._chase_cap_round_bump("inst-1") == 1
    assert pl._chase_cap_round_bump("inst-1") == 2
    assert pl._chase_cap_round_bump("inst-1") == 3


def test_instructions_do_not_cross_contaminate():
    """9/10 夜实况形态: AMD 连拦数十轮, 同时别的指令也在被拦 ⇒ 不得串号。"""
    assert pl._chase_cap_round_bump("AMD") == 1
    assert pl._chase_cap_round_bump("SKHY") == 1
    assert pl._chase_cap_round_bump("AMD") == 2
    assert pl._chase_cap_round_bump("SKHY") == 2
    assert pl._chase_cap_round_bump("AMD") == 3


def test_int_and_none_inst_id_are_safe():
    """inst_id 来自 DB 行 ⇒ 可能是 int / None; str() 后都必须能计数, 不得抛。"""
    assert pl._chase_cap_round_bump(1234) == 1
    assert pl._chase_cap_round_bump(1234) == 2
    assert pl._chase_cap_round_bump(None) == 1


def test_counter_is_bounded(monkeypatch):
    """长驻进程不得无界增长; 超限淘汰最旧键, 且**当前键计数不受影响**。"""
    monkeypatch.setattr(pl, "_CHASE_CAP_ROUNDS_MAX", 4)
    for i in range(10):
        pl._chase_cap_round_bump(f"old-{i}")
    assert len(pl._CHASE_CAP_ROUNDS) <= 4
    assert pl._chase_cap_round_bump("hot") == 1
    assert pl._chase_cap_round_bump("hot") == 2
    assert len(pl._CHASE_CAP_ROUNDS) <= 4


# ── ② 消费侧: 日报不得因轮次炸组 ────────────────────────────────
def _line(rounds: int, price: str) -> str:
    return (f"2026-09-10 21:45:26,973 [LLMStrategyLoop] [app.playbook.pipeline] WARNING: "
            f"[playbook] chase_cap拦截: US.AMD SELL_SHORT 下单价=${price} 越过禁追线 $525.00 "
            f"→ 本轮跳过 (每~5min重评, 回落帽下即放行; 同指令累计第{rounds}轮; P0-A enforce)")


def test_dedup_groups_all_rounds_into_one_alert():
    """**本改动最大的连带风险**: 轮次是裸整数, `_normalize` 原本只吃 %/$/股 三种单位。
    不补归一 ⇒ 69 轮变 69 组, 日报从 1 行炸成 69 行。"""
    alerts = [{"level": "WARNING", "content": _line(n, f"50{n}.52"),
               "time": f"2026-09-10 21:{45 + n}"} for n in range(1, 70)]

    grouped = das._dedup_alerts(alerts)

    assert len(grouped) == 1, f"同一模板被拆成 {len(grouped)} 组 ⇒ 日报炸组"
    assert grouped[0]["count"] == 69


def test_dedup_still_separates_different_templates():
    """补齐归一不得过头: 不同标的/不同门仍须分组 (否则日报漏报)。"""
    other = _line(1, "508.52").replace("US.AMD", "US.SKHY")
    grouped = das._dedup_alerts([
        {"level": "WARNING", "content": _line(1, "508.52"), "time": "t1"},
        {"level": "WARNING", "content": _line(2, "509.73"), "time": "t2"},
        {"level": "WARNING", "content": other, "time": "t3"},
    ])
    assert len(grouped) == 2
    assert sorted(g["count"] for g in grouped) == [1, 2]


# ── ③ 源级纪律 ──────────────────────────────────────────────────
def test_warning_carries_the_round_counter():
    assert "同指令累计第{_cc_rounds}轮" in _SRC_ENFORCE
    assert '_cc_rounds = _chase_cap_round_bump(inst["id"])' in _SRC_ENFORCE


def test_counter_taken_after_enforce_actions_not_before():
    """计数必须在落痕/审计**之后**取 — 观测面排在判定面后面, 不可能反过来影响判定。"""
    enforce_pos = _SRC_ENFORCE.index("if _cc_enforce:")
    audit_pos = _SRC_ENFORCE.index('action="skipped_chase_cap"', enforce_pos)
    bump_pos = _SRC_ENFORCE.index("_chase_cap_round_bump(inst[", enforce_pos)
    gate_ok_pos = _SRC_ENFORCE.index("_cc_gate_ok = True", enforce_pos)
    assert audit_pos < bump_pos < gate_ok_pos


def test_helper_is_exception_free_and_io_free():
    """G-06 fail-close 的 try 内抛异常 = 把"本轮跳过"升级成"终态跳过"。
    故助手必须既无 try/except (不吞也不新增异常面), 也不碰 DB/文件/网络。

    用 **AST 语义判定**而非子串匹配: 子串法会被 docstring 里"刻意不写 try/except"
    这句话自己绊倒 (本钉第一版正是这么红的), 且改个变量名就绕过去了。
    """
    import ast

    tree = ast.parse(_SRC_PIPE)
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "_chase_cap_round_bump"]
    assert len(fns) == 1, "助手函数被删/重名 ⇒ 观测面计数器失踪"
    fn = fns[0]

    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Try)], \
        "助手内出现 try/except ⇒ 要么在吞异常, 要么新增了异常面"

    used = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            used.add(n.id)
        elif isinstance(n, ast.Attribute):
            used.add(n.attr)
    forbidden = {"db", "open", "Path", "requests", "sqlite3", "json", "time",
                 "os", "socket", "urllib", "logging", "logger", "sleep"}
    hit = used & forbidden
    assert not hit, f"观测面计数器不得引入 I/O / 日志 / 阻塞: {sorted(hit)}"

    # 只允许内存字典操作 + str(): 白名单式钉住"不可能抛"的实现面
    # 注: `int` 来自返回注解 `-> int` (注解在 AST 里也是 Name), 属类型面非调用面
    allowed = {"str", "len", "next", "iter", "get", "pop", "int", "_CHASE_CAP_ROUNDS",
               "_CHASE_CAP_ROUNDS_MAX", "key", "rounds", "inst_id"}
    extra = used - allowed
    assert not extra, f"助手实现面超出白名单 (新增调用须重新评估会不会抛): {sorted(extra)}"


def test_enforce_decision_untouched():
    """零行为变更钉: 判定/落痕/终态三件套的原文案与次序一字未动。

    [RFC-120 2026-09-30] 放行文案升级为方向分流: SHORT cap=下限, "回落帽下即放行"
    对空单方向说反 → 值班人把结构性死锁误读成"等回落"。改钉新契约: 两个方向
    文案字面量都必须在源码中, 且由 _dir 分流选择。
    """
    assert "if _cc_violated(_dir, _new_entry, _cc_f):" in _SRC_ENFORCE
    assert 'chase_cap_enforced(reprice=' in _SRC_ENFORCE
    assert '"enforced": bool(_cc_enforce)' in _SRC_ENFORCE
    assert "→ 本轮跳过 (每~5min重评, {_cc_release}; " in _SRC_ENFORCE
    assert '"涨回帽上即放行"' in _SRC_ENFORCE   # SHORT 分支
    assert '"回落帽下即放行"' in _SRC_ENFORCE   # BUY 分支
    assert 'if str(_dir).upper() != "BUY"' in _SRC_ENFORCE  # 方向分流判据
    assert "P0-A enforce)" in _SRC_ENFORCE
