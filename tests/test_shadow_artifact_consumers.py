"""反向挂载哨兵 `check_artifact_consumers` 的口径契约 (2026-09-26, 坑2「哨兵单向」)

立起理由: `check_mounting` 只查「eval_script 没挂 cron ⇒ 悬空」(单向), **不查**
「你要摘的那条 cron 有没有生产读方」。实锤: universe_expansion_shadow 判据 FAIL 用户裁退役,
照判据原文「删 cron + 开关置 false, 零残留」准备摘 capture cron — 但 llm_universe_scan.py
`_gather_market_corroboration` fallback 只读 type=='capture' 行, DEB-20260914 实测该回退是
**常态路径** (da_feed 窗 ET 07:40 结构上永远早于生产者 DA ET 08:00), 且 crontab 有 inject
∧ entry_enabled=true ⇒ 摘 capture = 改 UE 交易行为, 远超「判据退役」授权。靠人工 grep 才发现。

本测试钉住: ① 基线干净 (不占周检噪音预算); ② 真问题必报 (**守卫有牙齿**);
③ 7 类假阳口径正确静默 (现役项/注释cron/写者/展示面/纯注释/已认领/DB口径)。
纪律: 基线 0 告警的守卫最可能是假绿 ⇒ 必须用变异测试证明它能告警 (memory: fixture全绿≠接线活)。
"""

from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import app.playbook.shadow_registry as sr  # noqa: E402


def _run_with(items=None, sources=None, cron=None, ack=None):
    """临时替换注册表/扫描面/cron面/豁免表, 跑守卫, 无论成败都恢复 (monkeypatch 语义)。"""
    saved = (sr.REGISTRY, sr._mounted_sources, sr._CONSUMER_ACK)
    orig_cron, orig_sources = saved[1]()
    try:
        if items is not None:
            sr.REGISTRY = items
        if sources is not None or cron is not None:
            sr._mounted_sources = lambda: (
                cron if cron is not None else orig_cron,
                sources if sources is not None else orig_sources)
        if ack is not None:
            sr._CONSUMER_ACK = ack
        return sr.check_artifact_consumers()
    finally:
        sr.REGISTRY, sr._mounted_sources, sr._CONSUMER_ACK = saved


def _fake_item(iid: str, status, data_path: str, eval_script=None):
    """克隆一个真实条目再改字段 (避免构造不全的 ShadowItem)。"""
    tpl = next(x for x in sr.REGISTRY if x.id == "universe_expansion_shadow")
    c = copy.copy(tpl)
    c.id, c.status, c.data_path, c.eval_script = iid, status, data_path, eval_script
    return c


# ── ① 基线 ──────────────────────────────────────────────────────────
def test_baseline_is_clean():
    """真实注册表下必须 0 告警 — 存量 4 条已逐条实测认领 (理由写在 _CONSUMER_ACK)。"""
    w = sr.check_artifact_consumers()
    assert w == [], f"基线应干净 (不占周检噪音预算), 实报 {len(w)} 条: {[x[:120] for x in w]}"


def test_baseline_is_fast_enough_for_weekly_checkpoint():
    """周检 check_shadow_registry() 的 subprocess timeout=90s ⇒ 守卫必须便宜。

    实测: 原型对每文件跑 tokenize = 111s (**会超时, 守卫自己变死门**);
    改「廉价粗筛 → 只 tokenize 命中文件」= ~1s (提速 102×, 结果一致)。
    """
    t = time.time()
    sr.check_artifact_consumers()
    dt = time.time() - t
    assert dt < 20, f"守卫耗时 {dt:.1f}s ⇒ 会挤爆周检 90s 预算 (含 refresh_counts/summary/intents)"


# ── ② 有牙齿: 真问题必报 ────────────────────────────────────────────
def test_terminal_with_active_cron_consumer_is_flagged():
    w = _run_with(
        items=[_fake_item("fake_x", sr.ShadowStatus.KILLED, "data/shadow/fake_x.jsonl")],
        sources={"/x/scripts/fake_prod.py": 'open("fake_x.jsonl").read()'},
        cron="0 5 * * 1-5 python scripts/fake_prod.py", ack={})
    assert len(w) == 1
    assert "fake_x" in w[0] and "fake_prod.py" in w[0]
    assert "判据退役 ≠ 采集退役" in w[0], "告警必须点明核心纪律, 否则读的人不知道该做什么"


def test_graduated_is_also_in_scope():
    """GRADUATED 同属终态 (毕业=已进生产, 产物被读虽正常但仍须显式认领)。"""
    w = _run_with(
        items=[_fake_item("fake_g", sr.ShadowStatus.GRADUATED, "data/shadow/fake_g.jsonl")],
        sources={"/x/scripts/fake_prod3.py": 'open("fake_g.jsonl").read()'},
        cron="0 5 * * 1-5 python scripts/fake_prod3.py", ack={})
    assert len(w) == 1 and "GRADUATED" in w[0]


def test_real_dependency_reappears_if_ack_removed():
    """最关键的一条: 撤掉 universe_expansion_shadow 的豁免 ⇒ 真生产依赖必须立刻报出来。"""
    w = _run_with(ack={k: v for k, v in sr._CONSUMER_ACK.items()
                       if k != "universe_expansion_shadow"})
    hit = [x for x in w if "universe_expansion_shadow" in x and "llm_universe_scan.py" in x]
    assert len(hit) == 1, f"真依赖 (UE 佐证段 fallback 原料) 未被抓到: {w}"


# ── ③ 假阳口径必须静默 ──────────────────────────────────────────────
def test_observing_item_is_not_flagged():
    """口径①: 现役项有消费者是正常的 (影子本该有读方) ⇒ 不报。"""
    w = _run_with(
        items=[_fake_item("fake_y", sr.ShadowStatus.OBSERVING, "data/shadow/fake_y.jsonl")],
        sources={"/x/scripts/fake_prod.py": 'open("fake_y.jsonl").read()'},
        cron="0 5 * * 1-5 python scripts/fake_prod.py", ack={})
    assert w == []


def test_commented_cron_is_not_active():
    """口径②: 消费者不在 cron 活跃面 (被注释) ⇒ 不断料, 不报。"""
    w = _run_with(
        items=[_fake_item("fake_z", sr.ShadowStatus.KILLED, "data/shadow/fake_z.jsonl")],
        sources={"/x/scripts/fake_prod.py": 'open("fake_z.jsonl").read()'},
        cron="# 0 5 * * 1-5 python scripts/fake_prod.py", ack={})
    assert w == []


def test_writer_is_not_a_consumer():
    """口径③: 与产物同名的脚本是**写者** ⇒ 不算消费者 (否则每条影子都自命中)。"""
    w = _run_with(
        items=[_fake_item("fake_w", sr.ShadowStatus.KILLED, "data/shadow/fake_w.jsonl")],
        sources={"/x/scripts/fake_w.py": 'open("fake_w.jsonl","a").write(x)'},
        cron="0 5 * * 1-5 python scripts/fake_w.py", ack={})
    assert w == []


def test_display_surface_is_not_a_consumer():
    """口径③: shadow_board/shadow_lexicon 只渲染看板不喂决策 ⇒ 不算生产消费者。"""
    for disp in ("shadow_board.py", "shadow_lexicon.py"):
        w = _run_with(
            items=[_fake_item("fake_v", sr.ShadowStatus.KILLED, "data/shadow/fake_v.jsonl")],
            sources={f"/x/scripts/{disp}": 'read("fake_v.jsonl")'},
            cron=f"0 5 * * 1-5 python scripts/{disp}", ack={})
        assert w == [], f"{disp} 被误判为生产消费者"


def test_pure_comment_mention_is_not_a_reference():
    """口径③: 纯注释提及不算引用 (复用 check_mounting 的 _code_only 口径)。"""
    w = _run_with(
        items=[_fake_item("fake_u", sr.ShadowStatus.KILLED, "data/shadow/fake_u.jsonl")],
        sources={"/x/scripts/fake_p2.py": "# 历史: 曾读 fake_u.jsonl 现已移除\ndef f(): pass"},
        cron="0 5 * * 1-5 python scripts/fake_p2.py", ack={})
    assert w == []


def test_db_backed_item_is_skipped():
    """DB 内口径的条目无产物文件可断供 ⇒ 跳过 (与 check_mounting 同口径)。"""
    w = _run_with(
        items=[_fake_item("fake_db", sr.ShadowStatus.KILLED, "data/ftseek_us.db (表 x)")],
        sources={"/x/scripts/fake_prod.py": 'q("data/ftseek_us.db")'},
        cron="0 5 * * 1-5 python scripts/fake_prod.py", ack={})
    assert w == []


# ── ④ 豁免表自身的纪律 ──────────────────────────────────────────────
def test_ack_entries_must_have_reason():
    """豁免必须写理由, 否则守卫失去意义 (空串/缺理由 = 批量豁免的后门)。"""
    assert sr._CONSUMER_ACK, "豁免表不应为空 (至少 universe_expansion_shadow 是合法保留)"
    for k, v in sr._CONSUMER_ACK.items():
        assert isinstance(v, str) and len(v.strip()) >= 40, f"{k} 的豁免理由过短/缺失: {v!r}"
        assert any(x in v for x in ("实测", "cron", "报表", "prompt", "grep")), \
            f"{k} 的豁免理由缺取证依据 (须写明是实测/grep 得来的, 不是拍脑袋)"


def test_ack_ids_must_exist_in_registry():
    """豁免表里的 id 必须真实存在 (防拼错导致豁免静默失效 = 噪音回流)。"""
    ids = {x.id for x in sr.REGISTRY}
    for k in sr._CONSUMER_ACK:
        assert k in ids, f"_CONSUMER_ACK 里的 {k!r} 不在注册表 ⇒ 豁免失效, 拼写错误?"


def test_ack_does_not_hide_unclaimed_new_cases():
    """豁免表只准含已实测认领的条目 (防后人批量塞 id 绕过守卫)。

    2026-10-03 清理批新增 4 条 (REVIEW-20261003, 各自 verdict_history 留证据):
    premarket_peer_shadow / ue_midday_shadow / ue_earnings_clue_shadow /
    eth_ext_entry_prefilter — 均为「判据终态但写方代码引用残留」型, 逐条 grep 实测后认领。
    2026-10-15 新增 ue_short_boost_shadow (审计 P1-3 结案): 消费者=shadow_board 只读展示
    + 一次性结算器, 写方链已退役 ⇒ 归档展示型, 逐条实测后认领。
    """
    expected = {"universe_expansion_shadow", "limit_entry", "da_glm_shadow",
                "da_prompt_distill_shadow", "premarket_peer_shadow",
                "ue_midday_shadow", "ue_earnings_clue_shadow", "eth_ext_entry_prefilter",
                "ue_short_boost_shadow"}
    assert set(sr._CONSUMER_ACK) == expected, (
        f"豁免表新增了未认领条目: {set(sr._CONSUMER_ACK) - expected} — "
        f"每条必须逐条实测 (消费者是终点报表还是生产门) 后才可加, 禁批量豁免")


def test_terminal_status_constant_matches_lifecycle():
    """终态集合必须与文件头声明的生命周期一致 (GRADUATED/KILLED/SUPERSEDED)。"""
    assert set(sr._TERMINAL_STATUS) == {sr.ShadowStatus.GRADUATED, sr.ShadowStatus.KILLED,
                                        sr.ShadowStatus.SUPERSEDED}
    assert sr.ShadowStatus.OBSERVING not in sr._TERMINAL_STATUS
    assert sr.ShadowStatus.READY_TO_EVALUATE not in sr._TERMINAL_STATUS


# ── ⑤ 周检接线 (防「产物被仲裁丢弃」= 死门) ─────────────────────────
def test_weekly_checkpoint_consumes_the_guard():
    """周检必须真消费 consumers 键 — 只算不推 = 死门 (memory: 产物被仲裁丢弃)。"""
    p = REPO / "ops" / "hermes" / "ftseek_weekly_checkpoint.py"
    src = p.read_text(encoding="utf-8")
    assert "check_artifact_consumers" in src, "周检未调用守卫"
    assert "'consumers': check_artifact_consumers()" in src, "守卫结果未进 JSON"
    assert 'reg.get("consumers"' in src, "consumers 键未被消费 ⇒ 算了但不推 = 死门"
