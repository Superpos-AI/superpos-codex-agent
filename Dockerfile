FROM node:22-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip git curl ripgrep tini && \
    rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY src/ /app/src/
COPY entrypoint.sh /app/entrypoint.sh
COPY workspace/ /workspace/

# Symlink module scripts onto PATH so Codex can call them by name
RUN mkdir -p /workspace/.codex/modules-bin && \
    for dir in /workspace/.codex/modules/*/scripts; do \
      if [ -d "$dir" ]; then \
        for script in "$dir"/*; do \
          chmod +x "$script" && \
          ln -sf "$script" /workspace/.codex/modules-bin/$(basename "$script"); \
        done; \
      fi; \
    done
ENV PATH="/workspace/.codex/modules-bin:$PATH"

# Create non-root user (required for full-auto mode)
RUN useradd -m -s /bin/bash -u 1001 agent && \
    mkdir -p /home/agent/.codex && \
    chown -R agent:agent /workspace /home/agent/.codex

ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1
ENV HOME="/home/agent"

VOLUME ["/home/agent/.codex"]

USER agent
WORKDIR /workspace

# tini runs as PID 1 and reaps orphaned grandchildren (esbuild/node
# subprocesses left behind when a codex run dies) — without it they
# accumulate as zombies because Python doesn't reap reparented orphans.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python3", "-m", "superpos_agent_codex"]
