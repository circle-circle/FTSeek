"""
Multi-agent runner — generalized N-agent, M-round execution engine.

Reads scenario definitions from app.agents.scenarios and executes them.
Replaces the hard-coded orchestration logic in DebateOrchestrator and
the parallel call pattern in llm_attribution.py.

A scenario can be:
  - 3 agents, 2 rounds + synthesis (debate)
  - 2 agents, 1 round (attribution)
  - 1 agent, 1 round (preview, meta)
  - Custom: registered at runtime via AgentRegistry.register()
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.agents.runner")


class MultiAgentRunner:
    """Generalized multi-agent execution engine.

    Usage:
        runner = MultiAgentRunner(DEBATE_SCENARIO, llm_config)
        result = await runner.run({"macro": ctx1, "technical": ctx2, "full": ctx3})
        # or sync:
        result = runner.run_sync({"default": ctx})
    """

    def __init__(self, scenario_def: Dict, llm_config: Optional[Dict] = None):
        self.scenario = scenario_def
        self.llm_config = llm_config or {}
        self.enable_thinking = self.llm_config.get("enable_thinking", False)

    async def run(self, contexts: Dict[str, str]) -> Dict:
        """Execute the scenario with provided contexts.

        Args:
            contexts: Dict keyed by context_key -> formatted data text.
                      E.g., {"macro": "...", "technical": "...", "full": "..."}

        Returns:
            Dict with keys: scenario, agent_results, synthesis, elapsed_sec
        """
        t0 = time.time()
        sc_name = self.scenario["name"]
        logger.info("[Runner] === %s scenario start ===", sc_name)

        agent_results: Dict[str, List[Dict]] = {
            a["id"]: [] for a in self.scenario["agents"]
        }

        for round_def in self.scenario["rounds"]:
            round_num = round_def["round"]
            logger.info("[Runner] %s Round %d...", sc_name, round_num)

            tasks = []
            agent_map: Dict[int, Dict] = {}

            for agent_def in self.scenario["agents"]:
                ctx_key = agent_def.get("context_key", "default")
                data_context = contexts.get(ctx_key, "")

                # Build template variables
                template_vars = {"data_context": data_context}

                # Inject previous round results for round 2+
                if round_num > 1:
                    prev_results = agent_results[agent_def["id"]]
                    if prev_results:
                        template_vars["my_round1"] = json.dumps(
                            prev_results[-1]["data"], ensure_ascii=False, indent=2,
                        )
                    # Pass other agents' opinions
                    other_opinions = []
                    for other in self.scenario["agents"]:
                        if other["id"] != agent_def["id"]:
                            other_results = agent_results.get(other["id"], [])
                            if other_results:
                                other_opinions.append(other_results[-1])
                    template_vars["other_opinions"] = other_opinions

                # Format the prompt
                prompt = round_def["prompt_template"].format(**{
                    k: template_vars.get(k, "") for k in round_def["template_keys"]
                })

                from app.llm.unified_service import get_unified_llm
                
                llm = get_unified_llm(
                    default_model=agent_def["model"],
                    default_timeout=agent_def.get("timeout", 90),
                    max_retries=1,
                    enable_thinking=self.enable_thinking,
                )
                messages = [
                    {"role": "system", "content": agent_def["system_prompt"]},
                    {"role": "user", "content": prompt},
                ]

                task = llm.call_and_parse(
                    messages, 
                    model=agent_def["model"],
                    timeout=agent_def.get("timeout", 90),
                    task_type=f"runner_{sc_name}"
                )
                tasks.append(task)
                agent_map[len(tasks) - 1] = agent_def

            # Execute (parallel or sequential)
            if round_def.get("parallel", True) and len(tasks) > 1:
                raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            else:
                raw_results = [await tasks[0]]

            for idx, data in enumerate(raw_results):
                if isinstance(data, Exception):
                    logger.warning(
                        "[Runner] %s agent %s failed: %s",
                        sc_name, agent_map[idx]["id"], data,
                    )
                    data = {}
                agent_def = agent_map[idx]
                agent_results[agent_def["id"]].append({
                    "round": round_num,
                    "agent": agent_def["name"],
                    "data": data,
                })

        # Synthesis (optional)
        synthesis = {}
        synth_def = self.scenario.get("synthesis")
        if synth_def:
            from app.llm.unified_service import get_unified_llm
            
            synth_llm = get_unified_llm(
                default_model=synth_def["model"],
                default_timeout=synth_def.get("timeout", 60),
                max_retries=1,
                enable_thinking=self.enable_thinking,
            )

            template_vars = {}
            for agent_def in self.scenario["agents"]:
                final_list = agent_results.get(agent_def["id"], [])
                if final_list:
                    template_vars[f"{agent_def['id']}_view"] = json.dumps(
                        final_list[-1]["data"], ensure_ascii=False, indent=2,
                    )
                else:
                    template_vars[f"{agent_def['id']}_view"] = "{}"

            prompt = synth_def["prompt_template"].format(**{
                k: template_vars.get(k, "") for k in synth_def["template_keys"]
            })
            messages = [
                {"role": "system", "content": synth_def["system_prompt"]},
                {"role": "user", "content": prompt},
            ]
            synthesis = await synth_llm.call_and_parse(
                messages,
                model=synth_def["model"],
                timeout=synth_def.get("timeout", 60),
                task_type=f"runner_{self.scenario['name']}_synthesis"
            )

        elapsed = time.time() - t0
        logger.info(
            "[Runner] === %s complete (%.1fs) ===", sc_name, elapsed,
        )

        return {
            "scenario": sc_name,
            "agent_results": agent_results,
            "synthesis": synthesis,
            "elapsed_sec": round(elapsed, 1),
        }

    def run_sync(self, contexts: Dict[str, str], timeout: int = 180) -> Dict:
        """Synchronous wrapper."""
        from app.llm.unified_service import UnifiedLLMService
        return UnifiedLLMService.run_sync(self.run(contexts), timeout=timeout)


def run_scenario_sync(
    scenario_def: Dict,
    contexts: Dict[str, str],
    llm_config: Optional[Dict] = None,
    timeout: int = 180,
) -> Dict:
    """Run any scenario synchronously in one call.

    Replaces run_debate_sync() and similar ad-hoc sync wrappers.

    Usage:
        result = run_scenario_sync(DEBATE_SCENARIO, {"macro": m, "technical": t, "full": f})
    """
    runner = MultiAgentRunner(scenario_def, llm_config)
    return runner.run_sync(contexts, timeout=timeout)
