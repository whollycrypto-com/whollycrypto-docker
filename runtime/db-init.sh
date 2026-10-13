#!/bin/bash
set -Eeuo pipefail
if [[ ! -f /opt/whollycrypto-db-provision.sh ]]; then
  password=$(< /run/whollycrypto-db-password)
  [[ "$password" =~ ^[0-9a-f]{64}$ ]] || exit 1
  PGPASSWORD="$POSTGRES_PASSWORD" psql --no-psqlrc -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<SQL
CREATE ROLE whollycrypto LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '$password';
CREATE DATABASE whollycrypto OWNER whollycrypto;
REVOKE ALL ON DATABASE whollycrypto FROM PUBLIC;
SQL
  unset password
  exit 0
fi
# Compatibility entry point; provisioning also runs on managed restarts.
source /opt/whollycrypto-db-provision.sh
db_checkpoint_read
db_provision
