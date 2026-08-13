"""Build boto3 clients from the parsed connection / auth globals.

The execution half of the global-option surface ``globalargs``
registers: ``build_client`` turns the connection / auth values into the
boto3 S3 client the library consumes (``design/aws-cli-option-handling.md``
section 5), and ``build_service_client`` builds the non-S3 clients ``mv``'s
path validation needs (section 5.8). Everything here reaches the AWS SDK.
"""

from __future__ import annotations

import argparse
import enum
import json
import os
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urlparse

# These exception names do not themselves import the AWS SDK.
from boto3_s3 import ConfigurationError, InvalidConfigError, InvalidValueError, ValidationError
from boto3_s3_cli import configfiles, s3errormsg
from boto3_s3_cli.globalargs import PROFILE_ENV_VARS

if TYPE_CHECKING:
    from boto3.session import Session as Boto3Session
    from botocore.session import Session as BotocoreSession
    from mypy_boto3_s3 import S3Client

    from boto3_s3 import S3


class _RegionUnresolved(enum.Enum):
    """The type behind `_UNRESOLVED` (the sentinel shape `boto3_s3.crtsupport`
    uses for its own region posture: a single-member enum, so a type checker
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


def resolve_profile(args: argparse.Namespace) -> str | None:
    """The profile the invocation's config reads apply to (aws-cli precedence).

    ``--profile`` (truthy only) > ``configfiles.env_profile`` > ``None``
    (the ``default`` profile). The ``--profile`` guard is aws-cli's truthy test
    (its ``_handle_top_level_args`` binds the profile only ``if getattr(args,
    'profile', False)``), so an empty ``--profile ""`` is ignored and the env
    chain wins - matching aws, which then reaches the server (rc 254) rather
    than raising ProfileNotFound. The env chain is instead present-wins, an
    empty value included (``AWS_PROFILE=`` selects the empty profile ->
    ProfileNotFound, like aws), so `aws s3` parity holds even when both env
    vars are set. Only the CLI layer corrects this; the library (boto3.client
    fallback) stays boto3/botocore-faithful and keeps stock order on purpose.

    This answers "which profile's keys do I read" for the callers that parse
    the config files themselves (the startup gates, the auto-prompt resolver).
    The session `_open_botocore_session` opens is *not* built from it - see
    there for why only the flag may be bound.
    """
    if args.profile:
        return args.profile
    return configfiles.env_profile()


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

    The session also pins ``api_versions`` empty. aws-cli v2 removed that
    setting in 2.0.0 (aws/aws-cli#4751) and its bundled botocore has no such
    config variable, so aws ignores the key outright; the installed botocore
    still reads it in ``create_client``, where a version other than the model's
    fails *every* command with ``Unable to load data for:
    s3/<version>/service-2`` - a config left over from aws-cli v1 would take
    the whole CLI down. An empty mapping as the session instance variable heads
    the config chain, so no env var or config file can put one back. The
    library's own ``S3()`` keeps reading it, staying boto3-faithful.

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

    Finally the temporary-credential providers get their on-disk cache, the
    last thing aws does to a session before running a command
    (`_inject_credential_cache`).
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
    session.set_config_variable("api_versions", {})
    _pin_regional_s3_endpoint(session)
    _drop_tls_key_log()
    session.set_default_client_config(_default_client_config(args))
    _inject_credential_cache(session)
    return session


class _RegionalS3Section:
    """The profile's ``[s3]`` section with ``us_east_1_regional_endpoint`` pinned.

    aws v2's bundled botocore dropped that key from its ``[s3]`` table outright
    - us-east-1 is regional there, always - so neither
    ``AWS_S3_US_EAST_1_REGIONAL_ENDPOINT`` nor the config key decides anything
    on that side, an invalid or empty value included. The installed botocore
    still reads *and validates* it, for every client that resolves an endpoint
    ruleset: dropping the key alone would not do, because its absence selects
    the legacy global endpoint here (``_should_force_s3_global`` reads a
    missing key as ``legacy``), so the value is pinned to ``regional`` instead.

    A value botocore's own section provider hands over as something other than
    a mapping (a degenerate ``s3 =`` line arrives as ``""``) is passed through
    untouched: that shape is botocore's to report, in the place and with the
    wording it reports it (``'str' object has no attribute 'get'``, which aws
    produces from the same read).
    """

    def __init__(self, section_provider: Any) -> None:
        self._section_provider = section_provider

    def provide(self) -> Any:
        section = self._section_provider.provide()
        if section is not None and not isinstance(section, dict):
            return section
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
        alike). Delegating leaves botocore's own bookkeeping intact; the value
        it writes for that key is ``regional``, which is what `provide` pins
        anyway, so the pin still decides. A wrapped provider that has no such
        method fails exactly as the unwrapped one would, with the wording
        botocore itself would produce.
        """
        self._section_provider.set_default_provider(key, default_provider)


def _pin_regional_s3_endpoint(session: BotocoreSession) -> None:
    """Wrap the session's ``[s3]`` section provider in `_RegionalS3Section`.

    The session's config store is the one place every client reads the section
    from - the S3 client, ``mv``'s s3control / sts resolver clients, and the
    clients the credential providers build for themselves - so pinning it here
    covers all of them at once, where a ``Config(s3=...)`` covers only what
    this module passes it (measured: with
    ``AWS_S3_US_EAST_1_REGIONAL_ENDPOINT=zzz``, ``mv
    --validate-same-s3-paths`` exited 255 on the validator's client where aws
    ran the move).

    The section provider's own entry for the key goes first, so the env var
    and the top-level ``s3_us_east_1_regional_endpoint`` key are not read at
    all - what a table without the key does. It shows through on a degenerate
    ``s3 =`` line, where botocore writes an override into what is still a
    string: the key aws does not have must not be the one that fails there.
    Botocore's own name for that table is private, so a botocore that renames
    it simply keeps reading the key, and the pin above still decides the
    value.
    """
    config_store = session.get_component("config_store")
    section_provider = config_store.get_config_provider("s3")
    section_overrides = getattr(section_provider, "_override_providers", None)
    if isinstance(section_overrides, dict):
        cast("dict[str, Any]", section_overrides).pop("us_east_1_regional_endpoint", None)
    config_store.set_config_provider("s3", _RegionalS3Section(section_provider))


def _drop_tls_key_log() -> None:
    """Take ``SSLKEYLOGFILE`` out of the environment, as the aws build does.

    botocore hands the variable to the SSL context it builds for every client,
    under a ``sys.flags.ignore_environment`` guard. aws ships a frozen
    interpreter that runs isolated, so the guard is always false there and the
    variable decides nothing: with a writable path aws writes no key log, and
    with an unopenable one it runs normally. Here the same botocore code runs
    on the host interpreter, so the path is opened while the client's HTTP
    session builds - writing TLS session keys aws would not write, and failing
    every client-building command (``presign`` included, rc 255) when the path
    cannot be opened. Dropping the variable as the session opens reproduces
    aws's outcome; an external ``!`` alias opens no session, so the child
    process this CLI launches still receives it, exactly as under aws.
    """
    os.environ.pop("SSLKEYLOGFILE", None)


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
    ``--sse aws:kms`` restores signing (`_sends_unsigned_requests`).

    The clients this module builds pass the same two timeouts in their own
    ``Config`` as well, which botocore merges on top of this one - the same
    number either way. The read timeout is coerced first, aws's registration
    order (`resolve_cli_timeouts`), so a run with both values broken reports
    the same one aws reports.
    """
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.endpoint import DEFAULT_TIMEOUT

    read = (
        DEFAULT_TIMEOUT
        if args.cli_read_timeout is None
        else _coerce_cli_timeout(args.cli_read_timeout)
    )
    connect = (
        DEFAULT_TIMEOUT
        if args.cli_connect_timeout is None
        else _coerce_cli_timeout(args.cli_connect_timeout)
    )
    overrides: dict[str, Any] = {}
    if args.no_sign_request:
        overrides["signature_version"] = UNSIGNED
    return Config(connect_timeout=connect, read_timeout=read, **overrides)


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

    A ``ProfileNotFound`` from building the chain is swallowed exactly as aws
    swallows it: the profile is reported by the caller's own config read
    instead, keeping that error's wording and position. The ``sso`` provider is
    fetched separately, under aws's wider ``UnknownCredentialError`` guard for
    a botocore whose chain has no such provider.
    """
    from botocore.exceptions import ProfileNotFound, UnknownCredentialError

    cache = _credential_cache_class()
    cache_dir = _credential_cache_dir()
    try:
        chain = session.get_component("credential_provider")
        chain.get_provider("assume-role").cache = cache(cache_dir)
        chain.get_provider("assume-role-with-web-identity").cache = cache(cache_dir)
    except ProfileNotFound:
        return
    try:
        chain.get_provider("sso").cache = cache(cache_dir)
    except (ProfileNotFound, UnknownCredentialError):
        return


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

    The class is built lazily because botocore is imported lazily, and cached
    so every cache this CLI installs is of one type.
    """
    # botocore.credentials re-exports this very class; `utils` is where it is
    # defined, and the only spelling botocore-stubs declares.
    from botocore.utils import JSONFileCache

    class CliCredentialCache(JSONFileCache):
        def __init__(self, working_dir: str) -> None:
            super().__init__(working_dir)
            # The base keeps its own copy under a private name botocore-stubs
            # does not declare, so hold one here rather than reach for it.
            self._dir = working_dir

        def __setitem__(self, cache_key: str, value: Any) -> None:
            full_key = os.path.join(self._dir, cache_key + ".json")
            try:
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


def _has_url_scheme(value: str) -> bool:
    """Does *value* carry a URL scheme by the rule the shipped aws build applies?

    ``urlsplit`` requires an ASCII letter to lead the scheme only from Python
    3.11 on; 3.10 and 3.9 accept any run of scheme characters, so
    ``192.168.0.9:9000`` - a MinIO / Ceph address typed without ``http://`` -
    parses there with ``192.168.0.9`` *as the scheme* and slips past a plain
    truthiness test, reaching botocore for a different message and rc. Testing
    the parsed scheme's first character adds exactly 3.11's condition (the
    scheme it returns is lower-cased and drawn from ASCII scheme characters, so
    one ``isalpha`` covers it), pinning every supported version to what the
    official aws distribution does - the same pin the argparse corners use
    (``design/cli.md`` section 2).
    """
    return urlparse(value).scheme[:1].isalpha()


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
    if endpoint_url is not None and not _has_url_scheme(endpoint_url):
        raise ValidationError(
            f'Bad value for --endpoint-url "{endpoint_url}": scheme is '
            "missing.  Must be of the form http://<hostname>/ or https://<hostname>/"
        )


def validate_profile(args: argparse.Namespace) -> None:
    """Resolve the session profile the way aws does at startup (rc 255 on failure).

    aws resolves ``--profile`` / the profile env chain in its session before
    any command validation runs, so a bad profile fails during the startup
    config reads (ProfileNotFound -> its general handler, rc 255) ahead of
    every post-parse usage error (252) - while an unresolvable *region* does
    NOT fail here (aws defers it to request time). Mirror the ordering by
    forcing one scoped-config read on a session opened like the run's own.
    """
    from botocore.exceptions import BotoCoreError

    try:
        _open_botocore_session(args).get_scoped_config()
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
    from botocore.exceptions import BotoCoreError

    from boto3_s3 import fast_parse_timestamp

    try:
        botocore_session = _open_botocore_session(args)
        botocore_session.get_scoped_config()
        botocore_session.get_component("response_parser_factory").set_parser_defaults(
            timestamp_parser=fast_parse_timestamp
        )
        return boto3.Session(botocore_session=botocore_session)
    except BotoCoreError as exc:
        raise InvalidConfigError(str(exc)) from exc


def build_s3(args: argparse.Namespace) -> S3:
    """Build the command's `S3`, binding client and config reads to one session.

    The region chain is walked **once** here and threaded into every client
    this `S3` hands out. It is the invocation's most expensive resolution: its
    last link is the EC2 IMDS probe, which on a host with no region configured
    and no metadata service answering costs seconds - and the chain's answer
    cannot change within one invocation, since the env, the bound session's
    config and IMDS are all fixed by then. The same value is what the CRT
    posture below declares, so client and engine can never disagree on it, and
    `_bind_region` puts it on the session so the clients *botocore* builds
    resolve it too.
    """
    from boto3_s3 import S3

    session = build_session(args)
    botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
    region = _resolve_region(args.region, botocore_session)
    _bind_region(botocore_session, args.region, region)

    class CliS3(S3):
        def client(self) -> S3Client:
            return build_client(args, session=session, region=region)

    # reusable_after_interrupt=False: Ctrl-C is process-fatal in the CLI, so an
    # operation's unwind must not wait for an in-flight listing page pull
    # (aws dies immediately); the library default keeps waiting.
    # endpoint_url: build_client already applies it to every client this S3
    # hands out (the client() override), so the S3-level copy only feeds the
    # CRT lane's explicit-endpoint pin (design/crt.md) - without it, an
    # --endpoint-url under an AWS domain (a VPC interface endpoint) would be
    # dropped by the host heuristic and the CRT would re-resolve to public S3.
    # crt_allow_absent_credentials=True: aws-cli hands its CRT client a
    # credentials delegate built from whatever the session resolved, ``None``
    # included, so a credential-less CRT upload fails inside the delegate
    # rather than falling back to classic. The library defaults to boto3's
    # opposite rule; the aws-faithful entry is this layer's (design/crt.md
    # section 4).
    # crt_allow_lockless=True: aws-cli's factory acquires the cross-process
    # CRT lock best-effort and, under an explicit 'crt' preference, builds the
    # CRT client regardless - so its construction-time failures (a bad CA
    # bundle, an unresolved region) surface under contention too, where the
    # library default silently selects classic and would report success. An
    # 'auto' preference respects the lock on both tools (design/crt.md
    # section 6).
    # crt_region: aws-cli resolves the CRT region from its own region chain
    # (the same `_resolve_region` every client here is built with), which
    # yields None when nothing is configured - where botocore would hand the
    # client the `aws-global` pseudo-region. Declaring the chain's answer is
    # what lets awscrt's own region assertion fire, as it does under aws
    # (design/crt.md section 6).
    return CliS3(
        session=session,
        endpoint_url=args.endpoint_url,
        reusable_after_interrupt=False,
        crt_allow_absent_credentials=True,
        crt_allow_lockless=True,
        crt_region=region,
    )


def _resolve_region(explicit: str | None, session: BotocoreSession) -> str | None:
    """The region to build a client in: the *explicit* value, else the chain.

    The *explicit* value is ``--region``, or the caller's region for
    ``build_service_client`` - and it wins whenever it was supplied at all,
    the empty string included. aws's s3 commands hand ``parsed_globals.region``
    straight to ``create_client``, so ``--region ""`` reaches the S3 client as
    itself and fails construction there on both tools (rc 255). What the
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
    botocore_session: BotocoreSession, explicit: str | None, resolved: str | None
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

    *resolved* is the answer `_resolve_region` already computed for the same
    flag, so the chain - whose last link is the IMDS probe - is walked once per
    invocation. Only the falsy-flag corner asks it again, no client's region
    being able to carry that answer. Nothing is bound when the chain resolved
    nothing, which leaves the session on botocore's identical answer.
    """
    if explicit:
        region = explicit
    elif explicit is None:
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
    value keeps its "verification off" meaning). Nothing set lands on the
    bundle botocore would have used at request time - certifi's, or botocore's
    own ``cacert.pem`` where certifi is absent, which is why this asks
    ``get_cert_path`` rather than importing certifi.

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
        return cast("str", args.ca_bundle)
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
    override. aws binds ``--region`` into the session itself at startup
    (clidriver's ``set_config_variable``), so a ``create_client`` with no
    ``region_name`` still lands in ``--region``; mirror that by falling back
    from the caller's ``None`` to ``args.region`` before the shared
    ``_resolve_region`` chain (``AWS_REGION`` > ``AWS_DEFAULT_REGION`` >
    config > IMDS), and by binding the same answer onto a session this builder
    opened itself (`_bind_region`). aws likewise resolves
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
    # clients regardless of the path shapes) pays the boto3 import.
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, NoCredentialsError, NoRegionError

    # Client construction can raise raw botocore errors (e.g. ProfileNotFound for
    # a bad --profile); translate them so the credential/region 253 split
    # survives - untranslated they would fall to the dispatcher's generic
    # chain (see build_client).
    try:
        if session is None:
            from boto3_s3 import fast_parse_timestamp

            botocore_session = _open_botocore_session(args)
            # Same fast timestamp parsing as build_session: every CLI-built
            # session carries it.
            botocore_session.get_component("response_parser_factory").set_parser_defaults(
                timestamp_parser=fast_parse_timestamp
            )
            session = boto3.Session(botocore_session=botocore_session)
            _bind_region(
                botocore_session, args.region, _resolve_region(args.region, botocore_session)
            )
        else:
            botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        # aws's startup handlers thread the timeouts through the session
        # default config that from_session's create_client inherits
        # (build_client mirrors the same); the UNSIGNED signature arrives
        # through that config too, so this client never names one - which is
        # what keeps `mv`'s resolver clients anonymous under
        # --no-sign-request even when --sse aws:kms signs the transfer.
        service_overrides: dict[str, Any] = {}
        if args.cli_read_timeout is not None:
            service_overrides["read_timeout"] = _coerce_cli_timeout(args.cli_read_timeout)
        if args.cli_connect_timeout is not None:
            service_overrides["connect_timeout"] = _coerce_cli_timeout(args.cli_connect_timeout)
        return _create_client(
            session,
            botocore_session,
            service,
            region_name=_resolve_region(
                region if region is not None else args.region, botocore_session
            ),
            verify=_resolve_verify(args, botocore_session),
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

    True for the shapes that must sign asymmetric SigV4a (an MRAP bucket, an
    S3 Outposts access point in either notation - the ARN or the ``--op-s3``
    alias) and for an S3 Express directory bucket (must sign
    ``sigv4-s3express`` with `CreateSession` credentials) - the shapes an
    explicit `signature_version` would mis-sign, so the s3v4 pin stands down
    for them.
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
    form before the check, which `is_s3express_path` and
    `is_outpost_alias_path` require precisely because a transfer positional
    could be a local file ending in ``--x-s3`` or ``--op-s3``. Without this,
    the s3v4 pin would stay on and the URL would sign plain SigV4 with no
    `CreateSession` - unusable against the directory bucket, where aws
    (resolving the auth scheme off the final Bucket, not the input notation)
    signs ``sigv4-s3express``; an Outposts alias would likewise get a
    symmetric signature its SigV4a endpoint rejects.
    """
    from boto3_s3.pathresolver import (
        is_mrap_path,
        is_outpost_alias_path,
        is_outpost_path,
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
        )
        for value in values
    )


def _sends_unsigned_requests(args: argparse.Namespace) -> bool:
    """Whether this run's S3 client is the anonymous one ``--no-sign-request`` asks for.

    aws leaves UNSIGNED where its startup handler put it - the session default
    client config (`_default_client_config`) - and names a per-client
    ``signature_version`` in exactly one case: ``--sse aws:kms``, for which its
    ``ClientFactory.create_client`` passes ``Config(signature_version='s3v4')``.
    botocore merges the per-client config on top of the session's, so that one
    case signs, resolves credentials, and reaches the credential chain, while
    every other unsigned run stays anonymous. Measured without credentials:
    ``cp``/``mv``/``sync``/``cp s3://.. s3://..`` with
    ``--no-sign-request --sse aws:kms`` are rc 1 ``Unable to locate
    credentials`` and send nothing under aws (this CLI uploaded anonymously,
    rc 0), and a broken ``source_profile`` under the same flags is aws's
    rc 255 profile report; ``--sse AES256`` and ``--sse-c AES256`` upload
    anonymously on both.

    ``--sse`` belongs to the transfer family alone, hence the ``getattr``, and
    the comparison is aws's own exact string - ``aws:kms:dsse`` gets no
    per-client config there either.
    """
    return bool(args.no_sign_request) and getattr(args, "sse", None) != "aws:kms"


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
    # Importing boto3 drags in botocore and s3transfer. The informational exits
    # (`--version`, the help token) return before this normal-dispatch path.
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, NoCredentialsError, NoRegionError

    _pin_python_sigv4_signers()

    # The transfer family already validated this up front (parse-time order);
    # re-checked here for the commands that build their client first (mb/rb)
    # and for direct callers - botocore would otherwise raise a bare
    # ValueError at client creation.
    validate_endpoint_url(args)

    # aws-cli v2's bundled botocore has no S3 SigV2 (hmacv1 "s3"-family
    # signers) left - only the generic query-protocol "v2" - while stock
    # botocore still downgrades *presigned URLs* to SigV2 in regions that
    # accept it (a default us-east-1 client). Pin s3v4 so every command -
    # visibly, presign's URLs - matches aws v2. (The other half of that
    # divergence, us-east-1 resolving to the legacy global endpoint, is pinned
    # on the session instead: `_pin_regional_s3_endpoint`.)
    # The pin stands down when the command targets an MRAP ARN, an S3 Outposts
    # access point (ARN or `--op-s3` alias), or an S3 Express directory
    # bucket: an explicit signature_version suppresses botocore's auth-scheme
    # resolution, and those endpoints must resolve to asymmetric SigV4a (the
    # MRAP and Outposts shapes) / ``sigv4-s3express`` (with `CreateSession`
    # credentials) - a pinned
    # s3v4 matches all those scheme names up to the first dash and silently
    # signs a plain SigV4 request instead. aws v2's bundled botocore pins
    # only the symmetric families (_pin_python_sigv4_signers) and leaves the
    # resolutions alive. With awscrt absent, a SigV4a target (MRAP or
    # Outposts) surfaces botocore's own MissingDependencyException
    # (-> ConfigurationError, 253) instead of a silently mis-signed SigV4
    # request.
    # The pin also stands down for an anonymous run (`_sends_unsigned_requests`),
    # where the UNSIGNED signature waiting in the session default client config
    # is what must reach the client - a per-client signature_version, this pin
    # included, beats it in botocore's merge.
    overrides: dict[str, Any] = {}
    if not _includes_endpoint_auth_path(args) and not _sends_unsigned_requests(args):
        overrides["signature_version"] = "s3v4"
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

            botocore_session = _open_botocore_session(args)
            # Same fast timestamp parsing as build_session: every CLI-built
            # session carries it.
            botocore_session.get_component("response_parser_factory").set_parser_defaults(
                timestamp_parser=fast_parse_timestamp
            )
            session = boto3.Session(botocore_session=botocore_session)
        else:
            botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        client_region = (
            _resolve_region(args.region, botocore_session) if region is _UNRESOLVED else region
        )
        _bind_region(botocore_session, args.region, client_region)
        client = _create_client(
            session,
            botocore_session,
            "s3",
            region_name=client_region,
            endpoint_url=args.endpoint_url,
            verify=_resolve_verify(args, botocore_session),
            config=Config(**overrides),
        )
        s3errormsg.register(client)
        return client
    except (NoCredentialsError, NoRegionError) as exc:
        # aws has dedicated handlers for these two (-> 253); every other botocore
        # error (ProfileNotFound, PartialCredentialsError, ...) falls to its
        # GeneralExceptionHandler (-> 255) via the BotoCoreError clause below -
        # InvalidConfigError, which exit_code_for maps to 255, not 253.
        raise ConfigurationError(str(exc)) from exc
    except BotoCoreError as exc:
        raise InvalidConfigError(str(exc)) from exc


def _create_client(
    session: Boto3Session, botocore_session: BotocoreSession, service: str, **kwargs: Any
) -> Any:
    """``session.client`` with aws v2's retry-mode vocabulary around it.

    The retry configuration itself is the session's (`_open_botocore_session`
    declares aws v2's ``standard`` / 3 defaults), so botocore resolves and
    validates it exactly where aws's does - and that placement is what a
    config carrying several mistakes reports: measured on the pinned aws-cli,
    the profile's ``services`` section is reported first, then the raw ``[s3]``
    read, then the ``max_attempts`` int cast, then ``[s3] addressing_style``,
    and only then the attempt range and the mode. Resolving the retry
    configuration ahead of ``create_client``, as this module used to, put the
    mode and the range in front of all four.

    Only the vocabulary differs: aws v2's bundled botocore accepts
    ``standard`` / ``adaptive`` alone, while the installed botocore still
    counts ``legacy`` as valid and lists all three in its own report. So the
    mode is judged where botocore judges it - rewording the rejection botocore
    raises, and rejecting the ``legacy`` it lets through, once the client is
    built and every earlier report has had its turn.

    Every client this CLI hands out is created here, which is what makes this
    the place the CONNECT pin goes on (`proxytunnel`): an HTTPS proxy sees the
    tunnel request the *host interpreter* writes, and only 3.12 and later write
    aws's.
    """
    from botocore.exceptions import InvalidRetryModeError

    from boto3_s3_cli import proxytunnel

    proxytunnel.pin_connect_request()
    try:
        client = session.client(service, **kwargs)  # pyright: ignore[reportUnknownMemberType]
    except InvalidRetryModeError:
        _reject_unsupported_retry_mode(botocore_session.get_config_variable("retry_mode"))
        raise
    retries = cast("dict[str, Any] | None", client.meta.config.retries) or {}
    _reject_unsupported_retry_mode(retries.get("mode"))
    return client


def _reject_unsupported_retry_mode(mode: Any) -> None:
    """Reject a retry mode aws v2 does not have, with aws's own wording.

    ``legacy`` is stock botocore's default and one of its three valid modes;
    aws v2's bundled botocore dropped it, so a profile carrying the aws-cli v1
    value (or any other typo) is rc 255 there - and the report names the two
    modes that remain, where the installed botocore names all three.
    """
    if mode is None or mode in ("standard", "adaptive"):
        return
    raise InvalidConfigError(
        f'Invalid value provided to "mode": "{mode}" must be one of: "standard" or "adaptive"'
    )
