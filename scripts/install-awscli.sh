#!/usr/bin/env bash
# Install the aws-cli v2 that matches the vendored aws-cli submodule into
# .venv/bin, for the e2e parity suite (it finds `aws` via PATH). The live `aws`
# then matches the source the library is ported against - and the version the
# goldens are captured with - so parity is checked against one reference.
#
# Idempotent: a matching install is reused (no re-download). The target version
# defaults to the vendored submodule's, or pass one explicitly:
#
#     scripts/install-awscli.sh            # match vendor/aws-cli
#     scripts/install-awscli.sh 2.34.53    # a specific version
#
# Unlike `aws/install`, this just keeps the zip's self-contained `dist/` and
# symlinks the entry points - faster, and it installs no Python package, so
# .venv's environment is untouched.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
venv="$repo/.venv"
target=${1:-$(grep -m1 -Po "__version__ = '\K[0-9][^']*" "$repo/vendor/aws-cli/awscli/__init__.py")}

current=$("$venv/bin/aws" --version 2>/dev/null | grep -m1 -Po 'aws-cli/\K[0-9.]+' || true)
if [[ "$current" == "$target" ]]; then
    echo "aws-cli $target already installed at $venv/bin/aws"
    exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
url="https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${target}.zip"
echo "Downloading aws-cli $target (was: ${current:-none})"
curl -fsSL "$url" -o "$tmp/awscliv2.zip"
unzip -q -d "$tmp" "$tmp/awscliv2.zip"

# The zip's aws/dist/ is the self-contained onedir bundle; the entry points
# resolve their libs by realpath, so a symlink straight to dist/aws works.
dest="$venv/aws-cli/$target"
rm -rf "$dest"
mkdir -p "$dest" "$venv/bin"
cp -a "$tmp/aws/dist/." "$dest/"
ln -sfn "$dest/aws" "$venv/bin/aws"
ln -sfn "$dest/aws_completer" "$venv/bin/aws_completer"

echo "Installed: $("$venv/bin/aws" --version)"
