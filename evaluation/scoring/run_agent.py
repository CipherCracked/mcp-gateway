"""OpenHands SDK driver for headless agent execution.

Used instead of the full ``openhands`` CLI (which pulls in browser-use /
playwright). This script runs the agent headless using only ``openhands-sdk``,
which is much lighter to install inside the Docker sandbox.

Environment variables:
    LLM_MODEL       Model name (e.g. "openrouter/nvidia/nemotron-3.5-lightning:free")
    LLM_API_KEY     OpenRouter API key
    LLM_BASE_URL    OpenRouter base URL (default: https://openrouter.ai/api/v1)

The agent workspace is /workspace (mounted from the host). The task instruction
is read from /workspace/task/instruction.md.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, Field

from openhands.sdk import Agent, Conversation, LLM, Tool, ToolDefinition
from openhands.sdk.event import Action, Observation
from openhands.sdk.security.confirmation_policy import NeverConfirm
from openhands.sdk.tool import register_tool, resolve_tool


# ---------------------------------------------------------------------------
# Custom tools (file read / write / shell) — no browser-use or playwright needed
# ---------------------------------------------------------------------------


class ReadFileAction(Action):
    path: str = Field(description="File path relative to the workspace root")


class ReadFileObservation(Observation):
    content: str = Field(description="File contents")
    error: str | None = Field(default=None)


class ReadFileExecutor:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def __call__(self, action: ReadFileAction, conversation=None) -> ReadFileObservation:
        try:
            target = (self.workspace / action.path).resolve()
            # Safety: keep inside workspace
            if not str(target).startswith(str(self.workspace.resolve())):
                return ReadFileObservation(content="", error="path escapes workspace")
            content = target.read_text(encoding="utf-8")
            return ReadFileObservation(content=content, error=None)
        except Exception as exc:  # noqa: BLE001
            return ReadFileObservation(content="", error=str(exc))


class ReadFileTool(ToolDefinition[ReadFileAction, ReadFileObservation]):
    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence[Self]:
        workspace = params.get("workspace", "/workspace")
        return [
            cls(
                action_type=ReadFileAction,
                observation_type=ReadFileObservation,
                description="Read a file from the workspace and return its contents.",
                executor=ReadFileExecutor(Path(workspace)),
                annotations=None,
            )
        ]


class WriteFileAction(Action):
    path: str = Field(description="File path relative to the workspace root")
    content: str = Field(description="Content to write to the file")


class WriteFileObservation(Observation):
    ok: bool = Field(description="Whether the write succeeded")
    error: str | None = Field(default=None)


class WriteFileExecutor:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def __call__(self, action: WriteFileAction, conversation=None) -> WriteFileObservation:
        try:
            target = (self.workspace / action.path).resolve()
            if not str(target).startswith(str(self.workspace.resolve())):
                return WriteFileObservation(ok=False, error="path escapes workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.content, encoding="utf-8")
            return WriteFileObservation(ok=True, error=None)
        except Exception as exc:  # noqa: BLE001
            return WriteFileObservation(ok=False, error=str(exc))


class WriteFileTool(ToolDefinition[WriteFileAction, WriteFileObservation]):
    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence[Self]:
        workspace = params.get("workspace", "/workspace")
        return [
            cls(
                action_type=WriteFileAction,
                observation_type=WriteFileObservation,
                description="Write content to a file in the workspace.",
                executor=WriteFileExecutor(Path(workspace)),
                annotations=None,
            )
        ]


class RunCommandAction(Action):
    command: str = Field(description="Shell command to run")
    cwd: str | None = Field(default=None, description="Working directory (relative to workspace)")


class RunCommandObservation(Observation):
    exit_code: int = Field(description="Process exit code")
    stdout: str = Field(description="Standard output")
    stderr: str = Field(description="Standard error")


class RunCommandExecutor:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def __call__(self, action: RunCommandAction, conversation=None) -> RunCommandObservation:
        try:
            cwd = self.workspace
            if action.cwd:
                cwd = (self.workspace / action.cwd).resolve()
                if not str(cwd).startswith(str(self.workspace.resolve())):
                    return RunCommandObservation(
                        exit_code=1, stdout="", stderr="cwd escapes workspace"
                    )
            result = subprocess.run(
                action.command,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=120,
            )
            return RunCommandObservation(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            return RunCommandObservation(exit_code=1, stdout="", stderr=str(exc))


class RunCommandTool(ToolDefinition[RunCommandAction, RunCommandObservation]):
    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence[Self]:
        workspace = params.get("workspace", "/workspace")
        return [
            cls(
                action_type=RunCommandAction,
                observation_type=RunCommandObservation,
                description="Run a shell command in the workspace.",
                executor=RunCommandExecutor(Path(workspace)),
                annotations=None,
            )
        ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    model = os.environ.get("LLM_MODEL")
    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")

    if not model:
        print("[FAIL] LLM_MODEL environment variable is not set", flush=True)
        raise SystemExit(1)
    if not api_key:
        print("[FAIL] LLM_API_KEY environment variable is not set", flush=True)
        raise SystemExit(1)

    print(f"[INFO] Model: {model}", flush=True)
    print(f"[INFO] Base URL: {base_url}", flush=True)

    llm = LLM(
        model=model,
        api_key=api_key,
        base_url=base_url,
    )

    workspace = Path("/workspace")

    # Register custom tools
    register_tool("read_file", ReadFileTool.create)
    register_tool("write_file", WriteFileTool.create)
    register_tool("run_command", RunCommandTool.create)

    # Resolve tool specs for the agent
    tools = [
        Tool(name="read_file", params={"workspace": str(workspace)}),
        Tool(name="write_file", params={"workspace": str(workspace)}),
        Tool(name="run_command", params={"workspace": str(workspace)}),
    ]

    agent = Agent(llm=llm, tools=tools)

    conversation = Conversation(
        agent=agent,
        workspace=str(workspace),
        max_iteration_per_run=500,
        stuck_detection=True,
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
    conversation.run()
    print("[INFO] Agent run complete.", flush=True)


if __name__ == "__main__":
    main()
