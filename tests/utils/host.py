"""Host-environment probes shared across suites.

Some test preconditions cannot be staged on every host. `chmod(0)` revokes
nothing on Windows (POSIX mode bits map only to the read-only attribute) and
nothing for root (which reads and deletes anything) - aws-cli's own suite
skips its unreadable-path tests the same way (`skip_if_windows`). Filesystem
case-sensitivity follows the filesystem, not the OS (macOS APFS and Windows
NTFS fold case, WSL2's `/mnt/c` too), so it is probed, never inferred from
`os.name`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# chmod-denial tests: unreadable files/dirs, undeletable entries.
skip_if_chmod_is_inert = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod cannot revoke access (Windows or root)",
)


def is_case_insensitive(path: Path) -> bool:
    """Whether `path`'s filesystem resolves names case-insensitively (probe)."""
    probe = path / "CaseProbe.tmp"
    probe.write_bytes(b"")
    try:
        return (path / "caseprobe.tmp").exists()
    finally:
        probe.unlink()
