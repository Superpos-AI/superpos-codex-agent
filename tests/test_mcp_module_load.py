"""Smoke/regression test for Superpos issue #205 (MCP-4).

Proves that an MCP-bearing module is loaded into the Codex runtime's MCP
config *by ``CodexExecutor.__init__``* — the behavior this PR exists to
protect.  The constructor wires the loading seam in ``codex_executor.py``:

    modules = discover_modules(config.modules_dir)   # ~line 74
    mcp = collect_mcp_servers(modules)               # ~line 75
    if mcp:
        self._write_mcp_config(mcp)                  # ~line 78

and ``_write_mcp_config`` writes ``~/.codex/config.json`` with
``existing["mcpServers"] = mcp_servers``.

The test instantiates ``CodexExecutor`` with a config whose ``modules_dir``
holds a canonical remote-HTTP MCP module and asserts the server + url land
in the materialized ``~/.codex/config.json``.  Because the assertion runs
against the *constructor's* side effect, it fails if ``__init__`` stops
calling ``discover_modules`` / ``collect_mcp_servers`` / ``_write_mcp_config``
(or calls them in the wrong order) — which driving those helpers directly
would not catch.
"""

import json

from superpos_agent_codex.codex_executor import CodexExecutor

# Canonical MCP-4 reference module (matches the other MCP-4 PRs exactly).
_MODULE_YAML = """description: "Example remote-HTTP MCP module (MCP-4 reference)."
env: []
mcp:
  example-remote:
    url: "https://mcp.example.com/sse"
"""


def _make_example_module(modules_dir):
    """Create the canonical example-remote-mcp module under ``modules_dir``."""
    mod = modules_dir / "example-remote-mcp"
    mod.mkdir(parents=True)
    (mod / "module.yaml").write_text(_MODULE_YAML)
    return mod


def test_mcp_module_loaded_by_codex_executor_init(
    tmp_path, monkeypatch, mock_config, mock_runtime
):
    """Constructing ``CodexExecutor`` discovers an mcp-bearing module and
    writes its server into ``~/.codex/config.json``.

    Regression guard: the assertion targets the constructor's side effect,
    so the test fails if ``CodexExecutor.__init__`` stops wiring
    ``discover_modules -> collect_mcp_servers -> _write_mcp_config``.
    """
    # Point HOME at a tmp dir so _write_mcp_config does NOT clobber the real
    # ~/.codex/config.json (it writes to Path.home()/".codex"/config.json).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    # mock_config.modules_dir is tmp_path/"modules"; seed our module there so
    # the executor's own discover_modules(config.modules_dir) picks it up.
    modules_dir = tmp_path / "modules"
    _make_example_module(modules_dir)
    assert str(mock_config.modules_dir) == str(modules_dir)

    # Drive the exact seam under test: CodexExecutor.__init__, end to end.
    CodexExecutor(mock_config, mock_runtime, None, None)

    config_path = home / ".codex" / "config.json"
    assert config_path.exists(), (
        "CodexExecutor.__init__ did not write the Codex runtime MCP config"
    )

    written = json.loads(config_path.read_text())
    assert written["mcpServers"]["example-remote"]["url"] == (
        "https://mcp.example.com/sse"
    )
