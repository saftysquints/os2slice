#!/usr/bin/env bash
# Stage Home Assistant local add-ons and (optionally) deploy them over SSH.
#
#   scripts/deploy_addon.sh stage  <dir> [os2slice|bambustudio_web|preform_server]   # build a staging folder
#   scripts/deploy_addon.sh deploy       [os2slice|bambustudio_web|preform_server]   # copy, install or rebuild
#
# Only source code goes over: no keys, tests, git history or local config.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Your Home Assistant SSH login, from the environment or an untracked .deploy.env, e.g.
#   OS2SLICE_HA_HOST=root@homeassistant.local
#   OS2SLICE_HA_KEY=~/.ssh/id_ed25519
# shellcheck disable=SC1091
[ -f "$ROOT/.deploy.env" ] && . "$ROOT/.deploy.env"
HOST="${OS2SLICE_HA_HOST:-root@homeassistant.local}"
KEY="${OS2SLICE_HA_KEY:-$HOME/.ssh/id_ed25519}"

stage() {
  local out="$1" addon="$2"
  rm -rf "$out" && mkdir -p "$out"
  case "$addon" in
    os2slice)
      mkdir -p "$out/app"
      cp "$ROOT"/addon/os2slice/{config.yaml,Dockerfile,DOCS.md} "$out/"
      cp "$ROOT"/{pyproject.toml,README.md} "$out/app/"
      (cd "$ROOT" && tar -c --exclude='__pycache__' src) | tar -x -C "$out/app"
      ;;
    bambustudio_web)
      (cd "$ROOT/addon/bambustudio_web" && tar -c --exclude='__pycache__' .) | tar -x -C "$out"
      ;;
    preform_server)
      cp "$ROOT"/addon/preform_server/{config.yaml,Dockerfile,DOCS.md} "$out/"
      ;;
    *) echo "unknown add-on $addon" >&2; exit 2 ;;
  esac
  echo "staged $addon in $out"
}

ssh_ha() { ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$HOST" "$@"; }

deploy() {
  local addon="$1" tmp slug="local_$1"
  tmp="$(mktemp -d)"
  trap "rm -rf '$tmp'" EXIT
  stage "$tmp/$addon" "$addon"
  local version
  version="$(sed -n 's/^version: *"\(.*\)"/\1/p' "$tmp/$addon/config.yaml")"
  tar -C "$tmp" -c "$addon" | ssh_ha "rm -rf /local_apps/$addon && tar -x -C /local_apps"
  # Wait until the store has read the new version: building before that tags the image
  # with the old version, and the start then fails with "image ... does not exist".
  ssh_ha "ha store reload >/dev/null
    for i in \$(seq 30); do
      info=\$(ha apps info $slug --raw-json 2>/dev/null)
      echo \"\$info\" | grep -q '\"version_latest\":\"$version\"' && break
      echo \"\$info\" | grep -q '\"version\":\"' || break  # not installed yet
      sleep 2
    done
    if echo \"\$info\" | grep -q '\"update_available\":true'; then
      echo 'updating (version changed)'; ha apps update $slug
    elif echo \"\$info\" | grep -q '\"version\":\"'; then
      echo 'rebuilding'; ha apps rebuild $slug
    else
      echo 'installing'; ha apps install $slug
    fi"
  echo "deployed $addon; start it with: ha apps start $slug (if it isn't running)"
}

case "${1:-}" in
  stage) stage "${2:?staging dir}" "${3:-os2slice}" ;;
  deploy) deploy "${2:-os2slice}" ;;
  *) echo "usage: $0 stage <dir> [addon] | deploy [addon]" >&2; exit 2 ;;
esac
