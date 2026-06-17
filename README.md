# MIAX — AgentCore RAG with Metadata Permission Filtering

An AWS CDK (Python) project that stands up a Retrieval-Augmented Generation
(RAG) pipeline on Amazon Bedrock with **per-document permission filtering**.
Documents are tagged with one of three permission groups, and the Bedrock
**AgentCore Runtime** agent only retrieves chunks the caller is allowed to see.

* **Vector store:** Amazon S3 Vectors
* **Embeddings:** `amazon.titan-embed-text-v2` (1024-dim)
* **Generation model:** `anthropic.claude-sonnet-4-6`
* **Region:** `us-east-1`
* **Permission groups:** `permissions_group_a`, `permissions_group_b`, `permissions_group_c`

---

## Architecture

```
 [ Future drag/drop front end ]
   ┌─────────────────────────────┬──────────────────────────────────┐
   │ A) SINGLE FILE              │ B) BULK CSV                        │
   │ pick a group, upload a file │ drop many files + a manifest.csv   │
   │ PUT <file>                  │ PUT bulk/<batch>/<file> + manifest │
   │ x-amz-meta-permissions_     │ then call Bulk-Ingest Function URL │
   │   group: permissions_group_x│ (group per row in the CSV)         │
   └──────────────┬──────────────┴──────────────────┬────────────────┘
                  │                                  │
                  ▼                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                       INPUT BUCKET (miax-input-*)              │
   │   <file> (+ S3 metadata)             bulk/<batch>/<file>       │
   └───────┬──────────────────────────────────────┬────────────────┘
            │ s3:ObjectCreated (EventBridge)        │ invoke (Function URL, IAM)
            ▼                                       ▼
   ┌──────────────────────┐            ┌──────────────────────────────┐
   │   INGEST LAMBDA       │            │   BULK-INGEST LAMBDA          │
   │   • group from S3     │            │   • parse manifest.csv        │
   │     object metadata   │            │   • group per CSV row         │
   │   • copy → source     │            │   • per row: copy → source    │
   │   • write sidecar     │            │   • write sidecar per file    │
   │   • delete from input │            │   • delete staged + manifest  │
   └──────────┬───────────┘            └──────────────┬───────────────┘
               │            both trigger KB-sync ↓     │
               └───────────────┬────────────────────────┘
                               ▼
   ┌──────────────────────┐         ┌──────────────────────────────┐
   │   SOURCE BUCKET       │ ◄───────│ <file> + <file>.metadata.json │
   │   miax-source-*       │         │ {"metadataAttributes":{       │
   │   (KB data source)    │         │   "permissions_group":"...a"}}│
   └──────────┬───────────┘         └──────────────────────────────┘
               │ KB-SYNC LAMBDA starts an ingestion job
               │ (embeddings written to S3 Vectors)
               ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                BEDROCK KNOWLEDGE BASE                         │
   │   embeddings: titan-embed-text-v2                             │
   │   storage:    S3 VECTORS (miax-vectors-* bucket + index)      │
   │   filterable metadata: permissions_group                      │
   └───────────────────────────────┬───────────────────────────────┘
                                     │ RetrieveAndGenerate(filter=permissions_group)
                                     ▼
   ┌─────────────────────────────────────────────────────────────┐
   │         QUERY LAMBDA  (HTTPS endpoint for the UI)            │
   │   POST { username, permission_group, prompt }                 │
   │   • Retrieve from KB with metadata filter                     │
   │   • Generate grounded answer (Converse)                       │
   │   • Log 1 row → DynamoDB audit table                          │
   │   • Return answer + citations + tokens + latency              │
   │   auth: AWS IAM / SigV4   model: claude-sonnet-4-6            │
   └───────────────┬─────────────────────────────────┬────────────┘
                   │                                   │
                   ▼                                   ▼
   ┌──────────────────────────┐        ┌──────────────────────────┐
   │   DYNAMODB QUERY LOG      │        │  (optional) AGENTCORE     │
   │   1 row / query: user,    │        │   RUNTIME container       │
   │   group, query, response, │        │   -c deployAgent=true     │
   │   latency, tokens, cites  │        └──────────────────────────┘
   └──────────────────────────┘
```


Note: the permission group is carried in **document metadata** (S3 object
metadata on upload → Bedrock `.metadata.json` sidecar → filterable vector
metadata), never in the S3 key/path. The agent endpoint uses **AWS IAM (SigV4)**
auth — no JWT/OIDC.


### Stacks

| Stack | Type | Resources |
|-------|------|-----------|
| **MiaxStateful** | Data | KMS key, access-logs/input/source buckets, S3 Vectors bucket + index, Bedrock Knowledge Base + S3 data source, **DynamoDB query-log** audit table (KMS-encrypted, PITR) |
| **MiaxStateless** | Compute | Ingest Lambda + S3 (EventBridge) trigger, Bulk-Ingest Lambda + Function URL, **Query Lambda + Function URL (the agent endpoint)**, KB-sync Lambda, Bedrock AgentCore Runtime (optional) |




`MiaxStateless` depends on `MiaxStateful` and consumes its bucket names and the
Knowledge Base id/arn. Splitting stateful from stateless lets you redeploy /
tear down compute without touching retained data (buckets use
`RemovalPolicy.RETAIN`).

---

## How permission filtering works

The permission group lives in **document metadata**, not the file path.

1. The front end (built later) uploads a file to the **input bucket** and sets
   the S3 object metadata `x-amz-meta-permissions_group: permissions_group_b`.
   (The S3 key/path is irrelevant to permissions.)
2. The **ingest Lambda** reads that object metadata, copies the file into the
   **source bucket**, and writes a Bedrock sidecar `report.pdf.metadata.json`:
   ```json
   { "metadataAttributes": { "permissions_group": "permissions_group_b" } }
   ```
3. Bedrock ingests the file and stamps every chunk with `permissions_group`.
   The S3 Vectors index keeps that key **filterable**.
4. The **AgentCore agent** receives `{ "prompt": "...", "permission_group":
   "permissions_group_b" }` and adds a retrieval filter
   `equals(permissions_group, permissions_group_b)` so only that group's content
   is ever returned.

> The bulk path is identical except the group comes from each manifest CSV row
> rather than per-object metadata.

---

## Bulk upload (CSV manifest)


For uploading many files at once, the front end stages files under a batch
prefix and submits a manifest CSV mapping each filename to its permission group.

**Manifest format** (`filename,permissions`, one row per file):

```csv
filename,permissions
q3-report.pdf,permissions_group_a
roadmap.docx,permissions_group_b
hr-policy.pdf,permissions_group_c
```

The group column may be `permissions` (preferred), `permission_group`, or
`permissions_group`; the file column may be `filename` or `file_name`.


**Flow:**
1. UI stages each file to `bulk/<batch_id>/<filename>` in the input bucket
   (these are ignored by the single-file trigger).
2. UI uploads the manifest to `bulk/<batch_id>/manifest.csv` (or passes the CSV
   inline in the request body).
3. UI invokes the **bulk-ingest Lambda** via its IAM-authenticated Function URL
   (output `BulkIngestFunctionUrl`):
   ```json
   { "batch_id": "<batch_id>" }
   ```
   or, with an inline manifest:
   ```json
   { "batch_id": "<batch_id>", "manifest_csv": "filename,permission_group\nq3-report.pdf,permissions_group_a" }
   ```
4. The Lambda validates each group, copies the file into the source bucket under
   a neutral `docs/<filename>` prefix, writes the `.metadata.json` sidecar
   carrying the permission group, and cleans up the staged files + manifest. It
   returns `{ processed: [...], errors: [...] }`.


Both upload paths converge on the same source bucket + sidecar mechanism, so the
Knowledge Base ingests them identically.

---


## Prerequisites

* Python 3.12+
* AWS CDK v2 CLI **>= 2.1127.0** — `npm install -g aws-cdk@latest`
  (the library is pinned to a recent `aws-cdk-lib`; older CLIs will report a
  "Cloud assembly schema version mismatch". You can also run the CLI ad-hoc via
  `npx aws-cdk@latest <cmd>`.)

* Docker (only needed when deploying the AgentCore runtime, `-c deployAgent=true`)
* Bedrock model access enabled in `us-east-1` for Titan Embed Text v2 and
  Claude Sonnet 4.6.

---

## Setup (virtual environment)

```bash
cd ~/Desktop/miax

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
# (optional) dev/test deps
pip install -r requirements-dev.txt
```

## Deploy

```bash
# One-time per account/region
cdk bootstrap aws://<ACCOUNT_ID>/us-east-1

# Synthesize / review
cdk synth

# Deploy the data + pipeline (no agent container build)
cdk deploy MiaxStateful MiaxStateless

# Deploy everything including the AgentCore runtime (requires Docker)
cdk deploy --all -c deployAgent=true
```

## Try it

```bash
# 1. Upload a doc tagged for group B via S3 OBJECT METADATA (not the path)
aws s3 cp ./report.pdf \
  s3://miax-input-<ACCOUNT>-us-east-1/report.pdf \
  --metadata permissions_group=permissions_group_b

# 2. The ingest Lambda + KB-sync run automatically. (You can also trigger a
#    sync manually if needed.)
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <KB_ID> --data-source-id <DS_ID> --region us-east-1

# 3. Query the agent via the HTTPS endpoint (output `QueryFunctionUrl`).
#    Requests are SigV4-signed (IAM auth). awscurl signs for you:
awscurl --service lambda --region us-east-1 -X POST "<QUERY_FUNCTION_URL>" \
  -d '{"username":"alice@example.com","permission_group":"permissions_group_b","prompt":"Summarize the report"}'
```

Sample response (one row is also written to the DynamoDB audit table):

```json
{
  "answer": "Q3 revenue was ...",
  "permission_group": "permissions_group_b",
  "citation_count": 2,
  "usage": { "tokens_in": 1840, "tokens_out": 215 },
  "latency_ms": 1320,
  "citations": [
    {
      "content": "...the exact retrieved chunk text...",
      "source_uri": "s3://miax-source-<ACCOUNT>-us-east-1/docs/report.pdf",
      "location_type": "S3",
      "score": 0.82,
      "permission_group": "permissions_group_b"
    }
  ]
}
```

## The agent endpoint (for the UI)

Your colleague's UI should call the **`QueryFunctionUrl`** stack output (an
IAM-authenticated HTTPS Lambda Function URL). It accepts a POST body:

```json
{ "username": "...", "permission_group": "permissions_group_x", "prompt": "..." }
```

and returns `{ answer, citations[], usage, latency_ms }`. The Lambda retrieves
permission-filtered chunks from the Knowledge Base, generates a grounded answer,
and the UI can render the citations alongside the answer.

> Requests must be SigV4-signed. Front ends typically call this through a small
> backend/BFF that holds AWS credentials (or an API Gateway with Cognito) rather
> than signing in the browser. The optional AgentCore runtime
> (`-c deployAgent=true`) is an alternative invocation path via
> `bedrock-agentcore invoke-agent-runtime`.

## Logging & audit

* **DynamoDB query-log table** (`QueryLogTableName` output) — **one row per
  query** with: `username`, `permission_group`, `query`, `response`,
  `latency_ms`, `tokens_in`, `tokens_out`, `citations`, and `timestamp`.
  Partitioned by `username` (time-sorted) with a `by-permission-group` GSI; KMS
  encrypted with PITR enabled.
* **CloudWatch Logs** — structured logs from every Lambda (configurable
  retention, default 90 days) + the AgentCore runtime.
* **S3 server access logs** — `miax-access-logs-*` bucket records reads/writes
  on the input and source buckets.
* **X-Ray** — active tracing on the Lambdas.



## Configuration (cdk context)

| Key | Default | Description |
|-----|---------|-------------|
| `deployAgent` | `false` | Build + deploy the AgentCore runtime container |
| `embeddingModelArn` | Titan Embed Text v2 | Embedding model ARN |
| `embeddingDimension` | `1024` | Must match the embedding model |
| `agentModelId` | `anthropic.claude-sonnet-4-6` | Generation model id |

## Project layout

```
miax/
├── app.py                       # CDK app entry (2 stacks)
├── cdk.json
├── requirements.txt
├── requirements-dev.txt
├── README.md
├── miax/
│   ├── constants.py             # permission groups + metadata key
│   ├── stateful_stack.py        # buckets, S3 Vectors, Knowledge Base
│   └── stateless_stack.py       # ingest Lambda + AgentCore runtime
├── lambda/
│   ├── ingest/handler.py        # single-file: move doc + write metadata sidecar
│   └── bulk_ingest/handler.py   # CSV bulk: parse manifest, move + sidecar per row
└── agent/

    ├── agent.py                 # AgentCore RAG entrypoint (filtered retrieval)
    ├── requirements.txt
    └── Dockerfile               # linux/arm64 runtime image
```

## Coming soon

A drag-and-drop front end where users select a permission group and upload
files. It will issue presigned PUTs to the input bucket with the chosen group
set as S3 object metadata (`x-amz-meta-permissions_group`), or use the bulk CSV
path for many files at once — the rest of the pipeline already handles tagging
and filtering automatically.

