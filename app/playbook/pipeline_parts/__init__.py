"""Playbook pipeline 分域拆分包 (P1a, DEB-20261008-playbook-pipeline-refactor-roadmap §4).

迁移纪律 (P1/2a):
1. 本体从 pipeline.py 逐字迁入本包, 行为/判据/日志文案零变更;
2. pipeline.py 保留同名转发器且仍是**内部调用点** — 既有
   ``monkeypatch.setattr("app.playbook.pipeline.<name>")`` 缝全部保持生效;
3. 禁止把本包当作第二交易入口 — 生产入口仍只有 app.playbook.pipeline;
4. 涉及 pipeline 模块级绑定的实现 (datetime / _now_et / 台账路径缝) 在过渡期
   经懒引用回读 pipeline, 直至测试与生产调用全部迁到本包后再解除。
"""
