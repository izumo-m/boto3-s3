"""The tuned boto3 Session factory: fast response-timestamp parsing.

botocore parses every response timestamp (an S3 listing's per-object
``LastModified`` included) through dateutil's generic date parser, which
dominates the CPU cost of large listings - about two thirds of a 100k-object
``ls``, on our side and aws-cli's alike (its bundled botocore is the same
code). `fast_parse_timestamp` short-cuts the ISO 8601 form S3 actually sends
through the C `datetime.fromisoformat`, and `session` returns a
`boto3.Session` whose clients parse through it - installed via botocore's
public ``ResponseParserFactory.set_parser_defaults`` seam on a fresh
botocore session, before any client exists.

The recommended construction is ``S3(session=boto3_s3.session())``. The
zero-config ``S3()`` deliberately stays plain ``boto3.client("s3")``
semantics: it rides the process-wide ``boto3.DEFAULT_SESSION`` exactly as
``boto3.client`` itself does (created on demand, shared), and boto3-s3
never *mutates* that session's parser factory - a mutation would retrofit
every client the application built from it (each client's endpoint holds
its session's factory and creates parsers per response), a global side
effect deliberately not taken. So unrelated boto3 use elsewhere in the
process never changes boto3-s3's parsing, and boto3-s3 never changes the
application's - in either direction.

This module is SDK-backed by declaration (docs/imports.md): it imports
boto3 at module top.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import boto3
import botocore.session
from botocore.utils import parse_timestamp


def fast_parse_timestamp(value: Any) -> datetime:
    """botocore's `parse_timestamp` with a C fast path for ISO 8601 strings.

    The ISO 8601 timestamps S3 sends (``2026-07-14T11:57:42.000Z``) parse
    through `datetime.fromisoformat` (~100x faster than the dateutil walk
    botocore falls back to); everything else - RFC 822 header dates, epoch
    numbers, a lowercase ``z`` suffix - falls through to botocore's own
    `parse_timestamp` untouched. The fast path requires a ``-`` in the
    string: a digit-only string like ``"20200101"`` is an epoch-seconds
    value to botocore, while Python 3.11+'s `fromisoformat` would read it
    as a basic-format date - the guard keeps such inputs on botocore's
    interpretation on every Python. For every input both paths accept the
    returned value is equal; only the tzinfo class differs
    (`datetime.timezone.utc` instead of dateutil's ``tzutc``), which
    compares, subtracts, and formats identically.
    """
    if isinstance(value, str) and "-" in value:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    return parse_timestamp(value)


def session(**kwargs: Any) -> boto3.session.Session:
    """A new `boto3.Session` whose clients parse timestamps via the fast path.

    Configuration semantics are exactly ``boto3.Session(**kwargs)``'s - the
    keyword arguments (``profile_name`` / ``region_name`` / credentials / ...)
    are forwarded verbatim, except ``botocore_session``: this factory
    supplies its own fresh botocore session (passing one raises the
    duplicate-argument ``TypeError``), and boto3 applies
    its usual user-agent branding. The one difference is the response
    parser default, registered before any client is built so every client
    later created from this session inherits `fast_parse_timestamp`. This
    library never retrofits a session it does not own; callers managing
    their own botocore session can register the same default on its
    ``response_parser_factory`` component themselves (on current botocore
    that reaches even the session's already-built clients - their endpoints
    hold the factory and create parsers per response).
    """
    botocore_session = botocore.session.Session()
    botocore_session.get_component("response_parser_factory").set_parser_defaults(
        timestamp_parser=fast_parse_timestamp
    )
    return boto3.session.Session(botocore_session=botocore_session, **kwargs)


__all__ = ["fast_parse_timestamp", "session"]
