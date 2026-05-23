## Round 2: 交叉质疑

阅读以上各模型的 Round 1 输出，进行交叉质疑。输出 JSON:

```json
{
  "challenges": [
    {
      "target_model": "被质疑的模型名",
      "target_claim": "被质疑的具体论点",
      "counter_evidence": "你的反驳证据",
      "risk_if_wrong": "如果对方错了的后果"
    }
  ],
  "missing_dimensions": ["其他模型忽略的分析维度"],
  "strongest_argument": "你认为最强的论点 (可以是其他模型的)",
  "weakest_argument": "你认为最弱的论点"
}
```

要求:
- 找出对方分析中的逻辑漏洞
- 用数据反驳对方的建议
- 提出"如果对方错了"的后果分析
- 指出对方忽略了哪个维度
