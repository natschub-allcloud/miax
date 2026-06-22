# MIAX — AgentCore RAG with Metadata Permission Filtering

An AWS CDK (Python) project that stands up a Retrieval-Augmented Generation
(RAG) pipeline on Amazon Bedrock with **per-document permission filtering**.
Each document is tagged with one of three permission groups, and the agent only
retrieves chunks the caller's group is allowed to see.

* **Vector store:** Amazon S3 Vectors
* **Embeddings:** `amazon.titan-embed-text-v2` (1024-dim)
* **Generation model:** `anthropic.claude-sonnet-4-6`
* **Region:** `us-east-1`
* **Permission groups:** `permissions_group_a`, `permissions_group_b`, `permissions_group_c`
* **Front-end API:** one **API Gateway** REST API, **API-key** auth (`x-api-key`)

---

## Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │        Front end  (sends header x-api-key)     │
                    └───────┬───────────────┬───────────────┬────────┘
                            │               │               │
          POST /single-file │   POST /query │  POST /bulk-ingest
                            ▼               ▼               ▼
                    ┌────────────────────────────────────────────────┐
                    │           API GATEWAY (REST, API key)           │
                    └───────┬───────────────┬───────────────┬─────────┘
                            ▼               ▼               ▼
                    ┌───────────────┐ ┌───────────┐ ┌──────────────────┐
                    │ UPLOAD Lambda │ │ QUERY     │ │ BULK-INGEST       │
                    │ file+perm →   │ │ Lambda    │ │ Lambda            │
                    │ source bucket │ │ (agent)   │ │ CSV → per-row perm│
                    │ + sidecar     │ │           │ │ from bulk/ stage  │
                    └──────┬────────┘ └─────┬─────┘ └────────┬──────────┘
                           │                │                │
   (also: S3 INPUT bucket  │                │                │ reads bulk/<batch>/*
    ObjectCreated → INGEST  │                │                ▼  from INPUT bucket
    Lambda, perm from S3    │                │        ┌──────────────────┐
    object metadata)        ▼                │        │  INPUT BUCKET     │
                    ┌──────────────────┐     │        │  (staging: bulk/) │
                    │   SOURCE BUCKET   │◄────┼────────┘
                    │  docs/<file> +    │     │
                    │  <file>.metadata  │     │ all writers trigger KB-SYNC Lambda
                    │  .json (perm grp) │     │ → Bedrock ingestion job
                    └────────┬──────────┘     │
                             ▼                │
                    ┌─────────────────────────┴──────────────────────┐
                    │           BEDROCK KNOWLEDGE BASE                 │
                    │  embeddings: titan-embed-text-v2                 │
                    │  store: S3 VECTORS  •  filterable: permissions_group│
                    └─────────────────────────┬───────────────────────┘
                                              │ Retrieve(filter=permissions_group)
                                              ▼  + Converse(answer)
                    QUERY Lambda → answer + citations  →  DynamoDB audit row
```

The permission group is always carried in **document metadata** (S3 sidecar →
filterable vector metadata), never in the file path. Encryption at rest uses
default AWS-managed keys (POC simplicity).

### Stacks

| Stack | Type | Resources |
|-------|------|-----------|
| **MiaxStateful** | Data | Access-logs/input/source buckets, S3 Vectors bucket + index, Bedrock Knowledge Base + data source, DynamoDB query-log table |
| **MiaxStateless** | Compute | API Gateway (+ API key/usage plan), Upload/Query/Bulk-ingest/Ingest/KB-sync Lambdas, EventBridge rule, optional AgentCore runtime |

---

## The three front-end paths

All API calls require the header **`x-api-key: <key>`** and go to the
**`ApiBaseUrl`** stack output. There is no Cognito/IAM signing — the POC assumes
any caller holding the key is authorised.

### 1) Single-file upload  — `POST {ApiBaseUrl}single-file`
The UI's "one file + one permission" field. The permission is supplied directly.
```json
{
  "filename": "report.pdf",
  "permission_group": "permissions_group_a",
  "content_base64": "<base64 of the file bytes>"
}
```
→ `{ "ok": true, "source_key": "docs/report.pdf", "permission_group": "..." }`
The Lambda writes the file + `.metadata.json` sidecar to the source bucket and
triggers a KB sync. (Max ~9 MB via the API; for bigger files use the S3 path
below.)

### 2) Bulk upload (CSV)  — `POST {ApiBaseUrl}bulk-ingest`
For many files at once. The **CSV is the source of truth for permissions** —
each row maps a filename to its group. The UI first stages the files in the
input bucket under `bulk/<batch_id>/<filename>`, then calls:
```json
{ "batch_id": "2026-06-22-abc",
  "manifest_csv": "filename,permissions\nq3.pdf,permissions_group_a\nplan.docx,permissions_group_b" }
```
(or omit `manifest_csv` and stage it at `bulk/<batch_id>/manifest.csv`.)
→ `{ "batch_id": "...", "processed": [...], "errors": [...] }`
For each row it copies `bulk/<batch_id>/<file>` → `docs/<file>`, writes the
sidecar from the row's permission, removes the staged copy, then KB-syncs.

Manifest columns: `filename` (or `file_name`) + `permissions` (or
`permission_group`/`permissions_group`). Limits: ≤5000 rows, ≤5 MB.

### 3) Ask the agent  — `POST {ApiBaseUrl}query`
```json
{ "username": "alice", "permission_group": "permissions_group_a", "prompt": "What was Q3 revenue?" }
```
→
```json
{
  "answer": "Q3 revenue was ...",
  "permission_group": "permissions_group_a",
  "citation_count": 2,
  "usage": { "tokens_in": 1840, "tokens_out": 215 },
  "latency_ms": 1320,
  "citations": [
    { "content": "...chunk text...", "source_uri": "s3://miax-source-.../docs/report.pdf",
      "location_type": "S3", "score": 0.82, "permission_group": "permissions_group_a" }
  ]
}
```
The query Lambda retrieves with `equals(permissions_group, <caller group>)` so
only that group's content is returned, then generates a grounded answer and
writes one audit row to DynamoDB.

> Alternate single-file path (no API): upload straight to the **input bucket**
> with S3 object metadata `x-amz-meta-permissions_group=<group>`. The Ingest
> Lambda fires off the `ObjectCreated` event and processes it the same way.

---

## How permission filtering works

1. A document is stored in the **source bucket** with a sidecar
   `report.pdf.metadata.json` = `{"metadataAttributes":{"permissions_group":"permissions_group_a"}}`.
2. Bedrock ingests it and stamps every chunk with `permissions_group`; the S3
   Vectors index keeps that key **filterable**.
3. The query Lambda adds a retrieval filter
   `equals(permissions_group, <caller's group>)`, so cross-group content is
   never returned.

---

## Prerequisites

* Python 3.12+
* AWS CDK v2 CLI **>= 2.1127.0** (`npm i -g aws-cdk@latest`, or `npx aws-cdk@latest`)
* Bedrock model access enabled in `us-east-1` for Titan Embed Text v2 and
  Claude Sonnet 4.6
* Docker only if deploying the optional AgentCore runtime (`-c deployAgent=true`)

## Setup

```bash
cd miax
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # add -r requirements-dev.txt for tests
```

## Deploy

```bash
cdk deploy --all
# If the account was bootstrapped with a custom qualifier (e.g. on a shared
# account), pass it:
#   cdk deploy --all -c env=dev --context @aws-cdk/core:bootstrapQualifier=<qualifier>
```

Outputs printed at the end:
- `ApiBaseUrl` — base URL for the three routes
- `ApiKeyId` — fetch the secret with:
  `aws apigateway get-api-key --api-key <ApiKeyId> --include-value --query value --output text`
- `InputBucketName`, `QueryLogTableName`, plus (from MiaxStateful) `KnowledgeBaseId`, `DataSourceId`, bucket names

## Try it

```bash
API=<ApiBaseUrl>
KEY=$(aws apigateway get-api-key --api-key <ApiKeyId> --include-value --query value --output text)

# Single-file upload
B64=$(base64 -w0 report.pdf 2>/dev/null || base64 report.pdf)
curl -s -X POST "${API}single-file" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d "{\"filename\":\"report.pdf\",\"permission_group\":\"permissions_group_a\",\"content_base64\":\"$B64\"}"

# Ask the agent (group A sees it; group B should not)
curl -s -X POST "${API}query" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice","permission_group":"permissions_group_a","prompt":"What was Q3 revenue?"}'
```

## Configuration (cdk context)

| Key | Default | Description |
|-----|---------|-------------|
| `env` | `dev` | Environment name (suffixes stack/resource names for non-dev) |
| `deployAgent` | `false` | Also build + deploy the AgentCore container runtime (needs Docker) |
| `allowedOrigins` | `*` | CORS origins for the API (set explicit origins for prod) |
| `agentModelId` | `anthropic.claude-sonnet-4-6` | Generation model id |
| `embeddingModelId` | `amazon.titan-embed-text-v2:0` | Embedding model id |

## Logging & audit

* **DynamoDB query-log** (`QueryLogTableName`): one row per query — `username`,
  `permission_group`, `query`, `response`, `latency_ms`, `tokens_in`,
  `tokens_out`, `citations`, `timestamp` (PK `username`, SK `timestamp`, GSI
  `by-permission-group`).
* **CloudWatch Logs** for every Lambda + **S3 server access logs** +
  **X-Ray** tracing.

## Project layout

```
miax/
├── app.py                       # CDK app entry (2 stacks)
├── miax/
│   ├── config.py                # env/model/security config
│   ├── constants.py             # permission groups + metadata key
│   ├── stateful_stack.py        # buckets, S3 Vectors, KB, DynamoDB
│   └── stateless_stack.py       # API Gateway + Lambdas (+ optional AgentCore)
├── lambda/
│   ├── ingest/handler.py        # S3-event path: perm from object metadata
│   ├── upload/handler.py        # API /single-file: file + permission string
│   ├── bulk_ingest/handler.py   # API /bulk-ingest: CSV-driven permissions
│   ├── query/handler.py         # API /query: filtered retrieval + answer + audit
│   └── kb_sync/handler.py       # starts Bedrock ingestion jobs
└── agent/                       # optional AgentCore container runtime
```
