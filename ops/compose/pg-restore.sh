#!/bin/bash
# M4 T4.4 restore drill (ADR-74; Artifact 12.6 steps 2 and 3): recover a base backup and the
# archived WAL into a fresh data directory, then promote. Run by the `postgres-restore`
# Compose service (profile `drill`), as the postgres user; the backup folder is read-only.
#   AISRE_RESTORE_BACKUP  the backup ID: /backups/base/<id>/base.tar (from `aisre backup take`)
#   AISRE_RESTORE_WAL     the WAL folder under /backups to recover from (default `wal`)
#   AISRE_RESTORE_TARGET  a point in time to stop at; empty recovers all the WAL there is
set -euo pipefail
: "${AISRE_RESTORE_BACKUP:?name the backup to restore}"
export PGDATA=/var/tmp/restore
backup="/backups/base/$AISRE_RESTORE_BACKUP"
rm -rf "$PGDATA"
mkdir -m 700 "$PGDATA"
tar -xf "$backup/base.tar" -C "$PGDATA"
if [ -f "$backup/pg_wal.tar" ]; then
  tar -xf "$backup/pg_wal.tar" -C "$PGDATA/pg_wal"
fi
{
  echo "restore_command = 'cp /backups/${AISRE_RESTORE_WAL:-wal}/%f %p'"
  if [ -n "${AISRE_RESTORE_TARGET:-}" ]; then
    echo "recovery_target_time = '$AISRE_RESTORE_TARGET'"
  fi
  echo "recovery_target_action = 'promote'"
  echo "archive_mode = 'off'"
} >> "$PGDATA/postgresql.auto.conf"
touch "$PGDATA/recovery.signal"
exec postgres -D "$PGDATA"
