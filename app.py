#!/usr/bin/env python3
"""MIAX - AgentCore RAG with metadata permission filtering.

Two stacks:
* ``MiaxStateful``  - storage (input/source buckets), S3 Vectors store, and the
  Bedrock Knowledge Base.
* ``MiaxStateless`` - ingest Lambda (+ S3 trigger) and the Bedrock AgentCore
  runtime.

All resources deploy to us-east-1 by default.
"""

import os

import aws_cdk as cdk

from miax.constants import DEFAULT_REGION
from miax.stateful_stack import MiaxStatefulStack
from miax.stateless_stack import MiaxStatelessStack

app = cdk.App()

# --- Configuration (overridable via cdk context) -----------------------------
embedding_model_arn = app.node.try_get_context("embeddingModelArn") or (
    "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
)
embedding_dimension = int(app.node.try_get_context("embeddingDimension") or 1024)
agent_model_id = (
    app.node.try_get_context("agentModelId") or "anthropic.claude-sonnet-4-6"
)
deploy_agent = str(app.node.try_get_context("deployAgent")).lower() == "true"

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", DEFAULT_REGION),
)

# --- Stateful stack (data) ----------------------------------------------------
stateful = MiaxStatefulStack(
    app,
    "MiaxStateful",
    embedding_model_arn=embedding_model_arn,
    embedding_dimension=embedding_dimension,
    env=env,
    description="MIAX RAG stateful resources: buckets, S3 Vectors store, Bedrock Knowledge Base.",
)

# --- Stateless stack (compute) ------------------------------------------------
stateless = MiaxStatelessStack(
    app,
    "MiaxStateless",
    input_bucket=stateful.input_bucket,
    source_bucket=stateful.source_bucket,
    knowledge_base_id=stateful.knowledge_base_id,
    knowledge_base_arn=stateful.knowledge_base_arn,
    agent_model_id=agent_model_id,
    deploy_agent=deploy_agent,
    env=env,
    description="MIAX RAG stateless resources: ingest Lambda + AgentCore runtime.",
)
stateless.add_dependency(stateful)

app.synth()
