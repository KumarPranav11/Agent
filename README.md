# Agentic AI Contributor for Go Projects

A small, focused agent that takes a GitHub issue from an approved Go project (`spf13/cobra`) and produces a code change, a unified diff, and a PR title + body. The agent runs a plan-act-validate loop over a fixed toolset — read/grep/edit + `go build` / `go vet` / `go test` — until it either calls `finish` or hits the iteration budget.

It is deliberately a thin framework, not a wrapper. The LLM only sees what the tools return, and every edit must pass real Go validation before the agent is allowed to declare success.

## What's in here

```
agent/
  llm.py        # Groq client (OpenAI-compat SDK, base_url override)
  github.py     # fetch issue + comments by URL (REST, no auth required)
  repo.py       # shallow-clone target repo, branch, diff
  tools.py      # tool implementations + JSON schemas for function-calling
  prompts.py    # system prompt
  loop.py       # plan-act-validate loop
  main.py       # CLI entrypoint
samples/        # captured outputs (diff.patch, pr_summary.md, transcript.md)
workspace/      # target repo is cloned here (gitignored)
```

## How it works

1. **Fetch issue** — `github.py` pulls the issue body and all comments by URL.
2. **Prepare repo** — `repo.py` shallow-clones (or resets) the target repo into `workspace/<repo>/` and creates a branch `agent/issue-<N>`.
3. **Agent loop** — `loop.py` opens a chat session with Groq. The system prompt establishes operating rules: understand before editing, smallest change possible, match conventions, validate before finishing. Each turn the model calls one or more tools; their outputs are fed back as `role=tool` messages. The loop ends when the model calls the `finish` tool (with a PR title + body) or hits `MAX_ITERATIONS`.
4. **Emit artifacts** — `main.py` writes `diff.patch`, `pr_summary.md`, `transcript.md`, and `transcript.json` into `samples/issue-<N>/`.

### Tools the agent has

| Tool | Purpose |
| --- | --- |
| `list_dir` | Explore project structure |
| `read_file` | Read with optional line range, capped at ~80 KB |
| `grep` | Recursive regex search, capped at 200 hits |
| `edit_file` | Find-and-replace exactly one occurrence (forces context-aware edits) |
| `write_file` | Overwrite/create a whole file (for new files) |
| `go_build` | Compile package(s) |
| `go_vet` | Static checks |
| `go_test` | Run tests (scoped by package; optional `-run` regex) |
| `gofmt` | List unformatted files |
| `finish` | Emit PR title + body and exit |

The `edit_file` tool refuses ambiguous matches and forces the model to disambiguate with surrounding context. This catches a class of hallucinated edits the model would otherwise make.

## Setup

Requires Python 3.10+, Git, and Go (for the validation tools). On Windows the agent locates `go.exe` even if it's not on `PATH`.

```bash
# 1. clone this repo, cd into it
# 2. install Python deps
python -m pip install -r requirements.txt

# 3. configure your Groq key
cp .env.example .env
# then edit .env and set GROQ_API_KEY=<your key from https://console.groq.com/keys>
```

Optional: set `GITHUB_TOKEN` in `.env` to raise the GitHub REST rate limit from 60/hr to 5000/hr.

## Running it

```bash
# basic — run against current HEAD of the target repo
python -m agent.main https://github.com/spf13/cobra/issues/1933

# reproduce a closed issue against a pre-fix tag so the bug is actually present
python -m agent.main https://github.com/spf13/cobra/issues/1933 --base-ref v1.9.1 --fresh
```

Flags:
- `--fresh` — re-clone the target repo from scratch (otherwise it resets to HEAD between runs)
- `--base-ref REF` — checkout a tag/branch/commit before running. Useful when the issue is already fixed in main and you want to reproduce it against an earlier release. Triggers a full clone instead of `--depth=1`.
- `--max-iter N` — cap iterations (default 25, override via `MAX_ITERATIONS` env)
- `--out NAME` — override output subdir under `samples/`

Outputs land in `samples/issue-<N>/`:
- `diff.patch` — `git diff HEAD` of the agent's changes
- `pr_summary.md` — the agent's PR title + body
- `transcript.md` — human-readable transcript of every thought/tool-call
- `transcript.json` — machine-readable transcript

## Model choice

Defaults via `.env`:
- `AGENT_MODEL=llama-3.3-70b-versatile` (Groq's strongest general model, 128k context)
- `AGENT_MODEL_FAST=llama-3.1-8b-instant` (reserved for future cheap retrieval calls)

Swap models without code changes by editing `.env`. Any Groq-served chat model that supports function calling will work.

## Sample run

`samples/issue-1933/` contains a checked-in run against [cobra#1933](https://github.com/spf13/cobra/issues/1933) — a real fish-completion quoting bug. The merged human fix is [PR #2285](https://github.com/spf13/cobra/pull/2285) (+2/-2 in `fish_completions.go`).

Outputs:
- `diff.patch` — the agent's change
- `pr_summary.md` — agent-generated PR title and body
- `transcript.md` — every thought and tool call
- `transcript.json` — same, machine-readable
- `NOTES.md` — honest comparison vs. the merged human PR (what the agent got right, what it got wrong, why the validation tools didn't catch the differences)

Read `samples/issue-1933/NOTES.md` for a writeup of the run.

## Design notes

- **Diff minimality.** The system prompt forbids speculative refactors. Reviewers grade on convention-matching and minimal diffs, so we score higher with a one-line fix than an ambitious rewrite.
- **Validation is mandatory.** The prompt blocks the model from calling `finish` until build + vet pass on the affected package. Without this, Llama models will declare victory after editing.
- **No embeddings / RAG.** A repo the size of cobra (~30k LoC Go, flat package layout) is small enough that `grep + read_file` outperform an embedding index on both latency and precision. We'd add retrieval only for a much larger codebase.
- **`edit_file` over freeform patches.** Unified-diff patching against LLM output is brittle. A find-and-replace tool with uniqueness checking is more robust and surfaces hallucinated context immediately.
- **Output truncation.** Tool outputs are capped at ~12 KB before going back to the model. Groq's TPM limits make long tool outputs expensive; truncation forces the agent to be selective.

## Known limitations

- One repo at a time; the loop has no support for parallel issues.
- No PR creation — the agent stops at the diff. Wiring `gh pr create` is straightforward but out of scope.
- Tests that need network or special build tags will fail in the sandbox; the agent treats those as red and may iterate. A `--skip-tests` flag is a reasonable next addition.
- Llama 70B can over-edit when the issue body is ambiguous. The prompt mitigates but does not eliminate this.
