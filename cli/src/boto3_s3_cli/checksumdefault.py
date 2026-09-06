"""aws's default request checksum, reproduced on the CLI's own clients.

aws v2 ships a botocore whose ``DEFAULT_CHECKSUM_ALGORITHM`` is ``CRC64NVME``;
pip botocore's is ``CRC32``. Neither tool's own code picks the algorithm:
botocore stamps its default on every request whose operation names a
``ChecksumAlgorithm`` member (PutObject, UploadPart, PutObjectAnnotation,
DeleteObjects, the bucket configuration puts) when the client's
``request_checksum_calculation`` is ``when_supported`` - the default - and
the caller named none; s3transfer copies the same constant into an upload's
arguments (its ``set_default_checksum_algorithm``), and aws's bundled
s3transfer has ``CRC64NVME`` written into its CRT module where pip's has
``CRC32``. So ``aws s3 cp`` leaves CRC64NVME full-object checksums on what it
uploads and sends CRC64NVME on a delete or a bucket put, and the same
commands here left CRC32 (docs/cli/aws-differences.md used to record it).

The CLI reproduces aws's value in two places, both on the CLI side - the
library keeps boto3's defaults (design/crt.md section 1):

- `default_algorithm` feeds the transfer options of an upload run
  (`transferargs.default_upload_checksum`), so both transfer engines name it:
  the classic engine hands it to botocore, and the CRT engine decides its
  trailing checksum from that argument alone.
- `register` stamps it on every other request a built client sends where
  botocore would have stamped its own default, at ``provide-client-params``,
  before botocore's own resolution sees the parameters.

Nothing process-global is patched: an application embedding the library keeps
its botocore's default. The value applies only where botocore can compute it
(CRC64NVME needs awscrt; without it botocore's own default stands) and only
where botocore would have stamped a default at all (a ``when_required`` client
sends no checksum on either tool; a presigned request gets none).

This is not cosmetic: aws-checksums computes CRC32 in software on x86-64
(about 3 GiB/s against about 20 for CRC64NVME), which on a 4-vCPU instance
pushing 800 MiB/s over the CRT engine was 10% of a 1 GB upload's wall time
against real S3 (benchmarks/RESULTS.md, 2026-09-06).
"""

from __future__ import annotations

from typing import Any

# aws v2's bundled botocore ``DEFAULT_CHECKSUM_ALGORITHM``.
AWS_DEFAULT_CHECKSUM_ALGORITHM = "CRC64NVME"


def default_algorithm(client: Any) -> str | None:
    """aws's default for this client, or None where botocore would send no
    default checksum (``request_checksum_calculation`` other than
    ``when_supported``, or a botocore that predates the setting) or cannot
    compute CRC64NVME (no awscrt, or one too old; botocore's own registry is
    the authority on that, so it is read rather than re-derived)."""
    if getattr(client.meta.config, "request_checksum_calculation", None) != "when_supported":
        return None
    from botocore import httpchecksum

    supported = getattr(httpchecksum, "_SUPPORTED_CHECKSUM_ALGORITHMS", ())
    if AWS_DEFAULT_CHECKSUM_ALGORITHM.lower() not in supported:
        return None
    return AWS_DEFAULT_CHECKSUM_ALGORITHM


def register(client: Any) -> None:
    """Attach aws's default to one S3 client (`clientfactory.build_client`).

    Stamps the algorithm on any request whose operation has a
    ``requestAlgorithmMember`` and whose caller left it unset - the same test
    botocore applies before stamping its own default - so the CLI's deletes,
    bucket puts and annotation writes carry what aws's do. An explicit value
    (``--checksum-algorithm``, or the transfer default) is left alone.
    """
    algorithm = default_algorithm(client)
    if algorithm is None:
        return

    def stamp(
        params: dict[str, Any], model: Any, context: dict[str, Any] | None = None, **_: Any
    ) -> None:
        if context and context.get("is_presign_request"):
            return
        member = model.http_checksum.get("requestAlgorithmMember")
        if member and member not in params:
            params[member] = algorithm

    client.meta.events.register("provide-client-params.s3.*", stamp)
