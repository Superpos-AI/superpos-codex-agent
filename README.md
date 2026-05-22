# slim-codex-agent

Docker agent that bridges **Superpos** and **Telegram** with **OpenAI Codex** as the brain.

## Setup

### 1. Configure environment

```bash
cp .env.example .env
```

Fill in your `.env`:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `TELEGRAM_ALLOWED_USERS` | Yes | Your Telegram user ID (comma-separated for multiple) |
| `TELEGRAM_CHAT_ID` | No | Default chat for Superpos task notifications |
| `SUPERPOS_BASE_URL` | No | Your Superpos instance URL |
| `SUPERPOS_HIVE_ID` | No | Hive ID from Superpos UI |
| `SUPERPOS_AGENT_ID` | No | Agent ID from agent creation dialog |
| `SUPERPOS_API_TOKEN` | No | API Token from agent creation dialog |
| `SUPERPOS_REFRESH_TOKEN` | No | Refresh Token from agent creation dialog |
| `SUPERPOS_CAPABILITIES` | No | Comma-separated capabilities |
| `SUPERPOS_POLL_INTERVAL` | No | Poll interval in seconds (default: 5) |
| `OPENAI_API_KEY` | No | Only if not using OAuth |
| `CODEX_MODEL` | No | Default: gpt-5.5 |
| `CODEX_REASONING_EFFORT` | No | Reasoning effort: minimal, low, medium, high, xhigh (default: high) |
| `CODEX_MAX_TURNS` | No | Default: 30 |
| `CODEX_WORKING_DIR` | No | Default: /workspace |

Superpos variables are optional -- if omitted, only the Telegram bot runs.

### 2. Build

```bash
docker build -t slim-codex-agent .
```

### 3. Authenticate Codex (OAuth)

One-time step. This lets you use your OpenAI subscription instead of setting an API key directly.

```bash
docker run -it -v codex_auth:/home/agent/.codex --entrypoint codex slim-codex-agent login
```

Follow the prompts to authenticate. Then restart the agent (keep the `-v` flag).

### 4. Run

```bash
docker run --env-file .env -v codex_auth:/home/agent/.codex slim-codex-agent
```

The `codex_auth` volume persists your auth session across container restarts.

To prevent your Mac from sleeping while the agent runs, wrap the command with `caffeinate`:

```bash
caffeinate -is docker run --env-file .env -v codex_auth:/home/agent/.codex slim-codex-agent
```

`-i` prevents idle sleep, `-s` prevents system sleep (keeps the machine awake even with the lid closed on AC power). `caffeinate` exits automatically when the Docker container stops.

### Alternative: API key auth

If you prefer API key auth, skip step 3, set `OPENAI_API_KEY` in `.env`, and run without the volume:

```bash
docker run --env-file .env slim-codex-agent
```

The `codex` CLI deliberately ignores `OPENAI_API_KEY` from the process env — it only reads `~/.codex/auth.json` (or OAuth tokens). The container's entrypoint handles this for you: on startup, if `OPENAI_API_KEY` is set and no `auth.json` exists yet, it runs `codex login --with-api-key` to materialize the env var into `~/.codex/auth.json`.

If you mount the `codex_auth` volume and `auth.json` already exists (e.g. from a prior OAuth login via step 3), the entrypoint leaves it untouched — your OAuth session is preserved and `OPENAI_API_KEY` is ignored.

## Multi-agent setup (Docker Compose)

Run multiple independent agents, each with its own Telegram bot and Superpos registration.

### 1. Create compose and env files

```bash
cp docker-compose.example.yml docker-compose.yml
```

Edit `docker-compose.yml` to add/remove agents as needed. Then create env files:

```bash
cp .env.example .env.agent1
cp .env.example .env.agent2
# ... etc
```

Fill in unique values per agent:
- `SUPERPOS_AGENT_ID` + `SUPERPOS_API_TOKEN` (register each agent in Superpos dashboard)
- `TELEGRAM_BOT_TOKEN` (create separate bots via @BotFather)

Shared values (Git, GitHub, Superpos URL/Hive) can be the same across all agents.

### 2. Build

```bash
docker compose build
```

### 3. Authenticate each agent (OAuth)

Each agent needs its own Codex OAuth session, stored in a separate volume:

```bash
docker compose run --rm agent1 codex login
docker compose run --rm agent2 codex login
# ... etc
```

Follow the prompts for each, then exit.

### 4. Run

Start all agents:

```bash
docker compose up -d
```

Start a specific agent:

```bash
docker compose up -d agent1
```

View logs:

```bash
docker compose logs -f           # all agents
docker compose logs -f agent1    # single agent
```

Stop all:

```bash
docker compose down
```

### Re-authenticate

If OAuth expires for an agent, stop it and re-auth:

```bash
docker compose stop agent1
docker compose run --rm agent1 codex login
docker compose up -d agent1
```

## Testing

Tests cover the concurrency-critical paths: task dedup, claim-expiry abort, and poller enqueue logic.

### Run tests

```bash
docker build -f Dockerfile.test -t slim-codex-test .
docker run --rm slim-codex-test
```

No credentials or environment variables needed -- everything is mocked.

### Test layout

```
tests/
  conftest.py          # shared fixtures (executor, mock_superpos, mock_config)
  test_executor.py     # dedup methods, _report_progress 409/500, claim-expiry cleanup
  test_poller.py       # skip in-flight tasks, claim+enqueue new tasks, skip malformed tasks
  test_telegram_bot.py # branch parsing, PR resolution, message handling
```

## Usage

- Send any text message to your Telegram bot -- Codex processes it and streams the response back
- `/status` -- check queue depth
- `/model [<id>|list]` -- show or change the model. Any valid id is accepted; `/model list` prints known ids. Persists across restarts.
- `/effort [minimal|low|medium|high|xhigh]` -- show or change reasoning effort. Persists across restarts.
- `/new` -- clear session (start fresh conversation)
- `/restart` -- restart the agent
- Superpos tasks are automatically polled, claimed, executed, and completed
