# Streaming to and from objects

`IOStorage` presents a binary stream you supply as one side of a transfer, and
`TextIOStorage` a text stream, so an object can be read or written without a
temporary file. The other side is always S3.

```python
import gzip
import io
from boto3_s3 import S3, IOStorage, TextIOStorage

s3 = S3()

# upload from an open file
with open("hello.txt", "rb") as f:
    s3.cp(IOStorage(f), "s3://bucket/hello.txt")

# upload from a text buffer
s3.cp(TextIOStorage(io.StringIO("hello")), "s3://bucket/hello.txt")

# download straight into a gzip writer — no temporary file
with gzip.open("hello.txt.gz", "wb") as f:
    s3.cp("s3://bucket/hello.txt", IOStorage(f))
```

`StdioStorage()` is the shortcut for the process's own standard input and
output, the equivalent of `aws s3 cp - …` and `aws s3 cp … -`. A download
through it goes straight into `sys.stdout.buffer`, which is flushed when the
transfer finishes: a stdout that cannot take the bytes (a full disk, a reader
that left the pipe) fails the download. Bytes it refused can stay in the
buffer (a large write bypasses it, so not always); if they do, Python tries
them again when your program exits (exit status 120) unless you discard
them.

## 1. Binary or text: pick the class

`IOStorage` takes a binary stream — `io.BytesIO`, a file opened `"rb"` or
`"wb"`, a `gzip` writer, a pipe — and uses it as it is; one that cannot be
positioned (a pipe, a socket) is read through a view that offers `read` only,
so an upload buffers it the way `aws s3 cp - s3://...` buffers stdin, on
either engine. `TextIOStorage` takes a text stream and adds a codec:

```python
TextIOStorage(io.StringIO("hello"), encoding="utf-8")   # utf-8 is the default
```

The class says which kind you pass, and a type checker holds you to it:
`IOStorage` takes a stream whose `read` returns bytes, `TextIOStorage` one
whose `read` returns `str`. A stream whose class makes it the other kind is
also refused when you build the storage: `IOStorage(io.StringIO())` — an
`io.TextIOBase`, as a text-mode file and `sys.stdout` are — raises
`ValidationError` pointing you to `TextIOStorage`, and
`TextIOStorage(io.BytesIO())` — an `io.RawIOBase` or `io.BufferedIOBase`, as a
file opened `"rb"` or `"wb"` is — points you to `IOStorage`. No other stream
is checked: a text-mode `tempfile.NamedTemporaryFile` or
`SpooledTemporaryFile` (on Windows, `TemporaryFile` too) or a `codecs` stream,
none of which derives from those classes, and one of your own making pass, and what the wrong kind
leads to is not specified (an upload can stop responding). The type checker
catches them.

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
`BatchError`, whose `__cause__` is the item's `OpResult.error`: a
`Boto3S3Error` with the `UnicodeDecodeError` or `UnicodeEncodeError` on its
own `__cause__`, on either transfer engine.

For the bytes exactly as they are, hand over a binary stream instead — a file
opened `"rb"` or `"wb"` — or use `StdioStorage()` for standard input and
output. Text you have printed may still sit in `sys.stdout`; call `sys.stdout.flush()`
first, or it comes out after the object. Likewise, once you have read from
`sys.stdin` it may hold input read ahead of what it returned, and
`StdioStorage()` starts after that. `TextIOStorage` never swaps a text stream
for its `.buffer` behind your back for the same reason: text it has already
read ahead would be skipped, and the encoding it was opened with would be
ignored.

## 2. The stream stays yours

**`IOStorage` and `TextIOStorage` never close your stream.** Its lifecycle is
your business.

A download writes your stream in order, from where it stands, on either
transfer engine and whatever the stream is: a seekable file or buffer, an
append-mode file, a pipe, a `gzip` writer. The engine still fetches a large
object's parts in parallel and holds a part that arrives early until the ones
before it are written. The stream is not touched until the first byte
arrives, so a download that fails before that — a missing object, a cancel —
leaves it as it was; one that fails later leaves what was written so far.

Reading a download back therefore needs a rewind.

```python
buf = io.StringIO()
s3.cp("s3://bucket/hello.txt", TextIOStorage(buf))
buf.seek(0)
print(buf.read())        # or just: buf.getvalue()
```

A stream that cannot seek is fine — a pipe, a terminal, or the `gzip` writer
in the example above. There is nothing to rewind; the bytes go wherever the
stream sends them, and your own `with` block or `close()` finalizes it.

An upload reads a seekable stream from the start, at the offsets the engine
chooses; one that cannot be positioned (a pipe, a socket or a console counts as
one even where it claims otherwise — a Windows pipe answers `seekable()` with
`True`) is read in order, and buffered the way `aws s3 cp - s3://...` buffers
stdin.

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

## 4. What the stream must do

`IOStorage` and `TextIOStorage` rely on your stream behaving as Python's own
I/O streams do (the `io` module's contract) and do not check it: `read` returns
bytes (`str` for `TextIOStorage`) and an empty value at the end, `write` takes them,
and `seekable()`, where the stream has one, answers `True` or `False`. A
stream that breaks this is not supported: one whose `seekable()` raises, such
as a `tarfile` member read in stream mode (`"r|"`), fails its transfer.

What the stream raises fails the item. A `Boto3S3Error` it raises is recorded
as it is; any other exception becomes a `Boto3S3Error` with the original on
`__cause__`, and an interrupt (Ctrl-C, `SystemExit`) cancels the item and
reaches you. Where the SDK reads the stream itself (the classic engine hands
a seekable binary stream to the SDK as the request body), a failure there
takes the SDK's category instead, with the original further down the
exception chain.

## 5. A stream endpoint of your own

A source or destination that is not a stream you already hold — a file the
storage opens itself when the transfer needs it, a sink of your own — derives from
one of the two bases. `IOStorageBase` is for bytes: implement
`open(key, mode, *, size=None)`, returning a binary stream for `"rb"` (an
upload) or `"wb"` (a download). `TextIOStorageBase` is for text: implement
`open_text(key, mode)`, returning a text stream for `"r"` or `"w"`, and the
base converts it in the `encoding` you give its constructor. `key` (and
`open`'s `size`) carry nothing for a stream endpoint: ignore them.

```python
from boto3_s3 import TextIOStorageBase

class Cp932File(TextIOStorageBase):
    """A cp932 text file, stored as a utf-8 object."""

    def __init__(self, path):
        super().__init__(encoding="utf-8")        # the object's encoding
        self._path = path

    def open_text(self, key, mode):
        return open(self._path, mode, encoding="cp932", newline="")

s3.cp(Cp932File("report.txt"), "s3://bucket/report.txt")
s3.cp("s3://bucket/report.txt", Cp932File("copy.txt"))
```

The transfer closes what `open` or `open_text` returns once the item
settles, so a file opened there is closed for you; a stream that must stay
open goes back behind a wrapper whose `close` only flushes it. Either method
is called once per transfer, with a codec of its own on the text side, so one
instance can serve several transfers. Both bases leave the method abstract: a
subclass that does not implement it cannot be instantiated.

When the method runs depends on the direction. For a download it runs when the
first byte arrives (an empty object: when the download succeeds), so a missing
object never opens — and never empties — `copy.txt` above; the stream it
returns is written in order. For an upload it
runs as `cp` starts, before any request, and an exception it raises stops the
run then, as `aws s3 cp - …` stops on a missing stdin: `cp` raises a
`Boto3S3Error` with that exception as `__cause__` (one of the library's own
exceptions is raised as it is). What an upload's `open` returns reaches the
transfer engines as it is: one that offers `seek` / `tell` and answers
`seekable()` with `True` is read at offsets on the classic engine and has its
length probed through them on the CRT engine, so it must honour them — a pipe
does not. Return one that offers only `read` to have it read in order. The
text side needs none of this: its codec is read in order whatever `open_text`
returns.

How a download lands (section 2), where a stream may appear (section 3) and
what the stream must do (section 4) apply to such a storage as they do to
`IOStorage` and `TextIOStorage`.
