"""Pydantic schemas for every structured ADK LlmAgent call in the framework.

Each schema is deliberately flat / list-of-objects rather than dict-keyed
(e.g. `tasks: List[PlannedTask]` instead of `Dict[str, PlannedTask]`) because
dynamic dict-keyed schemas are the least reliable shape for Gemini structured
output. Callers convert the list into the `id -> Task` dict the AOV graph
(`workflow.Workflow`) expects right after validation.
"""

from typing import List, Literal

from pydantic import BaseModel, Field


class PlannedTask(BaseModel):
    """One node of a planner/refiner-proposed workflow graph."""

    id: str = Field(description="Task identifier, e.g. 'task0'")
    objective: str = Field(description="What this task must accomplish")
    output_format: str = Field(
        default="", description="Required output format, e.g. 'JSON', 'Python code', 'Markdown'"
    )
    next: List[str] = Field(default_factory=list, description="Task IDs that depend on this task")
    prev: List[str] = Field(default_factory=list, description="Task IDs this task depends on")


class PlannedWorkflow(BaseModel):
    """Full structured output of the planner agent for one candidate graph.

    Agent/module assignment is deliberately not part of this schema: the
    router agent (see planning_agents.router_agent) assigns each task to a
    specialist from the fixed `agent.AGENTS` registry based on its objective,
    independent of the planner's own task breakdown.
    """

    tasks: List[PlannedTask]


class WorkflowDelta(BaseModel):
    """Structured output of the refiner agent.

    `has_changes=False` means "keep the current workflow as-is" (the refiner
    is conservative by design per prompt.UPDATE_WORKFLOW_PROMPT). When
    `has_changes=True`, `tasks` is the full resulting task set (existing tasks
    repeated verbatim where unchanged, edited where needed, new tasks added) -
    Workflow.merge_workflow() diffs this against the current graph to compute
    the actual add/edit/delete operations.
    """

    has_changes: bool = Field(description="Whether any changes are proposed to the workflow")
    tasks: List[PlannedTask] = Field(default_factory=list)


class ValidationVerdict(BaseModel):
    """Structured output of the text validator agent."""

    status: Literal["completed", "failed"]
    feedback: str = Field(default="", description="Improvement feedback if status is 'failed'")


class AgentSelection(BaseModel):
    """Structured output of the router agent that assigns a task to a specialist module."""

    agent_id: int = Field(description="ID of the selected agent from the AGENTS registry")
