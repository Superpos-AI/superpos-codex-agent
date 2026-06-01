"""Queue-based worker that invokes OpenAI Codex CLI and routes output.

This is Codex's concrete :class:`superpos_agent_core.Executor` subclass.  Core
modules (``superpos_poller``, ``telegram_bot``, ``run_agent``) drive every
agent through the abstract Executor surface; the Codex-specific bits live
here: subprocess management, JSONL stream parsing, persona injection via
AGENTS.md, codex MCP config materialization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from asyncio.subprocess import PIPE
from pathlib import Path

from superpos_agent_core import (
    ExecutionRequest,
    Executor,
    SessionStore,
    SuperposClient,
    TelegramGateway,
    TelegramStreamer,
    collect_mcp_servers,
    discover_modules,
    ensure_worktree,
    is_git_repo,
    report_progress,
    worktree_path,
)

from .config import CodexConfig
from .runtime_config import CodexRuntimeConfig

log = logging.getLogger(__name__)


# Cap prompt size — the CLI passes it as a positional arg, which must fit in
# ARG_MAX (~2MB, often less in containers).
_MAX_PROMPT = 500_000  # 500KB safe limit

_PERSONA_BEGIN = "<!-- PERSONA:BEGIN -->"
_PERSONA_END = "<!-- PERSONA:END -->"


class CodexExecutor(Executor):
    """Concrete executor that drives OpenAI's Codex CLI via subprocess."""

    def __init__(
        self,
        config: CodexConfig,
        runtime: CodexRuntimeConfig,
        superpos: SuperposClient | None,
        gateway: TelegramGateway | None,
        persona: str | None = None,
    ) -> None:
        super().__init__(max_parallel=config.executor_max_parallel)
        self._config = config
        self._runtime = runtime
        self._superpos = superpos
        self._gateway = gateway
        self._persona = persona
        self._sessions = SessionStore(
            path=os.path.join(config.home_dir, "session_store.json"),
        )
        self._semaphore = asyncio.Semaphore(config.executor_max_parallel)
        self._worktree_locks: dict[str, asyncio.Lock] = {}

        modules = discover_modules(config.modules_dir)
        mcp = collect_mcp_servers(modules)
        if mcp:
            log.info("Loaded %d MCP server(s) from %s", len(mcp), config.modules_dir)
            self._write_mcp_config(mcp)

        self._inject_persona_into_agents_md()

    # ── Persona injection (Codex-specific: via AGENTS.md, not CLI flags) ─────

    def _inject_persona_into_agents_md(self) -> None:
        """Prepend persona to AGENTS.md so Codex picks it up as system prompt."""
        if not self._persona:
            return
        agents_md = os.path.join(self._config.executor_working_dir, "AGENTS.md")
        existing = ""
        if os.path.exists(agents_md):
            with open(agents_md, "r") as f:
                existing = f.read()
        persona_block = (
            f"{_PERSONA_BEGIN}\n"
            f"{self._persona}\n"
            f"{_PERSONA_END}\n\n"
        )
        if _PERSONA_BEGIN in existing:
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

    @staticmethod
    def _write_mcp_config(mcp_servers: dict) -> None:
        """Write MCP server configuration to ~/.codex/config.json."""
        config_path = Path.home() / ".codex" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        existing["mcpServers"] = mcp_servers
        config_path.write_text(json.dumps(existing, indent=2))

    # ── Abstract method impls ──────────────────────────────────────────────

    def update_persona(self, prompt: str | None, version: int | None = None) -> None:
        """Replace the persona and re-inject into AGENTS.md.

        ``version`` is accepted for API compatibility with the core poller,
        but Codex doesn't track persona versions — each execution re-reads
        AGENTS.md, so a fresh persona takes effect on the next message
        without needing to invalidate sessions.
        """
        self._persona = prompt
        self._inject_persona_into_agents_md()

    def clear_session(self, chat_id: int | str) -> None:
        """Clear the stored session for a chat, starting fresh next message."""
        self._sessions.clear(chat_id)

    async def run(self) -> None:
        log.info(
            "Codex executor started (max_parallel=%d)",
            self._config.executor_max_parallel,
        )
        while True:
            req = await self.queue.get()
            asyncio.create_task(self._run_one(req))

    # ── Optional hooks ─────────────────────────────────────────────────────

    async def preflight(self) -> None:
        """Verify Codex CLI auth by making a minimal call.

        Core's ``run_agent`` marks the agent ``online`` in Superpos *before*
        invoking preflight.  If we exit/raise here, the asyncio.gather()
        ``finally`` that flips status back to ``offline`` never runs, and
        Superpos keeps advertising us as online until the heartbeat timeout
        fires.  Wrap the whole probe so *every* failure path — auth error,
        missing CLI, non-zero exit, unexpected exception — flips offline
        first.
        """
        log.info("Verifying Codex authentication...")
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    "codex", "exec", "--json",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--skip-git-repo-check",
                    "hi",
                    stdout=PIPE,
                    stderr=PIPE,
                    env={**os.environ},
                )
            except FileNotFoundError:
                await self._mark_offline_best_effort()
                log.critical(
                    "'codex' CLI not found on PATH. Install with: "
                    "npm install -g @openai/codex"
                )
                sys.exit(1)

            try:
                _, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=60,
                )
            except asyncio.TimeoutError:
                log.warning("Codex auth check timed out (60s) — proceeding anyway")
                return
            if process.returncode != 0:
                stderr_str = stderr.decode(errors="replace")
                # Flip offline BEFORE deciding how to surface the failure —
                # both the auth-error sys.exit() path and the generic
                # RuntimeError re-raise leave the asyncio.gather() finally
                # unreached otherwise.
                await self._mark_offline_best_effort()
                if _is_auth_error(stderr_str):
                    print(_AUTH_HELP_INVALID_KEY, file=sys.stderr)
                    sys.exit(1)
                raise RuntimeError(
                    f"Codex auth check failed (exit {process.returncode}): "
                    f"{stderr_str[:500]}"
                )
            log.info("Codex authentication OK")
        except SystemExit:
            raise
        except Exception:
            # Catch-all guard so an unexpected probe error (subprocess crash,
            # OSError, etc.) still flips status offline before the exception
            # bubbles up to run_agent's sys.exit(1).
            await self._mark_offline_best_effort()
            raise

    async def _mark_offline_best_effort(self) -> None:
        if not self._superpos:
            return
        try:
            await self._superpos.update_status("offline")
            log.info("Agent status set to offline (preflight failure)")
        except Exception:
            log.debug(
                "Failed to flip status offline during preflight cleanup",
                exc_info=True,
            )

    def cleanup_stale_sessions(self, max_age_hours: int = 24) -> dict[str, int]:
        """Remove old Codex session data while preserving active resumes."""
        counts = {"projects": 0, "session_env": 0, "bytes_freed": 0}
        cutoff = time.time() - (max_age_hours * 3600)
        preserve: set[str] = self._sessions.active_session_ids()

        codex_dir = os.path.join(os.environ.get("HOME", "/tmp"), ".codex")

        def _session_id_from_name(name: str) -> str:
            return name[:-6] if name.endswith(".jsonl") else name

        # Codex CLI writes sessions under projects/<encoded-cwd>/<sid>/ —
        # scan every project dir so worktree-scoped sessions get cleaned too.
        projects_root = os.path.join(codex_dir, "projects")
        if os.path.isdir(projects_root):
            for project_name in os.listdir(projects_root):
                projects_dir = os.path.join(projects_root, project_name)
                if not os.path.isdir(projects_dir):
                    continue
                for name in os.listdir(projects_dir):
                    path = os.path.join(projects_dir, name)
                    if not os.path.isdir(path):
                        continue
                    if _session_id_from_name(name) in preserve:
                        continue
                    try:
                        mtime = os.path.getmtime(path)
                        if mtime < cutoff:
                            size = sum(
                                os.path.getsize(os.path.join(dp, f))
                                for dp, _, fns in os.walk(path)
                                for f in fns
                            )
                            shutil.rmtree(path)
                            counts["projects"] += 1
                            counts["bytes_freed"] += size
                    except OSError:
                        pass

        session_env_dir = os.path.join(codex_dir, "session-env")
        if os.path.isdir(session_env_dir):
            for name in os.listdir(session_env_dir):
                path = os.path.join(session_env_dir, name)
                if not os.path.isdir(path):
                    continue
                if _session_id_from_name(name) in preserve:
                    continue
                try:
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        size = sum(
                            os.path.getsize(os.path.join(dp, f))
                            for dp, _, fns in os.walk(path)
                            for f in fns
                        )
                        shutil.rmtree(path)
                        counts["session_env"] += 1
                        counts["bytes_freed"] += size
                except OSError:
                    pass

        if preserve:
            log.info(
                "cleanup_stale_sessions: preserved %d active session(s)",
                len(preserve),
            )
        return counts

    # ── Worktree slot management ───────────────────────────────────────────

    def _get_worktree_lock(self, slot: str) -> asyncio.Lock:
        if slot not in self._worktree_locks:
            self._worktree_locks[slot] = asyncio.Lock()
        return self._worktree_locks[slot]

    def _resolve_slot(self, req: ExecutionRequest) -> str:
        if (
            req.branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            return worktree_path(self._config.executor_working_dir, req.branch)
        return "__main__"

    # ── Main consumer loop ─────────────────────────────────────────────────

    async def _run_one(self, req: ExecutionRequest) -> None:
        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None

        # Start heartbeat IMMEDIATELY — before semaphore/worktree waits.
        # This keeps the server-side claim alive while queued.
        if req.source == "superpos" and req.superpos_task_id and self._superpos:
            progress_task = asyncio.create_task(
                report_progress(self._superpos, req.superpos_task_id, claim_expired)
            )

        try:
            async with self._semaphore:
                if claim_expired.is_set():
                    log.warning(
                        "Claim expired while waiting for semaphore: %s",
                        req.superpos_task_id,
                    )
                    return

                slot = self._resolve_slot(req)
                wt_lock = self._get_worktree_lock(slot)

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
                        if lock_task in done and lock_task.result():
                            wt_lock.release()
                        log.warning(
                            "Claim expired while waiting for worktree lock: %s",
                            req.superpos_task_id,
                        )
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
            self._track_chat_task(req.chat_id, inner_task)
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
            try:
                await streamer.finish()
            except Exception:
                log.debug("Streamer finish failed (non-fatal)", exc_info=True)
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

    # ── Background tasks ───────────────────────────────────────────────────

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
        subprocess hangs the reader forever.
        """
        label = task_type.replace("_", " ")
        log.info("%s task %s starting in background", label.capitalize(), task_id)

        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None
        if self._superpos:
            progress_task = asyncio.create_task(
                report_progress(self._superpos, task_id, claim_expired)
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
                cwd=self._config.executor_working_dir,
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
                        "%s task %s cancelled: claim expired",
                        label.capitalize(), task_id,
                    )
                else:
                    raise

            if expired:
                return

            if timed_out:
                if self._superpos and not claim_expired.is_set():
                    try:
                        await self._superpos.fail_task(
                            task_id,
                            f"{label.capitalize()} timed out after {timeout_seconds}s",
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

    # ── Command construction & inner execute ───────────────────────────────

    def _build_codex_command(
        self,
        prompt: str,
        session_id: str | None = None,
        cwd: str | None = None,
        system_prompt_append: str | None = None,
    ) -> list[str]:
        """Build the codex CLI command list."""
        full_prompt = prompt
        if system_prompt_append:
            full_prompt = f"{system_prompt_append}\n\n---\n\n{prompt}"

        if session_id:
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
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            try:
                cwd_override = await ensure_worktree(
                    self._config.executor_working_dir, req.branch,
                )
            except Exception:
                log.warning(
                    "Failed to create worktree for branch %r; falling back to default cwd",
                    req.branch, exc_info=True,
                )

        # Inject worktree instructions for requests without an explicit branch
        system_prompt_append: str | None = None
        if (
            not req.branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            wt_base = self._config.executor_working_dir
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

        # Telegram messages resume the chat session; Superpos tasks run fresh
        resume_id = None
        if req.source == "telegram":
            resume_id = self._sessions.get(req.chat_id)

        effective_cwd = cwd_override or self._config.executor_working_dir

        prompt_text = req.prompt
        if req.image_paths:
            image_refs = "\n".join(f"- {p}" for p in req.image_paths)
            prompt_text = (
                f"The user sent these images. Read them first, then respond.\n"
                f"{image_refs}\n\n{prompt_text}"
            )

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
                    limit=16 * 1024 * 1024,
                )

                stderr_chunks: list[bytes] = []
                json_errors: list[str] = []

                log.debug("Running codex command: %s (cwd=%s)", cmd, effective_cwd)

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

                        if event.get("type") == "error":
                            json_errors.append(event.get("message", ""))
                        if event.get("type") == "turn.failed":
                            err_info = event.get("error", {})
                            if isinstance(err_info, dict):
                                json_errors.append(err_info.get("message", ""))

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

                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass

                try:
                    stderr_data = await asyncio.wait_for(
                        process.stderr.read(), timeout=10,
                    )
                    if stderr_data:
                        stderr_chunks.append(stderr_data)
                except asyncio.TimeoutError:
                    log.warning("Timed out reading stderr from codex process")
                stderr_str = b"".join(stderr_chunks).decode(errors="replace")

                if not stderr_str.strip() and json_errors:
                    stderr_str = " | ".join(filter(None, json_errors))

                if process.returncode != 0:
                    raise _CodexProcessError(process.returncode, stderr_str)

                await streamer.finish()

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
                return

            except _CodexProcessError as e:
                err_str = str(e)
                is_rate_limit = (
                    "rate_limit" in err_str.lower()
                    or "rate limit" in err_str.lower()
                    or "at capacity" in err_str.lower()
                    or "overloaded" in err_str.lower()
                )

                if _is_auth_error(err_str):
                    log.critical(
                        "Codex authentication failed — API key invalid or not configured. "
                        "Shutting down."
                    )
                    sys.exit(1)

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
                    await streamer.append(f"\n⏳ API error, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue

                if full_text.strip():
                    log.warning(
                        "Execution produced output but failed (attempt %d/%d); "
                        "not retrying to avoid duplicate side effects",
                        attempt, retries,
                    )
                elif is_rate_limit and attempt < retries:
                    wait = 30 * attempt
                    log.warning("Rate limited (attempt %d/%d), retrying in %ds", attempt, retries, wait)
                    await streamer.append(f"\n⏳ Rate limited, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue
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
                return

    # ── Message parsing ────────────────────────────────────────────────────

    @staticmethod
    def _extract_text(event: dict) -> str:
        """Extract assistant text from a Codex JSONL event."""
        etype = event.get("type", "")

        if etype == "message" and event.get("role") == "assistant":
            parts = []
            for block in event.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)

        if etype in ("response.output_text.delta", "content_block_delta"):
            return event.get("delta", event.get("text", ""))

        if etype == "text" and "text" in event:
            return event["text"]

        if etype == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                return item.get("text", "")

        return ""

    @staticmethod
    def _extract_tool_use(event: dict) -> tuple[str, object] | None:
        """Extract tool use info from a Codex JSONL event."""
        etype = event.get("type", "")

        if etype == "function_call":
            name = event.get("name", "unknown")
            args = event.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            return (name, args)

        if etype in ("tool_call", "tool_use"):
            name = event.get("name", event.get("function", {}).get("name", "unknown"))
            args = event.get("input", event.get("arguments", event.get("function", {}).get("arguments", {})))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            return (name, args)

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

        if etype == "item.started" and isinstance(item, dict) and item.get("type") == "command_execution":
            cmd = item.get("command", "")
            if cmd.startswith("/bin/bash -lc '") and cmd.endswith("'"):
                cmd = cmd[15:-1]
            elif cmd.startswith("/bin/bash -lc "):
                cmd = cmd[14:]
            return ("shell", {"command": cmd})

        return None

    @staticmethod
    def _extract_session_id(event: dict) -> str | None:
        """Extract session/thread ID from a Codex JSONL event."""
        for key in ("session_id", "thread_id", "id"):
            val = event.get(key)
            if val and isinstance(val, str):
                if key == "id" and event.get("type") not in ("response.completed", "session", "response"):
                    continue
                return val
        return None


# ── Helpers ────────────────────────────────────────────────────────────────


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

        if etype in ("response.created", "response.started"):
            self._saw_delta = False
            return ""

        if etype in ("response.output_text.delta", "content_block_delta"):
            self._saw_delta = True
            return event.get("delta", event.get("text", ""))

        if etype == "text" and "text" in event:
            self._saw_delta = True
            return event["text"]

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
            return None

        if name is None:
            return None

        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw": args}

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


def _is_auth_error(err: str) -> bool:
    lower = err.lower()
    return (
        "authentication" in lower
        or "invalid api key" in lower
        or "unauthorized" in lower
        or "invalid_api_key" in lower
    )


_AUTH_HELP_INVALID_KEY = """
╔══════════════════════════════════════════════════════════════╗
║         Codex authentication failed — cannot start          ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Option 1 — OAuth (codex login):                             ║
║                                                              ║
║    docker run -it \\                                         ║
║      -v codex_auth:/home/agent/.codex \\                     ║
║      --entrypoint codex superpos-codex-agent login           ║
║                                                              ║
║    Follow the prompts to authenticate.                       ║
║    Then restart the agent (keep the -v flag).                ║
║                                                              ║
║  Option 2 — API key:                                         ║
║                                                              ║
║    Set OPENAI_API_KEY=sk-... in your .env file.              ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""
