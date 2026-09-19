"""LLM-driven discovery: the model figures the flow out once, the recorder
turns that into a reusable capability.

The split matters. `agent.py` decides what to do and when to stop;
`recorder.py` decides what the run *means* as an artifact. The model never
sees a `TargetSpec` and could not write one — which is what stops its output
shape from becoming the schema.
"""

from .agent import DiscoveryAgent, DiscoveryResult, Transcript
from .catalogue import outcomes_for, recoveries_for
from .prompts import SYSTEM_PROMPT, render_for_model, substitute_secrets
from .recorder import Recorder, RecordedStep, RiskRules
from .tools import TOOL_SPECS, ToolBox, ToolOutcome

__all__ = [
    "DiscoveryAgent",
    "DiscoveryResult",
    "RecordedStep",
    "Recorder",
    "RiskRules",
    "SYSTEM_PROMPT",
    "TOOL_SPECS",
    "ToolBox",
    "ToolOutcome",
    "Transcript",
    "outcomes_for",
    "recoveries_for",
    "render_for_model",
    "substitute_secrets",
]
