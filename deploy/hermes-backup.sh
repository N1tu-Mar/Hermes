#!/bin/sh
# Encrypted backup + retention. Run by hermes-backup.service (passphrase arrives as a systemd credential).
set -eu
DEST=${BACKUP_DIR:-/var/backups/hermes}
KEEP=${BACKUP_KEEP:-14}
export HERMES_BACKUP_PASSPHRASE="$(cat "${CREDENTIALS_DIRECTORY:?run under systemd LoadCredential}/backup-passphrase")"
umask 0077
out="$DEST/$(date -u +%Y-%m-%dT%H%M%SZ).hbk"
/opt/hermes/.venv/bin/python -m app.ops backup "$out"
# Keep the newest $KEEP; the sort key is the UTC timestamp in the name.
ls -1 "$DEST"/*.hbk | sort -r | tail -n +$((KEEP + 1)) | while read -r old; do rm -f -- "$old"; done
echo "backup ok: $out"
