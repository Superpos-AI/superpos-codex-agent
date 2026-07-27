"""Smoke/regression test for Superpos issue #205 (MCP-4).

Proves that an MCP-bearing module is loaded into the Codex runtime's MCP
config.  The Codex executor wires this up at init in
``codex_executor.py``:

    modules = discover_modules(config.modules_dir)   # ~line 75
    mcp = collect_mcp_servers(modules)               # ~line 76
    if mcp:
        self._write_mcp_config(mcp)                  # ~line 79

and ``_write_mcp_config`` (static, ~line 113-125) writes
``~/.codex/config.json`` with ``existing["mcpServers"] = mcp_servers``.

This test drives that exact loading seam with a canonical remote-HTTP MCP
module and asserts the server + url land in the materialized config.
"""

import json

from superpos_agent_core import collect_mcp_servers, discover_modules
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


def test_mcp_module_loaded_into_codex_runtime_config(tmp_path, monkeypatch):
    """An mcp-bearing module is discovered and its server lands in
    ``~/.codex/config.json`` via the Codex runtime loading seam.

    Seam pinned: codex_executor.py ~line 75-79 (discover -> collect ->
    _write_mcp_config) and _write_mcp_config ~line 113-125.
    """
    # Point HOME at a tmp dir so _write_mcp_config does NOT clobber the real
    # ~/.codex/config.json (it writes to Path.home()/".codex"/config.json).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    modules_dir = tmp_path / "modules"
    _make_example_module(modules_dir)

    # Drive the same seam the executor uses at init.  include_bundled=False
    # keeps the assertion hermetic (only our example module contributes).
    mcp = collect_mcp_servers(discover_modules(str(modules_dir), include_bundled=False))

    # Non-vacuous precondition: the module actually produced a server.
    assert mcp["example-remote"]["url"] == "https://mcp.example.com/sse"

    CodexExecutor._write_mcp_config(mcp)

    config_path = home / ".codex" / "config.json"
    assert config_path.exists(), "Codex runtime MCP config was not written"

    written = json.loads(config_path.read_text())
    assert written["mcpServers"]["example-remote"]["url"] == (
        "https://mcp.example.com/sse"
    )
