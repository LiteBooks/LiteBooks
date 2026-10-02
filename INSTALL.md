# LiteBooks Install

LiteBooks has a single install entrypoint:

```bash
./install.sh
```

The installer creates `.env` from `.env.example`, generates a secure Django secret key and database password, pulls the published container images, starts Docker Compose, runs database migrations, and can create the first owner login.

## Common Install

```bash
git clone https://github.com/LiteBooks/LiteBooks.git
cd LiteBooks
./install.sh
```

The installer prompts for the first owner password. After startup, open:

```text
http://127.0.0.1:8000
```

From another machine, replace `127.0.0.1` with the server IP or hostname and make sure that value is listed in `DJANGO_ALLOWED_HOSTS` in `.env`.

## Useful Options

Skip first-owner setup:

```bash
./install.sh --skip-bootstrap
```

Use a different first owner username:

```bash
./install.sh --admin-username owner
```

Prepare config and containers but leave services stopped:

```bash
./install.sh --no-start
```

Build the images from this checkout instead of pulling the published ones:

```bash
./install.sh --build
```

Regenerate `.env` with new secrets:

```bash
./install.sh --force-env
```

Warning: `--force-env` generates a new `POSTGRES_PASSWORD`, which will lock an existing
installation out of its own database volume. Use it only on a fresh install.

Set `LITEBOOKS_PORT` in `.env` to publish a different host port.

## Keeping it updated

After installation, LiteBooks updates itself from **Settings -> Software update** in the
web interface. See [Update LiteBooks](README.md#update-litebooks) for how that works and
how to turn it off.

## Proxmox

For Proxmox, the easiest path is a Debian or Ubuntu VM with Docker installed, then `./install.sh` inside the LiteBooks checkout.

A Debian LXC can also work if Docker support is enabled for the container, usually with nesting enabled. In Proxmox terms this is still not an ISO; it is a normal container or VM running the LiteBooks Docker Compose stack.

A future appliance-style release could ship either a Proxmox LXC template archive or a VM disk image, but this script is the durable base those images would run on first boot.
