"""Shared constants for the MIAX AgentCore RAG project."""

# The three metadata permission filter values that gate document access.
# Every document ingested into the RAG pipeline is stamped with exactly one of
# these values under the ``permissions_group`` metadata key. At query time the
# agent applies a metadata filter so callers only retrieve chunks their
# permission group is allowed to see.
PERMISSION_GROUPS = [
    "permissions_group_a",
    "permissions_group_b",
    "permissions_group_c",
]

# The metadata key used for Bedrock Knowledge Base metadata filtering. This key
# must appear in the ``.metadata.json`` sidecar files written next to each
# source document and must be declared as a filterable field on the vector
# index.
PERMISSION_METADATA_KEY = "permissions_group"

# Logical resource prefix used to name resources consistently.
PROJECT_PREFIX = "miax"

# Prefix in the input bucket under which the front end stages files for a CSV
# bulk upload, e.g. ``bulk/<batch_id>/<filename>``. Files placed here are NOT
# processed by the single-file ingest trigger (which requires a permission-group
# prefix); they are instead processed by the bulk-ingest Lambda once the UI
# submits the manifest CSV.
BULK_STAGING_PREFIX = "bulk/"


# Default AWS region for the project.
DEFAULT_REGION = "us-east-1"
