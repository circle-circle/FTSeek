"""
辩论引擎命令行入口

Usage:
    python -m debate_engine --topic "SSS阈值调优" --type factor_param --market US
    python -m debate_engine --list-models
    python -m debate_engine --knowledge-stats
    python -m debate_engine --learning-report
    python -m debate_engine --list-overrides
    python -m debate_engine --revert debate_xxx
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="FTSeek 独立辩论引擎")
    
    # 辩论相关
    parser.add_argument("--topic", type=str, help="辩议论题标题")
    parser.add_argument("--type", type=str, default="custom",
                        choices=["factor_param", "exit_profile", "filter_addition",
                                 "filter_removal", "strategy_viability", "cross_market",
                                 "failure_diagnosis", "custom"],
                        help="议题类型")
    parser.add_argument("--market", type=str, default="US", choices=["US", "HK", "BOTH"])
    parser.add_argument("--description", type=str, default="", help="议题详细描述")
    parser.add_argument("--evidence", type=str, default="", help="证据数据 (JSON string)")
    parser.add_argument("--models", type=str, default="", help="指定模型 (逗号分隔)")
    
    # 查询相关
    parser.add_argument("--list-models", action="store_true", help="列出可用模型")
    parser.add_argument("--knowledge-stats", action="store_true", help="知识库统计")
    parser.add_argument("--learning-report", action="store_true", help="生成学习报告")
    parser.add_argument("--list-overrides", action="store_true", help="列出活跃 override")
    parser.add_argument("--revert", type=str, help="回滚指定辩论的所有注入")
    
    args = parser.parse_args()
    
    # 查询命令
    if args.list_models:
        from debate_engine.models.bailian import BailianClient
        client = BailianClient()
        models = client.list_models()
        print(f"Enabled models: {models}")
        return
    
    if args.knowledge_stats:
        from debate_engine.knowledge import DebateKnowledgeBase
        kb = DebateKnowledgeBase()
        stats = kb.stats
        print(json.dumps(stats, indent=2))
        return
    
    if args.learning_report:
        from debate_engine.outcome_tracker import OutcomeTracker
        tracker = OutcomeTracker()
        report = tracker.generate_learning_report()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    
    if args.list_overrides:
        from debate_engine.verdict import InjectionApplier
        applier = InjectionApplier()
        active = applier.list_active()
        for item in active:
            print(f"  {item['file']}: {item['target']} ({item['action']}) conf={item['confidence']}")
        if not active:
            print("  (no active overrides)")
        return
    
    if args.revert:
        from debate_engine.verdict import InjectionApplier
        applier = InjectionApplier()
        count = applier.revert_all(args.revert)
        print(f"Reverted {count} injections for {args.revert}")
        return
    
    # 执行辩论
    if not args.topic:
        parser.print_help()
        return
    
    from debate_engine.topic import DebateTopic, TopicType
    from debate_engine.rounds import DynamicRoundScheduler, DebateConfig
    from debate_engine.knowledge import DebateKnowledgeBase
    from debate_engine.models.bailian import BailianClient
    
    # 解析 evidence
    evidence = {}
    if args.evidence:
        try:
            evidence = json.loads(args.evidence)
        except json.JSONDecodeError:
            evidence = {"raw": args.evidence}
    
    # 构建议题
    topic = DebateTopic(
        topic_id=args.topic.replace(" ", "_").lower()[:30],
        topic_type=TopicType(args.type),
        market=args.market,
        title=args.topic,
        description=args.description,
        evidence=evidence,
    )
    
    # 配置模型
    client = BailianClient()
    models = client.select_models(manual=args.models.split(",") if args.models else None)
    
    config = DebateConfig(models=models)
    kb = DebateKnowledgeBase()
    scheduler = DynamicRoundScheduler(config=config, client=client, knowledge_base=kb)
    
    print(f"Starting debate: {topic.title}")
    print(f"  Type: {topic.topic_type.value}")
    print(f"  Market: {topic.market}")
    print(f"  Models: {models}")
    print(f"  Rounds: {config.min_rounds}-{config.max_rounds}")
    print()
    
    verdict = scheduler.run(topic)
    
    # 输出结果
    print(f"\n{'='*60}")
    print(f"Debate Complete: {verdict.debate_id}")
    print(f"  Rounds: {verdict.total_rounds}")
    print(f"  Consensus Score: {verdict.consensus_score:.2f}")
    print(f"  Stop Reason: {verdict.stop_reason}")
    print(f"  Consensus Items: {len(verdict.consensus_items)}")
    print(f"  Disagreement Items: {len(verdict.disagreement_items)}")
    print(f"  Injections: {len(verdict.injections)}")
    print(f"{'='*60}")
    
    # 保存完整结果
    output_path = Path("debate_results") / f"{verdict.debate_id}.json"
    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(verdict.to_dict(), f, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to: {output_path}")


if __name__ == "__main__":
    main()
