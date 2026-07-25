# DEB-20260725: LLM 缓存跨标的污染 — 修复方案待辩论

> **状态**: 🔶 待辩论 (作者=Qoder 起案+证据链 → 待辩论方证伪 → 用户裁决后才动手)
> **分类**: 修尺子候选 (数据正确性修复; 不改任何 prompt/信号逻辑/配置)
> **纪律声明**: 本 DEB 只起案, 零代码改动; 裁决后按选定方案单独执行并落变更日志。

---

## 一、问题 (一句话)

`app/llm/model_call.py` 的 `cache_on_fail` 缓存键**只有 feature 名、不区分输入内容** ——
A 标的分析成功后缓存被覆写, B 标的调用全模型失败时, 会把 **A 标的的分析当作 B 标的的结果返回**
(`success=True, from_cache=True`), 下游无任何防护。

## 二、证据链 (全部独立实证, 非引用二手结论)

| # | 证据 | 锚点 |
|---|---|---|
| E1 | 缓存路径 = `data/llm_cache/{feature}.json`, 单文件 | `model_call.py:239-245` |
| E2 | 每次成功即覆写 `{response, saved_at}` (只留最后一次成功) | `model_call.py:248-256, 404-405` |
| E3 | 全失败时 `_load_cache(feature, 24h)` 直接返回, 不校验输入 | `model_call.py:261-274, 409-417` |
| E4 | `cache_on_fail: true` 的 feature 共 6 个: daily_review / deep_target_analysis / dr_debate / playbook_generator / premarket_planner / web_search_fundamental | `unified_config.yaml:521,537,543,574,584,609` |
| E5 | **最小复现** (合成, 已跑通): NVDA 结果存入后, 模拟 TSLA 调用取缓存 → 返回 `{'symbol':'NVDA','verdict':'LONG'}` | 命令见 §七 |
| E6 | 下游唯一"防护" = `_meta.from_cache` 记录, 无消费端检查拒绝 | `deep_analysis_engine.py:612` (grep 全仓仅此一处 from_cache 引用) |
| E7 | **历史零命中**: `data/llm_audit.db` llm_calls 全表无 `model='cache'` 记录 (136 行全查) | §七 命令 3 |

## 三、影响面评估 (诚实定性: 今日无 live 交易暴露)

逐 feature 追消费端 (grep 全仓 call_llm 调用点):

| feature | 消费端 | 暴露面 |
|---|---|---|
| deep_target_analysis | `deep_analysis_engine.py:476` (dashboard 按需) + `portfolio_synth.py:301` (plan 层, 07-11 冻结) | **dashboard 研究报告**, 人在环 |
| dr_debate | `deep_research_pipeline.py:93` (dashboard 按需) + `portfolio_synth.py:302` | 同上 |
| premarket_planner / playbook_generator / daily_review / web_search_fundamental | **未找到现存 call_llm 调用点** (yaml 路由悬置/已迁移) | 疑似死路由, 待辩论方复核 |

- **live 交易链路不经过这些 feature**: DA 主链 = 工作区外 Hermes skill (自有缓存体系);
  UE = `universe_expansion_reasoning` **无 cache_on_fail** (默认 false); playbook 执行链不调用上述 feature。
- **当前实害**: dashboard 深度报告在"双模型同时故障"时可能张冠李戴 (NVDA 的多空论点标注为 TSLA) — 误导人工研判, 不自动交易。
- **升级路径 (为何不能拖)**: ① 这些 feature 任一被重新接入决策/复盘回灌 (review_loop/lesson_injector 类), 污染就从"报告"变"信号"; ② 故障模式恰好发生在 API 不稳定时 — 也是最不该拿错数据的时候。

## 四、附带发现的观测缺口 (独立问题, 顺带记录)

cache 文件 (`deep_target_analysis.json`/`dr_debate.json`, 07-24 10:36 写入) 证明这些 feature **真实被调用过**,
但 `llm_audit.db` 中这两个 feature 的 audit 行 = **0** (全表 136 行无一行)。→ `_record_audit` 对这些调用未覆盖
(疑 dashboard 进程写不同 DB 路径或静默失败)。**宪章规则 7 (遥测实测>文档自述) 的又一个缺口**, 建议与本案同修或另立 DEB。

## 五、修复方案 (三选, 附取舍)

### 方案 A: 缓存键加调用方标识 (feature + cache_key)
- `call_llm(..., cache_key: str | None = None)`; 缓存文件 `{feature}_{cache_key}.json`;
  调用方 (deep_analysis_engine / deep_research_pipeline / portfolio_synth) 传 `cache_key=symbol`。
- 全失败时只命中**同 feature 同标的**的 24h 缓存 — 语义恢复正确 ("同输入瞬时故障复用")。
- 取舍: 改动面 = model_call 3 函数签名 + ~3 调用点; 保留了可用性兜底。
- 注意: 不能只用 `prompt_hash()` 作键 — user prompt 含 current_price/fetched_at, 每次调用哈希都不同 → 等效方案 B。

### 方案 B: 全失败直接返回 None (删除缓存兜底)
- 一行级改动 (`cache_on_fail` 全置 false 或删 409-417 分支)。
- 依据: **E7 历史零命中** → 可用性损失≈0; 这些 feature 全是 on-demand, 人重试即可。
- 取舍: 改动最小、消毒最彻底; 失去"瞬时 API 故障自动复用"的 (从未兑现过的) 理论收益。

### 方案 C: 维持现状 + 消费端检查 from_cache 拒绝
- 把防护责任分散到 N 个消费端, 每个新增消费端都必须记得检查 — 脆弱, 不推荐。

**作者推荐**: **方案 B** (零命中实锤使 A 的保留价值≈0; B 一行回滚也最简单)。
若辩论方证明缓存兜底有未观测到的价值 (如 dashboard 批量深析 51 只时令故障可容忍), 则退而取 A。

## 六、预注册裁决判据 (辩论方必须回答, 数据判不投票)

1. **Q1 反证 live 暴露**: 是否存在我 grep 未覆盖的 live 消费端 (变量传 feature 名/间接封装)? — 若存在, 严重度从"中"升"高", 方案不变但优先级提前。
2. **Q2 死路由确认**: premarket_planner / playbook_generator / daily_review / web_search_fundamental 四个 yaml 路由是否确无调用点? 若是, 建议顺带按"做减法"流程停路由 (不占本案改动)。
3. **Q3 缓存价值反证**: 除 audit DB 外, 是否有其他证据表明 cache_on_fail 曾兑现过可用性价值 (如日志 "使用 24h 缓存" 命中)? — 可用 `grep -rn "使用 24h 缓存" logs/ | head` 取证。若有实质命中, 方案 B 失去依据, 转方案 A。
4. **Q4 观测缺口归因**: §四 audit 零记录的原因 (另进程/另DB/静默失败)? — 影响是否另立 DEB。

## 七、可复现命令

```bash
# E5 最小复现 (合成演示, 无污染)
./venv/bin/python -c "
from app.llm.model_call import _save_cache, _load_cache
_save_cache('repro_feature_xyz', {'symbol': 'NVDA', 'verdict': 'LONG'})
print(_load_cache('repro_feature_xyz'))   # 返回 NVDA 分析, 与调用方 prompt 无关
import pathlib; pathlib.Path('data/llm_cache/repro_feature_xyz.json').unlink()"

# E7 历史命中
./venv/bin/python -c "
import sqlite3
c = sqlite3.connect('file:data/llm_audit.db?mode=ro', uri=True)
print(c.execute(\"SELECT feature, COUNT(*) FROM llm_calls WHERE model='cache'\").fetchall())"

# Q3 日志取证
grep -rn "使用 24h 缓存" logs/ 2>/dev/null | head -5
```

## 八、状态流转记录

- 2026-07-25 Qoder 起案 (作者): 证据 E1-E7 + 三方案 + 预注册判据 Q1-Q4。源出 DA 链路审查 (探索代理报告 §三.1), 全部经本人独立复核后采信。
- **2026-07-25 辩论方 (Qoder-主工程会话) 逐 Q 实证作答 (全部独立重跑取证, 非引用)**:
  - **E5/E7 复现 ✓**: 合成缓存跨输入返回 NVDA 实锤; audit `model='cache'` = 0/136。
  - **Q1 (隐蔽消费端): 未发现**。grep 变量传 feature (`call_llm(feature=变量`) 全仓 0 命中 — 不存在绕过字面 grep 的活路径。严重度维持"中"。
  - **Q2 (死路由): 成立**。四 feature 的 grep 命中全部为注释 (model_call.py:12 docstring / deep_target_analysis.py:117 旧版注释 / deep_analysis_engine.py:6 docstring), 真调用点 = 0。支持顺带"做减法"停路由 (另单子, 不占本案)。
  - **Q3 (缓存价值反证): 无**。logs/ 全量 grep "24h 缓存"/from_cache 零命中 — cache_on_fail 从未兑现过可用性价值, **方案 B 依据成立**。
  - **Q4 (审计缺口归因): 方向确认 + 追加发现**。audit 库 feature 分布仅 6 种 (agent_bridge/news_classifier/llm_strategy_engine/thesis_inferer/universe_expansion_reasoning/test), 无 deep_target_analysis/dr_debate 任何行 — 而 cache 文件存在证明被调过 → dashboard 进程调用确实未入审计, 建议另立观测缺口案。**追加: audit 库 136 行中 77 行 (57%) 是 `test_feature_xyz` 测试污染** — 真实生产行仅 59, "样本薄"的另一半原因; 建议观测缺口案附带清理测试行+测试写入改 :memory:。
  - **攻击点 (唯一)**: 方案 B 的"人重试即可"前提是 dashboard 有明确失败提示 — 若删兑底后全失败路径返回 None 而前端静默, 用户体验从"错数据"变"无反馈", 后者不一定更好。要求: 方案 B 落地时附带确认失败路径有用户可见错误提示 (不新增机制, 只验证现有 error 展示)。
  - **辩论方结论: 支持方案 B** (零命中实锤 + 周末零风险窗口 + 一行回滚), 附上述失败提示验证条件。无法证伪主张。
- (待用户裁决)
- **2026-07-25 用户裁决: 方案 B 通过 ("无争议的完成更新")。已落地**: yaml 6 处 `cache_on_fail: true→false` (行内注释标源, 回滚=改回 true); 附带条件验证 ✓ — dashboard 失败路径 `deep_analysis_engine.py:489-494` 有显式 WARNING+return None 非静默。状态: ✅ 已裁决已执行。Q4 观测缺口 (audit 盲区+test 污染清理) 另立案待排。
