"""MIAX stateful stack.

Holds all *stateful* / data resources whose lifecycle and retained data should
be managed independently of compute:

* Customer-managed KMS key - encrypts buckets, vector store and logs.
* Access-logs bucket - S3 server access logs for audit.
* Input S3 bucket  - where raw documents are first uploaded (front end target).
* Source S3 bucket - the curated data source the Knowledge Base ingests from.
* S3 Vectors bucket + index - the vector store backing the Knowledge Base.
* Bedrock Knowledge Base + S3 data source - RAG index with ``permissions_group``
  metadata filtering enabled.

The companion :class:`MiaxStatelessStack` consumes the bucket/key references and
the Knowledge Base id/arn exposed as public attributes on this stack.
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    CfnOutput,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
    aws_iam as iam,
    aws_kms as kms,
    aws_bedrock as bedrock,
    aws_dynamodb as dynamodb,
)

from constructs import Construct

from miax.config import AppConfig
from miax.constants import PERMISSION_METADATA_KEY, PROJECT_PREFIX


class MiaxStatefulStack(Stack):
    """Storage + vector store + knowledge base."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        account = self.account
        region = self.region

        # Retain data in prod; allow clean teardown in ephemeral envs.
        removal_policy = (
            RemovalPolicy.RETAIN if config.is_prod else RemovalPolicy.DESTROY
        )
        auto_delete = not config.is_prod

        # ------------------------------------------------------------------
        # 0. Customer-managed KMS key for encryption at rest (rotation on).
        # ------------------------------------------------------------------
        self.data_key = kms.Key(
            self,
            "DataKey",
            alias=f"alias/{PROJECT_PREFIX}-{config.env_name}-data",
            description="MIAX RAG data encryption key (buckets, vectors, logs).",
            enable_key_rotation=True,
            removal_policy=removal_policy,
        )
        # Allow Bedrock and S3 service principals to use the key for KB ingestion
        # and bucket operations, scoped to this account.
        self.data_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowBedrockUseOfKey",
                principals=[iam.ServicePrincipal("bedrock.amazonaws.com")],
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:DescribeKey",
                ],
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceAccount": account}},
            )
        )

        # ------------------------------------------------------------------
        # 1. Access-logs bucket (S3 server access logging target).
        # ------------------------------------------------------------------
        self.access_logs_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            bucket_name=f"{PROJECT_PREFIX}-access-logs-{account}-{region}",
            encryption=s3.BucketEncryption.S3_MANAGED,  # log delivery cannot use SSE-KMS
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            removal_policy=removal_policy,
            auto_delete_objects=auto_delete,
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(365)),
            ],
        )

        # ------------------------------------------------------------------
        # 2. Input bucket - the front end uploads documents here, either under a
        #    permission-group prefix (single file) or the bulk staging prefix.
        # ------------------------------------------------------------------
        self.input_bucket = s3.Bucket(
            self,
            "InputBucket",
            bucket_name=f"{PROJECT_PREFIX}-input-{account}-{region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.data_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=removal_policy,
            auto_delete_objects=auto_delete,
            # Emit events to EventBridge so the (cross-stack) ingest Lambda can
            # subscribe without mutating this bucket to reference the function
            # (which would create a stack dependency cycle).
            event_bridge_enabled=True,
            server_access_logs_bucket=self.access_logs_bucket,
            server_access_logs_prefix="input-bucket/",
            cors=[
                s3.CorsRule(
                    allowed_methods=[
                        s3.HttpMethods.PUT,
                        s3.HttpMethods.POST,
                        s3.HttpMethods.GET,
                        s3.HttpMethods.HEAD,
                    ],
                    allowed_origins=config.allowed_origins,
                    allowed_headers=["*"],
                    exposed_headers=["ETag"],
                    max_age=3000,
                )
            ],
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
                s3.LifecycleRule(
                    # Reap any orphaned staged uploads that were never processed.
                    prefix="bulk/",
                    expiration=Duration.days(7),
                ),
                s3.LifecycleRule(
                    noncurrent_version_expiration=Duration.days(30),
                ),
            ],
        )

        # ------------------------------------------------------------------
        # 3. Source bucket - curated documents (+ sidecar metadata files) that
        #    the Knowledge Base ingests. Written to by the ingest Lambdas.
        # ------------------------------------------------------------------
        self.source_bucket = s3.Bucket(
            self,
            "SourceBucket",
            bucket_name=f"{PROJECT_PREFIX}-source-{account}-{region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.data_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=removal_policy,
            auto_delete_objects=auto_delete,
            server_access_logs_bucket=self.access_logs_bucket,
            server_access_logs_prefix="source-bucket/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    noncurrent_version_expiration=Duration.days(90),
                ),
            ],
        )

        # ------------------------------------------------------------------
        # 3b. DynamoDB audit table - one row per agent query. Records who asked
        #     what, the answer, permission group, latency, token usage and the
        #     citations. Partitioned by username with a time-sorted range key so
        #     a UI can page a user's history; a GSI lets you query across users
        #     by day. Encrypted with the customer-managed key; PITR enabled.
        # ------------------------------------------------------------------
        self.query_log_table = dynamodb.Table(
            self,
            "QueryLogTable",
            table_name=f"{PROJECT_PREFIX}-{config.env_name}-query-log",
            partition_key=dynamodb.Attribute(
                name="username", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.data_key,
            point_in_time_recovery=True,
            removal_policy=removal_policy,
            # Optional automatic expiry of old audit rows (set 'ttl' epoch attr).
            time_to_live_attribute="ttl",
        )
        # Query all activity for a permission group, newest first.
        self.query_log_table.add_global_secondary_index(
            index_name="by-permission-group",
            partition_key=dynamodb.Attribute(
                name="permission_group", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp", type=dynamodb.AttributeType.STRING
            ),
        )

        # ------------------------------------------------------------------
        # 4. S3 Vectors store - a vector bucket containing a single index that

        #    the Knowledge Base writes embeddings into. ``permissions_group``
        #    remains filterable (only the Bedrock chunk/text blobs are excluded)
        #    which is what powers per-request permission filtering.
        # ------------------------------------------------------------------
        self.vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"{PROJECT_PREFIX}-vectors-{account}-{region}",
            encryption_configuration=s3vectors.CfnVectorBucket.EncryptionConfigurationProperty(
                sse_type="aws:kms",
                kms_key_arn=self.data_key.key_arn,
            ),
        )

        self.vector_index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            index_name=f"{PROJECT_PREFIX}-kb-index",
            vector_bucket_name=self.vector_bucket.vector_bucket_name,
            data_type="float32",
            dimension=config.embedding_dimension,
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=[
                    "AMAZON_BEDROCK_TEXT",
                    "AMAZON_BEDROCK_METADATA",
                ],
            ),
        )
        self.vector_index.add_dependency(self.vector_bucket)

        # ------------------------------------------------------------------
        # 5. Bedrock Knowledge Base service role (least privilege).
        # ------------------------------------------------------------------
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            role_name=f"{PROJECT_PREFIX}-{config.env_name}-kb-role",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock:{region}:{account}:knowledge-base/*"
                    },
                },
            ),
        )

        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeEmbeddingModel",
                actions=["bedrock:InvokeModel"],
                resources=[config.embedding_model_arn],
            )
        )

        # Read the source bucket (data source) and decrypt with the data key.
        self.source_bucket.grant_read(kb_role)
        self.data_key.grant_decrypt(kb_role)

        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="OperateVectorStore",
                actions=[
                    "s3vectors:GetIndex",
                    "s3vectors:QueryVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:ListVectors",
                    "s3vectors:DeleteVectors",
                ],
                resources=[
                    self.vector_bucket.attr_vector_bucket_arn,
                    self.vector_index.attr_index_arn,
                ],
            )
        )
        # The vector store is KMS-encrypted; allow the KB role to use the key.
        self.data_key.grant_encrypt_decrypt(kb_role)

        # ------------------------------------------------------------------
        # 6. Knowledge Base backed by S3 Vectors.
        # ------------------------------------------------------------------
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"{PROJECT_PREFIX}-{config.env_name}-knowledge-base",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=config.embedding_model_arn,
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=self.vector_index.attr_index_arn,
                ),
            ),
        )
        self.knowledge_base.node.add_dependency(self.vector_index)
        self.knowledge_base.node.add_dependency(kb_role)

        # ------------------------------------------------------------------
        # 7. S3 data source pointing at the curated source bucket. Sidecar
        #    ``.metadata.json`` files supply the ``permissions_group`` attribute.
        # ------------------------------------------------------------------
        self.data_source = bedrock.CfnDataSource(
            self,
            "SourceDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name=f"{PROJECT_PREFIX}-source-data-source",
            # Keep vectors in sync when a source object is deleted.
            data_deletion_policy="DELETE",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=self.source_bucket.bucket_arn,
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="FIXED_SIZE",
                    fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                        max_tokens=512,
                        overlap_percentage=20,
                    ),
                ),
            ),
        )
        self.data_source.add_dependency(self.knowledge_base)

        # Expose identifiers for the stateless stack / operators.
        self.knowledge_base_id = self.knowledge_base.attr_knowledge_base_id
        self.knowledge_base_arn = self.knowledge_base.attr_knowledge_base_arn
        self.data_source_id = self.data_source.attr_data_source_id

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "DataKeyArn", value=self.data_key.key_arn)
        CfnOutput(self, "InputBucketName", value=self.input_bucket.bucket_name)
        CfnOutput(self, "SourceBucketName", value=self.source_bucket.bucket_name)
        CfnOutput(self, "VectorBucketName", value=self.vector_bucket.vector_bucket_name)
        CfnOutput(self, "VectorIndexArn", value=self.vector_index.attr_index_arn)
        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=self.data_source_id)
        CfnOutput(self, "QueryLogTableName", value=self.query_log_table.table_name)

        CfnOutput(
            self,
            "PermissionMetadataKey",
            value=PERMISSION_METADATA_KEY,
            description="Filterable metadata key used to gate retrieval by permission group.",
        )
