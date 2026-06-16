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

import os

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

REGION = os.environ.get("AWS_REGION", "us-east-1")
KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
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

bedrock_agent_runtime = boto3.client(
    "bedrock-agent-runtime", region_name=REGION
)


def _build_filter(permission_group: str) -> dict:
    """Restrict retrieval to chunks tagged with the caller's permission group."""
    return {
        "equals": {
            "key": PERMISSION_METADATA_KEY,
            "value": permission_group,
        }
    }


@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore runtime entrypoint."""
    prompt = (payload or {}).get("prompt")
    permission_group = (payload or {}).get("permission_group")

    if not prompt:
        return {"error": "Missing required field 'prompt'."}
    if permission_group not in PERMISSION_GROUPS:
        return {
            "error": (
                f"'permission_group' must be one of {PERMISSION_GROUPS}; "
                f"got {permission_group!r}."
            )
        }

    response = bedrock_agent_runtime.retrieve_and_generate(
        input={"text": prompt},
        retrieve_and_generate_configuration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": KNOWLEDGE_BASE_ID,
                "modelArn": MODEL_ARN,
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "numberOfResults": 8,
                        # Permission enforcement: only this group's chunks.
                        "filter": _build_filter(permission_group),
                    }
                },
            },
        },
    )

    citations = []
    for citation in response.get("citations", []):
        for ref in citation.get("retrievedReferences", []):
            citations.append(
                {
                    "location": ref.get("location"),
                    "metadata": ref.get("metadata"),
                }
            )

    return {
        "answer": response.get("output", {}).get("text", ""),
        "permission_group": permission_group,
        "citations": citations,
    }


if __name__ == "__main__":
    app.run()
