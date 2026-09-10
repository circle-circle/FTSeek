"""WARNING 级别纪律 (2026-09-10, DEB-20260910 §3 议题三, 用户裁「按照建议执行优化」).

病灶 (7 日日志实测): WARNING 1721 条 vs ERROR+CRITICAL 12 条 = **143:1**, 其中 27.8% 是级别误用:
  · `app.positions.reconciler` 359 条 (20.9%) — 全是启动对账**成功**摘要横幅 (16 行 × 8 次重启);
  · `ftseek_us.config_audit` 120 条 (7.0%) — 全是自带 `⚠️ [INFO]`/`⚠️ [DRIFT]` 标签的非阻断项。
后果: 成功与失败同级 ⇒ WARNING 失去区分度, 真告警被埋 (ERROR 覆盖只剩 intraday_heartbeat 一道)。

本文件是 DEB §3.4 判据 1/2/5 的**机器化**, 且刻意双向 —— 只测"降噪成功"会放过致盲:
  ① 成功路径零 WARNING (判据 1/2);
  ② 失败路径**必须仍** WARNING (判据 5: 人为制造一次补录失败必须仍出 WARNING);
  ③ 阻断路径必须仍 CRITICAL + strict=True 仍 raise (降噪不得削弱阻断语义)。
"""
import logging

import pytest
import yaml

from app.core import config_audit
from app.positions.reconciler import ReconcileResult, StartupReconciler

RECON_LOGGER = "app.positions.reconciler"
AUDIT_LOGGER = "ftseek_us.config_audit"


@pytest.fixture
def reconciler(tmp_path):
    """最小可用 reconciler: 无 broker adapter, 签名文件落 tmp (不碰 data/)。"""
    return StartupReconciler(position_store=None, broker_adapter=None,
                             db_path=str(tmp_path / "ftseek_us.db"))


def _levels(records, logger_name):
    return [r.levelname for r in records if r.name == logger_name]


# ───────────────────────── ① 成功路径零 WARNING ─────────────────────────

def test_success_summary_is_info_not_warning(reconciler, caplog):
    """判据 1: 对账成功摘要整段降为 INFO (改前 16 行全是 WARNING)。"""
    result = ReconcileResult(matched=3, broker_only_added=1, tagged_legacy=1,
                             local_only_ghosted=1, errors=0)
    with caplog.at_level(logging.INFO, logger=RECON_LOGGER):
        reconciler._log_summary(result, {"US.AAPL": {}}, {"US.MSFT": object()})

    assert "WARNING" not in _levels(caplog.records, RECON_LOGGER), \
        "成功摘要仍在打 WARNING ⇒ 成功与失败同级 (原病灶)"
    infos = [r for r in caplog.records
             if r.name == RECON_LOGGER and r.levelname == "INFO"]
    assert len(infos) == 1, f"摘要必须合成**单条原子 record** (防多线程穿插), 实际 {len(infos)} 条"
    body = infos[0].getMessage()
    for needle in ("[Reconciler] 启动对账完成", "matched         : 3",
                   "broker_only     : 1", "local_only      : 1", "errors          : 0"):
        assert needle in body, f"降噪不得丢字段: 缺 {needle!r}"


def test_success_summary_with_errors_stays_info(reconciler, caplog):
    """errors>0 时摘要仍 INFO — 信号不丢, 因为逐笔失败已在 :223/:252 各出一条 WARNING。"""
    with caplog.at_level(logging.INFO, logger=RECON_LOGGER):
        reconciler._log_summary(ReconcileResult(matched=1, errors=2), {}, {})
    assert "WARNING" not in _levels(caplog.records, RECON_LOGGER)
    assert "errors          : 2" in caplog.records[-1].getMessage()


def test_separator_lines_gone(reconciler, caplog):
    """DEB §9 议题 3 附带项: `"=" * 70` 纯分隔符行必须消失 (降级后仍是噪音)。"""
    with caplog.at_level(logging.INFO, logger=RECON_LOGGER):
        reconciler._log_summary(ReconcileResult(), {}, {})
    assert not any("=" * 70 in r.getMessage() for r in caplog.records), \
        "纯分隔符行仍在 — 它零信息量且占日志体积"


def test_config_audit_non_blocking_is_info(caplog):
    """判据 2: 自带 `[INFO]`/`[DRIFT]` 标签的非阻断项降为 INFO (改前 logger.warning)。"""
    yaml_data = _lower_keys(yaml.safe_load(
        open("config/unified_config.yaml", encoding="utf-8")))
    warnings = config_audit.audit_config_consistency(yaml_data)
    non_blocking = [w for w in warnings if "[BLOCKING]" not in w]
    assert non_blocking, "真实配置当前应有非阻断项 (若已归零, 本用例需改用注入桩)"

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        n_blocking = config_audit.print_config_audit(yaml_data, strict=True)

    assert n_blocking == 0
    assert "WARNING" not in _levels(caplog.records, AUDIT_LOGGER), \
        "消息自称 [INFO] 却用 WARNING 输出 ⇒ 名实矛盾 (原病灶)"
    infos = [r for r in caplog.records
             if r.name == AUDIT_LOGGER and "非阻断项" in r.getMessage()]
    assert len(infos) == 1, "非阻断清单必须合成单条原子 record"
    body = infos[0].getMessage()
    for w in non_blocking:
        assert w in body, f"降噪不得丢项: 缺 {w[:60]!r}"


def _lower_keys(d):
    if isinstance(d, dict):
        return {str(k).lower(): _lower_keys(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_lower_keys(x) for x in d]
    return d


# ───────────────── ② 失败路径必须仍告警 (降噪不得致盲) ─────────────────

def test_local_only_failure_still_warns(reconciler, caplog):
    """判据 5: 人为制造一次 local_only 标记失败 ⇒ 必须仍出 WARNING。"""
    def boom(position):
        raise RuntimeError("标记 ghost 失败 (测试注入)")

    reconciler._mark_ghost = boom
    with caplog.at_level(logging.INFO, logger=RECON_LOGGER):
        result = reconciler._reconcile({}, {"US.SKYT": object()})

    warns = [r.getMessage() for r in caplog.records
             if r.name == RECON_LOGGER and r.levelname == "WARNING"]
    assert any("Reconciler·local_only" in m and "US.SKYT" in m for m in warns), \
        f"降噪把真告警一起降了 (致盲): {warns}"
    assert result.errors == 1 and result.local_only_ghosted == 0


def test_broker_only_failure_still_warns(reconciler, caplog):
    """判据 5 (镜像): broker_only 补录失败仍须 WARNING, 且计入 errors。"""
    def boom(symbol, info):
        raise RuntimeError("补录失败 (测试注入)")

    reconciler._add_legacy_position = boom
    broker = {"US.GPN": {"direction": "LONG", "qty": 10, "cost": 100.0}}
    with caplog.at_level(logging.INFO, logger=RECON_LOGGER):
        result = reconciler._reconcile(broker, {})

    warns = [r.getMessage() for r in caplog.records
             if r.name == RECON_LOGGER and r.levelname == "WARNING"]
    assert any("Reconciler·broker_only" in m and "US.GPN" in m for m in warns), \
        f"降噪把真告警一起降了 (致盲): {warns}"
    assert result.errors == 1


def test_startup_reconcile_exception_still_warns(reconciler, caplog, monkeypatch):
    """判据 5 (顶层): 整段对账异常 → `:147` 的 WARNING + 栈必须原样保留。"""
    def boom():
        raise RuntimeError("broker 拉取炸了 (测试注入)")

    monkeypatch.setattr(reconciler, "_fetch_broker_positions", boom)
    with caplog.at_level(logging.INFO, logger=RECON_LOGGER):
        result = reconciler.run()

    hits = [r for r in caplog.records
            if r.name == RECON_LOGGER and "启动对账异常" in r.getMessage()]
    assert hits and hits[0].levelname == "WARNING", "顶层降级运行的 WARNING 不得被降"
    assert hits[0].exc_info, "必须带栈 (②批 19 处补 exc_info 的同款纪律)"
    assert result.errors == 1


# ───────────────── ③ 阻断语义不得被削弱 ─────────────────

def test_blocking_still_critical_and_raises(monkeypatch, caplog):
    """BLOCKING 项仍 CRITICAL, strict=True 仍 raise ConfigAuditError (阻断语义不变)。"""
    injected = ["🔴 [BLOCKING] YAML 缺少关键键 risk.trading.max_order_amount (测试注入)",
                "⚠️ [INFO] YAML 缺少键 signal_fusion.us.macro_weight (测试注入)"]
    monkeypatch.setattr(config_audit, "audit_config_consistency", lambda _d: list(injected))

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        with pytest.raises(config_audit.ConfigAuditError):
            config_audit.print_config_audit({}, strict=True)

    levels = _levels(caplog.records, AUDIT_LOGGER)
    assert "CRITICAL" in levels, "BLOCKING 必须仍 CRITICAL"
    assert "WARNING" not in levels, "阻断项走 CRITICAL, 非阻断项走 INFO — WARNING 档已无内容"
    crit = [r.getMessage() for r in caplog.records
            if r.name == AUDIT_LOGGER and r.levelname == "CRITICAL"]
    assert any("BLOCKING" in m for m in crit)
    info = [r.getMessage() for r in caplog.records
            if r.name == AUDIT_LOGGER and "非阻断项" in r.getMessage()]
    assert info and injected[1] in info[0], "混合场景下非阻断项仍须完整列出 (INFO)"


def test_blocking_observe_mode_does_not_raise(monkeypatch, caplog):
    """strict=False (观察模式) 仍不 raise, 但 CRITICAL 记录与"未阻止启动"提示都在。"""
    monkeypatch.setattr(config_audit, "audit_config_consistency",
                        lambda _d: ["🔴 [BLOCKING] 测试注入"])
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        assert config_audit.print_config_audit({}, strict=False) == 1
    msgs = [r.getMessage() for r in caplog.records if r.name == AUDIT_LOGGER]
    assert any("观察模式" in m for m in msgs), "观察模式提示不得随降噪消失"
