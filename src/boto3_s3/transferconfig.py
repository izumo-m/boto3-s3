"""``TransferConfig`` - boto3's transfer configuration plus CRT tuning fields.

The library re-exports this subclass of ``boto3.s3.transfer.TransferConfig``
as its public ``TransferConfig``. The transfer engine honors
``preferred_transfer_client`` (``'auto'`` / ``'classic'`` / ``'crt'``) with
boto3's own semantics (``transfer.py`` / ``crtsupport.py``; design in
``docs/crt.md``); the constructor forwards it to the base class when that boto3
accepts it and otherwise keeps it as a plain attribute (the floor boto3 lacks
the field - see the signature probe below). The subclass only adds the
CRT tuning knobs aws-cli keeps in its ``[s3]`` runtime config but boto3 has
no field for:

- ``target_bandwidth`` - target throughput for the CRT engine in bytes per
  second (aws-cli ``target_bandwidth``; becomes the CRT client's
  ``target_throughput``). The classic engine's cap stays ``max_bandwidth``.
- ``should_stream`` / ``disk_throughput`` / ``direct_io`` - aws-cli's CRT
  file-I/O options. The pip ``s3transfer`` CRT client cannot apply them yet
  (no ``fio_options`` parameter as of 0.19); they are accepted now and passed
  through automatically once ``create_s3_crt_client`` grows support.

The extras are plain attributes (not part of the base ``DEFAULTS`` sentinel
machinery), default ``None`` = unset, and are ignored by the classic engine.
A plain ``boto3.s3.transfer.TransferConfig`` remains accepted everywhere
a config is taken - readers fall back to ``None`` for the extra fields.
"""

from __future__ import annotations

import inspect

from boto3.s3.transfer import TransferConfig as Boto3TransferConfig

__all__ = ["TransferConfig"]

# Back-compat (floor boto3 1.28, docs/overview.md section 2): boto3's CRT
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


class TransferConfig(Boto3TransferConfig):
    """boto3's ``TransferConfig`` with aws-cli's CRT tuning fields appended.

    The base parameters keep boto3's exact names, order, and semantics; the
    CRT extras are keyword-only and appended last, so existing boto3 code
    works unchanged.
    """

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
    ) -> None:
        """Forward explicit classic values and retain CRT settings across SDK versions."""
        # Forward each base parameter only when set, letting the base ctor supply
        # its own default. On the floor boto3 (1.28 - ~1.40) the base signature
        # carries concrete defaults; forwarding None would overwrite them and
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
        if _BASE_ACCEPTS_PREFERRED_TRANSFER_CLIENT:
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
