"""Manage the target repository checkout.

Responsibilities:
- shallow-clone (or reuse) the repo under workspace/<repo>/
- optionally checkout a base ref (full clone in that case so tags are reachable)
- create the agent's working branch
- produce the final unified diff
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        cmd = " ".join(args)
        raise RuntimeError(
            f"command failed ({proc.returncode}): {cmd}\nstderr: {proc.stderr}"
        )
    return proc.stdout


def ensure_clone(
    owner: str,
    repo: str,
    issue_number: int,
    base_ref: str | None = None,
    fresh: bool = False,
) -> Path:
    """Return a path to a clean checkout of the repo on a new branch."""
    WORKSPACE.mkdir(exist_ok=True)
    repo_dir = WORKSPACE / repo
    url = f"https://github.com/{owner}/{repo}.git"

    if fresh and repo_dir.exists():
        shutil.rmtree(repo_dir)

    if not repo_dir.exists():
        clone_args = ["git", "clone"]
        if not base_ref:
            clone_args += ["--depth", "1"]
        clone_args += [url, str(repo_dir)]
        _run(clone_args)
    else:
        # Reset existing checkout so previous edits don't leak between runs.
        _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=repo_dir)
        _run(["git", "reset", "--hard"], cwd=repo_dir)
        _run(["git", "clean", "-fd"], cwd=repo_dir)

    if base_ref:
        _run(["git", "checkout", base_ref], cwd=repo_dir)

    branch = f"agent/issue-{issue_number}"
    # Drop a stale branch from a prior run, then create fresh from HEAD.
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    _run(["git", "checkout", "-b", branch], cwd=repo_dir)
    return repo_dir


def current_diff(repo_dir: Path) -> str:
    """Return `git diff HEAD` of the agent's edits."""
    return _run(["git", "diff", "HEAD"], cwd=repo_dir)


def stage_and_describe(repo_dir: Path) -> str:
    """Stage everything (for visibility in --stat) and return a short summary."""
    _run(["git", "add", "-A"], cwd=repo_dir)
    return _run(["git", "diff", "--cached", "--stat"], cwd=repo_dir)
