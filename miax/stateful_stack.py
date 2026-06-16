"""MIAX stateful stack.

Holds all *stateful* / data resources whose lifecycle and retained data should
be managed independently of compute:

* Input S3 bucket  - where raw documents are first uploaded (front end target).
* Source S3 bucket - the curated data source the Knowledge Base ingests from.
* S3 Vectors bucket + index - the vector store backing the Knowledge Base.
* Bedrock Knowledge Base - RAG index with ``permissions_group`` metadata
  filtering enabled.

The companion :class:`MiaxStatelessStack` consumes the bucket names and the
Knowledge Base id exposed as public attributes on this stack.
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    CfnOutput,
    aws_s3 as s3,
    aws_s3vectors as s3vectors,
    aws_iam as iam,
    aws_bedrock as bedrock,
)
from constructs import Construct

from miax.constants import (
    PERMISSION_METADATA_KEY,
    PROJECT_PREFIX,
)


class MiaxStatefulStack(Stack):
    """Storage + vector store + knowledge base."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        embedding_model_arn: str,
        embedding_dimension: int,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = self.account
        region = self.region

        # ------------------------------------------------------------------
        # 1. Input bucket - the front end (built later) uploads documents
        #    here under a prefix that names the chosen permission group, e.g.
        #    ``permissions_group_a/quarterly-report.pdf``.
        # ------------------------------------------------------------------
        self.input_bucket = s3.Bucket(
            self,
            "InputBucket",
            bucket_name=f"{PROJECT_PREFIX}-input-{account}-{region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            # Emit events to EventBridge so the (cross-stack) ingest Lambda can
            # subscribe without mutating this bucket to reference the function
            # (which would create a stack dependency cycle).
            event_bridge_enabled=True,
            # CORS so the future drag/drop front end can PUT directly.

            cors=[
                s3.CorsRule(
                    allowed_methods=[
                        s3.HttpMethods.PUT,
                        s3.HttpMethods.POST,
                        s3.HttpMethods.GET,
                        s3.HttpMethods.HEAD,
                    ],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                    exposed_headers=["ETag"],
                    max_age=3000,
                )
            ],
            lifecycle_rules=[
                s3.LifecycleRule(
                    # Clean up un-processed uploads after a while.
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
        )

        # ------------------------------------------------------------------
        # 2. Source bucket - curated documents (+ sidecar metadata files) that
        #    the Knowledge Base ingests. Written to by the ingest Lambda.
        # ------------------------------------------------------------------
        self.source_bucket = s3.Bucket(
            self,
            "SourceBucket",
            bucket_name=f"{PROJECT_PREFIX}-source-{account}-{region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # 3. S3 Vectors store - a vector bucket containing a single index that
        #    the Knowledge Base writes embeddings into.
        #
        #    All metadata keys are filterable by default; we only exclude the
        #    Bedrock-managed chunk text/metadata blobs from filtering (they are
        #    large and never used as filters). ``permissions_group`` therefore
        #    remains a filterable field, which is what powers the per-request
        #    permission filtering performed by the agent.
        # ------------------------------------------------------------------
        self.vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"{PROJECT_PREFIX}-vectors-{account}-{region}",
            encryption_configuration=s3vectors.CfnVectorBucket.EncryptionConfigurationProperty(
                sse_type="AES256",
            ),
        )

        self.vector_index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            index_name=f"{PROJECT_PREFIX}-kb-index",
            vector_bucket_name=self.vector_bucket.vector_bucket_name,
            data_type="float32",
            dimension=embedding_dimension,
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                # Keys that should NOT be filterable. The Bedrock chunk text and
                # source metadata blobs are excluded; everything else (incl.
                # ``permissions_group``) stays filterable.
                non_filterable_metadata_keys=[
                    "AMAZON_BEDROCK_TEXT",
                    "AMAZON_BEDROCK_METADATA",
                ],
            ),
        )
        self.vector_index.add_dependency(self.vector_bucket)

        # ------------------------------------------------------------------
        # 4. Bedrock Knowledge Base service role.
        # ------------------------------------------------------------------
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            role_name=f"{PROJECT_PREFIX}-kb-role",
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

        # Permission to invoke the embedding model.
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[embedding_model_arn],
            )
        )

        # Permission to read the source bucket (data source).
        self.source_bucket.grant_read(kb_role)

        # Permission to operate the S3 Vectors store.
        kb_role.add_to_policy(
            iam.PolicyStatement(
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

        # ------------------------------------------------------------------
        # 5. Knowledge Base backed by S3 Vectors.
        # ------------------------------------------------------------------
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"{PROJECT_PREFIX}-knowledge-base",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_model_arn,
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
        # 6. S3 data source pointing at the curated source bucket. Sidecar
        #    ``.metadata.json`` files written by the ingest Lambda supply the
        #    ``permissions_group`` attribute for each document.
        # ------------------------------------------------------------------
        self.data_source = bedrock.CfnDataSource(
            self,
            "SourceDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name=f"{PROJECT_PREFIX}-source-data-source",
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
        CfnOutput(self, "InputBucketName", value=self.input_bucket.bucket_name)
        CfnOutput(self, "SourceBucketName", value=self.source_bucket.bucket_name)
        CfnOutput(self, "VectorBucketName", value=self.vector_bucket.vector_bucket_name)
        CfnOutput(self, "VectorIndexArn", value=self.vector_index.attr_index_arn)
        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=self.data_source_id)
        CfnOutput(
            self,
            "PermissionMetadataKey",
            value=PERMISSION_METADATA_KEY,
            description="Filterable metadata key used to gate retrieval by permission group.",
        )
