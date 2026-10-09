"""KR2 冻结豁免契约钉子 (2026-09-01 复查收口).

红绿纪律: config_freeze_baseline.json 的 exit_versions 是周检告警的抑制通道 —
豁免清单每多一键 = 一类版本分裂从此不再告警。键集一旦变化本测试必须红,
新增豁免被迫改测试 = 强制留痕; 每条豁免必须带裁决说明。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASELINE = Path(__file__).resolve().parents[1] / "data" / "config_freeze_baseline.json"

KNOWN_EXEMPT = {
    "exit-7f5263af3cfd",   # KR2 冻结基线版本 (8/24-8/28 唯一版本)
    "exit-2623be9c8879",   # 45e62f0f 8/29 early_tight_sl 毕业案, 用户已裁决
    "exit-d1120a105711",   # e32d376f 9/1 用户裁"A" ATR 比例紧 SL + 9/2 R-4b 漏键收尾
    "exit-f837b002896d",   # RFC-073 9/2 用户裁"确认正效应直接上sim" DEAD_SIGNAL隔夜口径对齐
    "exit-de49b1299290",   # DEB-20260903 9/3 用户裁"按建议执行" 被动侧marketable限价(议题二方案A)
    "exit-13f6c2cc7fc4",   # DEB-20260909 9/9 用户裁"同意" M2: BE垫兜底收敛(0.4→0.15+共享常量), 零交易行为变更
    "exit-34c7153088a1",   # DEB-20260917 §20–§22/§23.8 (Codex 66d781dcc + 2d0bcf60f, 9/17 盘前批): adaptive trailing 上限开放; 基线 note 已留痕, 本钉子当时漏同步 (2026-09-24 补登, 非新增豁免 — 裁决先在)
    "exit-ca4487172a6f",   # 410297295 (2026-10-08, RFC-106 §5.2): overnight_gap_stop_pct=3.0 同值显式化, 零行为变更, 整文件哈希副作用; 2026-10-09 resume 会话逐提交二分归因 + 用户裁「继续」; 仅解 W1_exit_version_drift, cohort 两维 BROKEN 维持
}


def test_exit_versions_keyset_pinned():
    bl = json.loads(BASELINE.read_text(encoding="utf-8"))
    keys = set(bl.get("exit_versions", {}).keys())
    assert keys == KNOWN_EXEMPT, (
        f"exit_versions 豁免键集变化: 新增 {keys - KNOWN_EXEMPT} / 移除 "
        f"{KNOWN_EXEMPT - keys} — 新豁免必须走用户裁决并同步本钉子+ADR 留痕"
    )


def test_each_exemption_carries_adjudication_note():
    bl = json.loads(BASELINE.read_text(encoding="utf-8"))
    for k, v in bl.get("exit_versions", {}).items():
        assert isinstance(v, str) and len(v.strip()) >= 8, (
            f"豁免 {k} 缺裁决说明 — 抑制 KR2 告警必须留 adjudication 依据"
        )


# ── A2 回归钉 (2026-10-06 推送治理批) ──────────────────────────────
# 病灶: 周检 check_freeze 的 SQL 用 COALESCE(...,'(null)') 把「metadata_json 缺
#   config_version」折成字面量 '(null)', 与真 exit-* 哈希同维计数 ⇒ 渲染成
#   「⚠️ 参数冻结已破 (KR2 破, 需复盘)」永久重放。实测 8 笔 (PLAYBOOK_SIM 口径,
#   退出日 9/16–9/17) 是写方埋点缺口, 写方已修 (同口径 9/18 起 82 笔零缺失)。
# 为什么钉在渲染层而**不是**把 '(null)' 加进 exit_versions:
#   豁免键 = 从此不再告警 (本文件 KNOWN_EXEMPT 的红绿纪律)。把 '(null)' 登记进去
#   会让**未来写方埋点再次回归时静默通过** ⇒ 缺字段必须保持独立告警类别,
#   语义与「参数版本分裂」不同。本测试钉住这个分离, 防止被后人合并回同一告警。
_CHECKPOINT = Path(__file__).resolve().parents[1] / "ops" / "hermes" / "ftseek_weekly_checkpoint.py"


def test_null_hash_rendered_as_instrumentation_gap_not_freeze_break():
    src = _CHECKPOINT.read_text(encoding="utf-8")
    assert 'v == "(null)"' in src, (
        "周检不再单独识别 '(null)' 退出哈希 — 缺字段会重新混进「参数冻结已破 (KR2 破)」"
        "告警永久重放。缺字段是写方埋点缺口, 必须与真版本分裂分开渲染。"
    )
    assert "退出哈希缺字段" in src, "缺字段告警文案丢失 (须明示是埋点缺口, 非参数分裂)"
    assert "(null)" not in json.loads(BASELINE.read_text(encoding="utf-8")).get("exit_versions", {}), (
        "'(null)' 被登记进 exit_versions 豁免清单 — 这会让未来写方埋点回归时静默通过, "
        "缺字段必须保持可告警, 不得进豁免"
    )
