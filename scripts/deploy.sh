#!/usr/bin/env bash
# Copy the integration to Home Assistant over the Samba add-on's `config` share.
#
# Mount the share first (Finder: Go > Connect to Server > smb://homeassistant.local/config),
# or pass another mount point as the first argument / HA_CONFIG env var.
# Restart Home Assistant afterwards to load the new code.
set -euo pipefail

HA_CONFIG="${1:-${HA_CONFIG:-/Volumes/config}}"
SRC="$(cd "$(dirname "$0")/.." && pwd)/custom_components/bria/"
DEST="$HA_CONFIG/custom_components/bria/"

if [[ ! -d "$HA_CONFIG" ]]; then
  echo "HA config share not mounted at $HA_CONFIG" >&2
  echo "Mount smb://homeassistant.local/config or pass the mount point as an argument." >&2
  exit 1
fi

mkdir -p "$DEST"
rsync -av --delete --exclude '__pycache__' "$SRC" "$DEST"
echo "Deployed to $DEST. Restart Home Assistant to load it."
