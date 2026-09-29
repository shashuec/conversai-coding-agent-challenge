"""TOOLS
Uses the supplied repository tools for file discovery, focused reads, edits, and tests.

LOOP
Uses a reconnaissance call, an implementation call, then up to three test-driven
repair calls.

FAILURE HANDLING
Malformed model output becomes feedback; failed tests are returned for targeted repair.

STOPPING
Stops immediately after a passing test run or after the bounded repair budget.
"""

from __future__ import annotations

import json
from typing import Any


SYSTEM = """You are a senior coding agent working in a large Python repository.

Solve the user's issue with minimal, general source changes. Never edit tests.
Use the repository evidence provided; do not invent APIs or file contents.
Hidden regression tests exist, so implement the underlying behavior rather than
hardcoding visible-test expectations.

You must return exactly ONE JSON object with no prose outside JSON.

RECON MODE:
{
  "kind": "inspect",
  "paths": ["path1.py", "path2.py"],
  "reason": "..."
}

Choose at most 2 relevant files to read.
Prefer the implementation and the relevant visible test.
Paths must come from the repository listing.

EDIT MODE:
{
  "kind": "edit",
  "edits": [
    {
      "action": "patch",
      "path": "x.py",
      "replacements": [
        {
          "old": "exact existing text",
          "new": "replacement text"
        }
      ]
    }
  ]
}

OR:

{
  "kind": "edit",
  "edits": [
    {
      "action": "write",
      "path": "x.py",
      "content": "complete file"
    }
  ]
}

Use exactly one edit when a change is needed.
Prefer patch_file because it is safer and preserves unrelated code.
Never edit tests.

REPAIR MODE:
Return the same edit JSON format based on the latest test failure.
Make the smallest correction needed.

If the implementation is already correct or no safe progress is possible:
{
  "kind": "done",
  "summary": "..."
}

IMPORTANT:
- Exact patch text must match the supplied file content.
- Preserve unrelated behavior and public APIs.
- Consider edge cases and hidden tests.
- Do not change dependencies or configuration unless required.
- Do not edit test files.
"""


def parse_json(response: str) -> dict[str, Any]:
    """Parse JSON even when the model accidentally wraps it in markdown."""

    text = response.strip()

    if "```" in text:
        parts = text.split("```")

        candidates = [
            part.strip()
            for part in parts
            if part.strip().startswith("{")
        ]

        if candidates:
            text = candidates[0]

    if text.startswith("json"):
        text = text[4:].strip()

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:
        text = text[start:end + 1]

    value = json.loads(text)

    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")

    return value


def ask(
    llm: Any,
    messages: list[dict[str, str]],
    prompt: str,
) -> dict[str, Any]:
    """Ask the model and parse its structured response."""

    messages.append({
        "role": "user",
        "content": prompt,
    })

    response = llm.ask(messages)

    messages.append({
        "role": "assistant",
        "content": response,
    })

    return parse_json(response)


def execute_edit(edit: dict[str, Any], tools: Any) -> str:
    """Apply one safe model-generated source edit."""

    edits = edit.get("edits")

    if not isinstance(edits, list) or not edits:
        raise ValueError("no edit returned")

    item = edits[0]

    if not isinstance(item, dict):
        raise ValueError("invalid edit object")

    action = item.get("action")
    path = str(item.get("path", ""))

    if not path:
        raise ValueError("edit path is empty")

    normalized = path.replace("\\", "/").lower()

    if normalized.startswith("tests/") or "/tests/" in normalized:
        raise ValueError("refusing to edit tests")

    if action == "patch":
        replacements = item.get("replacements")

        if not isinstance(replacements, list) or not replacements:
            raise ValueError("patch requires replacements")

        return str(
            tools.patch_file(
                path,
                replacements,
            )
        )

    if action == "write":
        content = item.get("content")

        if not isinstance(content, str):
            raise ValueError("write requires string content")

        return str(
            tools.write_file(
                path,
                content,
            )
        )

    raise ValueError(f"unsupported edit action: {action}")


def solve(task: str, tools: Any, llm: Any) -> str:
    messages = [
        {
            "role": "system",
            "content": SYSTEM,
        },
        {
            "role": "user",
            "content": "TASK:\n" + task,
        },
    ]

    tool_calls = 0
    model_calls = 0

    # ------------------------------------------------------------
    # PHASE 1: RECONNAISSANCE
    # ------------------------------------------------------------

    listing = tools.list_files()
    tool_calls += 1

    recon = ask(
        llm,
        messages,
        "MODE: RECONNAISSANCE\n\n"
        "Here is the repository file listing:\n\n"
        + listing
        + "\n\n"
        "Choose at most TWO files that give the strongest evidence "
        "for solving the task.",
    )

    model_calls += 1

    paths = recon.get("paths", [])

    if not isinstance(paths, list):
        paths = []

    paths = [
        str(path)
        for path in paths[:2]
        if isinstance(path, str)
    ]

    # ------------------------------------------------------------
    # PHASE 2: FOCUSED CONTEXT
    # ------------------------------------------------------------

    context_parts = []

    for path in paths:
        try:
            content = tools.read_file(path)
            tool_calls += 1

            context_parts.append(
                "FILE: "
                + path
                + "\n"
                + str(content)
            )

        except Exception as error:
            tool_calls += 1

            context_parts.append(
                "READ_ERROR: "
                + path
                + "\n"
                + str(error)
            )

    context = "\n\n".join(context_parts)

    # ------------------------------------------------------------
    # PHASE 3: IMPLEMENT
    # ------------------------------------------------------------

    try:
        edit = ask(
            llm,
            messages,
            "MODE: IMPLEMENT\n\n"
            "TASK:\n"
            + task
            + "\n\n"
            "RELEVANT REPOSITORY CONTEXT:\n"
            + context
            + "\n\n"
            "Return the smallest safe source edit.",
        )

        model_calls += 1

        if edit.get("kind") == "done":
            return str(
                edit.get(
                    "summary",
                    "No change made.",
                )
            )

        edit_result = execute_edit(
            edit,
            tools,
        )

        tool_calls += 1

    except Exception as error:
        edit_result = (
            "EDIT_ERROR: "
            + type(error).__name__
            + ": "
            + str(error)
        )

    # ------------------------------------------------------------
    # PHASE 4: FIRST TEST
    # ------------------------------------------------------------

    test_result = str(
        tools.run_tests()
    )

    tool_calls += 1

    if test_result.startswith("Exit code: 0"):
        return "Implemented the fix; visible tests pass."

    # ------------------------------------------------------------
    # PHASE 5: TEST-DRIVEN REPAIR
    # ------------------------------------------------------------

    for repair_round in range(3):

        if model_calls >= 5:
            break

        repair = ask(
            llm,
            messages,
            "MODE: REPAIR\n\n"
            f"REPAIR ROUND: {repair_round + 1}\n\n"
            "LATEST TEST RESULT:\n"
            + test_result
            + "\n\n"
            "PREVIOUS EDIT RESULT:\n"
            + edit_result
            + "\n\n"
            "TASK:\n"
            + task
            + "\n\n"
            "Diagnose the failure carefully. "
            "Return ONE minimal corrective source edit. "
            "Do not edit tests.",
        )

        model_calls += 1

        if repair.get("kind") == "done":
            return str(
                repair.get(
                    "summary",
                    "No further safe change.",
                )
            )

        try:
            edit_result = execute_edit(
                repair,
                tools,
            )

            tool_calls += 1

        except Exception as error:
            edit_result = (
                "EDIT_ERROR: "
                + type(error).__name__
                + ": "
                + str(error)
            )

            tool_calls += 1

        test_result = str(
            tools.run_tests()
        )

        tool_calls += 1

        if test_result.startswith("Exit code: 0"):
            return (
                "Implemented the fix; tests pass after "
                f"{repair_round + 1} repair round(s)."
            )

        # Keep enough budget for another complete repair+test cycle.
        if tool_calls >= 12:
            break

    return (
        "Agent stopped after bounded implementation and "
        "repair attempts.\n\n"
        "Last test result:\n"
        + test_result
    )
