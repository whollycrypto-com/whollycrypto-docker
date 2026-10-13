# Docker integration releases

These versions identify the Docker integration, not the merchant application.
Fresh volumes download the latest signed stable merchant release; existing
volumes keep their installed version until a normal signed application update.

## 1.1.0

- Guided Bash bootstrap can install missing Git, Python, Docker Engine, Compose
  and Buildx on explicitly supported Linux distributions, with confirmation.
- Existing engines/containers/volumes are preserved; conflicting runtimes,
  remote/rootless contexts and unsafe environment overrides are rejected.
- Official Docker package repositories with pinned signing-key fingerprints.
- Private, atomic setup journal preserves saved secrets through interruption.
- IPv4/IPv6 port checks, disk checks, input-permission checks and symlink guards.
- Progress ends on verified readiness; safe resume follows ongoing setup and
  leaves ready installations alone. Failed setup reports an actionable error.
- Runtime compatibility verified with signed merchant 8.1.4, PostgreSQL 18.6,
  restart persistence, full cold backup and restore into new volumes.

Host package-manager plans and failure cases are covered by offline tests.
Live installation on every listed distribution and public Let's Encrypt issuance
are not claimed: HTTPS tests use isolated, test-only DNS/certificate fixtures.
Never delete volumes or regenerate secrets to recover a failed installation.

## 1.0.0

- Initial optional Docker Compose integration with signed merchant downloads,
  persistent PostgreSQL/wallet/TLS volumes, update bridge and full backup support.
