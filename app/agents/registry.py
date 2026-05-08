"""Agent Registry — discover and run analysis scenarios by name.

Provides a one-call entry point for all pipeline stages.
New scenarios can be registered at runtime for extensibility.

Usage:
    from app.agents.registry import get_registry, AnalysisScenario
    registry = get_registry(llm_config)
    result = registry.run_sync(AnalysisScenario.DEBATE, contexts)
    print(registry.list_scenarios())
"""

from enum import Enum
from typing import Dict, Optional
import logging

from .runner import MultiAgentRunner
from .scenarios import ALL_SCENARIOS

logger = logging.getLogger("ftseek.agents.registry")


class AnalysisScenario(str, Enum):
    """Registered analysis scenario identifiers."""
    DEBATE = "debate"
    ATTRIBUTION = "attribution"
    PREVIEW = "preview"
    META = "meta"


class AgentRegistry:
    """Registry mapping scenario names to definitions and runners.

    Single entry point for all pipeline stages to invoke LLM analysis.
    """

    def __init__(self, llm_config: Optional[Dict] = None):
        self.llm_config = llm_config or {}
        self._scenarios = dict(ALL_SCENARIOS)

    # ─── Lookup ──────────────────────────────────────────────────────

    def get_definition(self, scenario: AnalysisScenario) -> Dict:
        """Get the raw scenario definition dict."""
        return self._scenarios[scenario.value]

    def get_runner(self, scenario: AnalysisScenario) -> MultiAgentRunner:
        """Get a configured runner for the given scenario."""
        return MultiAgentRunner(self.get_definition(scenario), self.llm_config)

    # ─── Execution ───────────────────────────────────────────────────

    async def run(self, scenario: AnalysisScenario, contexts: Dict[str, str]) -> Dict:
        """Run a scenario asynchronously. One line."""
        return await self.get_runner(scenario).run(contexts)

    def run_sync(self, scenario: AnalysisScenario, contexts: Dict[str, str],
                 timeout: int = 180) -> Dict:
        """Run a scenario synchronously. One line.

        This is the primary entry point for pipeline callers
        (schedulers, reviews, refreshers) which run in non-async contexts.
        """
        return self.get_runner(scenario).run_sync(contexts, timeout=timeout)

    # ─── Discovery ───────────────────────────────────────────────────

    def list_scenarios(self) -> Dict[str, str]:
        """List all registered scenarios with descriptions."""
        return {name: d["description"] for name, d in self._scenarios.items()}

    # ─── Extensibility ───────────────────────────────────────────────

    def register(self, name: str, definition: Dict) -> None:
        """Register a new scenario definition at runtime.

        New pipeline stages or plugins can register custom scenarios:
            registry.register("my_custom", {
                "name": "my_custom",
                "agents": [...],
                "rounds": [...],
            })
        """
        self._scenarios[name] = definition
        logger.info("[AgentRegistry] Registered new scenario: %s", name)


# Module-level singleton
_registry: Optional[AgentRegistry] = None


def get_registry(llm_config: Optional[Dict] = None) -> AgentRegistry:
    """Get or create the global AgentRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = AgentRegistry(llm_config)
    elif llm_config:
        _registry.llm_config = llm_config
    return _registry
