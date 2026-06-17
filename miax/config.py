"""Centralised, environment-aware configuration for the MIAX project.

Enterprise deployments need per-environment (dev/stage/prod) parameterisation,
a consistent tagging strategy for cost allocation and ownership, and a single
place to tune security/ops knobs. ``AppConfig`` is resolved once in ``app.py``
from CDK context (``-c env=prod``) and passed to every stack.

Override any value at synth/deploy time, e.g.::

    cdk deploy --all -c env=prod -c allowedOrigins=https://miax.example.com
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import aws_cdk as cdk


# Default AWS region for the project.
DEFAULT_REGION = "us-east-1"

# Embedding model + dimension defaults (Amazon Titan Text Embeddings v2).
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
DEFAULT_EMBEDDING_DIMENSION = 1024

# Generation model. Claude Sonnet 4.x in us-east-1 is served through a
# cross-region *inference profile* (``us.<model_id>``), not a bare
# foundation-model ARN - see ``agent_model_inference_profile_arn``.
DEFAULT_AGENT_MODEL_ID = "anthropic.claude-sonnet-4-6"


@dataclass
class AppConfig:
    """Resolved configuration shared across all stacks."""

    env_name: str = "dev"
    account: str | None = None
    region: str = DEFAULT_REGION

    # Models
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION
    agent_model_id: str = DEFAULT_AGENT_MODEL_ID

    # Security / networking
    # Front end origin(s) permitted for S3 CORS and the Function URL. Defaults
    # to "*" only for non-prod; prod must set explicit origins.
    allowed_origins: List[str] = field(default_factory=lambda: ["*"])

    # Ops

    log_retention_days: int = 90
    enable_tracing: bool = True
    # Apply cdk-nag (AwsSolutions) checks at synth time.
    enable_cdk_nag: bool = False

    # Feature flags
    deploy_agent: bool = False

    @property
    def is_prod(self) -> bool:
        return self.env_name.lower() in ("prod", "production")

    @property
    def embedding_model_arn(self) -> str:
        """Foundation-model ARN for the embedding model (region-scoped)."""
        return (
            f"arn:aws:bedrock:{self.region}::foundation-model/{self.embedding_model_id}"
        )

    @property
    def agent_model_inference_profile_arn(self) -> str:
        """Cross-region inference-profile ARN used to invoke the agent model.

        Claude Sonnet 4.x is only invokable in us-east-1 via the ``us.`` system
        inference profile, which requires both the profile ARN and the
        underlying foundation-model ARNs on the IAM policy.
        """
        if self.account is None:
            # Use a token-friendly placeholder; the real account is substituted
            # at deploy time by the consuming stack via ``Stack.account``.
            account = cdk.Aws.ACCOUNT_ID
        else:
            account = self.account
        profile_id = f"us.{self.agent_model_id}"
        return (
            f"arn:aws:bedrock:{self.region}:{account}:inference-profile/{profile_id}"
        )

    @property
    def tags(self) -> dict:
        """Standard cost-allocation / ownership tags applied to every stack."""
        return {
            "Project": "miax",
            "Application": "agentcore-rag",
            "Environment": self.env_name,
            "ManagedBy": "cdk",
            "DataClassification": "confidential",
        }

    @classmethod
    def from_context(cls, app: cdk.App, account: str | None, region: str) -> "AppConfig":
        """Build configuration from CDK context with sensible defaults."""

        def ctx(key: str, default=None):
            val = app.node.try_get_context(key)
            return val if val is not None else default

        env_name = str(ctx("env", "dev"))

        allowed_origins_raw = ctx("allowedOrigins")
        if allowed_origins_raw:
            allowed_origins = [o.strip() for o in str(allowed_origins_raw).split(",") if o.strip()]
        else:
            allowed_origins = ["*"]

        return cls(
            env_name=env_name,
            account=account,
            region=region or DEFAULT_REGION,
            embedding_model_id=str(ctx("embeddingModelId", DEFAULT_EMBEDDING_MODEL_ID)),
            embedding_dimension=int(ctx("embeddingDimension", DEFAULT_EMBEDDING_DIMENSION)),
            agent_model_id=str(ctx("agentModelId", DEFAULT_AGENT_MODEL_ID)),
            allowed_origins=allowed_origins,
            log_retention_days=int(ctx("logRetentionDays", 90)),

            enable_tracing=str(ctx("enableTracing", "true")).lower() == "true",
            enable_cdk_nag=str(ctx("cdkNag", "false")).lower() == "true",
            deploy_agent=str(ctx("deployAgent", "false")).lower() == "true",
        )
