import asyncio
import sys
import time

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

import adk_runtime
from flow_agent import FlowAgent
from logging_config import (
    get_logger,
    get_run_directory,
    get_run_id,
    log_workflow_summary,
    save_results,
    save_run_metadata,
)
from summary_agent import summarize

logger = get_logger('main')

APP_NAME = "flow"
USER_ID = "flow_user"


async def _run_flow(flow_agent: FlowAgent):
    """Drive FlowAgent through a real ADK Runner, logging every progress Event it yields."""
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=flow_agent, session_service=session_service)
    session_id = f"flow_run_{get_run_id()}"
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=flow_agent.overall_task)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    logger.info(f"[{event.author}] {part.text}")

    return flow_agent.workflow


def main():
    """Entry point for running the workflow: builds a FlowAgent and drives it via an ADK Runner."""
    sys.stdout.reconfigure(encoding='utf-8')

    overall_task: str = '''
Your task is to take interview. The following are the steps
-"Analyze the job description",
    "Analyze the candidate resume",
    "Conduct technical interview",
    "Send emails as and when needed",
    "Score candidate responses",
    "Generate final evaluation report"

Return the output as a .txt file of all the steps taken to perform the tasks.


'''

    candidate_graphs: int = 5
    refine_threshold: int = 3
    max_refine_itt = 5
    max_validation_itt: int = 5

    config = {
        "candidate_graphs": candidate_graphs,
        "refine_threshold": refine_threshold,
        "max_refine_itt": max_refine_itt,
        "max_validation_itt": max_validation_itt,
    }
    save_run_metadata(overall_task, config)

    logger.info(f"Starting Flow execution for task: {overall_task[:100]}...")
    logger.info(f"Run ID: {get_run_id()}")
    logger.info(f"Run Directory: {get_run_directory()}")
    logger.info(
        f"Configuration: {candidate_graphs} candidates, refine_threshold={refine_threshold}, "
        f"max_validation_itt={max_validation_itt}"
    )

    start_time = time.time()

    flow_agent = FlowAgent(
        name="flow",
        overall_task=overall_task,
        refine_threshold=refine_threshold,
        max_refine_itt=max_refine_itt,
        n_candidate_graphs=candidate_graphs,
        max_validation_itt=max_validation_itt,
    )
    workflow = asyncio.run(_run_flow(flow_agent))

    elapsed_time = time.time() - start_time
    logger.info(f"Flow execution completed in {elapsed_time:.2f} seconds")

    workflow_data = workflow.to_dict()

    save_results(
        "workflow_final_state.json",
        workflow_data,
        f"Final workflow state for task: {overall_task[:50]}...",
    )

    chat_result = asyncio.run(summarize(overall_task, workflow_data))

    save_results(
        "final_summary.json",
        {"summary_text": chat_result, "original_task": overall_task},
        "Final synthesized summary of workflow execution",
    )

    example_file = get_run_directory() / "final_summary.txt"
    with open(example_file, "w", encoding="utf-8") as file:
        file.write(chat_result)

    log_workflow_summary(overall_task, workflow_data, elapsed_time, chat_result)

    logger.info(f"All results saved successfully to run directory: {get_run_directory()}")
    logger.info(f"Run completed! Check {get_run_directory()} for all files from this execution.")

    print("\nAgent Usage Statistics:")
    for agent_name, count in adk_runtime.agent_usage.items():
        print(f"{agent_name} -> {count}")


if __name__ == "__main__":
    main()
