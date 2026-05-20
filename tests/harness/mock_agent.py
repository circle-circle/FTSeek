"""
Mock Agent Bridge for Pre/Post Market Testing
"""
import time
from typing import Optional, Dict, Any

class MockAgentBridge:
    def __init__(self):
        self.responses: Dict[str, Any] = {}
        self.call_count = 0
        self.is_connected = True
        self.latency_seconds = 0.0

    def set_response(self, phase: str, response: Any):
        self.responses[phase] = response

    def set_latency(self, seconds: float):
        self.latency_seconds = seconds

    def ask(self, prompt: str, context: Dict = None) -> Optional[Any]:
        if not self.is_connected:
            raise ConnectionError("Agent Bridge Unavailable")
        
        if self.latency_seconds > 0:
            time.sleep(self.latency_seconds)
        
        self.call_count += 1
        phase = (context or {}).get("phase", "UNKNOWN")
        
        if phase in self.responses:
            return self.responses[phase]
        
        raise ValueError(f"No mock response configured for phase: {phase}")
