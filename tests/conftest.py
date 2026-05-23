import pytest
from unittest.mock import AsyncMock, MagicMock

from superpos_agent_codex.config import CodexConfig as Config
from superpos_agent_codex.codex_executor import CodexExecutor
from superpos_agent_codex.runtime_config import CodexRuntimeConfig as RuntimeConfig


@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock(spec=Config)
    cfg.codex_model = "gpt-5.4"
    cfg.codex_reasoning_effort = "high"
    cfg.executor_max_turns = 5
    cfg.executor_working_dir = "/tmp"
    cfg.executor_worktree_isolation = False
    cfg.superpos_poll_interval = 1
    cfg.telegram_chat_id = "123"
    cfg.executor_max_parallel = 3
    cfg.openai_api_key = ""
    cfg.home_dir = str(tmp_path)
    cfg.modules_dir = str(tmp_path / "modules")
    return cfg


@pytest.fixture
def mock_superpos():
    a = AsyncMock()
    a.update_progress = AsyncMock()
    a.poll_tasks = AsyncMock(return_value=[])
    a.claim_task = AsyncMock()
    a.complete_task = AsyncMock()
    a.fail_task = AsyncMock()
    a.heartbeat = AsyncMock()
    a.update_status = AsyncMock()
    return a


@pytest.fixture
def mock_gateway():
    gw = AsyncMock()
    gw.send_message = AsyncMock()
    gw.edit_message_text = AsyncMock()
    gw.delete_message = AsyncMock()
    gw.send_chat_action = AsyncMock()
    return gw


@pytest.fixture
def mock_runtime(tmp_path):
    return RuntimeConfig(
        model="gpt-5.4",
        effort="high",
        path=str(tmp_path / "runtime_config.json"),
    )


@pytest.fixture
def executor(mock_config, mock_runtime, mock_superpos, mock_gateway):
    return CodexExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)


@pytest.fixture
def executor_with_persona(mock_config, mock_runtime, mock_superpos, mock_gateway):
    return CodexExecutor(
        mock_config, mock_runtime, mock_superpos, mock_gateway,
        persona="You are a helpful assistant.",
    )
