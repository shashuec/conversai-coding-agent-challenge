# Experiment Report — `conversai-coding-agent-challenge`

## 1. Final `agent.py` Summary

The submitted `agent.py` is a focused-context orchestration layer. It pre-loads
the target source file and the visible test file before the first LLM call, then
asks the model only for structured `patch_file` / `write_file` JSON actions. The
agent auto-runs the tests after every successful edit and feeds the failure
output back to the model for the next repair attempt. The loop stops as soon as
`run_tests()` returns `Exit code: 0`, the model emits `finish`, or the
5-LLM-call / 12-tool-call budget is reached.

Constraints met:

- 242 lines, 9 KB (limits: 280 lines, 20 KB).
- Passes `check_agent.py` (`VALID agent.py`).
- Exports `solve(task, tools, llm)` with the required three-argument signature.
- Uses only `json`, `re`, and `typing.Any` — no forbidden imports or calls.
- Module docstring includes `TOOLS`, `LOOP`, `FAILURE HANDLING`, `STOPPING`
  sections (≈180 words, well under the 300-word limit).

## 2. Variations Tried

| Iter | What changed | Why I tried it | Cases run / result |
| --- | --- | --- | --- |
| 1 | Starter: LLM drives the loop, can emit any action including `list_files` and `read_file`. | Baseline. | Mock-trace: 5 LLM calls spent on `list → read source → read test → patch → run_tests`; only 1 cycle left for repair. |
| 2 | Extract the source-file path from the task description with `re.compile(r"src/[A-Za-z0-9_./]+\.py")` and skip `list_files` entirely. | All 10 practice task prompts name the target file explicitly, so listing the repo is wasted budget. | Mock: saved 1 LLM call + 1 tool call on every case. |
| 3 | Pre-read both the target source and the visible test (`tests/test_task.py`) before the first LLM call. | The visible test pins down expected behavior; the source pins down verbatim patch text. Pre-loading both into the first prompt lets the LLM produce a correct patch on call #1. | Mock: first-turn patches now plausible without further reads. |
| 4 | System prompt now explicitly prefers `patch_file` over `write_file` and forbids editing tests. | Surgical patches fit inside the 4 000-token output cap; rewriting large files (`_code/code.py` is 1 632 lines) would overflow. | Mock: patch outputs stayed well under 4 k tokens. |
| 5 | Auto-run `tools.run_tests()` immediately after every successful `patch_file`/`write_file` and feed the output back as the next user message. | Saves one LLM round-trip per edit (no need to ask the model "now run tests") and gives the next call ground-truth feedback instead of the model's guess. | Mock: budget now supports up to 5 patch attempts (2 tool calls each) inside the 12-call cap. |
| 6 | Stop the loop the instant `run_tests()` returns `Exit code: 0`. | Avoids wasting LLM calls on a final `finish` after success. | Mock: 1-LLM-call success path verified. |
| 7 | Add an explicit instruction: "Do not repeat the same failing patch." | Without this, temperature-0 models can re-emit a near-identical broken patch and burn the budget. | Mock: test-failure follow-up prompt now includes the full failure output and the "do not repeat" instruction. |
| 8 | Add: "Only emit `finish` when tests have already passed or no further safe progress is possible." | Prevents the model from giving up before exhausting repair attempts. | Mock: finish path still honored but discouraged without test success. |
| 9 | Reinforce: "The `old` string MUST be copied character-for-character, including indentation and newlines." | `tools.patch_file` raises `ValueError` on any mismatch, which would burn an LLM call. | Mock: `ACTION_ERROR` feedback path tested and works. |
| 10 | Defensive code-fence stripping in `parse_action`. | The runtime forces `response_format={"type":"json_object"}`, so fences shouldn't appear, but the cost is two lines and the safety is real. | Mock: parser handles ```` ```json … ``` ```` and bare ```` ``` ```` wrappers. |
| 11 (bonus) | Build a separate `_diff`-style follow-up prompt for the three cases: `test_failure`, `tool_result`, `error`. | Different feedback warrants different instructions — failures need "revise", tool results need "now patch", errors need "corrected patch". | Mock: all three branches tested. |

## 3. Final-Run Results on All 10 Public Cases

I could **not** execute the official `run_practice.py --all` evaluation because the
`OPENROUTER_API_KEY` environment variable was not available in my build
environment, and the runtime (`OpenRouterLLM.__init__`) hard-fails without it.
Rather than fabricate numbers, I report what I could verify:

- `python check_agent.py` → `VALID agent.py`.
- 7 mock orchestration tests in `scripts/test_agent_orchestration.py` (in my
  workspace, not part of the submission) pass against a fresh copy of
  `practice-repo/`. They verify:
  - target-path extraction for all 10 task prompts;
  - first-prompt composition includes the source file content and the visible
    test content;
  - `parse_action` handles raw JSON and code-fenced JSON;
  - `solve()` pre-loads source + test before the first LLM call;
  - `solve()` stops on `finish`;
  - `solve()` stops immediately when `run_tests()` returns `Exit code: 0`;
  - `solve()` feeds test failures back as `PATCH APPLIED. TEST OUTPUT: …`.

**Expected LLM-call usage per case** (based on the budget arithmetic, not
empirical runs):

- 2 tool calls pre-loaded (read source + read test).
- Each patch cycle: 1 LLM call + 1 `patch_file` + 1 `run_tests` = 1 LLM + 2 tool calls.
- Best case (1-shot fix): 1 LLM call, 4 tool calls total.
- Worst case (5 repair attempts): 5 LLM calls, 12 tool calls total. Exactly at
  the 12-tool-call cap, so a 6th attempt would be rejected by the runtime.

I therefore expect typical cases to consume 1–3 LLM calls and 4–8 tool calls.

## 4. Most Important Improvements

1. **Pre-loading context saves LLM calls.** The starter burns 2–3 of its 5 LLM
   calls on `list_files` / `read_file` / `read_file` before any patch is
   produced. Pre-loading collapses that to 0 LLM calls before the first patch,
   freeing the budget for up to 5 repair attempts.
2. **Auto-running tests after every patch** eliminates one LLM round-trip per
   edit and gives the model ground truth for the next call.
3. **Stopping on the first test-pass** instead of waiting for the model to emit
   `finish` saves one LLM call on every successful case.
4. **Structured patch_file JSON with verbatim `old` text emphasis** keeps edits
   small, verifiable, and inside the 4 000-token output cap.
5. **Three different follow-up prompts** (test failure / tool result / error)
   keep the LLM pointed at the right next step instead of giving it generic
   "continue" feedback.

## 5. What I Expect to Fail and What I Would Try Next

Even with the improvements above, the following cases are likely to need more
than 5 LLM calls for some failures of the underlying model:

- **Case 06 (`argparsing.py`)** — the task says "and any directly related help
  rendering code". The bug may straddle multiple files. The agent supports
  `read_file` and `list_files` mid-loop, but each extra read costs an LLM turn
  and pushes the budget toward the cap.
- **Case 08 (`_code/code.py`)** — the file is 1 632 lines, so
  `tools.read_file` returns a truncated beginning+end view. If the relevant
  function sits in the omitted middle, the model cannot form a verbatim `old`
  string. Next step: detect the truncation marker and either ask the model for
  a more specific search or implement a `read_section` helper that paginates
  the file.
- **Case 04 (`mark/structures.py`)** — the visible test expects a specific
  error message containing "expected a sequence of values, got NoneType". The
  model must reproduce that exact wording. If it paraphrases, the visible test
  fails and the private regression test likely fails too.

### What I would try next, given more time

1. **Acquire an `OPENROUTER_API_KEY` and actually run all 10 cases.** The
   arithmetic above is reasoned, not measured.
2. **Add a `read_section(path, start, end)` custom action** so the model can
   page through large files (`code.py`, `python_api.py`, `pathlib.py`) instead
   of working from a truncated view.
3. **Add a `grep`-style action** that returns matching line numbers, so the
   model can locate the exact context for a `patch_file` `old` string in a
   1 000-line file without burning an entire read.
4. **Track previous patch attempts** and refuse to apply the same patch twice,
   to defeat temperature-0 loops that re-emit identical broken patches.
5. **Insert the task name only as a path-derivation hint**, never as a string
   the model can hardcode, to stay clearly on the right side of the README's
   "no hardcoding task names" rule.

## 6. Files Submitted

- `agent.py` — final orchestration implementation.
- `REPORT.md` — this report.
