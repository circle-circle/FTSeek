## 深度收敛轮

前轮未达成共识，请深入分析分歧点。输出 JSON:

```json
{
  "revisited_disagreements": [
    {
      "statement": "分歧点",
      "new_evidence": "本轮新增的分析",
      "updated_position": "你更新后的立场",
      "concession": "你是否做出了让步"
    }
  ],
  "consensus": ["本轮新达成的共识"],
  "remaining_disagreements": ["仍无法达成共识的点"],
  "action_items": [
    {"action": "具体行动", "target": "注入目标", "params": {}, "priority": "P0"}
  ],
  "recommendation": "最终建议",
  "confidence": 0.7
}
```

要求:
- 重新审视分歧点，是否有可能让步
- 如果仍无法共识，建议 shadow test 方案
- 聚焦可执行的行动项
