"""Unit tests for boto3_s3.exceptions covering the exception-hierarchy spec."""

import asyncio
import concurrent.futures

import pytest

import boto3_s3
import boto3_s3.exceptions as ex

CATEGORIES: list[type[ex.Boto3S3Error]] = [
    ex.AccessDeniedError,
    ex.NotFoundError,
    ex.ValidationError,
    ex.TransportError,
    ex.ConfigurationError,
    ex.CancelledError,
]

# Direct ``Boto3S3Error`` subclasses that are not categories: aggregate errors.
AGGREGATES: list[type[ex.Boto3S3Error]] = [
    ex.BatchError,
]

# Refining subclasses: a category narrowed for the CLI's exit-code mapping
# (aws's general handler cases), still caught as their parent category.
REFINEMENTS: dict[type[ex.Boto3S3Error], type[ex.Boto3S3Error]] = {
    ex.InvalidValueError: ex.ValidationError,
    ex.InvalidConfigError: ex.ConfigurationError,
}

CATEGORY_NAMES = [
    "Boto3S3Error",
    "AccessDeniedError",
    "NotFoundError",
    "ValidationError",
    "InvalidValueError",
    "TransportError",
    "ConfigurationError",
    "InvalidConfigError",
    "CancelledError",
]


class TestRootExceptionClass:
    def test_boto3_s3_error_inherits_from_exception(self) -> None:
        assert issubclass(ex.Boto3S3Error, Exception)

    def test_every_category_is_caught_by_boto3_s3_error(self) -> None:
        for cls in CATEGORIES:
            with pytest.raises(ex.Boto3S3Error):
                raise cls("test")

    def test_standard_exceptions_are_not_boto3_s3_errors(self) -> None:
        assert not issubclass(TypeError, ex.Boto3S3Error)
        assert not issubclass(AssertionError, ex.Boto3S3Error)
        assert not issubclass(ValueError, ex.Boto3S3Error)


class TestCategorySubclasses:
    @pytest.mark.parametrize("category", CATEGORIES)
    def test_category_inherits_directly_from_boto3_s3_error(
        self, category: type[ex.Boto3S3Error]
    ) -> None:
        assert category.__bases__ == (ex.Boto3S3Error,)

    def test_categories_are_disjoint(self) -> None:
        for raised in CATEGORIES:
            for other in CATEGORIES:
                if raised is other:
                    continue
                with pytest.raises(raised) as exc_info:
                    raise raised("test")
                assert not isinstance(exc_info.value, other)

    def test_cancelled_error_distinct_from_asyncio_cancelled(self) -> None:
        assert not issubclass(ex.CancelledError, asyncio.CancelledError)
        assert not issubclass(ex.CancelledError, concurrent.futures.CancelledError)
        assert issubclass(ex.CancelledError, Exception)
        assert issubclass(ex.CancelledError, ex.Boto3S3Error)

    def test_direct_subclasses_form_closed_set(self) -> None:
        actual = set(ex.Boto3S3Error.__subclasses__())
        expected = set(CATEGORIES) | set(AGGREGATES)
        assert actual == expected


class TestRefinementSubclasses:
    @pytest.mark.parametrize(("refinement", "parent"), REFINEMENTS.items())
    def test_refinement_inherits_directly_from_its_category(
        self, refinement: type[ex.Boto3S3Error], parent: type[ex.Boto3S3Error]
    ) -> None:
        assert refinement.__bases__ == (parent,)

    @pytest.mark.parametrize(("refinement", "parent"), REFINEMENTS.items())
    def test_refinement_is_caught_as_its_category(
        self, refinement: type[ex.Boto3S3Error], parent: type[ex.Boto3S3Error]
    ) -> None:
        with pytest.raises(parent):
            raise refinement("test")

    def test_categories_have_no_other_subclasses(self) -> None:
        # The refinement set is closed: a new category subclass must be added
        # here (and to the CLI's exit-code mapping decision) deliberately.
        actual = {sub for category in CATEGORIES for sub in category.__subclasses__()}
        assert actual == set(REFINEMENTS)


class TestBatchError:
    def test_is_boto3_s3_error_not_a_category(self) -> None:
        assert issubclass(ex.BatchError, ex.Boto3S3Error)
        assert ex.BatchError not in CATEGORIES

    def test_rollup_counts_and_total(self) -> None:
        err = ex.BatchError(
            "2 of 10 failed", succeeded=3, failed=2, warned=1, skipped=4, operation="sync"
        )
        assert (err.succeeded, err.failed, err.warned, err.skipped) == (3, 2, 1, 4)
        assert err.total == 10
        assert err.operation == "sync"

    def test_counts_are_keyword_only(self) -> None:
        with pytest.raises(TypeError):
            ex.BatchError("msg", 1, 2, 3, 4)  # pyright: ignore[reportCallIssue]

    def test_caught_by_boto3_s3_error(self) -> None:
        with pytest.raises(ex.Boto3S3Error):
            raise ex.BatchError("x", succeeded=0, failed=1, warned=0, skipped=0)

    def test_re_exported(self) -> None:
        assert boto3_s3.BatchError is ex.BatchError
        assert "BatchError" in boto3_s3.__all__
        assert "BatchError" in ex.__all__


class TestStructuredContextFields:
    def test_construct_with_full_context(self) -> None:
        exc = ex.NotFoundError("object not found", operation="cp", bucket="b", key="k")
        assert exc.operation == "cp"
        assert exc.bucket == "b"
        assert exc.key == "k"
        assert "object not found" in str(exc)

    def test_construct_with_message_only_sets_fields_to_none(self) -> None:
        exc = ex.ConfigurationError("profile not found")
        assert exc.operation is None
        assert exc.bucket is None
        assert exc.key is None

    def test_context_fields_are_keyword_only(self) -> None:
        with pytest.raises(TypeError):
            ex.NotFoundError("msg", "cp")  # pyright: ignore[reportCallIssue]


class TestBackendExceptionChaining:
    def test_cause_exposes_backend_exception(self) -> None:
        class BackendError(Exception):
            pass

        original = BackendError("backend detail")
        try:
            try:
                raise original
            except BackendError as caught:
                raise ex.NotFoundError("missing", bucket="b", key="k") from caught
        except ex.NotFoundError as wrapped:
            assert wrapped.__cause__ is original

    def test_direct_raise_leaves_cause_unset(self) -> None:
        exc = ex.ConfigurationError("invalid profile name")
        assert exc.__cause__ is None


class TestPublicReExport:
    @pytest.mark.parametrize("name", CATEGORY_NAMES)
    def test_top_level_attribute_exists(self, name: str) -> None:
        assert hasattr(boto3_s3, name)

    @pytest.mark.parametrize("name", CATEGORY_NAMES)
    def test_top_level_identity_matches_module(self, name: str) -> None:
        assert getattr(boto3_s3, name) is getattr(ex, name)

    def test_all_enumerates_the_taxonomy(self) -> None:
        assert set(CATEGORY_NAMES) <= set(boto3_s3.__all__)
        assert set(CATEGORY_NAMES) <= set(ex.__all__)
