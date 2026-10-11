#!/usr/bin/env bash
# Builds the docfx site of each package into build/<package name>/.
# A package that fails does not stop the others; the failures are listed at the
# end and in $GITHUB_OUTPUT (failed=...). The exit code is always 0.
#
# usage: build-docs.sh [local package repository ...]
#        Without arguments: the packages selected by plan.sh (packages/.todo),
#        or all of packages.txt when plan.sh did not run yet.
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
  [ -f packages/.todo ] || FORCE=1 bash scripts/plan.sh
  mapfile -t packages < packages/.todo
  mapfile -t failed < packages/.failed
fi
total=$((${#packages[@]} + ${#failed[@]}))

build_package() {
  local path="$1" unity name version config
  [ -f "$path/docs/docfx.json" ] || { echo "$path/docs/docfx.json not found" >&2; return 1; }

  unity=$("$python" scripts/prepare_docfx.py "$path" --print-unity) || return 1
  version=$("$python" scripts/prepare_docfx.py "$path" --print-version) || return 1
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
  echo "$name $version" >> build/.built
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

built=0
[ -f build/.built ] && built=$(wc -l < build/.built | tr -d ' ')
echo "Built $built of $total packages. Failed: ${failed[*]:-none}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "built=$built" >> "$GITHUB_OUTPUT"
  echo "failed=${failed[*]:-}" >> "$GITHUB_OUTPUT"
fi
