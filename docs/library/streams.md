# Streaming to and from objects

`IOStorage` presents a stream you supply as one side of a transfer, so an object
can be read or written without a temporary file. The other side is always S3.

```python
import gzip
import io
from boto3_s3 import S3, IOStorage

s3 = S3()

# upload from an open file
with open("hello.txt", "rb") as f:
    s3.cp(IOStorage(f), "s3://bucket/hello.txt")

# upload from a text buffer
s3.cp(IOStorage(io.StringIO("hello")), "s3://bucket/hello.txt")

# download straight into a gzip writer — no temporary file. A gzip writer
# reports seekable() but seeks only forward, so hand over just its write():
# a stream without seek is written strictly in order.
class WriteOnly:
    def __init__(self, stream):
        self.write = stream.write

with gzip.open("hello.txt.gz", "wb") as f:
    s3.cp("s3://bucket/hello.txt", IOStorage(WriteOnly(f)))
```

`StdioStorage()` is the shortcut for the process's own standard input and
output, the equivalent of `aws s3 cp - …` and `aws s3 cp … -`. A download
through it goes straight into `sys.stdout.buffer` and is not flushed when the
transfer finishes, exactly as `aws` leaves it — whatever the stream still holds
goes out on your own `flush()` or on the interpreter's at exit.

## 1. Text streams carry the object as text

A binary stream — `io.BytesIO`, a file opened `"rb"` or `"wb"`, a `gzip` writer,
a pipe — is used as it is; one that cannot be positioned (a pipe, a socket) is
read through a view that offers `read` only, so an upload buffers it the way
`aws s3 cp - s3://...` buffers stdin, on either engine. A text stream is
wrapped with a codec:

```python
IOStorage(io.StringIO("hello"), encoding="utf-8")   # utf-8 is the default
```

Uploads encode, downloads decode, so a text stream carries the object as text
and its own rules apply on top. A text-mode file reads and writes in the
encoding it was opened with and translates newlines as its `newline=` says: by
default it reads `\r\n` and `\r` as `\n` on every platform, and on Windows
writes each `\n` as `\r\n`. `sys.stdin` and `sys.stdout` translate on Windows
only; `io.StringIO()` translates nothing.

Text that will not convert fails the transfer: on a download, an object that
is not valid text in `encoding` (an image, under utf-8) or a character the
stream cannot write in the encoding it was opened with; on an upload, bytes the
stream cannot read in that encoding (a text-mode `open("photo.jpg")`) or a
character `encoding` cannot encode. A failed download leaves in the stream
whatever was decoded before the failure. `cp` (or `mv`) then raises
`BatchError`, whose `__cause__` is the item's `OpResult.error`; on a download
that is a `Boto3S3Error` with the `UnicodeDecodeError` or
`UnicodeEncodeError` on its own `__cause__`.

For the bytes exactly as they are, hand over a binary stream instead — a file
opened `"rb"` or `"wb"` — or use `StdioStorage()` for standard input and
output. Prefer `StdioStorage()` to `IOStorage(sys.stdout.buffer)`: stdout
redirected to a file with `>` can seek, so the classic engine writes that
buffer from offset 0, over whatever the file already holds (section 2). Text
you have printed may still sit in `sys.stdout`; call `sys.stdout.flush()`
first, or it comes out after the object. Likewise, once you have read from
`sys.stdin` it may hold input read ahead of what it returned, and
`StdioStorage()` starts after that. A text stream is never swapped for its
`.buffer` behind your back for the same reason: text it has already read ahead
would be skipped, and the encoding it was opened with would be ignored.

## 2. The stream stays yours

**`IOStorage` never closes your stream.** Its lifecycle is your business.
Where a download writes into it depends on the stream and the engine: on the
classic transfer engine a seekable binary stream is written at absolute offsets
from 0, so whatever position it held beforehand is ignored, and a multipart
download can leave it anywhere. A text stream, a stream that cannot seek
(a pipe, a socket or a console counts as one even where it claims otherwise —
a Windows pipe answers `seekable()` with `True`), a
binary stream in append mode (`open(path, "ab")`, or a `>>` redirected
stdout's `.buffer` — every write lands at its end, so it is written in order
rather than at offsets that would scramble a multipart download), and any
stream under the CRT engine are written in order from where they stand.

Append mode is recognized from the stream's `mode` string and, on POSIX, from
the descriptor's own flag — which is how the `>>` redirect is caught, its
`mode` being `"wb"`. Windows gives no way to ask a descriptor, so there only
the `mode` string counts: a descriptor opened with `O_APPEND` under another
mode is written at offsets and comes out scrambled, and a `>>` redirect from
`cmd` is not an append stream at all — just a seekable one positioned at the
end, written from offset 0 like any other, over what the file held. Open the
file with `"ab"` yourself, or use the CRT engine, when you need appending
there.

Either way, reading a download back needs a rewind.

```python
buf = io.StringIO()
s3.cp("s3://bucket/hello.txt", IOStorage(buf))
buf.seek(0)
print(buf.read())        # or just: buf.getvalue()
```

A stream that cannot seek is fine — a pipe, a terminal, or a writer that
exposes only `write`, as in the `gzip` example above. There is nothing to
rewind; the bytes go wherever the stream sends them, and your own `with` block
or `close()` finalizes it. A `gzip` writer handed over directly is not such a
stream: it reports itself seekable but seeks only forward, so a download large
enough to go multipart can fail on it. `StdioStorage()` writes standard output
in order too, even when `>` has sent it to a regular file that can seek.

## 3. Where a stream may appear

A stream is a single endpoint, not a container: it holds one object's worth of
bytes and cannot be listed. It may be:

- **either side of a non-recursive `cp`**
- **the destination of a non-recursive `mv`** — the bytes are written to the
  stream, then the S3 source is deleted

It may not be:

- a `mv` **source**, since a move deletes its source and a stream is not
  something that can be deleted
- a recursive `mv` destination
- a target of `ls` or `rm`
- both sides at once — one side must be S3

`S3.mv` rejects the two `mv` cases with `ValidationError`.

Because nothing is listed, the records a streaming `cp` produces carry no
`FileInfo` on either side — `src_info` and `dest_info` are both `None` — and the
stream endpoint displays as `-`. See [`results.md`](./results.md).

A few transfer options do not apply. `recursive` is rejected, as is
`no_overwrite` on a download, since there is no destination to check for
existence. `filter` is ignored by a streaming `cp`, which takes a dedicated
path with nothing to enumerate; a `mv` onto a stream does not take that path,
so its `filter` still runs — against the one source entry, which it can
therefore drop. `expected_size` applies to uploads only, where it lets
multipart be planned in advance for a stream whose length is not otherwise
knowable (a non-seekable one; a seekable stream is sized by the engine itself
and the hint is ignored, so it never shortens what is sent).
