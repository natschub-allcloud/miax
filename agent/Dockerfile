# Bedrock AgentCore Runtime requires a linux/arm64 container that listens on
# port 8080. The bedrock-agentcore SDK's BedrockAgentCoreApp provides the
# HTTP server; we just start it.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .

EXPOSE 8080

CMD ["python", "agent.py"]
