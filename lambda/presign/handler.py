"""Presign Lambda - issues presigned S3 PUT URLs for direct browser uploads.

This is the enterprise upload pattern: the browser uploads the file bytes
**directly to S3** using a short-lived presigned URL, so the file never passes
through API Gateway (no 10 MB limit) or a Lambda body. The chosen permission
group is **baked into the presigned URL as S3 object metadata** at sign time, so
the uploader can't change it and the existing ingest Lambda can read it.

Flow:
    1. Browser POSTs {filename, permission_group} to /presign (x-api-key).
    2. This Lambda returns a presigned PUT URL that pins
       ``x-amz-meta-permissions_group: <group>``.
    3. Browser PUTs the file straight to the input bucket with that header.
    4. The S3 ``ObjectCreated`` event triggers the ingest Lambda, which reads the
       metadata, copies the doc into the source bucket + writes the Bedrock
       sidecar, and kicks off a KB sync.

Request body (JSON)::

    { "filename": "report.pdf", "permission_group": "permissions_group_a" }

Response (JSON)::

    {
        "upload_url": "https://<bucket>.s3.amazonaws.com/...signed...",
        "key": "report.pdf",
        "headers": { "x-amz-meta-permissions_group": "permissions_group_a" }
    }

The browser MUST send the returned ``headers`` on the PUT or S3 rejects it
(the signature covers them).

Environment variables:
    INPUT_BUCKET             bucket the browser uploads into
    PERMISSION_METADATA_KEY  metadata key (default: ``permissions_group``)
    PERMISSION_GROUPS        comma-separated allow-list
    URL_EXPIRATION_SECONDS   presigned URL lifetime (default: 900)
"""

import base64
import json
import logging
import os
import posixpath

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# SigV4 is required for presigned URLs that include metadata headers.
s3 = boto3.client("s3", config=Config(signature_version="s3v4"))

INPUT_BUCKET = os.environ["INPUT_BUCKET"]
PERMISSION_METADATA_KEY = os.environ.get("PERMISSION_METADATA_KEY", "permissions_group")
PERMISSION_GROUPS = [
    g.strip()
    for g in os.environ.get(
        "PERMISSION_GROUPS",
        "permissions_group_a,permissions_group_b,permissions_group_c",
    ).split(",")
    if g.strip()
]
URL_EXPIRATION_SECONDS = int(os.environ.get("URL_EXPIRATION_SECONDS", "900"))

METADATA_SUFFIX = ".metadata.json"


def _load_payload(event: dict) -> dict:
    if isinstance(event, dict) and "body" in event and "filename" not in event:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return json.loads(body)
    return event or {}


def _sanitize(name: str) -> str | None:
    if not name:
        return None
    name = name.strip()
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

    filename = (payload.get("filename") or "").strip()
    permission_group = (payload.get("permission_group") or "").strip()

    safe_filename = _sanitize(filename)
    if not safe_filename:
        return _response(400, {"error": "Missing or unsafe 'filename'."})
    if permission_group not in PERMISSION_GROUPS:
        return _response(
            400,
            {
                "error": "Field 'permission_group' is missing or not allowed.",
                "allowed_permission_groups": PERMISSION_GROUPS,
            },
        )

    # Upload to the input-bucket root so the standard ingest Lambda picks it up.
    key = safe_filename
    metadata_header = f"x-amz-meta-{PERMISSION_METADATA_KEY}"

    try:
        url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": INPUT_BUCKET,
                "Key": key,
                # Baking the metadata into the signed params means the browser
                # MUST send the matching header, and can't substitute a
                # different permission group.
                "Metadata": {PERMISSION_METADATA_KEY: permission_group},
            },
            ExpiresIn=URL_EXPIRATION_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to generate presigned URL")
        return _response(500, {"error": f"Failed to generate URL: {exc}"})

    logger.info(
        "Presigned PUT for s3://%s/%s (group=%s)", INPUT_BUCKET, key, permission_group
    )
    return _response(
        200,
        {
            "upload_url": url,
            "key": key,
            "headers": {metadata_header: permission_group},
        },
    )
