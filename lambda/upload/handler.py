"""Single-file upload Lambda (API Gateway backed).

The front end's "single file + permission" field calls this via API Gateway
(``POST /single-file`` with an ``x-api-key`` header). The flow is:

    file + permission string  ->  S3 source bucket  +  metadata sidecar  ->  KB

For each request it:
1. Validates the caller-supplied ``permission_group`` against the allow-list and
   sanitises ``filename``.
2. Writes the (base64-decoded) file to the **source bucket** at ``docs/<filename>``.
3. Writes the Bedrock sidecar ``docs/<filename>.metadata.json`` carrying the
   ``permissions_group`` attribute (this is what makes the doc filterable).
4. Triggers the KB-sync Lambda so the document is embedded.

Unlike the bulk path (which reads the permission from each CSV row), here the
permission comes directly from the request body - it's the UI's single
permission field.

Request body (JSON)::

    {
        "filename": "report.pdf",
        "permission_group": "permissions_group_a",
        "content_base64": "<base64 of the file bytes>"
    }

Response (JSON)::

    { "ok": true, "source_key": "docs/report.pdf", "permission_group": "..." }

Environment variables:
    SOURCE_BUCKET            target bucket for curated docs + sidecars
    KB_SYNC_FUNCTION_NAME    name of the KB-sync Lambda to invoke
    PERMISSION_METADATA_KEY  metadata key (default: ``permissions_group``)
    PERMISSION_GROUPS        comma-separated list of valid groups
"""

import base64
import json
import logging
import os
import posixpath

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")

SOURCE_BUCKET = os.environ["SOURCE_BUCKET"]
KB_SYNC_FUNCTION_NAME = os.environ.get("KB_SYNC_FUNCTION_NAME")
PERMISSION_METADATA_KEY = os.environ.get("PERMISSION_METADATA_KEY", "permissions_group")
PERMISSION_GROUPS = [
    g.strip()
    for g in os.environ.get(
        "PERMISSION_GROUPS",
        "permissions_group_a,permissions_group_b,permissions_group_c",
    ).split(",")
    if g.strip()
]

METADATA_SUFFIX = ".metadata.json"
MAX_FILE_BYTES = 9 * 1024 * 1024  # API Gateway caps payloads at ~10 MB


def _load_payload(event: dict) -> dict:
    """Accept an API Gateway proxy event or a direct invoke payload."""
    if isinstance(event, dict) and "body" in event and "filename" not in event:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return json.loads(body)
    return event or {}


def _sanitize_filename(filename: str) -> str | None:
    if not filename:
        return None
    name = filename.strip()
    if name.startswith("/") or name.startswith("\\") or ":" in name.split("/")[0]:
        return None
    normalised = posixpath.normpath(name)
    if normalised.startswith("..") or normalised.startswith("/") or normalised == ".":
        return None
    if any(part == ".." for part in normalised.split("/")):
        return None
    if normalised.endswith(METADATA_SUFFIX):
        return None
    return normalised


def _trigger_kb_sync() -> None:
    if not KB_SYNC_FUNCTION_NAME:
        return
    try:
        lambda_client.invoke(
            FunctionName=KB_SYNC_FUNCTION_NAME,
            InvocationType="Event",
            Payload=b"{}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to trigger KB-sync (non-fatal).")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,x-api-key",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def handler(event, context):
    try:
        payload = _load_payload(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(400, {"error": f"Invalid request body: {exc}"})

    raw_name = (payload.get("filename") or "").strip()
    permission_group = (payload.get("permission_group") or "").strip()
    content_b64 = payload.get("content_base64") or ""

    safe_name = _sanitize_filename(raw_name)
    if not safe_name:
        return _response(400, {"error": "Missing or unsafe 'filename'."})
    if permission_group not in PERMISSION_GROUPS:
        return _response(
            400,
            {
                "error": "Field 'permission_group' is missing or not allowed.",
                "allowed_permission_groups": PERMISSION_GROUPS,
            },
        )
    if not content_b64:
        return _response(400, {"error": "Missing 'content_base64' (file bytes)."})

    try:
        data = base64.b64decode(content_b64)
    except Exception:  # noqa: BLE001
        return _response(400, {"error": "'content_base64' is not valid base64."})
    if len(data) > MAX_FILE_BYTES:
        return _response(413, {"error": "File too large for the API (max ~9 MB)."})

    dest_key = f"docs/{safe_name}"
    try:
        # 1. Write the file to the source bucket.
        s3.put_object(Bucket=SOURCE_BUCKET, Key=dest_key, Body=data)
        # 2. Write the Bedrock metadata sidecar (permission lives here, not the path).
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key=f"{dest_key}{METADATA_SUFFIX}",
            Body=json.dumps(
                {"metadataAttributes": {PERMISSION_METADATA_KEY: permission_group}}
            ).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to store %s", safe_name)
        return _response(502, {"error": f"Failed to store file: {exc}"})

    # 3. Embed it.
    _trigger_kb_sync()

    logger.info("Uploaded %s -> s3://%s/%s (group=%s)", safe_name, SOURCE_BUCKET, dest_key, permission_group)
    return _response(
        200,
        {"ok": True, "source_key": dest_key, "permission_group": permission_group},
    )
