"""Presign Lambda - generates presigned S3 PUT URLs for bulk upload staging.

The front end calls this to get a temporary URL for each file, then uploads
directly to S3 (bypassing API Gateway's payload limit). Files are staged under
``bulk/<batch_id>/<filename>`` in the input bucket, ready for the bulk-ingest
Lambda to process.

Request body (JSON)::

    {
        "filename": "report.pdf",
        "batch_id": "upload-1719200000-abc"
    }

Response (JSON)::

    {
        "upload_url": "https://s3.amazonaws.com/...",
        "key": "bulk/upload-1719200000-abc/report.pdf"
    }

Environment variables:
    INPUT_BUCKET             bucket where files are staged
    BULK_STAGING_PREFIX      staging prefix (default: ``bulk/``)
    URL_EXPIRATION_SECONDS   presigned URL lifetime (default: 300)
"""

import json
import logging
import os
import posixpath
import base64

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")

INPUT_BUCKET = os.environ["INPUT_BUCKET"]
BULK_STAGING_PREFIX = os.environ.get("BULK_STAGING_PREFIX", "bulk/")
URL_EXPIRATION_SECONDS = int(os.environ.get("URL_EXPIRATION_SECONDS", "300"))


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
    batch_id = (payload.get("batch_id") or "").strip()

    safe_filename = _sanitize(filename)
    safe_batch_id = _sanitize(batch_id)

    if not safe_filename:
        return _response(400, {"error": "Missing or unsafe 'filename'."})
    if not safe_batch_id:
        return _response(400, {"error": "Missing or unsafe 'batch_id'."})

    key = f"{BULK_STAGING_PREFIX}{safe_batch_id}/{safe_filename}"

    try:
        url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": INPUT_BUCKET, "Key": key},
            ExpiresIn=URL_EXPIRATION_SECONDS,
        )
    except Exception as exc:
        logger.exception("Failed to generate presigned URL")
        return _response(500, {"error": f"Failed to generate URL: {exc}"})

    logger.info("Generated presigned URL for s3://%s/%s", INPUT_BUCKET, key)
    return _response(200, {"upload_url": url, "key": key})
