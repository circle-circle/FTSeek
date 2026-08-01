# DEB-20260725: LLM 调用链失败语义 — 超时/重试/截断/部分成功的现状与修补选项

> **✅ 用户裁决 2026-08-01: B 三件套全做批准** (②重试分流加 flag + 双信号触发 + 比例制降幅修正) — 系 thinking 灰度硬前置且独立价值成立, 双方无分歧。详见 handoff 变更日志 2026-08-01 首节。
- **状态**: 🔶 待辩论 (作者完成事实链与选项, 等待独立模型辩论后用户裁决)
- **作者**: Qoder (主会话)
- **日期**: 2026-07-25
- **缘起**: 用户质询 — "分析如果超时或者失败或者部分失败无输出时, 是否有重试或者
  接受部分成功的内容?" 用户同意留档, 交另一模型辩论后再决策。
- **关联**: DEB-20260725-llm-thinking-mode-tradeoff §五/Q5 (thinking ON 会拉高
  超时/截断概率, 本 DEB 的修尺子项是其灰度前置候选);
  DEB-20260725-llm-cache-cross-symbol-pollution (缓存兜底=失败链末节+审计盲区同源)。
- **宪章合规**: 一事一 DEB (失败语义独立于 thinking 裁决); 全部数字标注来源窗口;
  修补属"修尺子"白名单, 不改任何策略行为。

---

## 一、证据链 (全部含锚点, 可复核)

### E1. 单次调用层 — 有重试有降级, 但重试不分错误类型
标准链 (`app/llm/model_call.py`):
1. **超时**: 每模型 yaml timeout (deepseek-pro/qwen-max 300s, kimi-k2 60s,
   flash 8s, unified_config.yaml:622-664) 或调用方 override; urllib 超时抛异常,
   **无部分内容抢救** (无 streaming partial read)。
2. **重试**: `_try_single_model` 2 次尝试 (1 次重试, 1s 退避, L209-230),
   **所有异常同等对待** — timeout / empty_response / 截断 / 网络抖动 无分流。
   后果: 截断类失败用同一 max_tokens 原样重发, 大概率再截断, 浪费一次尝试。
3. **降级**: primary → fallback 各 2 次 = 单 feature 最多 4 次 HTTP (L354-365)。
4. **缓存兜底**: 6 个 cache_on_fail 特征全失败 → 24h 缓存 (L409-417)
   — 即 cache DEB 的跨标的污染源, 两 DEB 在此交汇。
5. **空输出**: content 为空 → 显式 raise `empty_response_from_*` (L168-169) → 走重试链。

### E2. 截断 — `finish_reason` 完全不检查 (观测缺口)
`_http_call` (L165-174) 只取 `choices[0].message.content`, **不读 finish_reason**。
max_tokens 截断 (finish_reason=length) 无显式检测、无审计字段、无计数。
截断 JSON 只能靠下游 parse 失败间接暴露, 与"模型本来就输出坏 JSON"不可区分。

### E3. 部分 JSON — 严格拒绝 (作者认为设计正确, 辩论方可挑战)
`json_schema_guard.parse_json_strict`: 三级抽取 (direct → fenced → 平衡括号),
**无截断修复**; 截断/残缺 JSON = parse 失败 = 视同本模型失败 → fallback。
原则 (模块 docstring): 拒绝自由文本影响仓位决策。
**唯一例外**: `agent_bridge._call_llm_direct` (L1059-1083) — parse 失败后自救
重试一次: max_tokens 砍 16000→2048 + prompt 追加"每项 ≤200 字符"
(2026-05-22 P1 修复, 根因=provider 截断 1668-2664 chars)。
这是仓内唯一的截断感知逻辑, 方向正确 (用更短输出换完整 JSON), 但未推广到标准链。

### E4. 编排层 — 部分成功接受, 三处语义不一
| 位置 | 语义 | 评价 |
|---|---|---|
| `dual_agent_debate` L63-92 | `gather(return_exceptions=True)` → 单侧失败**显式接受幸存侧** (mode=hermes_only/claude_only); 双失败→单模型降级; 超时→降级 | 清晰, 有 mode 标记可审计 ✅ |
| `hermes_worker._handle_debate` L274-278 | `future.result()` 无 per-role try → 一角色异常=整体 error; 但角色内部吞错返回空 verdict → **空意见静默混入法官 prompt**, 无非空质量门 | 两个极端: 硬失败全灭 / 软失败无感 ⚠️ |
| UE scan / 盘前批 | per-candidate except→continue 跳过, 批量部分成功是常态 | 合理, 但外部技能 per-symbol 失败处理在仓外 (盲区) |

### E5. 审计实证 — 失败率无法数据驱动
llm_audit.db 30d: 42 调用 0 失败 (7d: 39 调用)。样本太薄, 各层 (重试/降级/
缓存兜底) 触发率无统计。且 cache_on_fail 6 特征 (dr_debate 等推理最重者)
**零审计行** — 观测盲区与 cache DEB §四 同源。
外部 Hermes 盘前 DA 主链**完全无仓内审计** (thinking DEB E4 同指)。

### E6. 与 thinking 的耦合
thinking ON → 生成变长 → 超时/截断概率上升。当前 ①截断不可观测 (E2)
②重试不分流 (E1) ③截断自救只有 agent_bridge 一处 (E3) —
thinking 灰度若先于修尺子启动, 故障率上升将无可归因数据。

---

## 二、选项

### 选项 A: 维持现状
兜底链完整 (重试→降级→缓存→all_models_failed), 残缺 JSON 永不入决策。
**代价**: 截断不可观测; 截断重试空转; hermes 空 verdict 静默污染法官;
失败率永远无统计 — 且 thinking 灰度 (若裁决开启) 将在无失败基线下起跑。

### 选项 B: 修尺子三件套 (作者推荐)
1. **finish_reason 落审计**: `_http_call` 返回增加 finish_reason, `_record_audit`
   记 `result_summary` 或新字段; 截断 (length) 计数可查询。~10 行。
2. **重试按错误类型分流**: 截断类 (finish_reason=length 或 JSON parse 失败)
   → 第二次尝试降 max_tokens + 追加简洁指令 (把 agent_bridge L1059-1083 的
   自救模式上提到 `_try_single_model` 标准链); timeout/网络类维持原样重发。~15 行。
3. **hermes_worker 空 verdict 质量门**: `_handle_debate` per-role try +
   空 verdict 拒绝入法官 prompt (或标注"该角色缺席") + roles 缺席计数入响应。~10 行。
全部 record/robustness 性质, 不改策略行为, 属修尺子白名单。**风险**: 低;
注意 ②改变重试行为, 需避免把"降 max_tokens"应用到本就需要长输出的 feature
(建议仅 JSON parse 失败时启用, 非 schema 校验失败)。

### 选项 C: 接受部分 JSON (截断修复/流式抢救)
**作者反对**。残缺指令入仓位决策违反 json_schema_guard 的设计原则 (E3);
平衡括号抽取已是容忍上限。辩论方若支持, 须给出"哪些字段残缺仍可安全消费"
的白名单论证 — 作者认为不存在。

---

## 三、预注册判据 (辩论方须逐条作答)

- **Q1 优先级**: 修尺子三件套 (B) 是否应作为 thinking 灰度 (另一 DEB 选项 B)
  的**硬前置**? 还是可并行/随后? 若 thinking 裁决为不开 (选项 A), B 是否仍独立值得做?
- **Q2 重试分流的边界**: 截断重试降 max_tokens 的适用条件 — 仅 JSON parse 失败?
  还是 finish_reason=length 也触发? 降多少 (agent_bridge 用 16000→2048 一刀切,
  标准链应按 feature 比例还是固定档)? 长输出 feature (deep_target_analysis 3000)
  被降输出会不会反而制造新的截断?
- **Q3 hermes 质量门语义**: 角色缺席时法官 prompt 应"拒绝该场辩论"还是
  "标注缺席继续"? 缺席率多高应触发当日 DA 降级告警?
- **Q4 可观测验收**: finish_reason 落库后, 用什么查询/报表验收 (例:
  每日截断率 per feature)? 截断率阈值多少算异常 (作者预注册: 任何 feature
  7 日截断率 >5% 触发 max_tokens 复核)?

---

## 四、作者倾向 (供辩论方攻击, 不构成结论)

B 全做, 且作为 thinking 灰度的硬前置 (无失败基线不起跑); C 拒绝。
A 仅在辩论方证明"B 的改动本身会污染挑战者 A/B 等进行中实验"时临时接受,
但须限期补做 (最迟 thinking 灰度启动前)。

---

## 五、辩论方作答 (Qoder-主工程会话, 2026-07-25, 锁点独立复核)

**事实链复核**: E2 ✓ (`finish_reason` 全文件 grep 0 引用); E1 ✓ (L209-232 重试块无任何错误分型); E4-hermes ✓ (L270 `result.get("final_response","")` 空串直接 append 进 analysts, 无非空门)。事实链无误。

- **Q1 (优先级)**: B 应为 thinking 灰度**硬前置** — 同意作者。理由量化: thinking ON 后截断率若上升, 无 finish_reason 落库则与"模型本就坏 JSON"不可区分, 灰度判据 Q1 (JSON 一次合格率) 直接失真。且 **B 独立价值成立**: 即使 thinking 裁 A 不开, 当前审计库生产行仅 59 行 (另 57% 是 test 污染, 见 cache DEB 辩论节) — 失败可观测性本身就是宣章规则 7 的欠账。
- **Q2 (重试分流边界)**: 建议**仅 JSON parse 失败 且 finish_reason=length 同时成立**时降 tokens 重发 — 只有 parse 失败一个信号时可能是模型能力问题 (降 tokens 无益), 只有 length 一个信号时输出可能本就完整可解析。降幅建议**比例制 ×0.5 + 下限 1024** (非 agent_bridge 式一刀切 2048 — deep_target_analysis 3000×0.5=1500 仍够结构化输出, 而一刀切到 2048 对 800 档 feature 反而是升)。作者担忧的"降后新截断"由下限+只重试一次天然封顶。
- **Q3 (hermes 质量门语义)**: **标注缺席继续** (非拒绝整场) — 与 dual_agent_debate 的 hermes_only/claude_only 降级语义对齐 (E4 第一行已被评为✅的模式)。缺席告警阈候选: 单场辩论缺席角色≥2 或 当日缺席率>30% → 当日 DA 降级告警 (n 小, 阈值由落地后首周数据校准)。
- **Q4 (验收查询)**: 复用现有 `daily_llm_token_report.py` 加一列 (已有 cron 06:20), 每 feature 日截断率; 作者预注册的 "7日截断率>5% 触发 max_tokens 复核" 接受, 追加下限条件 n≥10 (低频 feature 免误报)。
- **攻击点 (一个)**: 三件套中②改变重试行为, 严格说不是纯"修尺子" (重发 payload 变了) — 但影响面仅"本就失败的调用"的第二次尝试, 成功路径零变更, 可接受; 建议②加 feature flag (`llm.retry_downshift_enabled`, 默认 true) 保一键回滚。
- **结论: 支持 B 全做** (①③纯记录性直接做; ②加 flag), 作为 thinking 灰度硬前置; C 同拒 (无安全残缺字段白名单存在)。

(待用户裁决)

**2026-07-25 用户裁决: 选项 B 三件套通过 ("无争议的完成更新")。已落地** (按辩论方修正版):
1. ① finish_reason 落审计: `_http_call` 透传 + `CallResult.finish_reason` 字段 + `_record_audit` 写 error_msg (仅非 stop 时, 不动 audit schema)。冒烟: 真调用 finish='stop' 全链透传 ✓。
2. ② 截断降档自救: call_llm schema 失败处, **双信号** (parse 失败 且 finish_reason=length) 触发同模型降 tokens 重试 (比例制 ×0.5 下限 1024 + 精简指令追加); flag `llm.retry_downshift_enabled` 默认 true, 回滚一键。
3. ③ hermes 空 verdict 质量门: per-role try 兑底 (单角色异常不再全灭) + 空意见不入法官 prompt + 缺席显式标注 + `absent_roles` 入响应体; 全缺席→显式 error。
验证: py_compile ✓ / CallResult 字段冒烟 ✓ / news_classifier 真调用回归 ✓。状态: ✅ 已裁决已执行。Q4 验收报表 (token report 加截断率列) 随首周数据补。

---

*本 DEB 仅含事实与选项, 不含任何代码变更。裁决前不动 model_call.py / hermes_worker.py。*
