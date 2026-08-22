#!/usr/bin/env bash
#
# backup_db.sh - consistent, timestamped SQLite backup for Hive.
#
# Uses the sqlite3 CLI ".backup" command (online, consistent snapshot even
# while the app is running). Falls back to the Python sqlite3 backup API
# when the sqlite3 CLI is not installed.
#
# Environment:
#   DB_PATH          Explicit path to the SQLite database (wins over DATABASE_URL)
#   DATABASE_URL     e.g. sqlite+aiosqlite:////opt/hive/data/agent_marketplace.db
#   BACKUP_DIR       Output directory (default: /var/backups/hive)
#   RETENTION_DAYS   Delete backups older than this many days (default: 14)
#   POST_BACKUP_HOOK Optional command run after a verified backup; the backup
#                    file path is passed as "$1". Use it to ship the archive
#                    off-box, e.g.:
#                      POST_BACKUP_HOOK='aws s3 cp "$1" s3://my-bucket/hive-db/'
#                      POST_BACKUP_HOOK='rest backup "$1"'
#
# Exit codes: 0 ok, 1 bad usage/config, 2 backup/verification failure.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/hive}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

log() { printf '[backup_db] %s\n' "$*"; }
die() { printf '[backup_db] ERROR: %s\n' "$*" >&2; exit 2; }

usage() {
	sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
	exit 1
}

case "${1:-}" in
-h | --help) usage ;;
"") ;;
*) die "unknown argument: $1 (see --help)" ;;
esac

# ---------------------------------------------------------------------------
# Resolve the database path: DB_PATH > DATABASE_URL > deployment default.
# ---------------------------------------------------------------------------
resolve_db_path() {
	local raw="${DB_PATH:-}"
	if [ -z "$raw" ] && [ -n "${DATABASE_URL:-}" ]; then
		raw="$DATABASE_URL"
		raw="${raw%%\?*}" # drop query string if present
		case "$raw" in
		postgres* | mysql*)
			die "DATABASE_URL is not SQLite ('$DATABASE_URL'); use pg_dump/mysqldump instead"
			;;
		sqlite+aiosqlite:///*) raw="${raw#sqlite+aiosqlite:///}" ;;
		sqlite:///*) raw="${raw#sqlite:///}" ;;
		esac
	fi
	if [ -z "$raw" ]; then
		raw="/opt/hive/data/agent_marketplace.db" # default on the VPS deploy
	fi
	printf '%s' "$raw"
}

DB_PATH="$(resolve_db_path)"
[ -f "$DB_PATH" ] || die "database not found: $DB_PATH"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASE="$(basename "$DB_PATH")"
BASE="${BASE%.*}"
TARGET="$BACKUP_DIR/${BASE}-${STAMP}.db"
TMP="$BACKUP_DIR/.${BASE}-${STAMP}.db.tmp.$$"

mkdir -p "$BACKUP_DIR"

# ---------------------------------------------------------------------------
# Create a consistent snapshot.
# ---------------------------------------------------------------------------
if command -v sqlite3 >/dev/null 2>&1; then
	log "backing up '$DB_PATH' with sqlite3 .backup -> '$TMP'"
	sqlite3 "$DB_PATH" ".backup '$TMP'"
else
	log "sqlite3 CLI not found; falling back to python3 sqlite3 backup API"
	command -v python3 >/dev/null 2>&1 ||
		die "neither sqlite3 nor python3 is available"
	python3 - "$DB_PATH" "$TMP" <<'PY'
import sys, sqlite3

src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
fi

# ---------------------------------------------------------------------------
# Verify the snapshot before it counts as a backup.
# ---------------------------------------------------------------------------
verify_integrity() {
	local file="$1"
	if command -v sqlite3 >/dev/null 2>&1; then
		[ "$(sqlite3 "$file" 'PRAGMA integrity_check;')" = "ok" ]
	else
		python3 - "$file" <<'PY'
import sys, sqlite3

db = sqlite3.connect(sys.argv[1])
row = db.execute("PRAGMA integrity_check").fetchone()
db.close()
sys.exit(0 if row and row[0] == "ok" else 1)
PY
	fi
}

if verify_integrity "$TMP"; then
	rm -f "${TMP}-wal" "${TMP}-shm" # drop read-check sidecar files
	mv "$TMP" "$TARGET"
	chmod 600 "$TARGET" 2>/dev/null || true
	log "backup OK: $TARGET ($(du -h "$TARGET" | cut -f1))"
else
	rm -f "$TMP" "${TMP}-wal" "${TMP}-shm"
	die "integrity check failed on snapshot; backup aborted"
fi

# ---------------------------------------------------------------------------
# Retention pruning.
# ---------------------------------------------------------------------------
pruned=$(find "$BACKUP_DIR" -type f -name "${BASE}-*.db" -mtime +"$RETENTION_DAYS" -print -delete | wc -l | tr -d ' ')
log "retention: removed ${pruned} backup(s) older than ${RETENTION_DAYS} day(s)"

# ---------------------------------------------------------------------------
# Off-box copy hook (S3 / restic / rsync ...).
# ---------------------------------------------------------------------------
if [ -n "${POST_BACKUP_HOOK:-}" ]; then
	log "running POST_BACKUP_HOOK"
	bash -c "$POST_BACKUP_HOOK" backup-hook "$TARGET" ||
		die "POST_BACKUP_HOOK failed (local backup is still valid: $TARGET)"
fi

log "done"
