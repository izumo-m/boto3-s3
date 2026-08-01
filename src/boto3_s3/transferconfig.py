"""`TransferConfig` - boto3's transfer configuration plus library settings.

The library re-exports this subclass of ``boto3.s3.transfer.TransferConfig``
as its public ``TransferConfig``. The transfer engine honors
``preferred_transfer_client`` (``'auto'`` / ``'classic'`` / ``'crt'``) with
boto3's own semantics (``transfer.py`` / ``crtsupport.py``; design in
``design/crt.md``); the constructor forwards it to the base class when that boto3
accepts it and otherwise keeps it as a plain attribute (the floor boto3 lacks
the field - see the signature probe below). The subclass adds the CRT tuning
knobs aws-cli keeps in its `[s3]` runtime config plus annotation staging:

- ``target_bandwidth`` - target throughput for the CRT engine in bytes per
  second (aws-cli ``target_bandwidth``; becomes the CRT client's
  ``target_throughput``). The classic engine's cap stays ``max_bandwidth``.
- ``should_stream`` / ``disk_throughput`` / ``direct_io`` - aws-cli's CRT
  file-I/O options. The pip ``s3transfer`` CRT client cannot apply them yet
  (no ``fio_options`` parameter as of 0.19); they are accepted now and passed
  through automatically once ``create_s3_crt_client`` grows support.
- `annotation_temp_dir` - directory for
  `AnnotationCopyMode.PRELOAD_TEMPFILE`; `None` delegates to the operating
  system's standard temporary-directory selection.

The extras are plain attributes (not part of the base `DEFAULTS` sentinel
machinery) and default to `None`. The CRT tuning fields are ignored by the
classic engine; `annotation_temp_dir` is consulted by the classic
multipart-copy path whenever `copy_props=ALL` staging is set up, but used
(as the tempfile location) only under `PRELOAD_TEMPFILE`. A plain
`boto3.s3.transfer.TransferConfig` remains accepted everywhere a config is
taken - readers fall back to `None` for the extra fields (and keeps boto3's own
defaults, including the one below).

One base default departs from boto3: `max_io_queue` defaults to the value
`aws s3` runs at rather than the one boto3 dials it down to. Names, order,
semantics and the `UNSET_DEFAULT` sentinel that tells an explicit value from a
defaulted one are boto3's throughout.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

from boto3.s3.transfer import TransferConfig as Boto3TransferConfig

__all__ = ["TransferConfig"]

# The library's download IO queue depth (s3transfer's ``max_io_queue_size``:
# the cap on read parts buffered for the disk writer). s3transfer defaults it
# to 1000 and aws-cli runs there - no ``[s3]`` key maps to it - while boto3
# alone dials the same default down to 100, which would leave a slow disk
# holding a tenth of aws's readahead. The buffered ceiling is this count x
# ``io_chunksize`` (~256 MiB at the default 256 KiB) across the manager's
# downloads - one shared io executor serves them all - and is reached only when
# the disk lags the network; pass ``max_io_queue=100`` for boto3's ceiling.
_MAX_IO_QUEUE = 1000

# Back-compat (floor boto3 1.28, docs/compatibility.md): boto3's CRT
# support added the ``preferred_transfer_client`` constructor parameter only in
# boto3 ~1.33; the declared floor's base ctor does not accept it. Forwarding it
# unconditionally raises TypeError on the floor (breaking every cp/mv/sync,
# even classic with no awscrt), so probe the base signature once and, when
# absent, keep the value as a plain attribute instead - the engine reads it via
# getattr (transfer.py). On the floor the s3transfer (0.6.2) has no CRT surface,
# so 'auto' falls to classic while an explicit 'crt' is rejected by crtsupport
# (not silently downgraded). Drop this shim once the boto3 floor is raised
# past 1.33.
_BASE_ACCEPTS_PREFERRED_TRANSFER_CLIENT = (
    "preferred_transfer_client" in inspect.signature(Boto3TransferConfig.__init__).parameters
)

# Back-compat (floor boto3 1.28): the ``DEFAULTS`` table the class below
# overrides arrived with the ``UNSET_DEFAULT`` sentinel machinery (~1.42, the
# boto3 that lets ``[s3]`` tuning reach the CRT manager - compatibility.md) -
# well after the ctor grew ``preferred_transfer_client`` (the shim above).
# Every DEFAULTS-less boto3 carries concrete ctor defaults instead, so there
# the value has to be forwarded explicitly. That is safe across the whole
# band, the CRT-capable releases without the table (~1.33 - ~1.41; verified
# on 1.34.162) included, because the explicit-'crt' validation that would
# read a forwarded value as an explicitly-set classic-only option is itself
# gated on the sentinel (`create_crt_transfer_manager` submits a config to it
# only when the config carries ``UNSET_DEFAULT``). On a modern boto3 that
# validation runs, which is why the table is the only way there. Drop this
# shim only once the floor passes the table's arrival (~1.42): dropping it at
# the shim above's boundary would put the base 100 back on every DEFAULTS-less
# boto3.
_BASE_HAS_DEFAULTS = hasattr(Boto3TransferConfig, "DEFAULTS")


class TransferConfig(Boto3TransferConfig):
    """boto3's `TransferConfig` with library-only settings appended.

    The base parameters keep boto3's exact names, order, and semantics, with
    one deliberate departure: `max_io_queue` defaults to `aws s3`'s depth
    instead of boto3's. The extra settings are keyword-only and appended last,
    so existing boto3 code works unchanged.
    """

    # boto3 resolves a defaulted base parameter through this table, leaving the
    # ``UNSET_DEFAULT`` sentinel on the instance - so overriding an entry moves
    # the default without making the value look caller-supplied, and a
    # default-constructed config still passes the explicit-'crt' validation
    # that rejects classic-only options. Both spellings are listed because
    # boto3 keeps its alias and the s3transfer name in step.
    DEFAULTS: dict[str, Any] = {  # noqa: RUF012 - ClassVar: boto3 declares it plainly
        **getattr(Boto3TransferConfig, "DEFAULTS", {}),
        "max_io_queue": _MAX_IO_QUEUE,
        "max_io_queue_size": _MAX_IO_QUEUE,
    }

    def __init__(
        self,
        multipart_threshold: int | None = None,
        max_concurrency: int | None = None,
        multipart_chunksize: int | None = None,
        num_download_attempts: int | None = None,
        max_io_queue: int | None = None,
        io_chunksize: int | None = None,
        use_threads: bool | None = None,
        max_bandwidth: int | None = None,
        preferred_transfer_client: str | None = None,
        *,
        target_bandwidth: int | None = None,
        should_stream: bool | None = None,
        disk_throughput: int | None = None,
        direct_io: bool | None = None,
        annotation_temp_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        """Forward classic values and retain library settings across SDK versions."""
        # Forward each base parameter only when set, letting the base ctor supply
        # its own default. On a DEFAULTS-less boto3 (1.28 - ~1.41) the base
        # signature carries concrete defaults; forwarding None would overwrite them and
        # reach s3transfer as None - a TypeError on the first size comparison, and
        # use_threads=None silently disables threading. On a modern boto3 an
        # omitted arg resolves to the same UNSET_DEFAULT sentinel a forwarded None
        # would, so behavior is identical there.
        base_kwargs: dict[str, object] = {
            name: value
            for name, value in (
                ("multipart_threshold", multipart_threshold),
                ("max_concurrency", max_concurrency),
                ("multipart_chunksize", multipart_chunksize),
                ("num_download_attempts", num_download_attempts),
                ("max_io_queue", max_io_queue),
                ("io_chunksize", io_chunksize),
                ("use_threads", use_threads),
                ("max_bandwidth", max_bandwidth),
            )
            if value is not None
        }
        if not _BASE_HAS_DEFAULTS and max_io_queue is None:
            # Floor boto3: no DEFAULTS table to override, so the base ctor's own
            # 100 would win. Forward the library default as if it were given.
            base_kwargs["max_io_queue"] = _MAX_IO_QUEUE
        if _BASE_ACCEPTS_PREFERRED_TRANSFER_CLIENT and preferred_transfer_client is not None:
            # Same only-when-set rule as the loop above: an omitted value lets
            # the base ctor supply its own default ("auto" / its UNSET
            # sentinel), so reading config.preferred_transfer_client sees the
            # base's semantics, not a None the base never chose.
            base_kwargs["preferred_transfer_client"] = preferred_transfer_client
        super().__init__(**base_kwargs)  # pyright: ignore[reportArgumentType]
        if not _BASE_ACCEPTS_PREFERRED_TRANSFER_CLIENT:
            # Floor boto3 has no such field; keep it readable via getattr.
            self.preferred_transfer_client = (
                "auto" if preferred_transfer_client is None else preferred_transfer_client
            )
        self.target_bandwidth = target_bandwidth
        self.should_stream = should_stream
        self.disk_throughput = disk_throughput
        self.direct_io = direct_io
        self.annotation_temp_dir = annotation_temp_dir
