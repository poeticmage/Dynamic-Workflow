"""Final result synthesis agent - ADK-native replacement for summary.py."""

from google.adk.agents import LlmAgent

import adk_runtime
import prompt
from config import Config

summarizer_agent = LlmAgent(
    name="workflow_summarizer",
    model=Config.SUMMARY_MODEL,
    instruction=prompt.RESULT_EXTRACT_PROMPT,
)


async def summarize(task: str, workflow_data: dict) -> str:
    """Synthesize the final deliverable from the completed workflow's task results."""
    user_content = f'''
        Here is the task description: {task}
        Here is the workflow for the task: {workflow_data}
    '''
    return await adk_runtime.run_text(summarizer_agent, user_content, session_id="summary")
