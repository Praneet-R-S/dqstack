#!/bin/bash
# Restore DQ-Stack's database to a chosen moment (deploy/README.md section 5).
#
#   deploy/restore-to-time.sh "2026-11-09 14:30:00+05:30"
#   deploy/restore-to-time.sh --switch-to dqstack_pgdata_restore_20261109T093000Z
#
# Run it on the server, from the repository folder, with the same .env the stack uses.
#
# 1. A fresh volume is made and the newest base backup that finished before the chosen
#    time is fetched into it from backup storage (WAL-G).
# 2. A separate, private PostgreSQL starts on that volume and replays the archived WAL
#    up to the chosen time, then pauses there, read-only, for you to look at.
#    The running database, its volume and the backup storage are not touched.
# 3. You confirm. Only then are app, worker, pitr, backup and db stopped, .env is
#    pointed at the new volume (DQ_PGDATA_VOLUME), the database finishes recovery and
#    opens, a fresh base backup is taken, and everything is started again.
#    The old volume is kept, so going back is one line in .env.
#
# Options:
#   --yes           switch without asking (for drills and scripts)
#   --db-only       after switching, start only the database (CI drill)
#   --switch-to V   switch to a restore made earlier and left for checking
#
# The time must carry its UTC offset, for example +05:30 for India or Z for UTC.
# Needs GNU date (any Linux server) and Docker Compose v2.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="${COMPOSE_PROJECT_NAME:-dqstack}"
NETWORK="${PROJECT}_default"
WALG_VOLUME="${PROJECT}_walg"
DB_SERVICES=(app worker pitr backup db)
TIMEOUT_S="${DQ_RESTORE_TIMEOUT_S:-14400}"      # the specification's 4 hours

YES=0
DB_ONLY=0
SWITCH_TO=""
TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --db-only) DB_ONLY=1 ;;
    --switch-to) SWITCH_TO="${2:?--switch-to needs a volume name}"; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) TARGET="$1" ;;
  esac
  shift
done

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ -f .env ] || die "no .env here: run this from the DQ-Stack folder on the server"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is needed"

# The image docker-compose.yml builds for db, pitr and backup (keep the two in step).
DB_IMAGE="${DB_IMAGE:-dqstack-db:16.15-walg3.0.9}"
docker image inspect "$DB_IMAGE" >/dev/null 2>&1 || docker compose build pitr

ENVFILE=$(mktemp)
chmod 600 "$ENVFILE"
trap 'rm -f "$ENVFILE"' EXIT

# The backup settings exactly as the stack resolves them (defaults included), taken
# from the pitr service's environment. Kept in a private temporary file.
docker compose run --rm --no-deps -T --entrypoint sh pitr -c env 2>/dev/null |
  grep -E '^(WALG_|AWS_|POSTGRES_USER=|POSTGRES_DB=|DQ_PG_MAX_CONNECTIONS=|TZ=)' >"$ENVFILE" || true
env_value() { grep -E "^$1=" "$ENVFILE" | tail -1 | cut -d= -f2-; }
PGU="$(env_value POSTGRES_USER)"; PGU="${PGU:-postgres}"
PGD="$(env_value POSTGRES_DB)"; PGD="${PGD:-dqstack}"
MAXCONN="$(env_value DQ_PG_MAX_CONNECTIONS)"; MAXCONN="${MAXCONN:-200}"

current_volume() {
  local v
  v=$(grep -E '^DQ_PGDATA_VOLUME=' .env | tail -1 | cut -d= -f2- || true)
  # docker-compose.yml's own default, which does not follow the project name.
  echo "${v:-dqstack_pgdata}"
}

walg_mount() {
  # The local-folder storage, read-only: a restore only ever reads backups.
  if docker volume inspect "$WALG_VOLUME" >/dev/null 2>&1; then
    printf -- '-v\n%s:/walg:ro\n' "$WALG_VOLUME"
  fi
}

set_env_line() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '\n%s=%s\n' "$key" "$value" >>.env
  fi
}

dbq() {   # a query on the restarted stack's database
  docker compose exec -T db psql -X -U "$PGU" -d "$1" -Atq -c "$2"
}

switch_to() {
  local vol="$1" check="$1-check" old stamp t0 state
  old=$(current_volume)
  [ "$vol" != "$old" ] || die "$vol is already the database in use"
  docker volume inspect "$vol" >/dev/null 2>&1 || die "no volume called $vol"
  t0=$(date +%s)

  say "Stopping the checking server"
  docker stop -t 120 "$check" >/dev/null 2>&1 || true
  docker rm "$check" >/dev/null 2>&1 || true
  docker run --rm --user postgres -v "$vol:/var/lib/postgresql/data" "$DB_IMAGE" \
    dq-pitr restore-finalize

  say "Stopping everything that uses the database: ${DB_SERVICES[*]}"
  docker compose stop "${DB_SERVICES[@]}"

  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  cp .env ".env.before-restore-$stamp"
  set_env_line DQ_PGDATA_VOLUME "$vol"
  echo "  .env now points at $vol (previous settings kept in .env.before-restore-$stamp)"

  say "Starting the database on the restored volume"
  docker compose up -d --no-deps db
  for _ in $(seq 1 720); do
    state=$(dbq postgres "SELECT pg_is_in_recovery()" 2>/dev/null || true)
    [ "$state" = f ] && break
    sleep 5
  done
  [ "$state" = f ] || die "the restored database did not finish recovery; see: docker compose logs db"
  for s in restore_command recovery_target_time recovery_target_timeline recovery_target_action; do
    dbq postgres "ALTER SYSTEM RESET $s" >/dev/null
  done
  dbq postgres "SELECT pg_reload_conf()" >/dev/null
  echo "  the database is open on a new timeline: $(dbq postgres "SELECT pg_walfile_name(pg_current_wal_lsn())")"

  say "Taking a fresh base backup of the restored database"
  if [ "$DB_ONLY" = 1 ]; then
    docker compose run --rm --no-deps -T pitr dq-pitr backup ||
      echo "  WARNING: the base backup failed; take one with: docker compose run --rm pitr dq-pitr backup"
  else
    docker compose up -d
    docker compose exec -T pitr dq-pitr backup ||
      echo "  WARNING: the base backup failed; take one with: docker compose exec pitr dq-pitr backup"
    docker compose run --rm app manage check || true
  fi

  say "Switched in $(( $(date +%s) - t0 )) s"
  cat <<EOF
The old database is untouched in volume $old.
  Go back:   set DQ_PGDATA_VOLUME=$old in .env, then: docker compose up -d
             (anything written since the switch would be lost)
  Remove it when you are sure, not before: docker volume rm $old
EOF
}

if [ -n "$SWITCH_TO" ]; then
  switch_to "$SWITCH_TO"
  exit 0
fi

# ------------------------------------------------------------------ restore
[ -n "$TARGET" ] || die 'give the time to restore to, e.g. "2026-11-09 14:30:00+05:30"'
if ! printf '%s' "$TARGET" | grep -qE '[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)? ?([+-][0-9]{2}(:?[0-9]{2})?|Z|UTC)$'; then
  die "give the time with its UTC offset, e.g. \"$TARGET+05:30\" (India) or \"${TARGET}Z\" (UTC)"
fi
TARGET_EPOCH=$(date -u -d "$TARGET" +%s 2>/dev/null) || die "cannot read the time '$TARGET'"
[ "$TARGET_EPOCH" -le "$(date +%s)" ] || die "$TARGET is in the future"
TARGET_UTC=$(date -u -d "$TARGET" '+%Y-%m-%d %H:%M:%S.%6N+00')
grep -qE '^WALG_(S3|FILE)_PREFIX=.+' "$ENVFILE" ||
  die "no backup storage is set (WALG_S3_PREFIX or WALG_FILE_PREFIX in .env)"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
NEWVOL="${PROJECT}_pgdata_restore_${STAMP}"
CHECK="${NEWVOL}-check"
mapfile -t WALG_MOUNT < <(walg_mount)
NET_ARGS=()
if docker network inspect "$NETWORK" >/dev/null 2>&1; then NET_ARGS=(--network "$NETWORK"); fi

T0=$(date +%s)
say "Restoring to $TARGET_UTC (you asked for $TARGET) into the new volume $NEWVOL"
echo "  the database in use ($(current_volume)) is not touched"
# Labelled as the project's pgdata volume, so Compose accepts it after a switch.
docker volume create \
  --label "com.docker.compose.project=$PROJECT" \
  --label "com.docker.compose.volume=pgdata" \
  --label "dqstack.restore-target=$TARGET_UTC" "$NEWVOL" >/dev/null

docker run --rm "${NET_ARGS[@]}" --env-file "$ENVFILE" \
  -v "$NEWVOL:/var/lib/postgresql/data" "${WALG_MOUNT[@]}" "$DB_IMAGE" \
  dq-pitr restore-prepare "$TARGET_UTC"
T_FETCHED=$(date +%s)

say "Replaying WAL to $TARGET_UTC in a private checking server ($CHECK)"
# No published ports and archiving off: nothing it does can reach the backup storage
# or the users, and a restore that is thrown away leaves no trace in the archive.
docker run -d --name "$CHECK" "${NET_ARGS[@]}" --env-file "$ENVFILE" \
  -v "$NEWVOL:/var/lib/postgresql/data" "${WALG_MOUNT[@]}" "$DB_IMAGE" \
  postgres -c archive_mode=off -c hot_standby=on -c "max_connections=$MAXCONN" >/dev/null

state=""
while :; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$CHECK" 2>/dev/null)" != true ]; then
    docker logs --tail 30 "$CHECK" 2>&1 | sed 's/^/  | /'
    die "the checking server stopped before reaching $TARGET_UTC.
If it says the recovery target was not reached, the archive does not yet hold WAL up to
that time: if the old database is still running, ship its WAL with
  docker compose exec db psql -U $PGU -c 'SELECT pg_switch_wal()'
and try again a minute later, or choose an earlier time. Clean up with:
  docker rm -f $CHECK; docker volume rm $NEWVOL"
  fi
  state=$(docker exec "$CHECK" psql -X -U "$PGU" -d postgres -Atq \
          -c "SELECT pg_get_wal_replay_pause_state()" 2>/dev/null || true)
  [ "$state" = paused ] && break
  if [ $(( $(date +%s) - T0 )) -gt "$TIMEOUT_S" ]; then
    die "replay has not finished after $TIMEOUT_S s; it is still running in $CHECK"
  fi
  sleep 5
done
T_DONE=$(date +%s)

say "Restored copy is paused at the chosen time"
echo "  last transaction replayed: $(docker exec "$CHECK" psql -X -U "$PGU" -d postgres -Atq -c "SELECT pg_last_xact_replay_timestamp()")"
docker exec "$CHECK" psql -X -U "$PGU" -d "$PGD" -q -c \
  "${DQ_RESTORE_SUMMARY_SQL:-SELECT (SELECT count(*) FROM bills) AS bills, (SELECT max(paid_at) FROM bills) AS last_bill_paid, (SELECT max(at) FROM stock_movements) AS last_stock_movement, (SELECT max(at) FROM activity_log) AS last_activity}" \
  2>/dev/null || echo "  (summary query skipped: the DQ-Stack tables are not in $PGD)"
echo "  fetch $(( T_FETCHED - T0 )) s, replay $(( T_DONE - T_FETCHED )) s"
echo "RESTORE_SECONDS=$(( T_DONE - T0 ))"
cat <<EOF

Look at it before deciding (read-only):
  docker exec -it $CHECK psql -U $PGU -d $PGD
EOF

if [ "$YES" != 1 ]; then
  answer=""
  if [ -t 0 ]; then
    read -r -p "Switch DQ-Stack to this restored database now? Type yes to switch: " answer
  fi
  if [ "$answer" != yes ]; then
    cat <<EOF

Not switched. Nothing in use has changed. When ready:
  deploy/restore-to-time.sh --switch-to $NEWVOL
To throw this restore away:
  docker rm -f $CHECK && docker volume rm $NEWVOL
EOF
    exit 0
  fi
fi

switch_to "$NEWVOL"
