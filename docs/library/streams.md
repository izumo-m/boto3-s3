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

# download straight into a gzip writer — no temporary file, no seeking
with gzip.open("hello.txt.gz", "wb") as f:
    s3.cp("s3://bucket/hello.txt", IOStorage(f))
```

`StdioStorage()` is the shortcut for the process's own standard input and
output, the equivalent of `aws s3 cp - …` and `aws s3 cp … -`.

## 1. Text streams are encoded for you

A binary stream — `io.BytesIO`, a file opened `"rb"` or `"wb"`, a `gzip` writer,
a pipe — is used as it is. A text stream is wrapped with a codec:

```python
IOStorage(io.StringIO("hello"), encoding="utf-8")   # utf-8 is the default
```

Uploads encode, downloads decode.

## 2. The stream stays yours

**`IOStorage` never closes your stream and never repositions it.** Its lifecycle
and its final position are your business.

The practical consequence is on downloads: afterwards the stream sits at the end
of the bytes just written, so reading them back needs a rewind.

```python
buf = io.StringIO()
s3.cp("s3://bucket/hello.txt", IOStorage(buf))
buf.seek(0)
print(buf.read())        # or just: buf.getvalue()
```

A stream that cannot seek is fine — a `gzip` writer, `sys.stdout`, a pipe.
There is nothing to rewind; the bytes go wherever the stream sends them, and
your own `with` block or `close()` finalizes it.

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
knowable.
