"""`pangan_daily_rebuild.py::step0_daily_increment` 的**流归属**契约钉 (2026-09-10)。

病灶 (DEB-20260910 §8.9): `refresh_daily_bars.py` 的 print→logger 后, 摘要走
`logging.basicConfig` 的默认流 = **stderr**; 而 futu SDK 自己的日志走 **stdout**。
原实现只读 `r.stdout` 的末行 ⇒ 恒为 SDK 的 `on_disconnect: Disconnected`,
真实摘要 (`完成: 刷新 0 | 已最新跳过 280 | 失败 1`) 被顶掉。
实证: `logs/pangan_rebuild.log-20260827.gz ~ -20260910` 连续五档的 `[step0]` 行全是
`on_disconnect` ⇒ 日K刷新的失败票名单在该日志里**从未出现过**, 病灶被行首的 "OK" 掩盖
(那是 returncode, 只证明子进程没崩, 不证明刷新成功)。

钉子口径: 直测纯函数 `_pick_refresh_summary`, 不真跑子进程 (真跑 = 281 票 × 1.2s ≈ 7m45s
且打真 Futu 配额)。合成正文取**日志实测逐字样本**, 不用理想化字符串。
"""
import scripts.pangan_daily_rebuild as pdb

# 实测样本: 2026-09-10 11:41 `--dry-run --limit 2` 的真 stdout/stderr 分流结果
SDK_STDOUT = (
    "2026-09-10 05:50:04,398 | 856200 | 139743641446208 | [open_context_base.py:411] "
    "_init_connect_sync: New connect ready: conn=7503570447971080896(1)\n"
    "2026-09-10 05:56:50,244 | 856266 | 140471866025536 | [open_context_base.py:521] "
    "on_disconnect: Disconnected: conn=0(1) reason=CallClose\n"
)
LOGGER_STDERR = (
    "2026-09-10 05:50:11,002 [ftseek.refresh_daily_bars] INFO: [日K刷新] 扫描 281 个日K缓存文件\n"
    "2026-09-10 05:56:50,240 [ftseek.refresh_daily_bars] INFO: "
    "[日K刷新] 完成: 刷新 0 | 已最新跳过 280 | 失败 1\n"
)


def test_summary_comes_from_stderr_not_stdout():
    """主判据: 摘要在 stderr ⇒ 必须取到, 且不得被 stdout 的 SDK 噪声顶掉。"""
    got = pdb._pick_refresh_summary(LOGGER_STDERR, SDK_STDOUT)
    assert "完成: 刷新 0 | 已最新跳过 280 | 失败 1" in got
    assert "on_disconnect" not in got


def test_failed_symbol_list_is_visible():
    """失败票名单是本次修复要救回的真实读数 —— 有失败时两行都要 (只有计数没有名单等于没修)。"""
    stderr = LOGGER_STDERR + (
        "2026-09-10 05:56:50,245 [ftseek.refresh_daily_bars] WARNING: "
        "[日K刷新] 失败票: US.SKYT\n")
    got = pdb._pick_refresh_summary(stderr, SDK_STDOUT)
    assert "完成:" in got and "失败票: US.SKYT" in got


def test_neither_stream_has_summary_is_loud_not_empty():
    """两流都无摘要 ⇒ 显式文案而非空串 (空串与"摘要为空"不可区分 = 静默失败形态)。"""
    got = pdb._pick_refresh_summary("", "")
    assert got and "无摘要输出" in got


def test_fallback_to_stdout_last_line_preserves_legacy_trace():
    """stderr 无 [日K刷新] 行 (例如脚本被改回 print 或崩在 import 前) ⇒ 退回 stdout 末行,
    至少留下 SDK 痕迹证明子进程真跑过, 不静默变空。"""
    got = pdb._pick_refresh_summary("", SDK_STDOUT)
    assert "on_disconnect" in got


def test_unrelated_stderr_noise_does_not_hijack():
    """stderr 里的其他 WARNING/Traceback 不得冒充摘要 (只认 [日K刷新] 标记行)。"""
    stderr = ("Traceback (most recent call last):\n  File \"x.py\", line 1\n"
              "RuntimeError: OpenD 未启动\n")
    got = pdb._pick_refresh_summary(stderr, SDK_STDOUT)
    assert "[日K刷新]" not in got and "RuntimeError" not in got
    assert "on_disconnect" in got  # 退回 stdout 末行
