"""MIAX stateless stack.

Holds all *stateless* / compute resources. These can be torn down and
re-deployed without losing data:

* Ingest Lambda + S3 ``ObjectCreated`` trigger on the input bucket. Moves
  documents into the source bucket and stamps the ``permissions_group``
  metadata sidecar consumed by the Knowledge Base.
* Bedrock AgentCore Runtime hosting the RAG agent. The agent queries the
  Knowledge Base with a per-request metadata filter so callers only retrieve
  content for their permission group.

Consumes references (buckets, KB id/arn) from :class:`MiaxStatefulStack`.

The AgentCore runtime is gated behind the ``deployAgent`` context flag because
it requires a Docker image build (``cdk synth``/``deploy`` of just the pipeline
should not force a container build). Enable with ``-c deployAgent=true``.
"""

import os

from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_ecr_assets as ecr_assets,
    aws_events as events,
    aws_events_targets as targets,
    aws_bedrockagentcore as agentcore,
)

from constructs import Construct

from miax.constants import (
    BULK_STAGING_PREFIX,
    PERMISSION_GROUPS,
    PERMISSION_METADATA_KEY,
    PROJECT_PREFIX,
)



class MiaxStatelessStack(Stack):
    """Ingest Lambda + AgentCore runtime."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        input_bucket: s3.IBucket,
        source_bucket: s3.IBucket,
        knowledge_base_id: str,
        knowledge_base_arn: str,
        agent_model_id: str,
        deploy_agent: bool,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = self.account
        region = self.region

        # ------------------------------------------------------------------
        # 1. Ingest Lambda - triggered by uploads to the input bucket.
        # ------------------------------------------------------------------
        ingest_fn = lambda_.Function(
            self,
            "IngestFunction",
            function_name=f"{PROJECT_PREFIX}-ingest",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda", "ingest")
            ),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "SOURCE_BUCKET": source_bucket.bucket_name,
                "PERMISSION_METADATA_KEY": PERMISSION_METADATA_KEY,
                "PERMISSION_GROUPS": ",".join(PERMISSION_GROUPS),
            },
        )

        # Read from input, write to source.
        input_bucket.grant_read_write(ingest_fn)
        source_bucket.grant_read_write(ingest_fn)

        # Trigger the Lambda for every new object in the input bucket. We use an
        # EventBridge rule (rather than a direct S3 bucket notification) so the
        # stateless stack can subscribe to the stateful stack's bucket without
        # mutating that bucket to reference this function - which would create a
        # cross-stack dependency cycle. The input bucket has
        # ``event_bridge_enabled=True``.
        events.Rule(
            self,
            "IngestRule",
            rule_name=f"{PROJECT_PREFIX}-ingest-rule",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [input_bucket.bucket_name]}},
            ),
            targets=[targets.LambdaFunction(ingest_fn)],
        )

        # ------------------------------------------------------------------
        # 2. Bulk-ingest Lambda - invoked by the front end after a CSV bulk
        #    upload. The UI stages files under ``bulk/<batch_id>/<filename>`` in
        #    the input bucket plus a manifest CSV (``filename,permission_group``
        #    per row), then calls this function. It copies each staged file into
        #    the source bucket under its permission-group prefix and writes the
        #    Bedrock metadata sidecar - the same mechanism as single-file ingest.
        #
        #    A Lambda Function URL (IAM-auth) is exposed so the future front end
        #    can invoke it directly. Swap for API Gateway if richer routing/auth
        #    is needed later.
        # ------------------------------------------------------------------
        bulk_ingest_fn = lambda_.Function(
            self,
            "BulkIngestFunction",
            function_name=f"{PROJECT_PREFIX}-bulk-ingest",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda", "bulk_ingest")
            ),
            timeout=Duration.minutes(15),
            memory_size=512,
            environment={
                "INPUT_BUCKET": input_bucket.bucket_name,
                "SOURCE_BUCKET": source_bucket.bucket_name,
                "BULK_STAGING_PREFIX": BULK_STAGING_PREFIX,
                "PERMISSION_METADATA_KEY": PERMISSION_METADATA_KEY,
                "PERMISSION_GROUPS": ",".join(PERMISSION_GROUPS),
            },
        )

        input_bucket.grant_read_write(bulk_ingest_fn)
        source_bucket.grant_read_write(bulk_ingest_fn)

        # Function URL (IAM-authenticated) for the front end to call later.
        bulk_ingest_url = bulk_ingest_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.POST],
                allowed_headers=["*"],
            ),
        )

        # ------------------------------------------------------------------
        # 3. AgentCore Runtime (optional - requires a container image build).
        # ------------------------------------------------------------------

        if deploy_agent:
            model_arn = (
                f"arn:aws:bedrock:{region}::foundation-model/{agent_model_id}"
            )

            # Build & push the ARM64 agent container image.
            agent_image = ecr_assets.DockerImageAsset(
                self,
                "AgentImage",
                directory=os.path.join(os.path.dirname(__file__), "..", "agent"),
                platform=ecr_assets.Platform.LINUX_ARM64,
            )

            # Execution role assumed by the AgentCore runtime.
            agent_role = iam.Role(
                self,
                "AgentRuntimeRole",
                role_name=f"{PROJECT_PREFIX}-agent-runtime-role",
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

            # Pull the agent image from ECR.
            agent_image.repository.grant_pull(agent_role)

            # Invoke the generation model.
            agent_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=[
                        model_arn,
                        f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                    ],
                )
            )

            # Query the Knowledge Base (with metadata filter).
            agent_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock:Retrieve",
                        "bedrock:RetrieveAndGenerate",
                    ],
                    resources=[knowledge_base_arn],
                )
            )

            self.agent_runtime = agentcore.CfnRuntime(
                self,
                "AgentRuntime",
                agent_runtime_name=f"{PROJECT_PREFIX}_rag_agent",
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
                    "MODEL_ARN": model_arn,
                    "PERMISSION_METADATA_KEY": PERMISSION_METADATA_KEY,
                    "PERMISSION_GROUPS": ",".join(PERMISSION_GROUPS),
                },
            )

            CfnOutput(
                self,
                "AgentRuntimeArn",
                value=self.agent_runtime.attr_agent_runtime_arn,
            )

        CfnOutput(self, "IngestFunctionName", value=ingest_fn.function_name)
        CfnOutput(self, "BulkIngestFunctionName", value=bulk_ingest_fn.function_name)
        CfnOutput(
            self,
            "BulkIngestFunctionUrl",
            value=bulk_ingest_url.url,
            description="IAM-authenticated Function URL the front end calls to run a CSV bulk upload.",
        )

