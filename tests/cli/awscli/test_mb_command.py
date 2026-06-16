"""Port of aws-cli's functional mb tests to ``boto3-s3 mb``.

Provenance: aws-cli's ``tests/functional/s3/test_mb_command.py``
(aws-cli 2.34.x snapshot). Test names, argv, canned responses, and
expected params/rc are kept verbatim where possible so the file stays
diffable against the aws-cli original when aws-cli is updated.

Adaptation rules (on top of the rm port's - see
``tests/cli/awscli/test_rm_command.py``):

- The aws-cli harness routes ``--region`` through parsed_globals into the
  client; here the recording client is built inside the client factory
  honoring ``args.region``, so the aws-cli argv stays verbatim and
  ``client.meta.region_name`` drives the LocationConstraint logic.
- ``test_tags_with_three_arguments_fails`` expects ``ParamValidation`` in
  stderr; with argparse the stray third token lands in ``parse_known_args``
  extras -> ``Unknown options: ExtraArg``. The rc (252) is the contract,
  the message is not (docs/cli.md section 6).
"""

from __future__ import annotations

from typing import Any

from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client


def _run_cmd(
    parsed_responses: list[dict[str, Any] | Exception], argv: list[str]
) -> tuple[CliResult, list[ApiCall]]:
    """The port's ``self.run_cmd``: the client is built per ``args.region``."""
    state: dict[str, list[ApiCall]] = {}

    def factory(args: Any) -> Any:
        client, calls = make_recording_client(
            parsed_responses, region=getattr(args, "region", None) or "us-east-1"
        )
        state["calls"] = calls
        return client

    result = run_cli_in_process(argv, ctx=Context(client_factory=factory))
    return result, state.get("calls", [])


def _assert_params(calls: list[ApiCall], expected_params: dict[str, Any]) -> None:
    """The port's ``assert_params_for_cmd`` (single CreateBucket expected)."""
    assert [(c.operation, c.params) for c in calls] == [("CreateBucket", expected_params)]


class TestMBCommand:
    def test_make_bucket(self) -> None:
        result, calls = _run_cmd([{}], ["mb", "s3://bucket"])
        assert result.rc == 0
        assert len(calls) == 1, calls
        assert calls[0].operation == "CreateBucket"

    def test_adds_location_constraint(self) -> None:
        result, calls = _run_cmd(
            [{"Location": "us-west-2"}], ["mb", "s3://bucket", "--region", "us-west-2"]
        )
        assert result.rc == 0
        _assert_params(
            calls,
            {
                "Bucket": "bucket",
                "CreateBucketConfiguration": {"LocationConstraint": "us-west-2"},
            },
        )

    def test_location_constraint_not_added_on_us_east_1(self) -> None:
        result, calls = _run_cmd([{}], ["mb", "s3://bucket", "--region", "us-east-1"])
        assert result.rc == 0
        _assert_params(calls, {"Bucket": "bucket"})

    def test_nonzero_exit_if_invalid_path_provided(self) -> None:
        result, calls = _run_cmd([{}], ["mb", "bucket"])
        assert result.rc == 252
        assert calls == []

    def test_incompatible_with_express_directory_bucket(self) -> None:
        result, calls = _run_cmd([{}], ["mb", "s3://bucket--usw2-az1--x-s3/"])
        assert result.rc == 252
        assert "Cannot use mb command with a directory bucket." in result.stderr
        assert calls == []

    def test_make_bucket_with_single_tag(self) -> None:
        result, calls = _run_cmd(
            [{}], ["mb", "s3://bucket", "--tags", "Key1", "Value1", "--region", "us-west-2"]
        )
        assert result.rc == 0
        _assert_params(
            calls,
            {
                "Bucket": "bucket",
                "CreateBucketConfiguration": {
                    "LocationConstraint": "us-west-2",
                    "Tags": [{"Key": "Key1", "Value": "Value1"}],
                },
            },
        )

    def test_make_bucket_with_single_tag_us_east_1(self) -> None:
        result, calls = _run_cmd(
            [{}], ["mb", "s3://bucket", "--tags", "Key1", "Value1", "--region", "us-east-1"]
        )
        assert result.rc == 0
        _assert_params(
            calls,
            {
                "Bucket": "bucket",
                "CreateBucketConfiguration": {"Tags": [{"Key": "Key1", "Value": "Value1"}]},
            },
        )

    def test_make_bucket_with_multiple_tags(self) -> None:
        result, calls = _run_cmd(
            [{}],
            [
                "mb",
                "s3://bucket",
                "--tags",
                "Key1",
                "Value1",
                "--tags",
                "Key2",
                "Value2",
                "--region",
                "us-west-2",
            ],
        )
        assert result.rc == 0
        _assert_params(
            calls,
            {
                "Bucket": "bucket",
                "CreateBucketConfiguration": {
                    "LocationConstraint": "us-west-2",
                    "Tags": [
                        {"Key": "Key1", "Value": "Value1"},
                        {"Key": "Key2", "Value": "Value2"},
                    ],
                },
            },
        )

    def test_account_regional_namespace_bucket(self) -> None:
        bucket = "amzn-s3-demo-bucket-111122223333-us-west-2-an"
        result, calls = _run_cmd(
            [{"Location": "us-west-2"}], ["mb", f"s3://{bucket}", "--region", "us-west-2"]
        )
        assert result.rc == 0
        _assert_params(
            calls,
            {
                "Bucket": bucket,
                "BucketNamespace": "account-regional",
                "CreateBucketConfiguration": {"LocationConstraint": "us-west-2"},
            },
        )

    def test_account_regional_namespace_bucket_us_east_1(self) -> None:
        bucket = "my-bucket-111122223333-us-east-1-an"
        result, calls = _run_cmd([{}], ["mb", f"s3://{bucket}", "--region", "us-east-1"])
        assert result.rc == 0
        _assert_params(calls, {"Bucket": bucket, "BucketNamespace": "account-regional"})

    def test_account_regional_namespace_short_bucket_name(self) -> None:
        bucket = "xyz-an"
        result, calls = _run_cmd([{}], ["mb", f"s3://{bucket}", "--region", "us-east-1"])
        assert result.rc == 0
        _assert_params(calls, {"Bucket": bucket, "BucketNamespace": "account-regional"})

    def test_regular_bucket_no_namespace(self) -> None:
        result, calls = _run_cmd([{}], ["mb", "s3://my-regular-bucket", "--region", "us-east-1"])
        assert result.rc == 0
        _assert_params(calls, {"Bucket": "my-regular-bucket"})

    def test_tags_with_three_arguments_fails(self) -> None:
        # aws-cli asserts 'ParamValidation' in stderr; our parser surfaces the
        # stray token as "Unknown options" (module docstring). rc matches.
        result, calls = _run_cmd(
            [{}], ["mb", "s3://bucket", "--tags", "Key1", "Value1", "ExtraArg"]
        )
        assert result.rc == 252
        assert "Unknown options: ExtraArg" in result.stderr
        assert calls == []
