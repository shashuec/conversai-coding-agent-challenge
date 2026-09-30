"""
TOOLS
The agent uses tools.list_files, tools.read_file, tools.patch_file,
tools.write_file, and tools.run_tests through the supplied tools object.
The agent itself calls tools to gather context (target source file and the
visible test at tests/test_task.py) before the first LLM call, then asks the
LLM only for structured patch_file / write_file / finish JSON actions which
the agent executes. The LLM never touches tools directly.

LOOP
1. Parse the task description for the source file path (src/.../*.py).
2. Read that source file and the visible test file (tests/test_task.py) once.
3. Send a focused prompt: task + target path + source excerpt + test excerpt.
4. Receive a JSON action (patch_file preferred, write_file allowed, finish
   allowed when no further safe progress is possible).
5. Execute the action. After a successful patch_file or write_file, immediately
   call run_tests and feed the result back to the LLM.
6. If tests pass, stop. Otherwise, ask the LLM for a revised patch.

FAILURE HANDLING
Invalid JSON, unknown actions, ambiguous patch text, and tool errors are
returned to the LLM as ACTION_ERROR feedback with a request for a corrected
patch_file action. Test failures are returned verbatim so the LLM can localize
the next edit. The agent never silently retries the same patch.

STOPPING
The agent stops as soon as run_tests returns "Exit code: 0", when the LLM
emits a finish action, or when the 5-LLM-call / 12-tool-call budget is
exhausted.
"""

from __future__ import annotations

import json
import re
from typing import Any


SYSTEM_PROMPT = """\
You are a careful Python bug-fix agent operating inside a pytest source \
repository.

You will receive:
- the task description,
- the relevant source file content (use it verbatim when forming patch \
"old" text),
- the visible test that defines the expected behavior.

Return exactly ONE JSON object describing the next action. The response \
must be valid JSON (no markdown fences, no commentary outside the JSON).

Supported actions:

{"action":"patch_file","path":"src/_pytest/...","replacements":[
  {"old":"exact existing text","new":"replacement text"},
  {"old":"another exact snippet","new":"replacement","occurrence":2}
]}

{"action":"write_file","path":"src/_pytest/...","content":"full new file"}

{"action":"read_file","path":"src/_pytest/..."}

{"action":"list_files"}

{"action":"finish","summary":"short explanation"}

Rules:
- Prefer patch_file over write_file. Make the smallest correct change.
- The "old" string MUST be copied character-for-character from the supplied \
source (including indentation and newlines). Never paraphrase or normalize \
whitespace.
- A single patch_file call may contain up to 8 replacements, all for the \
same file.
- If "old" text appears more than once in the file, add "occurrence": N \
(1-based) to disambiguate; otherwise omit it.
- Do not edit, rename, or create test files.
- If a previous attempt failed, read the test output carefully and revise \
the patch. Do not repeat the same failing patch.
- Target the underlying bug, not the visible assertion. Hidden regression \
tests will also be run.
- Only emit "finish" when tests have already passed or when no further safe \
progress is possible; otherwise keep editing.
- Output JSON only. No prose, no code fences.
""".strip()


PATH_RE = re.compile(r"src/[A-Za-z0-9_./]+\.py")
TEST_PATH = "tests/test_task.py"


def extract_target_path(task: str) -> str | None:
    """Return the first source file path mentioned in the task description."""
    match = PATH_RE.search(task)
    return match.group(0) if match else None


def parse_action(response: str) -> dict[str, Any]:
    """Parse an LLM JSON response into an action dict."""
    text = response.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```")
        text = text.removesuffix("```").strip()
    value = json.loads(text)
    if not isinstance(value, dict) or not isinstance(value.get("action"), str):
        raise ValueError("Expected a JSON object containing an 'action' field")
    return value


def execute_action(action: dict[str, Any], tools: Any) -> str:
    """Run the requested action through the supplied tools object."""
    name = action["action"]
    if name == "list_files":
        return tools.list_files()
    if name == "read_file":
        return tools.read_file(str(action.get("path", "")))
    if name == "write_file":
        return tools.write_file(
            str(action.get("path", "")),
            str(action.get("content", "")),
        )
    if name == "patch_file":
        replacements = action.get("replacements")
        if not isinstance(replacements, list):
            raise ValueError("patch_file requires a 'replacements' list")
        return tools.patch_file(str(action.get("path", "")), replacements)
    if name == "run_tests":
        return tools.run_tests()
    if name == "finish":
        return str(action.get("summary", "Finished"))
    raise ValueError(f"Unknown action: {name}")


def build_initial_prompt(
    task: str, target: str | None, source: str, test: str
) -> str:
    """Construct the first user message with pre-loaded context."""
    parts: list[str] = [f"TASK:\n{task}\n"]
    if target:
        parts.append(f"TARGET SOURCE FILE: {target}")
    if source:
        parts.append(f"SOURCE FILE CONTENT:\n{source}\n")
    else:
        parts.append(
            "SOURCE FILE CONTENT: (not available; call list_files or "
            "read_file to inspect the repository first)"
        )
    if test:
        parts.append(f"VISIBLE TEST FILE ({TEST_PATH}):\n{test}\n")
    else:
        parts.append("VISIBLE TEST FILE: (not available)")
    parts.append(
        "Return ONE patch_file (preferred) or write_file JSON action that "
        "fixes the bug. Do not edit tests."
    )
    return "\n".join(parts)


def build_followup_prompt(kind: str, payload: str) -> str:
    """Build the next-turn user message based on what just happened."""
    if kind == "test_failure":
        return (
            f"PATCH APPLIED. TEST OUTPUT:\n{payload}\n\n"
            "Read the failure carefully. Return ONE revised patch_file JSON "
            "action that fixes the failing tests while preserving existing "
            "behavior. Do not repeat the same failing patch."
        )
    if kind == "tool_result":
        return (
            f"TOOL RESULT:\n{payload}\n\n"
            "Now return ONE patch_file (preferred) or write_file JSON action."
        )
    return (
        f"{payload}\n\nReturn ONE corrected patch_file or write_file JSON "
        "action."
    )


def solve(task: str, tools: Any, llm: Any) -> str:
    """Drive the bug-fix loop within the 5-LLM-call / 12-tool-call budget."""
    target = extract_target_path(task)
    source = ""
    test = ""
    try:
        if target:
            source = tools.read_file(target)
    except Exception:
        source = ""
    try:
        test = tools.read_file(TEST_PATH)
    except Exception:
        test = ""

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_initial_prompt(task, target, source, test)},
    ]

    for _ in range(5):
        response = llm.ask(messages)
        messages.append({"role": "assistant", "content": response})
        try:
            action = parse_action(response)
            name = action["action"]
            if name == "finish":
                return str(action.get("summary", "Finished"))
            result = execute_action(action, tools)
            if name in ("patch_file", "write_file"):
                # Auto-run tests after every successful edit so the next
                # LLM call has ground-truth feedback to act on.
                try:
                    test_result = tools.run_tests()
                except Exception as error:
                    test_result = (
                        f"TEST_RUN_ERROR: {type(error).__name__}: {error}"
                    )
                if test_result.startswith("Exit code: 0"):
                    return "Patch applied; tests pass."
                messages.append(
                    {
                        "role": "user",
                        "content": build_followup_prompt(
                            "test_failure", test_result
                        ),
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": build_followup_prompt("tool_result", result),
                    }
                )
        except Exception as error:
            feedback = f"ACTION_ERROR: {type(error).__name__}: {error}"
            messages.append(
                {
                    "role": "user",
                    "content": build_followup_prompt("error", feedback),
                }
            )

    return "Model-call limit reached"
