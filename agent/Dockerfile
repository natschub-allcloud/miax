# Bedrock AgentCore Runtime requires a linux/arm64 container that listens on
# port 8080. The bedrock-agentcore SDK's BedrockAgentCoreApp provides the
# HTTP server; we just start it.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

# Security: run as an unprivileged user and avoid writing bytecode / buffering.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code.
COPY agent.py .

# Create and switch to a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Basic liveness check against the AgentCore ping endpoint.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping')" || exit 1

CMD ["python", "agent.py"]
