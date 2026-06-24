"""MIAX stateless stack.

Holds all *stateless* / compute resources. These can be torn down and
re-deployed without losing data:

* KB-sync Lambda - starts a Bedrock ingestion job (debounced) so newly curated
  documents are embedded automatically.
* Ingest Lambda - S3 event path: triggered by ``Object Created`` on the input
  bucket (via EventBridge). Reads ``permissions_group`` from the object's S3
  metadata, copies it into the source bucket, and writes the sidecar.
* Single-file upload Lambda - API path: receives {filename, permission_group,
  content_base64} and writes the file + metadata sidecar to the source bucket.
* Bulk-ingest Lambda - API path: parses a manifest CSV (filename + permissions
  per row) for files staged under ``bulk/<batch_id>/`` and ingests each.
* Query Lambda - API path: the RAG agent. Retrieves permission-filtered chunks,
  generates a grounded answer, logs to DynamoDB.

All three *API* paths are fronted by a single **API Gateway REST API** protected
by an **API key + usage plan** (header ``x-api-key``). No Cognito/IAM signing is
required - the POC assumes any caller with the key is authorised.

Consumes references (buckets, KB id/arn, audit table) from
:class:`MiaxStatefulStack`. The AgentCore container runtime is gated behind
``deploy_agent`` because it requires a Docker image build.
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
    aws_apigateway as apigw,
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
    """Ingest/upload/bulk/query Lambdas, API Gateway, and the AgentCore runtime."""

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

        model_profile_arn = config.agent_model_inference_profile_arn
        foundation_model_arn = (
            f"arn:aws:bedrock:{region}::foundation-model/{config.agent_model_id}"
        )

        # ------------------------------------------------------------------
        # 0. KB-sync Lambda - starts an ingestion job (idempotent / debounced).
        # ------------------------------------------------------------------
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
            dead_letter_queue=self._dlq("KbSyncDlq"),
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
        # 1. Single-file ingest Lambda - S3 EventBridge path (object metadata).
        # ------------------------------------------------------------------
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
            dead_letter_queue=self._dlq("IngestDlq"),
            environment={
                **common_env,
                "SOURCE_BUCKET": source_bucket.bucket_name,
                "KB_SYNC_FUNCTION_NAME": kb_sync_fn.function_name,
            },
        )
        input_bucket.grant_read_write(ingest_fn)
        source_bucket.grant_read_write(ingest_fn)
        kb_sync_fn.grant_invoke(ingest_fn)

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
        # 2. Single-file UPLOAD Lambda - API path (file + permission string).
        # ------------------------------------------------------------------
        upload_fn = lambda_.Function(
            self,
            "UploadFunction",
            function_name=f"{PROJECT_PREFIX}-{config.env_name}-single-file",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_LAMBDA_DIR, "upload")),
            timeout=Duration.minutes(2),
            memory_size=512,
            tracing=tracing,
            log_retention=log_retention,
            dead_letter_queue=self._dlq("UploadDlq"),
            environment={
                **common_env,
                "SOURCE_BUCKET": source_bucket.bucket_name,
                "KB_SYNC_FUNCTION_NAME": kb_sync_fn.function_name,
            },
        )
        source_bucket.grant_read_write(upload_fn)
        kb_sync_fn.grant_invoke(upload_fn)

        # ------------------------------------------------------------------
        # 3. Bulk-ingest Lambda - API path (CSV manifest drives permissions).
        # ------------------------------------------------------------------
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
            dead_letter_queue=self._dlq("BulkIngestDlq"),
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

        # ------------------------------------------------------------------
        # 3b. Presign Lambda - generates presigned S3 PUT URLs for bulk staging.
        # ------------------------------------------------------------------
        presign_fn = lambda_.Function(
            self,
            "PresignFunction",
            function_name=f"{PROJECT_PREFIX}-{config.env_name}-presign",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_LAMBDA_DIR, "presign")),
            timeout=Duration.seconds(10),
            memory_size=128,
            tracing=tracing,
            log_retention=log_retention,
            environment={
                **common_env,
                "INPUT_BUCKET": input_bucket.bucket_name,
            },
        )
        input_bucket.grant_put(presign_fn)

        # ------------------------------------------------------------------
        # 3c. Stats Lambda - counts curated docs in the source bucket so the UI
        #     "N indexed" badge reflects what's actually in the knowledge base.
        # ------------------------------------------------------------------
        stats_fn = lambda_.Function(
            self,
            "StatsFunction",
            function_name=f"{PROJECT_PREFIX}-{config.env_name}-stats",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_LAMBDA_DIR, "stats")),
            timeout=Duration.seconds(30),
            memory_size=128,
            tracing=tracing,
            log_retention=log_retention,
            environment={
                **common_env,
                "SOURCE_BUCKET": source_bucket.bucket_name,
            },
        )
        source_bucket.grant_read(stats_fn)

        # ------------------------------------------------------------------
        # 4. Query Lambda - API path (the RAG agent).

        # ------------------------------------------------------------------
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
            dead_letter_queue=self._dlq("QueryDlq"),
            environment={
                **common_env,
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "MODEL_ARN": model_profile_arn,
                "QUERY_LOG_TABLE": query_log_table.table_name,
                "NUMBER_OF_RESULTS": "8",
                # Relevance floor: drop weakly-related chunks from context +
                # citations so off-topic documents aren't cited.
                "MIN_SCORE": "0.4",
            },
        )
        query_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="RetrieveFromKnowledgeBase",
                actions=["bedrock:Retrieve"],
                resources=[knowledge_base_arn],
            )
        )
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
        query_log_table.grant_write_data(query_fn)

        # ------------------------------------------------------------------
        # 5. API Gateway (REST) - API-key protected, fronting the 3 API paths.
        #      POST /query        -> query_fn        {username, permission_group, prompt}
        #      POST /single-file  -> upload_fn       {filename, permission_group, content_base64}
        #      POST /bulk-ingest  -> bulk_ingest_fn  {batch_id, manifest_csv?}
        # ------------------------------------------------------------------
        api = apigw.RestApi(
            self,
            "Api",
            rest_api_name=f"{PROJECT_PREFIX}-{config.env_name}-api",
            description="MIAX RAG API: query the agent + ingest documents (API-key auth).",
            deploy_options=apigw.StageOptions(
                stage_name=config.env_name,
                throttling_rate_limit=50,
                throttling_burst_limit=20,
                tracing_enabled=config.enable_tracing,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=config.allowed_origins,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type", "x-api-key"],
            ),
        )

        def _add_route(path: str, fn: lambda_.IFunction) -> None:
            resource = api.root.add_resource(path)
            resource.add_method(
                "POST",
                apigw.LambdaIntegration(fn, proxy=True),
                api_key_required=True,
            )

        _add_route("query", query_fn)
        _add_route("single-file", upload_fn)
        _add_route("bulk-ingest", bulk_ingest_fn)
        _add_route("presign", presign_fn)

        # GET /stats -> stats_fn  (the "N indexed" badge)
        stats_resource = api.root.add_resource("stats")
        stats_resource.add_method(
            "GET",
            apigw.LambdaIntegration(stats_fn, proxy=True),
            api_key_required=True,
        )


        # API key + usage plan - callers must send header `x-api-key: <key>`.
        api_key = api.add_api_key(
            "ApiKey",
            api_key_name=f"{PROJECT_PREFIX}-{config.env_name}-key",
        )
        usage_plan = api.add_usage_plan(
            "UsagePlan",
            name=f"{PROJECT_PREFIX}-{config.env_name}-usage-plan",
            throttle=apigw.ThrottleSettings(rate_limit=50, burst_limit=20),
        )
        usage_plan.add_api_key(api_key)
        usage_plan.add_api_stage(stage=api.deployment_stage)

        # ------------------------------------------------------------------
        # 6. AgentCore Runtime (optional - requires a container image build).
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
            agent_role.add_to_policy(
                iam.PolicyStatement(
                    sid="RetrieveFromKnowledgeBase",
                    actions=["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                    resources=[knowledge_base_arn],
                )
            )
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
            CfnOutput(
                self,
                "AgentRuntimeArn",
                value=self.agent_runtime.attr_agent_runtime_arn,
                description="AgentCore runtime ARN (alternative invocation path via bedrock-agentcore).",
            )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(
            self,
            "ApiBaseUrl",
            value=api.url,
            description="Base URL of the REST API. Routes: POST {url}query, {url}single-file, {url}bulk-ingest. Send header x-api-key.",
        )
        CfnOutput(
            self,
            "ApiKeyId",
            value=api_key.key_id,
            description="API key id. Get the secret value: aws apigateway get-api-key --api-key <id> --include-value",
        )
        CfnOutput(self, "InputBucketName", value=input_bucket.bucket_name)
        CfnOutput(self, "QueryLogTableName", value=query_log_table.table_name)

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
    for threshold in sorted(mapping):
        if threshold >= days:
            return mapping[threshold]
    return logs.RetentionDays.ONE_YEAR
