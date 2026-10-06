"""tests/playbook 共享隔离 — 已上提至 tests/conftest.py (2026-10-06).

原在此的两个 autouse fixture (_isolate_live_arm_halt_file /
_isolate_live_probe_waiver_file) 与生产运维态文件 (HALT/waiver) 隔离相关:

- 2026-10-05 实证: 运维落 HALT 止血 ⇒ 走 LIVE gate 的 14 条用例恒红
  (红因与代码改动无关), 故立本目录级隔离 (efecbba00);
- 2026-10-06 晚实证: 同型病灶在 tests/ 根目录复现 — RFC-135 waiver 身份绑定
  上线后, 根目录 test_rfc080_half_flip_interlock.py 读到生产 waiver
  (authorization_id 与合成配置 TEST-AUTH 不匹配) 恒红, 不受本目录 conftest 覆盖。

处置: 两个 fixture 原样上提到 tests/conftest.py (全测试面单点收口),
本文件仅留此说明, 防止下一个人在 playbook/ 目录再写一份 (双源漂移)。
"""
