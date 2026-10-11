#!/usr/bin/env bash
# Clones the latest commit of every package in packages.txt into packages/ and
# decides which ones need a documentation build: those whose package.json
# version differs from the published one (versions.json of the site), or that
# are not published yet. FORCE=1 selects all of them.
#
# Writes packages/.todo (folders to build) and packages/.failed (packages that
# could not be cloned or read), and count=<number to build> to $GITHUB_OUTPUT
# (plus failed=... when nothing is built, so the failures are still reported).
#
# usage: plan.sh [published site folder]
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
python=python3
"$python" --version >/dev/null 2>&1 || python=python
site="${1:-}"

rm -rf "$root/packages"
mkdir -p packages
: > packages/.todo
: > packages/.failed

while read -r url branch; do
  case "$url" in ''|\#*) continue ;; esac
  dir="packages/$(basename "$url" .git)"
  if ! git clone --quiet --depth 1 ${branch:+--branch "$branch"} "$url" "$dir"; then
    echo "::error::Could not clone $url"
    echo "$url" >> packages/.failed
    continue
  fi
  if ! name=$("$python" scripts/prepare_docfx.py "$dir" --print-name) \
     || ! version=$("$python" scripts/prepare_docfx.py "$dir" --print-version); then
    echo "::error::Could not read the package in $url"
    echo "$url" >> packages/.failed
    continue
  fi

  published=""
  if [ -n "$site" ] && [ -f "$site/versions.json" ] && [ -d "$site/$name" ]; then
    published=$("$python" -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' \
      "$site/versions.json" "$name")
  fi

  if [ -z "${FORCE:-}" ] && [ "$published" = "$version" ]; then
    echo "$name $version: up to date"
  else
    echo "$name: ${published:-not published} -> $version"
    echo "$dir" >> packages/.todo
  fi
done < packages.txt

count=$(wc -l < packages/.todo | tr -d ' ')
echo "$count packages to build"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "count=$count" >> "$GITHUB_OUTPUT"
  [ "$count" != 0 ] || echo "failed=$(tr '\n' ' ' < packages/.failed)" >> "$GITHUB_OUTPUT"
fi
