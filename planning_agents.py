"""Planner, refiner, and router LlmAgents.

ADK-native replacement for workflowManager.py's raw GPT calls (planner/refiner)
and preprocessing.py's ad hoc `google.genai.Client` call (router). All three
are plain structured-output LlmAgents - no tools, since `output_schema` and
`tools` cannot coexist on the same ADK LlmAgent.
"""

from google.adk.agents import LlmAgent
from google.genai import types

import prompt
from agent import AGENTS
from config import Config
from schemas import AgentAssignments, PlannedWorkflow, WorkflowDelta

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
    f"ID {agent_id}\nName: {spec.name}\nDescription: {spec.description}"
    for agent_id, spec in AGENTS.items()
)

ROUTER_INSTRUCTION = f"""You are an agent router for a multi-task workflow.

You will be given a list of tasks (each with a task_id and an objective). You MUST assign
exactly one agent to EVERY task, from the Available Agents list below.

CRITICAL RULES:
1. Read EVERY available agent before making any decision.
2. Evaluate ALL tasks TOGETHER, not independently. Two tasks can look similar at a glance - do
   not default to the same agent for both just because it fit an earlier task. Re-derive the best
   match for each task_id from scratch against the FULL roster.
3. Prefer the agent whose description most SPECIFICALLY and LITERALLY names the task's domain
   (e.g. a task about analyzing a job description belongs to the agent explicitly described as
   parsing/structuring job descriptions) over a more general or adjacent agent that could
   plausibly also handle it. A specific-domain match always outranks a general one.
4. Do NOT invent new agents or agent IDs - only use IDs that appear in Available Agents below.
5. Do NOT assume capabilities that are not explicitly stated in an agent's description.
6. Every task_id you were given must appear exactly once in your output, each with exactly one
   agent_id.

Available agents:
{_AGENT_ROSTER}
"""

router_agent = LlmAgent(
    name="agent_router",
    model=Config.ROUTER_MODEL,
    instruction=ROUTER_INSTRUCTION,
    output_schema=AgentAssignments,
    output_key="agent_assignments",
    # Routing is a classification decision, not a generative one - deterministic
    # sampling makes it consistently pick its single best judgment instead of
    # occasionally sampling a plausible-but-wrong adjacent agent.
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
)
