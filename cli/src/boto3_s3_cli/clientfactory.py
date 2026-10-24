"""Build boto3 clients from the parsed connection / auth globals.

The execution half of the global-option surface ``globalargs``
registers: ``build_client`` turns the connection / auth values into the
boto3 S3 client the library consumes (``design/aws-cli-option-handling.md``
section 5), and ``build_service_client`` builds the non-S3 clients ``mv``'s
path validation needs (section 5.8). Everything here reaches the AWS SDK.
"""

from __future__ import annotations

import argparse
import contextlib
import enum
from typing import TYPE_CHECKING, Any, Final, Literal, cast

# These exception names do not themselves import the AWS SDK.
from boto3_s3 import ConfigurationError, InvalidConfigError, InvalidValueError
from boto3_s3_cli import s3errormsg, textencoding

if TYPE_CHECKING:
    from boto3.s3.transfer import TransferConfig
    from boto3.session import Session as Boto3Session
    from botocore.session import Session as BotocoreSession
    from mypy_boto3_s3 import S3Client

    from boto3_s3 import S3
    from boto3_s3.awsclisession import AwsCliSettings


class _RegionUnresolved(enum.Enum):
    """The type behind `_UNRESOLVED` (a single-member enum, so a type checker
    narrows ``str | None | Literal[...]`` on an identity test)."""

    TOKEN = "unresolved"


# `build_client(region=...)` default: "not supplied - walk the region chain
# yourself". Distinct from a supplied ``None``, which is the chain's own answer
# when nothing resolves and must NOT send the caller back through it.
_UNRESOLVED: Final = _RegionUnresolved.TOKEN


def _pin_python_sigv4_signers() -> None:
    """Keep the symmetric-SigV4 signers pure-Python, as aws-cli v2 does.

    Stock botocore swaps ``v4`` / ``v4-query`` / ``s3v4`` / ``s3v4-query`` to
    CRT-backed signers whenever awscrt is importable - true once the ``crt``
    extra (it backs the CRT checksum algorithms, ``cp --checksum-algorithm
    CRC32C`` ...) or any co-installed package brings awscrt in. aws-cli v2's
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


def _settings(args: argparse.Namespace) -> AwsCliSettings:
    """The parsed connection / auth globals as the library's `AwsCliSettings`.

    The timeouts arrive as raw strings (see globalargs.add_common_arguments)
    and are coerced aws-cli-style here (`_coerce_cli_timeout`), which is what
    stops a run on a bad value (255) before anything is sent; ``--ca-bundle``
    is the typed path in the form the OS takes (`textencoding.os_path`).
    """
    from boto3_s3.awsclisession import AwsCliSettings

    return AwsCliSettings(
        profile=args.profile,
        region=args.region,
        endpoint_url=args.endpoint_url,
        no_verify_ssl=args.no_verify_ssl,
        ca_bundle=(
            textencoding.os_path(cast("str", args.ca_bundle))
            if args.ca_bundle is not None
            else None
        ),
        no_sign_request=args.no_sign_request,
        cli_read_timeout=(
            _coerce_cli_timeout(args.cli_read_timeout)
            if args.cli_read_timeout is not None
            else None
        ),
        cli_connect_timeout=(
            _coerce_cli_timeout(args.cli_connect_timeout)
            if args.cli_connect_timeout is not None
            else None
        ),
    )


def _open_botocore_session(args: argparse.Namespace) -> BotocoreSession:
    """Open a botocore session the way aws-cli's driver opens its own.

    The library's `awsclisession.open_botocore_session` does it - the profile
    and its env order, aws-cli's retry posture, the ``[s3]`` regional pin, the
    session default client config, the empty CA bundle check on every client,
    the on-disk credential cache - and this adds what only the command does.
    The SSO token cache botocore builds from a class attribute, read when the
    credential chain is built, gets aws-cli's write too: process-wide, which
    the command owning its process may set and the library may not. And the
    two output-format settings are checked (`_reject_unknown_cli_formats`),
    as aws-cli checks them when its session starts.
    """
    from botocore.tokens import SSOTokenProvider

    from boto3_s3 import awsclisession

    SSOTokenProvider.DEFAULT_CACHE_CLS = awsclisession.credential_cache_class()
    session = awsclisession.open_botocore_session(_settings(args))
    _reject_unknown_cli_formats(session, args)
    return session


# The values aws-cli's `cli_timestamp_format` and `cli_binary_format`
# customizations accept; the comparison is exact and case-sensitive.
_TIMESTAMP_FORMATS = ("wire", "iso8601")
_BINARY_FORMATS = ("base64", "raw-in-base64-out")


class UnknownTimestampFormatError(ConfigurationError):
    """An unknown ``cli_timestamp_format`` in the selected profile (rc 253).

    aws-cli raises its own ``ConfigurationError`` for it, the one failure on this
    surface its ``ConfigurationErrorHandler`` reports, so the report carries
    that handler's ``Configuration`` code (`cli._configuration_report`).
    """


def _reject_unknown_cli_formats(session: BotocoreSession, args: argparse.Namespace) -> None:
    """Stop on a ``cli_timestamp_format`` / ``cli_binary_format`` aws-cli refuses.

    aws-cli checks both settings in the selected profile when its session starts,
    so a value it does not accept stops every command: rc 253 for the
    timestamp format, rc 255 for the binary format (design/cli.md section 6).
    This CLI uses neither value for anything else - its listings print the
    same line under every timestamp format, and no option here takes binary
    data - so the check is all that is left of them. An empty value is a
    value and is refused. An explicit ``--cli-binary-format`` replaces the
    config value, and argparse has already held it to the two choices.

    The values are read from botocore's scoped config, so a profile no file
    declares (``ProfileNotFound``) and a config file botocore cannot parse fail
    here with botocore's own error, which the callers report at rc 255.
    """
    scoped = session.get_scoped_config()
    timestamp_format = scoped.get("cli_timestamp_format")
    if timestamp_format is not None and timestamp_format not in _TIMESTAMP_FORMATS:
        raise UnknownTimestampFormatError(
            f"Unknown cli_timestamp_format value: {timestamp_format}, "
            'valid values are "wire" or "iso8601"'
        )
    binary_format = scoped.get("cli_binary_format")
    if (
        args.cli_binary_format is None
        and binary_format is not None
        and binary_format not in _BINARY_FORMATS
    ):
        raise InvalidConfigError(
            f"Unknown cli_binary_format value: {binary_format}, "
            'valid values are "base64" or "raw-in-base64-out"'
        )


def validate_endpoint_url(args: argparse.Namespace) -> None:
    """Reject a schemeless ``--endpoint-url`` (rc 252), as aws-cli does
    (`awsclisession.validate_endpoint_url`).

    Called by the client builders before the client is created, where
    botocore would otherwise raise a bare ``ValueError``.
    """
    from boto3_s3.awsclisession import validate_endpoint_url as validate

    validate(args.endpoint_url)


def _construction_errors() -> contextlib.AbstractContextManager[None]:
    """`awsclisession.construction_errors`: botocore's construction-time errors,
    in the library taxonomy (design/cli.md section 6)."""
    from boto3_s3.awsclisession import construction_errors

    return construction_errors()


def build_session(args: argparse.Namespace) -> Boto3Session:
    """Build and validate the one boto3 session owned by a CLI `S3` command.

    Every session this CLI opens is built here - the client builders open
    theirs through it when no session is handed in. The session's clients
    parse response timestamps through the library's `fast_parse_timestamp`
    (registered on this CLI-owned session before any client is built) -
    large listings parse their ``LastModified`` values at C speed where
    aws-cli walks dateutil's generic parser per object, with byte-identical
    output.
    """
    from boto3_s3.awsclisession import boto3_session

    with _construction_errors():
        return boto3_session(_open_botocore_session(args))


def build_s3(args: argparse.Namespace) -> S3:
    """Build the command's `S3`, binding client and config reads to one session.

    The region chain is walked **once** here and threaded into every client
    this `S3` hands out. It is the invocation's most expensive resolution: its
    last link is the EC2 IMDS probe, which on a host with no region configured
    and no metadata service answering costs seconds - and the chain's answer
    cannot change within one invocation, since the env, the bound session's
    config and IMDS are all fixed by then. `_bind_region` puts it on the
    session so the clients *botocore* builds resolve it too.

    A run on the CRT engine needs that answer: aws-cli hands its region chain's
    answer to the CRT client, and with none the run stops before it starts,
    `--dryrun` included, where botocore would give the client the
    ``aws-global`` pseudo-region and the CRT would sign every request for it.
    The check sits at the engine's construction slot
    (`S3.materialize_crt_engine`, which every transfer command and ``rm``
    call there) and raises botocore's own missing-region error, kept as the
    cause so the report is aws-cli's ``NoRegion`` one, rc 253.
    """
    from boto3_s3 import S3, CrtClientOptions

    session = build_session(args)
    botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
    region = _resolve_region(args.region, botocore_session)
    _bind_region(botocore_session, args, region)
    from boto3_s3.awsclisession import crt_verify as resolve_crt_verify

    crt_verify = resolve_crt_verify(_settings(args), botocore_session)

    class CliS3(S3):
        def client(self) -> S3Client:
            return build_client(args, session=session, region=region)

        def materialize_crt_engine(
            self, client: S3Client, *, transfer_config: TransferConfig | None = None
        ) -> None:
            if region is None:
                from boto3_s3 import crtsupport

                if crtsupport.selects_crt(self.resolve_transfer_config(transfer_config)):
                    from botocore.exceptions import NoRegionError

                    cause = NoRegionError()
                    raise ConfigurationError(str(cause)) from cause
            super().materialize_crt_engine(client, transfer_config=transfer_config)

    # reusable_after_interrupt=False: Ctrl-C is process-fatal in the CLI, so an
    # operation's unwind must not wait for an in-flight listing page pull
    # (aws-cli dies immediately); the library default keeps waiting.
    # endpoint_url: build_client already applies it to every client this S3
    # hands out (the client() override), so the S3-level copy only feeds the
    # CRT lane's explicit-endpoint pin (design/crt.md) - without it, an
    # --endpoint-url under an AWS domain (a VPC interface endpoint) would be
    # dropped by the host heuristic and the CRT would re-resolve to public S3.
    # crt: the CRT client's own settings, read as aws-cli's CRT factory reads
    # them, apart from the botocore clients'.
    # allow_lockless=True: aws-cli documents an explicit 'crt' preference as
    # the way to make concurrently running processes use the CRT client, and
    # its factory builds the CRT client whether or not it got the
    # cross-process lock (a lock it does get still marks the slot for 'auto'
    # runs). An 'auto' preference respects the lock on both tools
    # (design/crt.md section 6).
    # sign_requests: aws-cli's CRT factory attaches a credentials provider
    # on its own `sign_request` parameter - `--no-sign-request` and nothing
    # else - so the per-client Config(signature_version='s3v4') that
    # `--sse aws:kms` adds, which does restore signing on the classic lane
    # (`_signature_version_pin`), never reaches its CRT client. Declaring the
    # flag directly reproduces that: measured on the pinned aws-cli against a
    # fake S3, `--no-sign-request --sse aws:kms` sends every CRT request
    # anonymously (single PUT, the multipart trio, download HEAD/GET, mv, sync,
    # stream) and uploads with no credentials at all, rc 0. Deriving the mode
    # from the client instead signed those requests here (design/crt.md
    # section 4).
    # verify: aws-cli's CRT factory resolves its own trust source
    # (`awsclisession.crt_verify`), which differs from its botocore clients' by ignoring
    # REQUESTS_CA_BUNDLE and certifi.
    # filename_encoding: the encoding aws-cli reads and writes local names with
    # in this environment, whatever Python's own choice (textencoding).
    return CliS3(
        session=session,
        endpoint_url=args.endpoint_url,
        reusable_after_interrupt=False,
        crt=CrtClientOptions(
            allow_lockless=True,
            sign_requests=not args.no_sign_request,
            verify=crt_verify,
        ),
        filename_encoding=textencoding.filename_encoding(),
    )


def _resolve_region(explicit: str | None, session: BotocoreSession) -> str | None:
    """The region to build a client in: ``--region``, else aws-cli's chain
    (`awsclisession.resolve_region`)."""
    from boto3_s3.awsclisession import resolve_region

    return resolve_region(explicit, session)


def _bind_region(
    botocore_session: BotocoreSession, args: argparse.Namespace, resolved: str | None
) -> None:
    """Bind onto the session the region aws-cli's own chain answers
    (`awsclisession.bind_region`; *resolved* is `_resolve_region`'s answer for
    ``args.region``). Only the builder that opened the session binds, so
    ``--source-region``'s client leaves the credential chain in the run's own
    region, as aws-cli's source client does."""
    from boto3_s3.awsclisession import bind_region

    bind_region(botocore_session, args.region, resolved)


def _client_verify(args: argparse.Namespace) -> bool | str | None:
    """The ``verify`` every botocore client this CLI builds is given: the TLS
    flags alone (`awsclisession.client_verify`), botocore resolving the rest."""
    from boto3_s3.awsclisession import client_verify

    return client_verify(_settings(args))


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
    and the TLS ``verify`` flags (`_client_verify`) - no endpoint
    override. A caller's ``None`` is left to the session, as there: aws-cli binds
    its region chain's answer into the session at startup, a truthy typed
    ``--region`` at its head, so a ``create_client`` with no ``region_name``
    lands on that answer - which `build_s3` bound, or `_bind_region` binds
    here onto a session this builder opened itself. ``--region ""`` is the
    corner that tells them apart: it never binds, so the source and sts
    clients resolve the rest of the chain while the destination's s3control,
    handed the flag itself, fails on the empty region (measured). aws-cli likewise resolves
    ``--cli-read-timeout`` / ``--cli-connect-timeout`` into the *session*
    default client config at startup, so ``from_session``'s ``create_client``
    inherits them; fold the same timeouts into this client's ``Config``.
    ``--no-sign-request`` and the retry posture arrive through the session
    itself (`awsclisession.default_client_config`, `_open_botocore_session`), which is
    where aws-cli puts them too, so this client is created through the shared
    `_create_client`.
    """
    # Deferred like build_client: only a command that opts into path
    # resolution (mv's --validate-same-s3-paths, which builds both resolver
    # clients regardless of the path shapes) pays the SDK import.
    from botocore.config import Config

    with _construction_errors():
        opened_here = session is None
        if session is None:
            session = build_session(args)
        botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        if opened_here:
            _bind_region(botocore_session, args, _resolve_region(args.region, botocore_session))
        # aws-cli's startup handlers thread the timeouts through the session
        # default config that from_session's create_client inherits
        # (build_client mirrors the same); the UNSIGNED signature arrives
        # through that config too, so this client never names one - which is
        # what keeps `mv`'s resolver clients anonymous under
        # --no-sign-request even when --sse aws:kms signs the transfer.
        client = _create_client(
            session,
            service,
            region_name=region,
            verify=_client_verify(args),
            config=Config(**_cli_timeouts(args)),
        )
    return client


def _cli_timeouts(args: argparse.Namespace) -> dict[str, Any]:
    """The ``Config`` fields of the timeout flags given (`awsclisession.cli_timeouts`)."""
    from boto3_s3.awsclisession import cli_timeouts

    return cli_timeouts(_settings(args))


def _coerce_cli_timeout(value: str) -> int:
    """aws-cli's ``_resolve_timeout`` coercion of a raw timeout string: ``int(value)``.

    aws-cli applies this in a post-parse session handler, so a non-integer value
    raises a bare ``ValueError`` that reaches its general handler (rc 255), not
    the rc 252 of an argparse/usage error - the same path ``--page-size`` already
    takes. Translate that failure to ``InvalidValueError`` (-> 255) for parity
    instead of declaring the argparse arg ``type=int``. ``0`` stays ``0``: the
    library's settings read it as "no timeout" (aws-cli's ``int(value) or
    None``, cli.json help), botocore (via urllib3) rejecting a literal ``0``.
    """
    try:
        return int(value)
    except ValueError as exc:
        raise InvalidValueError(str(exc)) from exc


def _includes_endpoint_auth_path(args: argparse.Namespace) -> bool:
    """Whether a positional S3 path needs botocore's endpoint auth-scheme resolution.

    True for the shapes that must sign asymmetric SigV4a (an MRAP bucket, an
    S3 Outposts access point in either notation - the ARN or the ``--op-s3``
    alias) and for an S3 Express directory bucket or one of its access points
    (``--x-s3`` / ``--xa-s3``: must sign ``sigv4-s3express`` with
    `CreateSession` credentials) - the shapes an explicit `signature_version`
    would mis-sign, so the SigV4 pin stands down for them.
    Reads the parsed positionals off the namespace - `paths` (a string, or the
    transfer family's two-item list) and presign's `path`. The single-path
    commands' positionals arrive paramfile-expanded by client-build time; the
    transfer family's `paths` are consumed raw (a `file://` form there is
    just a local path string, hiding nothing S3-shaped). Non-string values
    never name either shape and are skipped.

    presign's `path` is always an S3 reference - the command takes the target
    with or without the `s3://` scheme (unlike the transfer family, where a
    scheme-less positional is a local path). So a scheme-less directory-bucket
    presign (``presign bucket--zone--x-s3/key``) is normalized to the `s3://`
    form before the check, which `is_s3express_path` and
    `is_outpost_alias_path` require precisely because a transfer positional
    could be a local file ending in ``--x-s3`` or ``--op-s3``. Without this,
    the SigV4 pin would stay on and the URL would sign plain SigV4 with no
    `CreateSession` - unusable against the directory bucket, where aws-cli
    (resolving the auth scheme off the final Bucket, not the input notation)
    signs ``sigv4-s3express``; an Outposts alias would likewise get a
    symmetric signature its SigV4a endpoint rejects.
    """
    from boto3_s3.pathresolver import (
        is_mrap_path,
        is_outpost_alias_path,
        is_outpost_path,
        is_s3express_accesspoint_path,
        is_s3express_path,
    )

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
        isinstance(value, str)
        and (
            is_mrap_path(value)
            or is_outpost_path(value)
            or is_outpost_alias_path(value)
            or is_s3express_path(value)
            or is_s3express_accesspoint_path(value)
        )
        for value in values
    )


def _signature_version_pin(args: argparse.Namespace) -> str | None:
    """The ``signature_version`` this run's S3 client names, or None for none.

    ``v4`` - always-on SigV4 (design/cli.md section 4 item 2): aws-cli v2's
    bundled botocore has no S3 SigV2 (hmacv1 "s3"-family signers) left - only
    the generic query-protocol "v2" - while stock botocore still downgrades
    *presigned URLs* to SigV2 in regions that accept it (a default us-east-1
    client). Pin SigV4 so every command - visibly, presign's URLs - matches aws-cli
    v2. (The other half of that divergence, us-east-1 resolving to the legacy
    global endpoint, is pinned on the session instead:
    `_pin_regional_s3_endpoint`.) The pinned name is the generic ``v4``, the
    one aws-cli's endpoint resolution hands its requests: botocore keeps a
    requested name as the request's ``auth_type``, and its own
    ``choose-signer`` handler turns ``v4`` into the S3 signer (``s3v4``) and
    answers there, while an ``s3v4`` there is passed by and left to whatever
    handler comes next. Every ``choose-signer`` handler also sees ``v4`` /
    ``v4-query``, as on `aws`.

    None when the command targets an MRAP ARN, an S3 Outposts access point
    (ARN or ``--op-s3`` alias), or an S3 Express directory bucket
    (`_includes_endpoint_auth_path`): an explicit signature_version suppresses
    botocore's auth-scheme resolution, and those endpoints must resolve to
    asymmetric SigV4a (the MRAP and Outposts shapes) / ``sigv4-s3express``
    (with `CreateSession` credentials) - a pinned ``s3v4`` (or ``v4``) matches
    all those scheme names up to the first dash and silently signs a plain
    SigV4 request instead. aws-cli v2's bundled botocore pins only the symmetric
    families (`_pin_python_sigv4_signers`) and leaves the resolutions alive.
    With awscrt absent, a SigV4a target (MRAP or Outposts) surfaces botocore's
    own MissingDependencyException (-> ConfigurationError, 253) instead of a
    silently mis-signed SigV4 request.

    None for an anonymous run (``--no-sign-request``) too: aws-cli leaves UNSIGNED
    where its startup handler put it - the session default client config
    (`awsclisession.default_client_config`) - and that is what must reach the client; a
    per-client ``signature_version``, this pin included, beats it in
    botocore's merge.

    ``s3v4`` under ``--sse aws:kms`` (`_requests_kms_signing`), the exception
    to both stand-downs: it is the one case for which aws-cli names a per-client
    ``signature_version`` - its ``ClientFactory.create_client`` passes
    ``Config(signature_version='s3v4')`` whatever the target. botocore merges
    the per-client config on top of the session's, so that one case signs,
    resolves credentials, and reaches the credential chain, while every other
    unsigned run stays anonymous. Measured without credentials:
    ``cp``/``mv``/``sync``/``cp s3://.. s3://..`` with
    ``--no-sign-request --sse aws:kms`` are rc 1 ``Unable to locate
    credentials`` and send nothing under `aws` (this CLI uploaded anonymously,
    rc 0), and a broken ``source_profile`` under the same flags is aws-cli's
    rc 255 profile report; ``--sse AES256`` and ``--sse-c AES256`` upload
    anonymously on both. An MRAP, Outposts or S3 Express target signs plain
    SigV4 there too, with or without ``--no-sign-request`` (measured offline
    on the pinned `aws`: ``AWS4-HMAC-SHA256`` with the client's region in the
    scope, where the resolution would have chosen SigV4a /
    ``sigv4-s3express``). That pin keeps aws-cli's own ``s3v4``, and with it the
    ``auth_type`` botocore's handler passes by.

    This governs the *botocore* client alone, which is aws-cli's own split: its
    CRT factory reads ``--no-sign-request`` directly and never the client, so
    the same KMS run transfers anonymously on the CRT lane. `build_s3`
    declares that separately (``CrtClientOptions.sign_requests``).
    """
    if _requests_kms_signing(args):
        return "s3v4"
    if _includes_endpoint_auth_path(args) or args.no_sign_request:
        return None
    return "v4"


def _requests_kms_signing(args: argparse.Namespace) -> bool:
    """Whether aws-cli would hand this run's S3 clients ``signature_version='s3v4'``.

    aws-cli's ``ClientFactory.create_client`` does so for ``--sse aws:kms`` and
    nothing else - aws-cli's own exact string, so ``aws:kms:dsse`` does not - and
    for the source client of an S3-to-S3 copy as for the destination's.
    ``--sse`` belongs to the transfer family alone, hence the ``getattr``.
    """
    return getattr(args, "sse", None) == "aws:kms"


def build_client(
    args: argparse.Namespace,
    *,
    session: Boto3Session | None = None,
    region: str | None | Literal[_RegionUnresolved.TOKEN] = _UNRESOLVED,
) -> S3Client:
    """Build the boto3 S3 client from the connection/auth globals (section 5).

    ``--profile`` selects the session (falling back to the ``AWS_PROFILE`` >
    ``AWS_DEFAULT_PROFILE`` env chain, aws-cli order - `_open_botocore_session`);
    the region resolves through aws-cli's chain (``--region`` > ``AWS_REGION`` >
    ``AWS_DEFAULT_REGION`` > config > IMDS - ``_resolve_region``), unless
    *region* supplies the chain's already-computed answer (what `build_s3`
    threads in so one invocation walks the chain - and its IMDS probe - once);
    ``--endpoint-url`` and the timeouts map to client kwargs / a botocore
    ``Config``; ``--no-verify-ssl`` and ``--ca-bundle`` become ``verify``
    (`_client_verify`), and botocore resolves the rest of the chain. Every client also
    carries aws-cli's S3 error-message rewriter (`s3errormsg`). The client is
    handed to the library through ``S3Storage`` - the library never rebuilds
    connection settings itself.

    ``--no-sign-request``, the retry posture and the ``[s3]`` regional pin are
    not client kwargs at all: they belong to the session
    (`awsclisession.default_client_config`, `_open_botocore_session`), which is what puts
    the clients botocore builds for itself on them too.
    """
    # The SDK is imported here, not at module load: the informational exits
    # (`--version`, the help token) return before this normal-dispatch path.
    from botocore.config import Config

    from boto3_s3 import awsclisession

    _pin_python_sigv4_signers()

    validate_endpoint_url(args)

    overrides: dict[str, Any] = {}
    signature_version = _signature_version_pin(args)
    if signature_version is not None:
        overrides["signature_version"] = signature_version
    overrides.update(_cli_timeouts(args))

    with _construction_errors():
        opened_here = session is None
        if session is None:
            session = build_session(args)
        botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        client_region = (
            _resolve_region(args.region, botocore_session) if region is _UNRESOLVED else region
        )
        if opened_here:
            # A session handed in belongs to its opener, who bound it: a
            # client built for another region (`--source-region`) must not
            # rebind it under the credential chain (`_bind_region`).
            _bind_region(botocore_session, args, client_region)
        client = _create_client(
            session,
            "s3",
            region_name=client_region,
            endpoint_url=args.endpoint_url,
            verify=_client_verify(args),
            config=Config(**overrides),
        )
        s3errormsg.register(client)
        awsclisession.register_default_checksum(client)
    return client


def _create_client(session: Boto3Session, service: str, **kwargs: Any) -> Any:
    """``session.client`` with the CONNECT pin and the CA bundle check around it.

    Every client this CLI hands out is created here, which is what makes this
    the place the CONNECT pin goes on (`boto3_s3.pin_proxy_connect`): an HTTPS
    proxy sees the tunnel request the *host interpreter* writes, and only 3.12
    and later write aws-cli's. The pin is process-wide, which the CLI, owning
    its process, may install where the library may not on its own.
    """
    from boto3_s3 import pin_proxy_connect
    from boto3_s3.awsclisession import create_client

    pin_proxy_connect()
    return create_client(session, service, **kwargs)
