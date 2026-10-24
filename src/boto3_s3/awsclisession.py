"""aws-cli's assembly of a session and its clients, behind `S3.from_aws_cli`.

An `S3` the caller builds itself (``S3()``) takes boto3's defaults for
everything the caller leaves out: the profile, the region, the retries, the
trust store, the default checksum. ``aws s3`` answers each of those its own
way - from its command line, its environment variables and its bundled
botocore - and an application that wants ``aws s3``'s results asks for that
assembly through `S3.from_aws_cli`, which builds an `AwsCliAssembly` from
`AwsCliSettings` (design/overview.md section 3, "Compatibility target").
The ``boto3-s3`` command is built on the same assembly.

Everything here is scoped to the session the assembly opens and the clients
it builds: nothing process-wide is changed. The two things aws-cli also does
to its process - the HTTPS-proxy CONNECT request (`pin_proxy_connect`) and
the class-level SSO token cache (`inject_credential_cache`) - are left to
the application (the command does both).

Module import stays SDK-free: botocore is imported when an assembly is built.
"""

from __future__ import annotations

import contextlib
import enum
import json
import os
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urlparse

from boto3_s3.exceptions import ConfigurationError, InvalidConfigError, ValidationError

if TYPE_CHECKING:
    from boto3.session import Session as Boto3Session
    from botocore.session import Session as BotocoreSession

# The environment variables aws-cli reads the active profile from, in its order
# (its bundled botocore lists them so; stock botocore reverses the two).
PROFILE_ENV_VARS = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")


@dataclass(frozen=True, slots=True, kw_only=True)
class AwsCliSettings:
    """The values aws-cli's common options carry, as `S3.from_aws_cli` takes them.

    Each field is the snake_case form of the ``aws`` option of the same name
    and means what that option means; one left at its default is resolved the
    way ``aws`` resolves the option's absence (environment variables, the
    config file, botocore's defaults). The timeouts are seconds, ``0`` meaning
    no timeout and ``None`` botocore's 60. ``cache_credentials`` has no
    ``aws`` option: ``True`` keeps the temporary credentials an assumed role,
    a web identity or SSO fetches in aws-cli's ``~/.aws/cli/cache``, as
    ``aws`` does, and ``False`` keeps them in memory, as boto3 does.
    """

    profile: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    no_verify_ssl: bool = False
    ca_bundle: str | None = None
    no_sign_request: bool = False
    cli_read_timeout: int | None = None
    cli_connect_timeout: int | None = None
    cache_credentials: bool = True


class _Unresolved(enum.Enum):
    """The type behind `UNRESOLVED` (a single-member enum, so a type checker
    narrows ``str | None | Literal[...]`` on an identity test)."""

    TOKEN = "unresolved"


# `AwsCliAssembly.client`'s "keep the assembly's own" default, distinct from a
# supplied ``None``.
UNRESOLVED: Final = _Unresolved.TOKEN

# The attribute carrying, on a client an assembly built, an `Assembled`: what
# `S3` reads to apply aws-cli's run-level defaults to that client's runs alone,
# any other client keeping its own settings.
ASSEMBLED_BY = "_boto3_s3_aws_cli_assembly"


@dataclass(frozen=True, slots=True)
class Assembled:
    """How an assembly built one client: by which assembly, for which region.

    ``region`` is the region the client was built for - the assembly's
    chain answer or one the builder named - ``None`` when nothing resolved
    (botocore then gives the client the ``aws-global`` pseudo-region).
    """

    assembly: AwsCliAssembly
    region: str | None


def assembled(client: Any) -> Assembled | None:
    """How an assembly built *client*, or None for any other client."""
    mark = getattr(client, ASSEMBLED_BY, None)
    return mark if isinstance(mark, Assembled) else None


class AwsCliAssembly:
    """One session opened the way aws-cli opens its own, and the clients built from it.

    Built from `AwsCliSettings`: the session is opened, checked and bound to
    aws-cli's region chain at once, so a broken profile or config file fails
    here, as ``aws`` fails before a command starts. ``region`` is the chain's
    answer (``None`` when nothing resolves), ``session`` the boto3 session the
    clients come from.
    """

    def __init__(self, settings: AwsCliSettings) -> None:
        self.settings = settings
        with build_errors():
            validate_endpoint_url(settings.endpoint_url)
            reject_empty_ca_bundle(settings.ca_bundle)
            botocore_session = open_botocore_session(settings)
            self.session: Boto3Session = boto3_session(botocore_session)
            self.region: str | None = resolve_region(settings.region, botocore_session)
            bind_region(botocore_session, settings.region, self.region)

    @property
    def botocore_session(self) -> BotocoreSession:
        return self.session._session  # pyright: ignore[reportPrivateUsage]

    def client(
        self,
        service: str = "s3",
        *,
        region: str | None | Literal[_Unresolved.TOKEN] = UNRESOLVED,
        endpoint_url: str | None | Literal[_Unresolved.TOKEN] = UNRESOLVED,
        config_overrides: Mapping[str, Any] | None = None,
    ) -> Any:
        """A client of *service* as aws-cli builds it.

        For ``s3`` that is aws-cli's ``ClientFactory.create_client``: the
        assembly's region and ``--endpoint-url``, the TLS flags as ``verify``
        (`client_verify`), the timeouts in its ``Config``, and aws-cli's
        default request checksum (`register_default_checksum`). Any other
        service is ``S3PathResolver.from_session``'s: no endpoint override.
        *region* / *endpoint_url* replace the assembly's own (``UNRESOLVED``,
        the default, keeps it); ``None`` for *region* leaves the session's
        bound answer to decide. *config_overrides* are further ``Config``
        fields the caller merges in.
        """
        from botocore.config import Config

        if region is UNRESOLVED:
            region = self.region
        if endpoint_url is UNRESOLVED:
            endpoint_url = self.settings.endpoint_url if service == "s3" else None
        overrides: dict[str, Any] = dict(cli_timeouts(self.settings))
        overrides.update(config_overrides or {})
        kwargs: dict[str, Any] = {
            "region_name": region,
            "verify": client_verify(self.settings),
            "config": Config(**overrides),
        }
        if endpoint_url is not None:
            kwargs["endpoint_url"] = endpoint_url
        with build_errors():
            client = create_client(self.session, service, **kwargs)
        if service == "s3":
            register_default_checksum(client)
        setattr(client, ASSEMBLED_BY, Assembled(self, region))
        return client

    def crt_verify(self) -> str | Literal[False]:
        """The CRT client's trust source for this assembly (`crt_verify`)."""
        return crt_verify(self.settings, self.botocore_session)


def validate_endpoint_url(endpoint_url: str | None) -> None:
    """Reject a schemeless endpoint URL, as aws-cli does before a command runs.

    botocore would otherwise raise a bare ``ValueError`` when the client is
    built; the wording is aws-cli's.
    """
    if endpoint_url is not None and not urlparse(endpoint_url).scheme:
        raise ValidationError(
            f'Bad value for --endpoint-url "{endpoint_url}": scheme is '
            "missing.  Must be of the form http://<hostname>/ or https://<hostname>/"
        )


@contextlib.contextmanager
def construction_errors() -> Generator[None, None, None]:
    """Translate botocore's construction-time errors into the library taxonomy.

    Opening a session and building a client can raise raw botocore errors
    (``ProfileNotFound`` for a bad profile, credential or region resolution
    failures). aws-cli has dedicated handlers for ``NoCredentialsError`` /
    ``NoRegionError`` (`ConfigurationError`, its rc 253); every other botocore
    error (``ProfileNotFound``, ``PartialCredentialsError``, ...) falls to its
    general handler (rc 255) - `InvalidConfigError`. The library's own errors
    pass through as they are.
    """
    from botocore.exceptions import BotoCoreError, NoCredentialsError, NoRegionError

    try:
        yield
    except (NoCredentialsError, NoRegionError) as exc:
        raise ConfigurationError(str(exc)) from exc
    except BotoCoreError as exc:
        raise InvalidConfigError(str(exc)) from exc


@contextlib.contextmanager
def build_errors() -> Generator[None, None, None]:
    """`construction_errors`, plus what the SDK raises outside its own family meanwhile.

    It reads the configuration while a session opens and a client is built: a
    malformed endpoint is a plain ``ValueError``, a config value of the wrong
    shape an ``AttributeError``, a ``credential_process`` that cannot run an
    ``OSError`` - the library's `client_build_error` classifies them, as for
    a client an `S3()` builds. The command, whose general handler takes them
    raw, keeps `construction_errors` alone.
    """
    from boto3_s3.sdkerrors import client_build_error

    with construction_errors():
        try:
            yield
        except (ValueError, AttributeError, OSError) as exc:
            raise client_build_error(exc) from exc


def open_botocore_session(settings: AwsCliSettings) -> BotocoreSession:
    """Open a botocore session the way aws-cli's driver opens its own.

    Only a truthy ``profile`` is bound onto the session (aws-cli's
    ``_handle_top_level_args``: ``set_config_variable('profile', ...)`` under a
    truthy guard). A profile named by ``AWS_PROFILE`` / ``AWS_DEFAULT_PROFILE``
    is deliberately left to botocore's own env read, because the two are not
    interchangeable: botocore treats a *session instance* profile as "the user
    asked for this profile explicitly" and drops the environment credential
    provider for it (``create_credential_resolver``'s ``disable_env_vars``), so
    promoting the env-named profile would turn ``AWS_PROFILE=x`` plus
    ``AWS_ACCESS_KEY_ID`` - a routine CI shape that works under `aws` - into
    "Unable to locate credentials". Redeclaring the profile config variable
    restores aws-cli's env order (stock botocore reads ``AWS_DEFAULT_PROFILE``
    first, botocore #1725) without that promotion.

    ``retry_mode`` / ``max_attempts`` are redeclared with the defaults aws-cli v2's
    bundled botocore hard-codes (``standard`` / 3, where stock botocore
    declares ``legacy`` and no cap). Declaring them on the *session* is what
    puts every client it builds on aws-cli's retry posture - not just the ones
    an assembly builds, but the STS / SSO clients the credential chain creates
    for itself, which a per-client ``Config`` never reaches (measured: a
    failing ``AssumeRole`` is attempted 3 times under `aws` and was attempted
    5 without). The ``[s3]`` regional pin (`pin_regional_s3_endpoint`) and the
    timeouts in the session default client config (`default_client_config`)
    reach the same clients for the same reason.

    Then every client the session builds is held to the empty CA bundle check
    (`check_every_client`), and the temporary-credential providers get their
    on-disk cache when the settings ask for it (`inject_credential_cache`),
    the last thing aws-cli does to a session before running a command.
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
    if settings.profile:
        session.set_config_variable("profile", settings.profile)
    pin_regional_s3_endpoint(session)
    session.set_default_client_config(default_client_config(settings))
    check_every_client(session)
    if settings.cache_credentials:
        inject_credential_cache(session)
    return session


def boto3_session(botocore_session: BotocoreSession) -> Boto3Session:
    """The boto3 session over *botocore_session*, its clients on `fast_parse_timestamp`.

    Registered before any client is built: large listings parse their
    ``LastModified`` values at C speed where aws-cli walks dateutil's generic
    parser per object, with byte-identical output.
    """
    import boto3

    from boto3_s3.sessions import fast_parse_timestamp

    botocore_session.get_component("response_parser_factory").set_parser_defaults(
        timestamp_parser=fast_parse_timestamp
    )
    return boto3.Session(botocore_session=botocore_session)


def check_every_client(session: BotocoreSession) -> None:
    """Hold every client the session builds to aws-cli's empty CA bundle check.

    aws-cli's bundled botocore refuses, in every ``create_client``, a CA bundle
    that resolves to an empty or whitespace-only value (`reject_empty_ca_bundle`).
    The SDK builds clients of its own from the same session - the credential
    chain's STS, SSO and sso-oidc clients, the CRT engine's request serializer
    - and those took an empty ``AWS_CA_BUNDLE`` beside a valid ``--ca-bundle``
    as "verification off", fetching credentials over unverified TLS where
    aws-cli stops (measured: rc 255 for ``ls``, a failed item for ``cp``, rc 0
    without the check). So the session's own ``create_client`` runs the check
    on what it built, resolving the trust source the way botocore does when
    the caller names none: the session's ``ca_bundle``, then
    ``REQUESTS_CA_BUNDLE``. It is installed before the credential chain is
    built: its SSO provider keeps the method it was handed then. Each client
    it returns carries the library's `CA_BUNDLE_ENVIRONMENT_VETTED` mark, so
    the library's own guess at the environment (made for a client a caller
    hands it) stands down: the check here already ran where aws-cli runs it,
    and the guess would refuse runs aws-cli completes.
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
        reject_empty_ca_bundle(verify)
        setattr(client, CA_BUNDLE_ENVIRONMENT_VETTED, True)
        return client

    # An instance attribute shadows the method for every caller that looks it
    # up on this session (boto3's `Session.client` included).
    setattr(session, "create_client", checked_create_client)  # noqa: B010


def reject_empty_ca_bundle(verify: Any) -> None:
    """Reject an empty or whitespace-only CA bundle value, like aws-cli v2 (>= 2.36.6).

    aws-cli refuses an empty or whitespace-only ``ca_bundle`` / ``AWS_CA_BUNDLE``
    / ``REQUESTS_CA_BUNDLE`` / ``--ca-bundle`` value outright at client
    construction (its bundled botocore's ``EndpointCreator._validate_verify_value``)
    rather than silently turning TLS verification off. The installed botocore
    adopted the same check in 1.43.54, but the supported floor is older
    (docs/compatibility.md), so a host botocore below it would still disable
    verification. Raising botocore's own ``InvalidConfigError`` holds the
    rejection across the whole support range; `construction_errors` refines it
    to the library's `InvalidConfigError`. ``False`` (verification off on
    purpose), ``None`` and a real path pass through untouched.
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


class _RegionalS3Section:
    """The profile's ``[s3]`` section with ``us_east_1_regional_endpoint`` pinned.

    aws-cli v2's bundled botocore dropped that key from its ``[s3]`` table outright
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
        where aws-cli - whose bundled botocore has no defaults modes at all -
        simply ran (measured for every valid mode, config key and env var
        alike). Delegating leaves botocore's own bookkeeping intact; `provide`
        pins the value whatever the write says.
        """
        self._section_provider.set_default_provider(key, default_provider)


def pin_regional_s3_endpoint(session: BotocoreSession) -> None:
    """Wrap the session's ``[s3]`` section provider in `_RegionalS3Section`.

    The session's config store is the one place every client reads the section
    from - the S3 client, ``mv``'s s3control / sts resolver clients, and the
    clients the credential providers build for themselves - so pinning it here
    covers all of them at once, where a ``Config(s3=...)`` covers only the
    clients it is passed to.
    """
    config_store = session.get_component("config_store")
    section_provider = config_store.get_config_provider("s3")
    config_store.set_config_provider("s3", _RegionalS3Section(section_provider))


def cli_timeouts(settings: AwsCliSettings) -> dict[str, Any]:
    """The ``Config`` fields of the timeouts the settings name: aws-cli's ``_resolve_timeout``.

    Read before connect, the order aws-cli registers its two handlers in;
    ``0`` becomes ``None``, botocore's "no timeout" (aws-cli's ``int(value) or
    None``). A timeout left out names no field, leaving the session's default
    (`default_client_config`).
    """
    timeouts: dict[str, Any] = {}
    if settings.cli_read_timeout is not None:
        timeouts["read_timeout"] = settings.cli_read_timeout or None
    if settings.cli_connect_timeout is not None:
        timeouts["connect_timeout"] = settings.cli_connect_timeout or None
    return timeouts


def default_client_config(settings: AwsCliSettings) -> Any:
    """The session default client config aws-cli's startup handlers build.

    aws-cli's globalargs resolves ``--cli-read-timeout`` / ``--cli-connect-timeout``
    into the session's default client config (``_resolve_timeout`` ->
    ``_update_default_client_config``), defaulting both to botocore's 60s, so
    every client created from that session inherits them - the credential
    chain's own STS / SSO clients included. Without that, a stalled STS ran to
    botocore's default while aws-cli gave up on the flag's budget (measured:
    ``--cli-read-timeout 2`` against an STS stalling 8s is rc 255 and a read
    timeout on `aws`, and was rc 254 with the server's own error without it).

    ``no_sign_request`` lands in the very same config, from aws-cli's
    ``no_sign_request`` handler, and that placement is load-bearing: it
    reaches the clients botocore builds for itself, so an unsigned run that
    still resolves credentials sends an *unsigned* ``AssumeRole``, as aws-cli
    does. The clients an assembly builds pass the same two timeouts in their
    own ``Config`` as well, which botocore merges on top of this one - the same
    number either way.
    """
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.endpoint import DEFAULT_TIMEOUT

    timeouts: dict[str, Any] = {
        "connect_timeout": DEFAULT_TIMEOUT,
        "read_timeout": DEFAULT_TIMEOUT,
        **cli_timeouts(settings),
    }
    overrides: dict[str, Any] = {}
    if settings.no_sign_request:
        overrides["signature_version"] = UNSIGNED
    return Config(**timeouts, **overrides)


def credential_cache_dir() -> str:
    """The directory aws-cli caches assumed-role / SSO credentials in.

    Literally aws-cli's own ``~/.aws/cli/cache`` (its ``CACHE_DIR``), so the two
    share one cache: credentials fetched under either are reused by the other
    for as long as they are valid. aws-cli freezes the expansion at import
    time; expanding per call is the same answer for a process whose ``HOME``
    does not move, and it keeps the directory testable.
    """
    return os.path.expanduser(os.path.join("~", ".aws", "cli", "cache"))


def inject_credential_cache(session: BotocoreSession) -> None:
    """Persist assumed-role / web-identity / SSO credentials across runs, as aws-cli does.

    botocore caches the temporary credentials these three providers fetch in a
    plain dict, which dies with the process: every run would re-call
    ``AssumeRole``, and an ``mfa_serial`` profile would prompt for a code
    every time - fatal in any non-interactive run. aws-cli's two
    ``session-initialized`` handlers replace that dict with a ``JSONFileCache``
    over `credential_cache_dir`; this does the same to this session's three
    providers, at aws-cli's timing (after the profile is bound, so the
    provider chain this reaches is built for the run's own profile). Nothing
    beyond the session is touched: the SSO token cache botocore builds from a
    class attribute (``SSOTokenProvider.DEFAULT_CACHE_CLS``, read when the
    chain is built) is process-wide, so it is the application's to set - the
    command sets it to `credential_cache_class` before it builds.
    """
    cache_class = credential_cache_class()
    cache_dir = credential_cache_dir()
    chain = session.get_component("credential_provider")
    chain.get_provider("assume-role").cache = cache_class(cache_dir)
    chain.get_provider("assume-role-with-web-identity").cache = cache_class(cache_dir)
    chain.get_provider("sso").cache = cache_class(cache_dir)


def _serialize_cache_entry(value: Any) -> Any:
    """Render for `json.dumps` what it cannot render itself: a ``datetime``.

    aws-cli never reaches this case - its ``cli_timestamp_format`` handler makes
    the response parser return timestamps as ISO-8601 *strings*, so a
    credential's ``Expiration`` is already text by the time the cache stores it
    and lands in the file verbatim (``2026-08-13T10:23:54+09:00``). An assembly
    parses timestamps to ``datetime`` for speed (`fast_parse_timestamp`), and
    botocore's own default renders one with ``strftime('%Y-%m-%dT%H:%M:%S%Z')``,
    whose ``%Z`` for an offset-carrying ``datetime.timezone`` is the literal
    ``UTC+09:00`` - which dateutil reads back with POSIX's inverted sign,
    leaving the entry looking valid for hours after it expired (for this
    library and for any aws-cli sharing the directory). ``isoformat()`` is the
    text aws-cli stores, byte for byte.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@cache
def credential_cache_class() -> type[Any]:
    """botocore's ``JSONFileCache`` with aws-cli's write, built on first use.

    The installed botocore writes an entry through ``tempfile.mkstemp`` +
    ``os.replace``; the botocore aws-cli ships opens the *final* path directly
    (``O_WRONLY | O_CREAT``, mode 0600, then truncate). The difference is only
    visible when the write fails, which is exactly when it is read: an
    unwritable ``~/.aws/cli/cache`` makes aws-cli report the cache entry's own
    name, stable across runs, where botocore's write reports a random
    ``tmpXXXXXXXX.tmp``. The same write keeps an entry aws-cli wrote and
    reads in the shape both expect.

    The class is built lazily because botocore is imported lazily, and cached
    so every cache an assembly installs is of one type. It takes a
    ``dumps_func`` the way botocore's does, which the SSO token cache uses.
    """
    # botocore.credentials re-exports this very class; `utils` is where it is
    # defined, and the only spelling botocore-stubs declares.
    from botocore.utils import JSONFileCache

    class AwsCliCredentialCache(JSONFileCache):
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

    return AwsCliCredentialCache


def resolve_region(explicit: str | None, session: BotocoreSession) -> str | None:
    """The region to build a client in: the *explicit* value, else the chain.

    The *explicit* value is ``--region`` - and it wins whenever it was
    supplied at all, the empty string included. aws-cli's s3 commands hand
    ``parsed_globals.region`` straight to ``create_client``, so ``--region ""``
    reaches the S3 client as itself and fails construction there. What the
    *session* is bound to is a different question with a different answer for
    that one value - see `bind_region`.
    """
    if explicit is not None:
        return explicit
    return region_chain(session)


def region_chain(session: BotocoreSession) -> str | None:
    """aws-cli's region chain below the flag.

    Mirrors the tail of aws-cli's ``_construct_cli_region_chain``:
    ``AWS_REGION`` env > ``AWS_DEFAULT_REGION`` env > the profile's config-file
    ``region`` > the EC2 IMDS region. Stock botocore never adopted ``AWS_REGION``
    (its region env is ``AWS_DEFAULT_REGION`` alone) and reserves its
    ``IMDSRegionProvider`` for smart-defaults, so a bare client would resolve a
    *different* region whenever ``AWS_REGION`` is the only source, or on an EC2
    host with no region configured. The env vars are present-wins, an empty
    value included (``AWS_REGION=`` -> ``""``, which aws-cli signs with). An
    `S3` the caller builds itself keeps stock botocore order on purpose
    (design/overview.md section 3).

    A ``BadIMDSRequestError`` reads as no region. aws-cli carries its own copy
    of botocore's region fetcher for exactly one added catch: botocore's
    swallows only the retries-exceeded failure, so a metadata service that
    rejects the token request outright - anything answering at the IMDS address
    that is not EC2's service - escapes as that error, where aws-cli logs it
    and walks on with an unresolved region.
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


def bind_region(
    botocore_session: BotocoreSession, explicit: str | None, resolved: str | None
) -> None:
    """Bind onto the session the region aws-cli's own chain answers.

    aws-cli installs its region chain as the session's own ``region`` config
    provider and binds a truthy ``--region`` at the head of it
    (``_update_config_chain`` / ``_handle_top_level_args``), so every client
    built from that session resolves the chain's answer - including the clients
    *botocore itself* builds, chiefly the STS client the assume-role and
    web-identity providers create for their own use. Passing the answer to each
    client as ``region_name`` never reached those, so an assume-role profile
    whose only region source was ``--region`` or ``AWS_REGION`` signed
    ``AssumeRole`` against the global STS endpoint - a different partition
    under ``cn-*`` / ``us-gov-*`` - or failed outright with ``NoRegion``.

    What binds is not what the client is built with. Only a *truthy* *explicit*
    region heads aws-cli's chain, so a falsy ``--region ""`` leaves the rest of
    the chain to answer for the session while the empty string still reaches
    the S3 client as its own ``region_name`` (`resolve_region`): measured,
    ``--region ""`` with ``AWS_REGION=eu-west-1`` has aws-cli sign
    ``AssumeRole`` in eu-west-1. An ``AWS_REGION=`` that is present but empty
    is the chain's own answer and binds as itself - aws-cli signs with an
    empty region scope.

    *resolved* is the answer `resolve_region` already computed for *explicit*,
    so the chain - whose last link is the IMDS probe - is walked once. Only
    the falsy-explicit corner asks it again, no client's region being able to
    carry that answer. Nothing is bound when the chain resolved nothing, which
    leaves the session on botocore's identical answer.
    """
    if explicit:
        region = explicit
    elif explicit is None:
        region = resolved
    else:
        region = region_chain(botocore_session)
    if region is not None:
        botocore_session.set_config_variable("region", region)


def crt_verify(settings: AwsCliSettings, botocore_session: BotocoreSession) -> str | Literal[False]:
    """The CRT client's trust source: aws-cli's CRT factory ``_resolve_verify``.

    The flags first (`client_verify`), then the ``ca_bundle`` config variable
    read off *botocore_session*, then botocore's bundled ``cacert.pem``
    (``DEFAULT_CA_BUNDLE``). ``REQUESTS_CA_BUNDLE`` is not read and neither is
    certifi, though the botocore clients honor both: aws-cli's CRT lane reads
    neither. An empty or whitespace-only value is refused as a client's would
    be (`reject_empty_ca_bundle`), before it could reach the CRT as
    "verification off".
    """
    verify = client_verify(settings)
    if verify is None:
        verify = cast("str | None", botocore_session.get_config_variable("ca_bundle"))
    if verify is None:
        from botocore.httpsession import DEFAULT_CA_BUNDLE

        verify = DEFAULT_CA_BUNDLE
    with construction_errors():
        reject_empty_ca_bundle(verify)
    return cast("str | Literal[False]", verify)


def client_verify(settings: AwsCliSettings) -> bool | str | None:
    """The ``verify`` every botocore client an assembly builds is given.

    aws-cli's ``resolve_verify_ssl``: ``no_verify_ssl`` -> ``False``, else
    ``ca_bundle``, else ``None``, which leaves the rest of the chain to
    botocore as aws-cli's ``ClientFactory`` does - the ``ca_bundle`` config
    variable (``AWS_CA_BUNDLE`` or the profile's ``ca_bundle``), then
    ``REQUESTS_CA_BUNDLE``, then its default bundle. The CRT client resolves
    its own (`AwsCliAssembly.crt_verify`).
    """
    if settings.no_verify_ssl:
        return False
    return settings.ca_bundle


def create_client(session: Boto3Session, service: str, **kwargs: Any) -> Any:
    """``session.client`` with the CA bundle check on the caller's own value.

    The session's own check (`check_every_client`) covers what the session
    resolves; this one covers the ``verify`` passed in, for a session an
    assembly did not open.
    """
    client = session.client(service, **kwargs)  # pyright: ignore[reportUnknownMemberType]
    reject_empty_ca_bundle(kwargs.get("verify"))
    return client


# aws-cli v2's bundled botocore ``DEFAULT_CHECKSUM_ALGORITHM``.
AWS_DEFAULT_CHECKSUM_ALGORITHM = "CRC64NVME"


def _computable_default() -> str | None:
    """aws-cli's default where the installed botocore can compute it, else None
    (no awscrt, or one too old; botocore's own registry is the authority on
    that, so it is read rather than re-derived)."""
    from botocore import httpchecksum

    supported = getattr(httpchecksum, "_SUPPORTED_CHECKSUM_ALGORITHMS", ())
    if AWS_DEFAULT_CHECKSUM_ALGORITHM.lower() not in supported:
        return None
    return AWS_DEFAULT_CHECKSUM_ALGORITHM


def default_upload_algorithm(client: Any, *, crt: bool) -> str | None:
    """aws-cli's default request checksum for an upload on *client*, or None.

    aws-cli v2 ships a botocore whose ``DEFAULT_CHECKSUM_ALGORITHM`` is
    ``CRC64NVME``; pip botocore's is ``CRC32``. Neither tool's own code picks
    the algorithm: botocore stamps its default on a request whose operation
    names a ``ChecksumAlgorithm`` member when the client's
    ``request_checksum_calculation`` is ``when_supported`` - the default - and
    the caller named none, and s3transfer copies the same constant into an
    upload's arguments. So an upload names CRC64NVME here, through the
    transfer options, where the engines would otherwise name CRC32.

    The classic engine's default follows ``request_checksum_calculation``
    (None under ``when_required``, or on a botocore that predates the
    setting). The CRT engine's (*crt*) holds whatever the setting says:
    aws-cli's bundled s3transfer has the constant written into its CRT module
    as the fallback for an upload that names no algorithm, and that module
    never consults the setting (measured on the wire). Either way None where
    botocore cannot compute CRC64NVME: no awscrt, or ``BOTO_DISABLE_CRT``
    hiding it from botocore.

    This is not cosmetic: aws-checksums computes CRC32 in software on x86-64
    (about 3 GiB/s against about 20 for CRC64NVME), which on a 4-vCPU instance
    pushing 800 MiB/s over the CRT engine was 10% of a 1 GB upload's wall time
    against real S3 (benchmarks/RESULTS.md, 2026-09-06).
    """
    if not crt and (
        getattr(client.meta.config, "request_checksum_calculation", None) != "when_supported"
    ):
        return None
    return _computable_default()


def register_default_checksum(client: Any) -> None:
    """Attach aws-cli's default request checksum to one S3 client.

    Stamps the algorithm on any request whose caller left the operation's
    ``requestAlgorithmMember`` unset and where botocore would then stamp its
    own default - botocore's own test, in its order: an operation that
    requires a checksum (``requestChecksumRequired``; DeleteObjects,
    PutBucketWebsite, and the PutObjectTagging a copy falls back to for a
    large tag set) gets one under either ``request_checksum_calculation``
    setting, every other one only under ``when_supported`` - so the deletes,
    bucket puts and annotation writes carry what aws-cli's do (a
    ``when_required`` ``website`` sends CRC64NVME on both tools, measured).
    An explicit value, or the upload default `default_upload_algorithm` puts
    in the transfer options, is left alone; a presigned request gets none.
    Only where botocore can compute CRC64NVME.
    """
    algorithm = _computable_default()
    if algorithm is None:
        return
    calculation = getattr(client.meta.config, "request_checksum_calculation", None)

    def stamp(
        params: dict[str, Any], model: Any, context: dict[str, Any] | None = None, **_: Any
    ) -> None:
        if context and context.get("is_presign_request"):
            return
        http_checksum = model.http_checksum
        member = http_checksum.get("requestAlgorithmMember")
        if not member or member in params:
            return
        required = getattr(model, "http_checksum_required", False) or http_checksum.get(
            "requestChecksumRequired"
        )
        if required or calculation == "when_supported":
            params[member] = algorithm

    client.meta.events.register("provide-client-params.s3.*", stamp)


# The assembly and its settings, and the building blocks the ``boto3-s3``
# command composes its own clients from.
__all__ = [
    "ASSEMBLED_BY",
    "AWS_DEFAULT_CHECKSUM_ALGORITHM",
    "PROFILE_ENV_VARS",
    "UNRESOLVED",
    "Assembled",
    "AwsCliAssembly",
    "AwsCliSettings",
    "assembled",
    "bind_region",
    "boto3_session",
    "build_errors",
    "check_every_client",
    "cli_timeouts",
    "client_verify",
    "construction_errors",
    "create_client",
    "credential_cache_class",
    "credential_cache_dir",
    "crt_verify",
    "default_client_config",
    "default_upload_algorithm",
    "inject_credential_cache",
    "open_botocore_session",
    "pin_regional_s3_endpoint",
    "region_chain",
    "register_default_checksum",
    "reject_empty_ca_bundle",
    "resolve_region",
    "validate_endpoint_url",
]
