#!/bin/zsh
set -euo pipefail

project_dir=${0:A:h:h}
cd "$project_dir"

dist_dir=${CODEXRELAY_DIST_DIR:-"$project_dir/artifacts/dist"}
work_dir=${CODEXRELAY_WORK_DIR:-"$project_dir/artifacts/build"}
app_path="$dist_dir/CodexRelay.app"

if [[ -e "$app_path" ]]; then
  print -u2 "Refusing to overwrite existing app: $app_path"
  print -u2 "Move it aside, or set CODEXRELAY_DIST_DIR and CODEXRELAY_WORK_DIR to new directories."
  exit 2
fi

uv run --extra gui --extra packaging python scripts/build_icon.py
uv run --extra gui --extra packaging pyinstaller \
  --distpath "$dist_dir" \
  --workpath "$work_dir" \
  packaging/CodexRelay.spec

echo "$app_path"
