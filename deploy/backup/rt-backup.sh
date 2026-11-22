#!/usr/bin/env bash
# rt-forecast nightly backup: consistent copy of rt.db + mirror of raw responses.
# Install: /usr/local/bin/rt-backup (run by rt-backup.timer as user rt).
set -euo pipefail

DATA=/var/lib/rt-forecast
DEST=/var/backups/rt-forecast
KEEP_DAYS=14

stamp=$(date -u +%Y-%m-%dT%H%MZ)
mkdir -p "$DEST/db" "$DEST/raw"

# .backup copies a consistent snapshot even while the collector is writing.
copy="$DEST/db/rt-$stamp.db"
sqlite3 "$DATA/rt.db" ".backup '$copy'"
# Prove the copy is usable before trusting it.
check=$(sqlite3 "$copy" "PRAGMA integrity_check;")
if [ "$check" != "ok" ]; then
    echo "integrity check failed: $check" >&2
    exit 1
fi
gzip "$copy"

# Raw files never change once written, so rsync only copies new ones.
# No --delete: a file removed from $DATA/raw stays in the backup.
rsync -a "$DATA/raw/" "$DEST/raw/"

# Keep two weeks of database copies.
find "$DEST/db" -name 'rt-*.db.gz' -mtime +"$KEEP_DAYS" -delete

echo "backup ok: $copy.gz ($(du -h "$copy.gz" | cut -f1)), raw $(du -sh "$DEST/raw" | cut -f1)"
