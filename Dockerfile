FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY data ./data
COPY skills ./skills
COPY web ./web
RUN pip install --no-cache-dir uv && uv sync --locked --no-dev
RUN useradd --create-home learner && mkdir /app/.agent-lab && chown learner /app/.agent-lab
USER learner
ENV AGENT_LAB_ALLOWED_HOSTS=localhost,127.0.0.1,[::1]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"]
# 容器内通过 CLI 统一入口监听 0.0.0.0：默认强制校验 AGENT_LAB_TOKEN / AGENT_LAB_TOKENS_JSON；
# 仅本机 127.0.0.1 端口映射练习时可显式传入 AGENT_LAB_ALLOW_INSECURE_LOCAL=1，并支持通过 AGENT_LAB_SANDBOX_URL 连接独立沙箱池。
CMD ["/app/.venv/bin/python", "-m", "agent_lab.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]
