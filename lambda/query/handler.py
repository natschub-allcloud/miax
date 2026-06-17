"""Query Lambda - the RAG agent endpoint for the front end.

Exposed via an IAM-authenticated Lambda Function URL so a UI can call it over
HTTPS. For each request it:

1. Validates the caller's ``permission_group`` against the allow-list.
2. Retrieves relevant chunks from the Bedrock Knowledge Base, enforcing a
   metadata filter ``equals(permissions_group, <caller group>)`` so only the
   caller's content is ever returned.
3. Generates an answer with the configured model (Converse API), grounded in
   the retrieved context. Converse returns token usage so we can record it.
4. Writes one audit row to DynamoDB: username, permission group, query,
   response, latency, tokens in/out, and citations.
5. Returns ``{ answer, citations, usage, latency_ms }`` to the caller.

Request body (JSON)::

    {
        "username": "alice@example.com",
        "permission_group": "permissions_group_b",
        "prompt": "What were Q3 revenues?"
    }

Environment variables (injected by the CDK stack):
    KNOWLEDGE_BASE_ID        Bedrock Knowledge Base id
    MODEL_ARN                inference-profile ARN used for generation
    QUERY_LOG_TABLE          DynamoDB audit table name
    PERMISSION_METADATA_KEY  metadata key (default: permissions_group)
    PERMISSION_GROUPS        comma-separated allow-list
    NUMBER_OF_RESULTS        chunks to retrieve (default 8)
"""

import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_cfg = Config(retries={"max_attempts": 3, "mode": "standard"})
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", config=_cfg)
bedrock_runtime = boto3.client("bedrock-runtime", config=_cfg)
dynamodb = boto3.resource("dynamodb")

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
MODEL_ARN = os.environ["MODEL_ARN"]
QUERY_LOG_TABLE = os.environ["QUERY_LOG_TABLE"]
PERMISSION_METADATA_KEY = os.environ.get("PERMISSION_METADATA_KEY", "permissions_group")
PERMISSION_GROUPS = [
    g.strip()
    for g in os.environ.get(
        "PERMISSION_GROUPS",
        "permissions_group_a,permissions_group_b,permissions_group_c",
    ).split(",")
    if g.strip()
]
NUMBER_OF_RESULTS = int(os.environ.get("NUMBER_OF_RESULTS", "8"))

_table = dynamodb.Table(QUERY_LOG_TABLE)

_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the user's question using ONLY the "
    "provided context. If the context does not contain the answer, say you "
    "don't know. Be concise and cite facts from the context."
)


def _load_payload(event: dict) -> dict:
    if isinstance(event, dict) and "body" in event and "prompt" not in event:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return json.loads(body)
    return event or {}


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _source_uri(location: dict) -> str | None:
    if not location:
        return None
    for value in location.values():
        if isinstance(value, dict):
            for k in ("uri", "url", "id"):
                if value.get(k):
                    return value[k]
    return None


def _retrieve(prompt: str, permission_group: str) -> list[dict]:
    """Retrieve permission-filtered chunks from the Knowledge Base."""
    resp = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": prompt},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": NUMBER_OF_RESULTS,
                "filter": {
                    "equals": {
                        "key": PERMISSION_METADATA_KEY,
                        "value": permission_group,
                    }
                },
            }
        },
    )
    citations = []
    for result in resp.get("retrievalResults", []):
        metadata = result.get("metadata", {}) or {}
        citations.append(
            {
                "content": (result.get("content", {}) or {}).get("text", ""),
                "source_uri": _source_uri(result.get("location", {})),
                "location_type": (result.get("location", {}) or {}).get("type"),
                "score": result.get("score"),
                "permission_group": metadata.get(PERMISSION_METADATA_KEY),
                "metadata": metadata,
            }
        )
    return citations


def _generate(prompt: str, citations: list[dict]) -> tuple[str, dict]:
    """Generate a grounded answer with Converse; returns (text, usage)."""
    context_blocks = "\n\n".join(
        f"[Source {i + 1}] {c['content']}" for i, c in enumerate(citations) if c["content"]
    )
    user_message = (
        f"Context:\n{context_blocks}\n\nQuestion: {prompt}"
        if context_blocks
        else prompt
    )
    resp = bedrock_runtime.converse(
        modelId=MODEL_ARN,
        system=[{"text": _SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
    )
    text = ""
    for block in resp.get("output", {}).get("message", {}).get("content", []):
        if "text" in block:
            text += block["text"]
    usage = resp.get("usage", {}) or {}
    return text, usage


def _log_query(item: dict) -> None:
    try:
        _table.put_item(Item=item)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write audit row (non-fatal).")


def handler(event, context):
    started = time.time()
    try:
        payload = _load_payload(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(400, {"error": f"Invalid request body: {exc}"})

    username = (payload.get("username") or "anonymous").strip()
    prompt = payload.get("prompt")
    permission_group = payload.get("permission_group")

    if not prompt or not isinstance(prompt, str):
        return _response(400, {"error": "Missing or invalid 'prompt'."})
    if permission_group not in PERMISSION_GROUPS:
        return _response(
            400,
            {
                "error": "Field 'permission_group' is missing or not allowed.",
                "allowed_permission_groups": PERMISSION_GROUPS,
            },
        )

    try:
        citations = _retrieve(prompt, permission_group)
        answer, usage = _generate(prompt, citations)
    except Exception:  # noqa: BLE001
        logger.exception("Query failed (group=%s)", permission_group)
        return _response(502, {"error": "Failed to answer the request."})

    latency_ms = int((time.time() - started) * 1000)
    tokens_in = int(usage.get("inputTokens", 0))
    tokens_out = int(usage.get("outputTokens", 0))
    now = datetime.now(timezone.utc).isoformat()

    # One audit row per query.
    _log_query(
        {
            "username": username,
            "timestamp": now,
            "query_id": str(uuid.uuid4()),
            "permission_group": permission_group,
            "query": prompt,
            "response": answer,
            "latency_ms": latency_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "citations": json.dumps(citations)[:380000],  # stay under item limit
            "citation_count": len(citations),
        }
    )

    return _response(
        200,
        {
            "answer": answer,
            "permission_group": permission_group,
            "citations": citations,
            "citation_count": len(citations),
            "usage": {"tokens_in": tokens_in, "tokens_out": tokens_out},
            "latency_ms": latency_ms,
        },
    )
