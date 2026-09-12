# Deployment

Install, upgrade, back up, restore and roll back. Requires Docker with Compose
v2 (the `service_completed_successfully` and `service_healthy` conditions are
used).

---

## First install

```sh
git clone https://github.com/YOURNAME/3dprinttally.git
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

1. **Settings** — labour rate, electricity rate, printer wattage, machine wear,
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

`TRUSTED_HOSTS` is an optional Host-header allowlist. Blank accepts any hostname,
which is correct behind a proxy that already filters them. When set, `localhost`
and `127.0.0.1` are always included so the container healthcheck keeps working.

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
