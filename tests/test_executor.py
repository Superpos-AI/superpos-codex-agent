import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

from superpos_agent_core import ExecutionRequest
from superpos_agent_codex.codex_executor import CodexExecutor, _EventDeduplicator


# --- Dedup method unit tests (pure sync logic) ---

def test_has_task_initially_false(executor):
    assert not executor.has_superpos_task("abc")


def test_add_then_has(executor):
    executor.add_superpos_task("abc")
    assert executor.has_superpos_task("abc")


def test_remove_clears_task(executor):
    executor.add_superpos_task("abc")
    executor.remove_superpos_task("abc")
    assert not executor.has_superpos_task("abc")


def test_remove_nonexistent_is_safe(executor):
    executor.remove_superpos_task("nonexistent")  # must not raise


# Note: report_progress (the heartbeat coroutine) lives in
# `superpos_agent_core.progress_reporter` now.  Its unit tests live next to it
# in core (`tests/test_progress_reporter.py`); we don't re-test the contract
# here.  Only behaviour that depends on *this* executor's wiring is kept.


# --- Claim expiry removes task from in-flight set ---

async def test_execute_removes_task_after_claim_expiry(executor):
    executor.add_superpos_task("task-x")

    async def fake_report_progress(client, task_id, claim_expired):
        claim_expired.set()

    async def fake_execute_inner(req, streamer, retries):
        await asyncio.sleep(10)  # blocks until cancelled

    req = ExecutionRequest(
        prompt="hello", chat_id="123", source="superpos", superpos_task_id="task-x"
    )

    # Patch the *imported* name in the executor module — `report_progress`
    # is a free function from core, not a method on the executor, so
    # `patch.object(executor, ...)` no longer applies.
    with patch("superpos_agent_codex.codex_executor.report_progress", fake_report_progress), \
         patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        # Put on queue so task_done() in _run_one works correctly
        await executor.queue.put(req)
        await executor.queue.get()  # simulate run() pulling from queue
        await asyncio.wait_for(executor._run_one(req), timeout=2.0)

    assert not executor.has_superpos_task("task-x")


# --- run_background() report_progress wiring ---

async def test_run_background_reacts_to_claim_expired(executor, mock_superpos):
    """run_background() must pass (client, task_id, claim_expired) to the
    imported report_progress helper, and when the claim expires it must cancel
    the inner subprocess loop and skip the complete_task / fail_task calls."""
    report_progress_called = {"args": None}

    async def fake_report_progress(client, task_id, claim_expired):
        report_progress_called["args"] = (client, task_id)
        await asyncio.sleep(0.05)
        claim_expired.set()
        # Keep coroutine alive until cancelled in the finally block
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])  # iteration ends immediately
    # Use a Future to model a process.wait() that blocks until kill() resolves it
    loop = asyncio.get_event_loop()
    wait_future: asyncio.Future = loop.create_future()

    async def fake_wait():
        return await wait_future

    mock_process.wait = AsyncMock(side_effect=fake_wait)
    mock_process.returncode = None

    def fake_kill():
        mock_process.returncode = 0
        if not wait_future.done():
            wait_future.set_result(0)

    mock_process.kill = MagicMock(side_effect=fake_kill)

    with patch("superpos_agent_codex.codex_executor.report_progress", fake_report_progress), \
         patch("asyncio.create_subprocess_exec", return_value=mock_process):
        await asyncio.wait_for(
            executor.run_background(
                "task-bg", "do stuff", task_type="dream", timeout_seconds=5,
            ),
            timeout=3.0,
        )

    # Wiring assertions: the imported report_progress was invoked with the
    # correct positional arguments (client, task_id).
    assert report_progress_called["args"] is not None
    assert report_progress_called["args"][0] is executor._superpos
    assert report_progress_called["args"][1] == "task-bg"

    # Cleanup assertions: claim expiry means we must not call complete/fail.
    mock_superpos.complete_task.assert_not_called()
    mock_superpos.fail_task.assert_not_called()


async def test_run_background_calls_complete_on_success(executor, mock_superpos):
    """When the subprocess exits cleanly, run_background() must call
    complete_task on the Superpos client — guarding the happy path."""

    async def fake_report_progress(client, task_id, claim_expired):
        # Idle until cancelled in finally — never set claim_expired.
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    events = [
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.output_text.delta", "delta": " world"},
    ]

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines(events)
    mock_process.wait = AsyncMock(return_value=0)
    mock_process.returncode = 0
    mock_process.kill = MagicMock()

    with patch("superpos_agent_codex.codex_executor.report_progress", fake_report_progress), \
         patch("asyncio.create_subprocess_exec", return_value=mock_process):
        await asyncio.wait_for(
            executor.run_background(
                "task-bg-ok", "do stuff", task_type="dream", timeout_seconds=5,
            ),
            timeout=3.0,
        )

    mock_superpos.complete_task.assert_called_once()
    args, _kwargs = mock_superpos.complete_task.call_args
    assert args[0] == "task-bg-ok"
    mock_superpos.fail_task.assert_not_called()


# --- _build_codex_command uses cwd override when provided ---

def test_build_codex_command_default(executor, mock_runtime):
    mock_runtime.model = "gpt-5.4"
    cmd = executor._build_codex_command("hello")
    assert "codex" == cmd[0]
    assert "exec" == cmd[1]
    assert "--json" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--model" in cmd
    assert "gpt-5.4" in cmd
    assert "hello" in cmd
    # prompt is a positional arg, not behind -p
    assert "-p" not in cmd


def test_build_codex_command_with_session_id(executor):
    cmd = executor._build_codex_command("hello", session_id="sess-123")
    assert "resume" in cmd
    assert "sess-123" in cmd


# --- _build_codex_command persona injection ---

def test_build_codex_command_persona_injected_via_agents_md(executor_with_persona):
    """Persona is injected into AGENTS.md at init time, not via CLI flags."""
    cmd = executor_with_persona._build_codex_command("hello")
    # Persona is NOT passed via --system-prompt (it's in AGENTS.md)
    assert "--system-prompt" not in cmd


def test_build_codex_command_no_system_prompt_when_persona_none(executor):
    cmd = executor._build_codex_command("hello")
    assert "--system-prompt" not in cmd


def test_build_codex_command_no_model_when_empty(executor, mock_runtime):
    mock_runtime.model = ""
    cmd = executor._build_codex_command("hello")
    assert "--model" not in cmd


def test_build_codex_command_prompt_is_positional(executor, mock_runtime):
    mock_runtime.model = ""
    cmd = executor._build_codex_command("hello world")
    # Prompt should be the last element as a positional arg
    assert cmd[-1] == "hello world"


# --- _execute_inner calls ensure_worktree when branch + isolation enabled ---

async def test_execute_inner_calls_ensure_worktree_for_superpos_with_branch(
    executor, mock_superpos, mock_config
):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    mock_config.openai_api_key = ""

    req = ExecutionRequest(
        prompt="review PR", chat_id="123", source="superpos",
        superpos_task_id="task-wt", branch="feature/my-branch",
    )

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=None)
    mock_process.returncode = 0

    with patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("superpos_agent_codex.codex_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("asyncio.create_subprocess_exec", return_value=mock_process), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feature-my-branch"
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_called_once_with("/workspace", "feature/my-branch")


# --- _execute_inner calls ensure_worktree for telegram source with explicit branch ---

async def test_execute_inner_telegram_with_explicit_branch_creates_worktree(
    executor, mock_config
):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    mock_config.openai_api_key = ""

    req = ExecutionRequest(
        prompt="hello", chat_id="123", source="telegram", branch="feature/x",
    )

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=None)
    mock_process.returncode = 0

    with patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("superpos_agent_codex.codex_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("asyncio.create_subprocess_exec", return_value=mock_process), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feature-x"
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_called_once_with("/workspace", "feature/x")


# --- _execute_inner skips worktree when isolation disabled ---

async def test_execute_inner_skips_worktree_when_isolation_disabled(executor, mock_config):
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"
    mock_config.openai_api_key = ""

    req = ExecutionRequest(
        prompt="do it", chat_id="123", source="superpos",
        superpos_task_id="task-no-wt", branch="some-branch",
    )

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=None)
    mock_process.returncode = 0

    with patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("superpos_agent_codex.codex_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("asyncio.create_subprocess_exec", return_value=mock_process), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_not_called()


# --- _execute_inner falls back gracefully when ensure_worktree fails ---

async def test_execute_inner_falls_back_when_worktree_fails(executor, mock_config):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    mock_config.openai_api_key = ""

    req = ExecutionRequest(
        prompt="do it", chat_id="123", source="superpos",
        superpos_task_id="task-fail", branch="bad-branch",
    )

    captured_cmds = []

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=None)
    mock_process.returncode = 0

    async def capture_exec(*args, **kwargs):
        captured_cmds.append((args, kwargs))
        return mock_process

    with patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("superpos_agent_codex.codex_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("asyncio.create_subprocess_exec", side_effect=capture_exec), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.side_effect = RuntimeError("git error")
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    # cwd should fall back to config default when worktree creation failed
    assert captured_cmds[0][1]["cwd"] == "/workspace"


# --- _execute_inner injects worktree hint for Telegram without branch ---

async def test_execute_inner_injects_worktree_hint_for_telegram_with_isolation(
    executor, mock_config
):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    mock_config.openai_api_key = ""

    req = ExecutionRequest(prompt="implement a feature", chat_id="123", source="telegram")

    captured_cmds = []

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=None)
    mock_process.returncode = 0

    async def capture_exec(*args, **kwargs):
        captured_cmds.append(args)
        return mock_process

    with patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("asyncio.create_subprocess_exec", side_effect=capture_exec), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    # Worktree hint is prepended to the prompt (last positional arg)
    cmd_args = list(captured_cmds[0])
    prompt_arg = cmd_args[-1]
    assert "Worktree Isolation" in prompt_arg
    assert "/workspace/.worktrees/<branch>" in prompt_arg


async def test_execute_inner_no_worktree_hint_when_isolation_disabled(executor, mock_config):
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"
    mock_config.openai_api_key = ""

    req = ExecutionRequest(prompt="implement a feature", chat_id="123", source="telegram")

    captured_cmds = []

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=None)
    mock_process.returncode = 0

    async def capture_exec(*args, **kwargs):
        captured_cmds.append(args)
        return mock_process

    with patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("asyncio.create_subprocess_exec", side_effect=capture_exec), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    cmd_args = list(captured_cmds[0])
    # Should NOT have --system-prompt (no persona, no worktree hint)
    assert "--system-prompt" not in cmd_args


async def test_execute_inner_exits_on_auth_error(executor, mock_config):
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"
    mock_config.openai_api_key = ""

    req = ExecutionRequest(prompt="hello", chat_id="123", source="telegram")

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"Error: Invalid API key - authentication failed")
    mock_process.wait = AsyncMock(return_value=None)
    mock_process.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_process), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer, \
         patch("sys.exit") as mock_exit:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=3)

    mock_exit.assert_called_once_with(1)


# --- has_free_slots ---

def test_has_free_slots_true_when_idle(executor):
    assert executor.has_free_slots


def test_has_free_slots_false_at_capacity(executor, mock_config):
    for i in range(mock_config.executor_max_parallel):
        executor.add_superpos_task(f"task-{i}")
    assert not executor.has_free_slots


# --- _resolve_slot ---

def test_resolve_slot_main_for_no_branch(executor, mock_config):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    req = ExecutionRequest(prompt="hi", chat_id="1", source="telegram")
    assert executor._resolve_slot(req) == "__main__"


def test_resolve_slot_worktree_path_for_branch(executor, mock_config):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    req = ExecutionRequest(prompt="hi", chat_id="1", source="superpos", branch="feat/x")
    with patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True):
        result = executor._resolve_slot(req)
    assert result == "/workspace/.worktrees/feat-x"


# --- Status transitions ---

async def test_status_busy_on_first_task_only(executor, mock_superpos, mock_config):
    """update_status('busy') is called once when two tasks run in parallel on different branches."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    async def fake_execute_inner(req, streamer, retries):
        await asyncio.sleep(0.1)

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        req1 = ExecutionRequest(prompt="a", chat_id="1", source="superpos", branch="branch-a")
        req2 = ExecutionRequest(prompt="b", chat_id="1", source="superpos", branch="branch-b")
        await executor.queue.put(req1)
        await executor.queue.put(req2)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.3)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    busy_calls = [c for c in mock_superpos.update_status.call_args_list if c.args == ("busy",)]
    assert len(busy_calls) == 1


async def test_status_online_when_all_done(executor, mock_superpos, mock_config):
    """update_status('online') is called only when the last task finishes."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    async def fake_execute_inner(req, streamer, retries):
        await asyncio.sleep(0.1)

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        req1 = ExecutionRequest(prompt="a", chat_id="1", source="superpos", branch="branch-a")
        req2 = ExecutionRequest(prompt="b", chat_id="1", source="superpos", branch="branch-b")
        await executor.queue.put(req1)
        await executor.queue.put(req2)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.4)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    online_calls = [c for c in mock_superpos.update_status.call_args_list if c.args == ("online",)]
    # Both tasks run in parallel on different branches, so online is called once when both finish
    assert len(online_calls) == 1


# --- Same-branch serialization ---

async def test_same_branch_tasks_serialize(executor, mock_config):
    """Two tasks targeting the same branch must not overlap."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    execution_log = []

    async def fake_execute_inner(req, streamer, retries):
        execution_log.append(f"start-{req.prompt}")
        await asyncio.sleep(0.05)
        execution_log.append(f"end-{req.prompt}")

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()

        req1 = ExecutionRequest(prompt="first", chat_id="1", source="superpos", branch="same-branch")
        req2 = ExecutionRequest(prompt="second", chat_id="1", source="superpos", branch="same-branch")
        await executor.queue.put(req1)
        await executor.queue.put(req2)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.3)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    # Because they share the same worktree lock, first must finish before second starts
    assert execution_log.index("end-first") < execution_log.index("start-second")


async def test_execute_inner_injects_worktree_hint_for_superpos_without_branch(
    executor, mock_superpos, mock_config
):
    """Superpos tasks without an explicit branch should get worktree instructions
    so the agent branches from origin/main instead of the current HEAD."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    mock_config.openai_api_key = ""

    req = ExecutionRequest(
        prompt="do superpos task", chat_id="123", source="superpos",
        superpos_task_id="task-999",
    )

    captured_cmds = []

    mock_process = AsyncMock()
    mock_process.stdout = _make_async_lines([])
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=None)
    mock_process.returncode = 0

    async def capture_exec(*args, **kwargs):
        captured_cmds.append(args)
        return mock_process

    with patch("superpos_agent_codex.codex_executor.is_git_repo", return_value=True), \
         patch("asyncio.create_subprocess_exec", side_effect=capture_exec), \
         patch("superpos_agent_codex.codex_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    cmd_args = list(captured_cmds[0])
    # Superpos tasks without a branch should get worktree hint prepended to prompt
    prompt_arg = cmd_args[-1]
    assert "Worktree Isolation" in prompt_arg
    assert "origin/main" in prompt_arg


# --- Extract methods ---

def test_extract_text_message_event():
    event = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello!"}]}
    assert CodexExecutor._extract_text(event) == "Hello!"


def test_extract_text_delta_event():
    event = {"type": "response.output_text.delta", "delta": "world"}
    assert CodexExecutor._extract_text(event) == "world"


def test_extract_text_ignores_non_assistant():
    event = {"type": "message", "role": "user", "content": [{"type": "text", "text": "hi"}]}
    assert CodexExecutor._extract_text(event) == ""


def test_extract_tool_use_function_call():
    event = {"type": "function_call", "name": "shell", "arguments": {"command": "ls"}}
    result = CodexExecutor._extract_tool_use(event)
    assert result == ("shell", {"command": "ls"})


def test_extract_tool_use_returns_none_for_text():
    event = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi"}]}
    assert CodexExecutor._extract_tool_use(event) is None


def test_extract_session_id_from_response():
    event = {"type": "response.completed", "session_id": "sess-abc"}
    assert CodexExecutor._extract_session_id(event) == "sess-abc"


def test_extract_session_id_returns_none_for_message():
    event = {"type": "message", "role": "assistant", "content": []}
    assert CodexExecutor._extract_session_id(event) is None


# --- _EventDeduplicator tests ---


def test_dedup_text_prefers_deltas_over_message():
    """When deltas are received, the completed message should be skipped."""
    dedup = _EventDeduplicator()
    # Receive streaming deltas
    assert dedup.extract_text({"type": "response.output_text.delta", "delta": "Hel"}) == "Hel"
    assert dedup.extract_text({"type": "response.output_text.delta", "delta": "lo!"}) == "lo!"
    # Completed message with same text — should be skipped
    msg = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello!"}]}
    assert dedup.extract_text(msg) == ""


def test_dedup_text_falls_back_to_message_when_no_deltas():
    """When no deltas are received, extract from the completed message."""
    dedup = _EventDeduplicator()
    msg = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello!"}]}
    assert dedup.extract_text(msg) == "Hello!"


def test_dedup_text_resets_on_new_response():
    """A new response.created event should reset delta tracking."""
    dedup = _EventDeduplicator()
    # First response: receive deltas
    dedup.extract_text({"type": "response.output_text.delta", "delta": "first"})
    # New response starts
    dedup.extract_text({"type": "response.created"})
    # This message should NOT be skipped (no deltas in this new response)
    msg = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "second"}]}
    assert dedup.extract_text(msg) == "second"


def test_dedup_text_skips_item_completed_after_deltas():
    """item.completed with agent_message should be skipped when deltas were seen."""
    dedup = _EventDeduplicator()
    dedup.extract_text({"type": "response.output_text.delta", "delta": "data"})
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": "data"}}
    assert dedup.extract_text(event) == ""


def test_dedup_text_content_block_delta():
    """content_block_delta events should mark deltas as seen."""
    dedup = _EventDeduplicator()
    assert dedup.extract_text({"type": "content_block_delta", "text": "chunk"}) == "chunk"
    msg = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "chunk"}]}
    assert dedup.extract_text(msg) == ""


def test_dedup_tool_skips_duplicate_by_call_id():
    """Two events with the same call_id should only return the first."""
    dedup = _EventDeduplicator()
    e1 = {"type": "function_call", "name": "shell", "arguments": {"command": "ls"}, "call_id": "call-1"}
    e2 = {"type": "function_call", "name": "shell", "arguments": {"command": "ls"}, "call_id": "call-1"}
    assert dedup.extract_tool_use(e1) == ("shell", {"command": "ls"})
    assert dedup.extract_tool_use(e2) is None


def test_dedup_tool_allows_different_calls():
    """Different tool calls should both be returned."""
    dedup = _EventDeduplicator()
    e1 = {"type": "function_call", "name": "shell", "arguments": {"command": "ls"}, "call_id": "call-1"}
    e2 = {"type": "function_call", "name": "shell", "arguments": {"command": "pwd"}, "call_id": "call-2"}
    assert dedup.extract_tool_use(e1) is not None
    assert dedup.extract_tool_use(e2) is not None


def test_dedup_tool_skips_nested_item_wrapper():
    """Nested item wrappers for function_call/tool_call should be ignored."""
    dedup = _EventDeduplicator()
    # Top-level function_call is accepted
    e1 = {"type": "function_call", "name": "shell", "arguments": {"command": "ls"}, "call_id": "call-1"}
    assert dedup.extract_tool_use(e1) is not None
    # Nested item with same type is skipped (event type is not function_call/tool_call/tool_use/item.started)
    e2 = {"type": "item.completed", "item": {"type": "function_call", "name": "shell", "arguments": {"command": "ls"}}}
    assert dedup.extract_tool_use(e2) is None


def test_dedup_tool_keeps_command_execution():
    """item.started with command_execution should be extracted."""
    dedup = _EventDeduplicator()
    event = {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/bash -lc 'ls -la'"}}
    result = dedup.extract_tool_use(event)
    assert result == ("shell", {"command": "ls -la"})


def test_dedup_tool_dedup_without_call_id():
    """When no call_id is present, dedup by name+args."""
    dedup = _EventDeduplicator()
    e1 = {"type": "function_call", "name": "shell", "arguments": {"command": "ls"}}
    e2 = {"type": "function_call", "name": "shell", "arguments": {"command": "ls"}}
    assert dedup.extract_tool_use(e1) is not None
    assert dedup.extract_tool_use(e2) is None


# --- Helpers ---

class _AsyncLineIter:
    """Mock async iterator over lines."""
    def __init__(self, lines):
        self._lines = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration


def _make_async_lines(json_events):
    """Create a mock stdout that yields JSONL lines."""
    import json
    lines = [json.dumps(e).encode() + b"\n" for e in json_events]
    return _AsyncLineIter(lines)
