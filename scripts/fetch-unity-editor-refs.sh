#!/usr/bin/env bash
# Copies the Unity Editor's managed reference assemblies out of a GameCI Editor
# image into <refs>/editor/<unity>/UnityEngine/. The Editor is never started, so
# no Unity license is needed. The assemblies are Unity's property: keep them in
# the (private) CI cache, never commit or publish them.
#
# usage: fetch-unity-editor-refs.sh <unity major.minor> [refs dir]
# env:   UNITY_EDITOR_IMAGE  full image name, skips the Docker Hub tag lookup
set -euo pipefail

unity="$1"
refs="${2:-${UNITY_REFS_DIR:-$(dirname "$0")/../.unity-refs}}"
dest="$refs/editor/$unity"

if [ -f "$dest/UnityEngine/UnityEngine.CoreModule.dll" ]; then
  echo "Unity $unity reference assemblies: cached ($(cat "$dest/image.txt" 2>/dev/null))"
  exit 0
fi

image="${UNITY_EDITOR_IMAGE:-}"
if [ -z "$image" ]; then
  # Newest patch release of this Unity version with a Linux "base" image.
  # Docker Hub lists tags newest first; take the highest patch on the first page that has one.
  python=python3
  "$python" --version >/dev/null 2>&1 || python=python
  url="https://hub.docker.com/v2/repositories/unityci/editor/tags?page_size=100&name=ubuntu-$unity."
  tag=""
  for _ in 1 2 3 4 5; do
    page=$(curl -fsSL --retry 3 "$url")
    { read -r tag; read -r url; } < <(printf '%s' "$page" | "$python" -c '
import json, re, sys
pattern = re.compile(r"^ubuntu-" + re.escape(sys.argv[1]) + r"\.(\d+)f(\d+)-base-(\d+)$")
page = json.load(sys.stdin)
tags = [(tuple(map(int, m.groups())), m.group(0)) for m in map(pattern.match, (r["name"] for r in page["results"])) if m]
print(max(tags)[1] if tags else "")
print(page.get("next") or "")
' "$unity")
    if [ -n "$tag" ] || [ -z "$url" ]; then break; fi
  done
  if [ -z "$tag" ]; then
    echo "No unityci/editor image found for Unity $unity" >&2
    exit 1
  fi
  image="unityci/editor:$tag"
fi

echo "Unity $unity reference assemblies: extracting from $image"
docker pull --quiet "$image"
container=$(docker create "$image")
trap 'docker rm "$container" >/dev/null; docker rmi "$image" >/dev/null || true' EXIT

rm -rf "$dest.tmp"
mkdir -p "$dest.tmp"
docker cp "$container:/opt/unity/Editor/Data/Managed/UnityEngine" "$dest.tmp/UnityEngine"
find "$dest.tmp" -type f ! -name '*.dll' -delete
echo "$image" > "$dest.tmp/image.txt"
rm -rf "$dest"
mv "$dest.tmp" "$dest"
echo "Copied $(find "$dest" -name '*.dll' | wc -l) assemblies to $dest"
