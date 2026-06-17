"""Unit tests for the Lambda handler helper logic (path safety, group parsing).

We exercise the pure helper functions without invoking AWS. Each handler module
reads required env vars at import time, so we set them before importing.
"""

import importlib
import os
import sys

import pytest

LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..", "lambda")


def _import_handler(subdir: str, module_alias: str, env: dict):
    os.environ.update(env)
    path = os.path.abspath(os.path.join(LAMBDA_ROOT, subdir))
    sys.path.insert(0, path)
    # Ensure a fresh import even if another handler.py was imported before.
    sys.modules.pop("handler", None)
    mod = importlib.import_module("handler")
    mod = importlib.reload(mod)
    sys.path.remove(path)
    renamed = sys.modules.pop("handler")
    sys.modules[module_alias] = renamed
    return renamed


@pytest.fixture()
def ingest():
    return _import_handler(
        "ingest",
        "ingest_handler",
        {
            "SOURCE_BUCKET": "src",
            "PERMISSION_GROUPS": "permissions_group_a,permissions_group_b,permissions_group_c",
        },
    )


@pytest.fixture()
def bulk():
    return _import_handler(
        "bulk_ingest",
        "bulk_handler",
        {
            "INPUT_BUCKET": "in",
            "SOURCE_BUCKET": "src",
            "PERMISSION_GROUPS": "permissions_group_a,permissions_group_b,permissions_group_c",
        },
    )


def test_ingest_rejects_unsafe_keys(ingest):
    assert ingest._is_safe_key("permissions_group_a/report.pdf") is True
    assert ingest._is_safe_key("../etc/passwd") is False
    assert ingest._is_safe_key("/abs/path") is False
    assert ingest._is_safe_key("a/../../b") is False


def test_ingest_group_resolution_reads_s3_metadata(ingest, monkeypatch):
    # Permission comes from S3 object metadata, not the key/path.
    def fake_head(Bucket, Key):  # noqa: N803 - boto3 kwarg casing
        store = {
            "any/path/report.pdf": {"Metadata": {"permissions_group": "permissions_group_b"}},
            "other.pdf": {"Metadata": {"permissions_group": "not_a_group"}},
            "missing.pdf": {"Metadata": {}},
        }
        return store[Key]

    monkeypatch.setattr(ingest.s3, "head_object", fake_head)

    assert ingest._resolve_permission_group("b", "any/path/report.pdf") == "permissions_group_b"
    assert ingest._resolve_permission_group("b", "other.pdf") is None
    assert ingest._resolve_permission_group("b", "missing.pdf") is None



def test_ingest_parses_eventbridge_shape(ingest):
    event = {
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": "miax-input"},
            "object": {"key": "permissions_group_a/report.pdf"},
        },
    }
    assert ingest._extract_records(event) == [("miax-input", "permissions_group_a/report.pdf")]


def test_bulk_filename_sanitization(bulk):
    assert bulk._sanitize_filename("report.pdf") == "report.pdf"
    assert bulk._sanitize_filename("sub/dir/report.pdf") == "sub/dir/report.pdf"
    assert bulk._sanitize_filename("../escape.pdf") is None
    assert bulk._sanitize_filename("/abs.pdf") is None
    assert bulk._sanitize_filename("a/../../b.pdf") is None
    assert bulk._sanitize_filename("doc.metadata.json") is None
    assert bulk._sanitize_filename("") is None
