"""Planner, refiner, and router LlmAgents.

ADK-native replacement for workflowManager.py's raw GPT calls (planner/refiner)
and preprocessing.py's ad hoc `google.genai.Client` call (router). All three
are plain structured-output LlmAgents - no tools, since `output_schema` and
`tools` cannot coexist on the same ADK LlmAgent.
"""

from google.adk.agents import LlmAgent

import prompt
from agent import AGENTS
from config import Config
from schemas import AgentSelection, PlannedWorkflow, WorkflowDelta

planner_agent = LlmAgent(
    name="workflow_planner",
    model=Config.PLANNER_MODEL,
    instruction=prompt.INIT_WORKFLOW_PROMPT,
    output_schema=PlannedWorkflow,
    output_key="proposed_workflow",
)

refiner_agent = LlmAgent(
    name="workflow_refiner",
    model=Config.PLANNER_MODEL,
    instruction=prompt.UPDATE_WORKFLOW_PROMPT,
    output_schema=WorkflowDelta,
    output_key="workflow_delta",
)

_AGENT_ROSTER = "\n\n".join(
    f"ID {agent_id}\nName: {spec.name}\nDescription: {spec.description}\nInstruction: {spec.instruction}"
    for agent_id, spec in AGENTS.items()
)

ROUTER_INSTRUCTION = f"""You are an agent router.

You MUST choose exactly one agent from the Available Agents list below to carry out the given
task objective.

CRITICAL RULES:
1. Read EVERY available agent before making a decision.
2. Evaluate EVERY agent's description and instruction.
3. Do NOT invent new agents.
4. Do NOT assume capabilities that are not explicitly stated.
5. You may ONLY select an agent ID that appears in Available Agents.
6. Compare all candidates before selecting the final answer.
7. Prefer the agent whose DESCRIPTION most directly matches the objective.
8. Use the instruction only as supporting evidence.
9. If multiple agents appear relevant, choose the most specialized one.

Available agents:
{_AGENT_ROSTER}
"""

router_agent = LlmAgent(
    name="agent_router",
    model=Config.ROUTER_MODEL,
    instruction=ROUTER_INSTRUCTION,
    output_schema=AgentSelection,
    output_key="agent_selection",
)
