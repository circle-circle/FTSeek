"""[P2.1 2026-09-12, proud-slope-falcon] UE 材料索引 — gather 现场登记, 取消行前缀猜测。

病灶 (只读实测, 见 scripts/llm_universe_scan.py `_BRIEF_EVIDENCE` 注释):
  旧 `_build_clue_brief` 尾部把 parts 按 "\\n" 拆行, 再用 `not line.startswith("[")`
  排除节标题 —— 该谓词同时误杀了四类**以 `[` 开头的事实行**:
    `[CRITICAL|HIGH|MEDIUM/…]`   _gather_hard_events      (分类新闻硬事件)
    `[HIGH/EARNINGS]`            _gather_earnings_events  (财报已发布)
    `[MEDIUM/EARNINGS_UPCOMING]` _gather_earnings_events  (财报临近)
    `[HIGH/INSIDER_CLUSTER]`     _gather_insider_clusters (内部人集群)
  这四类正是 SYSTEM_PROMPT_V2 要求模型为 self_tier=HARD_EVENT 去锚定 evidence_ref 的材料。
  生产实测 data/shadow/llm_universe_scan*.jsonl: 347 行带 evidence_grounded, 其中
  grounded=False 32 行 (9.2%), **31 行 (96.9%) 是 HARD_EVENT 逐字引用上述四类行**。

本文件钉三件事:
  ① 那四类事实现在**在索引里** (缺陷已修);
  ② 索引与正文**逐条对应** — 每条事实的 text 逐字出现在 brief 里, 反之 brief 里每条
     可引用行都有一条事实 (P2 验收: "引证统计与实际送入模型的材料逐条对应");
  ③ 渲染文本**零漂移** — P2.1a 只改材料索引来源, 不改送进模型的正文。
"""
import math
import os
import sys
from datetime import datetime, timedelta
from email.utils import format_datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.llm_universe_scan as ue  # noqa: E402


# ── 冻结夹具: 每个源给一条**与生产 f-string 逐字同形**的事实 ──────────────────
HARD_LINE = "[CRITICAL/REGULATION] FDA 拒绝批准主力药 → 关联: VRTX"
EARN_REPORTED = "[HIGH/EARNINGS] NVDA 财报已发布 2026-09-10(amc) → 关注财报后动量"
EARN_UPCOMING = "[MEDIUM/EARNINGS_UPCOMING] ORCL 财报临近 2026-09-15(2d) → 时点风险"
INSIDER_LINE = "[HIGH/INSIDER_CLUSTER] BABA 内部人集群买入信号 x1 → 看多"
K8_LINE = "8-K BBBY: 8.01 其他事件 (filed 2026-09-11T16:20)"
DG_LINE = "举牌 CNO: SC 13D (主动举牌, 看多先验强) (filed 2026-09-11T09:00)"
MACRO_LINE = "非农新增 15.2 万 (预期 13.0 万) → 利率敏感板块承压"
EVENT_SYM_LINE = "- TSLA (score=8): 交付超预期"
PEER_LINE = "电动车峰群 verdict=CONFIRM_DOWN: NIO-3.7%(量比2.80)"
RANK_LINE = "- SOLS 盘前涨#1 +12.8% 盘前成交$18.4M 市值$3.1B"
CORRO_IN = "轮入板块: 半导体, 能源"
# ⚠ 用**生产真实格式** (`_gather_market_corroboration` fallback 路径的
#   f"{symbol} {direction} {pct:+}%"), 不是臆想的 "ESTC+19.3%"。曾因为夹具用了臆想格式,
#   而渲染现场又按另一种臆想格式 (`split(":")[0].split("(")[0]`) 反解 symbols, 两边错得
#   不一样却互相看不见 ⇒ "整串当 ticker" 的缺陷在 60 例绿里活了下来 (见
#   TestMoversSymbolSource)。夹具必须与生产同形, 否则钉的是自己的想象。
CORRO_MOVERS = "宇宙外异动: ESTC UP +19.3%, SOLS DOWN -12.8%"
CORRO_MOVER_SYMBOLS = ["ESTC", "SOLS"]
FEEDBACK_LINE = "- 2026-09-10 ORCL SHORT: -1.2% ✗"

# 新闻行的 "(Nh前)" 时效标签由 `_build_clue_brief` 用**真实 now** 算 (stub `_today()`
# 不影响它), 故 published_at 必须相对 now 构造, 期望串才确定 — 硬编码 "3h前" 会在
# 跨小时边界时假红。取 now-3h ⇒ `f"{3.0:.0f}"` 稳定渲染 "3h前"。
_NEWS_PUB_DT = datetime.now().astimezone() - timedelta(hours=3)
_NEWS_PUB = format_datetime(_NEWS_PUB_DT)
NEWS_LINE = "- Fed 释放降息信号 (3h前)  [SPY, QQQ]"

HEADER_HARD = "[硬事件 (已分类, 传导优先级最高)]"
HEADER_NEWS_PREFIX = "[近24h 新闻头条"
DATE_LINE_PREFIX = "日期: "
FEEDBACK_RULE_PREFIX = "规则: 对上表中连续推错"


def _facts():
    return {
        "hard": ue._fact("hard_event_classified", HARD_LINE, symbols=["VRTX"],
                         event_key="CRITICAL/REGULATION"),
        "earn1": ue._fact("earnings_calendar", EARN_REPORTED, symbols=["NVDA"],
                          published_at="2026-09-10", event_key="EARNINGS"),
        "earn2": ue._fact("earnings_calendar", EARN_UPCOMING, symbols=["ORCL"],
                          published_at="2026-09-15", event_key="EARNINGS_UPCOMING"),
        "insider": ue._fact("insider_cluster", INSIDER_LINE, symbols=["BABA"],
                            published_at="2026-09-12", event_key="INSIDER_CLUSTER"),
        "k8": ue._fact("sec_8k", K8_LINE, symbols=["BBBY"],
                       published_at="2026-09-11T16:20", event_key="sec_8k"),
        "dg": ue._fact("sec_13dg", DG_LINE, symbols=["CNO"],
                       published_at="2026-09-11T09:00", event_key="SC 13D"),
        "macro": ue._fact("macro_brief", MACRO_LINE, published_at="2026-09-12",
                          event_key="macro"),
        "esym": ue._fact("event_symbols", EVENT_SYM_LINE, symbols=["TSLA"],
                         published_at="2026-09-12", event_key="event_symbol"),
        "peer": ue._fact("peer_surge", PEER_LINE, symbols=["NIO"],
                         published_at="2026-09-12", event_key="peer_surge/电动车"),
        "rank": ue._fact("premarket_rank", RANK_LINE, symbols=["SOLS"],
                         published_at="2026-09-12", event_key="premarket_rank/涨"),
        "corro_in": ue._fact("market_corroboration", CORRO_IN,
                             published_at="2026-09-12",
                             event_key="sector_rotation_in"),
        "corro_mv": ue._fact("market_corroboration", CORRO_MOVERS,
                             symbols=["ESTC", "SOLS"], published_at="2026-09-12",
                             event_key="out_of_universe_movers"),
        "news": ue._fact("news_headline", NEWS_LINE, symbols=["SPY", "QQQ"],
                         published_at=_NEWS_PUB, event_key="news"),
        "fb": ue._fact("feedback_table", FEEDBACK_LINE, symbols=["ORCL"],
                       published_at="2026-09-10", event_key="feedback/SHORT"),
    }


# 每类事实 → 它在 brief 里所属的段 (用于逐条对应断言)
ALL_FACT_LINES = [
    HARD_LINE, EARN_REPORTED, EARN_UPCOMING, INSIDER_LINE, K8_LINE, DG_LINE,
    MACRO_LINE, EVENT_SYM_LINE, PEER_LINE, RANK_LINE, CORRO_IN, CORRO_MOVERS,
    NEWS_LINE, FEEDBACK_LINE,
]


@pytest.fixture
def stubbed_brief(monkeypatch, tmp_path):
    """把所有 gather 换成冻结事实, 并把 data/ 副作用改道到 tmp。

    ⚠ `_build_clue_brief` 有三处生产写/读副作用 (event_symbols / premarket_rank /
      ue_corro_syms 落盘 + 财报影子 jsonl), 必须改道或 stub —— 否则测试会写生产 data/
      (conftest 硬闸会当场报错, 但报错不如一开始就不碰)。
    """
    f = _facts()
    monkeypatch.setattr(ue, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ue, "_today", lambda: "2026-09-12")

    cfg = {
        "universe_expansion.news_lookback_hours": 24,
        "universe_expansion.clue_hard_events": True,
        "universe_expansion.clue_earnings_events": True,
        "universe_expansion.clue_insider_clusters": True,
        # 8-K / 13D-G 走真实 EDGAR provider, 这里关掉网络路径, 改用 hard 段注入同形行
        "universe_expansion.clue_8k_events": False,
        "universe_expansion.clue_13dg_events": False,
        "universe_expansion.clue_macro_brief": True,
        "universe_expansion.clue_event_symbols": True,
        "universe_expansion.clue_peer_surge": True,
        "universe_expansion.clue_premarket_rank": True,
        "universe_expansion.clue_feedback": True,
        # regime 段要拉 MarketRegimeGate (AlphaVantage), 关掉
        "universe_expansion.scan_regime_brief": False,
        # P2.2 输入初筛会取真实行情 — 本文件只钉 P2.1a (索引/零漂移), 关掉以保持
        #   "正文零漂移"断言的语义纯净; P2.2 由 TestInputPrescreen 用假路由单独覆盖。
        "universe_expansion.input_prescreen": False,
    }
    monkeypatch.setattr(ue, "_cfg", lambda k, d=None: cfg.get(k, d))
    # 本文件钉 P2.1a 的"正文零漂移"; event_reaction_v1 的行由
    # tests/test_event_reaction.py + 生产 live 验证覆盖, 此处关掉以保持夹具逐字。
    monkeypatch.setattr(ue, "_build_reaction_ctx", lambda: None)

    monkeypatch.setattr(ue, "_gather_news", lambda hours: [
        {"title": "Fed 释放降息信号", "symbols": '["SPY","QQQ"]',
         "published_at": _NEWS_PUB}])
    monkeypatch.setattr(ue, "_gather_hard_events", lambda news: [f["hard"]])
    monkeypatch.setattr(ue, "_gather_earnings_events", lambda: [f["earn1"], f["earn2"]])
    monkeypatch.setattr(ue, "_gather_insider_clusters", lambda: [f["insider"]])
    monkeypatch.setattr(ue, "_latest_insider_report_age", lambda: 0)
    # 8-K/13DG 段被 cfg 关掉, 这两行改由 macro 段之外的 hard 列表补入以覆盖同形文本
    monkeypatch.setattr(ue, "_gather_macro_brief",
                        lambda: [f["k8"], f["dg"], f["macro"]])
    monkeypatch.setattr(ue, "_gather_event_symbols_file", lambda: [f["esym"]])
    monkeypatch.setattr(ue, "_gather_peer_surge", lambda: [f["peer"]])
    monkeypatch.setattr(ue, "_gather_premarket_rank_clues", lambda: [f["rank"]])
    monkeypatch.setattr(ue, "_gather_market_corroboration", lambda: {
        "date": "2026-09-12", "strong_sectors": ["半导体", "能源"],
        "weak_sectors": [],
        "movers": ["ESTC UP +19.3%", "SOLS DOWN -12.8%"],
        "mover_symbols": list(CORRO_MOVER_SYMBOLS),
        "intraday_pulse": []})
    monkeypatch.setattr(ue, "_build_feedback_table", lambda *a, **k: [f["fb"]])
    monkeypatch.setattr(ue, "_gather_earnings_shadow", lambda syms: None)
    return f


# ══ 1. 缺陷本体: 四类以 `[` 开头的事实行必须进索引 ══════════════════════════
class TestDroppedFactLinesNowIndexed:
    def test_hard_event_classified_line_is_indexed(self, stubbed_brief):
        brief = ue._build_clue_brief("")
        assert HARD_LINE in ue._LAST_BRIEF_MATERIALS

    def test_earnings_reported_line_is_indexed(self, stubbed_brief):
        brief = ue._build_clue_brief("")
        assert EARN_REPORTED in ue._LAST_BRIEF_MATERIALS

    def test_earnings_upcoming_line_is_indexed(self, stubbed_brief):
        ue._build_clue_brief("")
        assert EARN_UPCOMING in ue._LAST_BRIEF_MATERIALS

    def test_insider_cluster_line_is_indexed(self, stubbed_brief):
        ue._build_clue_brief("")
        assert INSIDER_LINE in ue._LAST_BRIEF_MATERIALS

    def test_production_artifact_citation_now_grounded(self, stubbed_brief):
        """生产实测 31/32 条误杀的代表样本 — BABA 内部人集群逐字引证。

        修前: 该行不在材料库 ⇒ _evidence_grounded 返回 False ⇒ 攒样把真引证记成伪造。
        """
        ue._build_clue_brief("")
        assert ue._evidence_grounded(INSIDER_LINE, ue._LAST_BRIEF_MATERIALS) is True

    def test_old_prefix_predicate_would_have_dropped_them(self):
        """反向钉: 旧谓词确实会丢掉这四类 —— 防"缺陷描述本身写错"。"""
        old_keep = lambda s: bool(s.strip()) and not s.strip().startswith("[") \
            and not s.strip().startswith("日期")
        for line in (HARD_LINE, EARN_REPORTED, EARN_UPCOMING, INSIDER_LINE):
            assert old_keep(line) is False, line
        for line in (K8_LINE, DG_LINE, EVENT_SYM_LINE, NEWS_LINE, FEEDBACK_LINE):
            assert old_keep(line) is True, line

    def test_all_fact_lines_indexed(self, stubbed_brief):
        ue._build_clue_brief("")
        missing = [x for x in ALL_FACT_LINES if x not in ue._LAST_BRIEF_MATERIALS]
        assert missing == []


# ══ 2. 索引 ↔ 正文逐条对应 (P2 验收项) ═══════════════════════════════════════
class TestIndexBodyCorrespondence:
    def test_every_indexed_text_appears_verbatim_in_brief(self, stubbed_brief):
        brief = ue._build_clue_brief("")
        body_lines = {ln.strip() for ln in brief.split("\n")}
        orphan = [e["text"] for e in ue.brief_evidence_index()
                  if e["text"] not in body_lines]
        assert orphan == [], f"索引里有正文没有的材料 (引证核验会凭空放行): {orphan}"

    def test_every_citable_body_line_is_indexed(self, stubbed_brief):
        """反向: 正文里可被引用的行必须都在索引里。

        例外只有三种, 且都是**按构造**排除 (不是靠前缀猜):
          ① 日期行 ② 节标题 ③ 负反馈段的"规则:"散文 (那是给模型的指令, 不是材料;
             模型把指令当 evidence_ref 引用本就该判不合格)。
        """
        brief = ue._build_clue_brief("")
        indexed = {e["text"] for e in ue.brief_evidence_index()}
        unindexed = []
        for raw in brief.split("\n"):
            line = raw.strip()
            if not line:
                continue
            if line in indexed:
                continue
            if line.startswith(DATE_LINE_PREFIX):
                continue
            if line.startswith(FEEDBACK_RULE_PREFIX):
                continue
            unindexed.append(line)
        headers = [x for x in unindexed if x.startswith("[") and x.endswith("]")]
        assert headers == unindexed, f"正文里有未登记且不是节标题的行: {set(unindexed) - set(headers)}"

    def test_section_headers_are_not_indexed(self, stubbed_brief):
        ue._build_clue_brief("")
        texts = [e["text"] for e in ue.brief_evidence_index()]
        assert HEADER_HARD not in texts
        assert not any(t.startswith(HEADER_NEWS_PREFIX) for t in texts)

    def test_date_line_is_not_indexed(self, stubbed_brief):
        ue._build_clue_brief("")
        assert not any(e["text"].startswith(DATE_LINE_PREFIX)
                       for e in ue.brief_evidence_index())

    def test_feedback_rule_prose_is_not_indexed(self, stubbed_brief):
        """规则散文是指令不是材料 — 显式钉住, 免得日后被当"漏登记"补回去。"""
        ue._build_clue_brief("")
        assert not any(e["text"].startswith(FEEDBACK_RULE_PREFIX)
                       for e in ue.brief_evidence_index())


# ══ 3. 渲染零漂移 (P2.1a 不改送进模型的正文) ═════════════════════════════════
class TestRenderingZeroDrift:
    def test_brief_contains_every_fact_line_verbatim(self, stubbed_brief):
        brief = ue._build_clue_brief("")
        for line in ALL_FACT_LINES:
            assert line in brief, line

    def test_brief_structure_order_preserved(self, stubbed_brief):
        """段顺序与旧实现一致: 硬事件 → 事件标的 → 峰群 → 盘前榜 → 佐证 → 新闻 → 负反馈。"""
        brief = ue._build_clue_brief("")
        order = [HEADER_HARD, "[今日事件驱动标的", "[盘前板块峰群",
                 "[盘前全市场榜质量门候选", "[市场佐证", "[近24h 新闻头条",
                 "[本源近7日浮现回顾"]
        positions = [brief.index(marker) for marker in order]
        assert positions == sorted(positions), list(zip(order, positions))

    def test_no_evidence_id_prefix_leaks_into_body(self, stubbed_brief):
        """P2.1a 刻意**不**把 evidence_id 渲染进正文 (那是 P2.1b 的版本化改动)。"""
        brief = ue._build_clue_brief("")
        assert "[E001]" not in brief
        assert "E0" not in brief


# ══ 4. 索引 schema 与 evidence_id ═══════════════════════════════════════════
class TestEvidenceSchema:
    def test_schema_keys_exactly(self, stubbed_brief):
        ue._build_clue_brief("")
        for entry in ue.brief_evidence_index():
            assert set(entry.keys()) == set(ue.EVIDENCE_SCHEMA)

    def test_evidence_ids_unique_and_sequential(self, stubbed_brief):
        ue._build_clue_brief("")
        ids = [e["evidence_id"] for e in ue.brief_evidence_index()]
        assert len(ids) == len(set(ids))
        assert ids == [f"E{i:03d}" for i in range(1, len(ids) + 1)]

    def test_index_reset_per_build(self, stubbed_brief):
        ue._build_clue_brief("")
        first = len(ue.brief_evidence_index())
        ue._build_clue_brief("")
        assert len(ue.brief_evidence_index()) == first
        assert [e["evidence_id"] for e in ue.brief_evidence_index()][0] == "E001"

    def test_symbols_normalized_upper_and_prefix_stripped(self, stubbed_brief):
        got = ue._fact("s", "t", symbols=["us.nvda", " amd ", "US.AMD", ""])
        assert got["symbols"] == ["AMD", "NVDA"]

    def test_structured_metadata_survives(self, stubbed_brief):
        ue._build_clue_brief("")
        by_text = {e["text"]: e for e in ue.brief_evidence_index()}
        assert by_text[EARN_REPORTED]["source"] == "earnings_calendar"
        assert by_text[EARN_REPORTED]["published_at"] == "2026-09-10"
        assert by_text[EARN_REPORTED]["event_key"] == "EARNINGS"
        assert by_text[EARN_REPORTED]["symbols"] == ["NVDA"]
        assert by_text[INSIDER_LINE]["source"] == "insider_cluster"
        assert by_text[NEWS_LINE]["symbols"] == ["QQQ", "SPY"]
        assert by_text[MACRO_LINE]["symbols"] == []

    def test_macro_fact_has_no_named_symbol(self, stubbed_brief):
        """P2.6 判据依赖"宏观材料无具名标的"这一事实, 钉住它不被顺手填上。"""
        ue._build_clue_brief("")
        by_text = {e["text"]: e for e in ue.brief_evidence_index()}
        assert by_text[MACRO_LINE]["event_key"] == "macro"
        assert by_text[MACRO_LINE]["symbols"] == []


# ══ 5. _take_facts 的降级兼容 ═══════════════════════════════════════════════
class TestTakeFacts:
    def test_accepts_bare_strings(self):
        ue._BRIEF_EVIDENCE = []
        lines = ue._take_facts(["裸字符串材料"], source="legacy")
        assert lines == ["裸字符串材料"]
        assert ue.brief_evidence_index()[0]["source"] == "legacy"

    def test_empty_and_whitespace_text_skipped(self):
        ue._BRIEF_EVIDENCE = []
        assert ue._take_facts([ue._fact("s", "   "), ue._fact("s", "")],
                              source="s") == []
        assert ue.brief_evidence_index() == []

    def test_none_and_empty_input_safe(self):
        ue._BRIEF_EVIDENCE = []
        assert ue._take_facts(None, source="s") == []
        assert ue._take_facts([], source="s") == []

    def test_text_is_stripped_on_registration(self):
        ue._BRIEF_EVIDENCE = []
        ue._take_facts([ue._fact("s", "  带空白的材料  ")], source="s")
        assert ue.brief_evidence_index()[0]["text"] == "带空白的材料"

    def test_fact_source_wins_over_caller_default(self):
        ue._BRIEF_EVIDENCE = []
        ue._take_facts([ue._fact("specific", "t")], source="fallback")
        assert ue.brief_evidence_index()[0]["source"] == "specific"

    def test_brief_evidence_index_returns_copy(self):
        ue._BRIEF_EVIDENCE = []
        ue._take_facts([ue._fact("s", "t")], source="s")
        got = ue.brief_evidence_index()
        got[0]["text"] = "被篡改"
        assert ue._BRIEF_EVIDENCE[0]["text"] == "t"


# ══ 6. gather 函数返回结构化事实 (不是裸行) ─────────────────────────────────
class TestGatherReturnsFacts:
    def test_insider_clusters_returns_facts(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ue, "_ROOT", tmp_path)
        monkeypatch.setattr(ue, "_today", lambda: "2026-09-12")
        reports = tmp_path / "data" / "reports"
        reports.mkdir(parents=True)
        (reports / "insider_daily_2026-09-12.json").write_text(
            '{"stats": {"by_symbol": {"US.BABA": 1}}}', encoding="utf-8")
        got = ue._gather_insider_clusters()
        assert len(got) == 1 and isinstance(got[0], dict)
        assert got[0]["text"] == INSIDER_LINE
        assert got[0]["symbols"] == ["BABA"]
        assert got[0]["event_key"] == "INSIDER_CLUSTER"

    def test_insider_clusters_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ue, "_ROOT", tmp_path)
        monkeypatch.setattr(ue, "_today", lambda: "2026-09-12")
        assert ue._gather_insider_clusters() == []

    def test_macro_brief_returns_facts(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ue, "_ROOT", tmp_path)
        monkeypatch.setattr(ue, "_today", lambda: "2026-09-12")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "ue_macro_brief_2026-09-12.json").write_text(
            '{"brief_lines": ["非农新增 15.2 万"]}', encoding="utf-8")
        got = ue._gather_macro_brief()
        assert got[0]["source"] == "macro_brief"
        assert got[0]["symbols"] == []
        assert got[0]["text"] == "非农新增 15.2 万"

    def test_event_symbols_returns_facts(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ue, "_ROOT", tmp_path)
        monkeypatch.setattr(ue, "_today", lambda: "2026-09-12")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "event_symbols_2026-09-12.json").write_text(
            '{"event_symbols": [{"symbol": "US.TSLA", "score": 8, '
            '"reasons": ["交付超预期"]}]}', encoding="utf-8")
        got = ue._gather_event_symbols_file()
        assert got[0]["symbols"] == ["TSLA"]
        assert got[0]["text"] == "- TSLA (score=8): 交付超预期"

    def test_earnings_events_returns_facts(self, monkeypatch):
        class _Cal:
            def get_recently_reported(self, days_back=2):
                return [{"ticker": "NVDA", "report_date": "2026-09-10",
                         "time_of_day": "amc"}]

            def get_watchlist_earnings(self, days_ahead=3):
                return [{"ticker": "ORCL", "report_date": "2026-09-15",
                         "days_until": 2}]

        import app.data.earnings_calendar as ec_mod
        monkeypatch.setattr(ec_mod, "EarningsCalendar", lambda: _Cal())
        got = ue._gather_earnings_events()
        assert [g["text"] for g in got] == [EARN_REPORTED, EARN_UPCOMING]
        assert [g["event_key"] for g in got] == ["EARNINGS", "EARNINGS_UPCOMING"]
        assert [g["published_at"] for g in got] == ["2026-09-10", "2026-09-15"]

    def test_hard_events_returns_facts(self, monkeypatch):
        class _Ev:
            severity = "CRITICAL"
            event_type = "REGULATION"
            headline = "FDA 拒绝批准主力药"
            tickers = ["US.VRTX"]

        class _Classifier:
            def __init__(self, sector_mapping=None):
                pass

            def classify(self, items, use_llm=False):
                return [_Ev()]

        import app.catalyst.event_classifier as ce
        monkeypatch.setattr(ce, "CatalystClassifier", _Classifier)
        got = ue._gather_hard_events([{"title": "x", "symbols": '["VRTX"]'}])
        assert got[0]["text"] == HARD_LINE
        assert got[0]["symbols"] == ["VRTX"]
        assert got[0]["event_key"] == "CRITICAL/REGULATION"


# ══ 7. [P2.2] 输入具名标的可交易性初筛 ═══════════════════════════════════════
from datetime import date  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

_ET = ZoneInfo("America/New_York")
_PS_NOW = datetime(2026, 9, 11, 10, 0, tzinfo=_ET)     # 常规时段 (不走 pre_price 分支)
_PS_ASOF = "2026-09-11"

# ── 阈值一律**注入**, 不读环境配置 (2026-09-13 全量套件实测教训) ──
# `load_candidate_policy()` 的 `min_price` 来自 `admission.get_admission_config()`,
# 后者有模块级缓存 `_cfg_cache`, 且 `except Exception: pass → 内置缺省` 会把
# `_DEFAULTS["min_entry_price"] = 0.0` ("0=不拦") **永久缓存**下去。全量套件里
# 先跑的某个用例会把容器桩掉/复位缓存, 于是本文件单独跑绿、进全量就红:
#   min_price 5.0 → 0.0 ⇒ 价格地板静默失效 ⇒ PNY@2.0 不再判 price_floor。
# 只读复现 (2026-09-13):
#   ① 干净起点 min_price=5.0, PNY@2.0 → REJECT ['price_floor']
#   ② 首次调用时容器不可用 → min_price=0.0; 容器恢复后再读**仍是 0.0** (缓存未刷新)
#      → PNY@2.0 不再因价格被拒
#   ③ 只有 get_admission_config(refresh=True) 才拿回 5.0
# 该缓存无 per-test 隔离属**共享基建缺陷**, 本文件不代其修 (见 DEB-20260910 §17.6 待裁⑤);
# 本文件的做法是按计划 P1.1「行情、配置、时间和账户上下文均可注入测试」把 policy 钉死,
# 阈值断言与另一个人跑测试的顺序无关。真配置路径由 TestRealPolicyWiring 单独冒烟。
_PS_POLICY_BASE = {"version": "da_ue_v1", "min_price": 5.0, "low_atr_enabled": True,
                   "min_atr_ratio": 0.03, "max_quote_age_sec": 90.0,
                   "max_spread_ratio": 0.05, "quote_gate_enabled": True,
                   "borrow_gate_enabled": True}


def _ps_policy():
    """扫描期口径 = 注入基座经**真** `_scan_phase_policy()` 派生 (关微结构门那步不桩)。"""
    return ue._scan_phase_policy(dict(_PS_POLICY_BASE))


def _ps_bars(n=20, vol=0.02, base=100.0):
    """n 根已完成日线, 末根 = asof 前一交易日; TR/price ≈ 2*vol。"""
    day = date.fromisoformat(_PS_ASOF) - timedelta(days=1)
    days = []
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    out, close = [], base
    for d in days:
        out.append({"time": d.isoformat(), "open": close,
                    "high": close * (1 + vol), "low": close * (1 - vol),
                    "close": close})
    return out


class _PsRouter:
    """假路由: 键必须是**规范化符号** (US.XXX)。

    ⚠ 教训 (离线全链 2026-09-12): 按裸符号建键会让所有标的落到同一份默认行情,
      于是 7 个标的同得一个 ATR —— 低ATR门形同虚设而测试全绿 (假绿)。
    """

    def __init__(self, quotes=None, bars=None):
        self.quotes, self.bars = quotes or {}, bars or {}
        self.quote_calls, self.bar_calls = [], []

    def get_batch_quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        return {s: self.quotes.get(s, {}) for s in symbols}

    def get_daily_bars(self, symbol, count=40):
        self.bar_calls.append(symbol)
        return self.bars.get(symbol, [])


def _ps_quote(price, *, asof=None, pre=None):
    stamp = asof if asof is not None else _PS_NOW
    q = {"price": price, "source": "futu",
         "source_asof": stamp if isinstance(stamp, str) else stamp.isoformat()}
    if pre is not None:
        q["pre_price"] = pre
    return q


def _ps_router(**prices):
    quotes, bars = {}, {}
    for sym, price in prices.items():
        full = f"US.{sym}"
        quotes[full] = _ps_quote(price) if price is not None else {}
        if price is not None:
            bars[full] = _ps_bars(base=price)
    return _PsRouter(quotes, bars)


class TestScanPhasePolicy:
    def test_drops_execution_microstructure_gates(self):
        """扫描期口径必须关掉报价新鲜度/点差/借券 —— 那是下单前的实时门。"""
        base = {"version": "da_ue_v1", "min_price": 5.0, "low_atr_enabled": True,
                "min_atr_ratio": 0.03, "max_quote_age_sec": 90.0,
                "max_spread_ratio": 0.05, "quote_gate_enabled": True,
                "borrow_gate_enabled": True}
        got = ue._scan_phase_policy(base)
        assert got["quote_gate_enabled"] is False
        assert got["borrow_gate_enabled"] is False
        # 有限大值而非 inf: inf 会让 policy 无法 JSON 指纹化 (见下方回归钉)
        assert got["max_quote_age_sec"] > 365 * 24 * 3600 * 100
        assert math.isfinite(got["max_quote_age_sec"])
        assert got["phase"] == "ue_input_prescreen"

    def test_keeps_structural_gates(self):
        """结构性判据 (价格地板 / 低ATR / 形状) 必须原样保留 —— 那才是本段要筛的。"""
        base = {"version": "da_ue_v1", "min_price": 5.0, "low_atr_enabled": True,
                "min_atr_ratio": 0.03, "max_quote_age_sec": 90.0,
                "quote_gate_enabled": True, "borrow_gate_enabled": True}
        got = ue._scan_phase_policy(base)
        assert got["min_price"] == 5.0
        assert got["low_atr_enabled"] is True
        assert got["min_atr_ratio"] == 0.03
        assert got["version"] == "da_ue_v1"

    def test_does_not_mutate_caller_policy(self):
        base = {"quote_gate_enabled": True, "borrow_gate_enabled": True,
                "max_quote_age_sec": 90.0}
        ue._scan_phase_policy(base)
        assert base == {"quote_gate_enabled": True, "borrow_gate_enabled": True,
                        "max_quote_age_sec": 90.0}

    def test_scan_phase_policy_is_json_fingerprintable(self):
        """回归钉: policy 必须能过 `_fingerprint` 的 `json.dumps(allow_nan=False)`。

        曾用 `float("inf")` 关新鲜度门 ⇒ `Infinity` 非法 JSON ⇒ screen_candidates 抛错
        ⇒ 被 fail-open 兜住 ⇒ **每次初筛都静默 unavailable**, 材料一条不标注, 而
        "零改动"看起来像正常的 fail-open。有限大值才可指纹化。
        """
        import json
        policy = ue._scan_phase_policy({"version": "v", "min_price": 5.0,
                                        "low_atr_enabled": True, "min_atr_ratio": 0.03,
                                        "max_quote_age_sec": 90.0,
                                        "quote_gate_enabled": True,
                                        "borrow_gate_enabled": True})
        assert math.isfinite(policy["max_quote_age_sec"])
        json.dumps(policy, sort_keys=True, allow_nan=False)   # 不抛 = 可指纹化

    def test_scan_phase_policy_fingerprintable_via_real_screen_candidates(self):
        """端到端确认: 用扫描期口径跑真 screen_candidates, input_hash 必须算得出来。"""
        from app.playbook.candidate_eligibility import screen_candidates
        router = _PsRouter({"US.AAA": _ps_quote(100.0)}, {"US.AAA": _ps_bars(base=100.0)})
        rows = screen_candidates([{"symbol": "AAA"}], router,
                                 source=ue.INPUT_PRESCREEN_SOURCE, batch_id="b",
                                 now=_PS_NOW, policy=ue._scan_phase_policy(
                                     {"version": "da_ue_v1", "min_price": 5.0,
                                      "low_atr_enabled": True, "min_atr_ratio": 0.03,
                                      "max_quote_age_sec": 90.0,
                                      "quote_gate_enabled": True,
                                      "borrow_gate_enabled": True}))
        assert rows[0]["candidate_screening"]["input_hash"]
        assert rows[0]["candidate_screening"]["status"] == "PASS"

    def test_stale_quote_does_not_reject_at_scan_phase(self):
        """设计取舍的**行为**证明: 10 小时前的报价在扫描期不判 stale。

        若照搬执行口径 (90s), ET 09:10 的盘前扫描会把输入标的几乎全判 stale_quote
        ⇒ 初筛退化成"全清" = P2 验收②要防的"背景事件和榜外发现被错误清空"。
        """
        old = (_PS_NOW - timedelta(hours=10)).isoformat()
        router = _PsRouter({"US.AAA": _ps_quote(100.0, asof=old)},
                           {"US.AAA": _ps_bars(base=100.0)})
        got = ue._prescreen_input_symbols(
            ["AAA"], router=router, now=_PS_NOW,
            policy=ue._scan_phase_policy({"version": "da_ue_v1", "min_price": 5.0,
                                          "low_atr_enabled": True,
                                          "min_atr_ratio": 0.03,
                                          "max_quote_age_sec": 90.0,
                                          "quote_gate_enabled": True,
                                          "borrow_gate_enabled": True}))
        assert got["tradeable"] == ["AAA"], got
        assert got["context_only"] == []


class TestPrescreenThreeStates:
    def _run(self, router, symbols, **kw):
        kw.setdefault("policy", _ps_policy())
        return ue._prescreen_input_symbols(list(symbols), router=router,
                                           now=_PS_NOW, **kw)

    def test_pass_goes_to_tradeable(self):
        got = self._run(_ps_router(AAA=100.0), ("AAA",))
        assert got["status"] == "ok" and got["tradeable"] == ["AAA"]
        assert got["checked"] == 1

    def test_price_floor_goes_to_context_only_with_reason(self):
        got = self._run(_ps_router(PNY=2.0), ("PNY",))
        assert [e["symbol"] for e in got["context_only"]] == ["PNY"]
        assert "price_floor" in got["context_only"][0]["reasons"]

    def test_low_atr_goes_to_context_only_with_reason(self):
        """低波 (ATR 比例 1% < 3%) → 仅背景。P2 验收①的"低波"那一半。"""
        router = _PsRouter({"US.QQT": _ps_quote(100.0)},
                           {"US.QQT": _ps_bars(base=100.0, vol=0.005)})
        got = self._run(router, ("QQT",))
        assert [e["symbol"] for e in got["context_only"]] == ["QQT"]
        assert "low_atr" in got["context_only"][0]["reasons"]
        assert got["context_only"][0]["screening_atr_ratio"] < 0.03

    def test_warrant_shape_goes_to_context_only(self):
        """权证/优先股 → 仅背景, 且原因码是"证券类型不支持"而非"标的写错"。"""
        got = self._run(_PsRouter({}, {}), ("BBBY-WT",))
        assert [e["symbol"] for e in got["context_only"]] == ["BBBY-WT"]
        assert "unsupported_security_shape" in got["context_only"][0]["reasons"]
        assert "invalid_symbol" not in got["context_only"][0]["reasons"]

    def test_missing_quote_is_unknown_not_context_only(self):
        """缺数 ≠ 拒绝 (计划 P1.3): 取不到行情只判"未定", 不得当不可交易清掉。"""
        got = self._run(_PsRouter({"US.NOPX": {}}, {}), ("NOPX",))
        assert got["context_only"] == []
        assert [e["symbol"] for e in got["unknown"]] == ["NOPX"]
        assert got["unknown"][0]["status"] == "UNKNOWN"

    def test_missing_bars_is_unknown(self):
        router = _PsRouter({"US.NOBAR": _ps_quote(100.0)}, {"US.NOBAR": []})
        got = self._run(router, ("NOBAR",))
        assert [e["symbol"] for e in got["unknown"]] == ["NOBAR"]
        assert "screening_atr_ratio" in got["unknown"][0]["missing_fields"]

    def test_three_states_are_disjoint_and_complete(self):
        router = _PsRouter({"US.GUD": _ps_quote(100.0), "US.CHEAP": _ps_quote(1.0),
                            "US.NOPX": {}},
                           {"US.GUD": _ps_bars(base=100.0),
                            "US.CHEAP": _ps_bars(base=1.0)})
        got = self._run(router, ("GUD", "CHEAP", "NOPX", "BBBY-WT"))
        buckets = ([e["symbol"] for e in got["context_only"]]
                   + [e["symbol"] for e in got["unknown"]] + got["tradeable"])
        assert sorted(buckets) == ["BBBY-WT", "CHEAP", "GUD", "NOPX"]
        assert got["checked"] == 4
        assert len(set(buckets)) == 4

    def test_empty_input_short_circuits_without_io(self):
        router = _PsRouter({}, {})
        got = self._run(router, ())
        assert got["status"] == "no_named_symbol"
        assert router.quote_calls == [] and router.bar_calls == []


class TestPrescreenFailOpen:
    def test_router_exception_degrades_to_unknown_not_context_only(self):
        """行情源整体挂掉: `screen_candidates` 自己吞掉 IO 异常 ⇒ 全部落 unknown。

        这是**比 status=unavailable 更好**的降级: 每个标的仍带一条可归因记录
        (缺什么写进 missing_fields), 而不是整段消失。关键断言是**没有一个被误判成
        不可交易** —— 行情挂了不等于标的不合格 (缺数 ≠ 拒绝)。
        """
        class Boom:
            def get_batch_quotes(self, symbols):
                raise RuntimeError("feed down")

            def get_daily_bars(self, symbol, count=40):
                raise RuntimeError("feed down")

        got = ue._prescreen_input_symbols(["AAA"], router=Boom(), now=_PS_NOW,
                                          policy=_ps_policy())
        assert got["status"] == "ok"
        assert got["context_only"] == []
        assert got["tradeable"] == []
        assert [e["symbol"] for e in got["unknown"]] == ["AAA"]

    def test_policy_loader_failure_is_unavailable(self):
        """配置读不到 (阈值不可知) ⇒ 整段 unavailable, 不猜阈值也不清材料。"""
        class BoomPolicy:
            def get(self, key, default=None):
                raise RuntimeError("cfg down")

        got = ue._prescreen_input_symbols(["AAA"], router=_ps_router(AAA=100.0),
                                          now=_PS_NOW, policy=BoomPolicy())
        assert got["status"] == "unavailable"
        assert got["tradeable"] == [] and got["context_only"] == []
        assert got["unknown"] == []

    def test_unavailable_renders_nothing(self):
        """fail-open 的落点: 筛不了 ⇒ **不追加任何段**, 材料零改动 (不是清空)。"""
        assert ue._render_input_prescreen({"status": "unavailable"}) == []
        assert ue._render_input_prescreen({}) == []
        assert ue._render_input_prescreen(None) == []

    def test_no_named_symbol_renders_nothing(self):
        assert ue._render_input_prescreen({"status": "no_named_symbol"}) == []

    def test_ok_but_all_buckets_empty_renders_nothing(self):
        assert ue._render_input_prescreen(
            {"status": "ok", "tradeable": [], "context_only": [], "unknown": []}) == []


class TestPrescreenBounds:
    def test_symbols_deduped_and_normalized(self):
        router = _ps_router(AAA=100.0)
        ue._prescreen_input_symbols(["AAA", "aaa", "US.AAA", " AAA "],
                                    router=router, now=_PS_NOW, policy=_ps_policy())
        assert router.quote_calls == [["US.AAA"]], router.quote_calls

    def test_max_symbols_bounds_io_and_reports_overflow(self):
        # 符号必须纯字母: `_SYMBOL` 不接受数字 (`US.S0` 会判 invalid_symbol),
        # 用带数字的名测到的是形状门, 不是名额上限。
        names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        router = _ps_router(**{n: 100.0 for n in names})
        got = ue._prescreen_input_symbols(names, router=router, now=_PS_NOW,
                                          max_symbols=4, policy=_ps_policy())
        assert got["checked"] == 4
        assert len(got["overflow_skipped"]) == 2
        assert len(router.quote_calls[0]) == 4

    def test_overflow_symbols_are_not_excluded(self):
        """名额上限外只"未判定", **不得**被当成不可交易 (计划 P1.3 缺数策略)。"""
        names = ["AAA", "BBB", "CCC", "DDD"]
        router = _ps_router(**{n: 100.0 for n in names})
        got = ue._prescreen_input_symbols(names, router=router, now=_PS_NOW,
                                          max_symbols=2, policy=_ps_policy())
        overflow = got["overflow_skipped"]
        assert overflow
        lines = ue._render_input_prescreen(got)
        text = " ".join(x["text"] for x in lines)
        assert "上限外未判定" in text
        for sym in overflow:
            assert sym in text
        assert not any(sym in [e["symbol"] for e in got["context_only"]]
                       for sym in overflow)

    def test_max_symbols_defaults_from_config(self, monkeypatch):
        monkeypatch.setattr(ue, "_cfg",
                            lambda k, d=None: 2 if k.endswith("max_symbols") else d)
        names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        router = _ps_router(**{n: 100.0 for n in names})
        got = ue._prescreen_input_symbols(names, router=router, now=_PS_NOW,
                                          policy=_ps_policy())
        assert got["checked"] == 2 and len(got["overflow_skipped"]) == 3


class TestPrescreenRendering:
    def _summary(self):
        return {"status": "ok", "named_in_materials": 3, "checked": 3,
                "overflow_skipped": [], "policy_version": "da_ue_v1",
                "tradeable": ["GUD"],
                "context_only": [{"symbol": "CHEAP", "status": "REJECT",
                                  "reasons": ["price_floor"], "missing_fields": [],
                                  "price": 1.0, "screening_atr_ratio": None,
                                  "atr_reason": None}],
                "unknown": [{"symbol": "NOPX", "status": "UNKNOWN", "reasons": [],
                             "missing_fields": ["price"], "price": None,
                             "screening_atr_ratio": None, "atr_reason": None}]}

    def test_renders_three_state_lines(self):
        lines = ue._render_input_prescreen(self._summary())
        assert len(lines) == 3
        assert all(isinstance(x, dict) and x["source"] == "input_prescreen"
                   for x in lines)
        assert [x["event_key"] for x in lines] == [
            "prescreen_tradeable", "prescreen_context_only", "prescreen_unknown"]

    def test_context_only_line_says_not_a_trade_candidate(self):
        text = ue._render_input_prescreen(self._summary())[1]["text"]
        assert "CHEAP" in text and "price_floor" in text
        assert "不得当直接交易候选" in text

    def test_unknown_line_does_not_claim_ineligible(self):
        """未定行必须说"不猜", 不能读起来像拒绝 (缺数 ≠ 拒绝)。"""
        text = ue._render_input_prescreen(self._summary())[2]["text"]
        assert "NOPX" in text and "不猜" in text
        assert "price_floor" not in text and "low_atr" not in text

    def test_lines_carry_symbols_for_index(self):
        lines = ue._render_input_prescreen(self._summary())
        assert lines[0]["symbols"] == ["GUD"]
        assert lines[1]["symbols"] == ["CHEAP"]
        assert lines[2]["symbols"] == ["NOPX"]


# ══ 8. [P2.1 修正 2026-09-13] 异动行 symbols 必须来自 gather 现场, 不是反解渲染串 ══
class TestMoversSymbolSource:
    """实测缺陷回归钉 (只读复现于 2026-09-13): "整串渲染文本被当 ticker 登记"。

    病灶: `_build_clue_brief` 曾写
        symbols=[str(x).split(":")[0].split("(")[0] for x in mkt["movers"][:12]]
    即按**臆想的** `"ESTC: +19.3% (量比2.8)"` 格式去反解 ticker; 而生产实际格式是
    `_gather_market_corroboration` fallback 的 f"{symbol} {direction} {pct:+}%"
    (例 `"ESTC UP +19.3%"`, 既无冒号也无括号) ⇒ 整串原样进索引。两层后果:
      ① 初筛判 invalid_symbol → brief 里凭空多一行
         "仅背景(context_only, 不得当直接交易候选): ESTC UP +19.3%" = 对真标的的**假指控**;
      ② 真 ESTC 从未进具名集合 ⇒ P2 验收①在异动源上等于没生效。
    与 P2.1 要修的是同一族根因: 事后从成品串里猜结构化字段。
    """

    def test_movers_fact_symbols_are_clean_tickers(self, stubbed_brief):
        ue._build_clue_brief("")
        entry = next(e for e in ue.brief_evidence_index()
                     if e["event_key"] == "out_of_universe_movers")
        assert entry["symbols"] == CORRO_MOVER_SYMBOLS

    def test_rendered_mover_string_is_not_registered_as_symbol(self, stubbed_brief):
        """反解口径若复活, 这条立刻红: 索引里不得出现带方向/涨幅的整串。"""
        ue._build_clue_brief("")
        bad = [s for e in ue.brief_evidence_index() for s in e["symbols"]
               if not ue._TICKERISH.fullmatch(s)]
        assert bad == [], f"非 ticker 形状的 token 进了 symbols 槽: {bad}"

    def test_movers_rendered_text_unchanged(self, stubbed_brief):
        """修的是 symbols 来源, **渲染串一字不改** (brief 正文零漂移)。"""
        brief = ue._build_clue_brief("")
        assert CORRO_MOVERS in brief

    def test_gather_returns_structured_mover_symbols(self, monkeypatch, tmp_path):
        """源头: fallback capture 路径必须同时给出结构化 ticker (与渲染串同处产出)。"""
        monkeypatch.setattr(ue, "_ROOT", tmp_path)
        monkeypatch.setattr(ue, "_today", lambda: "2026-09-12")
        monkeypatch.setattr(ue, "_MECH_CAPTURE_PATH", tmp_path / "cap.jsonl")
        (tmp_path / "cap.jsonl").write_text(json.dumps({
            "type": "capture", "date": "2026-09-12",
            "strong_sectors": [], "weak_sectors": [],
            "surfaced": [{"symbol": "ESTC", "direction": "UP", "change_pct": 19.3},
                         {"symbol": "SOLS", "direction": "DOWN", "change_pct": -12.8}],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        got = ue._gather_market_corroboration()
        assert got["movers"] == ["ESTC UP +19.3%", "SOLS DOWN -12.8%"]
        assert got["mover_symbols"] == ["ESTC", "SOLS"]

    def test_ticker_symbols_filter_drops_garbage_keeps_shapes(self):
        assert ue._ticker_symbols(["ESTC", "US.NVDA", " nvda ", "ESTC"]) == ["ESTC", "NVDA"]
        # 权证/优先股形状必须**留下** —— 判它 unsupported_security_shape 是
        #   candidate_eligibility 的职责, 索引不得先把它吃掉 (否则 P2.2 的
        #   context_only 三态在权证上永远测不到)。
        assert ue._ticker_symbols(["BBBY-WT", "BRK.B"]) == ["BBBY-WT", "BRK.B"]
        assert ue._ticker_symbols(["ESTC UP +19.3%", "SOLS+12.8%", "", None, "TOOLONGTK"]) == []

    def test_fact_drops_non_ticker_symbols(self):
        got = ue._fact("market_corroboration", "宇宙外异动: ESTC UP +19.3%",
                       symbols=["ESTC UP +19.3%"])
        assert got["symbols"] == []


# ══ 9. [P2.2] _build_clue_brief 端到端: 初筛段真的进了正文, 且只追加不改写 ══════
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

# 冻结夹具里出现过的全部具名标的 → 假路由报价。
#   NIO 定价 2.0 (低于价格地板) ⇒ context_only; ESTC 不给报价 ⇒ unknown;
#   其余 100.0 + 2% 日线波幅 (ATR 比 ≈4% > 3% 门) ⇒ tradeable。
_E2E_PRICES = {"VRTX": 100.0, "NVDA": 100.0, "ORCL": 100.0, "BABA": 100.0,
               "BBBY": 100.0, "CNO": 100.0, "TSLA": 100.0, "SOLS": 100.0,
               "QQQ": 100.0, "SPY": 100.0, "NIO": 2.0, "ESTC": None}
# 索引顺序去重后的具名集合 (hard→财报→内部人→8K→13DG→事件标的→峰群→盘前榜→佐证→新闻)。
#   ⚠ 显式列出是刻意的: "先定序后截断"这条契约要求顺序可见, 段落重排时本钉应当红。
_E2E_NAMED_ORDER = ["VRTX", "NVDA", "ORCL", "BABA", "BBBY", "CNO", "TSLA", "NIO",
                    "SOLS", "ESTC", "QQQ", "SPY"]


def _e2e_mention_counts():
    """[2026-09-15] 从**材料索引**(输入侧, 非被测代码)推每个具名标的被几条材料提到。

    名额排序契约从"保材料索引顺序"改成"提及数降序 → 同数保首现"后, 断言不能再硬编码一个
    顺序常量: 段落重排/材料增减都会让它假红。改为在测试里从索引现推, 再断言产出顺序
    **满足契约性质**(集合不变 / 提及数非增 / 同数保首现) ⇒ 钉的是规则, 不是某一次的排列。
    """
    index = [s for e in ue.brief_evidence_index()
             if e["source"] not in ue._PRESCREEN_EXCLUDED_SOURCES
             for s in e["symbols"]]
    first_seen = list(dict.fromkeys(index))
    return first_seen, {s: index.count(s) for s in first_seen}


def _e2e_router():
    """假路由 (零网络)。报价同时给 price 与 pre_price, 且时戳=now-5min。

    ⚠ 两个都是必需的:
      ① `candidate_eligibility._quote_price` 在 ET 04:00–09:30 **只读 pre_price** ⇒
         只给 price 的夹具在盘前时段整批退化成 unknown, 测试变成"看钟表白不白";
      ② 时戳取相对 now 而非固定日 ⇒ 报价龄恒为 +5min, 不随跑测试的日期漂移
         (扫描期口径已关新鲜度门, 但 `quote_age < -5` 的 future_quote 判据仍在)。
    """
    stamp = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
    quotes, bars = {}, {}
    for sym, price in _E2E_PRICES.items():
        full = f"US.{sym}"
        if price is None:
            quotes[full] = {}
            continue
        quotes[full] = {"price": price, "pre_price": price, "source": "futu",
                        "source_asof": stamp}
        bars[full] = _ps_bars(base=price)
    return _PsRouter(quotes, bars)


@pytest.fixture
def prescreen_on(stubbed_brief, monkeypatch):
    """在 stubbed_brief 之上打开 P2.2 初筛: 假路由 + 真 policy 派生 (零网络/零生产库)。

    刻意**不**桩 `_scan_phase_policy` —— 关微结构门那一步走真代码; 只把它读的
    `load_candidate_policy()` 换成冻结基座 (理由见 `_PS_POLICY_BASE` 注释: 真配置有
    会话级缓存污染, 阈值断言不能依赖测试执行顺序)。真配置路径由
    TestRealPolicyWiring 单独冒烟。
    """
    import app.playbook.candidate_eligibility as ce

    router = _e2e_router()
    base_cfg = ue._cfg
    over = {"universe_expansion.input_prescreen": True,
            "universe_expansion.input_prescreen_max_symbols": 30,
            # [ADR-046 2026-10-14] 借券段默认关: 真 load_borrow_rates 会读生产
            #   borrow_rate_series.jsonl (非密封), 既有断言须零漂移。接线验证在
            #   TestBriefBorrowSnapshotSection 显式开 + 注入 rates。
            "universe_expansion.borrow_snapshot_brief": False}
    monkeypatch.setattr(ue, "_cfg",
                        lambda k, d=None: over[k] if k in over else base_cfg(k, d))
    monkeypatch.setattr(ue, "_build_router", lambda: router)
    monkeypatch.setattr(ce, "load_candidate_policy",
                        lambda pb_cfg=None: dict(_PS_POLICY_BASE))
    return SimpleNamespace(router=router, cfg=over, facts=stubbed_brief)


class TestBriefEndToEndPrescreen:
    def test_section_rendered_with_three_states(self, prescreen_on):
        brief = ue._build_clue_brief("")
        assert "[输入具名标的可交易性初筛" in brief
        ps = ue._LAST_INPUT_PRESCREEN
        assert ps["status"] == "ok", ps
        assert "VRTX" in ps["tradeable"]
        assert [e["symbol"] for e in ps["context_only"]] == ["NIO"]
        assert "price_floor" in ps["context_only"][0]["reasons"]
        assert [e["symbol"] for e in ps["unknown"]] == ["ESTC"]
        for token in ("可作直接交易候选", "仅背景(context_only", "未定(缺数据"):
            assert token in brief, token

    def test_prescreen_lines_are_indexed_as_facts(self, prescreen_on):
        """初筛结果自己也是**事实** (进索引/进材料库), 不是游离在正文里的散文。"""
        ue._build_clue_brief("")
        ps_facts = [e for e in ue.brief_evidence_index()
                    if e["source"] == "input_prescreen"]
        assert ps_facts, "初筛段没登记进材料索引 ⇒ 模型引用它会被判 not_found"
        texts = {e["text"] for e in ps_facts}
        assert texts <= set(ue._LAST_BRIEF_MATERIALS)
        assert {e["event_key"] for e in ps_facts} == {
            "prescreen_tradeable", "prescreen_context_only", "prescreen_unknown"}

    def test_named_set_ordered_by_mention_count_then_first_seen(self, prescreen_on):
        """[2026-09-15 改契约] 名额排序 = 被几条材料具名(降序) → 同提及数保首现顺序。

        旧契约是"保材料索引顺序", 2026-09-14 两轮首跑**实测证伪**: 受检 30 个与模型
        surfaced 的重叠仅 4/10 与 1/6, 未受检的全落在 overflow_skipped (含当日真成交的
        AAOI/CRWD) ⇒ 初筛看的和模型选的几乎是两批票, P2 验收①落空。修法不新增变量
        (max_symbols 仍 30), 只换排序键: 多源交叉提及 = 更可能被提名。
        仍保"先定序后截断"(计划 P1.4): 砍的是提及数最少的尾巴, 不是 Z 开头。
        """
        ue._build_clue_brief("")
        asked = prescreen_on.router.quote_calls[0]
        first_seen, mentions = _e2e_mention_counts()
        bare = [a.replace("US.", "") for a in asked]
        # ① 同一集合: 不丢、不重、不外扩 (排序只重排, 不改成员)
        assert sorted(bare) == sorted(first_seen)
        # ② 提及数沿产出顺序非增 ⇒ 名额确实给了多源热点
        counts = [mentions[s] for s in bare]
        assert counts == sorted(counts, reverse=True), (bare, counts)
        # ③ 同提及数内保首现顺序 ⇒ 没丢掉 gather 传导优先级语义
        for a, b in zip(bare, bare[1:]):
            if mentions[a] == mentions[b]:
                assert first_seen.index(a) < first_seen.index(b), (a, b)
        # ④ 全为单提及时退回首现顺序, 此时仍不得是字典序 (旧病灶: 字典序丢今日热点)
        if len(set(counts)) == 1:
            assert bare != sorted(bare), "退化成字典序了 — 截断时会丢今日热点"
        assert ue._LAST_INPUT_PRESCREEN["ordering"] == \
            "mention_count_desc_then_first_seen"

    def test_named_set_is_derived_from_index_not_reparsed(self, prescreen_on):
        """具名集合取自材料索引 (P2.1), 不是再从渲染串里把 ticker 抠回来。"""
        ue._build_clue_brief("")
        from_index = list(dict.fromkeys(
            s for e in ue.brief_evidence_index()
            if e["source"] not in ue._PRESCREEN_EXCLUDED_SOURCES
            for s in e["symbols"]))
        assert from_index == _E2E_NAMED_ORDER
        # 2026-09-15: 顺序改由提及数决定, 但**成员必须仍是索引集合的重排** —
        #   "取自索引而非重解析正文"这条契约靠集合相等钉, 不靠顺序。
        asked = prescreen_on.router.quote_calls[0]
        assert sorted(asked) == sorted(f"US.{s}" for s in from_index)

    def test_feedback_symbols_do_not_consume_prescreen_budget(self, prescreen_on,
                                                             monkeypatch):
        """负反馈表是**反候选**清单 ("连续推错的同标的同方向不得再浮现"), 不占今日名额。"""
        f = prescreen_on.facts
        extra = ue._fact("feedback_table", "- 2026-09-09 FBKX SHORT: -1.0% ✗",
                         symbols=["FBKX"], published_at="2026-09-09",
                         event_key="feedback/SHORT")
        monkeypatch.setattr(ue, "_build_feedback_table", lambda *a, **k: [f["fb"], extra])
        ue._build_clue_brief("")
        asked = prescreen_on.router.quote_calls[0]
        assert "US.FBKX" not in asked
        assert "FBKX" not in ue._LAST_INPUT_PRESCREEN["tradeable"]

    def test_max_symbols_truncates_priority_tail(self, prescreen_on):
        prescreen_on.cfg["universe_expansion.input_prescreen_max_symbols"] = 3
        brief = ue._build_clue_brief("")
        asked = prescreen_on.router.quote_calls[0]
        ps = ue._LAST_INPUT_PRESCREEN
        assert ps["checked"] == 3
        overflow = ps["overflow_skipped"]
        # [2026-09-15] 截断契约: 名额内的提及数不得低于名额外任何标的 (砍的是优先级尾巴)
        first_seen, mentions = _e2e_mention_counts()
        asked_bare = [a.replace("US.", "") for a in asked]
        assert len(asked_bare) == 3
        assert min(mentions[s] for s in asked_bare) >= \
            max(mentions[s] for s in overflow), (asked_bare, overflow)
        assert sorted(asked_bare + list(overflow)) == sorted(first_seen)
        # 上限外只"未判定", 不得被当成不可交易 (计划 P1.3 缺数策略)
        assert "上限外未判定" in brief
        assert not [e for e in ps["context_only"] if e["symbol"] in overflow]

    def test_provider_timestamp_only_is_not_washed_to_pass(self, prescreen_on, monkeypatch):
        """[2026-10-02 P0] 抓取时戳不得冒充交易所源时点。

        `timestamp` 是抓取完成时刻, 可把交易所侧已陈旧的报价洗成新鲜
        (2026-10-01 OXY/VIR 影子实证)。缺真实 `update_time/source_asof` 必须落
        UNKNOWN, 不得为了 `tradeable` 覆盖率而伪造 PASS。
        """
        router = prescreen_on.router
        stamp = (datetime.now().astimezone() - timedelta(minutes=5)).timestamp()
        prod = {}
        for sym, q in router.quotes.items():
            stripped = {k: v for k, v in q.items()
                        if k not in ("source_asof", "update_time")}
            if stripped:            # 空报价保持空 (缺价路径不被时戳归一"洗白")
                stripped["timestamp"] = stamp
            prod[sym] = stripped
        monkeypatch.setattr(router, "quotes", prod)
        brief = ue._build_clue_brief("")
        ps = ue._LAST_INPUT_PRESCREEN
        assert ps["status"] == "ok", ps
        assert ps["tradeable"] == [], ps
        assert ps.get("source_asof_available", 0) == 0
        assert all(e["status"] == "UNKNOWN" and "source_asof" in e["missing_fields"]
                   for e in ps["unknown"])
        assert "[输入具名标的可交易性初筛" in brief

    def test_provider_exchange_update_time_reaches_pass(self, prescreen_on, monkeypatch):
        """Futu 快照透传交易所 `update_time` 后, PASS 态仍可达。"""
        router = prescreen_on.router
        now = ue._now_et() - timedelta(minutes=5)
        prod = {}
        for sym, q in router.quotes.items():
            stripped = {k: v for k, v in q.items()
                        if k not in ("source_asof", "update_time")}
            if stripped:
                stripped["update_time"] = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            prod[sym] = stripped
        monkeypatch.setattr(router, "quotes", prod)
        brief = ue._build_clue_brief("")
        ps = ue._LAST_INPUT_PRESCREEN
        assert ps["status"] == "ok", ps
        assert ps["tradeable"], ps
        assert "VRTX" in ps["tradeable"]
        assert ps.get("source_asof_available", 0) > 0
        assert ps.get("source_asof_basis") == "provider_exchange_timestamp"
        assert [e["symbol"] for e in ps["unknown"]] == ["ESTC"]
        assert "ESTC" not in ps["tradeable"]
        assert "[输入具名标的可交易性初筛" in brief

    def test_quote_prefetch_failure_fails_open(self, prescreen_on, monkeypatch):
        """预取报价炸了 ⇒ 退回 `screen_candidates` 自取价, 不炸本段、不伪造三态。"""
        router = prescreen_on.router

        def boom(symbols):
            router.quote_calls.append(list(symbols))
            raise RuntimeError("provider down")

        monkeypatch.setattr(router, "get_batch_quotes", boom)
        brief = ue._build_clue_brief("")
        ps = ue._LAST_INPUT_PRESCREEN
        assert ps["status"] in ("ok", "unavailable"), ps
        assert "quotes_prefetched" not in ps, "预取失败却声称预取成功"
        # 两条取价路径都炸 ⇒ 内部 fail-open 成 {} ⇒ 只能 UNKNOWN, 不得凭空判 PASS/REJECT
        assert not ps["tradeable"]
        assert not ps["context_only"]
        assert ps["checked"] == len(ps["unknown"])
        assert isinstance(brief, str)

    def test_now_resolved_after_quote_prefetch(self, prescreen_on, monkeypatch):
        """[2026-09-15 回归钉] `now` 必须在**取价之后**定型 — 否则 future_quote 误判。

        生产报价的 `timestamp` 是取价完成时刻 (`futu_lv3_provider` 各处 `time.time()`)。
        修前 `now = now or _now_et()` 在取价**之前**执行 ⇒ `_source_time` 算出的
        `quote_age = now - stamp` 为负; 批量取 30 只一旦超 5s 容差 (走降级源时
        AlphaVantage 免费档每只 sleep 12s), 归一出时戳的标的会被 `evaluate_candidate`
        判 `future_quote` **REJECT** —— 即时戳归一这个修反而把"缺数 UNKNOWN"升级成
        "明确不可交易"写进 brief, 反向误导模型 (比修前更糟)。
        钉**调用顺序**而非真 sleep ⇒ 确定、零耗时、不依赖机器快慢。
        """
        router = prescreen_on.router
        real_now_et = ue._now_et
        order = []
        monkeypatch.setattr(
            ue, "_now_et", lambda: (order.append("now"), real_now_et())[1])
        orig = router.get_batch_quotes
        monkeypatch.setattr(
            router, "get_batch_quotes",
            lambda syms: (order.append("quotes"), orig(syms))[1])
        ps = ue._prescreen_input_symbols(["VRTX", "NVDA"], router=router)
        assert ps["status"] == "ok", ps
        assert "quotes" in order and "now" in order, order
        assert order.index("quotes") < order.index("now"), \
            f"now 在取价前定型 ⇒ future_quote 误判风险: {order}"
        assert not [e for e in ps["context_only"] if "future_quote" in e["reasons"]], ps

    def test_only_appends_never_rewrites_existing_material(self, prescreen_on):
        """P2 验收②: 初筛只**追加**一段, 既有材料行一条不改一条不删。"""
        prescreen_on.cfg["universe_expansion.input_prescreen"] = False
        off = ue._build_clue_brief("")
        prescreen_on.cfg["universe_expansion.input_prescreen"] = True
        on = ue._build_clue_brief("")
        assert on.startswith(off + "\n"), "初筛段改写了既有正文 (不是纯追加)"
        for line in ALL_FACT_LINES:
            assert line in on, line

    def test_prescreen_does_not_rescreen_its_own_symbols(self, prescreen_on):
        """初筛事实自己带 symbols, 但具名集合在它之前就定稿 ⇒ 不得有第二趟取价。"""
        ue._build_clue_brief("")
        assert len(prescreen_on.router.quote_calls) == 1, prescreen_on.router.quote_calls

    def test_disabled_config_yields_no_section_and_empty_state(self, prescreen_on):
        prescreen_on.cfg["universe_expansion.input_prescreen"] = False
        brief = ue._build_clue_brief("")
        assert "[输入具名标的可交易性初筛" not in brief
        assert ue._LAST_INPUT_PRESCREEN == {}
        assert prescreen_on.router.quote_calls == []

    def test_fail_open_yields_no_section_and_identical_brief(self, prescreen_on,
                                                            monkeypatch):
        """初筛整体炸掉 ⇒ 不追加任何段, 正文与"关掉初筛"逐字相同 (材料零改动)。"""
        prescreen_on.cfg["universe_expansion.input_prescreen"] = False
        off = ue._build_clue_brief("")

        def _boom(base=None):
            raise RuntimeError("policy exploded")

        prescreen_on.cfg["universe_expansion.input_prescreen"] = True
        monkeypatch.setattr(ue, "_scan_phase_policy", _boom)
        on = ue._build_clue_brief("")
        assert on == off
        assert ue._LAST_INPUT_PRESCREEN["status"] == "unavailable"
        assert "[输入具名标的可交易性初筛" not in on

    def test_state_reset_between_builds(self, prescreen_on):
        """上一次构建的初筛结果不得残留 (残留=宣称"这次输入和上次一样")。"""
        ue._build_clue_brief("")
        assert ue._LAST_INPUT_PRESCREEN.get("status") == "ok"
        prescreen_on.cfg["universe_expansion.input_prescreen"] = False
        ue._build_clue_brief("")
        assert ue._LAST_INPUT_PRESCREEN == {}


# ══ 10. [P2.5] brief 三指纹 ══════════════════════════════════════════════════
class TestBriefFingerprints:
    def test_fingerprint_shape_and_self_consistency(self, stubbed_brief):
        import hashlib
        brief = ue._build_clue_brief("")
        fp = ue._LAST_BRIEF_FINGERPRINTS
        assert fp["brief_template_version"] == ue.BRIEF_TEMPLATE_VERSION
        assert len(fp["brief_template_hash"]) == 12
        assert len(fp["brief_content_hash"]) == 16
        # content_hash 必须是**返回给调用方那份正文**的哈希 (否则复现时对不上)
        assert fp["brief_content_hash"] == hashlib.sha1(
            brief.encode("utf-8")).hexdigest()[:16]
        index = ue.brief_evidence_index()
        assert fp["brief_material_count"] == len(index)
        assert sum(fp["brief_material_stats"].values()) == len(index)

    def test_material_stats_matches_index_per_source(self, stubbed_brief):
        from collections import Counter
        ue._build_clue_brief("")
        index = ue.brief_evidence_index()
        assert ue._LAST_BRIEF_FINGERPRINTS["brief_material_stats"] == dict(
            sorted(Counter(e["source"] for e in index).items()))

    def test_template_hash_stable_across_days_content_hash_not(self, stubbed_brief,
                                                              monkeypatch):
        """template_hash 是**结构**指纹 (数字掩码后跨日稳定), content_hash 逐run 唯一。

        这是两个键不可互相替代的证明: 若 template_hash 也随当日新闻变, 它就不能当
        "template_version 忘了升"的金丝雀 (天天变 = 没有信号)。
        """
        monkeypatch.setattr(ue, "_today", lambda: "2026-09-12")
        ue._build_clue_brief("")
        day1 = dict(ue._LAST_BRIEF_FINGERPRINTS)
        monkeypatch.setattr(ue, "_today", lambda: "2026-09-13")
        ue._build_clue_brief("")
        day2 = dict(ue._LAST_BRIEF_FINGERPRINTS)
        assert day1["brief_template_hash"] == day2["brief_template_hash"]
        assert day1["brief_content_hash"] != day2["brief_content_hash"]

    def test_template_hash_moves_when_section_added_but_version_does_not(
            self, prescreen_on):
        """金丝雀的正向用例: 段结构变了而 template_version 没升 ⇒ hash 必须动。

        这正是它存在的理由 —— PROMPT_HASH_V2 只哈希 SYSTEM_PROMPT_V2, brief 侧加段
        对它完全不可见 (v2.11 已实际发生过一次)。
        """
        prescreen_on.cfg["universe_expansion.input_prescreen"] = False
        ue._build_clue_brief("")
        off = dict(ue._LAST_BRIEF_FINGERPRINTS)
        prescreen_on.cfg["universe_expansion.input_prescreen"] = True
        ue._build_clue_brief("")
        on = dict(ue._LAST_BRIEF_FINGERPRINTS)
        assert off["brief_template_version"] == on["brief_template_version"]
        assert off["brief_template_hash"] != on["brief_template_hash"], (
            "加了一整段而 template_hash 未变 ⇒ 金丝雀死了")
        added = [x for x in on["brief_sections"] if x not in off["brief_sections"]]
        assert len(added) == 1 and added[0].startswith("[输入具名标的可交易性初筛")
        assert on["brief_section_count"] == off["brief_section_count"] + 1

    def test_section_headers_exclude_fact_lines(self, stubbed_brief):
        """段落清单只收节标题: 以 `[` 开头的**事实行** (四类硬事件) 不得混进去。"""
        ue._build_clue_brief("")
        sections = ue._LAST_BRIEF_FINGERPRINTS["brief_sections"]
        assert all(x.startswith("[") and x.endswith("]") for x in sections)
        for fact_line in (HARD_LINE, EARN_REPORTED, INSIDER_LINE):
            assert fact_line not in sections

    def test_fingerprints_invalidated_when_build_raises(self, stubbed_brief,
                                                        monkeypatch):
        """构建炸掉必须让上一次的指纹失效 —— 留着旧值等于宣称"这次输入和上次一样"。

        reset 必须排在**任何可能抛的语句之前**: 原先排在 `_cfg(...)` 之后, 于是
        "第一行就抛"这条最常见的失败路径上旧指纹原地留存, 而 P2.6 的空转守卫正是
        读这份索引/指纹判"今天有没有材料"。
        """
        ue._build_clue_brief("")
        assert ue._LAST_BRIEF_FINGERPRINTS

        def _boom(k, d=None):
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(ue, "_cfg", _boom)
        with pytest.raises(RuntimeError):
            ue._build_clue_brief("")
        assert ue._LAST_BRIEF_FINGERPRINTS == {}
        assert ue.brief_evidence_index() == []
        assert ue._LAST_BRIEF_MATERIALS == []


# ══ 11. 真配置路径冒烟 + 共享基建债标记 ══════════════════════════════════════
class TestRealPolicyWiring:
    """不注入 policy 的那条真路径也要有人看着 —— 但只钉**结构不变量**, 不钉具体阈值。

    阈值断言一旦依赖环境配置, 就会随别的测试污染 `_cfg_cache` 而漂 (见 `_PS_POLICY_BASE`
    注释)。这里断言的四条与阈值取值无关, 任何配置下都必须成立:
      ① 微结构门在扫描期确实被关掉 (P2.2 的设计取舍本身);
      ② `max_quote_age_sec` 是**有限**值 (否则 policy 无法 JSON 指纹化, 见 §7 回归钉);
      ③ 低ATR门开着时阈值必须为正 (0 或负 = 门形同虚设);
      ④ 端到端能真跑通 `screen_candidates` 并算出 `input_hash`。
    """

    def test_scan_phase_policy_from_real_config_is_structurally_sound(self):
        policy = ue._scan_phase_policy()          # 不注入 ⇒ 走真 load_candidate_policy()
        assert policy["quote_gate_enabled"] is False
        assert policy["borrow_gate_enabled"] is False
        assert math.isfinite(policy["max_quote_age_sec"])
        assert policy["phase"] == "ue_input_prescreen"
        if policy.get("low_atr_enabled"):
            assert policy["min_atr_ratio"] > 0, "低ATR门开着但阈值非正 = 门死了没人知道"

    def test_real_policy_screens_a_fake_symbol_end_to_end(self):
        from app.playbook.candidate_eligibility import screen_candidates
        router = _PsRouter({"US.ZZZ": _ps_quote(100.0)}, {"US.ZZZ": _ps_bars(base=100.0)})
        rows = screen_candidates([{"symbol": "ZZZ"}], router,
                                 source=ue.INPUT_PRESCREEN_SOURCE, batch_id="b",
                                 now=_PS_NOW, policy=ue._scan_phase_policy())
        scr = rows[0]["candidate_screening"]
        assert scr["input_hash"]
        assert scr["status"] in ("PASS", "REJECT", "UNKNOWN")

    def test_debt_marker_admission_cache_can_silently_disable_price_floor(self):
        """债标记的**修复守护** (2026-10-26 RFC-168 批修复, 原为"钉住现状, 修好那天必须变红")。

        病灶: `app/playbook/admission.get_admission_config()` 把「拿不到真阈值」
        的降级结果**写进模块级 `_cfg_cache`**, 而 `_DEFAULTS` 是**宽松档**
        (`min_entry_price`/`min_notional_usd`/`max_sl_pct` 均 0.0 = 不拦)
        ⇒ 首次读取失败即永久生效, 容器恢复后也不刷新 (只有 refresh=True 才拿回真值)。
        降级**不抛异常** ⇒ `cmd_inject` 的 P1.5 守卫 ("配置读取失败 → 停止注入,
        不猜阈值") 拦不住, "不猜阈值"的承诺被绕过。

        修法 (DEB-20260910 §17.6 待裁⑤ 采纳): 容器不可用**或该节缺失**时
        **不落缓存**, 下次重试 ⇒ 降级只在当次生效, 不固化。

        本例现守护修复: ①降级期间返回缺省; ②容器恢复后**自动拿回真值**;
        ③真值恢复后价格地板重新生效。修好之前这三条断言会红。
        """
        from app.playbook import admission as A
        from app.playbook.candidate_eligibility import evaluate_candidate
        import app.core.config_container as cc

        saved_cache, saved_get = A._cfg_cache, cc.get_config
        try:
            A.reset_cache()

            def _boom():
                raise RuntimeError("config container unavailable")

            cc.get_config = _boom
            degraded_min_price = A.get_admission_config()["min_entry_price"]
            cc.get_config = saved_get
            recovered_min_price = A.get_admission_config()["min_entry_price"]
            assert degraded_min_price == 0.0, (
                "容器不可用时返回的缺省价格地板已不是 0.0 — 债形态变了, 重写本例")
            assert recovered_min_price != 0.0, (
                "容器恢复后仍取缺省 ⇒ §17.6 待裁⑤ 的缓存固化债未修 "
                "(修法: 降级时不落缓存, 下次重试)")
            verdict = evaluate_candidate(
                {"symbol": "US.PNY", "price": 2.0, "screening_atr_ratio": 0.05,
                 "quote_age_sec": 10.0, "bid": 1.99, "ask": 2.01},
                {"min_price": recovered_min_price, "low_atr_enabled": True,
                 "min_atr_ratio": 0.03, "max_quote_age_sec": 90.0,
                 "quote_gate_enabled": True, "borrow_gate_enabled": False})
            assert "price_floor" in verdict["reasons"], (
                "容器恢复后价格地板应重新生效 — 真值未取回, 债仍在")
        finally:
            cc.get_config = saved_get
            A._cfg_cache = saved_cache


# ══ [ADR-046 2026-10-14] 借券快照段: 端到端接线 (显式开 + 注入 rates, 零真数据) ══
class TestBriefBorrowSnapshotSection:
    """病灶: 规则10 要求"材料无借券数据→UNKNOWN/false", 但台账从未进 brief ⇒
    10-02 起 UE 空头 11/11 被源头过滤全杀。修复 = 渲染已采集快照进材料。
    本类钉**接线活体** (fixture 全绿≠接线活): 段落进正文 + 事实进索引 +
    两条 fail-open 路径 (加载炸/初筛死) 材料零改动。"""

    _RATES = {
        "US.VRTX": {"rate_pct": 2.5, "rate_asof": "2026-09-12", "shortable": True,
                    "pool": 50000.0, "age_days": 0, "stale": False},
        "US.NIO": {"rate_pct": None, "rate_asof": "2026-09-12", "shortable": False,
                   "pool": None, "age_days": 0, "stale": False},
    }

    def _enable(self, prescreen_on, monkeypatch, rates=None, boom=False):
        prescreen_on.cfg["universe_expansion.borrow_snapshot_brief"] = True
        import app.playbook.planned_executability as pe
        if boom:
            def _raise(*a, **k):
                raise RuntimeError("borrow loader down")
            monkeypatch.setattr(pe, "load_borrow_rates", _raise)
        else:
            _rates = self._RATES if rates is None else rates
            monkeypatch.setattr(pe, "load_borrow_rates", lambda *a, **k: _rates)

    def test_section_rendered_and_indexed(self, prescreen_on, monkeypatch):
        self._enable(prescreen_on, monkeypatch)
        brief = ue._build_clue_brief("")
        assert "[借券快照" in brief
        assert ("[借券] US.VRTX shortable=yes pool=50,000 fee=2.5% "
                "(as of 2026-09-12)") in brief
        assert "[借券] US.NIO shortable=no" in brief  # 不可借照实渲染 (过滤器语义不变)
        bw = [e for e in ue.brief_evidence_index() if e["source"] == "borrow_snapshot"]
        assert {e["event_key"] for e in bw} == {"borrow:VRTX", "borrow:NIO"}
        assert {tuple(e["symbols"]) for e in bw} == {("VRTX",), ("NIO",)}
        assert all(e["text"] in set(ue._LAST_BRIEF_MATERIALS) for e in bw)

    def test_loader_failure_keeps_brief_identical(self, prescreen_on, monkeypatch):
        prescreen_on.cfg["universe_expansion.borrow_snapshot_brief"] = False
        off = ue._build_clue_brief("")
        self._enable(prescreen_on, monkeypatch, boom=True)
        on = ue._build_clue_brief("")
        assert on == off  # fail-open: 加载炸 = 材料零改动

    def test_prescreen_death_skips_borrow_section(self, prescreen_on, monkeypatch):
        """初筛 fail-open 不变量延伸: prescreen 炸 ⇒ 借券段同缺 (联动门 _ps_lines)。"""
        self._enable(prescreen_on, monkeypatch)

        def _boom(base=None):
            raise RuntimeError("policy exploded")

        monkeypatch.setattr(ue, "_scan_phase_policy", _boom)
        prescreen_on.cfg["universe_expansion.borrow_snapshot_brief"] = False
        baseline = ue._build_clue_brief("")
        prescreen_on.cfg["universe_expansion.borrow_snapshot_brief"] = True
        on = ue._build_clue_brief("")
        assert on == baseline
        assert "[借券快照" not in on
