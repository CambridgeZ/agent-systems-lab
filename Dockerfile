FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY data ./data
COPY skills ./skills
COPY web ./web
RUN pip install --no-cache-dir uv && uv sync --locked --no-dev
RUN useradd --create-home learner && mkdir /app/.agent-lab && chown learner /app/.agent-lab
USER learner
EXPOSE 8765
# 容器内监听 0.0.0.0；宿主端只映射 127.0.0.1，见 README。
CMD ["/app/.venv/bin/uvicorn", "agent_lab.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
