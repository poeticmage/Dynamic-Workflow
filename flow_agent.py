"""FlowAgent: the top-level AOV-graph orchestrator, as a real ADK BaseAgent.

ADK-native replacement for flow.py's `Flow` class. The scheduling/dependency
logic - `run`/`schedule_task`/`monitor_task`/`task_done_callback`, the
`asyncio.Event`/`asyncio.Lock`/`active_tasks` bookkeeping, the
refine_threshold/max_refine_itt gating - is unchanged from the original
design; it now lives inside `_run_async_impl` instead of a plain class, and
dynamic task fan-out stays ordinary asyncio (see task_agents.py's docstring
for why ADK's ParallelAgent/LoopAgent don't fit a runtime-mutating task set).

Being a real BaseAgent means the whole system is drivable via
`Runner(agent=FlowAgent(...)).run_async(...)` like any other ADK agent (see
main.py), yielding real ADK Events for progress instead of log-only feedback.
"""

import asyncio
from typing import Any, AsyncGenerator, Dict, Optional

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import PrivateAttr

from logging_config import get_logger, log_execution_result, log_intermediate_result
from task_agents import TaskAttemptAgent
from workflow_manager import WorkflowManager

logger = get_logger('execution')


class FlowAgent(BaseAgent):
    overall_task: str
    refine_threshold: int = 3
    max_refine_itt: int = 5
    n_candidate_graphs: int = 10
    max_validation_itt: int = 0
    initial_workflow: Optional[Any] = None

    _workflow_manager: WorkflowManager = PrivateAttr(default=None)
    _workflow: Any = PrivateAttr(default=None)
    _active_tasks: Dict[str, asyncio.Task] = PrivateAttr(default_factory=dict)
    _task_done_counter: int = PrivateAttr(default=0)
    _redefining: bool = PrivateAttr(default=False)
    _can_schedule_tasks: Any = PrivateAttr(default=None)
    _schedule_lock: Any = PrivateAttr(default=None)
    _event_queue: Any = PrivateAttr(default=None)
    _ctx: Any = PrivateAttr(default=None)

    def model_post_init(self, __context) -> None:
        super().model_post_init(__context)
        self._workflow_manager = WorkflowManager(self.overall_task)
        self._can_schedule_tasks = asyncio.Event()
        self._can_schedule_tasks.set()
        self._schedule_lock = asyncio.Lock()

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        self._ctx = ctx
        self._event_queue = asyncio.Queue()

        yield self._event(f"Planning workflow for: {self.overall_task[:80]}...")
        self._workflow = self.initial_workflow or await self._workflow_manager.init_workflow(
            self.n_candidate_graphs
        )
        self._workflow_manager.workflow = self._workflow
        yield self._event(f"Workflow planned with {len(self._workflow.tasks)} tasks.")

        await self.run()

        while not self._workflow.all_completed():
            async for event in self._drain_events():
                yield event
            await asyncio.sleep(0.1)

        async for event in self._drain_events():
            yield event
        yield self._event("All tasks completed.")

    @property
    def workflow(self):
        return self._workflow

    async def _drain_events(self) -> AsyncGenerator[Event, None]:
        while not self._event_queue.empty():
            yield await self._event_queue.get()

    async def run(self):
        """Schedule all pending/failed tasks whose dependencies are met."""
        if not self._can_schedule_tasks.is_set():
            return
        for task in self._workflow.get_runable_tasks():
            await self.schedule_task(task.id)

    async def schedule_task(self, task_id: str):
        """Schedule a single task for execution if it's not already active."""
        async with self._schedule_lock:
            if not self._can_schedule_tasks.is_set():
                return
            if task_id in self._active_tasks:
                return
            task = asyncio.create_task(self.run_task(task_id))
            self._active_tasks[task_id] = task
            asyncio.create_task(self.monitor_task(task_id, task))

    async def monitor_task(self, task_id: str, task: asyncio.Task):
        """Monitor a running task and handle its completion or error."""
        try:
            await task
        except asyncio.CancelledError:
            self._task_cancelled_callback(task_id)
        except Exception as e:
            self._task_error_callback(task_id, e)
        else:
            await self._task_done_callback(task_id)

    async def run_task(self, task_id: str):
        """Run one task's execute->validate->re-execute loop via TaskAttemptAgent."""
        task_obj = self._workflow.tasks[task_id]
        if task_obj.status == 'completed':
            logger.info(f"Task {task_id} completed; skipping.")
            return

        attempt_agent = TaskAttemptAgent(
            name=f"attempt_{task_id}",
            workflow=self._workflow,
            task_id=task_id,
            overall_task=self.overall_task,
            max_validation_itt=self.max_validation_itt,
        )
        async for event in attempt_agent.run_async(self._ctx):
            await self._event_queue.put(event)

        task = self._workflow.tasks[task_id]
        result, _ = task.get_latest_history()
        log_execution_result(
            task_id=task_id,
            agent_id=str(task.agent_id),
            objective=task.objective,
            result=result,
            status=task.status,
            duration=0.0,
        )

    def _task_cancelled_callback(self, task_id: str):
        self._active_tasks.pop(task_id, None)
        logger.info(f"Cancelled task {task_id} cleaned up.")

    def _task_error_callback(self, task_id: str, error: Exception):
        self._active_tasks.pop(task_id, None)
        logger.error(f"Task {task_id} encountered an error: {error}")

    async def _task_done_callback(self, task_id: str):
        """Called when a task completes. Updates downstream deps and may trigger refinement."""
        self._active_tasks.pop(task_id, None)
        self._task_done_counter += 1
        logger.info(f"Task {task_id} done. Total completed so far: {self._task_done_counter}")
        self._workflow.handle_task_done(task_id)

        will_refine = (
            self._task_done_counter >= self.refine_threshold
            and not self._redefining
            and self.max_refine_itt > 0
        )
        log_intermediate_result(
            task_id=task_id,
            iteration=1,
            result_type="task_completion_callback",
            data={
                "task_done_counter": self._task_done_counter,
                "refine_threshold": self.refine_threshold,
                "will_trigger_refinement": will_refine,
            },
            status="completed",
        )

        if will_refine:
            logger.info(f"Task {task_id} triggers workflow refinement.")
            self._task_done_counter = 0
            self._redefining = True
            self._can_schedule_tasks.clear()

            if self._active_tasks:
                logger.info("Waiting for active tasks to complete before refinement.")
                await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)

            await self._workflow_manager.update_workflow()
            self._can_schedule_tasks.set()
            self._redefining = False
            self.max_refine_itt -= 1
            await self._event_queue.put(
                self._event(f"Workflow refined (remaining refine iterations: {self.max_refine_itt}).")
            )

        await self.run()

    def _event(self, text: str) -> Event:
        return Event(
            author=self.name,
            invocation_id=self._ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
        )
