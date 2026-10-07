"""LLM provider: the already-authenticated local `claude` CLI (Claude Code),
run non-interactively. No API key and no local model server: whatever
account `claude` is logged in with answers the question.

`history` is a list of {"role": "user" | "assistant", "content": str} turns that
precede the current `user` message. `claude --print` takes one prompt, not a
turn list, so history is folded into the prompt text.
"""
import json
import subprocess
from collections.abc import Iterator
from typing import Protocol

from .config import Settings

History = list[dict]


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, user: str, history: History | None = None) -> str: ...

    def stream(self, system: str, user: str, history: History | None = None) -> Iterator[str]: ...


class ClaudeNotAvailable(RuntimeError):
    """The `claude` CLI is missing, not logged in, timed out, or exited with an error."""


def _render_prompt(user: str, history: History | None) -> str:
    if not history:
        return user
    lines = [f"{'Assistant' if t['role'] == 'assistant' else 'User'}: {t['content']}" for t in history]
    return "Earlier in this conversation:\n" + "\n\n".join(lines) + f"\n\nNow answer this:\nUser: {user}"


class ClaudeCliClient:
    """Shells out to `claude --print` for a single answer.

    `--safe-mode` keeps the call from loading this project's own Claude Code
    hooks, CLAUDE.md and plugins (cognitive-graph installs its own hooks, which
    would otherwise fire recursively on every question); `--tools ""` keeps it
    to a plain text answer from the context it is given, instead of letting it
    explore the filesystem itself; `--no-session-persistence` leaves no
    resumable session behind for a one-off question.
    """

    BASE_ARGS = ("--print", "--tools", "", "--safe-mode", "--no-session-persistence")

    def __init__(self, cli_path: str = "claude", model: str | None = None, timeout: float = 180) -> None:
        self._cli = cli_path
        self._model = model
        self._timeout = timeout
        self.name = f"claude-cli:{model}" if model else "claude-cli"

    def _args(self, system: str, output: list[str]) -> list[str]:
        args = [self._cli, *self.BASE_ARGS, "--system-prompt", system, *output]
        if self._model:
            args += ["--model", self._model]
        return args

    def _missing_cli(self, exc: FileNotFoundError) -> ClaudeNotAvailable:
        return ClaudeNotAvailable(
            f"'{self._cli}' was not found. Install Claude Code and make sure it is on PATH "
            "(and logged in: run `claude` once)."
        )

    def complete(self, system: str, user: str, history: History | None = None) -> str:
        args = self._args(system, ["--output-format", "text"])
        prompt = _render_prompt(user, history)
        try:
            proc = subprocess.run(
                [*args, prompt], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise self._missing_cli(exc) from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeNotAvailable(f"'{self._cli}' did not answer within {self._timeout:.0f}s.") from exc
        if proc.returncode != 0:
            raise ClaudeNotAvailable((proc.stderr or proc.stdout or "claude exited with an error").strip())
        return proc.stdout.strip()

    def stream(self, system: str, user: str, history: History | None = None) -> Iterator[str]:
        args = self._args(system, ["--output-format", "stream-json", "--include-partial-messages", "--verbose"])
        prompt = _render_prompt(user, history)
        try:
            proc = subprocess.Popen(
                [*args, prompt], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as exc:
            raise self._missing_cli(exc) from exc

        yielded = False
        error: str | None = None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "stream_event":
                delta = event.get("event", {}).get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yielded = True
                    yield delta["text"]
            elif event.get("type") == "result" and event.get("is_error"):
                error = str(event.get("result") or "claude reported an error")

        try:
            returncode = proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            returncode = proc.wait()
        if error or (not yielded and returncode != 0):
            stderr = proc.stderr.read().strip() if proc.stderr else ""
            raise ClaudeNotAvailable(error or stderr or "claude exited with an error")


def build_llm(settings: Settings) -> LLMClient:
    return ClaudeCliClient(settings.claude_cli, settings.claude_model, settings.claude_timeout)
