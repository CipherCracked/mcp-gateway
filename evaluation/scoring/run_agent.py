"""OpenHands SDK driver for headless agent execution (host-based).

Runs the OpenHands SDK agent on the host machine. File tools operate on
the mounted workspace directory (visible both on the host and inside the
Docker sandbox at /workspace). Shell commands are executed inside the
sandbox container via ``docker exec`` for isolation — the agent never
runs code directly on the Windows host.

Environment variables:
    LLM_MODEL        Model name (e.g. "openrouter/nvidia/nemotron-3.5-lightning:free")
    LLM_API_KEY      OpenRouter API key
    LLM_BASE_URL     OpenRouter base URL (default: https://openrouter.ai/api/v1)
    SANDBOX_CONTAINER  Name of the running Docker sandbox container
    WORKSPACE_HOST     Absolute host path to the model workspace
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

import tenacity
from pydantic import Field

from openhands.sdk import Agent, Conversation, LLM, Tool, ToolDefinition
from openhands.sdk.event import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    LLMCompletionLogEvent,
    MessageEvent,
    ObservationEvent,
)
from openhands.sdk.security.confirmation_policy import NeverConfirm
from openhands.sdk.tool.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolExecutor,
)
from openhands.sdk.tool.registry import register_tool


# ---------------------------------------------------------------------------
# Path normalization helper
# ---------------------------------------------------------------------------


def _resolve_workspace_path(workspace: Path, path: str) -> Path:
    """Resolve a user-supplied path against the workspace root.

    Handles both relative paths (``src/main.py``) and container absolute
    paths (``/workspace/src/main.py``) that the model may pick up from
    shell command output inside the Docker sandbox. Strips a leading
    ``/workspace/`` prefix so container paths work with host-side file tools.
    """
    workspace_resolved = workspace.resolve()

    p = Path(path)
    # Strip leading /workspace/ or / so container paths become relative.
    if p.is_absolute():
        parts = p.parts
        if len(parts) > 1 and parts[0] == "" and parts[1] == "workspace":
            p = Path(*parts[2:])
        elif len(parts) > 0 and parts[0] == "":
            p = Path(*parts[1:])

    target = (workspace_resolved / p).resolve()

    # Strict containment check: is_relative_to avoids false positives like
    # workspace/foo matching workspace/foobar (which startswith would allow).
    if not target.is_relative_to(workspace_resolved):
        raise ValueError("path escapes workspace")

    return target


# ---------------------------------------------------------------------------
# Progress logging callback
# ---------------------------------------------------------------------------


class AgentProgressLogger:
    """Callback that logs agent events concisely and accumulates run stats.

    Pass an instance as a ``callbacks`` entry to ``Conversation``. It receives
    every conversation event and writes a one-line summary to stdout (which the
    pipeline redirects to run.log). After the run finishes, call
    :meth:`summary` to print an aggregate block.
    """

    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self.iteration = 0
        self.tool_calls: dict[str, int] = {}
        self.files_written: set[str] = set()
        self.errors: list[str] = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_event_at = self.started_at

    def __call__(self, event: Event) -> None:
        self.iteration += 1
        self.last_event_at = datetime.now(timezone.utc)
        now = self.last_event_at.strftime("%H:%M:%S")

        if isinstance(event, ActionEvent):
            tool = event.tool_name
            self.tool_calls[tool] = self.tool_calls.get(tool, 0) + 1
            summary = event.summary or "(no summary)"
            print(
                f"[{now}] step={self.iteration} action={tool} summary={summary}",
                flush=True,
            )
            if tool == "write_file" and event.action is not None:
                self.files_written.add(event.action.path)

        elif isinstance(event, ObservationEvent):
            tool = event.tool_name
            obs = event.observation
            is_error = getattr(obs, "is_error", False)
            status = "error" if is_error else "ok"
            preview = str(obs)[:120].replace("\n", " ")
            print(
                f"[{now}] step={self.iteration} observation={tool} status={status} "
                f"result={preview}",
                flush=True,
            )

        elif isinstance(event, AgentErrorEvent):
            self.errors.append(event.error)
            print(
                f"[{now}] step={self.iteration} AGENT_ERROR: {event.error[:200]}",
                flush=True,
            )

        elif isinstance(event, LLMCompletionLogEvent):
            # Token usage tracking, if the SDK emits these events.
            usage = getattr(event, "usage", None)
            if usage is not None:
                self.total_input_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.total_output_tokens += (
                    getattr(usage, "completion_tokens", 0) or 0
                )

        elif isinstance(event, MessageEvent):
            # LLM assistant/user messages — log role only, keep it light.
            if event.llm_message:
                role = event.llm_message.role
                print(f"[{now}] step={self.iteration} message={role}", flush=True)

    def summary(self) -> None:
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        print("\n" + "=" * 60, flush=True)
        print("AGENT RUN SUMMARY", flush=True)
        print("=" * 60, flush=True)
        print(f"  elapsed_seconds: {elapsed:.1f}", flush=True)
        print(f"  total_events: {self.iteration}", flush=True)
        print(f"  tool_calls: {dict(sorted(self.tool_calls.items()))}", flush=True)
        print(f"  files_written: {sorted(self.files_written)}", flush=True)
        print(f"  errors: {len(self.errors)}", flush=True)
        if self.errors:
            for e in self.errors:
                print(f"    - {e[:200]}", flush=True)
        print(
            f"  llm_input_tokens: {self.total_input_tokens}", flush=True
        )
        print(
            f"  llm_output_tokens: {self.total_output_tokens}", flush=True
        )
        print("=" * 60, flush=True)


# ---------------------------------------------------------------------------
# Custom tools (file read / write / shell via docker exec)
# ---------------------------------------------------------------------------


class ReadFileAction(Action):
    path: str = Field(description="File path relative to the workspace root")


class ReadFileObservation(Observation):
    pass


class ReadFileExecutor(ToolExecutor[ReadFileAction, ReadFileObservation]):
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def __call__(
        self, action: ReadFileAction, conversation=None  # noqa: ARG002
    ) -> ReadFileObservation:
        try:
            target = _resolve_workspace_path(self.workspace, action.path)
            content = target.read_text(encoding="utf-8")
            return ReadFileObservation.from_text(text=content, is_error=False)
        except Exception as exc:  # noqa: BLE001
            return ReadFileObservation.from_text(text=str(exc), is_error=True)


class ReadFileTool(ToolDefinition[ReadFileAction, ReadFileObservation]):
    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence[Self]:
        workspace = params.get("workspace", "/workspace")
        return [
            cls(
                action_type=ReadFileAction,
                observation_type=ReadFileObservation,
                description="Read a file from the workspace. Accepts paths relative to the workspace root (e.g. src/main.py) or Docker container paths (e.g. /workspace/src/main.py).",
                executor=ReadFileExecutor(Path(workspace)),
                annotations=ToolAnnotations(
                    title="read_file",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
        ]


class WriteFileAction(Action):
    path: str = Field(description="File path relative to the workspace root")
    content: str = Field(description="Content to write to the file")


class WriteFileObservation(Observation):
    pass


class WriteFileExecutor(ToolExecutor[WriteFileAction, WriteFileObservation]):
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def __call__(
        self, action: WriteFileAction, conversation=None  # noqa: ARG002
    ) -> WriteFileObservation:
        try:
            target = _resolve_workspace_path(self.workspace, action.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.content, encoding="utf-8")
            return WriteFileObservation.from_text(text="ok", is_error=False)
        except Exception as exc:  # noqa: BLE001
            return WriteFileObservation.from_text(text=str(exc), is_error=True)


class WriteFileTool(ToolDefinition[WriteFileAction, WriteFileObservation]):
    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence[Self]:
        workspace = params.get("workspace", "/workspace")
        return [
            cls(
                action_type=WriteFileAction,
                observation_type=WriteFileObservation,
                description="Write content to a file in the workspace. Accepts paths relative to the workspace root (e.g. src/main.py) or Docker container paths (e.g. /workspace/src/main.py).",
                executor=WriteFileExecutor(Path(workspace)),
                annotations=ToolAnnotations(
                    title="write_file",
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
            )
        ]


class RunCommandAction(Action):
    command: str = Field(description="Shell command to run")
    cwd: str | None = Field(default=None, description="Working directory (relative to workspace)")
    time_limit: int | None = Field(default=120, description="Maximum seconds to wait for the command to complete")


class RunCommandObservation(Observation):
    pass


class RunCommandExecutor(ToolExecutor[RunCommandAction, RunCommandObservation]):
    def __init__(self, workspace: Path, container_name: str):
        self.workspace = workspace
        self.container_name = container_name

    def __call__(
        self, action: RunCommandAction, conversation=None  # noqa: ARG002
    ) -> RunCommandObservation:
        try:
            cmd = ["docker", "exec"]
            if action.cwd:
                cmd.extend(["--workdir", f"/workspace/{action.cwd}"])
            cmd.extend([self.container_name, "sh", "-c", action.command])
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=action.time_limit or 120,
            )
            output = result.stdout
            if result.stderr:
                output += "\nSTDERR:\n" + result.stderr
            return RunCommandObservation.from_text(
                text=f"exit_code={result.returncode}\n{output}",
                is_error=result.returncode != 0,
            )
        except Exception as exc:  # noqa: BLE001
            return RunCommandObservation.from_text(text=str(exc), is_error=True)


class RunCommandTool(ToolDefinition[RunCommandAction, RunCommandObservation]):
    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence[Self]:
        workspace = params.get("workspace", "/workspace")
        container_name = params.get("container_name", "secure-mcp-gateway-sandbox")
        return [
            cls(
                action_type=RunCommandAction,
                observation_type=RunCommandObservation,
                description="Run a shell command in the sandbox container.",
                executor=RunCommandExecutor(Path(workspace), container_name),
                annotations=ToolAnnotations(
                    title="run_command",
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
            )
        ]


# ---------------------------------------------------------------------------
# Run execution with retry + graceful error handling
# ---------------------------------------------------------------------------


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Return True for LLM errors worth retrying (rate limits, timeouts, 5xx)."""
    from litellm.exceptions import RateLimitError, APIConnectionError, APITimeoutError

    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return True
    # Fallback: check for litellm-style rate limit errors by class name.
    exc_name = type(exc).__name__
    if "RateLimit" in exc_name or "RateLimitError" in exc_name:
        return True
    return False


def run_with_retry(conversation: Conversation, max_attempts: int = 10) -> None:
    """Run the conversation with retry on transient LLM errors.

    Uses tenacity with exponential backoff. Rate-limit errors (429) are retried
    with longer waits since they are typically per-minute limits that reset
    quickly. Non-transient errors are re-raised immediately.
    """

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_exponential(multiplier=3, min=10, max=30),
        retry=tenacity.retry_if_exception(_is_transient_llm_error),
        reraise=True,
    )
    def _run():
        conversation.run()

    _run()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    model = os.environ.get("LLM_MODEL")
    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    container_name = os.environ.get("SANDBOX_CONTAINER", "secure-mcp-gateway-sandbox")

    if not model:
        print("[FAIL] LLM_MODEL environment variable is not set", flush=True)
        raise SystemExit(1)
    if not api_key:
        print("[FAIL] LLM_API_KEY environment variable is not set", flush=True)
        raise SystemExit(1)

    print(f"[INFO] Model: {model}", flush=True)
    print(f"[INFO] Base URL: {base_url}", flush=True)
    print(f"[INFO] Sandbox container: {container_name}", flush=True)
    print(f"[INFO] Started at: {datetime.now(timezone.utc).isoformat()}", flush=True)

    llm = LLM(
        model=model,
        api_key=api_key,
        base_url=base_url,
        reasoning_effort="medium",
    )

    # Host workspace path (mounted into the container at /workspace)
    workspace = Path(os.environ.get("WORKSPACE_HOST", "/workspace"))

    # Register custom tools
    register_tool("read_file", ReadFileTool.create)
    register_tool("write_file", WriteFileTool.create)
    register_tool("run_command", RunCommandTool.create)

    # Resolve tool specs for the agent
    tools = [
        Tool(name="read_file", params={"workspace": str(workspace)}),
        Tool(name="write_file", params={"workspace": str(workspace)}),
        Tool(
            name="run_command",
            params={"workspace": str(workspace), "container_name": container_name},
        ),
    ]

    agent = Agent(llm=llm, tools=tools)

    conversation = Conversation(
        agent=agent,
        workspace=str(workspace),
        max_iteration_per_run=500,
        stuck_detection=True,
        callbacks=[AgentProgressLogger()],
    )

    # Auto-approve all actions (headless mode)
    conversation.set_confirmation_policy(NeverConfirm())

    # Read the task instruction
    instruction_path = workspace / "task" / "instruction.md"
    if instruction_path.exists():
        instruction = instruction_path.read_text(encoding="utf-8")
    else:
        instruction = (
            "Implement the task described in /workspace/task/instruction.md. "
            "Read the file first to understand what needs to be done."
        )

    print(f"[INFO] Instruction length: {len(instruction)} chars", flush=True)
    conversation.send_message(instruction)
    print("[INFO] Running agent...", flush=True)

    try:
        run_with_retry(conversation)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] Agent run failed: {exc}", flush=True)
        # Try to surface any accumulated progress from the callback.
        callbacks = getattr(conversation, "_callbacks", None)
        if callbacks:
            for cb in callbacks:
                if isinstance(cb, AgentProgressLogger):
                    cb.summary()
                    break
        raise SystemExit(1)
    else:
        print("[INFO] Agent run complete.", flush=True)
        callbacks = getattr(conversation, "_callbacks", None)
        if callbacks:
            for cb in callbacks:
                if isinstance(cb, AgentProgressLogger):
                    cb.summary()
                    break


if __name__ == "__main__":
    main()
