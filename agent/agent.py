"""MIAX AgentCore runtime agent.

This is the application code hosted by the Bedrock AgentCore Runtime. It exposes
a single entrypoint that, given a user ``prompt`` and the caller's
``permission_group``, performs RAG against the Knowledge Base while enforcing a
metadata filter so that only chunks tagged with the caller's permission group
are retrieved.

The AgentCore Runtime invokes ``invoke(payload)`` for each request. ``payload``
is the JSON body sent to the runtime's ``InvokeAgentRuntime`` API.

Required request shape::

    {
        "prompt": "What were Q3 revenues?",
        "permission_group": "permissions_group_b"
    }

Environment variables (injected by the CDK stack):
    KNOWLEDGE_BASE_ID        the Bedrock Knowledge Base to query
    MODEL_ARN                the foundation model ARN used for generation
    PERMISSION_METADATA_KEY  metadata key to filter on (default permissions_group)
    PERMISSION_GROUPS        comma-separated list of valid groups
    AWS_REGION               region (provided by the runtime)
"""

import logging
import os

import boto3
from botocore.config import Config
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logger = logging.getLogger("miax.agent")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = BedrockAgentCoreApp()

REGION = os.environ.get("AWS_REGION", "us-east-1")
KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
# MODEL_ARN is the inference-profile ARN (e.g. us.anthropic.claude-sonnet-4-6).
MODEL_ARN = os.environ["MODEL_ARN"]
PERMISSION_METADATA_KEY = os.environ.get(
    "PERMISSION_METADATA_KEY", "permissions_group"
)
PERMISSION_GROUPS = [
    g.strip()
    for g in os.environ.get(
        "PERMISSION_GROUPS",
        "permissions_group_a,permissions_group_b,permissions_group_c",
    ).split(",")
    if g.strip()
]
NUMBER_OF_RESULTS = int(os.environ.get("NUMBER_OF_RESULTS", "8"))

bedrock_agent_runtime = boto3.client(
    "bedrock-agent-runtime",
    region_name=REGION,
    config=Config(retries={"max_attempts": 3, "mode": "standard"}),
)



def _build_filter(permission_group: str) -> dict:
    """Restrict retrieval to chunks tagged with the caller's permission group."""
    return {
        "equals": {
            "key": PERMISSION_METADATA_KEY,
            "value": permission_group,
        }
    }


def _source_uri(location: dict) -> str | None:
    """Extract a human-readable source URI from a retrieved-reference location.

    Handles the common location shapes (S3, web, Confluence, etc.) returned by
    Bedrock; falls back to None when no URI is present.
    """
    if not location:
        return None
    loc_type = (location.get("type") or "").lower()
    type_to_field = {
        "s3": ("s3Location", "uri"),
        "web": ("webLocation", "url"),
        "confluence": ("confluenceLocation", "url"),
        "salesforce": ("salesforceLocation", "url"),
        "sharepoint": ("sharePointLocation", "url"),
        "custom": ("customDocumentLocation", "id"),
        "kendra": ("kendraDocumentLocation", "uri"),
    }
    field = type_to_field.get(loc_type)
    if field:
        nested = location.get(field[0], {}) or {}
        if nested.get(field[1]):
            return nested[field[1]]
    # Fallback: return the first nested uri/url/id we can find.
    for value in location.values():
        if isinstance(value, dict):
            for k in ("uri", "url", "id"):
                if value.get(k):
                    return value[k]
    return None



@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore runtime entrypoint.

    The caller's ``permission_group`` MUST be supplied by a trusted layer (the
    front end derives it from the authenticated user). The agent never infers it
    from the prompt, and it is validated against the allow-list before any
    retrieval occurs.
    """

    prompt = (payload or {}).get("prompt")
    permission_group = (payload or {}).get("permission_group")

    if not prompt or not isinstance(prompt, str):
        return {"error": "Missing or invalid required field 'prompt'."}
    if permission_group not in PERMISSION_GROUPS:
        # Do not echo the invalid value back verbatim (avoid reflecting input).
        return {
            "error": "Field 'permission_group' is missing or not an allowed value.",
            "allowed_permission_groups": PERMISSION_GROUPS,
        }

    try:
        response = bedrock_agent_runtime.retrieve_and_generate(
            input={"text": prompt},
            retrieve_and_generate_configuration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": KNOWLEDGE_BASE_ID,
                    "modelArn": MODEL_ARN,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "numberOfResults": NUMBER_OF_RESULTS,
                            # Permission enforcement: only this group's chunks.
                            "filter": _build_filter(permission_group),
                        }
                    },
                },
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "retrieve_and_generate failed (kb=%s, group=%s)",
            KNOWLEDGE_BASE_ID,
            permission_group,
        )
        return {"error": "An internal error occurred while answering the request."}

    answer_text = response.get("output", {}).get("text", "")

    # Build a rich citation entry for every piece of retrieved content that the
    # answer drew from. Each entry includes the cited snippet of the answer, the
    # source location/URI, the exact retrieved text chunk, its relevance score,
    # and the document's permission group (from chunk metadata).
    citations = []
    for citation in response.get("citations", []):
        generated = citation.get("generatedResponsePart", {}).get(
            "textResponsePart", {}
        )
        cited_text = generated.get("text", "")

        for ref in citation.get("retrievedReferences", []):
            metadata = ref.get("metadata", {}) or {}
            location = ref.get("location", {}) or {}
            citations.append(
                {
                    # The portion of the answer this reference supports.
                    "cited_answer_text": cited_text,
                    # Human-readable source URI (e.g. the S3 object).
                    "source_uri": _source_uri(location),
                    "location_type": location.get("type"),
                    # The actual retrieved chunk text.
                    "content": (ref.get("content", {}) or {}).get("text", ""),
                    # Relevance score when provided by the retriever.
                    "score": ref.get("score"),
                    # Permission group recorded on the chunk's metadata.
                    "permission_group": metadata.get(PERMISSION_METADATA_KEY),
                    "metadata": metadata,
                }
            )

    return {
        "answer": answer_text,
        "permission_group": permission_group,
        "citations": citations,
        "citation_count": len(citations),
        "session_id": response.get("sessionId"),
    }




if __name__ == "__main__":
    app.run()
