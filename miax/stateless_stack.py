"""MIAX stateless stack.

Holds all *stateless* / compute resources. These can be torn down and
re-deployed without losing data:

* Ingest Lambda - single-file path, triggered by ``Object Created`` events on
  the input bucket (via EventBridge). Moves a document into the source bucket
  and stamps the ``permissions_group`` metadata sidecar.
* Bulk-ingest Lambda - CSV-manifest path, invoked by the front end via an
  IAM-authenticated Function URL.
* KB-sync Lambda - starts a Bedrock ingestion job (debounced; skips if one is
  already running) so newly curated documents are embedded automatically.
* Bedrock AgentCore Runtime - hosts the RAG agent that queries the Knowledge
  Base with a per-request metadata filter.

Cross-cutting hardening: dead-letter queues, CloudWatch log retention, X-Ray
tracing, least-privilege IAM, and KMS usage are applied to every function.

Consumes references (buckets, key, KB id/arn) from :class:`MiaxStatefulStack`.
The AgentCore runtime is gated behind ``deploy_agent`` because it requires a
Docker image build. Enable with ``-c deployAgent=true``.
"""

import os

from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_s3 as s3,
    aws_lambda as lambda_,

    aws_iam as iam,
    aws_logs as logs,
    aws_sqs as sqs,
    aws_dynamodb as dynamodb,
    aws_ecr_assets as ecr_assets,
    aws_events as events,
    aws_events_targets as targets,
    aws_bedrockagentcore as agentcore,
)

from constructs import Construct

from miax.config import AppConfig
from miax.constants import (
    BULK_STAGING_PREFIX,
    PERMISSION_GROUPS,
    PERMISSION_METADATA_KEY,
    PROJECT_PREFIX,
)

_LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")


class MiaxStatelessStack(Stack):
    """Ingest + bulk-ingest + KB-sync Lambdas and the AgentCore runtime."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        input_bucket: s3.IBucket,
        source_bucket: s3.IBucket,
        knowledge_base_id: str,

        knowledge_base_arn: str,
        data_source_id: str,
        query_log_table: dynamodb.ITable,
        **kwargs,
    ) -> None:

        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        account = self.account
        region = self.region

        log_retention = _resolve_log_retention(config.log_retention_days)
        tracing = (
            lambda_.Tracing.ACTIVE if config.enable_tracing else lambda_.Tracing.DISABLED
        )

        common_env = {
            "PERMISSION_METADATA_KEY": PERMISSION_METADATA_KEY,
            "PERMISSION_GROUPS": ",".join(PERMISSION_GROUPS),
            "BULK_STAGING_PREFIX": BULK_STAGING_PREFIX,
            "POWERTOOLS_SERVICE_NAME": f"{PROJECT_PREFIX}-{config.env_name}",
            "LOG_LEVEL": "INFO",
        }

        # ------------------------------------------------------------------
        # 0. KB-sync Lambda - starts an ingestion job (idempotent / debounced).
        #    Invoked asynchronously by the ingest Lambdas after they write to
        #    the source bucket, so the KB stays current without manual syncs.
        # ------------------------------------------------------------------
        kb_sync_dlq = self._dlq("KbSyncDlq")
        kb_sync_fn = lambda_.Function(
            self,
            "KbSyncFunction",
            function_name=f"{PROJECT_PREFIX}-{config.env_name}-kb-sync",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_LAMBDA_DIR, "kb_sync")),
            timeout=Duration.minutes(2),
            memory_size=256,
            tracing=tracing,
            log_retention=log_retention,
            dead_letter_queue=kb_sync_dlq,
            reserved_concurrent_executions=1,  # serialise ingestion-job starts
            environment={
                **common_env,
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "DATA_SOURCE_ID": data_source_id,
            },
        )
        kb_sync_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ManageIngestionJobs",
                actions=[
                    "bedrock:StartIngestionJob",
                    "bedrock:ListIngestionJobs",
                    "bedrock:GetIngestionJob",
                ],
                resources=[knowledge_base_arn],
            )
        )

        # ------------------------------------------------------------------
        # 1. Single-file ingest Lambda.
        # ------------------------------------------------------------------
        ingest_dlq = self._dlq("IngestDlq")
        ingest_fn = lambda_.Function(
            self,
            "IngestFunction",
            function_name=f"{PROJECT_PREFIX}-{config.env_name}-ingest",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_LAMBDA_DIR, "ingest")),
            timeout=Duration.minutes(5),
            memory_size=256,
            tracing=tracing,
            log_retention=log_retention,
            dead_letter_queue=ingest_dlq,
            environment={
                **common_env,
                "SOURCE_BUCKET": source_bucket.bucket_name,
                "KB_SYNC_FUNCTION_NAME": kb_sync_fn.function_name,
            },
        )
        input_bucket.grant_read_write(ingest_fn)
        source_bucket.grant_read_write(ingest_fn)
        kb_sync_fn.grant_invoke(ingest_fn)


        # EventBridge rule (avoids a cross-stack notification dependency cycle).
        events.Rule(
            self,
            "IngestRule",
            rule_name=f"{PROJECT_PREFIX}-{config.env_name}-ingest-rule",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [input_bucket.bucket_name]}},
            ),
            targets=[
                targets.LambdaFunction(
                    ingest_fn,
                    dead_letter_queue=self._queue("IngestRuleDlq"),
                    retry_attempts=2,
                )
            ],
        )

        # ------------------------------------------------------------------
        # 2. Bulk-ingest Lambda + IAM-authenticated Function URL.
        # ------------------------------------------------------------------
        bulk_dlq = self._dlq("BulkIngestDlq")
        bulk_ingest_fn = lambda_.Function(
            self,
            "BulkIngestFunction",
            function_name=f"{PROJECT_PREFIX}-{config.env_name}-bulk-ingest",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_LAMBDA_DIR, "bulk_ingest")),
            timeout=Duration.minutes(15),
            memory_size=512,
            tracing=tracing,
            log_retention=log_retention,
            dead_letter_queue=bulk_dlq,
            environment={
                **common_env,
                "INPUT_BUCKET": input_bucket.bucket_name,
                "SOURCE_BUCKET": source_bucket.bucket_name,
                "KB_SYNC_FUNCTION_NAME": kb_sync_fn.function_name,
            },
        )
        input_bucket.grant_read_write(bulk_ingest_fn)
        source_bucket.grant_read_write(bulk_ingest_fn)
        kb_sync_fn.grant_invoke(bulk_ingest_fn)


        bulk_ingest_url = bulk_ingest_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=config.allowed_origins,
                allowed_methods=[lambda_.HttpMethod.POST],
                allowed_headers=["content-type", "authorization"],
                max_age=Duration.seconds(3000),
            ),
        )

        # ------------------------------------------------------------------
        # 3. Query Lambda + IAM-authenticated Function URL - THE AGENT ENDPOINT.
        #    This is the HTTPS endpoint a UI calls. It retrieves permission-
        #    filtered chunks from the Knowledge Base, generates a grounded answer
        #    with the model, writes one audit row per query to DynamoDB, and
        #    returns the answer + citations + token usage + latency.
        # ------------------------------------------------------------------
        model_profile_arn = config.agent_model_inference_profile_arn
        foundation_model_arn = (
            f"arn:aws:bedrock:{region}::foundation-model/{config.agent_model_id}"
        )

        query_dlq = self._dlq("QueryDlq")
        query_fn = lambda_.Function(
            self,
            "QueryFunction",
            function_name=f"{PROJECT_PREFIX}-{config.env_name}-query",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_LAMBDA_DIR, "query")),
            timeout=Duration.minutes(2),
            memory_size=512,
            tracing=tracing,
            log_retention=log_retention,
            dead_letter_queue=query_dlq,
            environment={
                **common_env,
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "MODEL_ARN": model_profile_arn,
                "QUERY_LOG_TABLE": query_log_table.table_name,
                "NUMBER_OF_RESULTS": "8",
            },
        )
        # Retrieve from the KB (with the metadata filter).
        query_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="RetrieveFromKnowledgeBase",
                actions=["bedrock:Retrieve"],
                resources=[knowledge_base_arn],
            )
        )
        # Generate answers via the model inference profile.
        query_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeGenerationModel",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    model_profile_arn,
                    foundation_model_arn,
                    f"arn:aws:bedrock:*::foundation-model/{config.agent_model_id}",
                ],
            )
        )
        # Write audit rows to the query-log table.
        query_log_table.grant_write_data(query_fn)


        query_url = query_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=config.allowed_origins,
                allowed_methods=[lambda_.HttpMethod.POST],
                allowed_headers=["content-type", "authorization"],
                max_age=Duration.seconds(3000),
            ),
        )

        # ------------------------------------------------------------------
        # 4. AgentCore Runtime (optional - requires a container image build).
        # ------------------------------------------------------------------
        if config.deploy_agent:
            agent_image = ecr_assets.DockerImageAsset(

                self,
                "AgentImage",
                directory=os.path.join(os.path.dirname(__file__), "..", "agent"),
                platform=ecr_assets.Platform.LINUX_ARM64,
            )

            agent_role = iam.Role(
                self,
                "AgentRuntimeRole",
                role_name=f"{PROJECT_PREFIX}-{config.env_name}-agent-runtime-role",
                assumed_by=iam.ServicePrincipal(
                    "bedrock-agentcore.amazonaws.com",
                    conditions={
                        "StringEquals": {"aws:SourceAccount": account},
                        "ArnLike": {
                            "aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"
                        },
                    },
                ),
            )
            agent_image.repository.grant_pull(agent_role)

            # Invoke the generation model via its inference profile. Both the
            # profile ARN and the underlying foundation-model ARN are required.
            agent_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeGenerationModel",
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=[
                        model_profile_arn,
                        foundation_model_arn,
                        f"arn:aws:bedrock:*::foundation-model/{config.agent_model_id}",
                    ],
                )
            )
            # Retrieve from the Knowledge Base (with the metadata filter).
            agent_role.add_to_policy(
                iam.PolicyStatement(
                    sid="RetrieveFromKnowledgeBase",
                    actions=["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                    resources=[knowledge_base_arn],
                )
            )
            # Emit observability traces/metrics.

            agent_role.add_to_policy(
                iam.PolicyStatement(
                    sid="Observability",
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                        "cloudwatch:PutMetricData",
                    ],
                    resources=["*"],
                )
            )

            # The runtime uses the default AWS IAM (SigV4) auth - callers invoke
            # it with signed requests. No JWT/OIDC authorizer is configured
            # (kept intentionally simple).
            self.agent_runtime = agentcore.CfnRuntime(
                self,
                "AgentRuntime",
                agent_runtime_name=f"{PROJECT_PREFIX}_{config.env_name}_rag_agent",
                role_arn=agent_role.role_arn,
                network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                    network_mode="PUBLIC",
                ),
                agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                        container_uri=agent_image.image_uri,
                    ),
                ),
                environment_variables={
                    "KNOWLEDGE_BASE_ID": knowledge_base_id,
                    "MODEL_ARN": model_profile_arn,
                    "PERMISSION_METADATA_KEY": PERMISSION_METADATA_KEY,
                    "PERMISSION_GROUPS": ",".join(PERMISSION_GROUPS),
                    "LOG_LEVEL": "INFO",
                },
            )
            # The deployed inbound endpoint ARN callers invoke (SigV4-signed
            # InvokeAgentRuntime). This is the agent's "endpoint".
            CfnOutput(
                self,
                "AgentRuntimeArn",
                value=self.agent_runtime.attr_agent_runtime_arn,
                description="AgentCore runtime ARN - the endpoint to call via bedrock-agentcore InvokeAgentRuntime.",
            )


        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "IngestFunctionName", value=ingest_fn.function_name)
        CfnOutput(self, "BulkIngestFunctionName", value=bulk_ingest_fn.function_name)
        CfnOutput(self, "KbSyncFunctionName", value=kb_sync_fn.function_name)
        CfnOutput(self, "QueryFunctionName", value=query_fn.function_name)
        CfnOutput(
            self,
            "QueryFunctionUrl",
            value=query_url.url,
            description="THE AGENT ENDPOINT - IAM-authenticated HTTPS URL a UI POSTs {username, permission_group, prompt} to. Returns answer + citations.",
        )
        CfnOutput(
            self,
            "BulkIngestFunctionUrl",
            value=bulk_ingest_url.url,
            description="IAM-authenticated Function URL the front end calls to run a CSV bulk upload.",
        )


    # ----------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------
    def _queue(self, construct_id: str) -> sqs.Queue:
        """An SSE-SQS queue with a sensible retention window."""
        return sqs.Queue(
            self,
            construct_id,
            queue_name=f"{PROJECT_PREFIX}-{self.config.env_name}-{construct_id.lower()}",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )

    def _dlq(self, construct_id: str) -> sqs.Queue:
        """A dead-letter queue for a Lambda's async failures."""
        return self._queue(construct_id)



def _resolve_log_retention(days: int) -> logs.RetentionDays:
    """Map an integer day count to the nearest supported RetentionDays value."""
    mapping = {
        1: logs.RetentionDays.ONE_DAY,
        3: logs.RetentionDays.THREE_DAYS,
        5: logs.RetentionDays.FIVE_DAYS,
        7: logs.RetentionDays.ONE_WEEK,
        14: logs.RetentionDays.TWO_WEEKS,
        30: logs.RetentionDays.ONE_MONTH,
        60: logs.RetentionDays.TWO_MONTHS,
        90: logs.RetentionDays.THREE_MONTHS,
        180: logs.RetentionDays.SIX_MONTHS,
        365: logs.RetentionDays.ONE_YEAR,
        731: logs.RetentionDays.TWO_YEARS,
    }
    if days in mapping:
        return mapping[days]
    # Pick the smallest supported retention >= requested days.
    for threshold in sorted(mapping):
        if threshold >= days:
            return mapping[threshold]
    return logs.RetentionDays.ONE_YEAR
