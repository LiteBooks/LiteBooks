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

### 2. Create your private configuration

Copy the supplied example file:

```bash
cp .env.example .env
```

Generate two random values. Run the command twice and keep the results separate:

```bash
openssl rand -hex 32
```

Open `.env` in a text editor and replace:

- `DJANGO_SECRET_KEY` with the first random value
- `POSTGRES_PASSWORD` with the second random value
- `DJANGO_TIME_ZONE` with your local IANA time zone, such as `America/Los_Angeles` or `Europe/London`

Do not reuse your LiteBooks login password for either value. Do not publish or commit `.env`.

For access only from the server itself, the default allowed hosts are sufficient. For access from another computer on your network, add the server's IP address or local hostname to `DJANGO_ALLOWED_HOSTS`:

```dotenv
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50,books.lan
```

### 3. Start LiteBooks

```bash
docker compose up -d --build
```

The first build downloads the required images and can take several minutes. Confirm that both containers are running:

```bash
docker compose ps
```

The `db` service should report healthy and the `web` service should report running.

### 4. Create the first owner login

Replace the example username and password below. The password must contain at least eight characters.

```bash
docker compose exec web python manage.py bootstrap_litebooks \
  --username admin \
  --password 'choose-a-long-unique-password' \
  --first-name 'Your' \
  --last-name 'Name'
```

This creates the first owner and a starter chart of accounts. It is safe to run the command again if setup was interrupted; existing accounts are not duplicated.

### 5. Open the application

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

Create a backup first, then run:

```bash
git pull --ff-only
docker compose up -d --build
```

Database migrations run automatically when the updated application starts. Check the result with `docker compose ps` and `docker compose logs web`.

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
