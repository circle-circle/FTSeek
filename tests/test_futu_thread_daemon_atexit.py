"""futu 线程 daemon 化 + 单例 atexit 有界释放钉子 (2026-09-16, 用户裁「病灶修复」+「一起做掉」).

**病根** (与 `tests/conftest.py` 2026-08-27 那段同一根因, 本文件钉生产侧落点):
futu SDK 的 `CallbackExecutor` 是**非 daemon** `threading.Thread` (永久阻塞在
`self._queue.get()`); 只要有一个 `OpenQuoteContext` 未 `close`, 解释器收尾
`threading._shutdown()` 的 join 就**永不返回** ⇒ 进程干完活却不退出。
**实证 2026-09-16**: DA 盘前 pipeline 20:21 活已干完, 25 分钟不退出 → cron agent
判挂起 → SIGTERM(`PIPELINE_EXIT=143`) → 整条管线全量重跑 (白烧一倍 LLM 预算);
2026-09-11 更留下存活 **2 天 16 小时**的孤儿进程。
**A/B 实测** (同一脚本, 只改该标志): `daemon=False` → exit 124 (挂死);
`daemon=True` → exit 0。

**本文件钉住的不变量**:
  1. import `app.data.futu_lv3_provider` 即把 futu 内部线程置 daemon, 且
     `enable_daemon_threads()` 的模块级调用**位于任何 `OpenQuoteContext(` 之前**
     (线程 daemon 标志在建线程时读取, 事后设置无效);
  2. 单例 atexit 钩子**真的在退出时释放**单例 —— 子进程行为钉, 不是只查注册表;
  3. 该释放**有界**: close 阻塞时不拖住退出 (一个清理钩子不得把退出重新变成挂死);
  4. 单例为 None 时 no-op (不建无用线程、不抛异常)。

**常驻服务不受影响**: `main.py::_graceful_shutdown()` 末尾走 `os._exit(0)`,
atexit 在 os._exit 下**不执行** ⇒ 该钩子对服务是 no-op, 其 LV3 释放仍由
`data_router.close()` (第 4 步) 负责, 语义不变。

**回滚**: 删本文件 + 删 `futu_lv3_provider` 的 `enable_daemon_threads()` /
`_atexit_release_futu_lv3` 及其注册行 (回滚须另开辩论 —— 恢复即等于让 CLI 进程
重新可能挂死)。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "app/data/futu_lv3_provider.py"


@pytest.fixture()
def lv3_module():
    sys.path.insert(0, str(PROJECT_ROOT))
    import app.data.futu_lv3_provider as P
    return P


# ── 1. 模块级调用 + 位置 ────────────────────────────────────────────

def test_module_level_call_precedes_any_connect():
    """import 即生效, 且必须在第一条 futu 连接之前 (daemon 标志建线程时才读)。"""
    src = SRC_PATH.read_text(encoding="utf-8")
    m = re.search(r"^enable_daemon_threads\(\)$", src, re.M)
    assert m, "模块级 `enable_daemon_threads()` 调用缺失 —— import 即生效的前提被破坏"
    first_connect = src.index("OpenQuoteContext(")
    assert m.start() < first_connect, (
        "`enable_daemon_threads()` 出现在首个 OpenQuoteContext 之后 —— "
        "此时线程可能已按非 daemon 建出, 设置无效"
    )


def test_enable_daemon_threads_sets_flag(lv3_module):
    """函数真把 SDK 标志置 True (幂等), 且不残留 False 让测试进程自己挂死。"""
    from futu.common.sys_config import SysConfig

    prev = SysConfig.get_all_thread_daemon()
    try:
        SysConfig.set_all_thread_daemon(False)
        lv3_module.enable_daemon_threads()
        assert SysConfig.get_all_thread_daemon() is True
    finally:
        SysConfig.set_all_thread_daemon(True if prev else True)


def test_import_sets_flag_via_subprocess():
    """干净子进程里 import 本模块 → 标志为 True (证明落点在 import 路径上)。"""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from futu.common.sys_config import SysConfig\n"
        "assert SysConfig.get_all_thread_daemon() is False, '子进程初始就不是 False, 断言失效'\n"
        "import app.data.futu_lv3_provider  # noqa: F401\n"
        "print('FLAG', SysConfig.get_all_thread_daemon())\n" % str(PROJECT_ROOT)
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=120, cwd=str(PROJECT_ROOT))
    assert r.returncode == 0, r.stderr[-800:]
    assert "FLAG True" in r.stdout


# ── 2. atexit 真的释放 (子进程行为钉) ──────────────────────────────

def test_atexit_releases_singleton_in_subprocess(tmp_path):
    marker = tmp_path / "atexit_marker.txt"
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import app.data.futu_lv3_provider as P\n"
        "class _Fake:\n"
        "    def close(self):\n"
        "        open(%r, 'a').write('closed\\n')\n"
        "P._futu_lv3_instance = _Fake()\n"
        "print('MAIN_DONE')\n" % (str(PROJECT_ROOT), str(marker))
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=120, cwd=str(PROJECT_ROOT))
    assert r.returncode == 0, r.stderr[-800:]
    assert "MAIN_DONE" in r.stdout
    assert marker.exists(), "atexit 钩子未注册或未触发 (退出时单例没被 release)"
    assert "closed" in marker.read_text(encoding="utf-8")


# ── 3. 释放有界 (close 阻塞也不拖住退出) ───────────────────────────

def test_atexit_release_is_bounded(lv3_module, monkeypatch):
    monkeypatch.setattr(lv3_module, "_futu_lv3_instance", object())
    monkeypatch.setattr(lv3_module, "reset_futu_lv3_provider", lambda: time.sleep(5))
    t0 = time.monotonic()
    lv3_module._atexit_release_futu_lv3(timeout=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, (
        f"释放未受 timeout 约束 (耗时 {elapsed:.1f}s) —— 清理钩子会把退出变成挂死"
    )


# ── 4. 单例为 None 时 no-op ───────────────────────────────────────

def test_atexit_release_noop_when_no_singleton(lv3_module, monkeypatch):
    def _boom():
        raise AssertionError("单例为 None 时不应调用 reset")

    monkeypatch.setattr(lv3_module, "_futu_lv3_instance", None)
    monkeypatch.setattr(lv3_module, "reset_futu_lv3_provider", _boom)
    t0 = time.monotonic()
    lv3_module._atexit_release_futu_lv3(timeout=5.0)
    assert time.monotonic() - t0 < 1.0