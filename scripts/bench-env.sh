# scripts/bench-env.sh - select the benchmark lane's interpreter and environment.
#
# Source this file before the benchmark commands (it only exports environment
# variables, no side effects):
#
#   source scripts/bench-env.sh
#   uv sync --all-packages --locked    # provisions .venv-bench on first use
#   scripts/install-awscli.sh          # pinned aws -> .venv-bench/bin
#   source scripts/minio-env.sh
#   uv run python -m benchmarks run
#
# The lane runs on Python 3.14, the interpreter the pinned aws-cli bundles, so
# the E2E differential compares two tools rather than two interpreters
# (design/benchmark.md "Interpreter"). Development stays on the 3.10 floor in
# .venv (.python-version), so the lane gets an environment of its own:
# UV_PROJECT_ENVIRONMENT redirects uv sync/run there, UV_PYTHON overrides the
# .python-version pin, and the managed-only preference keeps the interpreter's
# build source the same as every other lane (a distro python3.14 would be a
# second variable). BOTO3_S3_BENCH_PYTHON selects another version for a
# one-off run; it gets its own environment (.venv-bench-<version>) so the
# default one is not rebuilt, and the results meta records what actually ran.

export UV_PROJECT_ENVIRONMENT=.venv-bench${BOTO3_S3_BENCH_PYTHON:+-$BOTO3_S3_BENCH_PYTHON}
export UV_PYTHON=${BOTO3_S3_BENCH_PYTHON:-3.14}
export UV_PYTHON_PREFERENCE=only-managed
