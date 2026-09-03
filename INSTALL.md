# LiteBooks Install

LiteBooks has a single install entrypoint:

```bash
./install.sh
```

The installer creates `.env` from `.env.example`, generates a secure Django secret key and database password, starts Docker Compose, runs database migrations, and can create the first owner login.

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

Regenerate `.env` with new secrets:

```bash
./install.sh --force-env
```

Set `LITEBOOKS_PORT` in `.env` to publish a different host port.

## Proxmox

For Proxmox, the easiest path is a Debian or Ubuntu VM with Docker installed, then `./install.sh` inside the LiteBooks checkout.

A Debian LXC can also work if Docker support is enabled for the container, usually with nesting enabled. In Proxmox terms this is still not an ISO; it is a normal container or VM running the LiteBooks Docker Compose stack.

A future appliance-style release could ship either a Proxmox LXC template archive or a VM disk image, but this script is the durable base those images would run on first boot.
