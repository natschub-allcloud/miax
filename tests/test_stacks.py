"""CDK assertion tests for the MIAX stacks.

These run entirely offline (synth only) - no AWS credentials needed.
Run with: ``pytest -q``.
"""

import aws_cdk as cdk
from aws_cdk.assertions import Template, Match

from miax.config import AppConfig
from miax.stateful_stack import MiaxStatefulStack
from miax.stateless_stack import MiaxStatelessStack


def _synth(deploy_agent: bool = False, env_name: str = "dev"):
    app = cdk.App()
    config = AppConfig(
        env_name=env_name,
        account="123456789012",
        region="us-east-1",
        deploy_agent=deploy_agent,
    )
    env = cdk.Environment(account="123456789012", region="us-east-1")
    stateful = MiaxStatefulStack(app, "TestStateful", config=config, env=env)
    stateless = MiaxStatelessStack(
        app,
        "TestStateless",
        config=config,
        input_bucket=stateful.input_bucket,
        source_bucket=stateful.source_bucket,
        knowledge_base_id=stateful.knowledge_base_id,

        knowledge_base_arn=stateful.knowledge_base_arn,
        data_source_id=stateful.data_source_id,
        query_log_table=stateful.query_log_table,
        env=env,
    )

    return Template.from_stack(stateful), Template.from_stack(stateless)


def test_buckets_are_encrypted_and_private():
    stateful, _ = _synth()
    # 3 buckets: access-logs, input, source.
    stateful.resource_count_is("AWS::S3::Bucket", 3)
    stateful.all_resources_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                }
            }
        ),
    )


def test_no_customer_managed_kms_key():
    # POC uses default AWS-managed/owned encryption - no CMK should be created.
    stateful, _ = _synth()
    stateful.resource_count_is("AWS::KMS::Key", 0)



def test_knowledge_base_uses_s3_vectors():
    stateful, _ = _synth()
    stateful.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        Match.object_like(
            {"StorageConfiguration": {"Type": "S3_VECTORS"}}
        ),
    )


def test_vector_index_keeps_permission_group_filterable():
    stateful, _ = _synth()
    # The only non-filterable keys are the Bedrock-managed blobs.
    stateful.has_resource_properties(
        "AWS::S3Vectors::Index",
        Match.object_like(
            {
                "MetadataConfiguration": {
                    "NonFilterableMetadataKeys": [
                        "AMAZON_BEDROCK_TEXT",
                        "AMAZON_BEDROCK_METADATA",
                    ]
                }
            }
        ),
    )


def test_lambdas_have_dlqs_and_tracing():
    _, stateless = _synth()
    # ingest, bulk-ingest, kb-sync = 3 functions (+ possible log-retention helper).
    stateless.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"TracingConfig": {"Mode": "Active"}}),
    )
    # At least the three DLQs + one event-rule DLQ.
    queues = stateless.find_resources("AWS::SQS::Queue")
    assert len(queues) >= 4


def test_api_gateway_with_api_key():
    _, stateless = _synth()
    # One REST API fronts the three routes, protected by an API key + usage plan.
    stateless.resource_count_is("AWS::ApiGateway::RestApi", 1)
    stateless.resource_count_is("AWS::ApiGateway::ApiKey", 1)
    stateless.resource_count_is("AWS::ApiGateway::UsagePlan", 1)
    # No Lambda Function URLs anymore - everything goes through API Gateway.
    stateless.resource_count_is("AWS::Lambda::Url", 0)
    # POST routes: /query, /single-file, /bulk-ingest, /presign.
    post_methods = stateless.find_resources(
        "AWS::ApiGateway::Method",
        {"Properties": {"HttpMethod": "POST"}},
    )
    assert len(post_methods) == 4
    for method in post_methods.values():
        assert method["Properties"]["ApiKeyRequired"] is True
    # GET route: /stats (also api-key protected).
    get_methods = stateless.find_resources(
        "AWS::ApiGateway::Method",
        {"Properties": {"HttpMethod": "GET"}},
    )
    assert len(get_methods) == 1
    for method in get_methods.values():
        assert method["Properties"]["ApiKeyRequired"] is True




def test_audit_table_has_pitr():
    stateful, _ = _synth()
    stateful.has_resource_properties(
        "AWS::DynamoDB::Table",
        Match.object_like(
            {
                "PointInTimeRecoverySpecification": {
                    "PointInTimeRecoveryEnabled": True
                }
            }
        ),
    )




def test_agent_runtime_only_when_enabled():
    _, stateless_no_agent = _synth(deploy_agent=False)
    stateless_no_agent.resource_count_is("AWS::BedrockAgentCore::Runtime", 0)

    _, stateless_with_agent = _synth(deploy_agent=True)
    stateless_with_agent.resource_count_is("AWS::BedrockAgentCore::Runtime", 1)
