# Recover a Docker installation

Use only your own trusted, complete backup. This procedure replaces no existing data: use a **fresh VPS and new, empty volumes**. Keep the old installation stopped throughout recovery so only one copy can operate on your wallets.

1. Install Docker Engine, Compose, Python 3.9+ and Git on Linux amd64. Clone this repository into `/root/whollycrypto-docker`. **Do not run setup** and do not generate new secrets.
2. Copy the complete backup directory to a root-only location. Confirm `backup.json` says `complete: true` and PostgreSQL major `18`. From that directory run `sha256sum -c data.tar.gz.sha256`.
3. Copy its `setup.json` and entire `secrets/` into the cloned directory, preserving root ownership. Use `0700` for `secrets/` and `0600` for its files and `setup.json`.
4. Build the runtime image with `docker compose build --pull`. Run `docker compose create` to create containers and volumes **without starting them**. Inspect `docker compose ps --all` and the containers' mounts to identify this project's `merchant`, `database`, `nginx` and `certificates` volumes.
5. Use the helper below. Substitute the exact validated volume names from step 4 and the absolute backup directory. The target volumes must have no prior merchant or PostgreSQL data. The new image's default Nginx files can be replaced by your matching Nginx backup.

```bash
docker run --rm --network none --entrypoint tar \
  --mount type=volume,src=whollycrypto_merchant,dst=/snapshot/merchant \
  --mount type=volume,src=whollycrypto_database,dst=/snapshot/database \
  --mount type=volume,src=whollycrypto_nginx,dst=/snapshot/nginx \
  --mount type=volume,src=whollycrypto_certificates,dst=/snapshot/certificates \
  --mount type=bind,src=/absolute/path/to/backup/data.tar.gz,dst=/recovery.tar.gz,readonly \
  whollycrypto-merchant-runtime:1.0.0 \
  -C /snapshot -xzf /recovery.tar.gz
```

On SELinux hosts, first label only the backup archive for the read-only bind: `chcon -t container_file_t -l s0 /absolute/path/to/backup/data.tar.gz`. Keep the archive root-only and leave SELinux enabled.

The archive includes database ownership, the original encryption key, account/installation identity, Basic Auth and TLS certificates. Never substitute a newly initialized database or a different wallet encryption key. Do not restore PostgreSQL 18 physical files into another major version.

6. If the VPS public IP changed, update DNS and the `WHOLLY_PUBLIC_IPS` value in the restored merchant volume's `config/whollycrypto.env` before activation. Preserve the rest of that private file. `setup.json` is used only for initial setup; editing it does not change an installed merchant's active domains.
7. Run `./whollycrypto-docker start` and `./whollycrypto-docker doctor`. The helper also labels its exact container input files on SELinux hosts without disabling enforcement. Verify Basic Auth, admin login, chain connectivity, wallet addresses/balances, invoices and callbacks before resuming business. Certificate/domain changes remain subject to normal validation.

An `update-pending.json` journal stops automatic startup. Preserve it and use `./whollycrypto-docker cli recover`, then `./whollycrypto-docker resume`; never delete the journal to force an uncertain release to start.

Before performing this on a production backup, rehearse on an isolated machine with outbound networking blocked. Keep that rehearsal offline to avoid duplicate notifications, registration or spending. A backup stored only on the same VPS is not an off-server backup.
