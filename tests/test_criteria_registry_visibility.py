"""守卫: 判据字典「影子类」条目必须在影子注册表可见 (2026-09-26 立项).

立起理由 (实测危害, 非假想):
  docs/ADJUDICATED-CRITERIA.md 是判据 SSOT, app/playbook/shadow_registry.py REGISTRY 是
  周检/哨兵/意图守卫/pending_adjudication 的唯一入口。两处**无强制同步** ⇒ 判据字典登记了、
  cron 挂了、结算器写了, 但注册表查无 ⇒ 一切自动化看不到, 判据永远不会被读。

  两次同型实锤:
    ① short_funnel_shadow — 8/16 起达标 (30/10 交易日) 却从未被通知 (RFC-092 补登记留痕);
    ② B19/B21/B22 — 2026-09-26 扫描发现三条有判据+有 cron+有结算器却注册表查无。
       其中 **B21 判据 n 轴实测 276 (门 20) 早已达标, 一个月从未被读过**;
       B22 是登记它的那个 agent 自己漏的 ⇒ 缺口是结构性的, 不是偶发疏忽。

  修法选源头 (MEMORY: 删代码消除根因 > 加校验打补丁 / 源头过滤 > 层层校验):
  不加"提醒人工同步"的文档纪律, 直接让 CI 红 —— 登记判据字典时漏同步注册表 = 测试失败。

纪律边界:
  - 只查**影子类**判据 (含 shadow/影子/record-only 字样), 纪律类判据 (A1 最小样本 /
    A5 双跑窗口 / C4 影子先行 / C7 冻结期) 不是观测事项, 不进注册表, 白名单豁免;
  - 匹配口径三选一即算覆盖: 判据 id (B19) 出现在任一条目的 notes/description/
    graduation_criteria, 或判据名括注的 slug (eod_depth_gate) == 注册表 id,
    或 slug 在注册表源码中出现;
  - 本守卫**不判判据对错**, 只判"可见性"; 判据达标与否由 refresh_counts + eval_script 管。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CRITERIA = REPO / "docs" / "ADJUDICATED-CRITERIA.md"
REGISTRY_PY = REPO / "app" / "playbook" / "shadow_registry.py"

# 纪律类判据 (非观测事项, 不该进注册表) — 按判据 id 白名单豁免。
# 加条目须写理由; 不得为了过测试把真影子塞进来 (那是掩盖缺口)。
NON_SHADOW_IDS = {
    "A1",  # 影子攒样最小样本 (纪律: 所有影子共用的样本量下限)
    "A5",  # 影子双跑窗口 (纪律: OOS 双窗口才采信)
    "C4",  # 影子先行纪律 (纪律: 改行为前先影子)
    "C7",  # 冻结期不动退出参数, 修尺子为主轴 (纪律)
}

_SHADOW_HINT = re.compile(r"shadow|影子|record-only|record only", re.I)
_ID_RE = re.compile(r"^[A-Z]\d+$")
_SLUG_RE = re.compile(r"\(([a-z][a-z0-9_]{3,})\)")
# 裸 snake_case 名称列 (B3 型: 判据名本身就叫 `da_glm_shadow`, 无括注) —
# 只认「含下划线且全小写」以免把中文/英文散文名当 slug。
_BARE_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
# 白名单滥用检测: 只认**具体观测载体**证据, 不认裸词。
# 为什么不用裸 `crontab`: C7 判据文写「台账 crontab 盲区棘轮」是散文提及 (讲的是修尺子
# 这件事本身), 按裸词匹配会误报真纪律项 ⇒ 守卫狼来了, 下次真影子塞白名单就没人信。
_CARRIER_RE = re.compile(
    r"data_path|eval_script|data/[\w./-]+\.jsonl|data/[\w./-]+\.json\b"
    r"|结算器\s*scripts/|count_mode", re.I)


def _criteria_rows() -> list[tuple[str, str]]:
    """判据字典表格行 → [(判据id, 整行文本)]。"""
    out = []
    for ln in CRITERIA.read_text(encoding="utf-8").split("\n"):
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) < 4:
            continue
        cid = cells[1]
        if not _ID_RE.fullmatch(cid or ""):
            continue
        out.append((cid, ln))
    return out


def test_criteria_file_and_registry_exist():
    """前置: 两个 SSOT 文件都在 (路径漂移时给出明确失败, 不是静默 0 行)。"""
    assert CRITERIA.exists(), f"判据字典缺失: {CRITERIA}"
    assert REGISTRY_PY.exists(), f"影子注册表缺失: {REGISTRY_PY}"


def test_criteria_rows_are_parsed():
    """守卫自身不得静默 0 行 (元环规则5: 「零违反」与「守卫崩了」必须可区分)。"""
    rows = _criteria_rows()
    assert len(rows) >= 10, f"判据字典只解析出 {len(rows)} 行 — 表格格式漂移? 守卫会假绿"


def test_shadow_criteria_are_visible_in_registry():
    """影子类判据必须在注册表可见 (id 提及 / slug==注册表id / slug 在源码出现)。"""
    from app.playbook.shadow_registry import REGISTRY

    reg_ids = {s.id for s in REGISTRY}
    reg_text_blob = "\n".join(
        (s.notes or "") + "\n" + (s.description or "") + "\n" + (s.graduation_criteria or "")
        for s in REGISTRY
    )
    reg_src = REGISTRY_PY.read_text(encoding="utf-8")

    gaps = []
    for cid, ln in _criteria_rows():
        if cid in NON_SHADOW_IDS:
            continue
        if not _SHADOW_HINT.search(ln):
            continue  # 非影子类判据 (硬门/口径/纪律), 不进注册表
        m = _SLUG_RE.search(ln.split("|")[2] or "")
        slug = m.group(1) if m else ""
        if not slug:
            # B3 型: 名称列本身就是裸 snake_case 标识符 (无括注)
            _name = (ln.split("|")[2] or "").strip()
            if _BARE_SLUG_RE.fullmatch(_name):
                slug = _name
        covered = (
            cid in reg_text_blob            # 判据 id 被某条目引用
            or (slug and slug in reg_ids)   # 判据名括注的 slug == 注册表 id
            or (slug and slug in reg_src)   # slug 在注册表源码出现 (含 cross_ref)
        )
        if not covered:
            gaps.append(f"{cid} ({(ln.split('|')[2] or '').strip()[:40]}"
                        f"{f' slug={slug}' if slug else ''})")

    assert not gaps, (
        f"判据字典有 {len(gaps)} 条影子类判据在注册表查无 ⇒ 周检/哨兵/意图守卫全盲, "
        f"判据永远不会被读 (实锤: B21 n=276 达标一个月未被读; short_funnel_shadow 8/16 起"
        f"未被通知)。缺: {gaps}。修法: 在 app/playbook/shadow_registry.py REGISTRY 补 "
        f"ShadowItem (判据文/阈值**逐字照抄**, 改=重裁), 并按判据 n 轴设 count_mode "
        f"(源 jsonl 行数 ≠ 判据 n 轴时用 json_key:<结算器键>, 否则虚假迁 READY) + "
        f"ADJUDICATED 已读登记 (若当日已实跑判据)。不得把判据塞进 NON_SHADOW_IDS 绕过。"
    )


def test_non_shadow_whitelist_is_not_abused():
    """白名单不得被当后门: 每项必须真是纪律类 (不含具体观测载体证据)。"""
    rows = {cid: ln for cid, ln in _criteria_rows()}
    for cid in NON_SHADOW_IDS:
        ln = rows.get(cid)
        if ln is None:
            continue  # 判据字典重编号时不硬失败, 由上一条守卫兜
        # 白名单项若含**具体观测载体**证据 = 疑似真影子被塞进来绕过
        suspicious = _CARRIER_RE.search(ln)
        assert not suspicious, (
            f"{cid} 在 NON_SHADOW_IDS 白名单里, 但其判据文含观测载体字样 "
            f"({suspicious.group(0)}) ⇒ 疑似真影子被塞进白名单绕过守卫。"
            f"请移出白名单并补注册表登记。"
        )


# ── 阳性对照 (协议 Gate 4: 拦截不安全 AND 放行安全) ──
# 为什么必须有: 一个恒绿的守卫 = 零价值 + 假安全感。上面两条断言当前都"通过",
# 但必须证明**该红的时候真会红** — 否则无法区分「缺口已补齐」与「守卫写错了永不触发」
# (元环规则5: 「零违反」与「守卫崩了」必须可区分)。用临时判据字典注入已知缺口,
# 断言守卫报 AssertionError, 且错误信息点名了注入的那条。
def test_guard_actually_fires_on_missing_registration(monkeypatch, tmp_path):
    """阳性对照①: 注入一条注册表查无的影子判据 ⇒ 守卫必须红且点名它。"""
    import sys

    import pytest

    fake = tmp_path / "CRITERIA.md"
    fake.write_text(
        "| id | 名称 | 判据 | 依据 |\n"
        "|---|---|---|---|\n"
        # X99: 影子类 (含"影子"字样) + slug 注册表绝无 ⇒ 必须被抓
        "| X99 | 注入的假影子 (zzz_never_registered_slug) | 影子 record-only n≥20 | 测试 |\n"
        # B3 真条目风格保留一条已覆盖的, 证明不是"见影子就红"
        "| B3 | da_glm_shadow | 采集环已退役 | 测试 |\n",
        encoding="utf-8",
    )
    # 注意: 必须用 sys.modules[__name__] — __import__(__name__) 在包内测试里返回
    # **顶层包** (tests), 打错对象会 AttributeError (2026-09-26 初版踩)。
    monkeypatch.setattr(sys.modules[__name__], "CRITERIA", fake)
    with pytest.raises(AssertionError) as ei:
        test_shadow_criteria_are_visible_in_registry()
    msg = str(ei.value)
    assert "X99" in msg, f"守卫红了但没点名注入项 (错误信息不可诊断): {msg[:200]}"
    assert "zzz_never_registered_slug" in msg, "守卫应回显 slug 供定位"
    assert "B3" not in msg, "B3 (da_glm_shadow) 已在注册表, 不得误报"


def test_guard_actually_fires_on_whitelist_abuse(monkeypatch, tmp_path):
    """阳性对照②: 把真影子塞进 NON_SHADOW_IDS 白名单 ⇒ 滥用检测必须红。"""
    import sys

    import pytest

    fake = tmp_path / "CRITERIA.md"
    fake.write_text(
        "| id | 名称 | 判据 | 依据 |\n|---|---|---|---|\n"
        "| Y7 | 被塞进白名单的真影子 | data_path=data/foo.jsonl, eval_script=x.py | 测试 |\n",
        encoding="utf-8",
    )
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "CRITERIA", fake)
    monkeypatch.setattr(mod, "NON_SHADOW_IDS", mod.NON_SHADOW_IDS | {"Y7"})
    with pytest.raises(AssertionError) as ei:
        test_non_shadow_whitelist_is_not_abused()
    assert "Y7" in str(ei.value), f"滥用检测没点名注入项: {str(ei.value)[:200]}"


def test_guard_does_not_fire_on_prose_mention_of_carrier_words(monkeypatch, tmp_path):
    """阳性对照③ (反向): 纪律项散文提及 'crontab' 不得误报 (C7 型假阳回归钉).

    2026-09-26 实锤: 初版滥用检测用裸词 `crontab` 匹配, 把 C7「冻结期不动退出参数,
    修尺子为主轴」判据文里的「台账 crontab 盲区棘轮」(散文, 讲的是修尺子这件事本身)
    误报成"真影子被塞白名单" ⇒ 守卫狼来了。现只认**具体载体证据** (data_path/
    eval_script/具体 .jsonl 路径/count_mode), 本用例钉死该边界。
    """
    import sys

    fake = tmp_path / "CRITERIA.md"
    fake.write_text(
        "| id | 名称 | 判据 | 依据 |\n|---|---|---|---|\n"
        "| C7 | 冻结期不动退出参数 | 台账 crontab 盲区棘轮 / 穿透影子三登记缺失 | 8/23 终裁 |\n",
        encoding="utf-8",
    )
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "CRITERIA", fake)
    monkeypatch.setattr(mod, "NON_SHADOW_IDS", mod.NON_SHADOW_IDS | {"C7"})
    # 不得抛 (散文提及 crontab/影子 不是观测载体)
    test_non_shadow_whitelist_is_not_abused()
