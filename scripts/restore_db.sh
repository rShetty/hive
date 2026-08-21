#!/usr/bin/env bash
#
# restore_db.sh - restore a Hive SQLite database from a backup created by
# scripts/backup_db.sh.
#
# Usage:
#   scripts/restore_db.sh [-y] /path/to/backup.db
#
# The target database is resolved like the backup script:
#   DB_PATH > DATABASE_URL > /opt/hive/data/agent_marketplace.db
#
# !! STOP SERVICES FIRST !!
#   ssh root@<host> 'cd /opt/hive && docker-compose -f docker-compose.prod.yml stop marketplace'
# Restoring under a running writer can corrupt the database. This script
# refuses to continue unless you pass -y/--yes to confirm services are stopped.
#
# Exit codes: 0 ok, 1 bad usage, 2 restore/verification failure.

set -euo pipefail

ASSUME_YES=0

log() { printf '[restore_db] %s\n' "$*"; }
die() { printf '[restore_db] ERROR: %s\n' "$*" >&2; exit 2; }

usage() {
	sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
	exit 1
}

while [ $# -gt 0 ]; do
	case "$1" in
	-y | --yes) ASSUME_YES=1 ;;
	-h | --help) usage ;;
	-*) die "unknown option: $1 (see --help)" ;;
	*)
		[ -z "${BACKUP_FILE:-}" ] || die "only one backup file may be given"
		BACKUP_FILE="$1"
		;;
	esac
	shift
done

[ -n "${BACKUP_FILE:-}" ] || usage
[ -f "$BACKUP_FILE" ] || die "backup file not found: $BACKUP_FILE"
[ -r "$BACKUP_FILE" ] || die "backup file not readable: $BACKUP_FILE"

# ---------------------------------------------------------------------------
# Resolve the target database path (same rules as backup_db.sh).
# ---------------------------------------------------------------------------
resolve_db_path() {
	local raw="${DB_PATH:-}"
	if [ -z "$raw" ] && [ -n "${DATABASE_URL:-}" ]; then
		raw="$DATABASE_URL"
		raw="${raw%%\?*}"
		case "$raw" in
		postgres* | mysql*)
			die "DATABASE_URL is not SQLite ('$DATABASE_URL'); use the matching DB tooling instead"
			;;
		sqlite+aiosqlite:///*) raw="${raw#sqlite+aiosqlite:///}" ;;
		sqlite:///*) raw="${raw#sqlite:///}" ;;
		esac
	fi
	if [ -z "$raw" ]; then
		raw="/opt/hive/data/agent_marketplace.db"
	fi
	printf '%s' "$raw"
}

DB_PATH="$(resolve_db_path)"

cat <<EOF

[restore_db] NOTE: make sure services are STOPPED before restoring, e.g.:

    cd /opt/hive && docker-compose -f docker-compose.prod.yml stop marketplace

  Target database : $DB_PATH
  Backup file     : $BACKUP_FILE

EOF

if [ "$ASSUME_YES" -ne 1 ]; then
	printf 'Continue with the restore? Type YES to proceed: '
	read -r answer
	[ "$answer" = "YES" ] || die "aborted by user (no changes made)"
fi

# ---------------------------------------------------------------------------
# Stage + verify before touching the live path.
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$DB_PATH")"
STAGED="$(dirname "$DB_PATH")/.restore-$(basename "$DB_PATH").tmp.$$"

cleanup() { rm -f "$STAGED" "${STAGED}-wal" "${STAGED}-shm"; }
trap cleanup EXIT

cp "$BACKUP_FILE" "$STAGED"

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

log "verifying staged copy ($STAGED)"
verify_integrity "$STAGED" || die "integrity check FAILED on $BACKUP_FILE; live database untouched"

# ---------------------------------------------------------------------------
# Atomically swap in the restored file.
# ---------------------------------------------------------------------------
if [ -f "$DB_PATH" ]; then
	# Keep the current ownership/mode of the live database.
	chmod --reference="$DB_PATH" "$STAGED" 2>/dev/null ||
		chmod "$(stat -f '%Lp' "$DB_PATH" 2>/dev/null || stat -c '%a' "$DB_PATH")" "$STAGED"
	chown --reference="$DB_PATH" "$STAGED" 2>/dev/null || true
	cp -p "$DB_PATH" "${DB_PATH}.pre-restore.bak" # safety copy of the old db
	log "previous database saved as ${DB_PATH}.pre-restore.bak"
fi

# Stale WAL/SHM sidecar files would corrupt the restored snapshot.
rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
mv "$STAGED" "$DB_PATH"
trap - EXIT

# ---------------------------------------------------------------------------
# Final verification on the restored database.
# ---------------------------------------------------------------------------
verify_integrity "$DB_PATH" || die "post-restore integrity check failed on $DB_PATH"

log "restore OK: $DB_PATH"
TABLES="$(sqlite3 "$DB_PATH" "SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo '?')"
log "tables: $TABLES"

cat <<EOF

[restore_db] Next steps:
  1. Review the output above (integrity_check must be "ok").
  2. Start services again:
       cd /opt/hive && docker-compose -f docker-compose.prod.yml up -d marketplace
  3. Smoke-test the app: curl -fsS http://localhost:8080/api/health
  4. Once satisfied, delete the safety copy ${DB_PATH}.pre-restore.bak.

EOF
