# ADR-031: 反馈注入链路修复 + LLM prompt 置信度校准修订

**日期**: 2026-08-22
**状态**: 已采纳 (用户裁决 "prompt修改不影响整个交易链路逻辑，请基于发现做修复调整")
**关联**: DEB-20260822 §九 (唯一详细事实源), ADR-030 (同日体检执行轮)

## 背景

"盘感"假设 (更多交易+数据 → LLM 形成盘感) 的查证发现: LLM 权重冻结、无自身历史检索, 反馈注入是模型经验的唯一通道; 而该通道三处坏损 (键名错配致置信度段恒空 / 依赖已删的 6/19 IC 报告 fallback 到硬编码数字 / 每日注入"优先推荐manual方向"误导结论)。prompt 侧另有与实测矛盾的硬规则 ("突发事件特权 conf≥0.7+" 在数据最少场景制造假自信; "防御模式只做SHORT" 推向实测最弱方向; "止损≥10%" 与执行器 2-5% 分桶 SL 冲突)。

## 决策

1. 重写 `_build_signal_quality_context` (llm_pipeline.py): 全部数据来自 `data/signal_quality/latest.json` 实时文件; 胜率旁必列期望值; 缺数据输出"反馈不可用"不编造; 无任何硬编码结论。调用方签名不变。
2. strategy.py 规则块修订: 置信度校准锚替代 0.7+ 特权; SHORT/LONG 同等证据门槛; 周期对齐 (T+5 正指, time_horizon 建议 120h); 职责边界声明 (SL/仓位以执行引擎为准)。Livermore 铁律行去执行器冲突。
3. deep_target_analysis.py 增补【自我认知原则】11-13 条: 校准 / 空头对称门槛 / 与历史模式一致须给差异化理由。
4. **全部纯增量**: 不改任何输出 JSON schema, 不碰执行器/风控/消费端解析。
5. **(同日追加, 用户令"继续") 个体级自战绩注入** `_build_track_record_context`: 近30日方向性判断逐行 `日期|标的|方向|conf|T+1|T+5 ✓✗` + 小结, 数据源 playbook_instructions.fwd_return_t{N} (方向带符号), 拼入 signal_quality 字符串尾部 → 辩论模板零改动全下游兼容; ~700 token/日; 缺数据报"不可用"不编造。详见 DEB-20260822 §十。
6. **(同日追加②, 用户质询"数据是否更新/是否堆数据"自查) 新鲜度修正**: 原查询要求 `fwd_return_t5 IS NOT NULL` 致近一周未结算判断整批不可见 (实测105笔被丢)。改双段渲染 (已结算 LIMIT 25 + 待定近10笔标"待定") + 小结双口径 + 独立性脚注; token 成本不变。详见 DEB-20260822 §十b。

## 影响

辩论引擎每日可见真实校准数据 (首日即呈现 conf 0-0.3 桶期望 +0.85% 最高、0.5-0.7 桶 -0.17% 最低的倒挂事实)。生成器侧变更, 影响此后指令生成样本 — 变更日期 2026-08-22 记录在案, 8/29 终裁如需可按此切窗对照。

## 验证

tests/core/test_signal_quality_context.py 6 例全过, 含对仓库真实 latest.json 的活体哨兵测试 (防硬编码结论复发)。

## 回滚

git revert 本批 hunk 即可 (app/pipeline/llm_pipeline.py / app/llm/prompts/{strategy,deep_target_analysis}.py), 无数据迁移; 测试文件删除即可。
