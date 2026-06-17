"""Single-file ingest Lambda.

Triggered by ``Object Created`` events (delivered via EventBridge) on the
**input bucket**. For each uploaded document this function:

1. Reads the document's permission group from the **S3 object metadata** set by
   the uploader (e.g. ``x-amz-meta-permissions_group: permissions_group_b``).
   The permission is intentionally NOT derived from the object key / path - the
   front end stamps it as metadata at upload time. Objects whose metadata is
   missing or not in the allow-list are rejected so we never ingest
   unfilterable content.
2. Copies the object into the **source bucket** (preserving the key).
3. Writes a Bedrock sidecar metadata file ``<key>.metadata.json`` carrying the
   ``permissions_group`` attribute, which Bedrock stores on every chunk and the
   agent uses as a retrieval filter.
4. Deletes the original object from the input bucket.
5. Asynchronously invokes the KB-sync Lambda to (re)embed the source bucket.

Environment variables:
    SOURCE_BUCKET            target bucket for curated docs + sidecars
    KB_SYNC_FUNCTION_NAME    name of the KB-sync Lambda to invoke
    PERMISSION_METADATA_KEY  metadata key (default: ``permissions_group``)
    PERMISSION_GROUPS        comma-separated list of valid groups
    BULK_STAGING_PREFIX      staging prefix to ignore (default: ``bulk/``)
"""

import json
import logging
import os
import urllib.parse

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
BULK_STAGING_PREFIX = os.environ.get("BULK_STAGING_PREFIX", "bulk/")

METADATA_SUFFIX = ".metadata.json"


def _is_safe_key(key: str) -> bool:
    """Reject keys that could escape the intended prefix or are otherwise unsafe."""
    if not key or key.startswith("/"):
        return False
    parts = key.split("/")
    if any(part in ("..", ".") for part in parts):
        return False
    return True


def _resolve_permission_group(bucket: str, key: str) -> str | None:
    """Return the permission group from the object's S3 user metadata.

    S3 lowercases user-metadata keys, so we look the configured key up
    case-insensitively. Returns None when missing or not in the allow-list.
    """
    head = s3.head_object(Bucket=bucket, Key=key)
    metadata = {k.lower(): v for k, v in head.get("Metadata", {}).items()}
    candidate = (metadata.get(PERMISSION_METADATA_KEY.lower()) or "").strip()
    return candidate if candidate in PERMISSION_GROUPS else None


def _extract_records(event: dict) -> list[tuple[str, str]]:
    """Return (bucket, key) tuples from either EventBridge or native S3 events."""
    records: list[tuple[str, str]] = []

    # EventBridge "Object Created" shape.
    if event.get("detail-type") == "Object Created" and "detail" in event:
        detail = event["detail"]
        bucket = detail.get("bucket", {}).get("name")
        key = detail.get("object", {}).get("key")
        if bucket and key:
            records.append((bucket, urllib.parse.unquote_plus(key)))
        return records

    # Native S3 notification shape (fallback).
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        records.append((bucket, key))
    return records


def _trigger_kb_sync() -> None:
    if not KB_SYNC_FUNCTION_NAME:
        return
    try:
        lambda_client.invoke(
            FunctionName=KB_SYNC_FUNCTION_NAME,
            InvocationType="Event",  # async
            Payload=b"{}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to trigger KB-sync (non-fatal).")


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event)[:2000])

    processed = 0
    for src_bucket, key in _extract_records(event):
        if key.endswith(METADATA_SUFFIX):
            logger.info("Skipping metadata sidecar object: %s", key)
            continue
        if key.startswith(BULK_STAGING_PREFIX):
            logger.info("Skipping bulk-staged object (handled by bulk Lambda): %s", key)
            continue
        if not _is_safe_key(key):
            logger.error("Rejecting unsafe object key: %s", key)
            continue

        group = _resolve_permission_group(src_bucket, key)
        if group is None:
            logger.error(
                "Object '%s' has no valid '%s' metadata (must be one of %s); "
                "skipping to avoid unfiltered content.",
                key,
                PERMISSION_METADATA_KEY,
                PERMISSION_GROUPS,
            )
            continue

        dest_key = key
        logger.info(
            "Copying s3://%s/%s -> s3://%s/%s (group=%s)",
            src_bucket, key, SOURCE_BUCKET, dest_key, group,
        )

        s3.copy_object(
            Bucket=SOURCE_BUCKET,
            Key=dest_key,
            CopySource={"Bucket": src_bucket, "Key": key},
            MetadataDirective="COPY",
        )

        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key=f"{dest_key}{METADATA_SUFFIX}",
            Body=json.dumps(
                {"metadataAttributes": {PERMISSION_METADATA_KEY: group}}
            ).encode("utf-8"),
            ContentType="application/json",
        )

        s3.delete_object(Bucket=src_bucket, Key=key)
        logger.info("Ingested and removed source object s3://%s/%s", src_bucket, key)
        processed += 1

    if processed:
        _trigger_kb_sync()

    return {"statusCode": 200, "processed": processed}
