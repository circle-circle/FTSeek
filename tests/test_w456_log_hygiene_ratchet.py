"""W4/W5/W6 日志卫生棘轮钉子 (2026-09-10, DEB-20260910 §8.2 + §5, 用户裁「按照建议执行优化」)。

病灶 (立法理由): `debug→warning` 逐案修 4 轮仍在漏 —— 已修锚点 scheduler_core.py:2375/:2450
(2026-07-04)、playbook_service.py:1466/:1486 (DEB-20260721 R4)、pipeline.py:800 (2026-07-27)、
DEB-20260904 #148; 本批又抓 4 处, 其中 :2461 与已修的 :2450 **相隔 11 行** ⇒ 逐案修不收敛。
全仓 AST 实测: except 块内 logger.debug 704 处/164 文件 (LOG_LEVEL=INFO 下全不可见)、
except 内告警无 exc_info 1135 处/244 文件 (4230 个处理器只有 103 处带栈)、裸名 logger 15 处。

本文件钉住接入 `scripts/daily_invariant_check.py` 后的语义 (照 W2 棘轮同款纪律):
  ① 基线外新增 / 基线内增长 → 当天告警 (守卫的证伪力所在);
  ② 基线内存量 → silenced_known 静默计数, 零告警 (存量在修, 不刷不可动作噪声);
  ③ 愈合只报告, **守卫绝不自动改写基线** (棘轮只降不升);
  ④ 基线损坏 → *_detector_error ("零违反"与"守卫自己崩了"必须可区分);
  ⑤ 文件解析失败 → W456_parse_error (不得当"零违反");
  ⑥ 首跑自建基线不告警;
  ⑦ 口径钉: 只算 **except 块内** 的 debug / 无栈告警; 带 exc_info=True 或 logger.exception
     不算无栈; 点号 logger 名不算裸名; 嵌套 except 只计一次 (防重复计数把基线抬虚);
  ⑧ 扫描面钉: tests/ 与 archive/ 必须排除 (墓地与测试不是生产可见面, 纳入即染红判据);
  ⑨ 接线钉: main() 必须真调 `_check_log_hygiene` 且结果进 result JSON (W2 ⑦同型 ——
     没有调度方的守卫等于纸面军规, 这正是 W0 工具此前的处境)。

回滚: 删本文件 + revert 对应提交 + 删 data/{debug_swallow,bare_logger,except_stack}_baseline.json。
"""
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check", PROJECT_ROOT / "scripts" / "daily_invariant_check.py")
dic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dic)

CLEAN = '''import logging
logger = logging.getLogger("app.clean")


def f():
    logger.debug("except 块外的 debug — 不算违反")
    try:
        return 1
    except Exception as e:
        logger.warning("失败: %s", e, exc_info=True)
    try:
        return 2
    except ValueError:
        logger.exception("带栈")
'''

SWALLOW = '''import logging
logger = logging.getLogger("app.swallow")


def f():
    try:
        return 1
    except Exception as e:
        logger.debug(f"失败被吞: {e}")
'''

NO_STACK = '''import logging
logger = logging.getLogger("app.nostack")


def f():
    try:
        return 1
    except Exception as e:
        logger.warning(f"失败无栈: {e}")
'''

BARE = '''import logging
logger = logging.getLogger("bare_name")
'''


def _repo(tmp_path, files: dict) -> Path:
    """搭一个最小仓: files = {相对路径: 源码}。"""
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return tmp_path


def _wire(monkeypatch, tmp_path):
    """把 REPO 与三个基线路径指到 tmp; 返回 hits 收集器。"""
    monkeypatch.setattr(dic, "REPO", tmp_path)
    monkeypatch.setattr(dic, "_HYGIENE_BASELINES", {
        d: tmp_path / "data" / f"{d}_baseline.json" for d in dic._HYGIENE_DIMS})
    (tmp_path / "data").mkdir(exist_ok=True)
    hits = []

    def rec(k, rid, sym, msg):
        hits.append((k, str(sym), msg))

    return rec, hits


def _baseline(tmp_path, dim, counts, refs=None):
    p = tmp_path / "data" / f"{dim}_baseline.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps({"counts": counts, "refs": refs or {}}, ensure_ascii=False),
                 encoding="utf-8")
    return p


# ─────────────────────── ⑦ 口径钉 (单文件扫描) ───────────────────────

def test_scan_dimensions_and_exclusions(tmp_path):
    """except 块外 debug / 带 exc_info / logger.exception / 点号 logger 名 都不得计入。"""
    got = dic._scan_log_hygiene(_repo(tmp_path, {"a.py": CLEAN}) / "a.py")
    assert got["debug_swallow"] == 0, "except 块外的 debug 被计入 ⇒ 正常调试日志会被判违"
    assert got["except_stack"] == 0, "exc_info=True / logger.exception 被计入 ⇒ 带栈也判违"
    assert got["bare_logger"] == 0, "点号 logger 名被当裸名"

    got = dic._scan_log_hygiene(_repo(tmp_path, {"b.py": SWALLOW}) / "b.py")
    assert got["debug_swallow"] == 1 and got["except_stack"] == 0

    got = dic._scan_log_hygiene(_repo(tmp_path, {"c.py": NO_STACK}) / "c.py")
    assert got["except_stack"] == 1 and got["debug_swallow"] == 0

    got = dic._scan_log_hygiene(_repo(tmp_path, {"d.py": BARE}) / "d.py")
    assert got["bare_logger"] == 1


def test_nested_except_counted_once(tmp_path):
    """嵌套 except 只计一次 — 否则重复计数把基线抬虚, 棘轮起点就是错的。"""
    src = '''import logging
logger = logging.getLogger("app.nested")


def f():
    try:
        try:
            return 1
        except ValueError as e:
            logger.debug(f"内层: {e}")
    except Exception as e:
        logger.debug(f"外层: {e}")
'''
    got = dic._scan_log_hygiene(_repo(tmp_path, {"n.py": src}) / "n.py")
    assert got["debug_swallow"] == 2, f"嵌套 except 计数错 (应 2, 实 {got['debug_swallow']})"


def test_refs_carry_line_and_code(tmp_path):
    """refs 是诊断样本: 必须带行号与代码片段 (告警正文要能直接指到病灶)。"""
    got = dic._scan_log_hygiene(_repo(tmp_path, {"b.py": SWALLOW}) / "b.py")
    ref = got["refs"]["debug_swallow"][0]
    assert ref["line"] == 9 and "logger.debug" in ref["code"]


# ─────────────────────── ⑧ 扫描面钉 ───────────────────────

def test_scan_face_excludes_tests_and_archive(tmp_path):
    """tests/ venv/ archive/ __pycache__/ 必须排除; main.py + app/scripts/ops/config 纳入。"""
    repo = _repo(tmp_path, {
        "main.py": CLEAN,
        "app/live.py": SWALLOW,
        "scripts/tool.py": NO_STACK,
        "scripts/archive/dead.py": SWALLOW,       # 墓地: 军规要求零 import, 不必守
        "tests/test_x.py": SWALLOW,               # 测试不是生产可见面
        "venv/lib/site-packages/x.py": SWALLOW,
        "ops/check.py": BARE,
        "config/gen.py": CLEAN,
        "app/__pycache__/cached.py": SWALLOW,
    })
    files = [str(p.relative_to(repo)) for p in dic._hygiene_py_files(repo)]
    assert "main.py" in files and "app/live.py" in files and "scripts/tool.py" in files
    assert "ops/check.py" in files and "config/gen.py" in files
    for banned in ("scripts/archive/dead.py", "tests/test_x.py",
                   "venv/lib/site-packages/x.py", "app/__pycache__/cached.py"):
        assert banned not in files, f"{banned} 不该进扫描面"
    assert files == sorted(files), "扫描面必须确定性排序 (否则基线逐日抖动)"


# ─────────────────────── ⑥ 首跑自建基线 ───────────────────────

def test_first_run_establishes_baselines_without_alarm(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"main.py": CLEAN, "app/a.py": SWALLOW, "app/b.py": NO_STACK + BARE})
    rec, hits = _wire(monkeypatch, repo)
    out = dic._check_log_hygiene(rec)
    assert hits == [], f"首跑不该告警: {hits}"
    for dim in dic._HYGIENE_DIMS:
        info = out[dic._HYGIENE_CODES[dim]]
        assert info["status"] == "baseline_established"
        p = dic._HYGIENE_BASELINES[dim]
        assert p.exists(), f"{dim} 基线未落盘"
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["counts"] and data["note"] and data["deb_ref"], "基线必须自带纪律说明与出处"
    assert out["W4_debug_swallow"]["total"] == 1
    assert out["W6_except_stack"]["total"] == 1
    assert out["W5_bare_logger"]["total"] == 1
    assert out["parse_errors"] == 0


# ─────────────────────── ① 基线外新增 / 增长 → 告警 ───────────────────────

def test_new_file_with_debug_swallow_alarms(tmp_path, monkeypatch):
    """①真目标: 新文件里写 `except → logger.debug` ⇒ 当晚 W4_debug_swallow_new_file。"""
    repo = _repo(tmp_path, {"main.py": CLEAN, "app/old.py": SWALLOW, "app/new.py": SWALLOW})
    rec, hits = _wire(monkeypatch, repo)
    _baseline(tmp_path, "debug_swallow", {"app/old.py": 1})
    _baseline(tmp_path, "bare_logger", {})
    _baseline(tmp_path, "except_stack", {})
    out = dic._check_log_hygiene(rec)
    keys = [h[0] for h in hits]
    assert "W4_debug_swallow_new_file" in keys, f"基线外新增未告警: {keys}"
    assert any(h[1] == "app/new.py" for h in hits), "告警必须指到具体文件"
    assert any("exc_info=True" in h[2] for h in hits), "告警必须带可执行处置 (改法)"
    assert out["W4_debug_swallow"]["status"] == "drift"


def test_growth_in_known_file_alarms(tmp_path, monkeypatch):
    """①同一文件从 1 处涨到 2 处 ⇒ W4_debug_swallow_grew (per-file 计数抓得住)。"""
    two = SWALLOW + '''

def g():
    try:
        return 2
    except Exception as e:
        logger.debug(f"又一处: {e}")
'''
    repo = _repo(tmp_path, {"main.py": CLEAN, "app/a.py": two})
    rec, hits = _wire(monkeypatch, repo)
    _baseline(tmp_path, "debug_swallow", {"app/a.py": 1})
    _baseline(tmp_path, "bare_logger", {})
    _baseline(tmp_path, "except_stack", {})
    out = dic._check_log_hygiene(rec)
    assert [h[0] for h in hits] == ["W4_debug_swallow_grew"], hits
    assert "1→2" in hits[0][2], f"告警正文要给增量: {hits[0][2]}"
    assert out["W4_debug_swallow"]["grew"] == ["app/a.py: 1→2 (+1)"]


def test_new_bare_logger_alarms(tmp_path, monkeypatch):
    """①W5: 新增裸名 logger ⇒ 告警 (裸名使按子系统设级/过滤覆盖不到, DEB §5)。"""
    repo = _repo(tmp_path, {"main.py": CLEAN, "scripts/new_tool.py": BARE})
    rec, hits = _wire(monkeypatch, repo)
    for dim in dic._HYGIENE_DIMS:
        _baseline(tmp_path, dim, {})
    dic._check_log_hygiene(rec)
    assert [h[0] for h in hits] == ["W5_bare_logger_new_file"], hits
    assert "ftseek." in hits[0][2], "处置必须给出 SSOT 命名约定"


def test_new_no_stack_warning_alarms(tmp_path, monkeypatch):
    """①W6: 新增无栈告警 ⇒ 告警 (有告警无栈 = 让值班人猜根因)。"""
    repo = _repo(tmp_path, {"main.py": CLEAN, "app/n.py": NO_STACK})
    rec, hits = _wire(monkeypatch, repo)
    for dim in dic._HYGIENE_DIMS:
        _baseline(tmp_path, dim, {})
    dic._check_log_hygiene(rec)
    assert [h[0] for h in hits] == ["W6_except_stack_new_file"], hits


# ─────────────────── ②③ 存量静默 / 愈合不改写基线 ───────────────────

def test_known_stock_is_silenced(tmp_path, monkeypatch):
    """②基线内存量零告警, 只进 silenced_known 计数 (不刷不可动作噪声)。"""
    repo = _repo(tmp_path, {"main.py": CLEAN, "app/old.py": SWALLOW})
    rec, hits = _wire(monkeypatch, repo)
    _baseline(tmp_path, "debug_swallow", {"app/old.py": 1})
    _baseline(tmp_path, "bare_logger", {})
    _baseline(tmp_path, "except_stack", {})
    out = dic._check_log_hygiene(rec)
    assert hits == []
    assert out["W4_debug_swallow"]["silenced_known"] == 1
    assert out["W4_debug_swallow"]["status"] == "ok"


def test_heal_reported_but_baseline_untouched(tmp_path, monkeypatch):
    """③计数下降 → healed 列出提示人工收缩, 但基线文件**一字不改** (守卫不得自改判据)。"""
    repo = _repo(tmp_path, {"main.py": CLEAN, "app/fixed.py": CLEAN})
    rec, hits = _wire(monkeypatch, repo)
    p = _baseline(tmp_path, "debug_swallow", {"app/fixed.py": 3, "app/gone.py": 2})
    _baseline(tmp_path, "bare_logger", {})
    _baseline(tmp_path, "except_stack", {})
    before = p.read_text(encoding="utf-8")
    out = dic._check_log_hygiene(rec)
    assert hits == [], "愈合不该告警"
    assert out["W4_debug_swallow"]["healed"] == ["app/fixed.py: 3→0", "app/gone.py: 2→0"]
    assert p.read_text(encoding="utf-8") == before, "守卫自动改写了基线 ⇒ 棘轮失效"


# ─────────────── ④⑤ 守卫自己坏了必须可区分 ───────────────

def test_corrupt_baseline_is_detector_error(tmp_path, monkeypatch):
    """④基线 JSON 损坏 → *_detector_error, 不得读成"零违反"。"""
    repo = _repo(tmp_path, {"main.py": CLEAN, "app/a.py": SWALLOW})
    rec, hits = _wire(monkeypatch, repo)
    p = _baseline(tmp_path, "debug_swallow", {"app/a.py": 1})
    p.write_text("{ 不是 JSON", encoding="utf-8")
    _baseline(tmp_path, "bare_logger", {})
    _baseline(tmp_path, "except_stack", {})
    out = dic._check_log_hygiene(rec)
    assert [h[0] for h in hits] == ["W4_debug_swallow_detector_error"], hits
    assert "不可信" in hits[0][2]
    assert out["W4_debug_swallow"]["status"] == "detector_error"
    assert out["W5_bare_logger"]["status"] == "ok", "一维基线坏不得牵连另两维"
    assert out["W6_except_stack"]["status"] == "ok"


def test_unparsable_file_is_loud(tmp_path, monkeypatch):
    """⑤语法坏文件 = 三维棘轮对它失明 ⇒ W456_parse_error, 不得当"零违反"。"""
    repo = _repo(tmp_path, {"main.py": CLEAN, "app/broken.py": "def f(:\n  pass\n"})
    rec, hits = _wire(monkeypatch, repo)
    for dim in dic._HYGIENE_DIMS:
        _baseline(tmp_path, dim, {})
    out = dic._check_log_hygiene(rec)
    assert out["parse_errors"] == 1
    assert any(h[0] == "W456_parse_error" and h[1] == "app/broken.py" for h in hits), hits


# ─────────────────────── ⑨ 接线钉 ───────────────────────

def test_wired_into_main_and_result_json():
    """⑨没有调度方的守卫 = 纸面军规 (W0 工具此前正是这个处境)。"""
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
    assert "log_hygiene = _check_log_hygiene(rec)" in src, "main() 未调用 W4/W5/W6"
    assert '"log_hygiene": log_hygiene' in src, "结果未进 invariant_check_latest.json (不可复盘)"
    # 告警面必须真接上: 本脚本 logger.error → logs/invariant_check.log → error_alert.py 扫描面
    assert "error_alert.py" in src, "文件头仍写 error_alert.sh ⇒ 告警链口径与实现不符"


def test_cron_face_still_mounts_invariant_check():
    """cron `5 5 * * 2-6` 是本守卫唯一调度方 — 摘掉即三条棘轮全部停摆。"""
    import subprocess
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
    except Exception as e:
        import pytest
        pytest.skip(f"crontab 不可用: {e}")
    if cur.returncode != 0:
        import pytest
        pytest.skip("无用户 crontab 的环境")
    lines = [l for l in cur.stdout.splitlines()
             if l.strip() and not l.strip().startswith("#") and "daily_invariant_check" in l]
    assert lines, "daily_invariant_check 已从 crontab 摘除 ⇒ W1/W2/W3/W4/W5/W6 全停"
    assert "invariant_check.log" in lines[0], "stdout 未落盘 ⇒ error_alert.py 扫不到违反"


# ─────────────────── 真实仓库现状 (慢测: 全仓 AST 一遍 ~7s) ───────────────────

def test_real_repo_within_registered_baseline():
    """真仓库 + 真基线: 零违反。基线若被删则重建 (fresh clone 形态), 同样零违反。

    这条用例与每晚 cron 同口径 —— 它红了就说明有人新增了基线外的日志卫生违反,
    处置 = 改代码 (推荐) 或经 ADR/DEB 落档后登记 data/*_baseline.json (改判据)。
    """
    hits = []

    def rec(k, rid, sym, msg):
        hits.append((k, str(sym), msg))

    out = dic._check_log_hygiene(rec)
    assert out["parse_errors"] == 0, "生产代码里有解析不了的 .py ⇒ 棘轮对它失明"
    assert hits == [], f"真实仓库出现基线外违反 (处置见告警正文): {hits[:5]}"
    for dim in dic._HYGIENE_DIMS:
        info = out[dic._HYGIENE_CODES[dim]]
        assert info["status"] in ("ok", "baseline_established"), info
        assert info["total"] > 0, f"{dim} 计数为 0 — 要么扫描面坏了, 要么存量真清零 (可收缩基线)"
    # 存量规模登记 (DEB §8.2 的数字必须与基线文件一致, 防文档与实现漂移)
    assert out["W4_debug_swallow"]["total"] >= 500
    assert out["W6_except_stack"]["total"] >= 900
    assert out["W5_bare_logger"]["total"] >= 10
