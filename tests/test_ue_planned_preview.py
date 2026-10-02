"""[P3.1 2026-09-13] UE 生成值留档 (planned_* / planned_preview) — proud-slope-falcon.

被测对象: `scripts/llm_universe_scan.py` 的 `_compute_numbers()` 与 `_cmd_inject_impl()`
写库现场。

立项依据 (DEB-20260910 §18.2/§18.3 只读实测, 生产库 2151 行):
  ① UE 行 `raw_instruction_json` 的 `planned_*` 覆盖率是 **0/183**, 而 DA 侧是
     457/470 (97.2%) ⇒ 触发时 `pipeline.py` 用重定价值整体覆盖
     entry_price/stop_loss/take_profit 三列之后, UE 的生成值在库里**一处都不剩**;
  ② 重定价自己的审计键 (reprice_from/to/original_sl) 又被 `db._TRACKING_FIELDS`
     静默丢弃 ⇒ P3 验收②「生成预览与最终委托可追踪」对 UE **不可测**。
     不是"还没做", 是"做了也没地方读"。

本文件钉三件事:
  A. 预览口径正确 —— 配置里的 default_stop_pct=3.0 / default_tp_pct=6.0 **不是**从
     entry 量出的比例: SL/TP 锚在 compute_entry_band 边界之外
     (KNIFE_FLOOR_PCT=0.03 / CHASE_ZONE_PCT=0.05), 落地是
     BUY −5.91%/+11.30% (RR 1.912)、SHORT +6.09%/−10.70% (RR 1.757)。
     这两组数与 §18.2 抽样的 8 行未触发 UE 生产行逐位吻合, 测试直接用那两个真实
     价位 (US.XOM 167.326 / US.GPC 134.06) 当夹具, 不用自造整数。
  B. **零漂移** —— 既有三键 (entry_zone/stop_loss/take_profit) 的值与返回面**逐字不变**;
     无效价路径仍返回空 zone + 0.0/0.0, 使调用方的 `continue` 守卫行为完全一致。
  C. **没换尺子** —— P3.1 不碰模型输入, 故 PROMPT_VERSION / PROMPT_HASH_V2 /
     SYSTEM_PROMPT_V2 长度 / BRIEF_TEMPLATE_VERSION 四项必须原样。

回滚: 删本文件即可 (代码侧回滚见 DEB §19)。
"""
import inspect
import hashlib
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import llm_universe_scan as ue  # noqa: E402

# §18.2 抽样里的两个真实生产价位 (未触发 UE 行, 列里仍是生成值)
XOM_LONG = 167.326     # id=2476 2026-09-10 BUY        实测 SL 5.91% / TP 11.3%
GPC_SHORT = 134.06     # id=2457 2026-09-09 SELL_SHORT 实测 SL 6.09% / TP 10.7%

# entry_band 常量 (app/playbook/entry_band.py:24-25) 与 UE 配置项
KNIFE = 0.03
CHASE = 0.05


def _pv(direction, entry):
    return ue._compute_numbers(entry, direction)["planned_preview"]


# ══ A. 预览口径 ═══════════════════════════════════════════════════════════

class TestPreviewCaliber:
    def test_long_matches_production_row_percentages(self):
        pv = _pv("LONG", XOM_LONG)
        assert pv["sl_pct_from_entry"] == 5.91
        assert pv["tp_pct_from_entry"] == 11.3
        assert pv["rr"] == 1.912

    def test_short_matches_production_row_percentages(self):
        pv = _pv("SHORT", GPC_SHORT)
        assert pv["sl_pct_from_entry"] == 6.09
        assert pv["tp_pct_from_entry"] == 10.7
        assert pv["rr"] == 1.757

    def test_cfg_percentages_recorded_alongside_effective(self):
        """配置口径与实测口径必须同时留档。

        只记 3.0/6.0 会让读账的人以为生成止损是 3%; 只记 5.91/11.30 则常量一改
        就无法复算历史行。两个都记 ⇒ 历史行自带换算, 不依赖常量不变。
        """
        for direction, entry in (("LONG", XOM_LONG), ("SHORT", GPC_SHORT)):
            pv = _pv(direction, entry)
            assert pv["sl_pct_cfg"] == 3.0
            assert pv["tp_pct_cfg"] == 6.0
            assert pv["sl_pct_from_entry"] > pv["sl_pct_cfg"]
            assert pv["tp_pct_from_entry"] > pv["tp_pct_cfg"]

    def test_risk_distance_is_abs_entry_minus_stop(self):
        for direction, entry in (("LONG", XOM_LONG), ("SHORT", GPC_SHORT)):
            nums = ue._compute_numbers(entry, direction)
            pv = nums["planned_preview"]
            assert pv["risk_distance"] == pytest.approx(
                abs(entry - nums["stop_loss"]), abs=1e-4)
            assert pv["risk_distance"] > 0

    def test_entry_zone_is_compute_entry_band_output(self):
        from app.playbook.entry_band import compute_entry_band
        pv = _pv("LONG", XOM_LONG)
        assert pv["entry_zone"] == list(compute_entry_band(XOM_LONG, False))
        # SL 严格低于 zone low, TP 严格高于 zone high (Track A 的原始不变式)
        assert pv["stop_loss"] < pv["entry_zone"][0]
        assert pv["take_profit"] > pv["entry_zone"][1]

    def test_short_geometry_is_above_below(self):
        pv = _pv("SHORT", GPC_SHORT)
        assert pv["stop_loss"] > pv["entry_zone"][1]
        assert pv["take_profit"] < pv["entry_zone"][0]

    def test_direction_uses_playbook_vocabulary_not_scan_vocabulary(self):
        """scan 侧说 LONG/SHORT, playbook 侧说 BUY/SELL_SHORT。

        留档必须用**落库口径** (BUY/SELL_SHORT), 否则 P4 漏斗按 direction 分层时
        会出现第三套词汇。
        """
        assert _pv("LONG", XOM_LONG)["direction"] == "BUY"
        assert _pv("SHORT", GPC_SHORT)["direction"] == "SELL_SHORT"

    def test_labels_are_enumerable_and_versioned(self):
        for direction, entry in (("LONG", XOM_LONG), ("SHORT", GPC_SHORT)):
            pv = _pv(direction, entry)
            assert pv["state"] == "generated"
            assert pv["value_origin"] == "code_track_a"
            # [P3.3-3.5 2026-09-13] v1→v2: blob 增加了 size_estimate/cost_coverage/
            #   executability 三段 (本文件只钉 P3.1 那三段, 新增段由
            #   tests/test_ue_planned_executability.py 钉)。升版是为了让读账的人能按
            #   版本切分: v1 行"当时刻意没算", v2 行才有"算了但缺数"的显式状态。
            assert pv["version"] == ue.PLANNED_PREVIEW_VERSION == "ue_planned_v3"

    def test_value_origin_distinguishes_code_from_model(self):
        """⚠ 与 DA 侧 planned_* 同名不同源, 必须能被分辨。

        DA 的 planned_* 是模型声明的数值, UE 的是代码出的数值 (Track A)。跨源比
        "planned vs 重定价" 测的不是同一件事; 没有这个键, 两者在报表里长得一样。
        """
        assert _pv("LONG", XOM_LONG)["value_origin"] == "code_track_a"

    def test_generated_at_is_tz_aware_iso(self):
        from datetime import datetime
        stamp = _pv("LONG", XOM_LONG)["generated_at"]
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None, "naive 时间戳会在跨时区复算时移半天"


class TestUEv214RiskLayering:
    def _cfg(self, key, default=None):
        return {
            "universe_expansion.default_stop_pct": 3.0,
            "universe_expansion.default_tp_pct": 6.0,
            "universe_expansion.risk_layering_enabled": True,
            "universe_expansion.conservative_stop_pct": 2.0,
            "universe_expansion.conservative_tp_pct": 4.0,
            "universe_expansion.atr_stop_multiplier": 1.25,
            "universe_expansion.atr_tp_multiplier": 2.0,
        }.get(key, default)

    @pytest.mark.parametrize("candidate", [
        {"event_phase": "PRICE_IN", "move_basis": "VOLATILITY_ALIGNED",
         "self_tier": "MARKET_CORROBORATED",
         "evidence_citation_state": "entity_linked"},
        {"event_phase": "CONTINUING", "move_basis": "UNQUALIFIED",
         "self_tier": "MARKET_CORROBORATED",
         "evidence_citation_state": "entity_linked"},
    ])
    def test_weak_evidence_uses_conservative_sl_tp(self, monkeypatch, candidate):
        monkeypatch.setattr(ue, "_cfg", self._cfg)
        preview = ue._compute_numbers(
            XOM_LONG, "LONG", candidate=candidate, screening={})["planned_preview"]
        assert preview["risk_profile"] == {
            "profile": "conservative", "stop_pct": 2.0, "tp_pct": 4.0,
            "atr_pct": None, "reasons": ["weak_evidence_or_price_in"],
        }
        assert preview["sl_pct_cfg"] == 2.0
        assert preview["tp_pct_cfg"] == 4.0

    def test_atr_caps_risk_without_enlarging_it_and_records_move_calibration(
            self, monkeypatch):
        candidate = {"event_phase": "CONTINUING",
                     "move_basis": "VOLATILITY_ALIGNED",
                     "self_tier": "MARKET_CORROBORATED",
                     "evidence_citation_state": "entity_linked",
                     "expected_move_pct": 3.0}
        screening = {"screening_atr_ratio": 0.01}
        monkeypatch.setattr(ue, "_cfg", self._cfg)
        preview = ue._compute_numbers(
            XOM_LONG, "LONG", candidate=candidate,
            screening=screening)["planned_preview"]
        assert preview["risk_profile"]["stop_pct"] == 1.25
        assert preview["risk_profile"]["tp_pct"] == 2.0
        assert "atr_stop_cap" in preview["risk_profile"]["reasons"]
        assert "atr_tp_cap" in preview["risk_profile"]["reasons"]
        assert preview["move_calibration"] == {
            "state": "exceeds_2atr", "expected_move_pct": 3.0,
            "atr_pct": 1.0, "expected_move_atr_multiple": 3.0,
        }


# ══ B. 零漂移: 既有契约逐字不变 ══════════════════════════════════════════

class TestZeroDriftOnExistingContract:
    def test_return_surface_only_gained_one_key(self):
        nums = ue._compute_numbers(XOM_LONG, "LONG")
        assert set(nums) == {"entry_zone", "stop_loss", "take_profit", "planned_preview"}

    def test_legacy_three_values_are_closed_form(self):
        """用闭式公式独立复算既有三键, 证明加预览没有移动它们。"""
        nums = ue._compute_numbers(XOM_LONG, "LONG")
        low, high = XOM_LONG * (1 - KNIFE), XOM_LONG * (1 + CHASE)
        assert nums["entry_zone"] == [round(low, 4), round(high, 4)]
        assert nums["stop_loss"] == round(low * (1 - 0.03), 4)
        assert nums["take_profit"] == round(high * (1 + 0.06), 4)

    def test_legacy_three_values_are_closed_form_short(self):
        nums = ue._compute_numbers(GPC_SHORT, "SHORT")
        low, high = GPC_SHORT * (1 - CHASE), GPC_SHORT * (1 + KNIFE)
        assert nums["entry_zone"] == [round(low, 4), round(high, 4)]
        assert nums["stop_loss"] == round(high * (1 + 0.03), 4)
        assert nums["take_profit"] == round(low * (1 - 0.06), 4)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_invalid_price_path_unchanged(self, bad):
        """无效价必须仍返回空 zone + 0.0/0.0 ⇒ 调用方的 `continue` 守卫行为不变。

        这条是零行为变更的关键: 加了预览之后, 无效价路径不能悄悄变成"有 zone"
        或抛异常, 否则 inject 会为无效价生成指令。
        """
        nums = ue._compute_numbers(bad, "LONG")
        assert nums["entry_zone"] == []
        assert nums["stop_loss"] == 0.0
        assert nums["take_profit"] == 0.0
        # 但预览仍要表态, 不留"键不存在"的歧义 (§17.3-② 同型教训)
        assert nums["planned_preview"]["state"] == "not_generated_invalid_band"
        assert nums["planned_preview"]["version"] == ue.PLANNED_PREVIEW_VERSION

    def test_numeric_string_still_accepted(self):
        """数字字符串口径不变 (修前修后都放行), 别把守卫做成类型门。"""
        nums = ue._compute_numbers("12.5", "LONG")
        assert nums["planned_preview"]["state"] == "generated"
        assert nums["stop_loss"] > 0

    def test_no_io_in_compute_numbers(self):
        """预览是纯派生: 不得引入 DB/网络/文件访问。"""
        src = inspect.getsource(ue._compute_numbers)
        for banned in ("sqlite3", "urlopen", "requests.", "open(", "get_db("):
            assert banned not in src, f"_compute_numbers 里出现了 {banned}"


# ══ C. 没换尺子: 模型输入零改动 ═══════════════════════════════════════════

class TestRulerUnchanged:
    def test_prompt_version_and_hash_untouched_by_p3(self):
        """落库审计与输入尺子分离; v2.14 是本批有意升级。"""
        assert ue.PROMPT_VERSION == "v2.14"
        assert ue.PROMPT_HASH_V2 == hashlib.sha1(
            ue.SYSTEM_PROMPT_V2.encode("utf-8")).hexdigest()[:12]
        assert ue.BRIEF_TEMPLATE_VERSION == "ue_brief_v5"

    def test_planned_preview_version_is_its_own_ruler(self):
        """输入版本与落库审计版本分尺: 改 planned 口径不该逼着升 prompt 版。"""
        assert ue.PLANNED_PREVIEW_VERSION != ue.PROMPT_VERSION
        assert ue.PLANNED_PREVIEW_VERSION != ue.BRIEF_TEMPLATE_VERSION


# ══ D. 写库现场接线 (源级契约, 体例同 test_ue_funnel_batch.py) ═══════════

class TestInjectSiteWiring:
    @pytest.fixture(scope="class")
    def src(self):
        return inspect.getsource(ue._cmd_inject_impl)

    def test_three_flat_keys_reuse_da_names(self, src):
        """三个平级键必须与 DA 侧**同名同层**。

        这是跨源查询的前提: P4 全漏斗要用同一条 json_extract('$.planned_entry')
        同时取 DA 与 UE。改名或嵌套进 planned_preview 都会让跨源比较断掉。
        """
        for key in ('"planned_entry"', '"planned_stop_loss"', '"planned_take_profit"'):
            assert key in src, f"缺少与 DA 同名的平级键 {key}"

    def test_flat_keys_carry_generation_time_values(self, src):
        """平级三键必须取生成时刻的值, 不能取重定价后的值。

        entry_price 顶层键此刻就是 px (生成值), 但这条断言钉的是**来源表达式**:
        planned_entry 取 px、planned_stop_loss/take_profit 取 nums[...] —
        即与 _compute_numbers 同源, 而不是从别处回读。
        """
        assert re.search(r'"planned_entry":\s*round\(float\(px\),\s*4\)', src)
        assert re.search(r'"planned_stop_loss":\s*nums\["stop_loss"\]', src)
        assert re.search(r'"planned_take_profit":\s*nums\["take_profit"\]', src)

    def test_preview_blob_is_written(self, src):
        """blob 必须落库, 且 P3.1 的三段不被 P3.3-3.5 的附加段顶掉。

        [P3.3-3.5 2026-09-13] 写库现场从 `nums["planned_preview"]` 改成 `_preview_full`
        (= `_planned_exec_block(...)` 的返回, 内部是 `{**preview, **extra}`) —— 合并方向
        是 preview 在前、附加段在后, 且附加段只写 exec_*/size_estimate/cost_coverage/
        executability 四个新键, 与 P3.1 的键集不相交 (由
        tests/test_ue_planned_executability.py::TestNoClobberOfP31Keys 钉住)。
        """
        assert re.search(r'"planned_preview":\s*_preview_full', src)
        assert re.search(r'_preview_full\s*=\s*_planned_exec_block\(\s*nums\["planned_preview"\]', src)

    def test_skip_row_carries_explicit_state(self, src):
        """SKIP 行从不调用 _compute_numbers ⇒ 必须显式表态, 不能靠"键不存在"。

        否则日后无法区分「UE 行没有 planned_* 因为它是 SKIP 行」(正常) 与
        「没有 planned_* 因为代码漏写」(缺陷) —— 与 §17.3-② 的
        「索引空 vs 没跑 gather」同型歧义。
        """
        assert '"not_generated_ineligible"' in src

    def test_no_position_size_hint_fabricated(self, src):
        """"不许造半截数字"的钉子 —— P3.3-3.5 之后**保留**, 但禁的东西更精确了。

        P3.1 立这条时, 费用覆盖那一批还没做, 任何股数提示都是半截数字。P3.3-3.5 已经把
        估算股数做全了 (带 size_basis / 权益 provenance / 费用逐项与未知项), 落在
        `planned_preview.size_estimate`, 由 tests/test_ue_planned_executability.py 钉。
        这条继续禁的是那个**无口径来源的键名** `position_size_hint`: 它既不是执行侧
        的真列名 (position_size), 也不带任何 basis, 谁再塞一个进来就是绕过 P3.3-3.5 的口径
        另造一个数。真正的股数预估必须走 size_estimate + size_basis。
        """
        assert '"position_size_hint"' not in src

    def test_preview_keys_do_not_leak_into_top_level_instruction(self, src):
        """planned_* 只进 raw_instruction, 不得成为指令顶层键。

        顶层键会被 save_instruction 映射到 DB 列并进执行判据; 预览是审计,
        进顶层就等于让"预估"参与定价。
        """
        head = src.split('"raw_instruction"')[0]
        for key in ('"planned_entry"', '"planned_stop_loss"', '"planned_take_profit"'):
            assert key not in head, f"{key} 出现在 raw_instruction 之前 (顶层)"

    def test_origin_carries_fields_needed_by_da_traceability(self, src):
        origin = src.split('"origin"', 1)[1].split('"raw_confidence"', 1)[0]
        for key in ('"evidence_citation_state"', '"confidence_basis"',
                    '"confidence_calibration"', '"counter_evidence"',
                    '"event_phase"', '"move_basis"'):
            assert key in origin


# ══ E. 非有限入场价拒绝 (本批测试自己抓到的既有缺陷, 计划 P3「非有限数…拒绝」) ══

class TestNonFinitePriceRejection:
    """修前实测: `_compute_numbers(nan)` 返回 `entry_zone=[nan, nan]` —— **非空**列表
    ⇒ 调用方 `if not nums["entry_zone"]: continue` 守卫放行 ⇒ 一条 stop_loss=NaN 的
    指令会被写库; 且 P3.1 新增的 planned_preview 是 JSON blob, `json.dumps(nan)` 吐出
    `NaN` (Python 能吃、严格 JSON 解析器不能) ⇒ 会把无效 JSON 写进 raw_instruction_json。

    根因在共享基建: `compute_entry_band` 只挡 `e <= 0`, 而 `NaN <= 0` 为 False、`inf > 0`
    ⇒ 两者都漏。本批只在 UE 边界拦 (共享基建留给自己的批次, 见 DEB §19 待裁)。
    """

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, "abc"])
    def test_non_finite_yields_empty_zone_and_zero_sl_tp(self, bad):
        nums = ue._compute_numbers(bad, "LONG")
        assert nums["entry_zone"] == [], "非有限价不得产出可用入场带"
        assert nums["stop_loss"] == 0.0
        assert nums["take_profit"] == 0.0

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_state_says_non_finite_not_invalid_band(self, bad):
        """状态必须能区分"非有限"与"带算不出来", 否则归因会并成一个桶。"""
        pv = ue._compute_numbers(bad, "SHORT")["planned_preview"]
        assert pv["state"] == "not_generated_non_finite_price"
        assert pv["version"] == ue.PLANNED_PREVIEW_VERSION

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0])
    def test_preview_blob_is_strict_json_serializable(self, bad):
        """allow_nan=False 是严格解析器的口径; 能过它就说明没把 NaN/Infinity 写进 blob。"""
        import json
        pv = ue._compute_numbers(bad, "LONG")["planned_preview"]
        json.dumps(pv, allow_nan=False)

    def test_generated_preview_is_strict_json_serializable(self):
        import json
        nums = ue._compute_numbers(XOM_LONG, "LONG")
        blob = json.dumps(nums["planned_preview"], allow_nan=False)
        assert "NaN" not in blob and "Infinity" not in blob

    def test_finite_guard_does_not_reach_into_shared_entry_band(self):
        """本批刻意不代改共享基建 —— 这条钉住"洞还在别处", 修好那天它会变红。

        `compute_entry_band` 是执行路径共用的 (pipeline re-price / entry_routing),
        改它的语义要有自己的批次与验证窗 (同 §17.3-⑥ 对 admission 的处置纪律)。
        这不是认可现状, 是把现状钉成可见的债。
        """
        import math
        from app.playbook.entry_band import compute_entry_band
        low, high = compute_entry_band(float("nan"), False)
        assert not math.isfinite(low) and not math.isfinite(high), (
            "compute_entry_band 已开始拒绝非有限价 ⇒ 本债标记应改写为正确断言, "
            "并把 UE 侧守卫降级为冗余防线 (DEB §19 待裁项)")
