#!/bin/bash
set -Eeuo pipefail
[[ -f /run/whollycrypto-db-final && $(< /run/whollycrypto-db-final) == ready ]] || exit 1
source /opt/whollycrypto-db-provision.sh
# Root health check only reads this PostgreSQL-owned checkpoint and app secret.
[[ -f "$DB_CHECKPOINT" && ! -L "$DB_CHECKPOINT" ]] || exit 1
[[ $(sed -n '2p' "$DB_CHECKPOINT") == complete ]] || exit 1
DB_PASSWORD=$(< /run/whollycrypto-db-password)
[[ "$DB_PASSWORD" =~ ^[0-9a-f]{64}$ ]] || exit 1
export PGCONNECT_TIMEOUT=3
db_verify
