"""Stats Lambda - reports how many documents are in the knowledge base.

Backs the UI's "N indexed" badge with a real number instead of a client-side
counter. It counts the curated documents in the **source bucket** (the objects
the Knowledge Base ingests), excluding the Bedrock ``.metadata.json`` sidecars.

Invoked via API Gateway ``GET/POST /stats`` (x-api-key). Returns::

    { "indexed": 12, "documents": ["docs/report.pdf", ...] }

Environment variables:
    SOURCE_BUCKET   bucket holding the curated docs + sidecars
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")

SOURCE_BUCKET = os.environ["SOURCE_BUCKET"]
METADATA_SUFFIX = ".metadata.json"
MAX_LISTED = 1000


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,x-api-key",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def handler(event, context):
    documents: list[str] = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=SOURCE_BUCKET):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # Count actual documents, not the Bedrock metadata sidecars.
                if key.endswith(METADATA_SUFFIX):
                    continue
                documents.append(key)
                if len(documents) >= MAX_LISTED:
                    break
            if len(documents) >= MAX_LISTED:
                break
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to list source bucket")
        return _response(502, {"error": f"Failed to read documents: {exc}"})

    return _response(200, {"indexed": len(documents), "documents": documents})
