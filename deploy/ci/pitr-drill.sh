#!/bin/bash
# The point-in-time restore drill, end to end, as CI runs it (job pitr-drill in
# .github/workflows/ci.yml). It uses the real docker-compose.yml and the real
# deploy/restore-to-time.sh, against whatever backup storage .env names.
#
#   1. PostgreSQL with WAL archiving and the pitr sidecar come up; the sidecar takes
#      the first base backup by itself.
#   2. Rows are written, a base backup is taken, more rows are written ("before"),
#      the time T is noted, and more rows are written ("after").
#   3. Without forcing anything, the WAL holding the last write must reach storage
#      within archive_timeout: that bounds what losing the server can cost.
#   4. The database is killed. restore-to-time.sh restores to T into a fresh volume
#      and switches to it.
#   5. Exactly the rows from before T are there; none from after. The old volume is
#      untouched (it still holds the "after" rows). Archiving resumes on a new
#      timeline.
#
# Needs a .env with the database passwords and the backup storage settings, and
# Docker. Exits non-zero if anything does not hold.
set -euo pipefail
cd "$(dirname "$0")/../.."

DB_IMAGE=dqstack-db:16.15-walg3.0.9
ARCHIVE_TIMEOUT=$(grep -E '^DQ_PITR_ARCHIVE_TIMEOUT=' .env | tail -1 | cut -d= -f2 || true)
ARCHIVE_TIMEOUT="${ARCHIVE_TIMEOUT:-300}"

step() { printf '\n=== %s\n' "$*"; }
fail() { printf '\nDRILL FAILED: %s\n' "$*" >&2; exit 1; }
report() {
  echo "$*"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "- $*" >>"$GITHUB_STEP_SUMMARY"; fi
}
q() { docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U postgres -d dqstack -Atq -c "$1"; }
wait_db() {
  for _ in $(seq 1 90); do
    if q "SELECT 1" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  fail "the database did not come up"
}
wait_archived() {   # until the archiver has shipped segment $1 (or a later one)
  local want="$1" limit="$2" got=""
  for _ in $(seq 1 $(( limit / 3 + 1 ))); do
    got=$(q "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")
    if [ -n "$got" ] && { [[ "$got" > "$want" ]] || [ "$got" = "$want" ]; }; then return 0; fi
    sleep 3
  done
  fail "segment $want was not archived within $limit s (last archived: ${got:-none})"
}

step "1. PostgreSQL with WAL archiving, and the pitr sidecar"
docker compose up -d --build db pitr
wait_db
[ "$(q "SHOW archive_mode")" = on ] || fail "archive_mode is not on"
report "archive_timeout: $(q "SHOW archive_timeout")"
for _ in $(seq 1 60); do
  if docker compose exec -T pitr dq-pitr list 2>/dev/null | grep -q 'base_'; then break; fi
  sleep 5
done
docker compose exec -T pitr dq-pitr list | grep -q 'base_' || fail "the pitr service took no first base backup"

step "2. Data, a base backup, rows before T, the time T, rows after T"
q "CREATE TABLE drill (id bigserial PRIMARY KEY, phase text NOT NULL,
                       at timestamptz NOT NULL DEFAULT clock_timestamp())"
q "INSERT INTO drill (phase) SELECT 'seed' FROM generate_series(1, 1000)"
docker compose exec -T pitr dq-pitr backup
for _ in $(seq 1 20); do
  q "INSERT INTO drill (phase) SELECT 'before' FROM generate_series(1, 50)"
  sleep 0.2
done
sleep 2
T=$(q "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00'")
sleep 2
for _ in $(seq 1 20); do
  q "INSERT INTO drill (phase) SELECT 'after' FROM generate_series(1, 50)"
  sleep 0.2
done
report "T = $T"

step "3. The last write reaches backup storage within archive_timeout, unforced"
last_wal=$(q "SELECT pg_walfile_name(pg_current_wal_lsn())")
wrote=$(date +%s)
wait_archived "$last_wal" $(( ARCHIVE_TIMEOUT + 120 ))
lag=$(( $(date +%s) - wrote ))
report "the WAL holding the last write was in storage ${lag} s after it was written (archive_timeout ${ARCHIVE_TIMEOUT} s)"
[ "$lag" -le $(( ARCHIVE_TIMEOUT + 60 )) ] || fail "archiving took longer than archive_timeout + 60 s"
docker compose exec -T pitr dq-pitr check || fail "dq-pitr check reports a problem before the disaster"

step "4. Disaster: the database server dies; restore to T"
docker compose kill db
docker compose stop pitr
t0=$(date +%s)
bash deploy/restore-to-time.sh --yes --db-only "$T" 2>&1 | tee restore.log
total=$(( $(date +%s) - t0 ))
restore_s=$(grep -oE '^RESTORE_SECONDS=[0-9]+' restore.log | cut -d= -f2)
report "restore to T (fetch the base backup + replay WAL): ${restore_s:-?} s; whole script including the switch-over and a fresh base backup: ${total} s (target: 4 hours)"

step "5. Exactly the rows written before T are back"
wait_db
got=$(q "SELECT string_agg(phase || '=' || n, ' ' ORDER BY phase) FROM (SELECT phase, count(*) AS n FROM drill GROUP BY phase) x")
report "restored rows: $got"
[ "$got" = "before=1000 seed=1000" ] || fail "expected 'before=1000 seed=1000', found '$got'"
[ "$(q "SELECT max(at) <= '$T'::timestamptz FROM drill")" = t ] || fail "a row from after T is present"

new_volume=$(grep -E '^DQ_PGDATA_VOLUME=' .env | tail -1 | cut -d= -f2)
[[ "$new_volume" == dqstack_pgdata_restore_* ]] || fail ".env does not point at the restored volume"
echo "the stack now runs on $new_volume; checking the old volume was left alone"
docker run -d --name drill-old-copy -v dqstack_pgdata:/var/lib/postgresql/data "$DB_IMAGE" \
  postgres -c archive_mode=off >/dev/null
old_after=""
for _ in $(seq 1 60); do
  old_after=$(docker exec drill-old-copy psql -X -U postgres -d dqstack -Atq \
              -c "SELECT count(*) FROM drill WHERE phase = 'after'" 2>/dev/null || true)
  [ -n "$old_after" ] && break
  sleep 2
done
docker rm -f drill-old-copy >/dev/null
[ "$old_after" = 1000 ] || fail "the old volume should still hold the 1000 'after' rows, found '${old_after}'"
report "old volume untouched: it still holds all rows, including the 1000 written after T"

step "6. Archiving carries on from the restored database, on a new timeline"
q "INSERT INTO drill (phase) VALUES ('after-restore')"
wal=$(q "SELECT pg_walfile_name(pg_current_wal_lsn())")
[ "${wal:0:8}" != 00000001 ] || fail "the restored database is still on timeline 1"
q "SELECT pg_switch_wal()" >/dev/null
wait_archived "$wal" 120
report "archiving resumed on timeline $((16#${wal:0:8})) ($wal)"
docker compose up -d pitr
docker compose exec -T pitr dq-pitr check || echo "(dq-pitr check after the switch is informational)"

step "DRILL PASSED"
