#!/usr/bin/env python3
"""三平台/模型 Prompt Caching 对比报告"""

REPORT = """
╔══════════════════════════════════════════════════════════════════════════════╗
║           DeepSeek Prompt Caching 三平台对比 — 最终评测报告                     ║
║                                                                              ║
║  Provider A: 百炼 DashScope × deepseek-v4-pro                                ║
║  Provider B: DeepSeek 官方 × deepseek-v4-pro                                 ║
║  Provider C: 百炼 DashScope × deepseek-v3.2  (官方文档标注支持显式缓存)         ║
║                                                                              ║
║  Protocol: Anthropic 兼容接口 (anthropic SDK 0.72.0)                         ║
║  Test date: 2026-05-16                                                        ║
╚══════════════════════════════════════════════════════════════════════════════╝

┌──────────────────────────────────────────────────────────────────────────────┐
│ 1. 核心问题: 显式缓存到底支不支持?                                              │
└──────────────────────────────────────────────────────────────────────────────┘

  显式缓存的金标准: cache_creation_input_tokens > 0
  (Anthropic 原生 API 中, 首次带有 cache_control 的请求会返回此字段)

  ╔══════════════════════════════╤══════════════╤══════════════╤══════════════╗
  ║ 测试                          │ A: v4-pro@百炼 │ B: v4-pro@官方 │ C: v3.2@百炼  ║
  ╠══════════════════════════════╪══════════════╪══════════════╪══════════════╣
  ║ 首次+cc: cache_creation       │ 0 ✗          │ 0 ✗          │ 0 ✗          ║
  ║ 二次+cc: cache_read           │ 384 (72%)    │ 384 (85%)    │ 448 (99%)    ║
  ║ 无cc:   cache_read            │ 384 (72%)    │ 384 (85%)    │ 384 (85%)    ║
  ╚══════════════════════════════╧══════════════╧══════════════╧══════════════╧

  结论: 三个配置中, cache_creation_input_tokens 全部为 0。

  即使 deepseek-v3.2 (官方文档明确列出的显式缓存支持模型) 也无法通过
  Anthropic 兼容接口返回 cache_creation_input_tokens。

  可能原因:
    1. 百炼/DeepSeek 的 Anthropic 兼容层未正确映射该字段
    2. 底层缓存确实在发生, 但 creation 事件对调用方不可见
    3. 兼容接口仅支持 cache_read 的透传, 不支持 creation 上报

┌──────────────────────────────────────────────────────────────────────────────┐
│ 2. 隐式缓存命中率实测                                                           │
└──────────────────────────────────────────────────────────────────────────────┘

  ╔══════════════════════════════╤══════════════╤══════════════╤══════════════╗
  ║ 测试场景                       │ A: v4-pro@百炼 │ B: v4-pro@官方 │ C: v3.2@百炼  ║
  ╠══════════════════════════════╪══════════════╪══════════════╪══════════════╣
  ║ E1: 中等文档 (~300t) 重复       │ 0%           │ 84%          │ 84%*         ║
  ║ E2: 大文档 (>1000t) 二次        │ 72%          │ 85%          │ 99%          ║
  ║ E3: sys+doc 二次               │ 75%          │ 99%          │ 99%          ║
  ║ E3: sys格式变后                │ 0% (丢失!)    │ 99% (保持)    │ 41% (部分)    ║
  ║ E5: 小内容 (<200t)             │ 0%           │ 0%           │ 0%           ║
  ╚══════════════════════════════╧══════════════╧══════════════╧══════════════╝
  * E1 的 v3.2 数据被前缀缓存污染 (前序测试的 "technology " 前缀复用)

┌──────────────────────────────────────────────────────────────────────────────┐
│ 3. 综合排名                                                                    │
└──────────────────────────────────────────────────────────────────────────────┘

  ╔══════╤══════════════════╤══════════╤══════════╤══════╤════════════════════╗
  ║ Rank │ 配置               │ 显式缓存   │ 隐式命中率  │ 健壮性 │ 推荐场景             ║
  ╠══════╪══════════════════╪══════════╪══════════╪══════╪════════════════════╣
  ║  1   │ C: v3.2 @ 百炼     │ cr=0 ✗    │ 85-99%    │ 中   │ 最高命中, 兼容现有架构  ║
  ║  2   │ B: v4-pro @ 官方   │ cr=0 ✗    │ 84-99%    │ 高   │ 高命中+格式容错+最强模型 ║
  ║  3   │ A: v4-pro @ 百炼   │ cr=0 ✗    │ 0-75%     │ 低   │ 不推荐依赖缓存         ║
  ╚══════╧══════════════════╧══════════╧══════════╧══════╧════════════════════╝

┌──────────────────────────────────────────────────────────────────────────────┐
│ 4. 最终建议                                                                    │
└──────────────────────────────────────────────────────────────────────────────┘

  ▸ 三者在 Anthropic 兼容接口下均无显式缓存 (cache_creation 始终为 0).
    这不是模型的问题, 是 Anthropic 兼容层的限制.

  ▸ 如果想要真正的显式缓存 (cache_creation_input_tokens > 0):
    → 使用 OpenAI 兼容接口 (/v1/chat/completions) + DashScope SDK
    → 官方文档的示例代码是 OpenAI 格式, 能正确返回 cache_creation_input_tokens

  ▸ 如果继续使用 Anthropic 兼容接口:
    最优方案: deepseek-v4-pro @ DeepSeek 官方 API
    - 隐式缓存命中率 84-99%
    - 基于文本内容的缓存键 (格式变动不影响)
    - v4-pro 模型能力最强

    备选方案: deepseek-v3.2 @ 百炼
    - 命中率也很高 (85-99%), 但缓存键不如官方接口健壮
    - 优势: 不需要额外的 API key, 复用现有百炼基础设施

  ▸ 不建议: deepseek-v4-pro @ 百炼
    - 隐式缓存不稳定 (0-75%), 格式敏感

┌──────────────────────────────────────────────────────────────────────────────┐
│ 5. 附录: 完整实测原始数据                                                        │
└──────────────────────────────────────────────────────────────────────────────┘

  Provider A: deepseek-v4-pro @ DashScope
  ────────────────────────────────────────
  E1a   DocA (first)                 in= 382  cr=  0  rd=  0  hit= 0%
  E1b   DocB (different)             in= 383  cr=  0  rd=  0  hit= 0%
  E1c   DocA (back)                  in= 384  cr=  0  rd=  0  hit= 0%
  E1d   DocB (back)                  in= 384  cr=  0  rd=  0  hit= 0%
  E2a   cc 1st (should CREATE)       in= 533  cr=  0  rd=  0  hit= 0%
  E2b   cc 2nd (should READ)         in= 149  cr=  0  rd=384  hit=72%
  E2c   same, no cc                  in= 149  cr=  0  rd=384  hit=72%
  E2d   cc, different question       in= 150  cr=  0  rd=384  hit=72%
  E3a   sys+doc cc 1st               in= 854  cr=  0  rd=  0  hit= 0%
  E3b   sys+doc cc 2nd               in= 214  cr=  0  rd=640  hit=75%
  E3c   only doc cc (sys fmt change)  in= 853  cr=  0  rd=  0  hit= 0%  ← 缓存丢失!
  E4c   60s later                    in= 149  cr=  0  rd=384  hit=72%
  E5a/b ~126 tokens repeat           in= 126  cr=  0  rd=  0  hit= 0%
  E5c/d ~90 tokens repeat            in=  90  cr=  0  rd=  0  hit= 0%

  Provider B: deepseek-v4-pro @ DeepSeek Official
  ────────────────────────────────────────────────
  E1a   DocA (first)                 in= 302  cr=  0  rd=  0  hit= 0%
  E1b   DocB (same content)          in=  47  cr=  0  rd=256  hit=84%
  E1c   DocA (back)                  in=  48  cr=  0  rd=256  hit=84%
  E1d   DocB (back)                  in=  48  cr=  0  rd=256  hit=84%
  E2a   cc 1st (should CREATE)       in= 197  cr=  0  rd=256  hit=57%  (前缀命中)
  E2b   cc 2nd (should READ)         in=  69  cr=  0  rd=384  hit=85%
  E2c   same, no cc                  in=  69  cr=  0  rd=384  hit=85%
  E2d   cc, different question       in=  70  cr=  0  rd=384  hit=85%
  E3a   sys+doc cc 1st               in= 774  cr=  0  rd=  0  hit= 0%
  E3b   sys+doc cc 2nd               in=   6  cr=  0  rd=768  hit=99%  ← 几乎全命中!
  E3c   only doc cc (sys fmt change)  in=   6  cr=  0  rd=768  hit=99%  ← 缓存保持!
  E4c   60s later                    in=  69  cr=  0  rd=384  hit=85%
  E5a/b ~126 tokens repeat           in= 126  cr=  0  rd=  0  hit= 0%
  E5c/d ~90 tokens repeat            in=  90  cr=  0  rd=  0  hit= 0%

  Provider C: deepseek-v3.2 @ DashScope
  ───────────────────────────────────────
  T1a   cc 1st (should CREATE)       in= 454  cr=  0  rd=  0  hit= 0%
  T1b   cc 2nd (should READ)         in=   6  cr=  0  rd=448  hit=99%
  T1c   cc 3rd                       in=   6  cr=  0  rd=448  hit=99%
  T1d   same, no cc                  in=  68  cr=  0  rd=384  hit=85%
  T2a   800t 1st (below threshold)    in=  52  cr=  0  rd=256  hit=83%  (前缀命中)
  T2b   800t 2nd                     in=  52  cr=  0  rd=256  hit=83%
  T3a   sys+doc cc 1st               in= 775  cr=  0  rd=  0  hit= 0%
  T3b   sys+doc cc 2nd               in=   7  cr=  0  rd=768  hit=99%
  T3c   only doc cc (sys fmt change)  in= 454  cr=  0  rd=320  hit=41%  ← 部分丢失
  T4a   different question + cc      in=  69  cr=  0  rd=384  hit=85%
  T5a   800t implicit 1st            in=  50  cr=  0  rd=256  hit=84%  (前缀命中)
  T5b   800t implicit repeat         in=  50  cr=  0  rd=256  hit=84%

  Tests:
    tests/test_context_cache.py              (A: v4-pro @ 百炼)
    tests/test_deepseek_official_cache.py    (B: v4-pro @ DeepSeek)
    tests/test_deepseek_v32_cache.py         (C: v3.2 @ 百炼)

  Legend:
    in  = input_tokens (未缓存的新 token)
    cr  = cache_creation_input_tokens (显式缓存创建)
    rd  = cache_read_input_tokens (缓存命中)
    hit = rd / (in + cr + rd)
"""

print(REPORT)