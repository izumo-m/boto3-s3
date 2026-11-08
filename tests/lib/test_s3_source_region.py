"""``source_region`` / `S3.source_client`, and ``mv(validate_same_s3_paths=...)``.

aws-cli's ``ClientFactory`` builds the source client of an S3-to-S3 run given
``--source-region`` in that region and without ``--endpoint-url``; its ``mv``
resolves both sides of a same-key S3 move through ``S3PathResolver`` under
``--validate-same-s3-paths`` and warns otherwise when a side may name an
access point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boto3_s3 import S3, LocalStorage, OpOutcome, OpResult, S3Storage, ValidationError, pathresolver


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    # No request leaves the host: no metadata probe, and any S3 request lands
    # on a port nothing listens on.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")


class TestSourceClient:
    @pytest.mark.parametrize("factory", ["S3", "from_aws_cli"])
    def test_built_in_the_region_without_the_explicit_endpoint(
        self, factory: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Building sends nothing; the environment's endpoint is cleared so the
        # client's own resolution shows.
        monkeypatch.delenv("AWS_ENDPOINT_URL")
        endpoint = "http://127.0.0.1:8"
        s3 = (
            S3(endpoint_url=endpoint)
            if factory == "S3"
            else S3.from_aws_cli(region="us-east-1", endpoint_url=endpoint)
        )
        assert s3.client().meta.endpoint_url == endpoint
        source = s3.source_client("eu-west-1")
        assert source.meta.region_name == "eu-west-1"
        assert source.meta.endpoint_url == "https://s3.eu-west-1.amazonaws.com"

    def test_an_endpoint_from_the_environment_still_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # botocore reads it for any client, aws-cli's source client included.
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9")
        assert S3.from_aws_cli().source_client("eu-west-1").meta.endpoint_url == (
            "http://127.0.0.1:9"
        )


class TestSourceRegion:
    def test_an_s3_to_s3_source_string_gets_the_source_client(self) -> None:
        s3 = S3.from_aws_cli(region="us-east-1")
        source = s3._resolve_source("s3://src/k", "s3://dst/k", "eu-west-1")  # pyright: ignore[reportPrivateUsage]
        assert isinstance(source, S3Storage)
        assert source.get_client().meta.region_name == "eu-west-1"
        assert source.normalized_uri() == "s3://src/k"

    @pytest.mark.parametrize(
        ("src", "dest", "region"),
        [
            ("s3://src/k", "local-dir", "eu-west-1"),
            ("s3://src/k", "s3://dst/k", ""),
            ("s3://src/k", "s3://dst/k", None),
        ],
        ids=["download", "empty", "absent"],
    )
    def test_any_other_run_ignores_it(self, src: str, dest: str, region: str | None) -> None:
        # aws-cli reads it for an s3s3 run, and only when it is non-empty.
        s3 = S3.from_aws_cli(region="us-east-1")
        source = s3._resolve_source(src, dest, region)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(source, S3Storage)
        assert source.get_client().meta.region_name == "us-east-1"

    def test_a_source_storage_keeps_its_own_client(self, tmp_path: Path) -> None:
        s3 = S3.from_aws_cli(region="us-east-1")
        own = S3Storage("s3://src/k", client=s3.client())
        assert s3._resolve_source(own, "s3://dst/k", "eu-west-1") is own  # pyright: ignore[reportPrivateUsage]
        local = LocalStorage(str(tmp_path))
        assert s3._resolve_source(local, "s3://dst/k", "eu-west-1") is local  # pyright: ignore[reportPrivateUsage]


_AP = "s3://arn:aws:s3:us-west-2:123456789012:accesspoint/myap/k"


class TestCheckMvPaths:
    """`S3.check_mv_paths`: mv's checks on their own, ahead of the run."""

    def test_the_same_object_is_refused_without_a_client(self) -> None:
        from boto3_s3.s3storage import held_client

        src, dest = S3Storage("s3://b/k"), S3Storage("s3://b/")
        with pytest.raises(
            ValidationError, match="Cannot mv a file onto itself: s3://b/k - s3://b/"
        ):
            S3().check_mv_paths(src, dest)
        assert held_client(src) is None and held_client(dest) is None
        with pytest.raises(ValidationError, match="onto itself"):
            S3().check_mv_paths("s3://b/k", "s3://b")

    def test_a_pair_that_is_not_s3_to_s3_passes(self, tmp_path: Path) -> None:
        S3().check_mv_paths(str(tmp_path), "s3://b/k")
        S3().check_mv_paths("s3://b/k", str(tmp_path))

    def test_mv_leaves_them_out_when_told(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # validate_same_s3_paths=None: the caller ran check_mv_paths itself.
        ran: list[bool] = []
        monkeypatch.setattr(S3, "_run_transfer", lambda *_a, **_k: ran.append(True))
        results: list[OpResult] = []
        S3().mv(_AP, "s3://mybucket/k", validate_same_s3_paths=None, on_result=results.append)
        assert results == [] and ran == [True]


class TestValidateSameS3Paths:
    @pytest.fixture
    def resolved(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Stand in for the resolver and its clients, recording the regions asked for."""
        seen: dict[str, Any] = {"regions": [], "map": {}}

        def service_client(_self: S3, service: str, *, region: str | None = None) -> Any:
            seen["regions"].append((service, region))
            return object()

        def resolve(_self: Any, path: str) -> list[str]:
            return [seen["map"].get(path, path)]

        def run(_self: S3, *_args: Any, **kwargs: Any) -> None:
            seen["ran"] = True

        seen["ran"] = False
        monkeypatch.setattr(S3, "service_client", service_client)
        monkeypatch.setattr(S3, "_run_transfer", run)
        monkeypatch.setattr(pathresolver.S3PathResolver, "__init__", lambda self, **_kw: None)
        monkeypatch.setattr(pathresolver.S3PathResolver, "resolve_underlying_s3_paths", resolve)
        return seen

    def test_a_move_onto_itself_through_an_access_point_is_refused(
        self, resolved: dict[str, Any]
    ) -> None:
        resolved["map"][_AP] = "s3://mybucket/k"
        s3 = S3.from_aws_cli(region="us-east-1")
        with pytest.raises(ValidationError, match="Cannot mv a file onto itself") as excinfo:
            s3.mv(_AP, "s3://mybucket/k", dryrun=True, validate_same_s3_paths=True)
        assert str(excinfo.value) == f"Cannot mv a file onto itself: {_AP} - s3://mybucket/k"

    def test_the_source_side_resolves_in_the_source_region(self, resolved: dict[str, Any]) -> None:
        results: list[OpResult] = []
        S3.from_aws_cli(region="us-east-1").mv(
            _AP,
            "s3://mybucket/k",
            dryrun=True,
            validate_same_s3_paths=True,
            source_region="us-west-2",
            on_result=results.append,
        )
        # aws-cli's from_session per side, in its order.
        assert resolved["regions"] == [
            ("s3control", "us-west-2"),
            ("sts", None),
            ("s3control", "us-east-1"),
            ("sts", None),
        ]
        assert results == []
        assert resolved["ran"]

    def test_without_it_an_access_point_side_gets_aws_clis_warning(
        self, resolved: dict[str, Any]
    ) -> None:
        results: list[OpResult] = []
        S3().mv(_AP, "s3://mybucket/k", dryrun=True, on_result=results.append)
        assert [r.outcome for r in results] == [OpOutcome.NOTICE]
        assert resolved["ran"]
        assert str(results[0].error).startswith(
            "warning: Provided s3 paths may resolve to same underlying s3 object(s)"
        )
        assert resolved["regions"] == []

    def test_different_keys_are_not_looked_at(self, resolved: dict[str, Any]) -> None:
        results: list[OpResult] = []
        S3().mv(_AP, "s3://mybucket/other", dryrun=True, on_result=results.append)
        assert results == []
        assert resolved["ran"]
        S3().mv(
            _AP,
            "s3://mybucket/other",
            dryrun=True,
            validate_same_s3_paths=True,
            on_result=results.append,
        )
        assert resolved["regions"] == []
