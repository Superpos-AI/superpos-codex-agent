"""Queue-based worker that invokes OpenAI Codex CLI and routes output."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from pathlib import Path

import httpx

from .superpos_client import SuperposClient
from .config import Config
from .module_loader import collect_mcp_servers, discover_modules
from .runtime_config import RuntimeConfig
from .recent_tasks import RecentTasksLog, TaskSummary
from .session_store import SessionStore
from .telegram_gateway import TelegramGateway
from .telegram_streamer import TelegramStreamer
from .worktree_manager import ensure_worktree, is_git_repo, worktree_path

log = logging.getLogger(__name__)


@dataclass
class ExecutionRequest:
    prompt: str
    chat_id: int | str
    source: str  # "telegram" | "superpos"
    superpos_task_id: str | None = None
    branch: str | None = None
    image_paths: list[str] | None = None


_modules = discover_modules()
_mcp = collect_mcp_servers(_modules)


def _write_mcp_config(mcp_servers: dict) -> None:
    """Write MCP server configuration to ~/.codex/config.json."""
    config_path = Path.home() / ".codex" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    existing["mcpServers"] = mcp_servers
    config_path.write_text(json.dumps(existing, indent=2))


# Write MCP config at import time if any modules define MCP servers
if _mcp:
    _write_mcp_config(_mcp)


class CodexExecutor:
    def __init__(
        self,
        config: Config,
        runtime: RuntimeConfig,
        superpos: SuperposClient | None,
        gateway: TelegramGateway | None,
        persona: str | None = None,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._superpos = superpos
        self._gateway = gateway
        self._persona = persona
        self._inject_persona_into_agents_md()
        self._sessions = SessionStore()
        self._recent_tasks = RecentTasksLog(max_per_chat=5)
        self.queue: asyncio.Queue[ExecutionRequest] = asyncio.Queue()
        self._in_flight_superpos_tasks: set[str] = set()
        self._semaphore = asyncio.Semaphore(config.codex_max_parallel)
        self._worktree_locks: dict[str, asyncio.Lock] = {}
        self._active_count: int = 0

    def _inject_persona_into_agents_md(self) -> None:
        """Prepend persona to AGENTS.md so Codex picks it up as system prompt."""
        if not self._persona:
            return
        agents_md = os.path.join(self._config.codex_working_dir, "AGENTS.md")
        existing = ""
        if os.path.exists(agents_md):
            with open(agents_md, "r") as f:
                existing = f.read()
        persona_block = (
            "<!-- PERSONA:BEGIN -->\n"
            f"{self._persona}\n"
            "<!-- PERSONA:END -->\n\n"
        )
        # Replace existing persona block or prepend
        if "<!-- PERSONA:BEGIN -->" in existing:
            import re
            existing = re.sub(
                r"<!-- PERSONA:BEGIN -->.*?<!-- PERSONA:END -->\n*",
                persona_block,
                existing,
                flags=re.DOTALL,
            )
            with open(agents_md, "w") as f:
                f.write(existing)
        else:
            with open(agents_md, "w") as f:
                f.write(persona_block + existing)
        log.info("Injected persona into %s", agents_md)

    def update_persona(self, prompt: str | None) -> None:
        """Update the persona and re-inject into AGENTS.md."""
        self._persona = prompt
        self._inject_persona_into_agents_md()

    @property
    def pending(self) -> int:
        return self.queue.qsize()

    @property
    def is_busy(self) -> bool:
        """True if any task is currently executing."""
        return self._active_count > 0

    @property
    def has_free_slots(self) -> bool:
        """True if the executor can accept more concurrent tasks.

        Uses the in-flight task set (populated at claim time, cleared after
        execution) to accurately count tasks that are queued, waiting for
        the semaphore, OR actively executing.  ``queue.qsize()`` and
        ``_active_count`` both miss the semaphore-waiting gap.
        """
        return len(self._in_flight_superpos_tasks) < self._config.codex_max_parallel

    def add_superpos_task(self, task_id: str) -> None:
        self._in_flight_superpos_tasks.add(task_id)

    def remove_superpos_task(self, task_id: str) -> None:
        self._in_flight_superpos_tasks.discard(task_id)

    def has_superpos_task(self, task_id: str) -> bool:
        return task_id in self._in_flight_superpos_tasks

    def clear_session(self, chat_id: int | str) -> None:
        """Clear the stored session for a chat, starting fresh next message."""
        self._sessions.clear(chat_id)

    def _get_worktree_lock(self, slot: str) -> asyncio.Lock:
        if slot not in self._worktree_locks:
            self._worktree_locks[slot] = asyncio.Lock()
        return self._worktree_locks[slot]

    def _resolve_slot(self, req: ExecutionRequest) -> str:
        if (
            req.branch
            and self._config.codex_worktree_isolation
            and is_git_repo(self._config.codex_working_dir)
        ):
            return worktree_path(self._config.codex_working_dir, req.branch)
        return "__main__"

    async def run(self) -> None:
        """Infinite loop: pull requests from queue, dispatch concurrent workers."""
        log.info("Codex executor started (max_parallel=%d)", self._config.codex_max_parallel)
        while True:
            req = await self.queue.get()
            asyncio.create_task(self._run_one(req))

    async def _run_one(self, req: ExecutionRequest) -> None:
        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None

        # Start heartbeat IMMEDIATELY — before semaphore/worktree waits.
        # This keeps the server-side claim alive while queued.
        if req.source == "superpos" and req.superpos_task_id and self._superpos:
            progress_task = asyncio.create_task(
                self._report_progress(req.superpos_task_id, claim_expired)
            )

        try:
            async with self._semaphore:
                if claim_expired.is_set():
                    log.warning("Claim expired while waiting for semaphore: %s", req.superpos_task_id)
                    return

                slot = self._resolve_slot(req)
                wt_lock = self._get_worktree_lock(slot)

                # Wait for worktree lock OR claim expiry — whichever comes first
                lock_acquired = False
                try:
                    lock_task = asyncio.create_task(wt_lock.acquire())
                    expire_task = asyncio.create_task(claim_expired.wait())
                    done, pending = await asyncio.wait(
                        [lock_task, expire_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                        try:
                            await p
                        except asyncio.CancelledError:
                            pass

                    if claim_expired.is_set():
                        # Release lock if we got it while also expiring
                        if lock_task in done and lock_task.result():
                            wt_lock.release()
                        log.warning("Claim expired while waiting for worktree lock: %s", req.superpos_task_id)
                        return

                    lock_acquired = True
                    await self._execute(req, claim_expired)
                finally:
                    if lock_acquired:
                        wt_lock.release()
        except asyncio.CancelledError:
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            log.warning("Spurious CancelledError during execution (suppressed)")
        except Exception:
            log.exception("Execution failed for request: %s", req)
        finally:
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
            if req.superpos_task_id:
                self.remove_superpos_task(req.superpos_task_id)
            self.queue.task_done()

    async def _report_progress(
        self, task_id: str, claim_expired: asyncio.Event, interval: int = 30
    ) -> None:
        """Send periodic progress updates to keep the Superpos task alive."""
        progress = 5
        while True:
            await asyncio.sleep(interval)
            progress = min(progress + 5, 95)
            try:
                await self._superpos.update_progress(task_id, progress)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    log.warning("Claim expired for task %s (409); aborting execution", task_id)
                    claim_expired.set()
                    return
                log.debug("Progress update failed for task %s", task_id)
            except Exception:
                log.debug("Progress update failed for task %s", task_id)

    async def _execute(
        self, req: ExecutionRequest, claim_expired: asyncio.Event, retries: int = 3,
    ) -> None:
        self._active_count += 1
        if self._active_count == 1 and self._superpos:
            try:
                await self._superpos.update_status("busy")
            except Exception:
                log.debug("Failed to set agent status to busy")

        streamer = TelegramStreamer(self._gateway, req.chat_id)
        try:
            await streamer.start()
        except Exception:
            log.debug("Streamer start failed (non-fatal)")

        inner_task: asyncio.Task | None = None
        watcher_task: asyncio.Task | None = None

        async def _watch_claim_expiry() -> None:
            await claim_expired.wait()
            if inner_task is not None:
                inner_task.cancel()

        try:
            inner_task = asyncio.create_task(self._execute_inner(req, streamer, retries))
            if req.source == "superpos" and req.superpos_task_id:
                watcher_task = asyncio.create_task(_watch_claim_expiry())
            try:
                await inner_task
            except asyncio.CancelledError:
                if claim_expired.is_set():
                    log.warning(
                        "Execution aborted: claim expired for superpos task %s",
                        req.superpos_task_id,
                    )
                else:
                    raise
        finally:
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass
            # Always drain/close the streamer — idempotent, bounded by
            # its own timeout so a wedged Telegram gateway can't hang us.
            try:
                await streamer.finish()
            except Exception:
                log.debug("Streamer finish failed (non-fatal)", exc_info=True)
            # Clean up temp media files
            if req.image_paths:
                for p in req.image_paths:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            self._active_count -= 1
            if self._active_count == 0 and self._superpos:
                try:
                    await self._superpos.update_status("online")
                except Exception:
                    log.debug("Failed to set agent status to online")

    async def run_dream(self, task_id: str, prompt: str) -> None:
        """Backwards-compatible alias for dream tasks."""
        await self.run_background(task_id, prompt, task_type="dream")

    async def run_background(
        self,
        task_id: str,
        prompt: str,
        task_type: str = "dream",
        timeout_seconds: int = 300,
    ) -> None:
        """Execute a background task (dream, knowledge_fillin, …).

        No streamer, no semaphore.  The inner subprocess loop runs inside a
        child task so we can forcibly cancel it when the Superpos claim
        expires or the overall timeout fires — otherwise a silent Codex
        subprocess hangs the reader forever (TASK-stuck-dream scenario).
        """
        label = task_type.replace("_", " ")
        log.info("%s task %s starting in background", label.capitalize(), task_id)

        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None
        if self._superpos:
            progress_task = asyncio.create_task(
                self._report_progress(task_id, claim_expired)
            )

        full_text = ""

        async def _run_inner() -> None:
            nonlocal full_text
            cmd = self._build_codex_command(prompt=prompt)
            env = {**os.environ}
            if self._config.openai_api_key:
                env["OPENAI_API_KEY"] = self._config.openai_api_key

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=PIPE,
                stderr=PIPE,
                cwd=self._config.codex_working_dir,
                env=env,
                limit=16 * 1024 * 1024,
            )

            try:
                dedup = _EventDeduplicator()
                async for line in process.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = dedup.extract_text(event)
                    if text:
                        full_text += text
                await process.wait()
            finally:
                # Ensure the subprocess is reaped when the inner task is
                # cancelled (claim expiry, overall timeout).  Without this,
                # the codex subprocess can linger and its stdout pipe keeps
                # the reader attached indefinitely.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except (asyncio.TimeoutError, Exception):
                        pass

        inner_task: asyncio.Task | None = None
        watcher_task: asyncio.Task | None = None

        async def _watch_claim_expiry() -> None:
            await claim_expired.wait()
            if inner_task is not None and not inner_task.done():
                inner_task.cancel()

        expired = False
        timed_out = False
        try:
            inner_task = asyncio.create_task(_run_inner())
            watcher_task = asyncio.create_task(_watch_claim_expiry())
            try:
                await asyncio.wait_for(inner_task, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                log.warning(
                    "%s task %s timed out after %ds — cancelling",
                    label.capitalize(), task_id, timeout_seconds,
                )
                inner_task.cancel()
                try:
                    await inner_task
                except (asyncio.CancelledError, Exception):
                    pass
            except asyncio.CancelledError:
                if claim_expired.is_set():
                    expired = True
                    log.warning(
                        "%s task %s cancelled: claim expired", label.capitalize(), task_id,
                    )
                else:
                    raise

            if expired:
                return

            if timed_out:
                if self._superpos and not claim_expired.is_set():
                    try:
                        await self._superpos.fail_task(
                            task_id, f"{label.capitalize()} timed out after {timeout_seconds}s",
                        )
                    except Exception:
                        log.debug("Failed to mark timed-out task %s", task_id)
                return

            result = full_text[-2000:] if len(full_text) > 2000 else full_text
            summary = {
                "description": f"{label.capitalize()}: automated background task",
                "output_excerpt": full_text[:500] if full_text else None,
            }
            if self._superpos and not claim_expired.is_set():
                await self._superpos.complete_task(task_id, result, summary=summary)
            log.info("%s task %s completed", label.capitalize(), task_id)
        except Exception:
            log.warning("%s task %s failed", label.capitalize(), task_id, exc_info=True)
            if self._superpos and not claim_expired.is_set():
                try:
                    await self._superpos.fail_task(task_id, f"{label.capitalize()} failed")
                except Exception:
                    pass
        finally:
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass

    def _build_codex_command(
        self,
        prompt: str,
        session_id: str | None = None,
        cwd: str | None = None,
        system_prompt_append: str | None = None,
    ) -> list[str]:
        """Build the codex CLI command list."""
        # Prepend system_prompt_append to the user prompt
        full_prompt = prompt
        if system_prompt_append:
            full_prompt = f"{system_prompt_append}\n\n---\n\n{prompt}"

        if session_id:
            # Resume an existing session
            cmd = [
                "codex", "exec", "resume",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
            ]
            if self._runtime.model:
                cmd.extend(["--model", self._runtime.model])
            if self._runtime.effort:
                cmd.extend(["-c", f"model_reasoning_effort={self._runtime.effort}"])
            cmd.extend([session_id, full_prompt])
        else:
            cmd = [
                "codex", "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
            ]
            if self._runtime.model:
                cmd.extend(["--model", self._runtime.model])
            if self._runtime.effort:
                cmd.extend(["-c", f"model_reasoning_effort={self._runtime.effort}"])
            cmd.append(full_prompt)
        return cmd

    async def _execute_inner(
        self, req: ExecutionRequest, streamer: TelegramStreamer, retries: int,
    ) -> None:
        t0 = time.monotonic()
        full_text = ""

        # Resolve worktree cwd for tasks that carry an explicit branch
        cwd_override: str | None = None
        if (
            req.branch
            and self._config.codex_worktree_isolation
            and is_git_repo(self._config.codex_working_dir)
        ):
            try:
                cwd_override = await ensure_worktree(
                    self._config.codex_working_dir, req.branch
                )
            except Exception:
                log.warning(
                    "Failed to create worktree for branch %r; falling back to default cwd",
                    req.branch,
                    exc_info=True,
                )

        # Inject worktree instructions for requests without an explicit branch
        system_prompt_append: str | None = None
        if (
            not req.branch
            and self._config.codex_worktree_isolation
            and is_git_repo(self._config.codex_working_dir)
        ):
            wt_base = self._config.codex_working_dir
            system_prompt_append = (
                "## Worktree Isolation\n"
                "When this task requires implementing code changes on a new branch:\n"
                f"1. First run `git -C {wt_base} fetch origin` to get latest refs.\n"
                f"2. Choose a branch name, then: `git worktree add {wt_base}/.worktrees/<branch> -b <branch> origin/main`\n"
                f"3. Do all file edits and git operations inside `{wt_base}/.worktrees/<branch>`\n"
                "4. Commit, push the branch, and open a PR from the worktree.\n"
                "IMPORTANT: Always branch from origin/main to avoid inheriting unrelated in-progress work.\n"
                "NEVER create branches from the current HEAD of the main workspace — it may be on an unmerged feature branch.\n"
                "For conversational replies or read-only tasks, skip this entirely."
            )

        # Telegram messages resume the chat session; Superpos tasks run fresh.
        # For Telegram, also prime the session with summaries of recent
        # Superpos tasks the user saw notifications about in this chat —
        # those tasks ran in isolated sessions, so the conversation has no
        # memory of them otherwise.
        resume_id = None
        if req.source == "telegram":
            resume_id = self._sessions.get(req.chat_id)
            recent = self._recent_tasks.render(req.chat_id)
            if recent:
                system_prompt_append = (
                    f"{system_prompt_append}\n\n{recent}"
                    if system_prompt_append else recent
                )

        effective_cwd = cwd_override or self._config.codex_working_dir

        # Prepend image references so Codex reads them via the Read tool
        prompt_text = req.prompt
        if req.image_paths:
            image_refs = "\n".join(f"- {p}" for p in req.image_paths)
            prompt_text = (
                f"The user sent these images. Read them first, then respond.\n"
                f"{image_refs}\n\n{prompt_text}"
            )

        # Cap prompt size — the CLI passes it as a positional arg,
        # which must fit in ARG_MAX (~2MB, often less in containers).
        _MAX_PROMPT = 500_000  # 500KB safe limit
        if len(prompt_text) > _MAX_PROMPT:
            log.warning("Prompt too large (%dKB), truncating", len(prompt_text) // 1024)
            prompt_text = prompt_text[:_MAX_PROMPT] + "\n... (truncated)"

        for attempt in range(1, retries + 1):
            try:
                cmd = self._build_codex_command(
                    prompt=prompt_text,
                    session_id=resume_id,
                    cwd=effective_cwd,
                    system_prompt_append=system_prompt_append,
                )

                env = {**os.environ}
                if self._config.openai_api_key:
                    env["OPENAI_API_KEY"] = self._config.openai_api_key

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=PIPE,
                    stderr=PIPE,
                    cwd=effective_cwd,
                    env=env,
                    limit=16 * 1024 * 1024,  # 16 MB — Codex JSONL events can contain large diffs
                )

                stderr_chunks: list[bytes] = []
                json_errors: list[str] = []

                log.debug("Running codex command: %s (cwd=%s)", cmd, effective_cwd)

                # Read stdout in a coroutine so we can apply a post-exit
                # timeout if the process dies but grandchild pipes stay open.
                async def _drain_stdout():
                    nonlocal full_text
                    dedup = _EventDeduplicator()
                    async for line in process.stdout:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            log.debug("Non-JSON line from codex: %s", line[:200])
                            continue

                        # Capture error events from JSON stream
                        if event.get("type") == "error":
                            json_errors.append(event.get("message", ""))
                        if event.get("type") == "turn.failed":
                            err_info = event.get("error", {})
                            if isinstance(err_info, dict):
                                json_errors.append(err_info.get("message", ""))

                        # Extract session ID
                        sid = self._extract_session_id(event)
                        if sid and req.source == "telegram":
                            self._sessions.set(req.chat_id, sid)

                        text = dedup.extract_text(event)
                        if text:
                            full_text += text
                            await streamer.append(text)

                        tool_info = dedup.extract_tool_use(event)
                        if tool_info:
                            await streamer.send_tool_notification(*tool_info)

                drain_task = asyncio.create_task(_drain_stdout())
                wait_task = asyncio.create_task(process.wait())

                # Wait for both drain and process exit with an overall
                # execution timeout (default 20 min).  Prevents runaway
                # tasks from blocking the queue indefinitely.
                _MAX_EXECUTION_SECS = 30 * 60
                try:
                    done, pending = await asyncio.wait_for(
                        asyncio.wait(
                            [drain_task, wait_task],
                            return_when=asyncio.ALL_COMPLETED,
                        ),
                        timeout=_MAX_EXECUTION_SECS,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "Codex execution timed out after %ds — killing process (pid=%s)",
                        _MAX_EXECUTION_SECS, process.pid,
                    )
                    # Kill process FIRST so pipes close, then cancel tasks
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    pending = {drain_task, wait_task}

                for p in pending:
                    if not p.done():
                        p.cancel()
                        try:
                            await asyncio.wait_for(p, timeout=5)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass

                # Ensure process is reaped
                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass

                # Read remaining stderr — with timeout
                try:
                    stderr_data = await asyncio.wait_for(
                        process.stderr.read(), timeout=10,
                    )
                    if stderr_data:
                        stderr_chunks.append(stderr_data)
                except asyncio.TimeoutError:
                    log.warning("Timed out reading stderr from codex process")
                stderr_str = b"".join(stderr_chunks).decode(errors="replace")

                # Combine stderr and any JSON error events
                if not stderr_str.strip() and json_errors:
                    stderr_str = " | ".join(filter(None, json_errors))

                if process.returncode != 0:
                    raise _CodexProcessError(process.returncode, stderr_str)

                await streamer.finish()

                # Complete Superpos task if applicable
                if req.source == "superpos" and req.superpos_task_id and self._superpos:
                    result = full_text[-2000:] if len(full_text) > 2000 else full_text
                    elapsed = int(time.monotonic() - t0)
                    summary = {
                        "description": req.prompt[:200],
                        "output_excerpt": full_text[:500] if full_text else None,
                        "duration_seconds": elapsed,
                    }
                    try:
                        await self._superpos.complete_task(
                            req.superpos_task_id, result, summary=summary,
                        )
                    except Exception:
                        log.warning(
                            "Failed to complete superpos task %s — claim may have expired",
                            req.superpos_task_id, exc_info=True,
                        )
                    # Record regardless of complete_task outcome — the work
                    # ran, the user saw the streamed output, and a future
                    # Telegram follow-up should still have the context.
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="succeeded",
                            detail=full_text[:500] if full_text else "",
                        ),
                    )
                return

            except _CodexProcessError as e:
                err_str = str(e)
                is_rate_limit = (
                    "rate_limit" in err_str.lower()
                    or "rate limit" in err_str.lower()
                    or "at capacity" in err_str.lower()
                    or "overloaded" in err_str.lower()
                )
                is_auth_error = (
                    "authentication" in err_str.lower()
                    or "invalid api key" in err_str.lower()
                    or "unauthorized" in err_str.lower()
                    or "invalid_api_key" in err_str.lower()
                )

                if is_auth_error:
                    log.critical(
                        "Codex authentication failed — API key invalid or not configured. "
                        "Shutting down."
                    )
                    sys.exit(1)

                # Transient API errors (500, overloaded) — retry with backoff
                is_api_500 = (
                    "internal server error" in err_str.lower()
                    or "api_error" in err_str.lower()
                    or "overloaded" in err_str.lower()
                )
                if is_api_500 and attempt < retries:
                    wait = 30 * attempt
                    log.warning(
                        "API server error (attempt %d/%d), retrying in %ds: %s",
                        attempt, retries, wait, err_str[:100],
                    )
                    await streamer.append(f"\n\u23f3 API error, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue

                # Don't retry if execution already produced output — side
                # effects (GitHub comments, commits, etc.) cannot be undone.
                if full_text.strip():
                    log.warning(
                        "Execution produced output but failed (attempt %d/%d); "
                        "not retrying to avoid duplicate side effects",
                        attempt, retries,
                    )
                elif is_rate_limit and attempt < retries:
                    wait = 30 * attempt
                    log.warning("Rate limited (attempt %d/%d), retrying in %ds", attempt, retries, wait)
                    await streamer.append(f"\n\u23f3 Rate limited, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue
                # If resume failed (stale session), retry without resume
                elif resume_id and attempt < retries:
                    log.warning("Session resume failed, retrying with fresh session")
                    self._sessions.clear(req.chat_id)
                    resume_id = None
                    continue

                log.error("Codex process error (exit %d): %s", e.returncode, e.stderr)
                try:
                    await streamer.error(f"Error: {e}")
                except asyncio.CancelledError:
                    log.warning("CancelledError while sending error to Telegram (suppressed)")
                except Exception:
                    log.warning("Failed to send error notification", exc_info=True)
                if req.source == "superpos" and req.superpos_task_id and self._superpos:
                    elapsed = int(time.monotonic() - t0)
                    summary = {
                        "description": req.prompt[:200],
                        "error": err_str[:500],
                        "duration_seconds": elapsed,
                    }
                    try:
                        await self._superpos.fail_task(
                            req.superpos_task_id, err_str, summary=summary,
                        )
                    except Exception:
                        log.warning("Failed to mark superpos task %s as failed", req.superpos_task_id)
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="failed",
                            detail=err_str[:500],
                        ),
                    )
                return

            except Exception as e:
                err_str = str(e)
                log.exception("Unexpected error during execution")
                try:
                    await streamer.error(f"Error: {e}")
                except asyncio.CancelledError:
                    log.warning("CancelledError while sending error to Telegram (suppressed)")
                except Exception:
                    log.warning("Failed to send error notification", exc_info=True)
                if req.source == "superpos" and req.superpos_task_id and self._superpos:
                    elapsed = int(time.monotonic() - t0)
                    summary = {
                        "description": req.prompt[:200],
                        "error": err_str[:500],
                        "duration_seconds": elapsed,
                    }
                    try:
                        await self._superpos.fail_task(
                            req.superpos_task_id, err_str, summary=summary,
                        )
                    except Exception:
                        log.warning("Failed to mark superpos task %s as failed", req.superpos_task_id)
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="failed",
                            detail=err_str[:500],
                        ),
                    )
                return

    @staticmethod
    def _extract_text(event: dict) -> str:
        """Extract assistant text from a Codex JSONL event."""
        etype = event.get("type", "")

        # Message event with content blocks
        if etype == "message" and event.get("role") == "assistant":
            parts = []
            for block in event.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)

        # Delta/streaming events
        if etype in ("response.output_text.delta", "content_block_delta"):
            return event.get("delta", event.get("text", ""))

        # Simple text output event
        if etype == "text" and "text" in event:
            return event["text"]

        # item.completed with agent_message
        if etype == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                return item.get("text", "")

        return ""

    @staticmethod
    def _extract_tool_use(event: dict) -> tuple[str, object] | None:
        """Extract tool use info from a Codex JSONL event."""
        etype = event.get("type", "")

        # Function call events
        if etype == "function_call":
            name = event.get("name", "unknown")
            args = event.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            return (name, args)

        # Tool call style events
        if etype in ("tool_call", "tool_use"):
            name = event.get("name", event.get("function", {}).get("name", "unknown"))
            args = event.get("input", event.get("arguments", event.get("function", {}).get("arguments", {})))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            return (name, args)

        # Nested in item
        item = event.get("item", {})
        if isinstance(item, dict) and item.get("type") in ("function_call", "tool_call", "tool_use"):
            name = item.get("name", "unknown")
            args = item.get("arguments", item.get("input", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            return (name, args)

        # Codex CLI command_execution events (item.started)
        if etype == "item.started" and isinstance(item, dict) and item.get("type") == "command_execution":
            cmd = item.get("command", "")
            # Strip the /bin/bash -lc wrapper if present
            if cmd.startswith("/bin/bash -lc '") and cmd.endswith("'"):
                cmd = cmd[15:-1]
            elif cmd.startswith("/bin/bash -lc "):
                cmd = cmd[14:]
            return ("shell", {"command": cmd})

        return None

    @staticmethod
    def _extract_session_id(event: dict) -> str | None:
        """Extract session/thread ID from a Codex JSONL event."""
        # Check common locations for session/thread ID
        for key in ("session_id", "thread_id", "id"):
            val = event.get(key)
            if val and isinstance(val, str):
                # Only use 'id' if it looks like a session/thread ID
                if key == "id" and event.get("type") not in ("response.completed", "session", "response"):
                    continue
                return val
        return None


class _EventDeduplicator:
    """Filters duplicate text and tool events from the Codex CLI JSONL stream.

    The Codex CLI emits overlapping events: streaming deltas AND completed
    message/item summaries containing the same text.  Similarly, a single
    tool invocation can fire multiple event types.  This class filters
    duplicates so each piece of content triggers only one Telegram API call.
    """

    def __init__(self) -> None:
        self._saw_delta: bool = False
        self._seen_tool_keys: set[str] = set()

    def extract_text(self, event: dict) -> str:
        """Extract text, preferring deltas and skipping duplicate completed messages."""
        etype = event.get("type", "")

        # New response turn resets delta tracking
        if etype in ("response.created", "response.started"):
            self._saw_delta = False
            return ""

        # Streaming deltas — always use, mark that we're receiving them
        if etype in ("response.output_text.delta", "content_block_delta"):
            self._saw_delta = True
            return event.get("delta", event.get("text", ""))

        # Simple text event (also streaming)
        if etype == "text" and "text" in event:
            self._saw_delta = True
            return event["text"]

        # Completed message — only use as fallback when no deltas were seen
        if etype == "message" and event.get("role") == "assistant":
            if self._saw_delta:
                return ""
            parts = []
            for block in event.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)

        # item.completed with agent_message — skip if deltas were seen
        if etype == "item.completed":
            if self._saw_delta:
                return ""
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                return item.get("text", "")

        return ""

    def extract_tool_use(self, event: dict) -> tuple[str, object] | None:
        """Extract tool use, deduplicating by call ID or name+args."""
        etype = event.get("type", "")

        name: str | None = None
        args: object = {}
        call_id = ""

        if etype == "function_call":
            name = event.get("name", "unknown")
            args = event.get("arguments", {})
            call_id = event.get("call_id", event.get("id", ""))
        elif etype in ("tool_call", "tool_use"):
            name = event.get("name", event.get("function", {}).get("name", "unknown"))
            args = event.get("input", event.get("arguments", event.get("function", {}).get("arguments", {})))
            call_id = event.get("call_id", event.get("id", ""))
        elif etype == "item.started":
            # Only match command_execution — shell commands are exclusively
            # reported via item.started, not via function_call.
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "command_execution":
                cmd = item.get("command", "")
                if cmd.startswith("/bin/bash -lc '") and cmd.endswith("'"):
                    cmd = cmd[15:-1]
                elif cmd.startswith("/bin/bash -lc "):
                    cmd = cmd[14:]
                name = "shell"
                args = {"command": cmd}
                call_id = item.get("call_id", item.get("id", ""))
            else:
                return None
        else:
            # Skip nested-item wrappers for function_call/tool_call/tool_use —
            # these duplicate the top-level events handled above.
            return None

        if name is None:
            return None

        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw": args}

        # Deduplicate by call_id when available, else by name + args prefix
        if call_id:
            dedup_key = f"id:{call_id}"
        else:
            args_str = str(args)[:200]
            dedup_key = f"na:{name}:{args_str}"

        if dedup_key in self._seen_tool_keys:
            return None
        self._seen_tool_keys.add(dedup_key)

        return (name, args)


class _CodexProcessError(Exception):
    """Raised when the codex subprocess exits with non-zero status."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"codex process exited with code {returncode}: {stderr[:500]}")
