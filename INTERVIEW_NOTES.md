# Interview notes — complete guide

Read this end-to-end. Every section is something an interviewer could ask. The language is kept simple on purpose so you can read it once and repeat the ideas in your own words.

---

## How to use this file

- Each heading is a likely question.
- The answer under each heading is written so you can speak it out loud.
- Hard words are explained the first time they appear.
- If you only have 10 minutes before the call: read sections 1, 2, 3, and 6.

---

# PART A — The basics

## 1. What does this project do, in one sentence?

> It takes a GitHub issue from a Go project (cobra), figures out how to fix it, writes the code change, runs the Go compiler and tests to make sure it works, and produces a pull request description — all on its own.

## 2. What does "agentic AI" mean here?

A regular AI just answers a question. An **agent** is an AI that can also *do things* — read files, search code, run commands — in a loop, deciding what to do next based on what it just learned. Like a junior developer who can use a terminal, not a chatbot that just talks.

My agent has nine actions ("tools") it can use. It picks one, sees what comes back, picks the next, and so on, until it decides the job is done.

## 3. What is an "LLM" and what role does it play?

**LLM** = Large Language Model = the AI brain (in my case, Meta's Llama-3.3-70B served by Groq). It's the part that *thinks* and *decides*. It cannot touch files by itself. My Python code provides the hands (the tools) and the loop (the heartbeat).

## 4. What is "function calling" / "tool use"?

When the LLM wants to do something (like read a file), it doesn't write English saying "please read foo.go." Instead it outputs a structured JSON like:

```json
{"name": "read_file", "arguments": {"path": "fish_completions.go"}}
```

My code sees this, runs the actual function in Python, and sends the result back to the LLM as another message. Modern LLM APIs (OpenAI, Groq) support this natively — you declare a list of "tools" with JSON schemas, and the model picks one.

## 5. Why did you build this?

It's a take-home assignment. The brief said: *"build an agentic AI platform that can work on issues from open-source Go projects."* They wanted to see how I think about agents, tools, validating code changes, and keeping things simple.

The brief specifically warned against "thin wrappers" — meaning if I just wrote a 20-line script that says `model.chat("fix this issue")`, that wouldn't pass. The framework around the LLM is the point.

## 6. How long did it take?

About a day of design and building. Most of the time went into:
- Designing the tool set (what should the agent be able to do?)
- Writing the system prompt (what rules should the agent follow?)
- Handling errors gracefully (Groq sometimes returns broken tool calls, see §16)
- Picking a real, small issue to demo on (cobra #1933)

---

# PART B — Project structure

## 7. Walk me through the folders.

```
aa/
├── agent/              ← all my code is here
├── samples/            ← outputs from runs go here
├── workspace/          ← the cobra repo gets cloned here (gitignored)
├── README.md           ← setup + how to run
├── INTERVIEW_NOTES.md  ← this file
├── requirements.txt    ← Python dependencies (3 libraries)
├── .env.example        ← template for the API key file
└── .gitignore
```

## 8. Walk me through each file in `agent/`.

| File | What it does | Size |
| --- | --- | --- |
| `main.py` | The CLI entry point. Parses command-line args, fetches the issue, clones the repo, runs the loop, saves outputs. | ~130 lines |
| `loop.py` | The agent loop. Talks to the LLM, dispatches tool calls, decides when to stop. | ~150 lines |
| `tools.py` | The nine tools the agent can use + their JSON schemas. | ~300 lines |
| `prompts.py` | The system prompt — the rules the agent must follow. | ~40 lines |
| `llm.py` | Wraps the Groq API. Has retry logic for rate limits. | ~60 lines |
| `github.py` | Fetches an issue and its comments using the GitHub REST API. | ~80 lines |
| `repo.py` | Clones the target repo, creates a branch, produces a diff at the end. | ~70 lines |
| `__init__.py` | Empty marker file to make `agent/` a Python package. | 0 lines |

Total: roughly 800 lines. Small enough that you can read all of it in 30 minutes.

---

# PART C — How the agent works

## 9. Explain the agent loop in plain English.

1. **Setup.** We send the LLM a system message ("here are your rules") and a user message ("here's the issue, here's the repo path").
2. **Turn 1.** The LLM responds. It says either:
   - "I want to call this tool with these arguments" — then we run the tool and send the result back. *(usual case)*
   - Free text without a tool call — then we nudge it: "you didn't call a tool, continue or finish."
3. **Turn 2…N.** Same thing. Each turn the chat history grows by an assistant message and one tool-result message per tool call.
4. **Stop condition.** When the LLM calls the special `finish` tool with a `pr_title` and `pr_body`, we exit the loop.
5. **Safety net.** If we hit 25 turns without finishing, we stop anyway so it can't spin forever.

## 10. What does a single "turn" actually look like on the network?

We send a POST to `https://api.groq.com/openai/v1/chat/completions` with:
- The full conversation so far (all previous user/assistant/tool messages)
- The list of tool schemas
- `tool_choice="auto"` (let the model decide)

Groq returns a chat completion with either text content, tool calls, or both. We parse it, execute any tool calls locally, append everything to the conversation, and POST again.

## 11. Why do you send the whole conversation history every turn?

Because the API is *stateless* — the server doesn't remember anything between calls. The full history is the context. This is true of every OpenAI-compatible API including Groq.

**Cost implication:** the conversation grows each turn. That's why I aggressively truncate tool output (12 KB max) and big file reads (80 KB max) — to keep the history from exploding.

---

# PART D — The tools

## 12. List every tool and explain it in one line.

| Tool | What it does | When the agent uses it |
| --- | --- | --- |
| `list_dir` | Shows files and subfolders in a directory | First thing the agent does — explore the repo |
| `read_file` | Reads a file with line numbers; can read a slice (lines 50-100) | After finding a candidate file |
| `grep` | Recursive regex search through the repo | Find where a function/variable is used |
| `edit_file` | Replace exactly ONE occurrence of `old_str` with `new_str` | The main editing tool |
| `write_file` | Overwrite or create a whole file | Only for new files |
| `go_build` | Runs `go build ./...` | After an edit, to check it compiles |
| `go_vet` | Runs `go vet ./...` | Catches common Go bugs |
| `go_test` | Runs `go test` on a package | Confirms tests still pass |
| `gofmt` | Lists files that aren't formatted | Style check |
| `finish` | Special tool that ends the loop with a PR title + body | When the agent thinks it's done |

## 13. Why is `edit_file` find-and-replace instead of a unified diff?

LLMs are terrible at producing valid unified diffs. They get line numbers wrong, miscount context lines, mess up whitespace. About 20-40% of diff applications fail in published research.

Find-and-replace sidesteps the problem:
- The model gives `old_str` (some unique text from the file) and `new_str` (what to replace it with).
- If `old_str` appears zero times → error "not found, re-read the file."
- If `old_str` appears more than once → error "ambiguous, include more context."
- If exactly one match → swap it.

This forces the model to look at the file before editing. If it hallucinates context that isn't really there, the tool catches it immediately.

## 14. Why are tool outputs truncated to 12 KB?

Two reasons:
1. **Cost.** The full history is sent every turn. A 100 KB grep result on turn 3 means that 100 KB is sent again on turns 4, 5, 6… up to maybe 20 turns. That's millions of tokens for one bad grep.
2. **Focus.** When you dump 100 KB on an LLM, it tends to skim. Forcing truncation means the agent has to ask focused questions and read targeted slices.

The truncation is symmetric — head + tail of the output is kept, middle is replaced with `[X bytes truncated]`. That way the agent sees the start and end of any big output, which is usually enough.

## 15. Why does `read_file` have line numbers and a line range?

- **Line numbers:** so the model can reference specific lines when discussing the file or when issuing an `edit_file` with `old_str` taken from a specific line.
- **Line range:** so the model can re-read just the area it cares about. Reading lines 100-150 of a 1000-line file is much cheaper than reading the whole thing.

---

# PART E — The system prompt

## 16. What's in the system prompt and why?

The system prompt is the "constitution" for the agent. Mine has seven rules:

1. **Understand first, edit second** — explore before changing anything.
2. **Smallest change that fixes the issue** — no speculative refactors.
3. **Match the project's conventions** — look at neighbouring code first.
4. **Validate every change** — `go build` and `go vet` must pass before calling `finish`.
5. **Prefer `edit_file` over `write_file`** — don't rewrite whole files.
6. **Be honest about uncertainty** — if you can't fix it, say so.
7. **Stop when done** — call `finish` with a structured PR title and body.

Plus style rules: be brief between tool calls, don't restate the issue, don't blindly retry on errors.

## 17. Why those rules specifically?

Each one fixes a real failure I saw or expected:

- *"Understand first"* — without this, Llama starts editing on turn 2 based on the issue title alone.
- *"Smallest change"* — the assignment grades on "follows project conventions" and minimal diffs score higher than ambitious rewrites.
- *"Validate every change"* — without this, the model declares victory after editing and never runs `go build`.
- *"Be honest about uncertainty"* — without this, the model fabricates a fix when it's stuck.

These aren't theoretical — three of the seven came from rules I added *after* watching the agent misbehave on early runs.

---

# PART F — The demo run

## 18. Walk me through what happened when you ran it on cobra #1933.

Command: `python -m agent.main https://github.com/spf13/cobra/issues/1933 --base-ref v1.9.1 --fresh`

Step by step:

1. **Fetched the issue** — title was "command line args should be quoted in generated fish completion script." Three comments.
2. **Cloned cobra** at tag `v1.9.1` (before the fix was merged).
3. **Turn 1-2:** `list_dir` on root. Saw `fish_completions.go` in the file list.
4. **Turn 3-4:** Tried to `list_dir` into `cmd/` (doesn't exist) — wasted turns. Then tried two bad grep patterns.
5. **Turn 5:** Read `fish_completions.go` end-to-end.
6. **Turn 6:** Grepped for `set -l requestComp` — found it at line 56.
7. **Turn 7:** Re-read lines 55-57 to confirm the exact text.
8. **Turn 8:** First `edit_file` attempt — Groq returned a malformed JSON error (the model output got cut off).
9. **Turn 9:** My retry logic injected a hint message ("your last call was malformed, retry"). Model self-corrected and made a clean edit.
10. **Turn 10:** `go_build ./...` — passed.
11. **Turn 11:** `go_vet ./...` — passed.
12. **Turn 12:** `go_test ./...` — cobra package passed, but the `doc` subpackage failed because Windows Application Control blocked a test binary (environmental, not the agent's fault).
13. **Turn 13:** Called `finish` with title "Fix command line args should be quoted in generated fish completion script" and a PR body.

Total: 13 turns. Compile clean. Diff is 2 lines changed in 1 file.

## 19. Did the agent's fix match the merged human PR?

**Mostly, but not perfectly.** Side-by-side:

```
Human PR #2285:
set -l requestComp "%[10]s=0 $args[1] %[3]s $(string join ' ' -- (string escape -- $args[2..-1])) $lastArg"

Agent's fix:
set -l requestComp "%[10]s=0 '$args[1]' %[3]s $(string join ' ' (string escape $args[2..-1])) '$lastArg'",
```

The agent got the *concept* right: wrap the unsafe variables, use `string escape` for the multi-element one. But it introduced two small flaws:

1. **Stray trailing comma** at the end (`'$lastArg'",` instead of `$lastArg"`). This breaks the generated fish script — but Go compiles it fine because it's just a character inside a raw string.
2. **Missing `--` separators** before `$args[2..-1]` in `string escape`. The `--` tells fish "no more options follow" so args starting with `-` aren't misinterpreted.

Both flaws are *runtime properties of the generated shell script*. `go build` and `go vet` can't see them because they only check the Go side. **This is a real, principled limitation of any agent that validates only in the host language.** I documented it in `samples/issue-1933/NOTES.md`.

## 20. So is the agent's fix a pass or a fail?

Partial pass. Compile clean, build clean, vet clean, found the right file unaided, applied the right concept. But would not be accepted by a human reviewer without changes. That's an honest result for a small LLM agent on a real issue, and it's exactly the kind of behavior worth showing.

---

# PART G — Why each technical choice

## 21. Why Go, why cobra?

- Approved repo from the assignment.
- Cobra is small (~30k lines), flat-packaged (no nested modules), and pure Go (no codegen).
- It has many small, well-scoped issues — perfect for a focused agent.
- The other approved repos (`gin`, `validator`, `golangci-lint`) are also good; `cobra` was the simplest.

## 22. Why Python and not Go for the agent itself?

Python is faster to write, has the best LLM SDK ecosystem, and is what most agentic tooling is built in. The agent only needs to *call* Go binaries, not compile Go code. There's no benefit to writing the agent in Go and a lot of friction.

## 23. Why Groq and not OpenAI / Anthropic?

User preference. The code uses the OpenAI-compatible SDK with a custom `base_url`, so swapping to OpenAI is two lines of config. Groq has nice properties:
- Llama-3.3-70B is open-weight and free-tier-friendly.
- Very fast inference (~2 sec/turn vs ~10 sec on GPT-4o).
- Same wire format as OpenAI, so no vendor lock-in.

Downside: Groq's Llama is slightly less reliable with tool calls than GPT-4o. I mitigate this with the retry-on-malformed-tool-call logic.

## 24. Why no LangChain / LlamaIndex / CrewAI?

The brief explicitly says *"not a thin wrapper"* and *"simple, thoughtful framework."* LangChain adds 500+ lines of abstraction over what's actually a 150-line problem. Rolling my own loop in `loop.py` means:
- I can explain every line.
- I can debug every prompt and tool result.
- There's no version-pinning hell.

If the repo were 1000x bigger I'd reconsider, but for this scope, hand-rolling is clearer and shorter.

## 25. Why no embeddings / RAG?

**RAG = Retrieval-Augmented Generation.** You chunk the codebase, embed each chunk into a vector, and search by similarity. Useful for huge codebases.

Cobra is 30k lines. `grep` runs in <100ms and returns exact line numbers. Embeddings would:
- Add a cold-start indexing step.
- Split functions across chunk boundaries (bad).
- Return approximate matches instead of exact line numbers.

For repos under ~100k lines, plain grep + read beats RAG on both speed and precision. I'd add RAG for kubernetes-scale repos.

## 26. Why no Docker?

Two reasons:
- Go has to run on the host so the agent can use the real Go toolchain anyway.
- Adding Docker means another dependency for whoever evaluates this.

If I were running this in CI or against untrusted code I'd absolutely sandbox it — but for a local demo, the path-safety check in `tools.py` (no escape from the repo dir) is enough.

## 27. Why GitHub REST API and not the `gh` CLI / PyGithub?

- `requests` is already in the dependency tree (no extra dep).
- `gh` CLI requires the user to install and auth it.
- PyGithub is heavier and pins specific API versions.

For just fetching an issue and its comments, two `requests.get()` calls is the simplest possible solution.

---

# PART H — Error handling

## 28. What can go wrong, and how do you handle it?

| Failure | What happens | How I handle it |
| --- | --- | --- |
| Groq rate limit (429) | API returns HTTP 429 | Exponential backoff with 4 retries in `llm.py` |
| Groq 5xx (server error) | Transient server issue | Same exponential backoff |
| Malformed tool call (400 `tool_use_failed`) | Llama emitted broken JSON | Catch in `loop.py`, inject "retry with valid JSON" hint, continue |
| Tool execution crashes | A tool raises an exception | Caught in `dispatch()` in `tools.py`, returned to LLM as an error string |
| Bad path (escapes repo) | Model tries `read_file("../../etc/passwd")` | `_resolve()` raises ValueError before any IO |
| `go test` timeout | Hangs > 180 seconds | `subprocess` timeout, returns error to LLM |
| Iteration budget exceeded | LLM never calls `finish` | Loop exits at 25 turns, writes a partial-result transcript |

## 29. What was the most interesting bug you hit?

The malformed tool call from Groq, on the first end-to-end run. The model output cut off mid-JSON inside a `<function=...>` block. Groq's server-side parser rejected it with HTTP 400 `tool_use_failed`. My first run *crashed* on it.

Fix: catch that specific error code in the loop, append a user message saying "your last call was malformed JSON, retry with shorter `old_str`", and continue. The model self-corrected on the next turn. **That single change is the difference between a toy that breaks on real workloads and a system that recovers.**

## 30. How do you handle untrusted code?

Right now I don't, beyond:
- The path-safety check in `tools.py` (no `../` escapes).
- A subprocess timeout on `go test`.
- The agent runs as the host user with full filesystem access.

For real production use I'd Dockerize the workspace, drop network privileges, and add a token budget.

---

# PART I — Limitations and what's missing

## 31. What can't this agent do?

- **Can't validate generated code from a generated language.** The fish-completion bug is in a fish script that's generated from a Go raw string. `go build` doesn't see the fish-side bug.
- **Can't reason about runtime behavior** — it can read tests but can't actually run the binary against real inputs.
- **Can't navigate cross-repo context** (e.g., a bug caused by a change in a dependency).
- **Doesn't write regression tests** unless the prompt forces it. I should add this to the system prompt.
- **Doesn't actually create a PR** — outputs a diff and PR body. `gh pr create` would be a 20-line addition.

## 32. What's the single biggest weakness?

The agent doesn't check that *its fix is covered by a test*. It runs `go test ./...` and is satisfied if everything passes — but it doesn't verify a test actually exercises the bug. A truly conservative agent would refuse to finish without adding a regression test.

## 33. What would you change with another day?

1. **Hard validation gate** — refuse the `finish` tool call unless `go build` and `go vet` pass *in the same turn*.
2. **Multi-edit tool** — batch several `edit_file` calls into one, saving turns.
3. **Evaluation harness** — run the agent against 20+ closed (issue, merged PR) pairs and compute file-overlap accuracy automatically.
4. **`--push` flag** — wire `gh pr create` for actual PR submission.
5. **Sharper grep tool** — currently the agent wastes turns on bad regex patterns. A "find symbol" tool that wraps Go's AST would be faster.

---

# PART J — Anticipated specific questions

## 34. "Show me where you handle path safety."

`tools.py`, function `_resolve(repo_dir, rel)`:

```python
def _resolve(repo_dir, rel):
    p = (repo_dir / rel).resolve()
    if repo_dir.resolve() not in p.parents and p != repo_dir.resolve():
        raise ValueError(f"path escapes repo: {rel!r}")
    return p
```

Every tool that touches the filesystem goes through this. It resolves the path to absolute, then checks that the resolved path is *inside* the repo directory. Prevents `../../etc/passwd` style escapes.

## 35. "Explain the retry-on-malformed-tool-call."

`loop.py`, in the iteration loop:

```python
try:
    resp = llm.chat(...)
except BadRequestError as e:
    if "tool_use_failed" in str(e):
        messages.append({"role": "user", "content": "Your last tool call was malformed. Retry with valid JSON..."})
        continue
    raise
```

When Groq returns 400 with `tool_use_failed`, we don't crash. We add a hint message and let the loop continue. The model retries with a shorter, cleaner argument and usually succeeds.

## 36. "How does the agent know when to stop?"

There's a special tool called `finish` in the registry. Its `execute` returns a sentinel string `FINISH_SIGNALED`. In the dispatch loop I check after each tool call: if `name == "finish"`, I save the title and body from the arguments and break out of the loop. The model is told in the system prompt to call this tool when (and only when) the build and vet pass.

## 37. "Why 25 max iterations?"

A working run takes 10-15 turns. 25 is generous headroom for retries and exploration. Beyond 25 the model is usually stuck and won't recover — better to fail loudly than burn tokens. It's configurable via `MAX_ITERATIONS` env var or `--max-iter` flag.

## 38. "Why temperature 0.2?"

Temperature controls randomness. 0 is fully deterministic, 1+ is creative. For code editing you want *low* randomness — the answer to "what file should I read next?" should be the most likely answer, not a creative one. 0.2 is low but not zero, which keeps the model from getting stuck in repetitive loops.

## 39. "What is `tool_choice='auto'`?"

It tells the API: "decide for me whether to call a tool or just reply with text." Alternatives are `"none"` (never call tools, just chat) and `"required"` (must call a tool). `"auto"` is right because sometimes the model needs to think out loud before acting.

## 40. "Why do you store the transcript?"

For debugging and for the demo. The reviewer can read `transcript.md` and see exactly what the agent did at each turn — thought process, tool call, tool result. Without this, the agent is a black box. With it, anyone can audit.

The JSON version is for any future tooling (e.g., a replay script or a metrics dashboard).

## 41. "How does `--base-ref` work?"

Most cobra issues are closed and already fixed in `main`. If we ran the agent against current `main`, it would find that the bug is already fixed and either do nothing or hallucinate a "fix."

`--base-ref v1.9.1` tells the repo manager to do a *full* clone (not shallow) and `git checkout v1.9.1` — a version before the fix was merged. Now the agent sees the bug as it was originally reported. This is the right way to demo against closed issues.

## 42. "How would you evaluate this systematically?"

Build a harness:
1. List 20 closed (issue, merged PR) pairs from each approved repo.
2. For each: run the agent with `--base-ref <commit-before-fix>`.
3. Compare the agent's diff to the merged PR using three metrics:
   - **File overlap:** did the agent touch the right files? (precision/recall on file paths)
   - **Diff similarity:** how close is the agent's diff to the human diff? (use an AST-aware diff, not character-level)
   - **Test pass-through:** apply the agent's diff to the pre-fix commit and run the project's full test suite. Did the tests that the merged PR added (or fixed) pass?
4. Report aggregate scores.

This would turn "the agent worked on one issue" into "the agent passes X% of N issues."

## 43. "What's the cost per run?"

For cobra #1933: about 80 KB of total prompt tokens across 13 turns. On Groq's free tier this is essentially zero cost. On a paid OpenAI tier with GPT-4o it would be maybe $0.50–$1.00 per run. Cheaper if I'd swapped to GPT-4o-mini for the grep/read turns and reserved the big model for the edit/validate turns — a future optimization.

## 44. "Could you point this at a non-cobra repo?"

Yes — the agent is repo-agnostic. The `--base-ref` and `ensure_clone` machinery work on any GitHub repo. The Go-specific bits (`go_build`, `go_vet`, `go_test`) only run if the repo is Go. For other languages I'd add `python_test`, `npm_test`, etc., as new tools. The framework doesn't change.

---

# PART K — Quick reference cheat sheet

## 45. Numbers to memorize

| Number | What it is |
| --- | --- |
| 9 | Tools the agent has |
| 25 | Max iterations per run |
| 80 KB | Max file read size |
| 12 KB | Max tool output sent back to LLM |
| 180s | Timeout per Go command |
| 13 | Iterations the demo run took |
| ~800 | Lines of Python in the whole project |
| 30k | Roughly the LOC of cobra |

## 46. Two-sentence summary if they only listen to one thing

> "It's a small Python framework — under 800 lines — that wraps the Groq Llama-3.3-70B model with nine tools and a plan-act-validate loop. The interesting parts are the tool design (especially `edit_file` with uniqueness enforcement), the retry-on-malformed-tool-call recovery, and the explicit acknowledgment of where my validation tools can't see the bug — like a generated fish script."

## 47. If they ask "are you proud of this?"

> "I'm proud of two specific things. First, the tool design — every tool earned its place, and `edit_file`'s uniqueness check catches a class of LLM hallucination automatically. Second, the honesty in the demo writeup — I could have hidden the fact that my agent's fix isn't byte-identical to the human PR. Instead I documented exactly where it falls short and why my validation tools couldn't catch it. I think that kind of honesty is what makes the framework actually useful instead of a demo."
