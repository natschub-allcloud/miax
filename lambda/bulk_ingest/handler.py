"""Bulk-ingest Lambda.

Invoked by the front end (e.g. via a Lambda Function URL or API Gateway) after a
user drag/drops a batch of files **plus a manifest CSV** for bulk upload.

Workflow expected from the UI:
1. The UI stages each file in the input bucket under the bulk prefix, e.g.
   ``bulk/<batch_id>/<filename>``.
2. The UI uploads (or inlines) a manifest CSV with one row per file mapping the
   file name to its permission group::

       filename,permission_group
       q3-report.pdf,permissions_group_a
       roadmap.docx,permissions_group_b

3. The UI invokes this Lambda with a JSON payload identifying the batch and the
   manifest.

This function parses the manifest, validates each permission group, then for
every row copies the staged file into the **source bucket** under
``<group>/<filename>`` and writes the Bedrock ``.metadata.json`` sidecar - the
same mechanism the single-file ingest path uses, so both routes feed the
Knowledge Base identically. Staged files are deleted after a successful copy.

Request payload (JSON)::

    {
        "batch_id": "2026-06-16T10-00-00-abc123",       # required
        "manifest_key": "bulk/<batch_id>/manifest.csv", # optional; defaults to
                                                          # <bulk_prefix><batch_id>/manifest.csv
        "manifest_csv": "filename,permission_group\\n..."# optional inline CSV
                                                          # (used if no manifest_key)
    }

Response (JSON)::

    {
        "batch_id": "...",
        "processed": [ {"filename": "...", "permission_group": "...", "source_key": "..."} ],
        "errors":    [ {"filename": "...", "reason": "..."} ]
    }

Environment variables:
    INPUT_BUCKET             bucket where the UI stages files (+ manifest)
    SOURCE_BUCKET            target bucket for curated docs + sidecars
    BULK_STAGING_PREFIX      staging prefix (default: ``bulk/``)
    PERMISSION_METADATA_KEY  metadata key (default: ``permissions_group``)
    PERMISSION_GROUPS        comma-separated list of valid groups
"""

import csv
import io
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

INPUT_BUCKET = os.environ["INPUT_BUCKET"]
SOURCE_BUCKET = os.environ["SOURCE_BUCKET"]
BULK_STAGING_PREFIX = os.environ.get("BULK_STAGING_PREFIX", "bulk/")
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

METADATA_SUFFIX = ".metadata.json"


def _load_payload(event: dict) -> dict:
    """Accept either a direct Lambda invoke payload or a Function URL/API GW event."""
    if isinstance(event, dict) and "body" in event and "batch_id" not in event:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        return json.loads(body)
    return event or {}


def _read_manifest(payload: dict, batch_id: str) -> str:
    """Return the manifest CSV text, from an inline string or from S3."""
    inline = payload.get("manifest_csv")
    if inline:
        return inline

    manifest_key = payload.get("manifest_key") or (
        f"{BULK_STAGING_PREFIX}{batch_id}/manifest.csv"
    )
    obj = s3.get_object(Bucket=INPUT_BUCKET, Key=manifest_key)
    return obj["Body"].read().decode("utf-8")


def _staged_key(batch_id: str, filename: str) -> str:
    return f"{BULK_STAGING_PREFIX}{batch_id}/{filename}"


def handler(event, context):
    logger.info("Received bulk-ingest event: %s", json.dumps(event)[:2000])
    payload = _load_payload(event)

    batch_id = payload.get("batch_id")
    if not batch_id:
        return _response(400, {"error": "Missing required field 'batch_id'."})

    try:
        manifest_text = _read_manifest(payload, batch_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read manifest")
        return _response(400, {"error": f"Could not read manifest: {exc}"})

    processed = []
    errors = []

    reader = csv.DictReader(io.StringIO(manifest_text))
    for row in reader:
        # Tolerate header/whitespace variations.
        filename = (row.get("filename") or row.get("file_name") or "").strip()
        group = (
            row.get("permission_group")
            or row.get("permissions_group")
            or ""
        ).strip()

        if not filename or not group:
            errors.append({"filename": filename, "reason": "Missing filename or permission_group."})
            continue
        if group not in PERMISSION_GROUPS:
            errors.append(
                {
                    "filename": filename,
                    "reason": f"Invalid permission_group '{group}'. Must be one of {PERMISSION_GROUPS}.",
                }
            )
            continue

        staged_key = _staged_key(batch_id, filename)
        dest_key = f"{group}/{filename}"

        try:
            # 1. Copy staged file into the source bucket under the group prefix.
            s3.copy_object(
                Bucket=SOURCE_BUCKET,
                Key=dest_key,
                CopySource={"Bucket": INPUT_BUCKET, "Key": staged_key},
                MetadataDirective="COPY",
            )

            # 2. Write the Bedrock sidecar metadata file.
            s3.put_object(
                Bucket=SOURCE_BUCKET,
                Key=f"{dest_key}{METADATA_SUFFIX}",
                Body=json.dumps(
                    {"metadataAttributes": {PERMISSION_METADATA_KEY: group}}
                ).encode("utf-8"),
                ContentType="application/json",
            )

            # 3. Remove the staged file from the input bucket.
            s3.delete_object(Bucket=INPUT_BUCKET, Key=staged_key)

            processed.append(
                {
                    "filename": filename,
                    "permission_group": group,
                    "source_key": dest_key,
                }
            )
            logger.info("Bulk-ingested %s -> s3://%s/%s", filename, SOURCE_BUCKET, dest_key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process %s", filename)
            errors.append({"filename": filename, "reason": str(exc)})

    # Best-effort cleanup of the manifest itself when it lives in S3.
    if not payload.get("manifest_csv"):
        manifest_key = payload.get("manifest_key") or (
            f"{BULK_STAGING_PREFIX}{batch_id}/manifest.csv"
        )
        try:
            s3.delete_object(Bucket=INPUT_BUCKET, Key=manifest_key)
        except Exception:  # noqa: BLE001
            logger.warning("Could not delete manifest %s", manifest_key)

    return _response(
        200,
        {"batch_id": batch_id, "processed": processed, "errors": errors},
    )


def _response(status: int, body: dict) -> dict:
    """Shape a response that works for both direct invokes and Function URLs."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
