"""boto3-s3-cli - `aws s3` compatible CLI built on the boto3-s3 library."""

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    __version__: str

__all__ = ["__version__"]

# botocore reads BOTO_DISABLE_CRT once, while ``botocore.compat`` is imported,
# and freezes ``HAS_CRT`` from what it finds there. The botocore aws ships
# carries no such switch, so the variable decides nothing on that side - an
# MRAP presign still signs with SigV4a and the CRT checksum families still
# work, with the variable set or not (measured). Dropping it here reproduces
# that: this package's ``__init__`` runs before any module of it, so nothing
# can have reached botocore yet, and what the environment says about CRT
# reaches botocore no more than it reaches aws's.
#
# The removed value is kept rather than discarded, because a child process
# should still see what its parent's environment said: aws ignores the
# variable itself but passes it on untouched (measured), which `child_environ`
# reproduces for the external aliases this CLI launches.
_DISABLE_CRT_ENV = "BOTO_DISABLE_CRT"
_disabled_crt = os.environ.pop(_DISABLE_CRT_ENV, None)


def child_environ() -> dict[str, str]:
    """The environment a process this CLI launches should inherit.

    ``os.environ`` with this package's own edit undone - the BOTO_DISABLE_CRT
    the import above removed goes back - so a child sees what aws would have
    handed it.
    """
    environ = dict(os.environ)
    if _disabled_crt is not None:
        environ[_DISABLE_CRT_ENV] = _disabled_crt
    return environ


def __getattr__(name: str) -> Any:
    """Resolve ``__version__`` on first access (PEP 562).

    importlib.metadata costs ~20ms to import; deferring it avoids that cost
    until the version is requested.
    """
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            value = version("boto3-s3-cli")
        except PackageNotFoundError:  # pragma: no cover - only from an unbuilt checkout
            value = "0.0.0+unknown"
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
