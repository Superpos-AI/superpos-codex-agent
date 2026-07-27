"""Smoke/regression test for Superpos issue #205 (MCP-4).

Proves that an MCP-bearing module is loaded into the Codex runtime's MCP
config *by ``CodexExecutor.__init__``* — the behavior this PR exists to
protect.  The constructor wires the loading seam in ``codex_executor.py``:

    modules = discover_modules(config.modules_dir)   # ~line 74
    mcp = collect_mcp_servers(modules)               # ~line 75
    if mcp:
        self._write_mcp_config(mcp)                  # ~line 78

and ``_write_mcp_config`` merges ``[mcp_servers.<name>]`` tables into
``~/.codex/config.toml`` — the file the Codex CLI actually reads (transport
chosen by ``command`` -> stdio vs ``url`` -> streamable HTTP).

The test instantiates ``CodexExecutor`` with a config whose ``modules_dir``
holds a canonical remote-HTTP MCP module and asserts the server + url land
in the materialized ``~/.codex/config.toml`` under a ``[mcp_servers.*]``
table.  Because the assertion runs against the *constructor's* side effect,
it fails if ``__init__`` stops calling ``discover_modules`` /
``collect_mcp_servers`` / ``_write_mcp_config`` (or calls them in the wrong
order) — which driving those helpers directly would not catch.  It also pins
the TOML contract, so it fails if the config regresses to the JSON
``mcpServers`` shape Codex ignores.
"""

from superpos_agent_codex.codex_executor import CodexExecutor

# A pre-existing config.toml as written by entrypoint.sh — the test asserts
# _write_mcp_config preserves it rather than clobbering the whole file.
_EXISTING_CONFIG_TOML = """[features]
apps = false
"""

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
    writes its server into ``~/.codex/config.toml`` as a ``[mcp_servers.*]``
    table (the format Codex reads).

    Regression guard: the assertion targets the constructor's side effect,
    so the test fails if ``CodexExecutor.__init__`` stops wiring
    ``discover_modules -> collect_mcp_servers -> _write_mcp_config``, or if the
    written config regresses away from the Codex TOML contract.
    """
    # Point HOME at a tmp dir so _write_mcp_config does NOT clobber the real
    # ~/.codex/config.toml (it writes to Path.home()/".codex"/config.toml).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    # Seed a pre-existing config.toml (as entrypoint.sh does) so we can prove
    # the merge preserves unrelated config rather than overwriting the file.
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_EXISTING_CONFIG_TOML)

    # mock_config.modules_dir is tmp_path/"modules"; seed our module there so
    # the executor's own discover_modules(config.modules_dir) picks it up.
    modules_dir = tmp_path / "modules"
    _make_example_module(modules_dir)
    assert str(mock_config.modules_dir) == str(modules_dir)

    # Drive the exact seam under test: CodexExecutor.__init__, end to end.
    CodexExecutor(mock_config, mock_runtime, None, None)

    config_path = codex_dir / "config.toml"
    assert config_path.exists(), (
        "CodexExecutor.__init__ did not write the Codex runtime MCP config"
    )
    text = config_path.read_text()

    # The Codex TOML contract: an [mcp_servers.<name>] table with a url key —
    # NOT a JSON `mcpServers` object, which Codex silently ignores.
    assert "[mcp_servers.example-remote]" in text
    assert 'url = "https://mcp.example.com/sse"' in text
    assert '"mcpServers"' not in text
    # Pre-existing config is preserved, not clobbered.
    assert "[features]" in text
    assert "apps = false" in text
    # A config.json (the wrong, ignored contract) must NOT be the mechanism.
    assert not (codex_dir / "config.json").exists()

    # When a real TOML parser is available (3.11+), prove the file parses and
    # resolves to the expected structured value — not just the right substrings.
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 — string assertions above suffice.
        tomllib = None
    if tomllib is not None:
        parsed = tomllib.loads(text)
        assert parsed["mcp_servers"]["example-remote"]["url"] == (
            "https://mcp.example.com/sse"
        )
        assert parsed["features"]["apps"] is False
