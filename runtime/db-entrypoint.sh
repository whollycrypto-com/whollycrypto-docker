#!/bin/bash
set -Eeuo pipefail
umask 077
# Compatibility for the bind mounts of an existing 1.0/1.1 container. New
# images contain the provisioning helper; legacy containers must keep starting
# unchanged until the guarded runtime upgrade replaces them.
if [[ ! -f /opt/whollycrypto-db-provision.sh ]]; then
  install -o postgres -g postgres -m 600 /run/secrets/database_password /run/whollycrypto-db-password
  exec /usr/local/bin/docker-entrypoint.sh "$@"
fi
# The checkpoint lives OUTSIDE PGDATA so initdb receives an empty directory.
# Keep the official image's initialization/privilege drop; never reset data.
source /usr/local/bin/docker-entrypoint.sh
source /opt/whollycrypto-db-provision.sh

[[ ${1:-} == postgres ]] || { echo 'Only the managed PostgreSQL command is supported.' >&2; exit 1; }
docker_setup_env
umask 022  # Official helper creates root-owned PGDATA parent directories.
docker_create_db_directories
umask 077
if [[ $(id -u) == 0 ]]; then
  install -o postgres -g postgres -m 600 /run/secrets/database_password /run/whollycrypto-db-password
  rm -f -- /run/whollycrypto-db-final
  install -o postgres -g postgres -m 600 /dev/null /run/whollycrypto-db-final
  exec gosu postgres /bin/bash /opt/whollycrypto-db-entrypoint.sh "$@"
fi

db_checkpoint_read
if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  [[ -z $(find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit) ]] || db_fail 'PostgreSQL initialization was interrupted before a valid cluster existed. Preserve this volume and inspect the database log; it was not reset.'
  [[ "$DB_PHASE" != complete ]] || db_fail 'Database checkpoint exists but its cluster is missing. Restore the matching backup; no new database was created.'
  db_checkpoint_write pending
  docker_verify_minimum_env
  docker_error_old_databases
  docker_init_database_dir
  pg_setup_hba_conf "$@"
elif [[ -z "$DB_PHASE" ]]; then
  # Validate legacy installs read-only; never invent missing objects in them.
  DB_PHASE=legacy
fi

export PGPASSWORD="$POSTGRES_PASSWORD"
docker_temp_server_start "$@"
trap 'docker_temp_server_stop >/dev/null 2>&1 || true' EXIT
db_provision
docker_temp_server_stop
trap - EXIT
printf 'ready\n' > /run/whollycrypto-db-final
unset POSTGRES_PASSWORD PGPASSWORD DB_PASSWORD
exec "$@"
