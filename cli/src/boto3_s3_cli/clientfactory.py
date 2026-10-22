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
import json
import os
from collections.abc import Callable, Generator
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urlparse

# These exception names do not themselves import the AWS SDK.
from boto3_s3 import ConfigurationError, InvalidConfigError, InvalidValueError, ValidationError
from boto3_s3_cli import checksumdefault, s3errormsg, textencoding
from boto3_s3_cli.globalargs import PROFILE_ENV_VARS

if TYPE_CHECKING:
    from boto3.s3.transfer import TransferConfig
    from boto3.session import Session as Boto3Session
    from botocore.session import Session as BotocoreSession
    from mypy_boto3_s3 import S3Client

    from boto3_s3 import S3


class _RegionUnresolved(enum.Enum):
    """The type behind `_UNRESOLVED` (a single-member enum, so a type checker
    narrows ``str | None | Literal[...]`` on an identity test)."""

    TOKEN = "unresolved"


# `build_client(region=...)` default: "not supplied - walk the region chain
# yourself". Distinct from a supplied ``None``, which is the chain's own answer
# when nothing resolves and must NOT send the caller back through it.
_UNRESOLVED: Final = _RegionUnresolved.TOKEN


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


def _open_botocore_session(args: argparse.Namespace) -> BotocoreSession:
    """Open a botocore session the way aws-cli's driver opens its own.

    Only a truthy ``--profile`` is bound onto the session (aws-cli's
    ``_handle_top_level_args``: ``set_config_variable('profile', ...)`` under a
    truthy guard). A profile named by ``AWS_PROFILE`` / ``AWS_DEFAULT_PROFILE``
    is deliberately left to botocore's own env read, because the two are not
    interchangeable: botocore treats a *session instance* profile as "the user
    asked for this profile explicitly" and drops the environment credential
    provider for it (``create_credential_resolver``'s ``disable_env_vars``), so
    promoting the env-named profile would turn ``AWS_PROFILE=x`` plus
    ``AWS_ACCESS_KEY_ID`` - a routine CI shape that works under aws - into
    "Unable to locate credentials". Redeclaring the profile config variable
    restores aws's env order (stock botocore reads ``AWS_DEFAULT_PROFILE``
    first, botocore #1725) without that promotion.

    ``retry_mode`` / ``max_attempts`` are redeclared with the defaults aws v2's
    bundled botocore hard-codes (``standard`` / 3, where stock botocore
    declares ``legacy`` and no cap). Declaring them on the *session* is what
    puts every client it builds on aws's retry posture - not just the ones this
    module builds, but the STS / SSO clients the credential chain creates for
    itself, which a per-client ``Config`` never reaches (measured: a failing
    ``AssumeRole`` is attempted 3 times under aws and was attempted 5 here).
    The ``[s3]`` regional pin (`_pin_regional_s3_endpoint`) and the timeouts in
    the session default client config (`_default_client_config`) reach the same
    clients for the same reason.

    Finally the two output-format settings are checked
    (`_reject_unknown_cli_formats`) and the temporary-credential providers get
    their on-disk cache (`_inject_credential_cache`), the last things aws does
    to a session before running a command.
    """
    import botocore.session

    # (config-file key, env var names, default, converter). botocore walks the
    # env names in order and only recognizes several of them as a `list`.
    profile_var = (None, list(PROFILE_ENV_VARS), None, None)
    session = botocore.session.Session(
        session_vars={
            "profile": profile_var,
            "retry_mode": ("retry_mode", "AWS_RETRY_MODE", "standard", None),
            "max_attempts": ("max_attempts", "AWS_MAX_ATTEMPTS", 3, int),
        }
    )
    if args.profile:
        session.set_config_variable("profile", args.profile)
    _pin_regional_s3_endpoint(session)
    session.set_default_client_config(_default_client_config(args))
    _check_every_client(session)
    _reject_unknown_cli_formats(session, args)
    _inject_credential_cache(session)
    return session


# The values aws's `cli_timestamp_format` and `cli_binary_format`
# customizations accept; the comparison is exact and case-sensitive.
_TIMESTAMP_FORMATS = ("wire", "iso8601")
_BINARY_FORMATS = ("base64", "raw-in-base64-out")


class UnknownTimestampFormatError(ConfigurationError):
    """An unknown ``cli_timestamp_format`` in the selected profile (rc 253).

    aws raises its own ``ConfigurationError`` for it, the one failure on this
    surface its ``ConfigurationErrorHandler`` reports, so the report carries
    that handler's ``Configuration`` code (`cli._configuration_report`).
    """


def _reject_unknown_cli_formats(session: BotocoreSession, args: argparse.Namespace) -> None:
    """Stop on a ``cli_timestamp_format`` / ``cli_binary_format`` aws refuses.

    aws checks both settings in the selected profile when its session starts,
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


def _check_every_client(session: BotocoreSession) -> None:
    """Hold every client the session builds to aws's empty CA bundle check.

    aws's bundled botocore refuses, in every ``create_client``, a CA bundle that
    resolves to an empty or whitespace-only value (`_reject_empty_ca_bundle`).
    `_create_client` supplies it for the clients this module builds, but the
    SDK builds others from the same session - the credential chain's STS, SSO
    and sso-oidc clients, the CRT engine's request serializer - and those took
    an empty ``AWS_CA_BUNDLE`` beside a valid ``--ca-bundle`` as "verification
    off", fetching credentials over unverified TLS where aws stops (measured:
    rc 255 for ``ls``, a failed item for ``cp``, rc 0 here). So the session's
    own ``create_client`` runs the check on what it built, resolving the
    trust source the way botocore does when the caller names none: the
    session's ``ca_bundle``, then ``REQUESTS_CA_BUNDLE``. It is installed
    before the credential chain is built: its SSO provider keeps the method
    it was handed then. Each client it returns carries the library's
    `CA_BUNDLE_ENVIRONMENT_VETTED` mark, so the library's own guess at the
    environment (made for a client a caller hands it) stands down: the check
    here already ran where aws runs it, and the guess would refuse runs aws
    completes.
    """
    import inspect

    from boto3_s3.s3storage import CA_BUNDLE_ENVIRONMENT_VETTED

    create_client = session.create_client
    signature = inspect.signature(create_client)

    def checked_create_client(*args: Any, **kwargs: Any) -> Any:
        client = create_client(*args, **kwargs)
        verify = signature.bind(*args, **kwargs).arguments.get("verify")
        if verify is None:
            verify = session.get_config_variable("ca_bundle")
        if verify is None:
            verify = os.environ.get("REQUESTS_CA_BUNDLE")
        _reject_empty_ca_bundle(verify)
        setattr(client, CA_BUNDLE_ENVIRONMENT_VETTED, True)
        return client

    # An instance attribute shadows the method for every caller that looks it
    # up on this session (boto3's `Session.client` included).
    setattr(session, "create_client", checked_create_client)  # noqa: B010


class _RegionalS3Section:
    """The profile's ``[s3]`` section with ``us_east_1_regional_endpoint`` pinned.

    aws v2's bundled botocore dropped that key from its ``[s3]`` table outright
    - us-east-1 is regional there, always - so ``legacy`` from
    ``AWS_S3_US_EAST_1_REGIONAL_ENDPOINT`` or the config key decides nothing on
    that side. The installed botocore still reads it, and dropping the key
    alone would not do, because its absence selects the legacy global endpoint
    here (``_should_force_s3_global`` reads a missing key as ``legacy``), so
    the value is pinned to ``regional`` instead.
    """

    def __init__(self, section_provider: Any) -> None:
        self._section_provider = section_provider

    def provide(self) -> Any:
        section = self._section_provider.provide()
        pinned: dict[str, Any] = dict(cast("dict[str, Any]", section or {}))
        pinned["us_east_1_regional_endpoint"] = "regional"
        return pinned

    def set_default_provider(self, key: str, default_provider: Any) -> None:
        """Pass botocore's smart-defaults write through to the wrapped provider.

        A ``defaults_mode`` other than ``legacy`` - the config key or
        ``AWS_DEFAULTS_MODE`` - sends botocore's smart-defaults machinery at
        the ``[s3]`` section provider with
        ``set_default_provider('us_east_1_regional_endpoint', ...)``, on a
        deepcopy of whatever the config store holds. A wrapper answering only
        ``provide()`` therefore failed *every* command at rc 255 with
        ``'_RegionalS3Section' object has no attribute 'set_default_provider'``
        where aws - whose bundled botocore has no defaults modes at all -
        simply ran (measured for every valid mode, config key and env var
        alike). Delegating leaves botocore's own bookkeeping intact; `provide`
        pins the value whatever the write says.
        """
        self._section_provider.set_default_provider(key, default_provider)


def _pin_regional_s3_endpoint(session: BotocoreSession) -> None:
    """Wrap the session's ``[s3]`` section provider in `_RegionalS3Section`.

    The session's config store is the one place every client reads the section
    from - the S3 client, ``mv``'s s3control / sts resolver clients, and the
    clients the credential providers build for themselves - so pinning it here
    covers all of them at once, where a ``Config(s3=...)`` covers only what
    this module passes it.
    """
    config_store = session.get_component("config_store")
    section_provider = config_store.get_config_provider("s3")
    config_store.set_config_provider("s3", _RegionalS3Section(section_provider))


def _default_client_config(args: argparse.Namespace) -> Any:
    """The session default client config aws's startup handlers build.

    aws's globalargs resolves ``--cli-read-timeout`` / ``--cli-connect-timeout``
    into the session's default client config (``_resolve_timeout`` ->
    ``_update_default_client_config``), defaulting both to botocore's 60s, so
    every client created from that session inherits them - the credential
    chain's own STS / SSO clients included. Without that, a stalled STS ran to
    botocore's default here while aws gave up on the flag's budget (measured:
    ``--cli-read-timeout 2`` against an STS stalling 8s is rc 255 and a read
    timeout on aws, and was rc 254 with the server's own error here).

    ``--no-sign-request`` lands in the very same config, from aws's
    ``no_sign_request`` handler, and that placement is load-bearing twice
    over. It reaches the clients botocore builds for itself, so an unsigned
    run that still resolves credentials sends an *unsigned* ``AssumeRole``
    (measured: with a working assume-role profile,
    ``cp --no-sign-request --sse aws:kms`` has aws call STS unsigned and then
    sign the upload with what it got back). And being the session *default*
    is what lets a per-client ``signature_version`` beat it, which is how
    ``--sse aws:kms`` restores signing (`_signature_version_pin`).

    The clients this module builds pass the same two timeouts in their own
    ``Config`` as well, which botocore merges on top of this one - the same
    number either way. Coercing them here is also what stops a run on a bad
    value (255) before anything is sent.
    """
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.endpoint import DEFAULT_TIMEOUT

    timeouts: dict[str, Any] = {
        "connect_timeout": DEFAULT_TIMEOUT,
        "read_timeout": DEFAULT_TIMEOUT,
        **_cli_timeouts(args),
    }
    overrides: dict[str, Any] = {}
    if args.no_sign_request:
        overrides["signature_version"] = UNSIGNED
    return Config(**timeouts, **overrides)


def _credential_cache_dir() -> str:
    """The directory aws caches assumed-role / SSO credentials in.

    Literally aws's own ``~/.aws/cli/cache`` (its ``CACHE_DIR``), so the two
    tools share one cache: credentials fetched under either are reused by the
    other for as long as they are valid. aws freezes the expansion at import
    time; expanding per call is the same answer for a process whose ``HOME``
    does not move, and it keeps the directory testable.
    """
    return os.path.expanduser(os.path.join("~", ".aws", "cli", "cache"))


def _inject_credential_cache(session: BotocoreSession) -> None:
    """Persist assumed-role / web-identity / SSO credentials across runs, as aws does.

    botocore caches the temporary credentials these three providers fetch in a
    plain dict, which dies with the process: every invocation would re-call
    ``AssumeRole``, and an ``mfa_serial`` profile would prompt for a code
    every time - fatal (rc 255 on the ``getpass`` EOF) in any non-interactive
    run. aws's two ``session-initialized`` handlers replace that dict with a
    ``JSONFileCache`` over `_credential_cache_dir`; do the same to every session
    this module opens, which is aws's timing too (its event fires after the
    driver has bound ``--profile``, so the provider chain this reaches is built
    for the run's own profile).

    The SSO token the ``sso-session`` provider refreshes is written back to
    ``~/.aws/sso/cache`` the same way - the installed botocore writes it
    through a temporary file and ``os.replace``, so a cache aws can write
    (an unwritable directory over a writable entry) failed here, one aws
    cannot (a read-only entry) succeeded here, and the entry's mode changed
    (measured) - by building that cache from the same class.
    """
    from botocore.tokens import SSOTokenProvider

    cache = _credential_cache_class()
    cache_dir = _credential_cache_dir()
    # The SSO provider builds its token cache itself, from this class
    # attribute, when the chain is built - and writes it back on every
    # refresh of an `sso-session` token. A CLI process opens no session that
    # should write it any other way.
    SSOTokenProvider.DEFAULT_CACHE_CLS = cache
    chain = session.get_component("credential_provider")
    chain.get_provider("assume-role").cache = cache(cache_dir)
    chain.get_provider("assume-role-with-web-identity").cache = cache(cache_dir)
    chain.get_provider("sso").cache = cache(cache_dir)


def _serialize_cache_entry(value: Any) -> Any:
    """Render for `json.dumps` what it cannot render itself: a ``datetime``.

    aws never reaches this case - its ``cli_timestamp_format`` handler makes
    the response parser return timestamps as ISO-8601 *strings*, so a
    credential's ``Expiration`` is already text by the time the cache stores it
    and lands in the file verbatim (``2026-08-13T10:23:54+09:00``). This CLI
    parses timestamps to ``datetime`` for speed, and botocore's own default
    renders one with ``strftime('%Y-%m-%dT%H:%M:%S%Z')``, whose ``%Z`` for an
    offset-carrying ``datetime.timezone`` is the literal ``UTC+09:00`` - which
    dateutil reads back with POSIX's inverted sign, leaving the entry looking
    valid for hours after it expired (for this CLI and for any aws sharing the
    directory). ``isoformat()`` is the text aws stores, byte for byte.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@cache
def _credential_cache_class() -> type[Any]:
    """botocore's ``JSONFileCache`` with aws's write, built on first use.

    The installed botocore writes an entry through ``tempfile.mkstemp`` +
    ``os.replace``; the botocore aws ships opens the *final* path directly
    (``O_WRONLY | O_CREAT``, mode 0600, then truncate). The difference is only
    visible when the write fails, which is exactly when it is read: an
    unwritable ``~/.aws/cli/cache`` makes aws report the cache entry's own
    name, stable across runs, where this CLI reported a random
    ``tmpXXXXXXXX.tmp`` (rc 255 on both).

    The SSO token cache (``~/.aws/sso/cache``) is one of these too: botocore
    builds it inside the SSO provider with its own serializer, which this
    class takes as ``dumps_func`` the way botocore's does.

    The class is built lazily because botocore is imported lazily, and cached
    so every cache this CLI installs is of one type.
    """
    # botocore.credentials re-exports this very class; `utils` is where it is
    # defined, and the only spelling botocore-stubs declares.
    from botocore.utils import JSONFileCache

    class CliCredentialCache(JSONFileCache):
        def __init__(
            self, working_dir: str, dumps_func: Callable[[Any], str] | None = None
        ) -> None:
            super().__init__(working_dir, dumps_func=dumps_func)
            # The base keeps its own copies under private names botocore-stubs
            # does not declare, so hold them here rather than reach for them.
            self._dir = working_dir
            self._dump = dumps_func

        def __setitem__(self, cache_key: str, value: Any) -> None:
            full_key = os.path.join(self._dir, cache_key + ".json")
            try:
                if self._dump is not None:
                    file_content = self._dump(value)
                else:
                    file_content = json.dumps(value, default=_serialize_cache_entry)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Value cannot be cached, must be JSON serializable: {value}"
                ) from exc
            if not os.path.isdir(self._dir):
                os.makedirs(self._dir, exist_ok=True)
            with os.fdopen(os.open(full_key, os.O_WRONLY | os.O_CREAT, 0o600), "w") as entry:
                entry.truncate()
                entry.write(file_content)

    return CliCredentialCache


def validate_endpoint_url(args: argparse.Namespace) -> None:
    """Reject a schemeless ``--endpoint-url`` (rc 252), as aws-cli does.

    Called by the client builders before the client is created, where
    botocore would otherwise raise a bare ``ValueError``.
    """
    endpoint_url: str | None = args.endpoint_url
    if endpoint_url is not None and not urlparse(endpoint_url).scheme:
        raise ValidationError(
            f'Bad value for --endpoint-url "{endpoint_url}": scheme is '
            "missing.  Must be of the form http://<hostname>/ or https://<hostname>/"
        )


@contextlib.contextmanager
def _construction_errors() -> Generator[None, None, None]:
    """Translate botocore's construction-time errors into the library taxonomy.

    Opening a session and building a client can raise raw botocore errors
    (``ProfileNotFound`` for a bad ``--profile``, credential or region
    resolution failures); left raw they would fall to the dispatcher's
    generic chain, losing aws's 253 / 255 split (design/cli.md section 6).
    aws has dedicated handlers for ``NoCredentialsError`` / ``NoRegionError``
    (-> `ConfigurationError`, 253); every other botocore error
    (``ProfileNotFound``, ``PartialCredentialsError``, ...) falls to its
    ``GeneralExceptionHandler`` (-> 255) - `InvalidConfigError`, which
    exit_code_for maps to 255, not 253. The library's own errors pass
    through as they are.
    """
    from botocore.exceptions import BotoCoreError, NoCredentialsError, NoRegionError

    try:
        yield
    except (NoCredentialsError, NoRegionError) as exc:
        raise ConfigurationError(str(exc)) from exc
    except BotoCoreError as exc:
        raise InvalidConfigError(str(exc)) from exc


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
    import boto3

    from boto3_s3 import fast_parse_timestamp

    with _construction_errors():
        botocore_session = _open_botocore_session(args)
        botocore_session.get_component("response_parser_factory").set_parser_defaults(
            timestamp_parser=fast_parse_timestamp
        )
        session = boto3.Session(botocore_session=botocore_session)
    return session


def build_s3(args: argparse.Namespace) -> S3:
    """Build the command's `S3`, binding client and config reads to one session.

    The region chain is walked **once** here and threaded into every client
    this `S3` hands out. It is the invocation's most expensive resolution: its
    last link is the EC2 IMDS probe, which on a host with no region configured
    and no metadata service answering costs seconds - and the chain's answer
    cannot change within one invocation, since the env, the bound session's
    config and IMDS are all fixed by then. `_bind_region` puts it on the
    session so the clients *botocore* builds resolve it too.

    A run on the CRT engine needs that answer: aws hands its region chain's
    answer to the CRT client, and with none the run stops before it starts,
    `--dryrun` included, where botocore would give the client the
    ``aws-global`` pseudo-region and the CRT would sign every request for it.
    The check sits at the engine's construction slot
    (`S3.materialize_crt_engine`, which every transfer command and ``rm``
    call there) and raises botocore's own missing-region error, kept as the
    cause so the report is aws's ``NoRegion`` one, rc 253.
    """
    from boto3_s3 import S3

    session = build_session(args)
    botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
    region = _resolve_region(args.region, botocore_session)
    _bind_region(botocore_session, args, region)

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
    # (aws dies immediately); the library default keeps waiting.
    # endpoint_url: build_client already applies it to every client this S3
    # hands out (the client() override), so the S3-level copy only feeds the
    # CRT lane's explicit-endpoint pin (design/crt.md) - without it, an
    # --endpoint-url under an AWS domain (a VPC interface endpoint) would be
    # dropped by the host heuristic and the CRT would re-resolve to public S3.
    # crt_allow_lockless=True: aws documents an explicit 'crt' preference as
    # the way to make concurrently running processes use the CRT client, and
    # its factory builds the CRT client whether or not it got the
    # cross-process lock (a lock it does get still marks the slot for 'auto'
    # runs). An 'auto' preference respects the lock on both tools
    # (design/crt.md section 6).
    # crt_sign_requests: aws-cli's CRT factory attaches a credentials provider
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
    # filename_encoding: the encoding aws reads and writes local names with
    # in this environment, whatever Python's own choice (textencoding).
    return CliS3(
        session=session,
        endpoint_url=args.endpoint_url,
        reusable_after_interrupt=False,
        crt_allow_lockless=True,
        crt_sign_requests=not args.no_sign_request,
        filename_encoding=textencoding.filename_encoding(),
    )


def _resolve_region(explicit: str | None, session: BotocoreSession) -> str | None:
    """The region to build a client in: the *explicit* value, else the chain.

    The *explicit* value is ``--region`` - and it wins
    whenever it was supplied at all, the empty string included. aws's s3
    commands hand ``parsed_globals.region`` straight to ``create_client``, so
    ``--region ""`` reaches the S3 client as itself and fails construction
    there on both tools (rc 255). What the
    *session* is bound to is a different question with a different answer for
    that one value - see `_bind_region`.
    """
    if explicit is not None:
        return explicit
    return _region_chain(session)


def _region_chain(session: BotocoreSession) -> str | None:
    """aws-cli's region chain below the flag.

    Mirrors the tail of aws-cli's ``_construct_cli_region_chain``:
    ``AWS_REGION`` env > ``AWS_DEFAULT_REGION`` env > the profile's config-file
    ``region`` > the EC2 IMDS region. Stock botocore never adopted ``AWS_REGION``
    (its region env is ``AWS_DEFAULT_REGION`` alone) and reserves its
    ``IMDSRegionProvider`` for smart-defaults, so a bare client would resolve a
    *different* region whenever ``AWS_REGION`` is the only source, or on an EC2
    host with no region configured. The env vars are present-wins, an empty value
    included (``AWS_REGION=`` -> ``""``, which aws signs with and this CLI walked
    past). Only the CLI corrects this; the library
    (``S3.client``'s ``boto3.client`` fallback) keeps stock botocore order on
    purpose - the same library=boto3 / CLI=aws split as the profile chain.

    A ``BadIMDSRequestError`` reads as no region. aws-cli carries its own copy
    of botocore's region fetcher for exactly one added catch: botocore's
    swallows only the retries-exceeded failure, so a metadata service that
    rejects the token request outright - anything answering at the IMDS address
    that is not EC2's service - escapes as that error and fails the whole
    invocation (rc 255) where aws logs it and walks on with an unresolved
    region.
    """
    # Import the providers only when region resolution needs them.
    from botocore.configprovider import (
        ChainProvider,
        EnvironmentProvider,
        ScopedConfigProvider,
    )
    from botocore.utils import BadIMDSRequestError, IMDSRegionProvider

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
    try:
        return ChainProvider(providers=providers).provide()
    except BadIMDSRequestError:
        # The IMDS probe is the chain's last link, so its failure and an
        # exhausted chain are the same answer.
        return None


def _bind_region(
    botocore_session: BotocoreSession, args: argparse.Namespace, resolved: str | None
) -> None:
    """Bind onto the session the region aws's own chain answers.

    aws installs its region chain as the session's own ``region`` config
    provider and binds a truthy ``--region`` at the head of it
    (``_update_config_chain`` / ``_handle_top_level_args``), so every client
    built from that session resolves the chain's answer - including the clients
    *botocore itself* builds, chiefly the STS client the assume-role and
    web-identity providers create for their own use. Passing the answer to each
    client as ``region_name`` never reached those, so an assume-role profile
    whose only region source was ``--region`` or ``AWS_REGION`` signed
    ``AssumeRole`` against the global STS endpoint - a different partition
    under ``cn-*`` / ``us-gov-*`` - or failed outright with ``NoRegion``
    (measured: rc 253 where aws exits 0).

    What binds is not what the client is built with. Only a *truthy*
    ``--region`` heads aws's chain, so a falsy ``--region ""`` leaves the rest
    of the chain to answer for the session while the empty string still reaches
    the S3 client as its own ``region_name`` (`_resolve_region`): measured,
    ``--region ""`` with ``AWS_REGION=eu-west-1`` has aws sign ``AssumeRole``
    in eu-west-1 where this CLI exited 253 with ``NoRegion`` and called STS not
    at all. An ``AWS_REGION=`` that is present but empty is the chain's own
    answer and binds as itself - aws signs with an empty region scope, where
    this CLI walked on to ``AWS_DEFAULT_REGION`` and the profile and signed in
    a region aws never used.

    A client built for another region binds nothing - only the builder that
    opened the session does, so ``--source-region``'s client leaves the
    credential chain in the run's own region, as aws's source client does.

    *resolved* is the answer `_resolve_region` already computed for
    ``args.region``, so the chain - whose last link is the IMDS probe - is
    walked once per invocation. Only the falsy-flag corner asks it again, no
    client's region being able to carry that answer. Nothing is bound when the
    chain resolved nothing, which leaves the session on botocore's identical
    answer.
    """
    if args.region:
        region = args.region
    elif args.region is None:
        region = resolved
    else:
        region = _region_chain(botocore_session)
    if region is not None:
        botocore_session.set_config_variable("region", region)


def _resolve_verify(args: argparse.Namespace, botocore_session: BotocoreSession) -> bool | str:
    """The TLS trust source, resolved to an explicit value for every CLI client.

    ``--no-verify-ssl`` -> ``False``, ``--ca-bundle`` -> that path, then the
    two fallbacks botocore itself would walk if the value were left ``None``:
    the ``ca_bundle`` config variable (``AWS_CA_BUNDLE`` env or the profile's
    ``ca_bundle`` key, read off the *same* session the client is built from, so
    the resolved profile is honored) and the ``REQUESTS_CA_BUNDLE`` env var
    (botocore's ``EndpointCreator._get_verify_value``, present-wins so an empty
    value stops the chain as itself rather than falling through). An empty or
    whitespace-only value that reaches the client is then rejected at build time
    like aws v2 (`_reject_empty_ca_bundle`), not read as "verification off".
    Nothing set lands on the bundle botocore would have used at request time -
    certifi's, or botocore's own ``cacert.pem`` where certifi is absent, which
    is why this asks ``get_cert_path`` rather than importing certifi.

    Resolving here rather than passing ``None`` is what gives the CRT engine a
    CA file: ``create_s3_crt_client(verify=None)`` means the *platform* trust
    store, so a ``None`` that classic silently resolved to certifi left the two
    engines trusting different roots. aws-cli has the same split in reverse
    (its CRT lane resolves its bundled ``cacert.pem`` explicitly, ``factory.py``
    ``_resolve_verify``, while its classic clients ride the botocore chain);
    resolving once, up front, keeps both of *our* engines on the file classic
    was already using - and keeps every CLI-built client on one value, which
    the CRT singleton's verify compatibility check requires.
    """
    if args.no_verify_ssl:
        return False
    if args.ca_bundle is not None:
        return textencoding.os_path(cast("str", args.ca_bundle))
    configured = botocore_session.get_config_variable("ca_bundle")
    if configured is not None:
        return cast("str", configured)
    requests_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
    if requests_bundle is not None:
        return requests_bundle
    # Present at the botocore floor (docs/compatibility.md); True means "the
    # default bundle" and is exactly what botocore resolves per request.
    from botocore.httpsession import get_cert_path

    return cast("str", get_cert_path(True))


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
    and the resolved TLS ``verify`` setting (`_resolve_verify`) - no endpoint
    override. A caller's ``None`` is left to the session, as there: aws binds
    its region chain's answer into the session at startup, a truthy typed
    ``--region`` at its head, so a ``create_client`` with no ``region_name``
    lands on that answer - which `build_s3` bound, or `_bind_region` binds
    here onto a session this builder opened itself. ``--region ""`` is the
    corner that tells them apart: it never binds, so the source and sts
    clients resolve the rest of the chain while the destination's s3control,
    handed the flag itself, fails on the empty region (measured). aws likewise resolves
    ``--cli-read-timeout`` / ``--cli-connect-timeout`` into the *session*
    default client config at startup, so ``from_session``'s ``create_client``
    inherits them; fold the same timeouts into this client's ``Config``.
    ``--no-sign-request`` and the retry posture arrive through the session
    itself (`_default_client_config`, `_open_botocore_session`), which is
    where aws puts them too, so this client is created through the shared
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
        # aws's startup handlers thread the timeouts through the session
        # default config that from_session's create_client inherits
        # (build_client mirrors the same); the UNSIGNED signature arrives
        # through that config too, so this client never names one - which is
        # what keeps `mv`'s resolver clients anonymous under
        # --no-sign-request even when --sse aws:kms signs the transfer.
        client = _create_client(
            session,
            service,
            region_name=region,
            verify=_resolve_verify(args, botocore_session),
            config=Config(**_cli_timeouts(args)),
        )
    return client


def _cli_timeouts(args: argparse.Namespace) -> dict[str, Any]:
    """The ``Config`` fields of the timeout flags given: aws-cli's ``_resolve_timeout``.

    ``--cli-read-timeout`` / ``--cli-connect-timeout`` arrive as raw strings
    (see globalargs.add_common_arguments) and are coerced aws-style
    (`_coerce_cli_timeout`), read before connect - the order aws registers its
    two handlers in. A flag not given names no field, leaving the session's
    default (`_default_client_config`).
    """
    timeouts: dict[str, Any] = {}
    if args.cli_read_timeout is not None:
        timeouts["read_timeout"] = _coerce_cli_timeout(args.cli_read_timeout)
    if args.cli_connect_timeout is not None:
        timeouts["connect_timeout"] = _coerce_cli_timeout(args.cli_connect_timeout)
    return timeouts


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
    `CreateSession` - unusable against the directory bucket, where aws
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
    client). Pin SigV4 so every command - visibly, presign's URLs - matches aws
    v2. (The other half of that divergence, us-east-1 resolving to the legacy
    global endpoint, is pinned on the session instead:
    `_pin_regional_s3_endpoint`.) The pinned name is the generic ``v4``, the
    one aws's endpoint resolution hands its requests: botocore keeps a
    requested name as the request's ``auth_type``, and its own
    ``choose-signer`` handler turns ``v4`` into the S3 signer (``s3v4``) and
    answers there, while an ``s3v4`` there is passed by and left to whatever
    handler comes next. Every ``choose-signer`` handler also sees ``v4`` /
    ``v4-query``, as on aws.

    None when the command targets an MRAP ARN, an S3 Outposts access point
    (ARN or ``--op-s3`` alias), or an S3 Express directory bucket
    (`_includes_endpoint_auth_path`): an explicit signature_version suppresses
    botocore's auth-scheme resolution, and those endpoints must resolve to
    asymmetric SigV4a (the MRAP and Outposts shapes) / ``sigv4-s3express``
    (with `CreateSession` credentials) - a pinned ``s3v4`` (or ``v4``) matches
    all those scheme names up to the first dash and silently signs a plain
    SigV4 request instead. aws v2's bundled botocore pins only the symmetric
    families (`_pin_python_sigv4_signers`) and leaves the resolutions alive.
    With awscrt absent, a SigV4a target (MRAP or Outposts) surfaces botocore's
    own MissingDependencyException (-> ConfigurationError, 253) instead of a
    silently mis-signed SigV4 request.

    None for an anonymous run (``--no-sign-request``) too: aws leaves UNSIGNED
    where its startup handler put it - the session default client config
    (`_default_client_config`) - and that is what must reach the client; a
    per-client ``signature_version``, this pin included, beats it in
    botocore's merge.

    ``s3v4`` under ``--sse aws:kms`` (`_requests_kms_signing`), the exception
    to both stand-downs: it is the one case for which aws names a per-client
    ``signature_version`` - its ``ClientFactory.create_client`` passes
    ``Config(signature_version='s3v4')`` whatever the target. botocore merges
    the per-client config on top of the session's, so that one case signs,
    resolves credentials, and reaches the credential chain, while every other
    unsigned run stays anonymous. Measured without credentials:
    ``cp``/``mv``/``sync``/``cp s3://.. s3://..`` with
    ``--no-sign-request --sse aws:kms`` are rc 1 ``Unable to locate
    credentials`` and send nothing under aws (this CLI uploaded anonymously,
    rc 0), and a broken ``source_profile`` under the same flags is aws's
    rc 255 profile report; ``--sse AES256`` and ``--sse-c AES256`` upload
    anonymously on both. An MRAP, Outposts or S3 Express target signs plain
    SigV4 there too, with or without ``--no-sign-request`` (measured offline
    on the pinned aws: ``AWS4-HMAC-SHA256`` with the client's region in the
    scope, where the resolution would have chosen SigV4a /
    ``sigv4-s3express``). That pin keeps aws's own ``s3v4``, and with it the
    ``auth_type`` botocore's handler passes by.

    This governs the *botocore* client alone, which is aws's own split: its
    CRT factory reads ``--no-sign-request`` directly and never the client, so
    the same KMS run transfers anonymously on the CRT lane. `build_s3`
    declares that separately (``crt_sign_requests``).
    """
    if _requests_kms_signing(args):
        return "s3v4"
    if _includes_endpoint_auth_path(args) or args.no_sign_request:
        return None
    return "v4"


def _requests_kms_signing(args: argparse.Namespace) -> bool:
    """Whether aws would hand this run's S3 clients ``signature_version='s3v4'``.

    aws's ``ClientFactory.create_client`` does so for ``--sse aws:kms`` and
    nothing else - aws's own exact string, so ``aws:kms:dsse`` does not - and
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
    ``Config``; ``--no-verify-ssl`` and ``--ca-bundle`` head
    the ``verify`` chain (`_resolve_verify`, always resolved to an explicit
    value so both transfer engines trust the same roots). Every client also
    carries aws's S3 error-message rewriter (`s3errormsg`). The client is
    handed to the library through ``S3Storage`` - the library never rebuilds
    connection settings itself.

    ``--no-sign-request``, the retry posture and the ``[s3]`` regional pin are
    not client kwargs at all: they belong to the session
    (`_default_client_config`, `_open_botocore_session`), which is what puts
    the clients botocore builds for itself on them too.
    """
    # The SDK is imported here, not at module load: the informational exits
    # (`--version`, the help token) return before this normal-dispatch path.
    from botocore.config import Config

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
            verify=_resolve_verify(args, botocore_session),
            config=Config(**overrides),
        )
        s3errormsg.register(client)
        checksumdefault.register(client)
    return client


def _create_client(session: Boto3Session, service: str, **kwargs: Any) -> Any:
    """``session.client`` with the CONNECT pin and the CA bundle check around it.

    Every client this CLI hands out is created here, which is what makes this
    the place the CONNECT pin goes on (`proxytunnel`): an HTTPS proxy sees the
    tunnel request the *host interpreter* writes, and only 3.12 and later write
    aws's.
    """
    from boto3_s3_cli import proxytunnel

    proxytunnel.pin_connect_request()
    client = session.client(service, **kwargs)  # pyright: ignore[reportUnknownMemberType]
    _reject_empty_ca_bundle(kwargs.get("verify"))
    return client


def _reject_empty_ca_bundle(verify: Any) -> None:
    """Reject an empty or whitespace-only CA bundle, like aws v2 (>= 2.36.6).

    aws refuses an empty or whitespace-only ``ca_bundle`` / ``AWS_CA_BUNDLE`` /
    ``REQUESTS_CA_BUNDLE`` / ``--ca-bundle`` value outright at client
    construction (its bundled botocore's ``EndpointCreator._validate_verify_value``)
    rather than silently turning TLS verification off. The installed botocore
    adopted the same check in 1.43.54, but the supported floor is older
    (docs/compatibility.md), so a host botocore below it would still disable
    verification. Re-raising botocore's own ``InvalidConfigError`` here holds
    the rejection across the whole support range: on a
    botocore that already rejects, ``session.client`` raised the identical
    exception first and this is unreached; on an older one, this supplies it.

    Every client a CLI-opened session builds is held to it
    (`_check_every_client`), the SDK's own included; `_create_client` checks
    again, for a client built from a session this module did not open.
    ``build_client`` / ``build_service_client`` catch it in their
    `_construction_errors` scope and refine it to the library
    ``InvalidConfigError`` (rc 255), exactly as they would botocore's own.
    ``False`` (``--no-verify-ssl``) and a real path pass through untouched.
    """
    from botocore.exceptions import InvalidConfigError as BotocoreInvalidConfigError

    if isinstance(verify, str) and not verify.strip():
        raise BotocoreInvalidConfigError(
            error_msg=(
                "Invalid CA bundle: the configured value (ca_bundle, "
                "AWS_CA_BUNDLE, REQUESTS_CA_BUNDLE, or verify) resolved to an "
                "empty or whitespace-only string. Provide a valid path to a "
                "CA bundle file."
            )
        )
