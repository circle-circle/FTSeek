"""U1 钉子: fwd_return_* 方向符号契约 (2026-09-11, DEB-20260910-da-ue 优先待裁项①).

病灶: `fwd_return_*` / `fwd_ret_*` 两张库的前瞻收益列**写侧都已按方向签名**
(`app/playbook/ic_tracker.py` 乘 sign, BUY=+1 / SELL_SHORT=-1;
`app/analysis/signal_ic_tracker.py::_calc_fwd` 用倒数式), 即**存的已经是"该方向指令赚了多少"**。
而三个读方各自又乘了一次方向符号 ⇒ SHORT 行被二次取负 = 标签整体反转。
潜伏 6 周的原因就是**零测试覆盖**: 修前全仓没有任何测试碰过这三个函数。

本文件钉四件事:

① **活体读方已修** — `scripts/llm_universe_scan.py::_build_feedback_table`, 注入 LLM prompt 的
   "本源近7日浮现回顾"表。实测 (只读生产库) 自 2026-07-25 上线以来 UE SHORT 有前瞻收益的
   **66 条 66/66 反转**, 其中 **41 条做对的空头被标"推错"**, 而紧邻的 prompt 规则是
   "连续推错的 同标的+同方向 不得再次浮现" ⇒ 反馈环在空头侧**反向训练**: 驱逐正确空头、
   强化错误空头。下面用实测真值做表驱动复现 (含 LONG 侧"不得顺手翻两侧")。
   **①b 同表第二类缺陷**: 渲染式把 `adj==0` 归进 ✗, 而表头/抑制规则把"推错"定义为负收益
   ⇒ 持平被当推错喂抑制规则 (prompt 自我矛盾, 非策略选择)。已改三态 ✓/✗/＝ 并同步改
   prompt 文本, 钉子见下方 ①b 节。

② **冻结读方只钉现状不修** — `scripts/entry_gate_replay.py::load_instructions` 同处还藏着
   第二个缺陷: 兜底值留成 fraction 而主路径 `replay_timing` 的 `ret` 是 percentage,
   `outcome()` 把两者混进同一个均值 ⇒ ATR 门网格的"拦掉组均%<0=拦对"定性跨 100× 量纲失真。
   该脚本被 `docs/REPLAY-SCRIPT-LEDGER.md` 列为**证据冻结 (不可改不可删)**, 故两缺陷按仓内
   "债标记测试"体例钉成现状断言 —— 冻结例外获批之日**必须变红**并被改写成正确断言。

③ **契约正文留在写侧** (单一真源), 并把"反签名还原价位"这个**唯一合法例外**一并记下 ——
   否则日后有人把 `loser_hold_path_replay.py` 的正确用法当 bug"顺手修"。

④ **第二把尺子 (U1b, 活体 cron) 同族反号已修** — `scripts/direction_counterfactual_ruler.py`
   B 节读**另一张库** `signal_snapshots.fwd_ret_*` 时又乘了一次 sign。该列写侧
   (`app/analysis/signal_ic_tracker.py::_calc_fwd`) 用**倒数式**签名, 与 `playbook_instructions`
   同约定。实测 SHORT 全窗真值 **+0.159%** 被报成 **−0.159%**, 而 B 节 `flag` 判据是
   费后 `cost_adj > 0` (cost=13bp) ⇒ **自 2026-08-20 立项起每日 06:50 输出
   "pool_does_not_support_this_side" 于空头侧**, 用反过来的证据反对做空。

关于"两张库两种约定"这个说法 (**错的, 刻意不写进契约**): 两张库的 fwd 列**都已签名**,
差别只在公式 (倒数式 vs 乘号式), 不在约定。U1b 的错因恰恰是尺子作者**以为**两库约定不同。

刻意**不立**全仓正则棘轮: 同一文本形态 `sign * fwd` 在 `loser_hold_path_replay.py` 是
正确的反签名 (还原价位), 在上面三处读方是 bug —— 意图不可 grep, 只能按读方逐个上钉子
(④ 另加一条 AST 结构钉: 两个 section 函数体内不得出现 `sign` 标识符)。

回滚: 删本文件 + revert `scripts/llm_universe_scan.py` 与
`scripts/direction_counterfactual_ruler.py` 两处读侧改动 + revert `ic_tracker.py` 契约段。
零生产代码依赖 (`entry_gate_replay.py` 与 HEAD 逐字节相同, 无需回滚)。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from scripts import llm_universe_scan as ue  # noqa: E402
import entry_gate_replay as egr  # noqa: E402

from app.utils.timezone import ET  # noqa: E402

_COLS = ("id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, atr_pct REAL, "
         "market_regime TEXT, executed INTEGER DEFAULT 0, "
         "fwd_return_intraday REAL, fwd_return_t1 REAL, raw_instruction_json TEXT")

_TODAY = "2026-09-10"
_UE_SRC = '{"source": "universe_expansion"}'


def _mk_db(tmp_path, rows):
    """rows: (date, symbol, direction, fwd_intraday, fwd_t1[, source_json])"""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    db = data / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE IF EXISTS playbook_instructions")  # 同一 tmp_path 可重复建夹具
    conn.execute(f"CREATE TABLE playbook_instructions ({_COLS})")
    for r in rows:
        src = r[5] if len(r) > 5 else _UE_SRC
        conn.execute(
            "INSERT INTO playbook_instructions (date, symbol, direction, atr_pct, market_regime, "
            "executed, fwd_return_intraday, fwd_return_t1, raw_instruction_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (r[0], r[1], r[2], 3.5, "neutral", 0, r[3], r[4], src))
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def _frozen_today(monkeypatch):
    monkeypatch.setattr(ue, "_today", lambda: _TODAY)


def _fb(monkeypatch, tmp_path, rows, **kw):
    db = _mk_db(tmp_path, rows)
    monkeypatch.setattr(ue, "_ROOT", tmp_path)
    out = ue._build_feedback_table(**kw)
    assert out or not rows, f"反馈表为空 — 夹具或 SQL 窗口有问题, 不是被测行为: {db}"
    # [P2.1 2026-09-12, DEB-20260910 §17] `_build_feedback_table` 现返回**结构化事实**
    #   (dict: source/text/symbols/published_at/event_key) 而非裸 str —— 材料索引改在
    #   gather 现场登记, 取消事后按行前缀猜材料 (旧谓词误杀了 4 类以 `[` 开头的事实行)。
    #   本文件钉的是**渲染文本**的方向符号与三态口径, 故在此取 text 面即可; 结构化字段
    #   与"索引↔正文逐条对应"由 tests/test_ue_evidence_index.py 覆盖。裸 str 仍兼容
    #   (降级路径 _take_facts 也接受 str), 所以两种形态都能过。
    return [x["text"] if isinstance(x, dict) else str(x) for x in out]


def _line(out, symbol):
    hits = [ln for ln in out if symbol in ln]
    assert len(hits) == 1, f"{symbol} 应恰出现 1 行, 实得 {hits}"
    return hits[0]


# ══════════ ① UE prompt 反馈表 (活体信号路径) ══════════

# 实测真值 (2026-09-11 只读生产库复算): 存储值 → 正确渲染。
# 修前这 5 行的 ✓/✗ **全部相反** (例: NTAP 存 -0.158263 却显示 "+15.8% ✓")。
_REAL = [
    ("2026-09-03", "US.NTAP", "SELL_SHORT", -0.158263, "-15.8%", "✗"),
    ("2026-09-03", "US.HPE", "SELL_SHORT", -0.128875, "-12.9%", "✗"),
    ("2026-09-04", "US.LULU", "SELL_SHORT", -0.02968, "-3.0%", "✗"),
    ("2026-09-04", "US.TTD", "SELL_SHORT", 0.03992, "+4.0%", "✓"),
    ("2026-09-08", "US.NVS", "SELL_SHORT", 0.011912, "+1.2%", "✓"),
]


@pytest.mark.parametrize("day,sym,direction,stored,pct,mark", _REAL)
def test_feedback_table_short_labels_match_stored_sign(
        monkeypatch, tmp_path, _frozen_today, day, sym, direction, stored, pct, mark):
    """SHORT 行: 存正数=空头赚钱=✓, 存负数=空头亏钱=✗。修前逐行相反。"""
    out = _fb(monkeypatch, tmp_path, [(day, sym, direction, stored, None)])
    ln = _line(out, sym.replace("US.", ""))
    assert pct in ln, f"量纲/符号错: {ln}"
    assert mark in ln, f"✓/✗ 与存储符号不一致 (= 反号回潮): {ln}"
    assert ln.count("✓") + ln.count("✗") == 1, f"一行只能有一个判定标记: {ln}"
    assert "SHORT" in ln


@pytest.mark.parametrize("direction", ["BUY", "SELL_SHORT"])
@pytest.mark.parametrize("win", [True, False])
@pytest.mark.parametrize("horizon", ["intraday", "t1"])
def test_feedback_table_eight_cell_sign_matrix(
        monkeypatch, tmp_path, _frozen_today, direction, win, horizon):
    """DEB §U1 验收原文「多空×盈亏×日内/T+1 八例符号全部正确」的 2×2×2 全覆盖。

    不变量: 渲染出的百分数与 ✓/✗ **只由存储值的符号决定**, 方向与周期都不得改变它。
    修前 SHORT 两列 (4 例) 全部相反 —— 这正是"八例"这个口径能一眼抓住病灶的原因。
    """
    stored = 0.02 if win else -0.02
    pct, mark = ("+2.0%", "✓") if win else ("-2.0%", "✗")
    row = (("2026-09-08", "US.MTX", direction, stored, None) if horizon == "intraday"
           else ("2026-09-08", "US.MTX", direction, None, stored))
    ln = _line(_fb(monkeypatch, tmp_path, [row]), "MTX")
    assert pct in ln, f"{direction}/{horizon}/{'盈' if win else '亏'} 量纲或符号错: {ln}"
    assert mark in ln, f"{direction}/{horizon}/{'盈' if win else '亏'} 判定标记反号: {ln}"
    assert ln.count("✓") + ln.count("✗") == 1


def test_feedback_table_long_rows_not_overcorrected(monkeypatch, tmp_path, _frozen_today):
    """LONG 行修前就是对的 (乘 1) ⇒ 本修不得把它改坏 (防"顺手翻两侧")。"""
    out = _fb(monkeypatch, tmp_path, [
        ("2026-09-08", "US.AAA", "BUY", 0.021, None),
        ("2026-09-08", "US.BBB", "BUY", -0.034, None),
    ])
    assert "+2.1%" in _line(out, "AAA") and "✓" in _line(out, "AAA")
    assert "-3.4%" in _line(out, "BBB") and "✗" in _line(out, "BBB")
    assert "LONG" in _line(out, "AAA")


def test_feedback_table_t1_fallback_also_uses_stored_sign(monkeypatch, tmp_path, _frozen_today):
    """intraday 缺失走 t1 兜底: 兜底路径同样不得二次签名。"""
    out = _fb(monkeypatch, tmp_path, [("2026-09-05", "US.CCC", "SELL_SHORT", None, 0.0275)])
    ln = _line(out, "CCC")
    assert "+2.8%" in ln and "✓" in ln, ln


def test_feedback_table_pending_rows_still_filtered(monkeypatch, tmp_path, _frozen_today):
    """U5 (2026-09-05) 既有行为不得回归: 两个 fwd 都为 NULL 的"待回填"行不入表。"""
    out = _fb(monkeypatch, tmp_path, [
        ("2026-09-09", "US.DDD", "SELL_SHORT", None, None),
        ("2026-09-09", "US.EEE", "BUY", 0.01, None),
    ])
    assert "DDD" not in "".join(out), "待回填行没有方向信息, 入表即纯占位噪音"
    assert "EEE" in "".join(out)


def test_feedback_table_ignores_other_sources(monkeypatch, tmp_path, _frozen_today):
    """只回看本源 (universe_expansion): 他源指令不得混进本源的负反馈表。"""
    out = _fb(monkeypatch, tmp_path, [
        ("2026-09-09", "US.FFF", "SELL_SHORT", 0.05, None, '{"source": "deep_stock_analysis"}'),
        ("2026-09-09", "US.GGG", "SELL_SHORT", 0.05, None),
    ])
    assert "FFF" not in "".join(out)
    assert "GGG" in "".join(out)


def test_feedback_table_window_and_cap_respected(monkeypatch, tmp_path, _frozen_today):
    """窗外行不入表; max_rows 截断仍生效 (修不得改动取样口径, 只改符号)。"""
    rows = [("2026-08-01", "US.OLD", "SELL_SHORT", 0.09, None)]
    rows += [(f"2026-09-0{i + 4}", f"US.S{i}", "SELL_SHORT", 0.01 * (i + 1), None) for i in range(4)]
    out = _fb(monkeypatch, tmp_path, rows)
    assert "OLD" not in "".join(out), "7 日窗外的行不得入表"
    assert len(out) == 4, f"窗内 4 行应全入表: {out}"
    capped = _fb(monkeypatch, tmp_path, rows, max_rows=2)
    assert len(capped) == 2


# ══════════ ①b U1b 零收益三态 (2026-09-11, 用户裁"挂起到 U2 落定后" → U2 已落定 ⇒ 落地) ══════════
#
# 缺陷形态不是"符号反了", 而是 prompt **自我矛盾**: 表头原文「负=推错」、抑制规则原文
# 「连续推错(连续负收益)…不得再次浮现」都把"推错"定义为**负**, 而渲染式 `adj > 0 else ✗`
# 把 adj==0 归进 ✗ ⇒ 一次持平被当一次推错喂进抑制规则, 可能压掉一个本不该压的浮现。
# 实测血量 (只读生产库, 2026-09-11 复测): 全历史 172 行有 fwd 的 UE 指令中**真零 3 行**
# (全为 SELL_SHORT: 07-31 US.SKYT / 09-01 US.CRNX / 09-01 US.NCSM);
# 活体窗口 (date>=今日-7d) **15 行 / 真零 0 行**, 函数 max_rows 默认 10 ⇒ 实际入表 10 行。
# ⇒ 低频但非零、方向明确 (关乎抑制的正确性), 故修。
# 修法已定稿 (用户裁): 三态 ✓/✗/＝ + 同步改表头 :1332 + **精确** adj==0 判定。

_ZERO_CELLS = [("BUY", "intraday"), ("BUY", "t1"),
               ("SELL_SHORT", "intraday"), ("SELL_SHORT", "t1")]


@pytest.mark.parametrize("direction,horizon", _ZERO_CELLS)
def test_zero_return_renders_neutral_not_miss(
        monkeypatch, tmp_path, _frozen_today, direction, horizon):
    """精确零收益 → ＝ (不计推错), 不得再落 ✗。方向×周期 2×2 全覆盖。"""
    row = (("2026-09-08", "US.ZZZ", direction, 0.0, None) if horizon == "intraday"
           else ("2026-09-08", "US.ZZZ", direction, None, 0.0))
    ln = _line(_fb(monkeypatch, tmp_path, [row]), "ZZZ")
    assert "+0.0%" in ln, f"量纲/符号错: {ln}"
    assert "＝" in ln, f"零收益仍被判 ✗ = 把持平当推错喂抑制规则: {ln}"
    assert "✗" not in ln, ln
    assert "✓" not in ln, ln


def test_negative_zero_storage_is_still_neutral(monkeypatch, tmp_path, _frozen_today):
    """SQLite REAL 可存 -0.0; Python `-0.0 < 0` 为 False ⇒ 天然落 ＝。钉住不得回潮。"""
    ln = _line(_fb(monkeypatch, tmp_path,
                   [("2026-09-08", "US.NZ", "SELL_SHORT", -0.0, None)]), "NZ")
    assert "＝" in ln and "✗" not in ln, ln


def test_tiny_nonzero_must_not_be_absorbed_into_neutral(monkeypatch, tmp_path, _frozen_today):
    """判定是**精确零**, 不是显示舍入零 —— 这条专防"顺手放宽成容差带"。

    实测反例: 2026-08-11 US.CVX 真值 **+0.0249%**, 渲染为 "+0.0%"。若改判据为
    `round(adj, 1) == 0` 或 `|adj| < 0.05`, 这笔**真实盈利**会被改判持平 ⇒ 把渲染层的
    显示精度当成业务判据。显示可以粗, 判据必须用原值。
    (夹具日期取窗内 09-08: 本例钉的是**数值**, 与日期无关; 真值来自 08-11 那一行。)
    """
    ln = _line(_fb(monkeypatch, tmp_path,
                   [("2026-09-08", "US.CVX", "BUY", 0.000249, None)]), "CVX")
    assert "+0.0%" in ln, f"本例前提: 显示确为 +0.0%: {ln}"
    assert "✓" in ln, f"真实非零小收益被判中性 = 用显示精度当业务判据: {ln}"
    assert "＝" not in ln, ln


def test_prompt_side_defines_all_three_states():
    """三态必须同步写进 prompt 文本: 只改渲染不改表头, 模型仍会把 ＝ 读成一种推错。

    源级钉 (不跑 LLM、不改 brief 生成路径)。
    """
    src = (REPO / "scripts" / "llm_universe_scan.py").read_text(encoding="utf-8")
    assert "✓=推对盈利" in src, "表头未定义 ✓"
    assert "✗=推错亏损" in src, "表头未定义 ✗"
    assert "＝=零收益持平" in src, "表头未定义 ＝ — 模型无从知道第三态含义"
    assert "不得计入「连续推错」" in src, \
        "抑制规则未显式排除 ＝ ⇒ 代码侧三态在 prompt 侧没有对应语义"
    assert "方向调整后前瞻收益, 负=推错)]" not in src, \
        "旧两态表头仍在 = 与三态渲染互相矛盾 (本缺陷的原始形态)"


def test_prompt_version_bumped_and_blind_spot_declared():
    """用户裁「升 v2.11 记 U1, 缺口另立项」: brief 侧变更也留版本戳, 否则 9/11 前后样本不可分。

    同时钉住**盲区声明在场**: PROMPT_HASH_V2 只哈希 SYSTEM_PROMPT_V2, 本版两处变更都不在
    system prompt 内 ⇒ 升版号不改变哈希。不写这句, v2.11 会被误读成"指纹已覆盖 brief"。

    [2026-09-12 补记] 该盲区已由 P2.5 (DEB-20260910 §17) 补上 brief 侧指纹基建
    (`BRIEF_TEMPLATE_VERSION` / `_brief_fingerprints`), 故 v2.12 起 brief 变更可被自动指纹
    看见。但**哈希口径本批仍不动** —— 它是既有 thinking-eval A/B 预注册判据的切分键,
    改它=中途换尺子, 须用户裁。下面最后一条断言因此继续有效。
    """
    src = (REPO / "scripts" / "llm_universe_scan.py").read_text(encoding="utf-8")
    # 版本戳棘轮跟**当前**版号走 (升版者必须同时改这里 — 变红是设计意图, 不是回归)。
    # v2.13→v2.14 (2026-10-02, 证据分层/校准/反证/事件阶段契约, brief/user 侧变更)。
    assert 'PROMPT_VERSION = "v2.14"' in src, \
        "版本戳已升过 v2.14 — 同步改本钉 (棘轮意图: 升版必留痕)"
    # U1 的变更记录不得随后续升版被顺手抹掉 (删注释 = 归因链断, 9/11 前后样本不可分)。
    # v2.14 (2026-10-02, ADR-044) 将原 PROMPT_VERSION 行长注释截短, v2.11 归因从
    #   版本行移入 P2.5 brief 指纹段 (line ~221: "v2.11 改的正是反馈表渲染")。
    #   钉点随之迁移: 只要 v2.11 教训在仓内**任一处**可检索即视为归因链不断。
    assert "v2.11" in src and "反馈表" in src, "U1 版号变更记录被删 — 归因链断"
    # 盲区声明: v2.14 后措辞演进为 "只覆盖系统 prompt" (line ~217), 语义等价。
    assert ("只覆盖系统 prompt" in src or "只哈希 SYSTEM_PROMPT_V2" in src), \
        "PROMPT_HASH_V2 盲区声明被删 — 后人会误以为哈希覆盖了 brief"
    # 哈希口径本批**不动** (缺口另立项, 需独立授权) — 被动过即红
    assert ('PROMPT_HASH_V2 = hashlib.sha1(SYSTEM_PROMPT_V2.encode("utf-8"))'
            '.hexdigest()[:12]') in src


# ══════════ ② entry_gate_replay: 同族缺陷, 但脚本系台账"证据冻结" ⇒ 只钉现状不修 ══════════
#
# docs/REPLAY-SCRIPT-LEDGER.md 状态定义: 「证据冻结 = 被已裁决 DEB 引用为证据基线 —
# **不可改不可删** (改了=裁决记录失真)」, entry_gate_replay.py 在冻结名单内。
# 只读实测 (2026-09-11): 其唯一产物 data/entry_gate_replay_latest.json (mtime 08-01)
# 全仓**零程序性读者**; DEB-20260726 引用的两轮数字都走 replay_timing 现算路径
# (bar 覆盖 307/308 与 119/120 ⇒ :77 兜底每轮 ≤1 条) ⇒ 该缺陷**零活体血量**,
# 且**未污染任何已裁决结论**。
# ⇒ 处置: 不动代码 (我曾改过一版, 已 `git checkout` 撤回到与 HEAD 逐字节相同),
#   按仓内既有"债标记测试"体例钉住现状 —— 冻结例外获批之日, 下面两例**必须变红并被
#   改写成正确断言**, 而不是继续绿。键名 fwd_signed 本身就是那个错误假设的化石。

_LEDGER = REPO / "docs" / "REPLAY-SCRIPT-LEDGER.md"


def _load(monkeypatch, tmp_path, rows, since="2026-09-01"):
    db = _mk_db(tmp_path, rows)
    monkeypatch.setattr(egr, "DB_PATH", db)
    return egr.load_instructions(since)


def test_entry_gate_replay_is_ledger_frozen():
    """钉住"为什么不修": 一旦台账把它解冻, 本例变红 ⇒ 提醒把下面两条债标记改成正确断言。"""
    txt = _LEDGER.read_text(encoding="utf-8")
    row = [ln for ln in txt.splitlines() if ln.startswith("| entry_gate_replay.py")]
    assert row, "台账里找不到 entry_gate_replay.py 行 — 台账结构变了, 先重判冻结状态"
    assert "证据冻结" in row[0], f"已解冻 ⇒ 应按 U1 正式修 :77 并改写两条债标记: {row[0]}"
    assert "不可改不可删" in txt, "冻结状态的定义正文被改动 ⇒ 重新判处置通道"


def test_debt_replay_fallback_still_double_signs(monkeypatch, tmp_path):
    """债标记 (缺陷现状): 兜底值仍被二次签名 ⇒ SHORT 行反号, 与 llm_universe_scan 修前同族。

    现状 (HEAD `:77`) = 反号 + **留 fraction** (不换算成 percentage)。
    修好之后本例**应当变红**; 届时把断言改成 `== pytest.approx(-3.992)` / `15.8263`
    (即"不再反号"且"与主路径同量纲"), 并把键名换成能体现"已签名"的名字 ——
    `fwd_signed` 这个键名本身就是那个错误假设的化石: 它暗示"这里才签名",
    而真相是 DB 里存的**早就签过了**。
    """
    ins = _load(monkeypatch, tmp_path, [
        ("2026-09-03", "US.NTAP", "SELL_SHORT", -0.158263, None),
        ("2026-09-04", "US.TTD", "SELL_SHORT", 0.03992, None),
        ("2026-09-08", "US.LNG", "BUY", 0.02, None),
    ])
    by = {i["symbol"]: i for i in ins}
    assert by["US.NTAP"]["fwd_signed"] == pytest.approx(0.158263), "SHORT 亏钱却得正数 = 反号现状"
    assert by["US.TTD"]["fwd_signed"] == pytest.approx(-0.03992), "SHORT 赚钱却得负数 = 反号现状"
    assert by["US.LNG"]["fwd_signed"] == pytest.approx(0.02), "BUY 乘 1 本来就对, 不受本缺陷影响"


def _bars(seq):
    return [{"t": datetime(2026, 9, 4, hh, mm, tzinfo=ET), "c": c, "h": max(c, h), "l": min(c, h)}
            for (hh, mm), c, h in seq]


def test_debt_replay_fallback_unit_is_100x_off_from_timing_path(monkeypatch, tmp_path):
    """债标记 (缺陷现状): 同一笔 SHORT「100→95」两条路径差 100 倍量纲。

    主路径 replay_timing 出 percentage (+5.0), 兜底 load_instructions 出 fraction (−0.05),
    而 outcome() 把两者混进同一个均值 ⇒ ATR 门网格"拦掉组均%<0=拦对"的定性跨量纲失真。

    **这是漏改不是设计**: 同函数 `:70-71` 的注释自己写着「DB 存 fraction (0.038=3.8%),
    门阈值用 percentage — 统一乘 100」, 且紧接着的 `:74` 确实给 `atr_pct` 乘了 100,
    偏偏 `:77` 的 `fwd_signed` 没乘。
    修好之后本例**应当变红**; 届时改成断言两者相等 (都 +5.0)。
    """
    bars = _bars([((9, 35), 100.0, 100.5), ((10, 0), 100.0, 100.2), ((16, 0), 95.0, 100.0)])
    one = _load(monkeypatch, tmp_path, [("2026-09-04", "US.QQQ", "SELL_SHORT", 0.05, None)])[0]
    timing_ret = egr.replay_timing(one, bars)[egr.GATE_REF_TIMING]["ret"]
    assert timing_ret == pytest.approx(5.0), "主路径 (现算价差×sign) 口径正确, 不得被本债影响"
    assert one["fwd_signed"] == pytest.approx(-0.05), "兜底路径现状: 反号 + fraction"
    assert abs(timing_ret) / abs(one["fwd_signed"]) == pytest.approx(100.0), \
        "量纲差应为 100×; 若本例变红说明兜底单位已修 (好) 或主路径单位被改 (坏) — 先看前者"


def test_replay_fwd_none_stays_none(monkeypatch, tmp_path):
    """非债部分: 无回填 ⇒ None (不得被折算成 0, 0 与"没数据"在均值里语义不同)。"""
    ins = _load(monkeypatch, tmp_path, [("2026-09-03", "US.PPP", "SELL_SHORT", None, None)])
    assert ins[0]["fwd_signed"] is None


# ══════════ ③ 契约正文留在写侧 (单一真源) ══════════

def test_write_side_contract_is_documented():
    """契约必须留在写侧模块 docstring: 它是唯一能阻止下一个作者重复签名东西。"""
    src = (REPO / "app" / "playbook" / "ic_tracker.py").read_text(encoding="utf-8")
    head = src.split('"""', 2)[1]
    assert "fwd_return_* 符号契约" in head, "契约段被删 = 下一个读方会重犯 U1"
    assert "已按方向签名" in head
    assert "读侧一律不得再乘方向" in head


def test_design_exception_is_recorded_so_nobody_fixes_correct_code():
    """`sign × fwd` 在 loser_hold_path_replay 是**正确的反签名** (还原价位)。

    不记这一条, 日后清剿 U1 同族时会把它当 bug 改掉 ⇒ 价位重建静默错 100 倍方向。
    """
    src = (REPO / "app" / "playbook" / "ic_tracker.py").read_text(encoding="utf-8")
    head = src.split('"""', 2)[1]
    assert "反签名还原价位" in head
    assert "loser_hold_path_replay" in head
    # 该正确用法本身仍在位 (被"修"掉即红)
    lp = (REPO / "scripts" / "loser_hold_path_replay.py").read_text(encoding="utf-8")
    assert 'r["entry_price"] * (1 + sign * r["fwd_return_t1"])' in lp, \
        "反签名还原价位的写法被改动 — 若确需改动, 先改 ic_tracker 契约段再改这里"


def test_ic_ruler_depends_on_signed_values():
    """IC 判据前提: compute_playbook_ic 直接拿存储值与 confidence 求相关 ⇒ 必须已签名。

    钉住"不再签名"这件事在写侧仍然成立 (ic_tracker 内部只签一次)。
    """
    src = (REPO / "app" / "playbook" / "ic_tracker.py").read_text(encoding="utf-8")
    assert src.count('sign = 1 if direction == "BUY" else -1') == 1, \
        "写侧签名点应恰为 1 处; 多出来的一处意味着某条回填列被签了两次"


# ══════════ ④ U1b: 第二把尺子同族反号 (direction_counterfactual_ruler, 活体 cron) ══════════
#
# 同一个错误假设的第二个受害者, 但**在另一张库上**, 且危害形态不同:
#   `signal_snapshots.fwd_ret_*` 写侧 `app/analysis/signal_ic_tracker.py::_calc_fwd` 用
#   **倒数式**签名 (LONG=(tn/t0)−1, SHORT=(t0/tn)−1), 与 `playbook_instructions` 的
#   **乘号式**约定相同 (都是"正=该方向赚钱"), 只是公式不同。
#   尺子 B 节误以为这张库是"原始 close-to-close 未方向调整" (修前 docstring 原文),
#   于是又乘了一次 sign ⇒ SHORT 全反号。
# 实测 (只读生产库, 2026-09-11): 1366/1366 SHORT 行匹配倒数式, 仅 11 行 (close_t0==close_t1
#   退化) 匹配未签名式 ⇒ 约定确定无疑。
# 危害比 U1 更直接: B 节的 `flag` 判据是 `cost_adj > 0` (cost=13bp), 而 SHORT 全窗真值
#   +0.159% ⇒ 修前 cost_adj=−0.289 判 "pool_does_not_support_this_side", 修后 +0.029 判
#   "pool_supports_this_side"。**自 2026-08-20 立项起每日 06:50 cron 用反过来的证据反对做空。**
# 该脚本**不在** REPLAY-SCRIPT-LEDGER (非证据冻结), 产物 json/jsonl **零程序性读者**
#   (纯遥测), 且 A 节 (唯一交易口径, 驱动过 T+5 决策) 一直没再签名 ⇒ 修 B 节不影响任何
#   已裁决结论, 也不改交易路径。
# 同仓另有 `scripts/factor_ic_backfill.py:134` 早就写对了 ("fwd_ret_t1 已是方向调整过的")
#   ⇒ 仓内对同一列曾**并存两种矛盾记载**, 错的那份在活体尺子里。这正是契约必须留在写侧的理由。

_SNAP_COLS = ("id INTEGER PRIMARY KEY, trade_date TEXT, strategy_id TEXT, direction TEXT, "
              "fwd_ret_t1 REAL, fwd_ret_t5 REAL, fwd_ret_t20 REAL, "
              "alpha_t1 REAL, alpha_t5 REAL, alpha_t20 REAL")


def _mk_snap(tmp_path, rows):
    """rows: (trade_date, strategy_id, direction, fwd_t1, alpha_t1)"""
    db = tmp_path / "signal_snapshots.db"
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE IF EXISTS signal_snapshots")
    conn.execute(f"CREATE TABLE signal_snapshots ({_SNAP_COLS})")
    conn.executemany(
        "INSERT INTO signal_snapshots (trade_date, strategy_id, direction, fwd_ret_t1, alpha_t1) "
        "VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def _cand(tmp_path, rows, since="2026-07-11"):
    import direction_counterfactual_ruler as dcr
    return dcr, dcr.section_candidate_counterfactual(_mk_snap(tmp_path, rows), since)


# 生产实测均值 (只读复算, 全窗): SHORT +0.159% / LONG −0.055% / NEUTRAL +0.128%。
# n 必须 >= MIN_N(30) 才会给 flag, 否则恒为 insufficient_n ⇒ 判据测不到。
_PROD_SHORT_T1, _PROD_SHORT_A1 = 0.00159, -0.00005
_PROD_LONG_T1 = -0.00055
_PROD_NEUTRAL_T1, _PROD_NEUTRAL_A1 = 0.00128, 0.00021


def test_ruler_short_side_keeps_stored_sign_and_flips_flag(tmp_path):
    """核心钉: SHORT 真值 +0.159% 必须报成正数, 且费后 >0 ⇒ pool_supports_this_side。

    修前这三条全部相反 (−0.159% / flip +0.159% / pool_does_not_support_this_side)。
    """
    dcr, out = _cand(tmp_path, [
        ("2026-08-20", "deep_analysis", "SHORT", _PROD_SHORT_T1, _PROD_SHORT_A1)] * 30)
    row = out["DA"]["SHORT"]
    assert row["n_by_horizon"]["T+1"] == 30, "n<MIN_N 会让 flag 退化成 insufficient_n, 测不到判据"
    ns = row["nominated_side"]["T+1"]
    assert ns["dir_adj_pct"] == pytest.approx(0.159), \
        f"SHORT 已签名收益被二次反号 (U1b 回潮): {ns}"
    assert row["counterfactual_flip"]["T+1"]["dir_adj_pct"] == pytest.approx(-0.159)
    da = row["dir_adj_alpha"]["T+1"]
    assert da["dir_adj_alpha_pct"] == pytest.approx(-0.005), f"alpha 同样不得再签名: {da}"
    assert da["cost_adj_pct"] == pytest.approx(round(0.159 - dcr.COST_BPS_ROUNDTRIP / 100.0, 3))
    assert da["flag"] == "pool_supports_this_side", \
        "费后为正却判不支持空头 = 修前那个每日反向结论"


def test_ruler_long_and_neutral_not_overcorrected(tmp_path):
    """LONG 修前就是对的 (sign=+1) ⇒ 本修不得改坏它; NEUTRAL 仍报原始漂移不签名。"""
    _, out = _cand(tmp_path, [
        ("2026-08-20", "deep_analysis", "LONG", _PROD_LONG_T1, None)] * 30
        + [("2026-08-20", "universe_expansion", "NEUTRAL", _PROD_NEUTRAL_T1, _PROD_NEUTRAL_A1)] * 30)
    lng = out["DA"]["LONG"]
    assert lng["nominated_side"]["T+1"]["dir_adj_pct"] == pytest.approx(-0.055)
    assert lng["counterfactual_flip"]["T+1"]["dir_adj_pct"] == pytest.approx(0.055)
    assert lng["dir_adj_alpha"]["T+1"]["flag"] == "pool_does_not_support_this_side"
    neu = out["UE"]["NEUTRAL"]["nominated_side"]["T+1"]
    assert neu["raw_drift_pct"] == pytest.approx(0.128), "NEUTRAL 走 _calc_fwd 的 else 分支 = 原始收益"
    assert neu["alpha_pct"] == pytest.approx(0.021)
    assert "dir_adj_pct" not in neu, "NEUTRAL 无方向, 不得给提名侧读数"


def test_ruler_flip_is_negation_of_nominated(tmp_path):
    """对侧反事实恒等于提名侧取负 (一阶近似) — 修前 SHORT 侧两者**同号**, 自相矛盾。"""
    _, out = _cand(tmp_path, [
        ("2026-08-20", "universe_expansion", "SHORT", _PROD_SHORT_T1, _PROD_SHORT_A1)] * 30)
    row = out["UE"]["SHORT"]
    nom = row["nominated_side"]["T+1"]["dir_adj_pct"]
    flip = row["counterfactual_flip"]["T+1"]["dir_adj_pct"]
    assert flip == pytest.approx(-nom), f"提名侧与对侧必须反号, 实得 {nom} / {flip}"
    assert nom > 0, "空头池真值为正 ⇒ 修前 nom<0 且 flip>0 = 两边都指向'别做空'"


def test_ruler_never_resigns_structurally():
    """结构钉: 两节里都不得出现 `sign` 这个标识符 ⇒ 再签名无法静默回潮。

    用 AST 取标识符而非 grep 文本: docstring 里讲"签名/已签名"是必要的, 不该被误判。
    """
    import ast
    import direction_counterfactual_ruler as dcr
    tree = ast.parse((REPO / "scripts" / "direction_counterfactual_ruler.py").read_text(encoding="utf-8"))
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in (
                "section_candidate_counterfactual", "section_instruction_matrix"):
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            assert "sign" not in names, \
                (f"{fn.name} 又出现了 sign 变量 —— 两张库的 fwd 列**都已签名**, "
                 f"读侧再乘方向即 U1/U1b 回潮 (写侧 SSOT: {dcr.__name__} 见 ic_tracker 契约)")


def test_second_db_sign_convention_is_pinned_at_its_writer():
    """第二张库的约定 SSOT: `_calc_fwd` 的 SHORT 倒数分支必须在位。

    它一被"统一成乘号式"或"去掉方向分支", 上面所有 U1b 钉子的前提就变了 ⇒ 必须先改契约。
    """
    src = (REPO / "app" / "analysis" / "signal_ic_tracker.py").read_text(encoding="utf-8")
    assert 'if direction == "SHORT":' in src
    assert "return (t0 / close_tn) - 1.0" in src, "SHORT 倒数式被改 ⇒ signal_snapshots 约定变了"
    assert "return (close_tn / t0) - 1.0" in src
    assert "前瞻收益率 T+1 (方向调整)" in src, "DDL 上的方向调整注释被删 ⇒ 下一个读方会重犯 U1b"
