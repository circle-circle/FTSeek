## Round 3: 收敛 — 共识与分歧

综合所有轮次的分析，识别共识和分歧。输出 JSON:

```json
{
  "consensus": [
    {"statement": "共识结论", "confidence": 0.9, "evidence": "支撑数据"}
  ],
  "disagreements": [
    {"statement": "分歧点", "side_a": "支持方观点", "side_b": "反对方观点", "resolution": "shadow_test"}
  ],
  "blind_spots": ["所有模型都未考虑的维度"],
  "action_items": [
    {"action": "具体行动", "target": "注入目标", "params": {}, "priority": "P0"}
  ],
  "final_recommendation": "最终建议 (1句话)",
  "overall_confidence": 0.75
}
```

收敛框架:
- 共识区: 所有模型同意且数据支持 → 直接执行
- 分歧区: 意见不同 → 标记为 shadow test 候选
- 盲区: 都未考虑 → 标记为研究项
