#!/bin/zsh
set -euo pipefail

project_dir=${0:A:h:h}
app_path=${1:-"$project_dir/artifacts/current/CodexRelay.app"}
version=${CODEXRELAY_VERSION:-$(PYTHONPATH="$project_dir/src" uv run python -c 'from codexrelay.version import __version__; print(__version__)')}
architecture=${CODEXRELAY_ARCH:-arm64}
output_dir=${CODEXRELAY_RELEASE_DIR:-"$project_dir/artifacts/release"}
output_path="$output_dir/CodexRelay-macos-$architecture-v$version.dmg"
checksum_path="$output_dir/SHA256SUMS.txt"

if [[ ! -d "$app_path" ]]; then
  print -u2 "App bundle not found: $app_path"
  exit 1
fi

mkdir -p "$output_dir"
staging_dir=$(mktemp -d "$project_dir/artifacts/dmg-staging.XXXXXX")
trap 'rm -rf "$staging_dir"' EXIT

cp -R "$app_path" "$staging_dir/CodexRelay.app"
ln -s /Applications "$staging_dir/Applications"

rm -f "$output_path"
hdiutil create \
  -volname "CodexRelay $version" \
  -srcfolder "$staging_dir" \
  -ov \
  -format UDZO \
  "$output_path" >/dev/null

checksum=$(shasum -a 256 "$output_path" | awk '{print $1}')
printf '%s  %s\n' "$checksum" "${output_path:t}" > "$checksum_path"
cat "$checksum_path"
print "$output_path"
