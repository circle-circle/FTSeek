"""[P1.4 2026-09-12] DA 候选选择契约 (DEB-20260910 / proud-slope-falcon).

被测对象在**仓外** skill: `/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py`
(+ 同目录 `market_scanner.py` 的完整分桶队列)。cron 每次加载源码, 不随本仓提交,
因此本文件是它唯一的回归闸。

⚠ 与 test_da_output_contract / test_da_trading_day_guard 的 `skip` 惯例**刻意不同**:
  计划验证清单 §2 明确"仓外脚本找不到导致 skip 不算通过"。skill 缺失/被搬走时本文件
  直接 `pytest.fail` —— 静默 skip 会让"先过滤后预算"这条链在 CI 里无声消失,
  而它正是本批要修的生产缺陷本身。

执行方式: 子进程跑 `da_selection_driver.py` (伪造 data_collector/market_scanner),
  理由见该驱动器 docstring (避免 skill `config.py` 顶掉本仓 `config` 包 + 避免 futu
  非 daemon 线程挂死解释器)。全部断言读驱动器产出的 JSON。

钉住的不变量:
  1. 池源 = scanner **完整分桶队列** (bullish_all/bearish_all), 不是展示用 top_*;
  2. 最强空头 (score 最低 → conviction=10-score 最高) 不会在进池前被丢掉;
  3. 资格判定前置, 预算 = top_n+2 不变, 淘汰从剩余合格队列**补位**;
  4. `selected_symbols` = **过滤后**实际提交深分析的名单 (修前记过滤前);
  5. extras / UE(≤3) / 盘前异动(≤2) 各自先筛后消费, 跨来源重复项不占两个名额;
  6. 低 ATR 阈值显式 2.9% → 3.0% (统一到执行端配置), 且分母优先候选快照价;
  7. 缺日线 → UNKNOWN 且 atr=None, 绝不编造 0 让门恒放;
  8. DailyActivePool 只消费最终合格名单 + 合格 analyses, **不从 scanner Top 重拼**;
  9. checkpoint 按候选策略口径校验 (阈值/分桶排序/先过滤后预算 变了即作废);
 10. UE feed 只认最新有效 `da_feed` 批次 (窗口隔离/空批/失败批/过期/未来/naive 全拒);
 11. deps 不可导入 → fail-open (只标注 UNKNOWN 不拒绝), 不静默改判定口径。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DRIVER = Path(__file__).with_name("da_selection_driver.py")
SKILL = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
SKILL_PIPELINE = SKILL / "pipeline.py"
SKILL_SCANNER = SKILL / "market_scanner.py"

CASES = ("thresholds", "atr", "main_pool", "backfill", "dedup_budgets",
         "empty_and_legacy", "checkpoint", "ue_feed", "compat_and_failopen",
         "pool_write")


@pytest.fixture(scope="module")
def driver_result(tmp_path_factory):
    """跑一次子进程驱动器, 全模块共享结果 (10 个用例一次进程, ~3s)。"""
    for path, what in ((SKILL_PIPELINE, "pipeline.py"), (SKILL_SCANNER, "market_scanner.py"),
                       (DRIVER, "da_selection_driver.py")):
        if not path.exists():
            # 计划验证清单 §2: "仓外脚本找不到导致 skip 不算通过"
            pytest.fail(f"仓外 DA {what} 缺失: {path} — 无法验证先过滤后预算链路 "
                        f"(按本批约定不计 skip); 若 skill 已迁移, 同步更新本测试路径")
    workdir = tmp_path_factory.mktemp("da_driver")
    out = workdir / "result.json"
    proc = subprocess.run(
        [sys.executable, str(DRIVER), "--workdir", str(workdir), "--out", str(out)],
        capture_output=True, text=True, timeout=600, cwd=str(REPO))
    if not out.exists():
        pytest.fail(f"驱动器未产出结果 (rc={proc.returncode})\n"
                    f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-3000:]}")
    with out.open(encoding="utf-8") as fh:
        result = json.load(fh)
    result["_stderr"] = proc.stderr
    result["_workdir"] = str(workdir)
    return result


def case(result, name):
    assert name in result, f"驱动器未跑用例 {name}: {sorted(result)}"
    payload = result[name]
    assert "error" not in payload, f"用例 {name} 抛异常:\n{payload.get('traceback')}"
    return payload


# ══ 0. 驱动器自身健康 ════════════════════════════════════════════════════
class TestDriverHealth:
    def test_all_cases_ran_without_error(self, driver_result):
        assert driver_result.get("_failed_cases") == {}
        for name in CASES:
            case(driver_result, name)

    def test_skill_pipeline_is_the_one_under_test(self, driver_result):
        assert driver_result["skill_pipeline"] == str(SKILL_PIPELINE)

    def test_no_real_market_data_touched(self, driver_result):
        """驱动器只用伪造 K 线: 未登记 symbol 取数会抛, 因此无 traceback 即无真取数。"""
        assert "unexpected kline fetch" not in driver_result["_stderr"]
        assert "Traceback" not in driver_result["_stderr"]


# ══ 1. 阈值单一真源 + 2.9→3.0 显式变更 ══════════════════════════════════
class TestThresholds:
    def test_reads_repo_config_not_local_constant(self, driver_result):
        thr = case(driver_result, "thresholds")
        assert thr["deps_available"] is True
        assert thr["thresholds"]["config_source"] == "playbook+admission"

    def test_values_match_repo_policy(self, driver_result):
        """DA 侧阈值必须与 FTSeek `load_candidate_policy()` 逐项一致 (反分叉钉)。"""
        thr = case(driver_result, "thresholds")
        shared = ("version", "min_price", "min_atr_ratio", "low_atr_enabled",
                  "max_scan_age_hours")
        for key in shared:
            assert thr["thresholds"][key] == thr["repo_policy"][key], key

    def test_low_atr_threshold_moved_from_2_9_to_3_0(self, driver_result):
        """⚠ 显式策略行为变更: 取消 0.1pp 缓冲, 统一到执行端 low_atr_gate_min_atr_pct。

        2.9% ≤ atr < 3.0% 区间的标的由"DA 放行 → 执行端拦 (白算 LLM)"变为
        "DA 直接拒"。谁把 DA 侧改回 2.9 (或另立本地常量) → 本例立红。
        """
        thr = case(driver_result, "thresholds")["thresholds"]
        assert thr["min_atr_ratio"] == pytest.approx(0.03)
        assert thr["min_atr_ratio"] != pytest.approx(0.029)
        assert case(driver_result, "main_pool")["filter"]["screen_thresholds"][
            "min_atr_pct"] == pytest.approx(3.0)

    def test_ratio_is_fraction_not_percent(self, driver_result):
        """量纲钉: 0.03 = 3%。写成 3.0 会让门恒拒 (全部候选被判低波)。"""
        assert 0 < case(driver_result, "thresholds")["thresholds"]["min_atr_ratio"] <= 1.0

    def test_price_floor_from_admission(self, driver_result):
        assert case(driver_result, "thresholds")["thresholds"]["min_price"] == pytest.approx(5.0)

    def test_quote_and_borrow_gates_off_at_da(self, driver_result):
        """DA 只有扫描快照价, 无实时盘口/借券数据 ⇒ 两门恒不启用 (缺数不当硬拒);
        仓内 policy 是开的 —— 差异必须是**有意的**, 不是漏抄。"""
        thr = case(driver_result, "thresholds")
        assert thr["thresholds"]["quote_gate_enabled"] is False
        assert thr["thresholds"]["borrow_gate_enabled"] is False
        assert thr["repo_policy"]["quote_gate_enabled"] is True

    def test_deps_cover_shared_helpers(self, driver_result):
        """DA 不自带第二套判定数学: 关键纯函数必须来自仓内模块。"""
        keys = set(case(driver_result, "thresholds")["deps_keys"])
        assert {"compute_screening_atr", "evaluate_candidate", "select_candidates",
                "load_candidate_policy", "canonical_symbol", "record_candidate_batch",
                "make_batch_id", "latest_valid_batch"} <= keys


# ══ 2. 筛选 ATR 口径 ═════════════════════════════════════════════════════
class TestScreeningAtr:
    def test_uses_completed_daily_bars_only(self, driver_result):
        atr = case(driver_result, "atr")["high_vol"]
        assert atr["status"] == "PASS"
        assert atr["basis"] == "daily_tr14_rolling_mean"
        assert atr["bars_used"] == 15                     # 15 根 → 14 个 TR
        # 只用严格早于 asof 的已完成日线: 末根日期必须早于当日 (当日未完成 bar 排除)
        assert case(driver_result, "atr")["bars_asof_before_today"] is True

    def test_source_declared_as_da_kline(self, driver_result):
        """取数走 DA 自有 collect_kline (不新建第二条行情连接), 且来源写进审计。"""
        assert case(driver_result, "atr")["high_vol"]["source"] == \
            "da.data_collector.collect_kline"

    def test_denominator_prefers_candidate_snapshot_price(self, driver_result):
        atr = case(driver_result, "atr")
        assert atr["high_vol"]["denominator"] == "candidate_snapshot_price"
        assert atr["high_vol_no_ref_price"]["denominator"] == "last_completed_daily_close"
        # 分母不同 ⇒ 比例不同 (100 vs 末根收盘), 证明分母口径真的按调用方走
        assert atr["high_vol"]["ratio_pct"] != pytest.approx(
            atr["high_vol_no_ref_price"]["ratio_pct"])

    def test_screening_ratio_is_around_4_percent_for_2pct_bars(self, driver_result):
        """±2% 日振幅 ⇒ TR≈4% ⇒ 高于 3% 门 (夹具自洽性检查, 防"门恒放"假绿)。"""
        assert case(driver_result, "atr")["high_vol"]["ratio_pct"] == pytest.approx(4.05, abs=0.2)
        assert case(driver_result, "atr")["low_vol"]["ratio_pct"] < 3.0

    def test_missing_bars_is_unknown_not_zero(self, driver_result):
        nod = case(driver_result, "atr")["no_data"]
        assert nod["status"] == "UNKNOWN"
        assert nod["atr"] is None and nod["ratio"] is None
        assert nod["reason"] == "no_completed_daily_bars"

    def test_compat_fetch_atr_pct_keeps_none_semantics(self, driver_result):
        atr = case(driver_result, "atr")
        assert atr["compat_fetch_atr_pct"] == pytest.approx(atr["high_vol"]["ratio_pct"])
        assert atr["compat_fetch_atr_pct_no_data"] is None

    def test_intra_run_cache_reused_across_stages(self, driver_result):
        """同一 (symbol, 观测日) 只取一次数 ⇒ 全量送筛异动/UE 不放大取数量。"""
        atr = case(driver_result, "atr")
        assert atr["cache_reused_with_other_denominator"] == "candidate_snapshot_price"
        assert atr["cache_size_after_run"] == 3            # HIVOL / LOWVOL / NODATA


# ══ 3. 主池: 完整队列 + 最强空头 + 预算 ═════════════════════════════════
class TestMainPool:
    def test_pool_built_from_full_buckets_not_display_topn(self, driver_result):
        """top_bullish/top_bearish 各只给 2 (旧截断口径), 池必须是 5+5=10。"""
        main = case(driver_result, "main_pool")
        assert main["filter"]["pool_size"] == 10
        assert main["filter"]["input"] == 10

    def test_strongest_bearish_not_dropped(self, driver_result):
        """SAA (score=1.0 ⇒ conviction=9.0) 必须入池且排第二 (仅次于 BAA score=9.0)。"""
        selected = case(driver_result, "main_pool")["selected"]
        assert "US.SAA" in selected
        assert selected[1] == "US.SAA"
        # 完整队列下 SAA/SAB/SAC/SAD 都在; 若退回 top_bearish[:2] 则只有 SAA/SAB
        assert {"US.SAA", "US.SAB", "US.SAC", "US.SAD"} <= set(selected)

    def test_budget_is_top_n_plus_two(self, driver_result):
        main = case(driver_result, "main_pool")
        assert len(main["selected"]) == 7                   # top_n=5 → 5+2
        assert main["filter"]["output"] == 7
        assert main["screening"]["selection"] == {"selected": 7, "budget_cut": 3}

    def test_selected_symbols_recorded_after_filtering(self, driver_result):
        """审计口径: filter.selected_symbols 必须与实际提交深分析的列表逐字相同。"""
        main = case(driver_result, "main_pool")
        assert main["filter"]["selected_symbols"] == main["selected"] == main["filter_selected"]

    def test_conviction_order_interleaves_long_and_short(self, driver_result):
        """多空比例由方向强度决定 (2026-07-18 口径未被本批改变)。"""
        assert case(driver_result, "main_pool")["selected"] == [
            "US.BAA", "US.SAA", "US.SAB", "US.BAB", "US.SAC", "US.BAC", "US.SAD"]

    def test_bucket_order_version_propagated(self, driver_result):
        assert case(driver_result, "main_pool")["filter"]["bucket_order_version"] == \
            driver_result["bucket_order_version"] == "bearish_conviction_v1"

    def test_da_snapshot_has_no_fake_source_asof(self, driver_result):
        """扫描快照不是实时行情: 不冒充 source_asof ⇒ 记 missing, 状态 UNKNOWN 而非 PASS。

        执行端 admission/quote gate 仍全量复核; 这里若伪造 source_asof, 就等于把盘前
        快照当实时行情用 (计划 §3 明令禁止)。
        """
        screening = case(driver_result, "main_pool")["screening"]
        assert screening["missing_fields"] == {"source_asof": 10}
        assert screening["eligibility"] == {"UNKNOWN": 10}


# ══ 4. 淘汰补位 ══════════════════════════════════════════════════════════
class TestBackfill:
    def test_rejected_does_not_consume_budget(self, driver_result):
        bf = case(driver_result, "backfill")
        assert bf["pool_size"] == 10
        assert bf["eligibility"] == {"REJECT": 2, "UNKNOWN": 8}
        assert len(bf["selected"]) == 7                     # 8 合格 > 预算 7 ⇒ 填满

    def test_highest_conviction_low_atr_rejected(self, driver_result):
        """HAA 信念最高但 ATR≈0.8% < 3% ⇒ 拒 (修前它照样烧一次 LLM 再被执行端拦)。"""
        assert "US.HAA" not in case(driver_result, "backfill")["selected"]

    def test_price_floor_rejected(self, driver_result):
        assert "US.HAD" not in case(driver_result, "backfill")["selected"]

    def test_backfill_reaches_beyond_old_truncation(self, driver_result):
        """修前 `[:top_n+2]` 先截断 ⇒ HAH/HAI 永远进不了池; 修后补位取到。"""
        selected = case(driver_result, "backfill")["selected"]
        assert {"US.HAH", "US.HAI"} <= set(selected)

    def test_reason_codes_separated_not_one_skip(self, driver_result):
        assert case(driver_result, "backfill")["reason_codes"] == {
            "low_atr": 1, "price_floor": 1}


# ══ 5. 跨来源预算与去重 ══════════════════════════════════════════════════
class TestCrossSourceBudgets:
    def test_no_duplicate_symbol_across_sources(self, driver_result):
        dd = case(driver_result, "dedup_budgets")
        assert dd["duplicates"] is False
        assert len(dd["selected"]) == len(set(dd["selected"]))

    def test_duplicates_do_not_take_two_slots(self, driver_result):
        """MAB 在 extras、MAA 在 UE、MAC 在异动里重复出现 ⇒ 各记 duplicate 不占名额。"""
        by_stage = case(driver_result, "dedup_budgets")["by_stage"]
        assert by_stage["da_extra_watchlist"] == {"selected": 1}     # MAB 重复, 只 XAA 入
        assert by_stage["da_ue_feed"]["duplicate"] == 1
        assert by_stage["da_premarket_mover"]["duplicate"] == 1
        # extras/UE/异动 各 1 次重复 (MAB/MAA/MAC) — 其中 MAB 记在 extras 阶段
        assert case(driver_result, "dedup_budgets")["selection"]["duplicate"] == 2

    def test_extras_uncapped_but_screened(self, driver_result):
        """extras 原语义 = 全量追加只去重 ⇒ 预算 = 去重后条数, 但仍要过资格判定。"""
        dd = case(driver_result, "dedup_budgets")
        assert "US.XAA" in dd["selected"]
        assert dd["sources"].count("watchlist") == 1

    def test_ue_feed_cap_three_with_backfill(self, driver_result):
        """UAA 低 ATR 被拒 ⇒ 不占 UE 名额, 由 UAB/UAC/UAD 补满 cap=3 (UEE 落 budget_cut)。"""
        dd = case(driver_result, "dedup_budgets")
        assert dd["ue_injected"] == 3
        assert "US.UAA" not in dd["selected"]
        assert {"US.UAB", "US.UAC", "US.UAD"} <= set(dd["selected"])
        assert "US.UEE" not in dd["selected"]
        assert dd["by_stage"]["da_ue_feed"]["budget_cut"] == 1

    def test_premarket_mover_cap_two_with_backfill(self, driver_result):
        dd = case(driver_result, "dedup_budgets")
        assert dd["mover_injected"] == 2
        assert "US.PAA" not in dd["selected"]              # 低 ATR 拒 → PAB/PAC 补位
        assert {"US.PAB", "US.PAC"} <= set(dd["selected"])

    def test_source_tags_preserved(self, driver_result):
        """`_source` 标签不变 (下游 llm_synth 的 UE 因果链注入依赖它)。"""
        assert case(driver_result, "dedup_budgets")["sources"] == [
            None, None, None, "watchlist",
            "ue_candidate", "ue_candidate", "ue_candidate",
            "premarket_mover", "premarket_mover"]


# ══ 6. 候选漏斗 JSONL ════════════════════════════════════════════════════
class TestFunnel:
    def test_written_under_output_dir(self, driver_result):
        """随 output_dir 走 ⇒ A/B 隔离目录不污染生产影子档。"""
        assert case(driver_result, "main_pool")["funnel_exists"] is True
        path = Path(driver_result["_workdir"]) / "shadow" / "candidate_funnel.jsonl"
        assert path.exists()

    def test_batch_schema_and_audit_fields(self, driver_result):
        rows = case(driver_result, "main_pool")["funnel_head"]
        assert rows and rows[0]["type"] == "candidate_batch"
        assert rows[0]["source"] == "DA" and rows[0]["phase"] == "da_selection"
        assert rows[0]["batch_id"] == "DA:driver-main"
        assert rows[0]["policy_version"] == "da_ue_v1"
        cand = rows[0]["candidates"][0]
        # 最小审计字段 (计划 P1.1) + 量纲/口径声明 (AGENT.md 军规)
        for key in ("batch_id", "candidate_id", "source", "scan_window", "symbol",
                    "direction_hint", "policy_version", "input_hash", "observed_at",
                    "price", "quote_source", "source_asof", "fetched_at",
                    "screening_atr_ratio", "atr_basis", "bars_asof", "status",
                    "reasons", "missing_fields", "rank", "selection_status"):
            assert key in cand, key
        assert cand["atr_basis"] == "daily_tr14_rolling_mean"
        assert cand["atr_adjustment"] == "split_guarded_source_undeclared"
        assert cand["atr_source"] == "da.data_collector.collect_kline"
        assert cand["min_atr_pct"] == pytest.approx(3.0)
        assert cand["scan_window"] == "da_premarket"
        assert cand["trade_mode"] == "sim"

    def test_production_shadow_file_untouched(self, driver_result):
        """驱动器只写 tmp workdir; 生产 data/shadow 不得出现驱动器批次号。"""
        prod = REPO / "data" / "shadow" / "candidate_funnel.jsonl"
        if not prod.exists():
            return
        with prod.open(encoding="utf-8") as fh:
            ids = {json.loads(l).get("batch_id") for l in fh if l.strip()}
        assert not ids & {f"DA:driver-{n}" for n in
                          ("main", "backfill", "dedup", "empty", "legacy", "off", "pool")}


# ══ 7. 全空 / 旧 scanner 回退 ════════════════════════════════════════════
class TestEmptyAndLegacy:
    def test_empty_scan_selects_zero(self, driver_result):
        """分析名额不是成交配额 ⇒ 允许零推荐, 不硬凑。"""
        empty = case(driver_result, "empty_and_legacy")
        assert empty["empty_selected"] == []
        assert empty["empty_screening_total"] == 0

    def test_legacy_scanner_without_full_queues_still_works(self, driver_result):
        """旧 market_scanner (无 bullish_all/bearish_all) 回退 top_* ⇒ 行为不劣化。"""
        assert case(driver_result, "empty_and_legacy")["legacy_selected"] == [
            "US.OAA", "US.OAB", "US.OAC"]


# ══ 8. checkpoint 策略版本 ═══════════════════════════════════════════════
class TestCheckpointPolicy:
    def test_version_is_stable_16_hex(self, driver_result):
        ck = case(driver_result, "checkpoint")
        assert ck["stable"] is True
        assert len(ck["version"]) == 16 and int(ck["version"], 16) >= 0

    @pytest.mark.parametrize("field", ["min_atr_ratio", "min_price",
                                       "low_atr_enabled", "version"])
    def test_threshold_change_invalidates(self, driver_result, field):
        assert case(driver_result, "checkpoint")["variants_differ"][field] is True

    def test_screen_off_switch_invalidates(self, driver_result):
        """DA_CANDIDATE_SCREEN=0 (回滚开关) 也是口径变更 ⇒ 不得复用开启期的缓存。"""
        assert case(driver_result, "checkpoint")["screen_off_differs"] is True

    def test_bucket_order_version_participates(self, driver_result):
        assert case(driver_result, "checkpoint")["bucket_order_differs"] is True

    def test_load_semantics(self, driver_result):
        load = case(driver_result, "checkpoint")["load"]
        assert load["no_expect"] is True          # expect_policy=None 保持旧行为
        assert load["match"] is True
        assert load["mismatch"] is True           # 口径不符 → 作废
        assert load["model_mismatch"] is True     # 模型校验未被削弱
        assert load["caller_dict_unpolluted"] is True   # _ckpt_policy 不进生产载荷


# ══ 9. UE feed 批次纪律 ══════════════════════════════════════════════════
class TestUeFeedBatch:
    def test_window_isolation(self, driver_result):
        """premarket / midday 更新也不顶替 da_feed (修前 scans[-1] 会误消费异窗)。"""
        ue = case(driver_result, "ue_feed")
        assert ue["premarket_does_not_override"] == ["US.AAA"]
        assert ue["midday_does_not_override"] == ["US.AAA"]
        assert ue["only_premarket"] == []

    def test_latest_da_feed_batch_wins(self, driver_result):
        assert case(driver_result, "ue_feed")["latest_da_feed_wins"] == ["US.CCC"]

    def test_empty_batch_is_not_silently_reused(self, driver_result):
        """空批次 = 当日无机会的正常结束, 但绝不回用更早的候选。"""
        assert case(driver_result, "ue_feed")["empty_batch_not_reused"] == []

    def test_failed_batch_invalidates_earlier_success(self, driver_result):
        assert case(driver_result, "ue_feed")["failed_batch_invalidates"] == []

    def test_stale_future_and_naive_timestamps_rejected(self, driver_result):
        ue = case(driver_result, "ue_feed")
        assert ue["stale_rejected"] == []          # 400min > 3h
        assert ue["future_rejected"] == []         # 未来时间戳
        assert ue["naive_timestamp_rejected"] == []   # 无时区 → 拒, 不猜时区

    def test_no_records_is_not_an_error(self, driver_result):
        assert case(driver_result, "ue_feed")["no_records"] == []

    def test_only_short_and_source_filters_preserved(self, driver_result):
        """原有消费端过滤 (只取 SHORT / 不可借券 / streak 硬闸) 语义不变。"""
        ue = case(driver_result, "ue_feed")
        assert ue["only_short_kept"] == ["US.EEE"]
        assert ue["borrow_infeasible_filtered"] == []
        assert ue["streak_filtered"] == []

    def test_field_passthrough_and_symbol_normalization(self, driver_result):
        ue = case(driver_result, "ue_feed")
        assert ue["symbol_normalized"] == "US.HHH"
        assert {"ue_causal_chain", "ue_invalidation", "ue_horizon_hours",
                "ue_expected_move_pct", "ue_self_tier", "ue_short_check",
                "ue_streak_days", "ue_scanned_at"} <= set(ue["fields_passthrough"])


# ══ 10. 兼容包装 / fail-open ═════════════════════════════════════════════
class TestCompatAndFailOpen:
    def test_prefilter_wrapper_still_filters(self, driver_result):
        """`_prefilter_candidates()` 保留为兼容薄包装 (回滚入口), 判定与新链同源。"""
        assert case(driver_result, "compat_and_failopen")["prefilter_kept"] == ["US.PFA"]

    def test_env_switch_degrades_to_annotate_only(self, driver_result):
        """DA_CANDIDATE_SCREEN=0 → 只标注 UNKNOWN 不拒绝 (选择仍按预算工作)。"""
        co = case(driver_result, "compat_and_failopen")
        assert all(row["status"] == "UNKNOWN" for row in co["screen_off"])
        assert all(row["missing"] == ["screening_disabled_or_deps_unavailable"]
                   for row in co["screen_off"])
        assert all(row["policy_version"] is None for row in co["screen_off"])
        assert co["screen_off_selected"] == ["US.ZAA", "US.ZAB"]

    def test_missing_repo_deps_fails_open(self, driver_result):
        """仓内件导入失败 ⇒ 不阻断 DA 主流程, 也不静默改判定口径 (全部 UNKNOWN)。"""
        assert all(row["status"] == "UNKNOWN"
                   for row in case(driver_result, "compat_and_failopen")["no_deps"])


# ══ 11. DailyActivePool 一致性 ═══════════════════════════════════════════
class TestPoolConsistency:
    def test_pool_only_final_eligible_plus_analyses(self, driver_result):
        """RJC/RJD 是被资格判定拒绝的 scanner Top ⇒ 不得重新出现在活跃池。"""
        pw = case(driver_result, "pool_write")
        assert pw["selected"] == ["US.EAA", "US.EAB"]
        assert pw["pool_codes"] == ["US.EAA", "US.EAB", "US.NEW"]
        assert "US.RJC" not in pw["pool_codes"] and "US.RJD" not in pw["pool_codes"]

    def test_skip_analysis_not_admitted(self, driver_result):
        assert "US.SKIP" not in case(driver_result, "pool_write")["pool_codes"]

    def test_missing_selected_symbols_never_falls_back_to_scanner_top(self, driver_result):
        """兼容旧调用点: 无最终名单时只按 analyses, 绝不回退 scanner Top。"""
        codes = case(driver_result, "pool_write")["pool_none_codes"]
        assert codes == ["US.EAA", "US.NEW"]
        assert {"US.RJC", "US.RJD", "US.EAB"} & set(codes) == set()

    def test_metadata_lookup_does_not_re_admit(self, driver_result):
        """scan_result 降级为纯 metadata 来源 (score/strategy_type), 不做准入。"""
        meta = case(driver_result, "pool_write")["pool_metadata"]
        assert meta["US.EAA"] == {"strategy_type": "quality_breakout", "score": 8}
        assert meta["US.NEW"] == {"strategy_type": "trend_short", "score": 6}
        assert set(meta) == {"US.EAA", "US.EAB", "US.NEW"}

    def test_pool_summary_scan_added_matches(self, driver_result):
        pw = case(driver_result, "pool_write")
        assert pw["pool_summary"]["scan_added"] == len(pw["pool_codes"])
        assert pw["pool_summary"]["total"] == len(pw["pool_codes"])
