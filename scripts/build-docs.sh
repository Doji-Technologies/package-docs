#!/usr/bin/env bash
# Builds the docfx site of every package in packages.txt into build/<package name>/.
# Each repository is cloned at its latest commit into packages/.
# A package that fails does not stop the others; the failures are listed at the
# end and in $GITHUB_OUTPUT (failed=...). The exit code is 0 unless nothing
# could be built.
#
# usage: build-docs.sh [local package repository ...]   (default: clone packages.txt)
# env:   UNITY_REFS_DIR          cache folder (default: .unity-refs)
#        UNITY_EDITOR_MANAGED    use a local Editor/Data/Managed instead of Docker
#        SKIP_EDITOR_FETCH=1     do not try to fetch Editor assemblies
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
export UNITY_REFS_DIR="${UNITY_REFS_DIR:-$root/.unity-refs}"
python=python3
"$python" --version >/dev/null 2>&1 || python=python

failed=()
packages=()
if [ "$#" -gt 0 ]; then
  packages=("$@")
else
  while read -r url branch; do
    case "$url" in ''|\#*) continue ;; esac
    dir="packages/$(basename "$url" .git)"
    rm -rf "$root/$dir"
    if git clone --quiet --depth 1 ${branch:+--branch "$branch"} "$url" "$dir"; then
      packages+=("$dir")
    else
      echo "::error::Could not clone $url"
      failed+=("$url")
    fi
  done < packages.txt
fi
total=$((${#packages[@]} + ${#failed[@]}))

build_package() {
  local path="$1" unity name config
  [ -f "$path/docs/docfx.json" ] || { echo "$path/docs/docfx.json not found" >&2; return 1; }

  unity=$("$python" scripts/prepare_docfx.py "$path" --print-unity) || return 1
  name=$("$python" scripts/prepare_docfx.py "$path" --print-name) || return 1

  if [ -z "${UNITY_EDITOR_MANAGED:-}" ] && [ -z "${SKIP_EDITOR_FETCH:-}" ]; then
    bash scripts/fetch-unity-editor-refs.sh "$unity" "$UNITY_REFS_DIR" \
      || echo "::warning::$name: no Unity $unity Editor assemblies; Unity types stay unresolved"
  fi

  config=$("$python" scripts/prepare_docfx.py "$path") || return 1
  rm -rf "$path/docs/_site"
  docfx "$config" || return 1

  rm -rf "build/$name"
  cp -r "$path/docs/_site" "build/$name" || return 1
  echo "$name" >> build/.built
}

mkdir -p build
rm -f build/.built
for path in "${packages[@]}"; do
  echo "::group::$path"
  if build_package "$path"; then
    echo "::endgroup::"
  else
    echo "::endgroup::"
    echo "::error::Documentation build failed for $path"
    failed+=("$path")
  fi
done

cp index.html build/index.html

# Old URLs: "<old folder> <new folder>" per line.
if [ -f redirects.txt ]; then
  while read -r from to; do
    case "$from" in ''|\#*) continue ;; esac
    [ -e "build/$from" ] && continue
    mkdir -p "build/$from"
    cat > "build/$from/index.html" <<EOF
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="0; url=../$to/">
  <link rel="canonical" href="../$to/">
  <title>Redirecting...</title>
</head>
<body>
  <p>This documentation moved to <a href="../$to/">$to</a>.</p>
</body>
</html>
EOF
  done < redirects.txt
fi

built=0
[ -f build/.built ] && built=$(wc -l < build/.built)
rm -f build/.built
echo "Built $built of $total packages. Failed: ${failed[*]:-none}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "failed=${failed[*]:-}" >> "$GITHUB_OUTPUT"
fi
[ "$built" -gt 0 ]
