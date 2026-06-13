"""The agent loop: plan -> act (call tools) -> validate -> finish.

The loop owns the chat history. Each iteration:
- send conversation + tool schemas to the LLM
- if the model returned tool_calls, dispatch each and append the results
- if it returned only text, nudge it to continue or finish
- if the model called `finish`, capture title/body and exit
- if we hit MAX_ITERATIONS, exit with whatever we have

Robustness:
- Groq occasionally returns HTTP 400 `tool_use_failed` when Llama emits broken
  JSON inside a tool call. We catch it, inject a hint, and let the model retry.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import BadRequestError

from . import llm, tools
from .prompts import SYSTEM_PROMPT


MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "25"))


@dataclass
class TranscriptEntry:
    turn: int
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_name: str | None = None


@dataclass
class LoopResult:
    finished: bool
    iterations: int
    pr_title: str
    pr_body: str
    transcript: list[TranscriptEntry]
    messages: list[dict[str, Any]]


def _user_intro(issue_rendered: str, repo_dir: Path) -> str:
    return (
        f"You have been assigned the following GitHub issue. The repository is "
        f"already cloned at:\n\n  {repo_dir.as_posix()}\n\n"
        f"All file paths you give to tools are relative to that root.\n\n"
        f"{issue_rendered}\n\n"
        f"Begin by exploring the repository. Make the smallest correct change. "
        f"Validate with go_build and go_vet before calling finish."
    )


def run(
    issue_rendered: str,
    repo_dir: Path,
    max_iter: int | None = None,
) -> LoopResult:
    max_iter = max_iter or MAX_ITERATIONS
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_intro(issue_rendered, repo_dir)},
    ]
    transcript: list[TranscriptEntry] = [
        TranscriptEntry(turn=0, role="user", content=messages[-1]["content"]),
    ]

    pr_title = ""
    pr_body = ""
    finished = False
    schemas = tools.schemas()

    for turn in range(1, max_iter + 1):
        try:
            resp = llm.chat(messages=messages, tools=schemas, tool_choice="auto")
        except BadRequestError as e:
            text = str(e)
            if "tool_use_failed" in text or "tool_use" in text.lower():
                hint = (
                    "Your last tool call was malformed. Retry with valid JSON arguments. "
                    "If old_str is very long, shorten it to a unique substring. "
                    "Do not include backticks or unescaped newlines inside JSON strings."
                )
                messages.append({"role": "user", "content": hint})
                transcript.append(TranscriptEntry(turn=turn, role="user", content=hint))
                continue
            raise

        choice = resp.choices[0]
        msg = choice.message

        # Persist the assistant message verbatim so tool_call_ids match later.
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            assistant_msg["content"] = msg.content
        else:
            assistant_msg["content"] = ""
        tool_calls_serialized: list[dict[str, Any]] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls_serialized.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                })
            assistant_msg["tool_calls"] = tool_calls_serialized
        messages.append(assistant_msg)
        transcript.append(TranscriptEntry(
            turn=turn,
            role="assistant",
            content=msg.content or "",
            tool_calls=tool_calls_serialized,
        ))

        if not msg.tool_calls:
            # The model replied with text only. Nudge it.
            nudge = (
                "You did not call a tool. If you are done, call `finish` with "
                "pr_title and pr_body. Otherwise continue by calling a tool."
            )
            messages.append({"role": "user", "content": nudge})
            transcript.append(TranscriptEntry(turn=turn, role="user", content=nudge))
            continue

        # Dispatch every tool call in order; append each result as role=tool.
        for tc in tool_calls_serialized:
            name = tc["function"]["name"]
            args = tc["function"]["arguments"] or "{}"
            result = tools.dispatch(name, args, repo_dir)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": name,
                "content": result,
            })
            transcript.append(TranscriptEntry(
                turn=turn,
                role="tool",
                content=result,
                tool_name=name,
            ))

            if name == "finish" and result == tools.FINISH_SIGNAL:
                try:
                    parsed = json.loads(args)
                except json.JSONDecodeError:
                    parsed = {}
                pr_title = parsed.get("pr_title", "")
                pr_body = parsed.get("pr_body", "")
                finished = True
                break

        if finished:
            break

    return LoopResult(
        finished=finished,
        iterations=min(turn, max_iter),
        pr_title=pr_title,
        pr_body=pr_body,
        transcript=transcript,
        messages=messages,
    )
