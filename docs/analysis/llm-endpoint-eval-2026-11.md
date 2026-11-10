---
title: 百炼三接入端点对比评测(直连 / Token Plan / Workspace 网关)
status: active
module: cross
related_code: app/llm/model_call.py
tags: [llm, dashscope, eval, architecture]
updated: 2026-11-09
---

# 百炼三接入端点评测报告

> 实测日期: 2026-11-09。方法: 同一问题、同一模型(deepseek-v4.1-flash)、真实 HTTP 调用对照。
> 测试模型: deepseek-v4.1-flash(主测)、qwen-max / qwen-plus / kimi-k2.5 / MiniMax-M2.5 / glm-5 / qwen3.7-max(可用性矩阵)。

## 1. 三端定义

| 端点 | Base URL | Key 形态 | 计费 |
|---|---|---|---|
| A 直连(旧端点,项目现用) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `sk-` | 按量付费 |
| B Token Plan(订阅套餐) | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `sk-sp-` | Credits 额度池扣减 |
| C Workspace 网关(新控制台) | `https://llm-<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `sk-` | 按量付费(与 A 同账户同账单) |

## 2. 鉴权矩阵(实测)

| Key \ 端点 | A | B | C |
|---|---|---|---|
| `sk-`(BAILIAN_API_KEY) | ✅ | ❌ 401 | ✅ |
| `sk-sp-`(Token Plan) | ❌ 401 | ✅ | ❌ 401 |

结论: A 与 C 是同一计费平面的两个入口(同一套 `sk-` key 通用);B 是严格隔离的独立平面。

## 3. 模型可用性(实测)

| 模型 | A | B | C |
|---|---|---|---|
| deepseek-v4.1-flash | ✅ | ✅ | ✅ |
| qwen-max(项目 BAILIAN_MODEL) | ✅ | ❌ 404 Model not exist | ✅ |
| qwen-plus | ✅ | ❌ 404 | ✅ |
| qwen3-max | ✅* | ❌ 404 | — |
| deepseek-v4.1 | — | ❌ 404 | — |
| kimi-k2.5 | — | ✅ | — |
| MiniMax-M2.5 | — | ✅ | — |
| glm-5 | — | ✅ | — |
| qwen3.7-max | — | ✅ | — |

(* 未单独验证 A/C, 标注 — 的未测。)

**B(Token Plan)是策展白名单,不含 qwen-max/qwen-plus —— FTSeek-US 当前主模型 qwen-max 在 B 端不可用,项目现阶段无法整体切 Token Plan。**

## 4. Token 计数与协议一致性(A vs B vs C)

- 同一问题 R1: 三端 `prompt_tokens` 全部 = 40,分词器/计数逻辑完全一致。
- 多轮: prompt 按全量历史累计,三端规则相同。
- usage 字段结构完全一致(均含 `prompt_tokens_details.cached_tokens`、`completion_tokens_details.reasoning_tokens`)。
- 响应顶层字段一致;B 侧 `choices[0]` 多一个 `logprobs: null`(无影响)。
- 流式(SSE): 三端均正常。

## 5. 延迟(deepseek-v4.1-flash,单轮 5 次)

| 端点 | min | 中位 | max |
|---|---|---|---|
| A | 1.18s | 1.35s | 1.59s |
| B | 1.21s | 1.41s | 1.86s |
| C | 1.07s | 1.50s | 1.57s |

三端延迟同一量级,无统计意义差异;差异主要来自生成内容长度(采样随机性)。

## 6. Prompt Cache 实测(4.2k token system + cache_control)

- A: 第1发 cached=0,第2发 cached=4210/4220(99.8% 命中)✅
- C: 第1发即 cached=4210 —— A/C 共享后端缓存池。
- 2026-11-09 统一 deepseek-v4.1-flash 复测(3.8k system): A [0→3840],B 和 C 第1发均直接命中 3840 —— **三端(含 Token Plan)共享同一账户级缓存池**,进一步证实三端背后是同一推理服务,只是入口/计费通道不同。

## 6.1 统一模型复测: deepseek-v4.1-flash 三端全量对照(2026-11-09)

三轮连续对话(Q1 动量因子 → Q2 与反转因子区别 → Q3 具体计算例子):

| 端点 | R1 (p/c/t) | R2 (p/c/t) | R3 (p/c/t) | JSON模式 | 流式 | 延迟min/med/max |
|---|---|---|---|---|---|---|
| A 直连 | 40/93/133 | 102/158/260 | 176/330/506 | ✅ | ✅ | 1.43/1.55/1.76s |
| B Token Plan | 40/138/178 | 89/151/240 | 147/570/717 | ✅ | ✅ | 1.41/1.50/1.77s |
| C 网关 | 40/153/193 | 98/191/289 | 158/474/632 | ✅ | ✅ | 1.11/1.57/1.82s |

- R1 三端 prompt 均为 40(输入计数完全一致);R2/R3 的 prompt 差异来自各端生成的历史回复长度不同(采样随机性),非计数差异 —— 此前交叉历史测试(同一历史文本喂两端)已证明 prompt 计数逐 token 相等。
- 三端均返回 `reasoning_tokens` 明细,deepseek-v4.1-flash 推理 token 占比高(45%~81%),是模型特性,与端点无关。
- JSON mode、流式 SSE、prompt cache 三端全部可用。
- 延迟三端同一量级(中位 1.50~1.57s)。

## 7. ⚠️ 代码风险: model_call.py 的 URL 字符串硬编码门控

`app/llm/model_call.py` 有两处行为依赖 base_url 里的字面量 `"dashscope.aliyuncs.com"`:

1. **L128** prompt_cache 前缀缓存仅在匹配时启用 → 切到 B 或 C(`*.maas.aliyuncs.com`)后**前缀缓存静默失效**,成本上升。
2. **L159** `enable_thinking=False` 仅在匹配时注入 → 切到 B/C 后 glm-5.2 / qwen3.7-max / deepseek-v4-pro 等推理模型**回到默认 thinking 开启**,2026-07-06 修过的 P0(empty_response 根因: reasoning 吃光 max_tokens)会**静默回归**,且速度慢 10 倍、reasoning token 白烧钱。

切换任何端点前,必须把这两处改为配置驱动(如 yaml 里 `provider: dashscope` 标志位),不能用 URL 子串判断。

## 8. 实际任务效果对照(deepseek-v4.1-flash, 每任务×3次, temperature=0)

| 任务 | A 直连 | B Token Plan | C 网关 | 评分方式 |
|---|---|---|---|---|
| 代码生成(双排序数组中位数 O(log)) | 3/3 | 3/3 | 3/3 | 提取代码本地 exec + 4 组断言 |
| 数学推理(三管水池, 答案=3h) | 3/3 | 3/3 | 3/3 | 精确匹配 |
| 长上下文 needle(3万tok, 70%处) | 3/3 | 3/3 | 3/3 | 检索"北极星-4827-金库" |
| Function calling(get_price) | 3/3 | 3/3 | 3/3 | 校验 tool_calls 结构 |
| 严格 JSON 输出 | 3/3 | 3/3 | 3/3 | json.loads + 字段断言 |
| 延迟中位数 | 1.5s | 1.4s | 1.4s | |

**15/15 全对 × 3 端: 输出质量、agent 能力(function calling)、长上下文检索无任何可测差异。** 结合 §4-§7, 三端是同一推理服务的三个入口, 差异仅在计费与准入。

## 9. 客户评测数据差异分析(Token Plan vs 直连, 2026-11-09 追加)

客户四渠道 coding-agent 评测(通过题数/turns/token 消耗)中, Token Plan(10-30)对比阿里云直连(11-06):
通过率 73.9% vs 77.6%, turns 87 vs 129, input 122k vs 267k/题, 总 token 4.97M vs 7.98M。

**验证结论: 差异不来自端点能力, 端点层全部等价。**

逐项探针(全部实测):
- 上下文窗口: 两端同测至 131k tokens 均正常, 无截断差异。
- 并发: 15 并发与 40 并发×3 轮大输入压测(≈45 万 TPM), 两端 0 个 429, 延迟曲线一致。
- 长输出: max_tokens=16384, 两端均正常产出 1 万+ token, 无输出截断差异。
- token 计数/缓存: 见 §4/§6, 逐 token 一致, 缓存池共享。

数据形态判读:
- per-turn 口径两端接近(新输入 2075 vs 1408 tok/turn, cache read 59k vs 55k/turn);**总量差异几乎全部来自 turns 少了 1/3(129→87)** —— Token Plan run 的 agent 系统性提前收工。
- 通过率差 z=0.71(n≈134),**在统计噪声范围内, 不构成显著差异**。

疑似根因(按可能性):
1. **Token Plan 套餐级并发/限流**: 官方文档明确并发与档位挂钩(Lite 1-2 / Essential 2-3 / Standard 3-4 / Pro 6-8 个 Agent), 高峰期排队, 限流阈值动态且不可提升; 按量付费 deepseek-v4.1-flash TPM=120万。长时间多 agent 评测中排队/429 → harness 重试耗尽或超时 → agent 提前终止(turns↓、token↓、通过率微降)。本次短时压测(40并发/分钟级)未复现, 评测是小时级持续负载, 机制仍成立。
2. **运行日期混杂**: 四渠道分别跑于 10-19/10-26/10-30/11-06, 模型版本与题目集可能漂移, 非纯净 A/B。
3. harness 的 timeout/max_turns/retry 配置若不一致, 直接解释 turns 差异。

复现/确认建议:
- 查 Token Plan run 日志: 429(Throttling/Requests rate limit exceeded)、排队延迟、TIMEOUT 题的错误类型分布。
- 对照复跑: 同日期、同题目集、同 harness 配置, A/B 两端各跑一次; 并发数降到套餐档位建议值内。
- 若确认是限流: 升档(团队版/更高档)或评测负载走按量付费端点。

## 10. 结论与选型建议

| 维度 | A 直连 | B Token Plan | C Workspace 网关 |
|---|---|---|---|
| 模型目录 | 全量 | 白名单(缺 qwen-max/plus) | 全量 |
| 计费 | 按量 | Credits 套餐(成本可预期) | 按量(同 A) |
| 协议/usage/缓存 | 基准 | 一致 | 一致(与 A 共享缓存池) |
| 项目兼容性 | ✅ 现用 | ❌ 主模型不可用 | 需先修 §7 两处门控 |

建议:
1. **短期**: 维持 A,零风险。
2. **中期**: 若新控制台(workspace)是阿里主推方向,修掉 §7 两处 URL 硬编码门控后切 C,行为与 A 完全等价(已实测)。
3. **B(Token Plan)**: 仅当套餐模型名单补齐 qwen-max,或项目主模型迁到白名单内模型(kimi-k2.5 / glm-5 / qwen3.7-max / MiniMax-M2.5 / deepseek-v4.1-flash)后才可考虑;同样需先修 §7。可作为编码/巡检类负载的降本通道单独评估,不适合现在整体切换。
