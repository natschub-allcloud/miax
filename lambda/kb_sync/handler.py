"""KB-sync Lambda.

Starts a Bedrock Knowledge Base ingestion job so that documents newly written
to the source bucket are embedded into the vector store. Invoked asynchronously
by the ingest / bulk-ingest Lambdas after they finish writing.

To avoid launching redundant, overlapping jobs (Bedrock only allows one active
ingestion job per data source at a time), this function is **debounced**: it
first lists in-progress jobs and skips starting a new one if any is already
``STARTING`` or ``IN_PROGRESS``. The Lambda is also configured with reserved
concurrency of 1 so starts are serialised.

Environment variables:
    KNOWLEDGE_BASE_ID  the Bedrock Knowledge Base id
    DATA_SOURCE_ID     the data source id within the KB
"""

import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

bedrock_agent = boto3.client("bedrock-agent")

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
DATA_SOURCE_ID = os.environ["DATA_SOURCE_ID"]

_ACTIVE_STATUSES = {"STARTING", "IN_PROGRESS"}


def _has_active_job() -> bool:
    """Return True if an ingestion job is already starting or running."""
    try:
        resp = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            maxResults=10,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        )
    except Exception:  # noqa: BLE001
        # If listing fails, fall through and attempt to start; Bedrock will
        # reject a concurrent job, which we handle below.
        logger.exception("Could not list ingestion jobs; will attempt to start.")
        return False

    for job in resp.get("ingestionJobSummaries", []):
        if job.get("status") in _ACTIVE_STATUSES:
            logger.info(
                "Active ingestion job %s (%s) already running; skipping start.",
                job.get("ingestionJobId"),
                job.get("status"),
            )
            return True
    return False


def handler(event, context):
    logger.info(
        "KB-sync invoked for KB=%s data_source=%s",
        KNOWLEDGE_BASE_ID,
        DATA_SOURCE_ID,
    )

    if _has_active_job():
        return {"started": False, "reason": "active_job_exists"}

    try:
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            description="Automated sync triggered by document ingestion.",
        )
        job_id = resp.get("ingestionJob", {}).get("ingestionJobId")
        logger.info("Started ingestion job %s", job_id)
        return {"started": True, "ingestion_job_id": job_id}
    except bedrock_agent.exceptions.ConflictException:
        # A job started between our check and now; that's fine.
        logger.info("Ingestion job already in progress (conflict); skipping.")
        return {"started": False, "reason": "conflict"}
