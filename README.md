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
   │ PUT permissions_group_x/... │ PUT bulk/<batch>/<file> + manifest │
   │                             │ then call Bulk-Ingest Function URL │
   └──────────────┬──────────────┴──────────────────┬────────────────┘
                  │                                  │
                  ▼                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                       INPUT BUCKET (miax-input-*)              │
   │   permissions_group_x/<file>         bulk/<batch>/<file>       │
   └───────┬──────────────────────────────────────┬────────────────┘
           │ s3:ObjectCreated (EventBridge)        │ invoke (Function URL, IAM)
           ▼                                       ▼
   ┌──────────────────────┐            ┌──────────────────────────────┐
   │   INGEST LAMBDA       │            │   BULK-INGEST LAMBDA          │
   │   • group from prefix │            │   • parse manifest.csv        │
   │   • copy → source     │            │   • per row: copy → source    │
   │   • write sidecar     │            │   • write sidecar per file    │
   │   • delete from input │            │   • delete staged + manifest  │
   └──────────┬───────────┘            └──────────────┬───────────────┘
              │                                        │
              └───────────────┬────────────────────────┘
                              ▼
   ┌──────────────────────┐         ┌──────────────────────────────┐
   │   SOURCE BUCKET       │ ◄───────│ <file> + <file>.metadata.json │
   │   miax-source-*       │         │ {"metadataAttributes":{       │
   │   (KB data source)    │         │   "permissions_group":"...a"}}│
   └──────────┬───────────┘         └──────────────────────────────┘
              │ ingestion job (embeddings written to S3 Vectors)
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
   │              BEDROCK AGENTCORE RUNTIME                        │
   │   request: { prompt, permission_group }                       │
   │   applies metadata filter → returns answer + citations        │
   │   model: claude-sonnet-4-6                                    │
   └─────────────────────────────────────────────────────────────┘
```

### Stacks

| Stack | Type | Resources |
|-------|------|-----------|
| **MiaxStateful** | Data | Input bucket, Source bucket, S3 Vectors bucket + index, Bedrock Knowledge Base + S3 data source |
| **MiaxStateless** | Compute | Ingest Lambda + S3 (EventBridge) trigger, Bulk-Ingest Lambda + Function URL, Bedrock AgentCore Runtime (optional) |


`MiaxStateless` depends on `MiaxStateful` and consumes its bucket names and the
Knowledge Base id/arn. Splitting stateful from stateless lets you redeploy /
tear down compute without touching retained data (buckets use
`RemovalPolicy.RETAIN`).

---

## How permission filtering works

1. The front end (built later) uploads a file under a prefix naming the chosen
   group, e.g. `permissions_group_b/report.pdf`, into the **input bucket**.
2. The **ingest Lambda** reads that prefix, copies the file into the **source
   bucket**, and writes a Bedrock sidecar `report.pdf.metadata.json`:
   ```json
   { "metadataAttributes": { "permissions_group": "permissions_group_b" } }
   ```
3. Bedrock ingests the file and stamps every chunk with `permissions_group`.
   The S3 Vectors index keeps that key **filterable**.
4. The **AgentCore agent** receives `{ "prompt": "...", "permission_group":
   "permissions_group_b" }` and adds a retrieval filter
   `equals(permissions_group, permissions_group_b)` so only that group's content
   is ever returned.

---

## Bulk upload (CSV manifest)

For uploading many files at once, the front end stages files under a batch
prefix and submits a manifest CSV mapping each filename to its permission group.

**Manifest format** (`filename,permission_group`, one row per file):

```csv
filename,permission_group
q3-report.pdf,permissions_group_a
roadmap.docx,permissions_group_b
hr-policy.pdf,permissions_group_c
```

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
   `<group>/<filename>`, writes the `.metadata.json` sidecar, and cleans up the
   staged files + manifest. It returns `{ processed: [...], errors: [...] }`.

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
# 1. Upload a doc tagged for group B
aws s3 cp ./report.pdf \
  s3://miax-input-<ACCOUNT>-us-east-1/permissions_group_b/report.pdf

# 2. Sync the Knowledge Base data source (or wait for scheduled sync)
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <KB_ID> --data-source-id <DS_ID> --region us-east-1

# 3. Invoke the agent (when deployed with -c deployAgent=true)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn <AGENT_RUNTIME_ARN> \
  --payload '{"prompt":"Summarize the report","permission_group":"permissions_group_b"}' \
  --region us-east-1 /dev/stdout
```

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
files. It will issue presigned PUTs to the input bucket under the
`permissions_group_*` prefix — the rest of the pipeline already handles tagging
and filtering automatically.
