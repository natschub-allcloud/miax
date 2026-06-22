"""Bulk-ingest Lambda.

Invoked by the front end via **API Gateway** (``POST /bulk-ingest`` with an
``x-api-key`` header) after a user drag/drops a batch of files **plus a manifest
CSV** for bulk upload. The CSV is the source of truth for permissions: each row
maps a filename to its permission group (unlike the single-file path, where the
permission comes from one UI field).


Workflow expected from the UI:
1. Stage each file in the input bucket under ``bulk/<batch_id>/<filename>``.
2. Provide a manifest CSV mapping file name -> permission group, either staged
   at ``bulk/<batch_id>/manifest.csv`` or passed inline in the request body::

       filename,permissions
       q3-report.pdf,permissions_group_a
       roadmap.docx,permissions_group_b

   The group column may be named ``permissions`` (preferred), ``permission_group``
   or ``permissions_group``; the file column may be ``filename`` or ``file_name``.


3. Invoke this Lambda with ``{"batch_id": "..."}`` (or include ``manifest_csv``).

For every valid row the file is copied into the **source bucket** under a
neutral ``docs/<filename>`` prefix with a Bedrock ``.metadata.json`` sidecar
that carries the permission group. The group is stored ONLY in metadata (never
in the path), identical to the single-file path. The staged copy is deleted,
and finally the KB-sync Lambda is invoked once to (re)embed the source bucket.


Security:
* Filenames are sanitised; rows attempting path traversal are rejected.
* Permission groups are validated against the allow-list.
* The request body size and row count are bounded.

Environment variables:
    INPUT_BUCKET             bucket where the UI stages files (+ manifest)
    SOURCE_BUCKET            target bucket for curated docs + sidecars
    KB_SYNC_FUNCTION_NAME    name of the KB-sync Lambda to invoke
    BULK_STAGING_PREFIX      staging prefix (default: ``bulk/``)
    PERMISSION_METADATA_KEY  metadata key (default: ``permissions_group``)
    PERMISSION_GROUPS        comma-separated list of valid groups
"""

import base64
import csv
import io
import json
import logging
import os
import posixpath

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")

INPUT_BUCKET = os.environ["INPUT_BUCKET"]
SOURCE_BUCKET = os.environ["SOURCE_BUCKET"]
KB_SYNC_FUNCTION_NAME = os.environ.get("KB_SYNC_FUNCTION_NAME")
BULK_STAGING_PREFIX = os.environ.get("BULK_STAGING_PREFIX", "bulk/")
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
MAX_ROWS = 5000
MAX_MANIFEST_BYTES = 5 * 1024 * 1024  # 5 MB


def _load_payload(event: dict) -> dict:
    """Accept either a direct Lambda invoke payload or a Function URL event."""
    if isinstance(event, dict) and "body" in event and "batch_id" not in event:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return json.loads(body)
    return event or {}


def _sanitize_filename(filename: str) -> str | None:
    """Return a safe relative path for *filename* or None if it is unsafe."""
    if not filename:
        return None
    name = filename.strip()
    # Reject absolute paths and Windows-style absolute/UNC paths outright.
    if name.startswith("/") or name.startswith("\\") or ":" in name.split("/")[0]:
        return None
    # Normalise and reject traversal / absolute escapes.
    normalised = posixpath.normpath(name)
    if normalised.startswith("..") or normalised.startswith("/") or normalised == ".":
        return None

    if any(part == ".." for part in normalised.split("/")):
        return None
    if normalised.endswith(METADATA_SUFFIX):
        return None
    return normalised


def _read_manifest(payload: dict, batch_id: str) -> str:
    inline = payload.get("manifest_csv")
    if inline:
        if len(inline.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ValueError("Inline manifest exceeds size limit.")
        return inline

    manifest_key = payload.get("manifest_key") or (
        f"{BULK_STAGING_PREFIX}{batch_id}/manifest.csv"
    )
    obj = s3.get_object(Bucket=INPUT_BUCKET, Key=manifest_key)
    return obj["Body"].read(MAX_MANIFEST_BYTES + 1).decode("utf-8")


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
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    logger.info("Received bulk-ingest event: %s", json.dumps(event)[:2000])
    try:
        payload = _load_payload(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(400, {"error": f"Invalid request body: {exc}"})

    batch_id = (payload.get("batch_id") or "").strip()
    if not batch_id or not _sanitize_filename(batch_id):
        return _response(400, {"error": "Missing or invalid 'batch_id'."})

    try:
        manifest_text = _read_manifest(payload, batch_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read manifest")
        return _response(400, {"error": f"Could not read manifest: {exc}"})

    processed: list[dict] = []
    errors: list[dict] = []

    reader = csv.DictReader(io.StringIO(manifest_text))
    for i, row in enumerate(reader):
        if i >= MAX_ROWS:
            errors.append({"reason": f"Row limit ({MAX_ROWS}) exceeded; remaining rows ignored."})
            break

        raw_name = (row.get("filename") or row.get("file_name") or "").strip()
        # Accept several column spellings: "permissions" (preferred),
        # "permission_group", or "permissions_group".
        group = (
            row.get("permissions")
            or row.get("permission_group")
            or row.get("permissions_group")
            or ""
        ).strip()


        safe_name = _sanitize_filename(raw_name)
        if not safe_name:
            errors.append({"filename": raw_name, "reason": "Missing or unsafe filename."})
            continue
        if group not in PERMISSION_GROUPS:
            errors.append(
                {"filename": raw_name, "reason": f"Invalid permission_group '{group}'."}
            )
            continue

        staged_key = f"{BULK_STAGING_PREFIX}{batch_id}/{safe_name}"
        # The permission group lives ONLY in the Bedrock metadata sidecar, never
        # in the destination path. Files are stored under a neutral docs/ prefix.
        dest_key = f"docs/{safe_name}"


        try:
            s3.copy_object(
                Bucket=SOURCE_BUCKET,
                Key=dest_key,
                CopySource={"Bucket": INPUT_BUCKET, "Key": staged_key},
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
            s3.delete_object(Bucket=INPUT_BUCKET, Key=staged_key)
            processed.append(
                {"filename": safe_name, "permission_group": group, "source_key": dest_key}
            )
            logger.info("Bulk-ingested %s -> s3://%s/%s", safe_name, SOURCE_BUCKET, dest_key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process %s", safe_name)
            errors.append({"filename": safe_name, "reason": str(exc)})

    # Best-effort cleanup of the manifest in S3.
    if not payload.get("manifest_csv"):
        manifest_key = payload.get("manifest_key") or (
            f"{BULK_STAGING_PREFIX}{batch_id}/manifest.csv"
        )
        try:
            s3.delete_object(Bucket=INPUT_BUCKET, Key=manifest_key)
        except Exception:  # noqa: BLE001
            logger.warning("Could not delete manifest %s", manifest_key)

    if processed:
        _trigger_kb_sync()

    status = 200 if processed or not errors else 422
    return _response(
        status,
        {"batch_id": batch_id, "processed": processed, "errors": errors},
    )
