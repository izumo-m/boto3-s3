# API reference

The per-symbol contract of the `boto3-s3` library: what each class, function,
enum and callback alias accepts, what it guarantees, and what it raises. The
narrative guide — how the pieces fit together, with worked examples — is
[`../library/README.md`](../library/README.md). The `boto3-s3` command ships in
a separate package and is documented in [`../cli/README.md`](../cli/README.md);
nothing on these pages is about the command.

Coverage follows one rule: every name importable from the package root — the
contents of `boto3_s3.__all__` — has an entry in the symbol index at the end of
this page. Names that are public under a submodule only, such as the rest of
the pattern engine in `boto3_s3.globsieve`, are named on the page that covers
their area but are not indexed here.

## Pages

- [`s3.md`](./s3.md) — the `S3` object: its constructor, the `client` /
  `resolve` / `aws_config` seams, thread safety, and subclassing.
- [`operations/README.md`](./operations/README.md) — what the nine operations
  share: the location forms they accept, the module-level functions that wrap
  the methods, the parameters that recur across them, and the end-of-run error
  model. One page each — [`cp`](./operations/cp.md),
  [`ls`](./operations/ls.md), [`mb`](./operations/mb.md),
  [`mv`](./operations/mv.md), [`presign`](./operations/presign.md),
  [`rb`](./operations/rb.md), [`rm`](./operations/rm.md),
  [`sync`](./operations/sync.md), [`website`](./operations/website.md).
- [`options.md`](./options.md) — `TransferOptions`, `TransferConfig`, the
  `ScanOptions` family, and the mode enums they carry.
- [`results.md`](./results.md) — `OpResult` and `OpOutcome`, transfer progress,
  the listing entry types, the callback aliases, and `CancelToken`.
- [`exceptions.md`](./exceptions.md) — `Boto3S3Error` and its subclasses: what
  each one means, when it is raised, and what it carries.
- [`filters.md`](./filters.md) — `FileFilter`, and the `GlobFilter` /
  `GlobPattern` include/exclude implementation of one.
- [`comparator.md`](./comparator.md) — the `sync` merge-join, the pair shapes
  it emits, the update strategies, and the predicate combinators.
- [`storage.md`](./storage.md) — the `Storage` contract, the capabilities that
  gate it, the local / S3 / stream backends, and the local walk's override
  seams.
- [`misc.md`](./misc.md) — the tuned session, the AWS config-file reader,
  masked debug logging, access-point path resolution, batch deletion, and
  `__version__`.

## Symbol index

Every name in `boto3_s3.__all__`, alphabetically. The nine operation entries
point at the `S3` method that carries the contract; the module-level function
that wraps it is documented on the same page.

- [`__version__`](./misc.md#__version__)
- [`AccessDeniedError`](./exceptions.md#accessdeniederror)
- [`all_of`](./comparator.md#all_of)
- [`AnnotationCopyMode`](./options.md#annotationcopymode)
- [`any_of`](./comparator.md#any_of)
- [`AwsCliComparison`](./comparator.md#awsclicomparison)
- [`AwsConfig`](./misc.md#awsconfig)
- [`BatchError`](./exceptions.md#batcherror)
- [`Boto3S3Error`](./exceptions.md#boto3s3error)
- [`CancelledError`](./exceptions.md#cancellederror)
- [`CancelMode`](./results.md#cancelmode)
- [`CancelToken`](./results.md#canceltoken)
- [`CaseConflictMode`](./options.md#caseconflictmode)
- [`ChecksumComparison`](./comparator.md#checksumcomparison)
- [`Comparator`](./comparator.md#comparator)
- [`ConfigSection`](./misc.md#configsection)
- [`ConfigurationError`](./exceptions.md#configurationerror)
- [`CopyPropsMode`](./options.md#copypropsmode)
- [`cp`](./operations/cp.md#s3cp)
- [`DestOnlyPair`](./comparator.md#destonlypair)
- [`EtagComparison`](./comparator.md#etagcomparison)
- [`fast_parse_timestamp`](./misc.md#fast_parse_timestamp)
- [`FileFilter`](./filters.md#filefilter)
- [`FileInfo`](./results.md#fileinfo)
- [`FileKind`](./results.md#filekind)
- [`GlobFilter`](./filters.md#globfilter)
- [`GlobPattern`](./filters.md#globpattern)
- [`has_underlying_s3_path`](./misc.md#has_underlying_s3_path)
- [`InvalidConfigError`](./exceptions.md#invalidconfigerror)
- [`InvalidValueError`](./exceptions.md#invalidvalueerror)
- [`IOStorage`](./storage.md#iostorage)
- [`ListingCallback`](./results.md#listingcallback)
- [`LocalFileGenerator`](./storage.md#localfilegenerator)
- [`LocalFileInfo`](./results.md#localfileinfo)
- [`LocalScanOptions`](./options.md#localscanoptions)
- [`LocalStorage`](./storage.md#localstorage)
- [`Location`](./storage.md#location)
- [`LoopDetector`](./storage.md#loopdetector)
- [`ls`](./operations/ls.md#s3ls)
- [`mb`](./operations/mb.md#s3mb)
- [`MergedPair`](./comparator.md#mergedpair)
- [`mv`](./operations/mv.md#s3mv)
- [`NotFoundError`](./exceptions.md#notfounderror)
- [`OpOutcome`](./results.md#opoutcome)
- [`OpResult`](./results.md#opresult)
- [`PairFilter`](./comparator.md#pairfilter)
- [`ParallelFilter`](./comparator.md#parallelfilter)
- [`PatternKind`](./filters.md#patternkind)
- [`presign`](./operations/presign.md#s3presign)
- [`ProgressCallback`](./results.md#progresscallback)
- [`rb`](./operations/rb.md#s3rb)
- [`ResultCallback`](./results.md#resultcallback)
- [`rm`](./operations/rm.md#s3rm)
- [`rm_filter_root`](./operations/rm.md#rm_filter_root)
- [`S3`](./s3.md#s3)
- [`S3_DELETE_BATCH`](./misc.md#s3_delete_batch)
- [`S3Deleter`](./misc.md#s3deleter)
- [`S3FileInfo`](./results.md#s3fileinfo)
- [`S3PathResolver`](./misc.md#s3pathresolver)
- [`S3ScanOptions`](./options.md#s3scanoptions)
- [`S3Storage`](./storage.md#s3storage)
- [`ScanOptions`](./options.md#scanoptions)
- [`session`](./misc.md#session)
- [`set_stream_logger`](./misc.md#set_stream_logger)
- [`SrcOnlyPair`](./comparator.md#srconlypair)
- [`StdioStorage`](./storage.md#stdiostorage)
- [`Storage`](./storage.md#storage)
- [`StorageCapability`](./storage.md#storagecapability)
- [`sync`](./operations/sync.md#s3sync)
- [`SyncPair`](./comparator.md#syncpair)
- [`TransferConfig`](./options.md#transferconfig)
- [`TransferOptions`](./options.md#transferoptions)
- [`TransferProgress`](./results.md#transferprogress)
- [`TransferType`](./results.md#transfertype)
- [`TransportError`](./exceptions.md#transporterror)
- [`ValidationError`](./exceptions.md#validationerror)
- [`WalkChild`](./storage.md#walkchild)
- [`website`](./operations/website.md#s3website)
