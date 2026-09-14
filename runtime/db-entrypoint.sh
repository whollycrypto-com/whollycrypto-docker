#!/bin/bash
set -Eeuo pipefail
# The official entrypoint starts as root, then drops to the postgres user.
# Do not place database passwords in Compose environment values or arguments.
install -o postgres -g postgres -m 600 /run/secrets/database_password /run/whollycrypto-db-password
exec /usr/local/bin/docker-entrypoint.sh "$@"
