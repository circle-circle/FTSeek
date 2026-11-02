---
title: C-A挑战者Prompt离线验证归档包
status: active
module: cross
related_code: /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py
related_okr: OKR-2026-11
tags: [deep-analysis, prompt, offline-replay, evidence]
updated: 2026-10-27
---

# C-A 离线验证归档包

## 1. 范围与裁决

本包持久保存两次**彼此独立、不得混算**的隔离验证；权威解释见 `docs/debates/DEB-20261027-da-scope-and-confidence-coupling.md` §14–§15。它们不是生产影子，不是上线批准，也不是盈利证据。

| 子目录 | 对应DEB | 有效范围 | 结果 |
|---|---|---|---|
| `da_ca_replay_20261027_qoder/` | §14 | 2个历史/重建样本×baseline/C-A；4次primary挑战 | `REPLAY_LIMITED`：关键反例未触发，但输入重建与覆盖不足，不支持申请生产影子 |
| `da_ca_full_replay_20261027_qoder/` | §15 | 2个fresh Pass1样本；2次Pass1＋8次挑战；三项指定验收 | `OFFLINE_CHECK_PASS_LIMITED`：证据、JSON/重复、gate/conf在限定范围通过；不自动获得生产影子资格 |

生产 `llm_synth.py` 在实验时的SHA256为 `97367403caac2a27e53e0879d743202e474232ad780899aecd257d803f037ba1`；隔离baseline与其一致，C-A副本仅替换system/user两个反证裁决文本块，variant SHA256为 `e793c131b43dca4ad2fb156f021eb4ec6e4309f8a4e012f6bfe33eb53e0540b6`。归档的源码副本是实验输入，不代表当前生产版本，也不含其他生产WIP。

## 2. 可复核指标

### §14 初步回放

- 4次逻辑调用/4次HTTP尝试；0错误、0重试、0fallback；82,751 tokens；661,135ms。
- 单一样本：AGREE/BUY→PARTIAL_AGREE/BUY；同向PARTIAL样本保持PARTIAL_AGREE/SELL_SHORT。
- 原挑战请求未存，Pass1为文档化逆变换重建；10/08为历史输入＋当前baseline。仅两对、无重复、仅T1，不能据此判完整回放通过。
- 入口：`static_contract.json`、`capture_contract.json`、`evaluation.json`、`manifest.json`、`fixture_manifest.json`；逐臂必要响应在 `results/<label>/<arm>.json`。

### §15 fresh Pass1验收

- 10次逻辑调用/10次HTTP尝试；0错误、0重试、0fallback；161,951 tokens；907,138ms。
- 证据审查覆盖20条 `challenged_claims.counter_evidence`：13直接来源、4推导复算、2有效负证据、1有源但证明力有限；所审范围0编造。未穷举validation/missed_risks。
- 2份Pass1及8次挑战JSON契约均有效、finish=stop；各臂verdict/direction重复稳定，但challenged内容不稳定，不能宣称语义稳定。
- AMD两臂均ANALYST_SKIP/BLOCK/0.50/不可发布；CRM两臂均PARTIAL_AGREE/WARN/0.35/可发布。仅为公式重算，不是实际入库、排序、成交或收益。
- 入口：`acceptance_summary.json`、`evaluation.json`、`manual_evidence_review.json`、`fallback_contract.json`、`manifest.json`。每样本的 `pass1/` 保存fresh请求与原始应答，`captured/` 保存双臂完整请求，`challenge/` 保存每臂两次完整响应。
- T1–T4仅为stub路由验证；无T2–T4实模质量、真实HTTP故障或反向一条决定性反证对照。

## 3. 排除项与敏感信息审查

- 未复制 `__pycache__`、`*.pyc`、`.env`、生产数据库、生产日志、其他议题WIP或扩样目录。
- 归档前对两源目录扫描 API key、secret、password、authorization、webhook、access token、private key、Bearer及常见私钥形态；仅发现baseline/variant源码中的 `Authorization: Bearer {DASHSCOPE_KEY}` 变量引用，未发现密钥字面值。模型名、usage中的token计数和公开股票研究数据不是凭据。
- 未改生产Prompt、配置、DB、checkpoint、bridge或记忆，未重启。归档动作本身不触发交易行为。

## 4. 完整性与复现

先在本目录校验全部归档文件：

```bash
sha256sum -c SHA256SUMS
```

runner保留了实验时的原始绝对路径及外部skill依赖，**不是可独立运行的应用**。复现时先将两个子目录复制到其原名的空 `/tmp` 路径；若目标已存在，不得覆盖：

```bash
test ! -e /tmp/da_ca_replay_20261027_qoder && cp -a da_ca_replay_20261027_qoder /tmp/
test ! -e /tmp/da_ca_full_replay_20261027_qoder && cp -a da_ca_full_replay_20261027_qoder /tmp/
```

无需调用模型的复核：

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/ftseek/FTSeek-US/venv/bin/python -B /tmp/da_ca_replay_20261027_qoder/replay.py --capture
PYTHONDONTWRITEBYTECODE=1 /opt/ftseek/FTSeek-US/venv/bin/python -B /tmp/da_ca_full_replay_20261027_qoder/full_replay.py capture --label short_partial
PYTHONDONTWRITEBYTECODE=1 /opt/ftseek/FTSeek-US/venv/bin/python -B /tmp/da_ca_full_replay_20261027_qoder/full_replay.py capture --label long_partial
PYTHONDONTWRITEBYTECODE=1 /opt/ftseek/FTSeek-US/venv/bin/python -B /tmp/da_ca_full_replay_20261027_qoder/full_replay.py evaluate
```

上述capture使用stub，evaluate复用已保存响应；只写暂存目录。`replay.py --live`、`full_replay.py pass1` 与 `full_replay.py challenge` 会调用付费模型，**本归档不授权执行**；只有另获模型调用及成本批准后才可按原CLI参数复跑。生产源码或外部依赖hash不同须先停止并审查，不得用新依赖冒充原实验。

## 5. 后续门禁

本包只证明已保存样本、契约和审查范围内的结果。反向单反证、T2–T4实模、真实故障、完整散文事实核验及任何生产影子均须另行批准；不得由归档动作自动进入C-B、生产影子、worker/pullback并发或上线。任何收益主张仍需独立前瞻样本、日聚类与MDE。
