#!/usr/bin/env python3
"""MIAX - AgentCore RAG with metadata permission filtering.

Two stacks:
* ``MiaxStateful``  - data: KMS key, access-logs/input/source buckets, S3
  Vectors store, Bedrock Knowledge Base + data source.
* ``MiaxStateless`` - compute: ingest, bulk-ingest and KB-sync Lambdas plus the
  Bedrock AgentCore runtime.

Configuration is resolved from CDK context into a single ``AppConfig`` (see
``miax/config.py``). Select an environment with ``-c env=prod``. Everything
defaults to us-east-1.
"""

import os

import aws_cdk as cdk

from miax.config import AppConfig, DEFAULT_REGION
from miax.stateful_stack import MiaxStatefulStack
from miax.stateless_stack import MiaxStatelessStack

app = cdk.App()

account = os.environ.get("CDK_DEFAULT_ACCOUNT")
region = os.environ.get("CDK_DEFAULT_REGION", DEFAULT_REGION)
config = AppConfig.from_context(app, account=account, region=region)

# Fail fast on unsafe production configuration.
if config.is_prod and config.allowed_origins == ["*"]:
    raise ValueError(
        "Refusing to synthesize prod with wildcard CORS. "
        "Pass -c allowedOrigins=https://your-frontend.example.com"
    )

env = cdk.Environment(account=account, region=region)

stack_suffix = "" if config.env_name in ("dev", "") else f"-{config.env_name}"

# --- Stateful stack (data) ----------------------------------------------------
stateful = MiaxStatefulStack(
    app,
    f"MiaxStateful{stack_suffix}",
    config=config,
    env=env,
    termination_protection=config.is_prod,
    description="MIAX RAG stateful resources: KMS, buckets, S3 Vectors store, Bedrock Knowledge Base.",
)

# --- Stateless stack (compute) ------------------------------------------------
stateless = MiaxStatelessStack(
    app,
    f"MiaxStateless{stack_suffix}",
    config=config,
    input_bucket=stateful.input_bucket,
    source_bucket=stateful.source_bucket,
    data_key=stateful.data_key,
    knowledge_base_id=stateful.knowledge_base_id,
    knowledge_base_arn=stateful.knowledge_base_arn,
    data_source_id=stateful.data_source_id,
    query_log_table=stateful.query_log_table,
    env=env,

    description="MIAX RAG stateless resources: ingest/bulk/sync Lambdas + AgentCore runtime.",
)
stateless.add_dependency(stateful)

# --- Consistent tagging across every resource ---------------------------------
for key, value in config.tags.items():
    cdk.Tags.of(app).add(key, value)

# --- Optional security/best-practice checks (cdk-nag) -------------------------
if config.enable_cdk_nag:
    try:
        from cdk_nag import AwsSolutionsChecks

        cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    except ImportError:
        print("cdk-nag not installed; skipping. `pip install cdk-nag` to enable.")

app.synth()
