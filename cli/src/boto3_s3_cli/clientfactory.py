"""Build boto3 clients from the parsed connection / auth globals.

The execution half of the global-option surface ``globalargs``
registers: ``build_client`` turns the connection / auth values into the
boto3 S3 client the library consumes (``docs/aws-cli-option-handling.md``
section 5), and ``build_service_client`` builds the non-S3 clients ``mv``'s
path validation needs (section 5.8). Everything here reaches the AWS SDK.
"""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

# These exception names do not themselves import the AWS SDK.
from boto3_s3 import ConfigurationError, InvalidConfigError, InvalidValueError, ValidationError
from boto3_s3_cli.globalargs import PROFILE_ENV_VARS

if TYPE_CHECKING:
    from boto3.session import Session as Boto3Session
    from botocore.session import Session as BotocoreSession
    from mypy_boto3_s3 import S3Client

    from boto3_s3 import S3


def _pin_python_sigv4_signers() -> None:
    """Keep the symmetric-SigV4 signers pure-Python, as aws v2 does.

    Stock botocore swaps ``v4`` / ``v4-query`` / ``s3v4`` / ``s3v4-query`` to
    CRT-backed signers whenever awscrt is importable - true once the ``crt``
    extra (it backs the CRT checksum algorithms, ``cp --checksum-algorithm
    CRC32C`` ...) or any co-installed package brings awscrt in. aws v2's
    bundled botocore instead hard-pins the pure-Python classes for those
    four, reserving CRT for the asymmetric SigV4a family (aws-cli's
    ``awscli/botocore/auth.py`` ``AUTH_TYPE_MAPS``). The difference is
    user-visible: the CRT presigner renders ``X-Amz-Expires`` after
    ``X-Amz-SignedHeaders``, so ``presign``'s URLs would diverge. Restore
    aws-cli's entries (in-place table update - botocore resolves the signer
    from this table per request, however it was imported; without awscrt
    this just re-asserts the defaults).
    """
    from botocore import auth

    auth.AUTH_TYPE_MAPS.update(
        {
            "v4": auth.SigV4Auth,
            "v4-query": auth.SigV4QueryAuth,
            "s3v4": auth.S3SigV4Auth,
            "s3v4-query": auth.S3SigV4QueryAuth,
        }
    )


def resolve_profile(args: argparse.Namespace) -> str | None:
    """The profile to open the session with (aws-cli precedence).

    ``--profile`` (truthy only) > the first env var of
    ``PROFILE_ENV_VARS`` that is *present* > ``None``
    (boto3 then falls back to the ``default`` profile). The ``--profile`` guard is
    aws-cli's truthy test (its ``_handle_top_level_args`` binds the profile only
    ``if getattr(args, 'profile', False)``), so an empty ``--profile ""`` is
    ignored and the env chain wins - matching aws, which then reaches the server
    (rc 254) rather than raising ProfileNotFound. The env chain is instead
    present-wins, an empty value included (``AWS_PROFILE=`` selects the empty
    profile -> ProfileNotFound, like aws), so `aws s3` parity holds even when both
    env vars are set. Only the CLI layer corrects this; the library (boto3.client
    fallback) stays boto3/botocore-faithful and keeps stock order on purpose.
    """
    if args.profile:
        return args.profile
    for name in PROFILE_ENV_VARS:
        if name in os.environ:
            return os.environ[name]
    return None


def validate_endpoint_url(args: argparse.Namespace) -> None:
    """Reject a schemeless ``--endpoint-url`` with aws's wording (rc 252).

    aws-cli validates the value at parse time - before the integer coercions,
    the session profile, and every path validation - so this runs first in
    the commands whose later checks could otherwise mask it (measured against
    the pinned aws-cli: ``--page-size abc --endpoint-url badurl`` is the endpoint's
    252, not the conversion's 255). Also called inside the client builders,
    where botocore would otherwise raise a bare ``ValueError``.
    """
    endpoint_url: str | None = args.endpoint_url
    if endpoint_url is not None and not urlparse(endpoint_url).scheme:
        raise ValidationError(
            f'Bad value for --endpoint-url "{endpoint_url}": scheme is '
            "missing.  Must be of the form http://<hostname>/ or https://<hostname>/"
        )


def validate_profile(args: argparse.Namespace) -> None:
    """Resolve the session profile the way aws does at startup (rc 255 on failure).

    aws binds ``--profile`` / the profile env chain into its session before
    any command validation runs, so a bad profile fails during the startup
    config reads (ProfileNotFound -> its general handler, rc 255) ahead of
    every post-parse usage error (252) - while an unresolvable *region* does
    NOT fail here (aws defers it to request time). Mirror the ordering by
    forcing one scoped-config read on a session bound to the resolved profile.
    """
    import botocore.session
    from botocore.exceptions import BotoCoreError

    try:
        botocore.session.Session(profile=resolve_profile(args)).get_scoped_config()
    except BotoCoreError as exc:
        raise InvalidConfigError(str(exc)) from exc


def build_session(args: argparse.Namespace) -> Boto3Session:
    """Build and validate the one boto3 session owned by a CLI `S3` command.

    The session's clients parse response timestamps through the library's
    `fast_parse_timestamp` (registered on this CLI-owned session before any
    client is built) - large listings parse their ``LastModified`` values at
    C speed where aws-cli walks dateutil's generic parser per object, with
    byte-identical output.
    """
    import boto3
    import botocore.session
    from botocore.exceptions import BotoCoreError

    from boto3_s3 import fast_parse_timestamp

    try:
        botocore_session = botocore.session.Session(profile=resolve_profile(args))
        botocore_session.get_scoped_config()
        botocore_session.get_component("response_parser_factory").set_parser_defaults(
            timestamp_parser=fast_parse_timestamp
        )
        return boto3.Session(botocore_session=botocore_session)
    except BotoCoreError as exc:
        raise InvalidConfigError(str(exc)) from exc


def build_s3(args: argparse.Namespace) -> S3:
    """Build the command's `S3`, binding client and config reads to one session."""
    from boto3_s3 import S3

    session = build_session(args)

    class CliS3(S3):
        def client(self) -> S3Client:
            return build_client(args, session=session)

    # wait_on_interrupt=False: Ctrl-C is process-fatal in the CLI, so an
    # operation's unwind must not wait for an in-flight listing page pull
    # (aws dies immediately); the library default keeps waiting.
    # endpoint_url: build_client already applies it to every client this S3
    # hands out (the client() override), so the S3-level copy only feeds the
    # CRT lane's explicit-endpoint pin (docs/crt.md) - without it, an
    # --endpoint-url under an AWS domain (a VPC interface endpoint) would be
    # dropped by the host heuristic and the CRT would re-resolve to public S3.
    return CliS3(session=session, endpoint_url=args.endpoint_url, wait_on_interrupt=False)


def _resolve_region(explicit: str | None, session: BotocoreSession) -> str | None:
    """The region to build the client in, via aws-cli's region chain.

    Mirrors aws-cli's ``_construct_cli_region_chain``: the *explicit* value
    (``--region``, or the caller's region for ``build_service_client``) >
    ``AWS_REGION`` env > ``AWS_DEFAULT_REGION`` env > the profile's config-file
    ``region`` > the EC2 IMDS region. Stock botocore never adopted ``AWS_REGION``
    (its region env is ``AWS_DEFAULT_REGION`` alone) and reserves its
    ``IMDSRegionProvider`` for smart-defaults, so a bare client would resolve a
    *different* region whenever ``AWS_REGION`` is the only source, or on an EC2
    host with no region configured. The env vars are present-wins, an empty value
    included (``AWS_REGION=`` -> ``""`` -> the same ``Invalid endpoint`` failure
    as aws, rc 255). Only the CLI corrects this; the library
    (``S3.client``'s ``boto3.client`` fallback) keeps stock botocore order on
    purpose - the same library=boto3 / CLI=aws split as the profile chain.
    """
    if explicit is not None:
        return explicit
    # Import the providers only when region resolution needs them.
    from botocore.configprovider import (
        ChainProvider,
        EnvironmentProvider,
        ScopedConfigProvider,
    )
    from botocore.utils import IMDSRegionProvider

    # botocore-stubs types ChainProvider's `providers` as Sequence[BaseProvider],
    # but IMDSRegionProvider (botocore.utils) is not declared a BaseProvider there
    # even though aws-cli composes it into this very chain; list[Any] bridges the
    # stub gap (all four are duck-typed providers exposing .provide()).
    providers: list[Any] = [
        EnvironmentProvider(name="AWS_REGION", env=os.environ),
        EnvironmentProvider(name="AWS_DEFAULT_REGION", env=os.environ),
        ScopedConfigProvider(config_var_name="region", session=session),
        IMDSRegionProvider(session),
    ]
    return ChainProvider(providers=providers).provide()


def build_service_client(
    service: str,
    args: argparse.Namespace,
    *,
    region: str | None = None,
    session: Boto3Session | None = None,
) -> Any:
    """Build a non-S3 service client for path validation (``mv``'s resolver).

    aws-cli's ``S3PathResolver.from_session``: a plain ``create_client`` carrying
    only the profile session, the caller's region choice (the source side
    passes ``--source-region``, the destination ``--region``, sts none),
    and the ``--no-verify-ssl`` / ``--ca-bundle`` verify setting - no endpoint
    override. aws binds ``--region`` into the session itself at startup
    (clidriver's ``set_config_variable``), so a ``create_client`` with no
    ``region_name`` still lands in ``--region``; mirror that by falling back
    from the caller's ``None`` to ``args.region`` before the shared
    ``_resolve_region`` chain (``AWS_REGION`` > ``AWS_DEFAULT_REGION`` >
    config > IMDS). aws likewise resolves ``--cli-read-timeout`` /
    ``--cli-connect-timeout`` and ``--no-sign-request`` into the *session*
    default client config at startup, so ``from_session``'s ``create_client``
    inherits them; fold the same timeouts and the UNSIGNED signature into this
    client's ``Config``.
    """
    # Deferred like build_client: only a command that opts into path
    # resolution (mv's --validate-same-s3-paths, which builds both resolver
    # clients regardless of the path shapes) pays the boto3 import.
    import boto3
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, NoCredentialsError, NoRegionError

    verify: bool | str | None
    if args.no_verify_ssl:
        verify = False
    else:
        verify = args.ca_bundle
    # Client construction can raise raw botocore errors (e.g. ProfileNotFound for
    # a bad --profile); translate them so the credential/region 253 split
    # survives - untranslated they would fall to the dispatcher's generic
    # chain (see build_client).
    try:
        if session is None:
            from boto3_s3 import fast_parse_timestamp

            botocore_session = botocore.session.Session(profile=resolve_profile(args))
            # Same fast timestamp parsing as build_session: every CLI-built
            # session carries it.
            botocore_session.get_component("response_parser_factory").set_parser_defaults(
                timestamp_parser=fast_parse_timestamp
            )
            session = boto3.Session(botocore_session=botocore_session)
        else:
            botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        # aws's bundled botocore applies its standard/3 retry defaults to
        # these validator clients too, and its startup handlers thread the
        # timeouts / UNSIGNED signature through the session default config that
        # from_session's create_client inherits (build_client mirrors the same).
        service_overrides: dict[str, Any] = {"retries": _retry_defaults(botocore_session)}
        if args.no_sign_request:
            service_overrides["signature_version"] = UNSIGNED
        if args.cli_read_timeout is not None:
            service_overrides["read_timeout"] = _coerce_cli_timeout(args.cli_read_timeout)
        if args.cli_connect_timeout is not None:
            service_overrides["connect_timeout"] = _coerce_cli_timeout(args.cli_connect_timeout)
        return session.client(
            service,
            region_name=_resolve_region(
                region if region is not None else args.region, botocore_session
            ),
            verify=verify,
            config=Config(**service_overrides),
        )
    except (NoCredentialsError, NoRegionError) as exc:
        raise ConfigurationError(str(exc)) from exc
    except BotoCoreError as exc:
        raise InvalidConfigError(str(exc)) from exc


def resolve_cli_timeouts(args: argparse.Namespace) -> None:
    """Coerce ``--cli-read-timeout`` then ``--cli-connect-timeout`` (255 on failure).

    aws resolves both at ``top-level-args-parsed`` in registration order (read
    first), before any command-layer parsing, so a bad value beats an invalid
    choice, unknown options, and missing arguments; the dispatcher's pre-pass
    calls this to keep that order. Validation-only: the client builders coerce
    the same namespace strings again when they build.
    """
    if args.cli_read_timeout is not None:
        _coerce_cli_timeout(args.cli_read_timeout)
    if args.cli_connect_timeout is not None:
        _coerce_cli_timeout(args.cli_connect_timeout)


def _coerce_cli_timeout(value: str) -> int | None:
    """aws-cli's ``_resolve_timeout`` coercion: ``int(value)``, then ``0`` -> ``None``.

    aws applies this in a post-parse session handler, so a non-integer value
    raises a bare ``ValueError`` that reaches its general handler (rc 255), not
    the rc 252 of an argparse/usage error - the same path ``--page-size`` already
    takes. Translate that failure to ``InvalidValueError`` (-> 255) for parity
    instead of declaring the argparse arg ``type=int``. The ``0`` -> ``None``
    sentinel ("no timeout"; cli.json help) is preserved because ``int("0") or
    None`` is ``None``, and botocore (via urllib3) rejects a literal ``0`` anyway.
    """
    try:
        return int(value) or None
    except ValueError as exc:
        raise InvalidValueError(str(exc)) from exc


def _includes_endpoint_auth_path(args: argparse.Namespace) -> bool:
    """Whether a positional S3 path needs botocore's endpoint auth-scheme resolution.

    True for an MRAP ARN bucket (must sign asymmetric SigV4a) and for an
    S3 Express directory bucket (must sign ``sigv4-s3express`` with
    `CreateSession` credentials) - the two shapes an explicit
    `signature_version` would mis-sign, so the s3v4 pin stands down for them.
    Reads the parsed positionals off the namespace - `paths` (a string, or the
    transfer family's two-item list) and presign's `path`. The single-path
    commands' positionals arrive paramfile-expanded by client-build time; the
    transfer family's `paths` are consumed raw (a `file://` form there is
    just a local path string, hiding nothing S3-shaped). Non-string values
    (the readable-`fileb://` quirk leaves
    `bytes`) never name either shape and are skipped.

    presign's `path` is always an S3 reference - the command takes the target
    with or without the `s3://` scheme (unlike the transfer family, where a
    scheme-less positional is a local path). So a scheme-less directory-bucket
    presign (``presign bucket--zone--x-s3/key``) is normalized to the `s3://`
    form before the check, which `is_s3express_path` requires precisely
    because a transfer positional could be a local file ending in ``--x-s3``.
    Without this, the s3v4 pin would stay on and the URL would sign plain
    SigV4 with no `CreateSession` - unusable against the directory bucket,
    where aws (resolving the auth scheme off the final Bucket, not the input
    notation) signs ``sigv4-s3express``.
    """
    from boto3_s3.pathresolver import is_mrap_path, is_s3express_path

    values: list[object] = []
    paths: object = getattr(args, "paths", None)
    if isinstance(paths, (list, tuple)):
        values.extend(cast("list[object]", paths))
    else:
        values.append(paths)
    presign_path = getattr(args, "path", None)
    if isinstance(presign_path, str) and not presign_path.startswith("s3://"):
        presign_path = f"s3://{presign_path}"
    values.append(presign_path)
    return any(
        isinstance(value, str) and (is_mrap_path(value) or is_s3express_path(value))
        for value in values
    )


def build_client(args: argparse.Namespace, *, session: Boto3Session | None = None) -> S3Client:
    """Build the boto3 S3 client from the connection/auth globals (section 5).

    ``--profile`` selects the session (falling back to the ``AWS_PROFILE`` >
    ``AWS_DEFAULT_PROFILE`` env chain, aws-cli order - ``resolve_profile``);
    the region resolves through aws-cli's chain (``--region`` > ``AWS_REGION`` >
    ``AWS_DEFAULT_REGION`` > config > IMDS - ``_resolve_region``);
    ``--endpoint-url``, the timeouts, and ``--no-sign-request`` map to client
    kwargs / a botocore ``Config``; ``--no-verify-ssl`` and ``--ca-bundle`` map to
    ``verify``. The client is handed to the library through ``S3Storage`` - the
    library never rebuilds connection settings itself.
    """
    # Importing boto3 drags in botocore and s3transfer. The top-level
    # --help/--version exits return before this normal-dispatch path.
    import boto3
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, NoCredentialsError, NoRegionError

    _pin_python_sigv4_signers()

    # The transfer family already validated this up front (parse-time order);
    # re-checked here for the commands that build their client first (mb/rb)
    # and for direct callers - botocore would otherwise raise a bare
    # ValueError at client creation.
    validate_endpoint_url(args)

    verify: bool | str | None
    if args.no_verify_ssl:
        verify = False
    else:
        verify = args.ca_bundle  # a path, or None to use the default trust store

    # aws-cli v2's bundled botocore has no S3 SigV2 (hmacv1 "s3"-family
    # signers) left - only the generic query-protocol "v2" - while stock
    # botocore still downgrades *presigned URLs* to SigV2 in regions that
    # accept it (a default us-east-1 client) and resolves us-east-1
    # to the legacy global endpoint where aws v2 uses the regional one. Pin
    # both so every command - visibly, presign's URLs - matches aws v2.
    # The pin stands down when the command targets an MRAP ARN or an S3
    # Express directory bucket: an explicit signature_version suppresses
    # botocore's auth-scheme resolution, and those endpoints must resolve to
    # asymmetric SigV4a / ``sigv4-s3express`` (with `CreateSession`
    # credentials) respectively - a pinned s3v4 matches both scheme names up
    # to the first dash and silently signs a plain SigV4 request instead.
    # aws v2's bundled botocore pins only the symmetric families
    # (_pin_python_sigv4_signers) and leaves both resolutions alive. With
    # awscrt absent, an MRAP target surfaces botocore's own
    # MissingDependencyException (-> ConfigurationError, 253) instead of a
    # silently mis-signed SigV4 request.
    overrides: dict[str, Any] = {
        "s3": {"us_east_1_regional_endpoint": "regional"},
    }
    if not _includes_endpoint_auth_path(args):
        overrides["signature_version"] = "s3v4"
    if args.no_sign_request:
        overrides["signature_version"] = UNSIGNED
    # The timeouts arrive as raw strings (see globalargs.add_common_arguments)
    # and are coerced here, aws-cli-style: int() with a 0 -> None ("no timeout")
    # sentinel, a bad value mapped to rc 255 rather than a parse-time rc 252.
    if args.cli_read_timeout is not None:
        overrides["read_timeout"] = _coerce_cli_timeout(args.cli_read_timeout)
    if args.cli_connect_timeout is not None:
        overrides["connect_timeout"] = _coerce_cli_timeout(args.cli_connect_timeout)

    # Client construction can raise raw botocore errors (e.g. ProfileNotFound for
    # a bad --profile, or credential/region resolution failures). Translate them
    # into the library taxonomy so exit_code_for maps them (credential/region ->
    # ConfigurationError [253], the rest -> InvalidConfigError [255]); left
    # raw they would fall to the dispatcher's generic chain, losing that
    # 253/255 split.
    try:
        if session is None:
            from boto3_s3 import fast_parse_timestamp

            botocore_session = botocore.session.Session(profile=resolve_profile(args))
            # Same fast timestamp parsing as build_session: every CLI-built
            # session carries it.
            botocore_session.get_component("response_parser_factory").set_parser_defaults(
                timestamp_parser=fast_parse_timestamp
            )
            session = boto3.Session(botocore_session=botocore_session)
        else:
            botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        overrides["retries"] = _retry_defaults(botocore_session)
        config = Config(**overrides)
        return session.client(
            "s3",
            region_name=_resolve_region(args.region, botocore_session),
            endpoint_url=args.endpoint_url,
            verify=verify,
            config=config,
        )
    except (NoCredentialsError, NoRegionError) as exc:
        # aws has dedicated handlers for these two (-> 253); every other botocore
        # error (ProfileNotFound, PartialCredentialsError, ...) falls to its
        # GeneralExceptionHandler (-> 255) via the BotoCoreError clause below -
        # InvalidConfigError, which exit_code_for maps to 255, not 253.
        raise ConfigurationError(str(exc)) from exc
    except BotoCoreError as exc:
        raise InvalidConfigError(str(exc)) from exc


def _retry_defaults(botocore_session: BotocoreSession) -> dict[str, Any]:
    """aws v2's retry defaults, honoring the user's env/config overrides.

    aws v2's bundled botocore hard-codes ``retry_mode='standard'`` and
    ``max_attempts=3`` as its session defaults (its ``configprovider``),
    where stock botocore defaults to ``legacy`` with 5 total attempts - a
    user-visible difference in how ``aws s3`` behaves under throttling. The
    aws default fills in only when neither ``AWS_RETRY_MODE`` /
    ``AWS_MAX_ATTEMPTS`` nor the profile's ``retry_mode`` / ``max_attempts``
    supplies a value, exactly like the bundled default chain. A supplied mode
    is validated here against the bundled botocore's restricted set -
    ``standard`` / ``adaptive`` only, with aws's exact wording (measured, rc
    255): stock botocore would otherwise *accept* ``legacy`` (its own valid
    mode) where aws v2 rejects it. An unconvertible ``max_attempts`` raises
    ``ValueError`` like the bundled botocore's int cast (aws's general
    handler, rc 255 - main()'s backstop maps it the same).
    """
    scoped = botocore_session.get_scoped_config()
    # Present-wins reads, like the profile/region env chains: aws treats a
    # present-but-empty AWS_RETRY_MODE / AWS_MAX_ATTEMPTS as a fatal value
    # (rc 255), never as "unset" - the empty string fails the mode validation
    # below (attempts via the int cast).
    mode = os.environ.get("AWS_RETRY_MODE")
    if mode is None:
        mode = scoped.get("retry_mode", "standard")
    if mode not in ("standard", "adaptive"):
        raise InvalidConfigError(
            f'Invalid value provided to "mode": "{mode}" must be one of: "standard" or "adaptive"'
        )
    attempts_raw = os.environ.get("AWS_MAX_ATTEMPTS")
    if attempts_raw is None:
        attempts_raw = scoped.get("max_attempts")
    attempts = 3 if attempts_raw is None else int(attempts_raw)
    return {"mode": mode, "total_max_attempts": attempts}
