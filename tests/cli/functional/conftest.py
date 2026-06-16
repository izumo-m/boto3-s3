"""Fixtures for the functional (moto-backed) CLI suite."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

if TYPE_CHECKING:
    from collections.abc import Iterator

# Fixed bucket the golden-replay tests resolve scenario argv against. The
# goldens store ``<BUCKET>`` tokens, so the concrete name never leaks into
# them.
FUNCTIONAL_BUCKET = "golden-replay-bucket"


@pytest.fixture
def moto_s3() -> Iterator[Any]:
    """A moto-backed S3 client with ``FUNCTIONAL_BUCKET`` already created.

    The CLI under test builds its own client via ``globals.build_client``;
    ``mock_aws`` patches botocore process-wide, so that client (and the
    prefetch worker thread inside ``Storage.scan``) hits the same in-memory
    backend as this seeding client. Credentials/region come from the root
    conftest's ``_moto_isolation`` fixture (us-east-1 avoids the
    ``LocationConstraint`` quirk).
    """
    with mock_aws():
        # Fresh Session: the process-global default session caches resolved
        # credentials, which would leak this suite's fake ones into the e2e
        # suite's real client within the same pytest run.
        client = boto3.session.Session().client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=FUNCTIONAL_BUCKET)
        yield client
