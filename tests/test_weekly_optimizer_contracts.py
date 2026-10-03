#!/usr/bin/env python3
"""WeeklyOptimizer 退役墓碑测试 (2026-10-03 DEB-20261003 方案A, 用户裁「A」).

历史 (本文件同日两形, 每一形都钉真实行为差异):
  v1 (P0-3 绊线, commit 00cccd060): 3 条 xfail(strict) 钉列名错配 + 参数不可识别
     缺陷现状 (修复即 XPASS 报警)。反证已做: 列名正确的合成表上 4 参数全部可识别
     ⇒ 断言逻辑真绿非恒红。
  v2 (本文件): 用户裁方案 A = 退役收敛 — weekly_optimizer.py (957 行) +
     live_backtest_comparison.py (394 行) 整体删除, v1 绊线对象不复存在。
     按退役纪律 (工程变更规范 §Dead Code Removal): **不删测试**, 转墓碑 —
     钉「不得复活」+「让事故成为可能的土壤条件不得再生」。

退役根因 (证据链见 docs/debates/DEB-20261003-weekly-optimizer-p26-rebuild.md §2):
  ① 列名错配: load_trade_rows 请求 max_drawdown_during_trade, 生产 DDL 真实列名
     max_drawdown ⇒ pick() 静默回退 NULL ⇒ SL 反事实 205 笔零触发, stop_loss_pct
     不可识别 (5 网格值 1 个 pnl 序列), 头条 sl=-0.4 = tie-break 恒选网格序首个,
     连续 7 周输出常量 (08-28~10-02 报告留档 data/optimization_reports/);
  ② 量纲猜测: `mdd>1.5 → /100` 启发式把百分数列劈两半量纲差 100 倍, 修好列名后
     全部候选被 MAX_DD_HARD_LIMIT 淘汰 ⇒ 输出「无解」;
  ③ 虚构基线: LIVE_CURRENT_PARAMS 自称与 unified_config.yaml 对齐, 实测三值
     (sl=-0.045/tp=0.025/trailing_stop_trigger=0.01) 生产配置零命中;
  ④ 架构平行: _replay_trade 与生产退出链零共享代码, 优化的从来不是真实策略。

职责归宿 (收敛到的既有资产, 全部活跃):
  · 实弹影子: app/playbook/exit_shadow.py → data/shadow/exit_param_shadow.jsonl
    + scripts/exit_shadow_graduation.py 预注册三门 (9/26 裁决留痕 shadow_registry);
  · 官方回放: scripts/exit_replay_backtest.py (复用生产 SSOT
    app.playbook.exit_engine._compute_dynamic_effective_sl, reason_match<0.80 冻结门);
  · 参数真源: config/playbook_horizon.yaml;
  · 周报展示: ops/hermes/weekly_scorecard_carrier.py 第三块 exit_shadow_report
    (真实参数 vs 影子参数对比, 2026-10-03 已落地)。
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_SCHEDULER_SRC = os.path.join(REPO, "app", "core", "scheduler_triggers.py")
# 生产 DDL 真源 (CREATE TABLE trade_records)
_TRADE_STORE_DDL = os.path.join(REPO, "app", "storage", "trade_store.py")


class TestRetirementTombstone:
    """退役墓碑: 模块不得复活, scheduler 与全仓不得再有活引用。"""

    def test_weekly_optimizer_module_deleted(self):
        """weekly_optimizer.py 必须不存在 (复活 = 推翻用户裁决, 须先删本测试并重新提案)。"""
        p = os.path.join(REPO, "app", "backtest", "weekly_optimizer.py")
        assert not os.path.exists(p), (
            f"{p} 复活了 — DEB-20261003 方案A 已裁退役。复活前提: 新 DEB 提案 + "
            "复用生产 SSOT 数学 (exit_engine._compute_dynamic_effective_sl) + "
            "live 基线运行时读 playbook_horizon.yaml + 列名/量纲契约测试先行"
        )

    def test_live_backtest_comparison_module_deleted(self):
        """live_backtest_comparison.py 同批退役 (import LIVE_CURRENT_PARAMS 共享虚构基线)。"""
        p = os.path.join(REPO, "app", "backtest", "live_backtest_comparison.py")
        assert not os.path.exists(p), f"{p} 复活了 — 同 weekly_optimizer 退役批 (DEB-20261003)"

    def test_modules_not_importable(self):
        """import 层面钉死 (防 .pyc 残魂 / sys.path hack 绕过文件存在性检查)。"""
        import importlib
        for mod in ("app.backtest.weekly_optimizer", "app.backtest.live_backtest_comparison"):
            try:
                importlib.import_module(mod)
                raise AssertionError(
                    f"{mod} 仍可 import — 先清 __pycache__ 复跑; 若真复活见墓碑 docstring"
                )
            except ModuleNotFoundError:
                pass

    def test_scheduler_has_no_live_reference(self):
        """scheduler_triggers.py 不得再有活引用 (注释墓碑允许, import/调用/flag 不允许)。"""
        with open(_SCHEDULER_SRC, encoding="utf-8") as f:
            src = f.read()
        live_refs = []
        for i, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue  # 注释墓碑 = 退役留痕, 合法
            if re.search(r"weekly_optimizer|run_weekly_optimization|live_backtest_comparison|"
                         r"run_comparison\b|_TASK16_WEEKLY_OPTIMIZER_ENABLED", line):
                live_refs.append(f"L{i}: {s[:100]}")
        assert not live_refs, f"scheduler 出现 Task#16 活引用 (退役被推翻?): {live_refs}"

    def test_no_other_live_consumers_repo_wide(self):
        """全仓 app/ scripts/ ops/ 扫描: 除本墓碑与注释外不得有可执行引用。"""
        hits = []
        self_rel = os.path.relpath(os.path.abspath(__file__), REPO)
        for root in ("app", "scripts", "ops"):
            base = os.path.join(REPO, root)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fn in filenames:
                    if not fn.endswith(".py"):
                        continue
                    fp = os.path.join(dirpath, fn)
                    rel = os.path.relpath(fp, REPO)
                    if rel == self_rel:
                        continue
                    with open(fp, encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if line.strip().startswith("#"):
                                continue
                            if re.search(r"\bweekly_optimizer\b|\blive_backtest_comparison\b", line):
                                hits.append(f"{rel}:{i}: {line.strip()[:90]}")
        assert not hits, f"发现 Task#16 退役模块的活引用: {hits}"


class TestRootCauseGuards:
    """根因守卫: 让本次事故成为可能的土壤条件, 独立于模块存亡继续钉住。"""

    def test_production_ddl_column_name(self):
        """生产 DDL 的列名是 max_drawdown (不是 max_drawdown_during_trade)。

        v1 契约①的存活部分: 未来任何消费 trade_records 回撤列的代码, 用错列名
        + 静默 NULL 回退 = 本次事故重演。此测试钉住真名, 供未来契约引用。
        """
        with open(_TRADE_STORE_DDL, encoding="utf-8") as f:
            src = f.read()
        m = re.search(
            r"CREATE TABLE IF NOT EXISTS trade_records \((.*?)\n\s*\)\s*\n", src, re.S
        )
        assert m, "trade_records CREATE TABLE 块消失 (DDL 真源漂移?)"
        cols = set(re.findall(r"^\s*(\w+)\s+(?:INTEGER|TEXT|REAL)", m.group(1), re.M))
        cols.update(re.findall(r"ADD COLUMN (\w+)\s", src))
        assert "max_drawdown" in cols
        assert "max_drawdown_during_trade" not in cols

    def test_fabricated_baseline_constants_not_regenerated(self):
        """LIVE_CURRENT_PARAMS 式虚构基线常量不得在 app/ 任何活代码里再生。

        钉的是**模式**不是裸数值: ①常量名 LIVE_CURRENT_PARAMS (自称「与
        unified_config.yaml 对齐」而实测三值生产配置零命中的伪影源头);
        ②死参数键 trailing_stop_trigger (生产配置不存在该键、全仓零消费点)。
        任何未来模块若要声明 live 基线, 必须运行时读 config (纪律: 口径核线上
        config 实值, 勿用代码默认)。
        排除项: trailing_stop_triggered (布尔遥测列, 合法近邻); 裸数值 -0.045
        不扫 — 实测 app/risk/position_exit_monitor.py:537 存在既有的
        `dyn_cfg.get("stop_loss", -0.045)` 运行时 fallback 默认值, 属同族
        「代码默认值」坏习惯但非本常量伪影, 且在风控退出路径、不在本批范围
        (2026-10-03 退役批核查时发现, 留待独立评估)。
        """
        pat = re.compile(r"\bLIVE_CURRENT_PARAMS\b|\btrailing_stop_trigger\b")
        hits = []
        base = os.path.join(REPO, "app")
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dirpath, fn)
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if line.strip().startswith("#"):
                            continue
                        probe = line.replace("trailing_stop_triggered", "")
                        if pat.search(probe):
                            hits.append(
                                f"{os.path.relpath(fp, REPO)}:{i}: {line.strip()[:90]}"
                            )
        assert not hits, f"虚构基线常量/死参数键复活 (live 基线必须运行时读 config): {hits}"
