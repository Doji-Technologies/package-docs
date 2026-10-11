#!/usr/bin/env bash
# Copies the packages built by build-docs.sh (build/<package name>/) into a
# checkout of the published site. Only the folders of these packages are
# replaced; all other packages keep their published documentation.
#
# usage: publish.sh <site folder>
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
python=python3
"$python" --version >/dev/null 2>&1 || python=python
site="${1:?usage: publish.sh <site folder>}"
mkdir -p "$site"

while read -r name version; do
  [ -n "$name" ] || continue
  rm -rf "${site:?}/${name:?}"
  cp -r "build/$name" "$site/$name"
  echo "Published $name $version"
done < build/.built

# The published version of every package; plan.sh compares against it.
"$python" - "$site/versions.json" build/.built <<'PY'
import json, os, sys
path, built = sys.argv[1], sys.argv[2]
versions = json.load(open(path)) if os.path.exists(path) else {}
for line in open(built):
    if line.strip():
        name, version = line.split()
        versions[name] = version
with open(path, "w", newline="\n") as f:
    json.dump(dict(sorted(versions.items())), f, indent=2)
    f.write("\n")
PY

cp index.html CNAME "$site/"
touch "$site/.nojekyll"  # docfx output has folders that Jekyll would drop

# Old URLs: "<old folder> <new folder>" per line.
while read -r from to; do
  case "$from" in ''|\#*) continue ;; esac
  [ -e "$site/$from" ] && [ ! -f "$site/$from/.redirect" ] && continue
  mkdir -p "$site/$from"
  touch "$site/$from/.redirect"
  cat > "$site/$from/index.html" <<HTML
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
HTML
done < redirects.txt
