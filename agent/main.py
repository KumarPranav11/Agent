"""CLI entrypoint: agent.main <issue-url> [flags]

Wires together github + repo + loop, then writes artifacts to
samples/issue-<N>/.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing any agent submodule that may read env vars.
load_dotenv()

from . import github, loop, repo


REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = REPO_ROOT / "samples"


def _render_transcript_md(result: loop.LoopResult) -> str:
    lines = [
        "# Agent transcript",
        "",
        f"- Iterations: {result.iterations}",
        f"- Finished cleanly: {result.finished}",
        "",
    ]
    for entry in result.transcript:
        header = f"## Turn {entry.turn} — {entry.role}"
        if entry.tool_name:
            header += f" ({entry.tool_name})"
        lines.append(header)
        if entry.content:
            lines.append("")
            lines.append("```")
            lines.append(entry.content)
            lines.append("```")
        for tc in entry.tool_calls:
            lines.append("")
            lines.append(f"**tool_call** `{tc['function']['name']}`")
            lines.append("```json")
            lines.append(tc["function"]["arguments"])
            lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _write_artifacts(out_dir: Path, result: loop.LoopResult, diff: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "diff.patch").write_text(diff, encoding="utf-8")

    pr_md = f"# {result.pr_title}\n\n{result.pr_body}\n" if result.finished else (
        "# (agent did not finish)\n\nThe agent exited without calling `finish`.\n"
    )
    (out_dir / "pr_summary.md").write_text(pr_md, encoding="utf-8")

    (out_dir / "transcript.md").write_text(
        _render_transcript_md(result), encoding="utf-8"
    )
    (out_dir / "transcript.json").write_text(
        json.dumps(
            {
                "finished": result.finished,
                "iterations": result.iterations,
                "pr_title": result.pr_title,
                "pr_body": result.pr_body,
                "transcript": [asdict(e) for e in result.transcript],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Agentic AI contributor for Go projects.",
    )
    parser.add_argument("issue_url", help="GitHub issue URL")
    parser.add_argument("--fresh", action="store_true",
                        help="re-clone the target repo from scratch")
    parser.add_argument("--base-ref", default=None,
                        help="checkout tag/branch/commit before running (triggers full clone)")
    parser.add_argument("--max-iter", type=int, default=None,
                        help="cap iterations (default 25, override via MAX_ITERATIONS env)")
    parser.add_argument("--out", default=None,
                        help="override output subdir under samples/")
    args = parser.parse_args(argv)

    print(f"Fetching issue: {args.issue_url}")
    issue = github.fetch_issue(args.issue_url)
    print(f"  -> {issue.repo_slug}#{issue.number}: {issue.title}")

    print(f"Preparing checkout of {issue.repo_slug} ...")
    repo_dir = repo.ensure_clone(
        owner=issue.owner,
        repo=issue.repo,
        issue_number=issue.number,
        base_ref=args.base_ref,
        fresh=args.fresh,
    )
    print(f"  -> {repo_dir}")

    print("Running agent loop ...")
    result = loop.run(
        issue_rendered=issue.render(),
        repo_dir=repo_dir,
        max_iter=args.max_iter,
    )
    print(f"  -> iterations={result.iterations}, finished={result.finished}")

    diff = repo.current_diff(repo_dir)
    out_name = args.out or f"issue-{issue.number}"
    out_dir = SAMPLES_DIR / out_name
    _write_artifacts(out_dir, result, diff)

    print(f"Artifacts written to {out_dir}")
    if result.finished:
        print(f"PR title: {result.pr_title}")
    else:
        print("Agent did not call `finish`; review the transcript for the partial run.")

    return 0 if result.finished else 1


if __name__ == "__main__":
    sys.exit(main())
