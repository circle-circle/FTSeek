"""DEB-20261002-input-validity P0-2 守卫: 及时性层不得再出现「未核实的自我声明」。

病灶 (2026-10-02 实测): `delay_ms` 在三条取数路径上被硬编码成字面量
"<100"/"<100"/"<10" (futu_lv3_provider.py:966/:1008/:1063), 下游
market_data_router.py:271 渲染进日志、:589 落 `ts_ctx["lv3_delay_ms"]`
⇒ 「及时性」这一层此前**零真实观测**, 只有未经核实的声明。
全仓 grep 确认 `delay_ms` **无任何门消费方** (只渲染 + 落 ts_ctx,
且 `lv3_delay_ms` 不进 prompt) ⇒ 改值是纯遥测, 零行为变更。

⚠️ 语义区分 (本层此前把两者混为一谈, 这正是错的根源):
  · `delay_ms`     = 链路投递延迟 — 需要交易所**发送**时戳, 我方拿不到 ⇒ 不可测;
  · `quote_age_ms` = 报价陈旧度 (交易所 `update_time` → now) — **可测**, 且这才是
    SSOT 新鲜度门 (`candidate_eligibility._source_time` → `stale_quote`) 量的那个量。

本测试钉住 (照 RFC-068 守卫惯例, 含变异反证证明测试能咬人):
  1. 三条路径的 `delay_ms` 均为 "unmeasured" — 诚实标注不可测, 不再编数字
  2. 新增 additive 字段 `quote_age_ms` 承载真观测值
  3. `_measured_quote_age_ms` 复用 SSOT `_source_time`: ET-naive 串按 ET 解析
     (按本机 CST 解析会凭空多算 12h ⇒ 全批假陈旧), 无 12h 偏差
  4. fail-open: None/""/垃圾串 → None (不抛异常、不编数字)
  5. router 透传 `lv3_quote_age_ms` 且兜底默认不再是假数字 "<10"
  6. 防覆盖集合含新键 (否则 TwelveData 合并会把它覆盖掉)
  7. 变异反证: 把字面量改回 "<100" → 测试 1 必须红
"""
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz

from app.data import futu_lv3_provider as _prov_mod
from app.data import market_data_router as _mdr_mod
from app.data.futu_lv3_provider import _measured_quote_age_ms

_PROV = Path(_prov_mod.__file__)
_MDR = Path(_mdr_mod.__file__)
_ET = pytz.timezone("America/New_York")


# ── 1/2/7: 源码级契约 — 三处均不得再写自我声明的字面量 ──
def test_no_hardcoded_delay_claim_literals_remain():
    """三条路径的 delay_ms 硬编码字面量必须清零 (只允许出现在注释里)。"""
    src = _PROV.read_text(encoding="utf-8")
    # 逐行扫: 排除注释行 (# 开头), 只查真赋值
    bad = []
    for i, line in enumerate(src.split("\n"), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if re.search(r'"delay_ms"\s*(:|\])\s*=?\s*"<\d+"', stripped):
            bad.append(f":{i} {stripped.strip()}")
    assert not bad, f"🔴 delay_ms 仍写死自我声明字面量 (及时性=零观测):\n  " + "\n  ".join(bad)


def test_three_paths_write_unmeasured_and_age_field():
    """三条路径 (push / subscribed / lv3) 都写 unmeasured + additive quote_age_ms。"""
    src = _PROV.read_text(encoding="utf-8")
    n_unmeasured = len(re.findall(r'"delay_ms"\s*(?::\s*"unmeasured"|\]\s*=\s*"unmeasured")',
                                  src))
    n_age = len(re.findall(r'"quote_age_ms"\s*:\s*_measured_quote_age_ms|'
                           r'quote\["quote_age_ms"\]\s*=\s*_measured_quote_age_ms', src))
    assert n_unmeasured == 3, f"期望 3 处 unmeasured, 实 {n_unmeasured}"
    assert n_age == 3, f"期望 3 处 quote_age_ms 观测赋值, 实 {n_age}"


def test_mutation_counterproof_literal_would_be_caught():
    """变异反证: 把字面量改回 "<100" 后, 测试 1 的判据必须命中 (证明能咬人)。"""
    src = _PROV.read_text(encoding="utf-8")
    mutated = src.replace('"delay_ms": "unmeasured"', '"delay_ms": "<100"', 1)
    assert mutated != src, "变异未生效 (源里没有该锚点) ⇒ 测试失去意义"
    bad = []
    for i, line in enumerate(mutated.split("\n"), 1):
        s = line.lstrip()
        if s.startswith("#"):
            continue
        if re.search(r'"delay_ms"\s*(:|\])\s*=?\s*"<\d+"', s):
            bad.append(i)
    assert bad, "🔴 变异后判据没命中 ⇒ 测试 1 是假绿"


# ── 3/4: helper 行为契约 — SSOT 复用 + fail-open ──
def test_et_naive_stamp_has_no_12h_offset():
    """ET-naive 串 (futu update_time 真实形态) 必须按 ET 解析, 不得凭空多 12h。

    这是 SSOT `_source_time` 的隐藏约定; 自写解析按本机 CST 会 +12h ⇒ 全批假陈旧
    (影子 ue_quote_freshness_shadow._age_sec docstring 逐字警告过此事)。
    """
    five_min_ago_et = (datetime.now(_ET).replace(tzinfo=None) - timedelta(minutes=5))
    age_ms = _measured_quote_age_ms(five_min_ago_et.strftime("%Y-%m-%d %H:%M:%S"))
    assert age_ms is not None, "ET-naive 串解析失败 ⇒ fail-open 掩盖了真问题"
    # 容差 ±90s; 若有 12h (43200s) 偏差会远超
    assert 300_000 - 90_000 <= age_ms <= 300_000 + 90_000, (
        f"期望 ~300000ms (5分钟), 实 {age_ms}ms — 疑似 12h 时区偏差 (未复用 SSOT)")


def test_unix_float_stamp_supported():
    """unix float 时戳同样支持 (adaptive_polling 那条流写的是 float)。"""
    age_ms = _measured_quote_age_ms(time.time() - 10)
    assert age_ms is not None
    assert 10_000 - 3_000 <= age_ms <= 10_000 + 3_000, f"期望 ~10000ms, 实 {age_ms}ms"


def test_fail_open_on_missing_or_garbage():
    """不可得/垃圾串 → None (fail-open): 缺数据不编数据, 也不抛异常炸取数路径。"""
    for bad in (None, "", "   ", "garbage", "2026-13-45 99:99:99"):
        assert _measured_quote_age_ms(bad) is None, f"{bad!r} 未 fail-open"


def test_return_type_is_int_ms():
    """返回 int 毫秒 (与 ts_ctx 其余数值字段同型, 便于下游直接比较/落库)。"""
    v = _measured_quote_age_ms(time.time() - 3)
    assert isinstance(v, int) and not isinstance(v, bool)


# ── 5/6: router 透传契约 ──
def test_router_passes_through_age_and_no_fake_default():
    """router 落 lv3_quote_age_ms, 且 lv3_delay_ms 兜底默认不再是假数字。"""
    src = _MDR.read_text(encoding="utf-8")
    assert 'ts_ctx["lv3_quote_age_ms"] = futu_quote.get("quote_age_ms")' in src, (
        "🔴 router 未透传真观测字段 ⇒ 及时性在落库侧仍是盲区")
    # 兜底默认不得再是 "<10" 这种未核实声明
    # ⚠️ 消息串用单引号 + 变量拼接: 直接写 "... "<10" ..." 会破坏引号 (本次真踩过)
    _fake_default = '"<10"'
    assert 'ts_ctx["lv3_delay_ms"] = futu_quote.get("delay_ms", "<10")' not in src, (
        f"🔴 router 仍用 {_fake_default} 假数字兜底")
    assert 'ts_ctx["lv3_delay_ms"] = futu_quote.get("delay_ms")' in src, (
        "🔴 取不到应落 None, 不是编数字")


def test_merge_guard_covers_new_field():
    """TwelveData 合并的防覆盖集合须含新键, 否则真观测值会被 td_ctx 覆盖掉。"""
    src = _MDR.read_text(encoding="utf-8")
    m = re.search(r"_futu_price_keys\s*=\s*\{(.*?)\}", src, re.S)
    assert m, "找不到 _futu_price_keys 防覆盖集合"
    assert '"lv3_quote_age_ms"' in m.group(1), (
        "🔴 新字段未入防覆盖集合 ⇒ 合并路径下真观测值会被覆盖")
