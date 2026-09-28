#!/bin/sh
# Usage: sudo deploy/hermes-restore.sh /var/backups/hermes/<file>.hbk
# Stops the service, restores (old data root kept as <root>.pre-restore-<timestamp>), then starts it again.
# Passphrase: read from /etc/hermes/backup-passphrase, or HERMES_BACKUP_PASSPHRASE if already set.
set -eu
file=${1:?usage: $0 BACKUP.hbk}
[ -r "$file" ] || { echo "cannot read $file" >&2; exit 1; }
[ -n "${HERMES_BACKUP_PASSPHRASE:-}" ] || HERMES_BACKUP_PASSPHRASE="$(cat /etc/hermes/backup-passphrase)"
export HERMES_BACKUP_PASSPHRASE
systemctl stop hermes
set -a; . /etc/hermes/hermes.env; set +a
cd /opt/hermes
setpriv --reuid=hermes --regid=hermes --init-groups --inh-caps=-all .venv/bin/python -m app.ops restore "$file"
systemctl start hermes
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -fsS http://127.0.0.1:8765/readyz >/dev/null && { echo "restore ok, service ready"; exit 0; }
  sleep 2
done
echo "restored, but /readyz not ready; check journalctl -u hermes" >&2; exit 1
