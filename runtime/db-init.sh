#!/bin/bash
set -Eeuo pipefail
password=$(< /run/whollycrypto-db-password)
[[ "$password" =~ ^[0-9a-f]{64}$ ]] || { echo 'Invalid application database secret.' >&2; exit 1; }
# Only generated hexadecimal data is interpolated. Never echo queries/secrets.
PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<SQL
CREATE ROLE whollycrypto LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '$password';
CREATE DATABASE whollycrypto OWNER whollycrypto;
REVOKE ALL ON DATABASE whollycrypto FROM PUBLIC;
SQL
unset password
