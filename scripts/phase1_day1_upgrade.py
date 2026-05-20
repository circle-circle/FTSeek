#!/usr/bin/env python3
"""
Phase 1 Day 1 改造脚本 - 升级盘前/盘后核心逻辑

此脚本展示如何直接修改现有代码，不创建适配层。

使用方法:
1. 阅读此脚本了解改造逻辑
2. 手动应用patch到现有代码
3. 验证改造后的功能
"""

import os
import sys

# 添加项目路径
sys.path.insert(0, "/opt/ftseek/FTSeek-US")
sys.path.insert(0, "/opt/ftseek/FTSeek-US/app")

print("=" * 60)
print("Phase 1 Day 1 改造指南")
print("=" * 60)

print("""
## 改造1: premarket_planner.py

### 原代码 (简化)
```python
async def run(self):
    # 收集数据
    data = await self.collect_data()
    
    # 单模型调用
    plan = await self._call_llm(data)
    
    # 保存计划
    self.save_plan(plan)
```

### 改造后
```python
async def run(self):
    # 收集数据
    data = await self.collect_data()
    
    # === 新增：双Agent辩论 ===
    from app.llm.dual_agent_debate import DualAgentDebate
    
    debate = DualAgentDebate(self.agent_bridge, config={
        "timeout": 300,
        "fallback_model": "qwen-max"
    })
    
    # 读取历史lessons
    lessons = self.lesson_injector.get_lessons_for_date(self.date)
    
    # 运行辩论
    debate_result = await debate.run_debate(
        prompt=self.build_prompt(data),
        system_prompt=self.SYSTEM_PROMPT,
        lessons=lessons,
        round_num=1
    )
    
    # 使用收敛结果
    plan = debate_result["converged_result"]
    
    # 保存计划
    self.save_plan(plan)
```

## 改造2: eod_attribution.py

### 原代码 (简化)
```python
def _merge_results(self, hermes: dict, claude: dict) -> Dict:
    import copy
    if hermes and not claude:
        return copy.deepcopy(hermes)
    if claude and not hermes:
        return copy.deepcopy(claude)
    
    merged = copy.deepcopy(hermes)
    merged["systemic_insights"]["claude_findings"] = claude.get("systemic_findings", [])
    return merged
```

### 改造后
```python
def _merge_results(self, hermes: dict, claude: dict) -> Dict:
    from app.llm.dual_agent_debate import DualAgentDebate
    
    # 使用结构化合并
    return DualAgentDebate.merge_eod_results(hermes, claude)
```

## 验证步骤

1. 语法检查:
   ```bash
   python3 -m py_compile /opt/ftseek/FTSeek-US/app/llm/dual_agent_debate.py
   ```

2. 单元测试:
   ```bash
   cd /opt/ftseek/FTSeek-US
   python3 -m pytest tests/ -k "debate" -v
   ```

3. 集成测试:
   ```bash
   # 模拟运行盘前计划
   python3 scripts/run_premarket.sh --test
   ```

## 回退方案

如果改造后出现问题，可以快速回退：

```bash
# 备份现有文件
cp app/us_pipeline/premarket_planner.py app/us_pipeline/premarket_planner.py.bak
cp app/us_pipeline/eod_attribution.py app/us_pipeline/eod_attribution.py.bak

# 回退
mv app/us_pipeline/premarket_planner.py.bak app/us_pipeline/premarket_planner.py
mv app/us_pipeline/eod_attribution.py.bak app/us_pipeline/eod_attribution.py
```
""")

print("\n" + "=" * 60)
print("改造完成指南已输出")
print("=" * 60)
