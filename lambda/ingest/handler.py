"""Ingest Lambda.

Triggered by ``s3:ObjectCreated:*`` on the **input bucket**. For each uploaded
document this function:

1. Derives the document's permission group from the object key. The front end
   uploads to a prefix that names the chosen group, e.g.
   ``permissions_group_b/quarterly-report.pdf``. If no recognised prefix is
   present the upload is rejected (left in place / logged) so we never ingest
   an unlabeled — and therefore unfilterable — document.
2. Copies the object into the **source bucket** under ``<group>/<filename>``.
3. Writes a Bedrock sidecar metadata file ``<key>.metadata.json`` next to the
   copied object, stamping the ``permissions_group`` attribute. Bedrock reads
   this sidecar at ingestion time and stores the attribute on every chunk so it
   can be used as a retrieval filter.
4. Deletes the original object from the input bucket.

Environment variables:
    SOURCE_BUCKET            target bucket for curated docs + sidecars
    PERMISSION_METADATA_KEY  metadata key (default: ``permissions_group``)
    PERMISSION_GROUPS        comma-separated list of valid groups
"""

import json
import logging
import os
import urllib.parse

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

SOURCE_BUCKET = os.environ["SOURCE_BUCKET"]
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

# Bedrock requires sidecar metadata files to use this exact suffix.
METADATA_SUFFIX = ".metadata.json"

# Files staged here are part of a CSV bulk upload and are processed by the
# bulk-ingest Lambda, not by this single-file trigger.
BULK_STAGING_PREFIX = os.environ.get("BULK_STAGING_PREFIX", "bulk/")



def _resolve_permission_group(key: str) -> str | None:
    """Return the permission group encoded as the first path segment of *key*."""
    parts = key.split("/")
    if len(parts) < 2:
        return None
    candidate = parts[0]
    return candidate if candidate in PERMISSION_GROUPS else None


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event))

    for record in event.get("Records", []):
        src_bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        # Never re-process a sidecar file we (or anyone) may have dropped.
        if key.endswith(METADATA_SUFFIX):
            logger.info("Skipping metadata sidecar object: %s", key)
            continue

        # Files staged for a CSV bulk upload are handled by the bulk-ingest
        # Lambda, which knows the permission group from the manifest row.
        if key.startswith(BULK_STAGING_PREFIX):
            logger.info("Skipping bulk-staged object (handled by bulk Lambda): %s", key)
            continue

        group = _resolve_permission_group(key)

        if group is None:
            logger.error(
                "Object '%s' is not under a recognised permission-group prefix "
                "(%s); skipping ingestion to avoid unfiltered content.",
                key,
                PERMISSION_GROUPS,
            )
            continue

        # Preserve the relative path beneath the group prefix.
        dest_key = key

        logger.info(
            "Copying s3://%s/%s -> s3://%s/%s (group=%s)",
            src_bucket,
            key,
            SOURCE_BUCKET,
            dest_key,
            group,
        )

        # 1. Copy the document into the source bucket.
        s3.copy_object(
            Bucket=SOURCE_BUCKET,
            Key=dest_key,
            CopySource={"Bucket": src_bucket, "Key": key},
            MetadataDirective="COPY",
        )

        # 2. Write the Bedrock sidecar metadata file.
        sidecar_key = f"{dest_key}{METADATA_SUFFIX}"
        sidecar_body = {
            "metadataAttributes": {
                PERMISSION_METADATA_KEY: group,
            }
        }
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key=sidecar_key,
            Body=json.dumps(sidecar_body).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("Wrote sidecar s3://%s/%s", SOURCE_BUCKET, sidecar_key)

        # 3. Delete the original from the input bucket.
        s3.delete_object(Bucket=src_bucket, Key=key)
        logger.info("Deleted source object s3://%s/%s", src_bucket, key)

    return {"statusCode": 200, "body": "ok"}
