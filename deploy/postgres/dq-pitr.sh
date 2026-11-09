#!/bin/bash
# dq-pitr: continuous WAL archiving and point-in-time recovery for DQ-Stack, with WAL-G.
# Installed in the database image as /usr/local/bin/dq-pitr (deploy/postgres/Dockerfile).
#
#   dq-pitr archive %p          PostgreSQL's archive_command (db service)
#   dq-pitr fetch %f %p         restore_command while a restored copy replays WAL
#   dq-pitr backup              take a base backup now (pitr service)
#   dq-pitr retain              delete backups and WAL older than DQ_PITR_RETAIN_DAYS
#   dq-pitr list                the base backups held
#   dq-pitr check               everything an operator wants to know; exit 1 on a problem
#   dq-pitr health              the pitr service's health check (reads the loop's status)
#   dq-pitr loop                the pitr service: first backup at once, then nightly
#   dq-pitr restore-prepare T   into an empty PGDATA: fetch the newest base backup that
#                               finished before T and set up recovery to stop at T
#   dq-pitr restore-finalize    turn the paused, checked restore into one that promotes
#
# Where backups go (S3 wins when both are set):
#   WALG_S3_PREFIX=s3://bucket/prefix  plus AWS_ENDPOINT (for non-AWS S3), AWS_REGION,
#                                      AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
#                                      AWS_S3_FORCE_PATH_STYLE, WALG_S3_SSE
#   WALG_FILE_PREFIX=/walg/dqstack     a folder (the walg volume): small installs only
# Encryption: WALG_LIBSODIUM_KEY (64 hex characters, WALG_LIBSODIUM_KEY_TRANSFORM=hex)
# encrypts every file before it leaves the server.
set -euo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
STATUS_FILE=/tmp/dq-pitr.status
LOCK_FILE=/tmp/dq-pitr.lock
STORAGE=none

log() { printf '%s dq-pitr: %s\n' "$(date '+%F %T %Z')" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Empty settings are removed, so WAL-G never mistakes "" for a value, and only one
# storage is left configured.
storage_env() {
  local v
  for v in WALG_S3_PREFIX WALG_FILE_PREFIX AWS_ENDPOINT AWS_REGION AWS_ACCESS_KEY_ID \
           AWS_SECRET_ACCESS_KEY AWS_S3_FORCE_PATH_STYLE WALG_S3_SSE WALG_S3_SSE_KMS_ID \
           WALG_LIBSODIUM_KEY WALG_LIBSODIUM_KEY_TRANSFORM WALG_COMPRESSION_METHOD \
           WALG_DELTA_MAX_STEPS WALG_UPLOAD_CONCURRENCY WALG_DOWNLOAD_CONCURRENCY; do
    if [ -z "${!v:-}" ]; then unset "$v"; fi
  done
  if [ -n "${WALG_S3_PREFIX:-}" ]; then
    unset WALG_FILE_PREFIX
    STORAGE=s3
  elif [ -n "${WALG_FILE_PREFIX:-}" ]; then
    STORAGE=file
  else
    STORAGE=none
  fi
  if [ -z "${WALG_LIBSODIUM_KEY:-}" ]; then unset WALG_LIBSODIUM_KEY_TRANSFORM; fi
  return 0
}

require_storage() {
  [ "$STORAGE" != none ] || die "no backup storage is set: give WALG_S3_PREFIX (object storage) or WALG_FILE_PREFIX (a local folder) in .env"
}

encrypted() { [ -n "${WALG_LIBSODIUM_KEY:-}" ] || [ -n "${WALG_S3_SSE:-}" ]; }

# The backups held, as JSON. Fails only when the storage cannot be read.
list_json() {
  local out err=/tmp/dq-pitr.list.err
  if out=$(wal-g backup-list --detail --json 2>"$err"); then
    if printf '%s' "$out" | jq -e 'type == "array"' >/dev/null 2>&1; then
      printf '%s\n' "$out"
    else
      echo '[]'
    fi
  elif grep -qi 'no backups found' "$err"; then
    echo '[]'
  else
    cat "$err" >&2
    return 1
  fi
}

# "name<TAB>finish epoch" for each backup, oldest first.
backup_rows() {
  local json name finish t
  json=$(list_json) || return 1
  printf '%s\n' "$json" | jq -r '.[] | [.backup_name, (.finish_time // .time // "")] | @tsv' |
    while IFS=$'\t' read -r name finish; do
      if [ -n "$finish" ] && t=$(date -u -d "$finish" +%s 2>/dev/null); then
        printf '%s\t%s\n' "$name" "$t"
      fi
    done | sort -t$'\t' -k2,2n
}

cmd_archive() {
  storage_env
  if [ "$STORAGE" = none ]; then
    # Nothing to archive to: the segment is let go, exactly as with archiving off.
    # The pitr service reports this as a problem.
    log "WAL archiving has no storage set; segment $1 not kept"
    exit 0
  fi
  exec wal-g wal-push "$1"
}

cmd_fetch() {
  storage_env
  require_storage
  exec wal-g wal-fetch "$1" "$2"
}

cmd_backup() {
  storage_env
  require_storage
  (
    flock 9
    log "base backup starting (storage: $STORAGE)"
    # Explicit: errexit does not apply when the caller tests this function with "if".
    wal-g backup-push "$PGDATA" || exit 1
    log "base backup finished"
  ) 9>"$LOCK_FILE"
}

cmd_retain() {
  storage_env
  require_storage
  local days="${DQ_PITR_RETAIN_DAYS:-8}" cutoff
  if [ "$days" -lt 8 ]; then
    log "DQ_PITR_RETAIN_DAYS=$days is too short for a 7-day restore window; keeping 8 days"
    days=8
  fi
  cutoff=$(date -u -d "-${days} days" +%Y-%m-%dT%H:%M:%SZ)
  # FIND_FULL keeps the full backup that the oldest kept delta backup builds on.
  log "deleting base backups and WAL from before $cutoff"
  wal-g delete before FIND_FULL "$cutoff" --confirm
}

cmd_list() {
  storage_env
  require_storage
  wal-g backup-list --detail
}

# Fills PROBLEMS and NOTES. Needs the database (PGHOST etc.) for the archiver figures.
assess() {
  PROBLEMS=()
  NOTES=()
  storage_env
  if [ "$STORAGE" = none ]; then
    PROBLEMS+=("no backup storage is set, so WAL is not archived and point-in-time restore is impossible")
    return 0
  fi
  if [ "$STORAGE" = s3 ] && ! encrypted; then
    PROBLEMS+=("backups go to object storage unencrypted: set WALG_LIBSODIUM_KEY (or WALG_S3_SSE)")
  fi
  if [ "$STORAGE" = file ]; then
    NOTES+=("backups are on this server's own disk ($WALG_FILE_PREFIX): this is not a backup against losing the server")
  fi
  local row archived failed failed_count last_wal
  if row=$(psql -X -w -Atq -F ' ' -c "SELECT coalesce(extract(epoch FROM last_archived_time)::bigint, 0), coalesce(extract(epoch FROM last_failed_time)::bigint, 0), failed_count, coalesce(last_archived_wal, '-') FROM pg_stat_archiver" 2>/dev/null); then
    read -r archived failed failed_count last_wal <<<"$row"
    NOTES+=("last WAL archived: $last_wal at $( [ "$archived" -gt 0 ] && date -d "@$archived" '+%F %T %Z' || echo never)")
    if [ "$failed" -gt "$archived" ]; then
      PROBLEMS+=("WAL archiving is failing (last failure $(date -d "@$failed" '+%F %T %Z'), $failed_count failures so far): WAL is piling up on the database server")
    fi
  else
    PROBLEMS+=("cannot reach the database to read the archiver's state")
  fi
  local rows newest=0 count=0 max_age_h="${DQ_PITR_MAX_BACKUP_AGE_H:-26}"
  if rows=$(backup_rows); then
    if [ -n "$rows" ]; then
      count=$(printf '%s\n' "$rows" | wc -l)
      newest=$(printf '%s\n' "$rows" | tail -1 | cut -f2)
    fi
    if [ "$count" -eq 0 ]; then
      PROBLEMS+=("no base backup exists yet")
    else
      NOTES+=("$count base backups; newest finished $(date -d "@$newest" '+%F %T %Z')")
      if [ $(( $(date +%s) - newest )) -gt $(( max_age_h * 3600 )) ]; then
        PROBLEMS+=("the newest base backup is more than $max_age_h hours old")
      fi
    fi
  else
    PROBLEMS+=("cannot list the backups in storage ($STORAGE)")
  fi
  return 0
}

cmd_check() {
  assess
  echo "storage: $STORAGE${WALG_S3_PREFIX:+ $WALG_S3_PREFIX}${WALG_FILE_PREFIX:+ $WALG_FILE_PREFIX}"
  if encrypted; then
    echo "encryption: ${WALG_LIBSODIUM_KEY:+libsodium (client side) }${WALG_S3_SSE:+S3 server side ($WALG_S3_SSE)}"
  else
    echo "encryption: none"
  fi
  if [ "$STORAGE" != none ]; then
    echo "--- base backups"
    wal-g backup-list --detail 2>&1 || true
    echo "--- WAL chain since the oldest backup"
    local verify
    verify=$(wal-g wal-verify integrity timeline 2>&1 || true)
    printf '%s\n' "$verify"
    if printf '%s' "$verify" | grep -q 'FAILURE'; then
      PROBLEMS+=("wal-verify found a gap in the archived WAL")
    fi
  fi
  local n
  for n in "${NOTES[@]}"; do echo "note: $n"; done
  if [ "${#PROBLEMS[@]}" -eq 0 ]; then
    echo "OK"
    return 0
  fi
  for n in "${PROBLEMS[@]}"; do echo "PROBLEM: $n"; done
  return 1
}

write_status() {
  assess
  local n
  if [ "${#PROBLEMS[@]}" -eq 0 ]; then
    printf 'ok %s\n' "$(date '+%F %T %Z')" >"$STATUS_FILE"
  else
    printf 'problem: %s\n' "${PROBLEMS[@]}" >"$STATUS_FILE"
    for n in "${PROBLEMS[@]}"; do log "PROBLEM: $n"; done
  fi
  return 0
}

cmd_health() {
  [ -f "$STATUS_FILE" ] || exit 0          # still starting
  cat "$STATUS_FILE"
  grep -q '^ok' "$STATUS_FILE"
}

cmd_loop() {
  storage_env
  local at="${DQ_PITR_BACKUP_AT:-01:30}" every="${DQ_PITR_CHECK_EVERY_S:-600}"
  log "starting: storage $STORAGE, base backup daily at $at ($(date +%Z)), keep ${DQ_PITR_RETAIN_DAYS:-8} days"
  until pg_isready -q; do sleep 5; done
  local rows newest due now
  while :; do
    if [ "$STORAGE" != none ] && rows=$(backup_rows); then
      newest=0
      if [ -n "$rows" ]; then newest=$(printf '%s\n' "$rows" | tail -1 | cut -f2); fi
      due=$(date -d "today $at" +%s)
      now=$(date +%s)
      if [ "$newest" -eq 0 ] || { [ "$now" -ge "$due" ] && [ "$newest" -lt "$due" ]; }; then
        if [ "$newest" -eq 0 ]; then log "no base backup yet: taking the first one now"; fi
        if cmd_backup; then
          cmd_retain || log "retention FAILED; old backups were not deleted"
        else
          log "base backup FAILED"
        fi
      fi
    fi
    write_status
    sleep "$every"
  done
}

cmd_restore_prepare() {
  local target="${1:?usage: dq-pitr restore-prepare 'YYYY-MM-DD HH:MM:SS+00'}" t rows best="" name ft
  storage_env
  require_storage
  t=$(date -u -d "$target" +%s) || die "cannot read the time '$target'"
  if [ -n "$(ls -A "$PGDATA" 2>/dev/null)" ]; then die "$PGDATA is not empty; restore only into a fresh volume"; fi
  rows=$(backup_rows) || die "cannot list the backups in storage"
  while IFS=$'\t' read -r name ft; do
    if [ -n "$name" ] && [ "$ft" -le "$t" ]; then best="$name"; fi
  done <<<"$rows"
  if [ -z "$best" ]; then
    log "base backups held (name, finished):"
    while IFS=$'\t' read -r name ft; do
      if [ -n "$name" ]; then log "  $name  $(date -u -d "@$ft" '+%F %T UTC')"; fi
    done <<<"$rows"
    die "no base backup finished before $target: choose a later time"
  fi
  log "restoring base backup $best (the newest one finished before $target)"
  wal-g backup-fetch "$PGDATA" "$best"
  cat >>"$PGDATA/postgresql.auto.conf" <<EOF
# --- added by dq-pitr restore-prepare: replay WAL to the chosen time, then pause
restore_command = '/usr/local/bin/dq-pitr fetch "%f" "%p"'
recovery_target_time = '$target'
recovery_target_timeline = 'latest'
recovery_target_action = 'pause'
EOF
  touch "$PGDATA/recovery.signal"
  chown -R postgres:postgres "$PGDATA"
  chmod 0700 "$PGDATA"
  log "base backup in place; WAL is replayed when the server starts"
}

cmd_restore_finalize() {
  local conf="$PGDATA/postgresql.auto.conf"
  [ -f "$PGDATA/recovery.signal" ] || die "$PGDATA is not a prepared restore (no recovery.signal)"
  sed -i "s/^recovery_target_action = 'pause'\$/recovery_target_action = 'promote'/" "$conf"
  grep -q "^recovery_target_action = 'promote'\$" "$conf" || die "could not set the restore to promote"
  log "restore set to promote when it reaches its target"
}

case "${1:-}" in
  archive)          shift; cmd_archive "$@" ;;
  fetch)            shift; cmd_fetch "$@" ;;
  backup)           cmd_backup ;;
  retain)           cmd_retain ;;
  list)             cmd_list ;;
  check)            cmd_check ;;
  health)           cmd_health ;;
  loop)             cmd_loop ;;
  restore-prepare)  shift; cmd_restore_prepare "$@" ;;
  restore-finalize) cmd_restore_finalize ;;
  *) sed -n '2,24p' "$0" >&2; exit 2 ;;
esac
