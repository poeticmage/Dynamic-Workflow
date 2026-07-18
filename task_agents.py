"""TaskAttemptAgent: the execute -> validate -> re-execute loop for one task.

ADK-native replacement for taskexecuter.py + runner.py's AsyncRunner. A plain
Python for-loop inside a custom BaseAgent, not ADK's LoopAgent: LoopAgent needs
an explicit escalate-signaling wrapper agent to stop early, which is more
ceremony than a for-loop with an early break at this granularity of control
flow. The loop body itself is 100% ADK - every model call runs through a real
LlmAgent via adk_runtime.

Each task keeps one persistent ADK session (`exec_<task_id>`) for its
specialist persona across re-execution attempts, so ADK's own session/event
history carries the accumulated conversation - only the *new* turn (the
initial task prompt, or just the feedback on retries) needs to be sent.
"""

from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

import adk_runtime
import prompt
import validation_agents
from agent import AGENTS


class TaskAttemptAgent(BaseAgent):
    workflow: Any
    task_id: str
    overall_task: str
    max_validation_itt: int = 0

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        task_obj = self.workflow.tasks[self.task_id]
        context = self.workflow.get_context(self.task_id)
        next_objective = self.workflow.get_downsteam_objectives(self.task_id)
        specialist = AGENTS[task_obj.agent_id]
        session_id = f"exec_{self.task_id}"

        result = await adk_runtime.run_text(
            specialist,
            self._build_initial_prompt(task_obj, context, next_objective),
            session_id,
        )

        if self.max_validation_itt == 0:
            task_obj.save_history(result, "")
            task_obj.set_status("completed")
            yield self._event(ctx, f"Task {self.task_id} completed (validation disabled).")
            return

        for iteration in range(self.max_validation_itt):
            feedback, status = await validation_agents.validate(
                task_obj.objective,
                result,
                self.task_id,
                self.overall_task,
                task_obj.output_format,
            )
            task_obj.save_history(result, feedback)

            if status == "completed":
                task_obj.set_status("completed")
                yield self._event(
                    ctx, f"Task {self.task_id} validated after {iteration + 1} attempt(s)."
                )
                return

            if iteration < self.max_validation_itt - 1:
                result = await adk_runtime.run_text(
                    specialist, self._build_reexecution_prompt(task_obj, feedback), session_id
                )

        task_obj.set_status("failed")
        yield self._event(
            ctx, f"Task {self.task_id} failed validation after {self.max_validation_itt} attempt(s)."
        )

    def _build_initial_prompt(self, task_obj, context: str, next_objective: str) -> str:
        parts = [
            f"{prompt.TASK_EXECUTION_PROMPT}\n\n---\n\n",
            f"# **The Overall Goal**\n{self.overall_task}\n\n---\n",
            f"# **Context from Upstream Tasks**\n{context}\n\n---\n",
            f"# **Downstream Tasks Objectives**\n{next_objective}\n\n---\n",
            f"# **Current Task Requirements**\n{task_obj.objective}",
        ]
        if task_obj.output_format.strip():
            parts.append(f"\n\n---\n\n# **Required Output Format**\n{task_obj.output_format}")
        return "".join(parts)

    def _build_reexecution_prompt(self, task_obj, feedback: str) -> str:
        parts = [
            f"{prompt.TASK_REEXECUTION_PROMPT}\n\n---\n\n",
            f"# **Feedback on Previous Attempt**\n{feedback}\n\n---\n",
            f"# **Current Task Requirements**\n{task_obj.objective}",
        ]
        if task_obj.output_format.strip():
            parts.append(f"\n\n---\n\n# **Required Output Format**\n{task_obj.output_format}")
        return "".join(parts)

    def _event(self, ctx: InvocationContext, text: str) -> Event:
        return Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
        )
