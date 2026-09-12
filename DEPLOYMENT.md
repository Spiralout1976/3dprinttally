# Deployment

Install, upgrade, back up, restore and roll back. Requires Docker with Compose
v2 (the `service_completed_successfully` and `service_healthy` conditions are
used).

---

## First install

```sh
# Set REPOSITORY_URL to the HTTPS clone URL of the repository you are using.
git clone "$REPOSITORY_URL" 3dprinttally
cd 3dprinttally
cp .env.example .env
```

Edit `.env`. At minimum set `TZ`, and set `APP_UID` / `APP_GID` to the account
that should own the data on the host (`id -u`, `id -g`).

```sh
docker compose up -d --build
docker compose ps -a
curl --fail http://127.0.0.1:8091/healthz
```

Three services start:

| Service | Role |
|---|---|
| `volume-permissions` | One-shot. Chowns `data/` and `backups/` to `APP_UID:APP_GID` so the non-root app can write. Should exit 0 and stay exited. |
| `app` | The application. Should report healthy within ~30s. |
| `backups` | Writes a verified backup ZIP once the app is healthy, then every 24h. |

The database is created on first run. Open <http://127.0.0.1:8091>.

### Set up in this order

Prices cascade, so the sequence matters:

1. **Settings** — labor rate, electricity rate, printer wattage, machine wear,
   margins, scrap rate, design rate, minimum job charge.
2. **Filament** — the spools you own and what you paid.
3. **Products** — SKUs and the printed parts they are built from.
4. **Job Calculator** — quote a job.

---

## Network exposure

`BIND_ADDRESS` defaults to `127.0.0.1`. The app is reachable only from the host
until you change that.

**Do not widen it until authentication exists.** There is no login page and no
user accounts. Anything that can reach the port can read and change your entire
catalog, costs and customer job history.

Pick one:

**A. Built-in HTTP Basic auth**

```sh
docker compose exec app python tools/password_hash.py
```

Put the result in `.env`, **single-quoted** — the hash contains `$` characters
that Compose will otherwise interpolate away:

```
BASIC_AUTH_USERNAME=you
BASIC_AUTH_PASSWORD_HASH='pbkdf2:sha256:...'
BIND_ADDRESS=0.0.0.0
COOKIE_SECURE=1
```

Then `docker compose up -d`. Setting only one of the two variables is a startup
error, on purpose.

**B. Reverse proxy or VPN**

Leave auth unset and let the proxy or VPN authenticate. Bind to an address the
proxy can actually reach — `127.0.0.1` only works if the proxy runs on the same
host.

Either way, restrict the port with a host or network firewall as well. Set
`COOKIE_SECURE=1` when access is HTTPS-only; leave it `0` for plain HTTP on a
LAN, or browsers will drop the session cookie and nothing will stay logged in.

`TRUSTED_HOSTS` adds explicit allowed hostnames to localhost, 127.0.0.1 and
[::1]. Blank permits only these loopback names. Add every intended proxy or LAN
hostname; unknown hosts receive 400 before authentication. Never use a wildcard.
When TLS terminates at a proxy, forward the correct request scheme using a
trusted WSGI/proxy configuration; do not trust arbitrary forwarded headers.

There is no canonical-host redirect and no shared cookie domain, so the app can
be served under several names at once. Each name gets its own session and CSRF
token, so submit a form on the same hostname that served it.

---

## What is protected out of the box

CSRF checking with constant-time comparison, an Origin check on mutating
requests, upload and form-part limits, `no-store` caching, framing and
MIME-sniffing protection, and a Content Security Policy using per-request
nonces. Uploaded images are re-encoded to drop trailing executable payloads.
Containers run read-only, non-root, with `cap_drop: ALL` and
`no-new-privileges`.

HTTPS and HSTS are the proxy's job. Do not trust client-supplied forwarded
headers without a restricted proxy boundary. `/healthz` is intentionally
unauthenticated and returns only a database liveness flag — no business data.

---

## Upgrading

```sh
cd /path/to/3dprinttally
docker compose stop
```

Take a rollback copy first. This archive contains your `.env` — keep it private
and off any repository:

```sh
umask 077
tar -czf ../3dprinttally-before-update-$(date +%Y%m%d-%H%M%S).tar.gz .
```

Then pull and rebuild:

```sh
umask 022
git pull
docker compose up -d --build
docker compose ps -a
docker compose logs --tail 100 app backups volume-permissions
curl --fail http://127.0.0.1:8091/healthz
```

`git pull` never touches `data/` or `.env` — both are gitignored.

> **The umask matters.** A shell left at `umask 077` (which the `tar` line above
> sets) will write source files at mode 600. Docker copies those modes into the
> image and the non-root container dies with `PermissionError: /app/app.py`.
> Confirm `ls -la app.py` shows `644` before building.

After upgrading, exercise the real paths: add a label, load catalog parts into a
job, calculate, save and edit and delete a test job, download a backup, and check
the logs. Physical label alignment still needs the calibration sheet printed at
Actual Size / 100%.

---

## Backups

The `backups` service writes a verified ZIP — database, product photos and a
SHA-256 manifest — immediately after the app becomes healthy, then every 24
hours, keeping the newest 30 in `./backups/`. Settings shows the last success
time.

Settings also offers an on-demand full ZIP, and `/backup` downloads a consistent
SQLite-only snapshot.

**Local snapshots do not protect against losing the host.** Copy `./backups/`
somewhere else with whatever backup system you already run, and alert if no
successful backup exists in 36 hours. Watch disk space — product photos grow.

### Verify a backup before you trust it

```sh
docker compose exec backups python backup_tools.py \
  --verify /backups/FILE.zip --restore-to /backups/restore-check
```

The destination must not already exist. Verification checks the manifest, the
database integrity and the foreign keys. For real confidence, start a temporary
second instance against the restored directory and open its pages.

### Restoring

1. Stop `app` and `backups`.
2. Move the current `data/` aside — do not delete it until the restore is verified.
3. Move the verified restore into `data/`.
4. `docker compose up -d` so the permissions service prepares ownership.

Keep the existing `.env`. A regenerated session key only signs users out. Never
restore over a database that is being written, and never merge a restored
database with unrelated photos.

---

## Rolling back

Stop all services, move the new `data/` aside separately, and restore both source
and matching data from the private rollback archive. Rebuild. Do not run an older
source release against a newer database. Keep the newer backup ZIPs until the
rollback is confirmed good.

---

## Release checks

On Python 3.12:

```sh
pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python tools/check_dependencies.py
python tools/check_hosts.py tally.example.com
```

Tests use disposable data directories. The dependency check sends only package
names and versions to OSV. Host checks are read-only; run them from every client
network you expect to use, so a firewall rule that only works from one subnet is
caught before your users find it. Test code and test dependencies are excluded
from the production image.


## Security release and source availability

Direct `python app.py` is for local development and binds only 127.0.0.1:8080.
Use Compose for deployment. App and backup services have a 1 GiB memory budget,
two-CPU quota and 128-process limit. The one-shot volume initializer runs as root;
the application and backup services run without root privileges.

Use HTTPS and an authenticating reverse proxy for remote access. Enable rate
limiting at that proxy, including for failed authentication and large downloads.
Plain HTTP Basic credentials are not protected on the wire. Use COOKIE_SECURE=1
for HTTPS. When extending TRUSTED_HOSTS, configure BASIC_AUTH_* or authenticated
proxy access before exposing the service. Do not publish port 8080 directly from
an ad-hoc container.

The sidebar's Source code link downloads the corresponding source from an
explicit SOURCE-MANIFEST.txt, including the full AGPL license. Runtime data,
credentials and backups are never included. When modifying a deployment, update
that manifest for new source files and rebuild/restart the app; its source offer
is cached at startup. Keep build/install instructions and dependency pins with it.

## Backups and fresh installations

Only restore backups from a trusted source. Checksums are not signatures. Restore
verification allows at most 10,000 ZIP entries, a 1 MiB manifest, a 256 MiB database,
32 MiB per image and 512 MiB total decompressed content. It checks actual streamed
bytes and validates image formats/pixels; SQLite checking has a 10-second budget.
A larger installation needs reviewed limits and an independently tested backup
strategy. Keep off-host backups. Rehearse restore before relying on a deployment.

The old reset-catalog.py is retired and always exits without changing data.
For an empty installation, use a separate new installation/data directory and
port, preserving the existing data and verified full backup. No reset deletes
purchases, relationships or photos as part of this release.

The release uses the official Python Alpine image pinned by digest, applies
Alpine package updates during build, and removes pip from the runtime image
after dependency installation. Rebuild from source to change dependencies;
do not install packages into a running container.
