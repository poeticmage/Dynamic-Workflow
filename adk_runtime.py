"""Shared ADK Runner/Session plumbing used by every LLM call site in the framework.

Replaces the old raw-OpenAI `GPTClient`. There is no bespoke HTTP client here at
all: every call to a model goes through a real `google.adk.runners.Runner`
driving an `LlmAgent`, exactly like the specialist agents in agent.py already do.

One `Runner` is cached per distinct `LlmAgent` instance (mirrors the old
`GPTClient.runners` dict keyed by agent id). Sessions are the unit callers use
for conversational continuity: pass the same `session_id` across calls to let
ADK's own session/event history accumulate the conversation (e.g. a task's
executor persona across re-execution attempts); pass a fresh, unique
`session_id` for one-shot/parallel calls (e.g. each planner candidate).

`Runner` + `InMemorySessionService` are treated purely as a per-call execution
shell here - the AOV task graph itself lives in plain Python objects
(workflow.Workflow/Task), not in ADK session state.
"""

import asyncio
from typing import Dict, Type, TypeVar

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

APP_NAME = "flow"
USER_ID = "flow_user"
TIMEOUT_SECONDS = 300.0

ModelT = TypeVar("ModelT", bound=BaseModel)

_session_service = InMemorySessionService()
_runners: Dict[str, Runner] = {}
agent_usage: Dict[str, int] = {}


def _runner_for(agent: LlmAgent) -> Runner:
    runner = _runners.get(agent.name)
    if runner is None:
        runner = Runner(app_name=APP_NAME, agent=agent, session_service=_session_service)
        _runners[agent.name] = runner
        agent_usage[agent.name] = 0
    return runner


async def _ensure_session(session_id: str) -> None:
    existing = await _session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if existing is None:
        await _session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )


async def _drive(runner: Runner, session_id: str, user_content: str) -> str:
    final_text_parts = []
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=user_content)]),
    ):
        # Only the final-response event carries the agent's actual answer;
        # intermediate/partial streaming events would otherwise be double-counted.
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text_parts.append(part.text)
    return "".join(final_text_parts)


async def _run(agent: LlmAgent, user_content: str, session_id: str) -> str:
    runner = _runner_for(agent)
    await _ensure_session(session_id)
    agent_usage[agent.name] += 1
    try:
        return await asyncio.wait_for(
            _drive(runner, session_id, user_content), timeout=TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"ADK agent '{agent.name}' call timed out after {TIMEOUT_SECONDS}s"
        ) from exc


async def run_text(agent: LlmAgent, user_content: str, session_id: str) -> str:
    """Run a plain-text LlmAgent (no output_schema) and return its reply text."""
    return await _run(agent, user_content, session_id)


async def run_structured(
    agent: LlmAgent, user_content: str, session_id: str, schema: Type[ModelT]
) -> ModelT:
    """Run a structured-output LlmAgent (output_schema=schema) and return the parsed model."""
    raw = await _run(agent, user_content, session_id)
    return schema.model_validate_json(raw)
