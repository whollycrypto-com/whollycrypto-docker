# Wholly Crypto · Docker

An optional Docker Compose installation for [Wholly Crypto](https://www.whollycrypto.com/), the self-hosted, non-custodial, multi-chain payment processor.

**Your server. Your wallets. The same merchant application.** This repository contains container setup code, not the merchant backend source. The runtime downloads and verifies the official signed merchant release, with the official GitHub release mirror as a fallback.

The [native Linux installer](https://www.whollycrypto.com/documentation/#install) remains available. Choose **one** installation method on a fresh VPS; this setup does not migrate or overwrite a native installation.

## Install

Start with a fresh **Linux amd64 / x86-64** VPS, root access, Bash and curl. The **1.1.0 bootstrap offers to install missing Python 3.9+, Git, Docker Engine, Docker Compose and Buildx**. It asks before package changes. It never removes an existing container runtime, deletes volumes or stops another web server. ARM64 images are not available yet.

Automatic prerequisite installation supports Ubuntu 22.04/24.04/26.04, Debian 12/13, Fedora 43/44/45 and RHEL 9/10 with systemd. Other maintained Linux distributions can use an existing rootful Docker Engine with Compose v2+ and Buildx after you install the prerequisites yourself. Conflicting runtimes/repositories stop with instructions for manual review. See [Docker's distribution guides](https://docs.docker.com/engine/install/).

For light use, start with 2 vCPU, 4 GB RAM and 30 GB SSD. Allow room for the OS, images, invoice history and backups; have at least 3 GiB free before setup. More payment activity and enabled chains may require more capacity.

1. Point your merchant, checkout and API DNS records directly at this VPS. Defaults are `merchant.example.com`, `pay.example.com` and `api.example.com`; setup lets you choose other labels and additional base domains.
2. Keep ports **80 and 443** available and open in your VPS/provider firewalls. Keep SSH allowed. With Cloudflare, use **DNS-only during initial setup**; enable the proxy after HTTPS validation finishes.
3. Run as root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/whollycrypto-com/whollycrypto-docker/v1.1.0/install.sh)
```

The command needs Bash and curl to fetch the bootstrap. If curl is missing, install it with your distribution's package manager first. The integration is pinned to v1.1.0; a **new application volume downloads the latest signed stable merchant release**, with signature/checksum verification before execution. An existing application volume keeps its installed version.

Prefer to inspect the code first, and already have Git?

```bash
git clone --branch v1.1.0 --depth 1 https://github.com/whollycrypto-com/whollycrypto-docker.git /root/whollycrypto-docker &&
cd /root/whollycrypto-docker &&
./whollycrypto-docker setup
```

`./whollycrypto-docker check` is a read-only prerequisite check. `./whollycrypto-docker prerequisites` installs only missing host tools, not the application. `--yes` approves prerequisite installation; it does **not** answer the domain/password wizard or accept software agreements. Existing Docker engines are not upgraded automatically. A stopped engine is started only with your confirmation.

Choose your domains, public VPS IP, renewal email, default fiat currency and HTTP Basic login. The helper builds the runtime image, starts PostgreSQL, verifies the signed merchant download, issues Let's Encrypt certificates and starts the merchant. **No Rust compiler, blockchain node or merchant source checkout is needed.** The first image build and database initialization can take several minutes.

Setup reports progress and exits successfully only after verified application readiness. Ctrl+C closes the view, not the running containers. Open the displayed merchant URL, enter the Basic Auth login, then register your separate administrator email/password. Normal automatic registration and welcome-credit rules apply.

Private setup files are `setup.json` and `secrets/`. Keep them root-owned, mode `0600`, with `secrets/` mode `0700`. A generated Basic Auth password is shown only by the interactive setup helper and retained in `secrets/basic_password`. Never commit, upload or share these files.

If setup stops, fix the reported cause, then run `/root/whollycrypto-docker/whollycrypto-docker resume`. A private `.setup-pending.json` journal recovers interrupted configuration without generating new secrets. Never delete it to start over. An already-ready installation is not rebuilt/restarted by `resume`; active setup is followed, and interrupted updates require `cli recover` first.

## Day-to-day commands

Run these from `/root/whollycrypto-docker`:

| Command | Purpose |
| --- | --- |
| `./whollycrypto-docker check` | Read-only host prerequisite check |
| `./whollycrypto-docker prerequisites` | Offer to install missing host tools only |
| `./whollycrypto-docker status` | Container and health status |
| `./whollycrypto-docker logs` | Follow setup and worker lifecycle logs |
| `./whollycrypto-docker cli logs` | Recent private merchant application log |
| `./whollycrypto-docker doctor` | Read-only merchant checks |
| `./whollycrypto-docker doctor --fix` | The merchant's bounded repairs |
| `./whollycrypto-docker resume` | Retry unfinished setup without regenerating keys |
| `./whollycrypto-docker update` | Signed merchant update, including normal credit checks |
| `./whollycrypto-docker backup` | Verified full cold backup; briefly pauses checkout |
| `./whollycrypto-docker cli backup` | Merchant recovery bundle without a full container snapshot |
| `./whollycrypto-docker cli htaccess` | Change the console HTTP Basic login |
| `./whollycrypto-docker cli admin-reset` | Recover a console administrator password |
| `./whollycrypto-docker cli 2fa-reset` | Reset an account's 2FA through the recovery flow |
| `./whollycrypto-docker cli ssl` | Inspect/repair the merchant's managed HTTPS certificates |
| `./whollycrypto-docker cli transfers status` | Check the server's transfer safety setting |
| `./whollycrypto-docker stop` / `start` | Stop/start without deleting data |

Add `--help` to an underlying merchant command for its options, for example `./whollycrypto-docker cli ssl --help`.

**Console updates work too:** Settings → System uses the same signed update worker as the CLI. Active background jobs are drained before an update. Never interrupt an update by recreating/stopping the container. If interrupted, preserve the volumes and run `./whollycrypto-docker cli recover`, then `./whollycrypto-docker resume`.

### Runtime image updates

The Docker integration/runtime image and merchant application have separate versions. Rebuilding the image updates container dependencies and integration code; it **does not** install a newer merchant binary into an existing volume or bypass application update eligibility.

After a full backup, and when no update/domain job is running:

```bash
git diff --quiet && git diff --cached --quiet &&
git fetch origin tag v1.1.0 && git switch --detach v1.1.0
docker compose build --pull
./whollycrypto-docker start
./whollycrypto-docker doctor
```

Replace the tag only with a newer published integration release you have reviewed. Keep any local changes; do not use reset/clean to force an upgrade. Older v1.0.0 checkouts can use these steps too. Run `./whollycrypto-docker prerequisites` after switching if Compose/Buildx needs to be installed. If a package transaction would replace or upgrade your existing Docker engine, setup stops for manual review.

Keep the same Compose project/directory. The helper fixes the project name to `whollycrypto` and refuses remote Docker or conflicting Compose environment overrides; changing the project name creates different volumes. PostgreSQL is pinned to a tested **18.6** image/digest; future major database upgrades require an explicit migration, not a tag swap.

## What runs where

| Container / volume | Contents |
| --- | --- |
| `merchant` service | Signed merchant binary, Nginx, Certbot and a small service supervisor |
| `database` service | PostgreSQL 18; dedicated non-superuser application role |
| `merchant` volume | Installed merchant, encrypted wallet material, configuration, uploads and recovery bundles |
| `database` volume | PostgreSQL database files |
| `nginx` volume | Managed web configuration, Basic Auth and live domain access restrictions |
| `certificates` volume | Let's Encrypt account, certificates, renewal settings and TLS private keys |
| `database_socket` volume | Local database socket, not persistent business data |

Only HTTP/HTTPS are published. The merchant listens on `127.0.0.1:8080` **inside** its container. PostgreSQL listens only on the shared Unix socket; neither 5432 nor 8080 is exposed on the host. No privileged container, host networking, host systemd, host Docker socket or cgroup write access is required. Processes use the internal container root account, not a host filesystem bind.

Rates, scanners, notifications, exchanges and sweeps run in the merchant application as usual. The supervisor handles domain activation (after completion, every 10 seconds), software requests (15 seconds), token icons (6 hours), backups (daily) and certificate renewal (12 hours). Logs are bounded; scheduled merchant backups retain three snapshots. Host-created full backups are not deleted automatically.

The service bridge supports **only** known merchant jobs. It is not systemd and does not pretend to provide native systemd sandboxing. Docker's namespaces, default seccomp profile, no-new-privileges setting, reduced capabilities and absence of host mounts form the container boundary. Access to root or the Docker daemon is still full administrative control.

## Back up before taking payments

Export project wallet backups in the console and keep a secure off-server copy. Also run:

```bash
./whollycrypto-docker backup
```

This drains workers, stops both containers for a consistent snapshot, verifies the archive, saves the setup/secret files, and restarts services that were running. Copy the **entire timestamped directory** from `backups/` to secure offline storage. It contains **unencrypted private keys and database credentials**. Encrypt it before storing with a third party.

The merchant's scheduled recovery bundles contain database dumps and wallet/configuration data, but are not a substitute for the full Docker backup: the full snapshot also includes TLS and the host-side setup secrets. Follow a connected Lightning wallet's own channel/seed backup procedure separately.

**Never run `docker compose down -v`, delete the data volumes or regenerate secrets to fix an error.** Deleting wallet keys or the database can permanently lose funds or payment history. `docker compose stop` preserves data.

### Recovery

Use a trusted backup and a fresh destination. Do not start a second live copy of the same merchant: duplicate scanners, notifications and transfer workers can operate on the same wallets.

See [the recovery procedure](doc/recovery.md) for restoring the four durable volumes and the matching setup secrets. Recovery is an operator procedure, not automatic database rollback. Test it before relying on backups.

## Troubleshooting and limits

- **Setup failed:** inspect `logs`, fix DNS/firewall/disk issues, then `resume`. Existing keys are preserved. Failed bootstrap leaves checkout unavailable until setup completes.
- **DNS:** enter the host's public IP, not the Docker subnet. Remove stale AAAA records if you do not serve IPv6. DNS caching and propagation still apply; setup never skips validation.
- **Port conflict:** use another VPS or plan a migration. This stack manages its own Nginx and certificates. An external reverse proxy, rootless Docker and Docker Desktop are not supported configurations for this initial release.
- **Host security:** Docker-published ports can bypass some UFW rules. Use provider firewall rules and Docker-aware filtering; see [Docker firewall guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/). Keep Basic Auth enabled and configure domain/API IP restrictions in Wholly Crypto where appropriate. Leave public checkout accessible to customers.
- **SELinux:** the setup helper keeps enforcement enabled and labels only its six bind-mounted input files for container reads. After replacing those files or restoring on an SELinux host, use the helper's `start`/`resume` command to reapply labels. Do not disable SELinux to work around a mount permission error.
- **Low credits:** Docker has the same payment and degraded-mode policies as the native installation. It is not a separate free-fee edition.
- **No container health yet:** first-start downloads, migrations and certificates take time. The health check allows 15 minutes for startup; check logs rather than deleting volumes.

## Development and verification

```bash
python3 -B -m unittest discover -s tests -v
docker compose config --quiet
```

Integration verification uses isolated PostgreSQL and merchant containers, no published ports, a signed public release and local test certificates. It checks bootstrap/resume, HTTP Basic protection, database health, worker status, restart persistence and recovery. Live ACME issuance requires your real DNS records and reachable port 80. No payment or real merchant account is created by the tests.

Docker integration: MIT. The downloaded merchant application is separately licensed; its proprietary license and required third-party notices remain in the signed package. [Product documentation](https://www.whollycrypto.com/documentation/docker/) · [Merchant releases](https://github.com/whollycrypto-com/whollycrypto/releases) · [Report an issue](https://github.com/whollycrypto-com/whollycrypto-docker/issues).
