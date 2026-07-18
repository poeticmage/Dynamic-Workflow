"""Validator dispatch: deterministic Python-code testing vs. LLM text judgment.

Mirrors the paper's validator-module design (route by content type). In the
original repo this dispatch existed as separate classes (autovalidator.py /
pythonvalidator.py / textvalidator.py) but was never actually wired together -
Validator.validate() unconditionally called the text path, leaving
pythonvalidator.py/code_test_module.py dead. This rebuild wires the dispatch
the way it was clearly intended: python-looking output gets deterministic
AST + exec()-based testing (no LLM call needed - it's not a generative
judgment), everything else goes to a real structured-output LlmAgent judge.
"""

import re
from typing import Tuple

from google.adk.agents import LlmAgent

import adk_runtime
import prompt
from code_test_module import CodeTester
from config import Config
from schemas import ValidationVerdict

text_validator_agent = LlmAgent(
    name="text_validator",
    model=Config.VALIDATOR_MODEL,
    instruction=prompt.TEXT_VALIDATION_PROMPT,
    output_schema=ValidationVerdict,
    output_key="verdict",
)

_code_tester = CodeTester()

_PYTHON_LINE_MARKERS = re.compile(r"^\s*(def |class |import |from \S+ import )", re.MULTILINE)
_CODE_FENCE = re.compile(r"^```(?:python)?\s*\n?|\n?```\s*$", re.IGNORECASE)


def _looks_like_python(result: str) -> bool:
    return "```python" in result.lower() or bool(_PYTHON_LINE_MARKERS.search(result))


def _strip_code_fence(text: str) -> str:
    return _CODE_FENCE.sub("", text.strip())


async def validate(
    task_objective: str,
    result: str,
    task_id: str,
    overall_task: str = "",
    output_format: str = "",
) -> Tuple[str, str]:
    """Validate a task's result.

    Returns (feedback, status) with status in {'completed', 'failed'} - same
    contract as the original autovalidator.Validator.validate.
    """
    if _looks_like_python(result):
        code = _strip_code_fence(result)
        if not code:
            return "No Python code found to validate", "failed"
        test_result, status = await _code_tester.test_code(code, task_objective)
        return f"### **Code Validation Results:**\n{test_result}", status

    content_parts = []
    if overall_task:
        content_parts.append(f"## Overall User Goal:\n{overall_task}\n\n---\n")
    content_parts.append(f"## Current Task Requirement:\n{task_objective}\n\n---\n")
    if output_format.strip():
        content_parts.append(f"## Required Output Format:\n{output_format}\n\n---\n")
    content_parts.append(f"## Current Task Latest Result:\n{result}")

    try:
        verdict = await adk_runtime.run_structured(
            text_validator_agent,
            "".join(content_parts),
            session_id=f"validate_{task_id}",
            schema=ValidationVerdict,
        )
    except Exception as exc:
        return f"Validation system error: {exc}", "failed"

    return verdict.feedback, verdict.status
