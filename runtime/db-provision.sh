#!/bin/bash
# Sourced only by the managed database scripts. No passwords in logs/arguments.
DB_CHECKPOINT=/var/lib/postgresql/whollycrypto-init.state
db_fail() { printf 'Database setup: %s\n' "$1" >&2; exit 1; }
db_sql() {
  PGPASSWORD="$POSTGRES_PASSWORD" psql --no-psqlrc --no-password --host=/var/run/postgresql \
    --username=postgres --dbname=postgres --set=ON_ERROR_STOP=1 --tuples-only --no-align "$@"
}
db_checkpoint_read() {
  DB_PASSWORD=$(< /run/whollycrypto-db-password)
  [[ "$DB_PASSWORD" =~ ^[0-9a-f]{64}$ ]] || db_fail 'Invalid application database secret.'
  DB_DIGEST=$(printf '%s' "$DB_PASSWORD" | sha256sum | cut -d ' ' -f 1)
  DB_PHASE=''
  if [[ -e "$DB_CHECKPOINT" || -L "$DB_CHECKPOINT" ]]; then
    [[ -f "$DB_CHECKPOINT" && ! -L "$DB_CHECKPOINT" && $(stat -c '%u:%a' "$DB_CHECKPOINT") == "$(id -u):600" ]] || db_fail 'Unsafe database checkpoint. No data was changed.'
    local header saved extra
    { IFS= read -r header; IFS= read -r DB_PHASE; IFS= read -r saved; extra=$(cat); } < "$DB_CHECKPOINT"
    [[ "$header" == whollycrypto-db-1 && "$DB_PHASE" =~ ^(pending|complete)$ && "$saved" == "$DB_DIGEST" && -z "$extra" ]] || db_fail 'Database checkpoint and saved credentials do not match. Restore matching secrets; no password was reset.'
  fi
}
db_checkpoint_write() {
  local temporary
  temporary=$(mktemp /var/lib/postgresql/.whollycrypto-db-state.XXXXXXXX)
  printf 'whollycrypto-db-1\n%s\n%s\n' "$1" "$DB_DIGEST" > "$temporary"
  chmod 600 "$temporary"
  sync -f "$temporary"
  mv -T -- "$temporary" "$DB_CHECKPOINT"
  sync -f /var/lib/postgresql
  DB_PHASE=$1
}
db_verify() {
  local result
  result=$(PGPASSWORD="$DB_PASSWORD" psql --no-psqlrc --no-password --host=/var/run/postgresql \
    --username=whollycrypto --dbname=whollycrypto --set=ON_ERROR_STOP=1 --tuples-only --no-align \
    --command="SELECT current_user='whollycrypto' AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND rolcanlogin AND (SELECT pg_get_userbyid(datdba)='whollycrypto' FROM pg_database WHERE datname=current_database()) FROM pg_roles WHERE rolname=current_user;" 2>/dev/null) || return 1
  [[ "$result" == t ]]
}
db_provision() {
  if [[ "$DB_PHASE" == pending ]]; then
    local exists
    exists=$(db_sql --command="SELECT count(*) FROM pg_roles WHERE rolname='whollycrypto';")
    if [[ "$exists" == 0 ]]; then
      # Generated hexadecimal secret only; never arbitrary SQL input.
      db_sql >/dev/null <<SQL
CREATE ROLE whollycrypto LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '$DB_PASSWORD';
SQL
    fi
    exists=$(db_sql --command="SELECT count(*) FROM pg_database WHERE datname='whollycrypto';")
    if [[ "$exists" == 0 ]]; then
      db_sql --command='CREATE DATABASE whollycrypto OWNER whollycrypto;' >/dev/null
    fi
    db_verify || db_fail 'Application credentials, owner or role policy failed verification. Existing roles/passwords were not replaced.'
    db_sql --command='REVOKE ALL ON DATABASE whollycrypto FROM PUBLIC;' >/dev/null
    db_checkpoint_write complete
  else
    db_verify || db_fail 'The existing application database is not ready. Restore matching credentials or inspect the incomplete legacy setup; no existing database was modified.'
    [[ "$DB_PHASE" == complete ]] || db_checkpoint_write complete
  fi
}
