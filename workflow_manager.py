"""Planner/refiner orchestration: candidate generation, scoring, and workflow refinement.

ADK-native replacement for the old workflowManager.py's raw GPT calls. The
scoring math (compare_results / Workflow.calculate_average_parallelism /
Workflow.calculate_dependency_complexity, all in workflow.py) is unchanged -
only the mechanism for generating candidates changed: N planner_agent calls
run concurrently via asyncio.gather over independent ADK Runner invocations,
replacing the old ThreadPoolExecutor + synchronous OpenAI calls.
"""

import asyncio
import json
from typing import Dict, List, Optional

import adk_runtime
from agent import AGENTS
from history import History
from logging_config import get_logger, get_run_directory, log_intermediate_result
from planning_agents import planner_agent, refiner_agent, router_agent
from schemas import AgentAssignments, PlannedTask, PlannedWorkflow, WorkflowDelta
from workflow import Task, Workflow

logger = get_logger('workflow')


class WorkflowManager:
    """Generates candidate workflow graphs and refines the live one."""

    def __init__(self, objective: str):
        self.objective = objective
        self.workflow: Optional[Workflow] = None

    async def _resolve_agent_ids(
        self, planned_tasks: List[PlannedTask], session_suffix: str
    ) -> Dict[str, int]:
        """Route every task to a specialist agent in a single batched call.

        All tasks are routed together (not one independent call per task) so the
        router can reason about them as a set - e.g. not pick the same agent for
        two different tasks just because it fit an earlier one, when a more
        specific agent exists for one of them.
        """
        if not planned_tasks:
            return {}

        tasks_payload = "\n".join(
            f"- task_id: {t.id}\n  objective: {t.objective}" for t in planned_tasks
        )
        assignments = await adk_runtime.run_structured(
            router_agent,
            f"Tasks to route:\n{tasks_payload}",
            session_id=f"route_{session_suffix}",
            schema=AgentAssignments,
        )

        result: Dict[str, int] = {}
        for a in assignments.assignments:
            if a.agent_id in AGENTS:
                result[a.task_id] = a.agent_id
            else:
                logger.warning(
                    f"Router returned unknown agent_id {a.agent_id} for task {a.task_id}; "
                    f"defaulting to {min(AGENTS)}"
                )
                result[a.task_id] = min(AGENTS)

        for t in planned_tasks:
            if t.id not in result:
                logger.warning(
                    f"Router did not return an assignment for task {t.id}; defaulting to {min(AGENTS)}"
                )
                result[t.id] = min(AGENTS)
        return result

    async def _to_workflow(self, planned_tasks: List[PlannedTask], id_prefix: str) -> Workflow:
        agent_ids = await self._resolve_agent_ids(planned_tasks, session_suffix=id_prefix)
        tasks: Dict[str, Task] = {}
        for planned_task in planned_tasks:
            agent_id = agent_ids[planned_task.id]
            tasks[planned_task.id] = Task(
                id=planned_task.id,
                objective=planned_task.objective,
                agent_id=agent_id,
                next=list(planned_task.next),
                prev=list(planned_task.prev),
                status='pending',
                history=History(),
                agent=AGENTS[agent_id].name,
                output_format=planned_task.output_format,
            )
        return Workflow(tasks)

    async def _generate_candidate(self, index: int) -> Optional[Workflow]:
        try:
            user_content = f"the objective need to be achieved is: {self.objective}"
            planned = await adk_runtime.run_structured(
                planner_agent,
                user_content,
                session_id=f"plan_candidate_{index}",
                schema=PlannedWorkflow,
            )
            return await self._to_workflow(planned.tasks, id_prefix=f"cand{index}")
        except Exception as exc:
            logger.error(f"Failed to generate workflow candidate {index}: {exc}")
            return None

    def compare_results(self, all_result: List[Workflow]) -> Workflow:
        """Score candidates by dependency complexity vs. parallelism (unchanged math)."""
        if not all_result:
            raise ValueError("No results to compare. All iterations failed.")

        dependency_complexities = [wf.calculate_dependency_complexity() for wf in all_result]
        parallelisms = [wf.calculate_average_parallelism() for wf in all_result]
        logger.info(f'Comparing {len(all_result)} workflow candidates...')
        logger.info(f'Dependency complexities: {dependency_complexities}')
        logger.info(f'Parallelisms: {parallelisms}')

        mean_dependency_complexity = sum(dependency_complexities) / len(dependency_complexities)
        mean_parallelism = sum(parallelisms) / len(parallelisms)

        std_dependency_complexity = (
            sum((x - mean_dependency_complexity) ** 2 for x in dependency_complexities)
            / len(dependency_complexities)
        ) ** 0.5
        std_parallelism = (
            sum((x - mean_parallelism) ** 2 for x in parallelisms) / len(parallelisms)
        ) ** 0.5

        epsilon = 0.01
        best_workflow = None
        best_score = float('inf')
        for wf, dep_complexity, parallelism in zip(all_result, dependency_complexities, parallelisms):
            z_dependency_complexity = (dep_complexity - mean_dependency_complexity) / (
                std_dependency_complexity + epsilon
            )
            z_parallelism = (parallelism - mean_parallelism) / (std_parallelism + epsilon)
            score = z_dependency_complexity - z_parallelism
            if score < best_score:
                best_score = score
                best_workflow = wf

        log_intermediate_result(
            task_id="workflow_initialization",
            iteration=1,
            result_type="workflow_candidates_comparison",
            data={
                "total_candidates": len(all_result),
                "dependency_complexities": dependency_complexities,
                "parallelisms": parallelisms,
                "best_workflow_score": best_score,
                "mean_dependency_complexity": mean_dependency_complexity,
                "mean_parallelism": mean_parallelism,
            },
            status="candidate_selection_completed",
        )
        return best_workflow

    async def init_workflow(self, n_candidate_graphs: int = 10) -> Workflow:
        """Generate N candidate graphs concurrently and select the best by score."""
        results = await asyncio.gather(*[self._generate_candidate(i) for i in range(n_candidate_graphs)])
        results = [r for r in results if r is not None]
        failed = n_candidate_graphs - len(results)
        logger.info(f"Workflow generation complete: {len(results)} successful, {failed} failed")

        if not results:
            raise ValueError(
                f"All {n_candidate_graphs} parallel workflow generation attempts failed. "
                "Check the logs above for specific error details."
            )

        best_workflow = self.compare_results(results)
        self.workflow = best_workflow

        initflow_path = get_run_directory() / "initflow.json"
        best_workflow.to_json(str(initflow_path))
        logger.info(f"Initial workflow saved to {initflow_path}")
        return best_workflow

    async def update_workflow(self) -> None:
        """Refine the current workflow based on completed/failed task progress."""
        logger.info("Starting workflow refinement...")

        current_data = {tid: t.to_dict() for tid, t in self.workflow.tasks.items()}
        log_intermediate_result(
            task_id="workflow_refinement",
            iteration=1,
            result_type="workflow_pre_refinement",
            data={
                "total_tasks": len(current_data),
                "completed_tasks": sum(1 for t in current_data.values() if t['status'] == 'completed'),
                "pending_tasks": sum(1 for t in current_data.values() if t['status'] == 'pending'),
                "failed_tasks": sum(1 for t in current_data.values() if t['status'] == 'failed'),
            },
            status="refinement_starting",
        )

        refiner_input = {
            "current_workflow": [
                {
                    "id": tid,
                    "objective": t.objective,
                    "next": t.next,
                    "prev": t.prev,
                    "status": t.status,
                    "output_format": t.output_format,
                }
                for tid, t in self.workflow.tasks.items()
            ],
            "final_goal": self.objective.strip(),
        }

        delta = await adk_runtime.run_structured(
            refiner_agent,
            json.dumps(refiner_input, indent=2),
            session_id="refine",
            schema=WorkflowDelta,
        )

        if not delta.has_changes:
            logger.info("Workflow refinement: no changes proposed.")
            return

        agent_ids = await self._resolve_agent_ids(delta.tasks, session_suffix="refine")
        merge_payload = {
            t.id: {
                'objective': t.objective,
                'agent_id': agent_ids[t.id],
                'next': list(t.next),
                'prev': list(t.prev),
                'agent': AGENTS[agent_ids[t.id]].name,
                'output_format': t.output_format,
            }
            for t in delta.tasks
        }
        self.workflow.merge_workflow(merge_payload)

        updated_data = {tid: t.to_dict() for tid, t in self.workflow.tasks.items()}
        log_intermediate_result(
            task_id="workflow_refinement",
            iteration=2,
            result_type="workflow_post_refinement",
            data={
                "total_tasks_after": len(updated_data),
                "new_tasks_added": len(updated_data) - len(current_data),
                "refinement_completed": True,
            },
            status="refinement_completed",
        )
        logger.info("Workflow refinement complete.")
