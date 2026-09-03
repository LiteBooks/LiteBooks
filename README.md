# LiteBooks

LiteBooks is a self-hosted, single-business accounting application built around a double-entry general ledger. It includes accounts, transactions, invoices, bills, linked payments, owner contributions and loans, attachments, monthly locks, financial reports, role-based logins, and Excel exports. The user interface is a locally bundled React and Material UI application with responsive navigation and breadcrumbs.

## Accounting model

The posted journal is the source of truth. Every posting must contain at least two lines, have positive activity, and have equal debits and credits. Invoices, bills, payments, and owner activity all post through the same validation service. Posted general entries can be edited while their accounting periods are open; each edit increments the entry version and stores before/after snapshots in the audit history. Source-managed entries are protected from direct editing.

LiteBooks uses accrual accounting, calendar months, USD, and these access roles:

- **Owner:** all application and administrative access
- **Administrator:** chart of accounts, users, period locks, and bookkeeping
- **Bookkeeper:** transactions, contacts, documents, attachments, and reports
- **Viewer:** read-only access

This software helps maintain books, but it does not replace review by a qualified accountant or automatically guarantee GAAP compliance for every business-specific transaction.

## Docker installation

1. Copy `.env.example` to `.env` and replace both password values and `DJANGO_SECRET_KEY`.
2. Start the database and application:

   ```bash
   docker compose up --build -d
   ```

3. Bootstrap the first owner and starter chart of accounts:

   ```bash
   docker compose exec web python manage.py bootstrap_litebooks --username admin --password 'choose-a-long-password'
   ```

4. Open `http://localhost:8000`.

PostgreSQL and attachments are stored in named Docker volumes. Back up both `postgres_data` and `attachments`; the database backup alone does not contain receipt and invoice files.

## Local development

Python 3.13 is recommended.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install
npm run build
.venv/bin/python manage.py migrate
.venv/bin/python manage.py bootstrap_litebooks --password 'development-password'
.venv/bin/python manage.py runserver
```

Without `DATABASE_URL`, development uses SQLite in `litebooks.sqlite3`. Docker and production use PostgreSQL.

Run the checks and tests with:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py test
```

## Exports and QuickBooks

The Excel export includes the chart of accounts, transaction headers, journal lines, contacts, invoices and bills, payments, owner balances, trial balance, and profit and loss detail. The normalized journal-line sheet is the most useful source for a later QuickBooks conversion. QuickBooks import formats differ by product and region, so treat the workbook as a clean transfer source and map it to the target import format before migration.

## Production notes

- Put the app behind an HTTPS reverse proxy and set `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_SECURE_COOKIES=true`, and `DJANGO_SECURE_SSL_REDIRECT=true`.
- Once HTTPS is confirmed, set `DJANGO_SECURE_HSTS_SECONDS` deliberately; do not enable HSTS while testing a plain-HTTP hostname.
- Keep `DJANGO_DEBUG=false` and use a unique secret key.
- Restrict access to the local network or VPN unless public access is required.
- Schedule encrypted backups and test restoration of both database and attachment volumes.
- Close completed months after reconciliation. Reopening a period is logged.
