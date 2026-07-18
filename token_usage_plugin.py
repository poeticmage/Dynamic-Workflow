"""ADK plugin that tallies Gemini token usage across every model call in a run.

Hooks `BasePlugin.after_model_callback`, which every `Runner` invokes right after
each raw model response comes back - regardless of which agent or session
triggered the call - so a single shared instance sees every specialist, planner,
router, validator, and summarizer call without any of them needing to report in
manually (unlike `adk_runtime.agent_usage`, which is a plain call counter).
"""

from collections import defaultdict
from typing import Dict, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin


class TokenUsagePlugin(BasePlugin):
    """Accumulates prompt/response/total token counts per agent, and overall."""

    def __init__(self, name: str = "token_usage"):
        super().__init__(name=name)
        self.per_agent: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"prompt": 0, "response": 0, "total": 0}
        )

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        usage = llm_response.usage_metadata
        if usage is None:
            return None
        counts = self.per_agent[callback_context.agent_name]
        counts["prompt"] += usage.prompt_token_count or 0
        counts["response"] += usage.candidates_token_count or 0
        counts["total"] += usage.total_token_count or 0
        return None

    @property
    def total_tokens(self) -> int:
        return sum(counts["total"] for counts in self.per_agent.values())
