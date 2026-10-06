import os
from pathlib import Path

import pytest


@pytest.mark.parametrize("op_name", ["replace", "rename"])
def test_atomic_write_guard_blocks_production_data(op_name, tmp_path):
    src = tmp_path / "missing"
    dst = Path("data") / "_conftest_guard_probe.tmp"
    with pytest.raises(RuntimeError, match="测试写入生产数据被拦"):
        getattr(os, op_name)(src, dst)
