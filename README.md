# LiteBooks

LiteBooks is a self-hosted accounting application for one business. It provides a double-entry general ledger, chart of accounts, transactions, invoices and bills, linked payments, owner contributions and loans, attachments, monthly locks, financial reports, user logins, and Excel exports.

The recommended installation uses Docker Compose. It runs LiteBooks and PostgreSQL together, keeps all accounting data on your server, and works on a Linux VM, home server, mini PC, or NAS that supports Docker Compose.

> LiteBooks helps maintain your books, but it does not replace review by a qualified accountant or guarantee GAAP compliance for every business-specific transaction.

## Before you start

You need:

- A server with Docker Engine and the Docker Compose plugin installed
- Git installed on the server
- A terminal or SSH connection to the server
- TCP port `8000` available, unless you will place LiteBooks behind a reverse proxy
- Enough disk space for your database, receipts, invoices, and backups

These instructions use `docker compose`, the current Compose command. If your system only provides the older `docker-compose` command, install the Docker Compose plugin before continuing.

## Install LiteBooks

### 1. Download the application

```bash
git clone https://github.com/LiteBooks/LiteBooks.git
cd LiteBooks
```

### 2. Run the installer

```bash
./install.sh
```

The installer creates `.env`, generates private secrets, builds and starts the Docker Compose stack, runs database migrations, and prompts for the first owner login password.

For access from another computer on your network, add the server's IP address or local hostname to `DJANGO_ALLOWED_HOSTS` in `.env`, then apply the setting:

```dotenv
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50,books.lan
```

```bash
docker compose up -d
```

### 3. Open the application

On the server, open:

```text
http://localhost:8000
```

From another computer on the same network, replace `SERVER-IP` with the address of your server:

```text
http://SERVER-IP:8000
```

Sign in with the owner username and password from the previous step. Additional local users can be created from **Settings > Users**. LiteBooks does not send email, so give each user their username and temporary password directly.

## Internet access and HTTPS

Do not expose port `8000` directly to the public internet. Put LiteBooks behind an HTTPS reverse proxy such as Caddy, Nginx Proxy Manager, Traefik, or Nginx, and restrict direct access to port `8000` with your server firewall.

For a public hostname, update `.env` with the exact hostname and HTTPS origin:

```dotenv
DJANGO_ALLOWED_HOSTS=books.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://books.example.com
DJANGO_SECURE_COOKIES=true
DJANGO_SECURE_SSL_REDIRECT=true
```

Then apply the settings:

```bash
docker compose up -d
```

Your reverse proxy must forward the original host and protocol. A minimal Caddy site is:

```caddyfile
books.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Only set `DJANGO_SECURE_SSL_REDIRECT=true` after HTTPS is working. Enable HSTS later, and deliberately, by setting `DJANGO_SECURE_HSTS_SECONDS`; an incorrect HSTS configuration can make a hostname difficult to recover in a browser.

## Everyday administration

Check service status:

```bash
docker compose ps
```

View application logs:

```bash
docker compose logs -f web
```

Restart LiteBooks:

```bash
docker compose restart
```

Stop and start it without deleting data:

```bash
docker compose stop
docker compose start
```

`docker compose down` removes the containers and network but preserves the named data volumes. Do not add `-v` unless you intentionally want to permanently delete the database and every uploaded attachment.

## Update LiteBooks

LiteBooks updates itself from the web interface. Sign in as an owner and open
**Settings -> Software update**. The page shows the version you are running, whether
`main` has moved ahead, and the list of commits you would be installing. Press
**Update now** and LiteBooks will:

1. Take a full database backup into the `update_state` volume.
2. Pull the published image for that commit from GHCR.
3. Restart the web container, which applies database migrations on start.
4. Verify that the new build is actually serving before reporting success.

The browser reconnects on its own when the restart finishes. Administrators see the
page and can check for updates; only an owner can install one.

LiteBooks checks GitHub at most once a day, during a page load. **Check for updates**
forces a check immediately.

### How the update actually runs

The web container runs unprivileged and has no access to Docker, so it cannot update
itself. The `updater` sidecar in `compose.yaml` does that work: the web container writes
an update request into a shared volume, and the updater pulls the image and restarts the
service.

That sidecar mounts the Docker socket, which is **root-equivalent on the host** -- the
same tradeoff Watchtower and the Home Assistant supervisor make. To opt out, set
`LITEBOOKS_UPDATES_ENABLED=false` in `.env` and remove the `updater` service from
`compose.yaml`. The UI then reports that updates are disabled, and you update from the
command line instead.

### If an update fails

LiteBooks stops and shows the log rather than rolling back. This is deliberate: the
container applies database migrations as it starts, Django migrations are not reliably
reversible, and restoring the old image over a migrated database would leave the
application and the schema out of step.

The failure report names the pre-update backup. Restore it with the commands in
[Restore a backup](#restore-a-backup), then set `LITEBOOKS_TAG` in `.env` back to the
previous `sha-...` value and run `docker compose up -d web`.

### Updating from the command line

```bash
docker compose pull
docker compose up -d
```

To run a specific build, set `LITEBOOKS_TAG=sha-abc1234` in `.env` first. To build from
your own checkout instead of the published image:

```bash
git pull --ff-only
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

Check the result with `docker compose ps` and `docker compose logs web`. `GET /healthz/`
reports the running commit and whether migrations are pending.

## Back up your data

LiteBooks stores data in two Docker volumes:

- `postgres_data` contains the PostgreSQL accounting database and user accounts.
- `attachments` contains uploaded receipts, invoices, and other files.

An Excel export is useful for reporting and migration, but it is not a complete backup. Back up both volumes.

The following commands create timestamped database and attachment archives in a local `backups` directory:

```bash
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M%S)

docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  | gzip > "backups/litebooks-db-$STAMP.sql.gz"

docker compose exec -T web \
  tar -czf - -C /app media \
  > "backups/litebooks-attachments-$STAMP.tar.gz"
```

Copy backups to a different machine or encrypted backup destination. Test your restore process periodically and keep more than one backup generation.

### Restore a backup

Restoring replaces the current LiteBooks database and attachments. Stop, verify the selected backup filenames, and preserve a copy of the current data before proceeding.

```bash
docker compose stop web

docker compose exec -T db sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'

gunzip -c backups/litebooks-db-YYYYMMDD-HHMMSS.sql.gz \
  | docker compose exec -T db sh -c \
    'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'

docker compose run --rm -T --entrypoint sh web -c \
  'rm -rf /app/media/* && tar -xzf - -C /app' \
  < backups/litebooks-attachments-YYYYMMDD-HHMMSS.tar.gz

docker compose up -d
```

Replace both `YYYYMMDD-HHMMSS` placeholders with the same backup timestamp, then sign in and verify recent transactions and attachments.

## Troubleshooting

### The page does not open

Run `docker compose ps` and `docker compose logs web`. Also confirm that port `8000` is allowed through the server firewall and that the server address appears in `DJANGO_ALLOWED_HOSTS`.

### You see a DisallowedHost or CSRF error

Add the hostname to `DJANGO_ALLOWED_HOSTS`. For HTTPS, add the full origin, including `https://`, to `DJANGO_CSRF_TRUSTED_ORIGINS`, then run `docker compose up -d`.

### You forgot a user's password

An owner with user-management access can change it in **Settings > Users**. From the server terminal, you can also reset a known username with:

```bash
docker compose exec web python manage.py changepassword USERNAME
```

### A container keeps restarting

Inspect both logs:

```bash
docker compose logs db
docker compose logs web
```

The most common first-install cause is an unchanged or mismatched `POSTGRES_PASSWORD` in `.env`.

## Where your data goes

LiteBooks does not require a cloud service or email provider. The application, database, credentials, and uploaded documents remain on the Docker host and in the backups you create. Normal Docker container replacement and application updates preserve the named volumes.
