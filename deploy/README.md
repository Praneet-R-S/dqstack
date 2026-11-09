# Deploying DQ-Stack

One server runs everything: PostgreSQL, the application (API, store app, public site,
planner), the scheduler that runs the nightly forecasts, HTTPS, and nightly backups.
That is enough for the first year's scale in the specification (100 stores per
company, 200 tills sending sales at once) on a machine with 4 CPUs and 16 GB of
memory. Larger chains train by category in parallel; see "Scale" below.

## 1. A server in India

Any Linux server with Docker works. The specification asks for an Indian cloud region
by default, for example AWS Mumbai (ap-south-1), Azure Central India, or Google Cloud
Mumbai (asia-south1). Encrypt the disk (the default on all three), open ports 80 and
443 only, and point your domain's DNS at the server.

## 2. Configure

```bash
git clone <this repository> dqstack && cd dqstack
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # for DQ_SECRET_KEY
```

Edit `.env`: the domain (`DQ_DOMAIN`, `DQ_PUBLIC_URL` with `https://`), the secret
key, three different database passwords, and an SMTP account for invitations,
password resets and alerts. Without SMTP, emails are written to the log instead and
an administrator passes links on by hand.

The server refuses to start in production with an unsafe setting: the default secret,
SQLite, cookies without HTTPS, or a non-https address. `manage check` says which.

## 3. Start

```bash
docker compose up -d --build
docker compose ps                         # db, app, worker, caddy, backup: running
docker compose run --rm app manage check  # every line should read well
```

`migrate` runs once on every start and exits; the app waits for it. Caddy obtains the
HTTPS certificate on first request.

Create the vendor's own account for the platform portal (`/platform`). It asks for a
password in the terminal, or reads `DQ_ADMIN_PASSWORD`:

```bash
docker compose run --rm app manage create-platform-admin --email ops@yourcompany.in --name "Operations"
```

Clients then sign up at `https://your-domain/app/#/signup`, or you create a company
for them under `/platform`, which emails their Owner an invitation.

## 4. Database roles

`deploy/postgres-init.sh` makes two roles when the database is first created:

* `dqstack_owner` owns the tables; only migrations use it.
* `dqstack_app` is what the application and the scheduler connect as. It can read and
  write rows but owns nothing, so it cannot disable the triggers that keep stock
  movements and the activity log append-only.

With a managed PostgreSQL, create the same two roles and grants by hand (the script is
the reference), run migrations with the owner's address in `DATABASE_URL`, and run
the app with the app role's.

## 5. Backups and restores

The `backup` service writes a compressed dump every night at about 02:30 to the
`backups` volume and keeps 30 days. Copy that volume off the server every day (for
example to object storage in another region).

**Restore drill (do it every quarter, section 15):**

```bash
docker compose exec backup ls -1 /backups                       # pick a dump
docker compose stop app worker
docker compose exec db dropdb -U postgres dqstack
docker compose exec db createdb -U postgres -O dqstack_owner dqstack
docker compose exec backup pg_restore -h db -U postgres -d dqstack --no-owner \
  --role=dqstack_owner /backups/dqstack-YYYY-MM-DD-HHMM.dump
docker compose run --rm migrate                                 # no-op if current
docker compose start app worker
docker compose run --rm app manage check
```

Record the time it took. The target is service back within 4 hours.

**What this does not yet meet.** Nightly dumps lose up to a day of data in the worst
case. The specification's target is at most 15 minutes lost and restore to any point
in the last 7 days. That needs continuous archiving of PostgreSQL's write-ahead log:
use a managed PostgreSQL with point-in-time recovery (RDS, Azure Database for
PostgreSQL and Cloud SQL all offer it), or add WAL-G to this setup. Tills and phones
lose nothing either way: their unsent work stays on the device until it is accepted.

## 6. Upgrades

```bash
git pull
docker compose up -d --build        # migrations run first, then the new app starts
docker compose run --rm app manage check
```

Read the release notes for anything that needs a step of its own. Migrations are
tested in CI on PostgreSQL 16 against the models on every push.

## 7. Watching it

* `https://your-domain/api/health` answers 200 when the database is reachable;
  point an uptime monitor at it.
* `/platform` shows the scheduler's last pass and the last day's failed forecast runs.
* A failed nightly forecast keeps yesterday's on screen with its age, and alerts the
  company's Admin and Owner in the app and by email.
* `docker compose logs -f app worker` for the logs.

## 8. The planner demo data (optional)

The analysts' planner at `/planner` shows the engine's demonstration data set. To
make it (about 20 minutes, once):

```bash
docker compose run --rm app sh -c "python -m dqstack.data.generate && python -m dqstack.pipeline --export"
```

Companies' own forecasts do not need this; they appear in the store app.

## 9. Scale

Overnight forecasting from saved models takes seconds to minutes per company. Weekly
training is the heavy part (about 20 to 60 minutes per 1,000 product-store pairs on 2
CPUs). For chains far beyond the first year's scale, run more than one `worker`
container on separate machines; each takes the scheduler lock in turn. Splitting one
company's training by category is the next step and is not built yet.

## 10. Before the first paying client

* Have a lawyer review `site/privacy.html` and `site/terms.html`; fill in the legal
  name, contact address and governing law (they are marked).
* If a store will use Checkout as its legal billing system, confirm the receipt
  format with a chartered accountant (GST invoice rules). Until then Checkout is a
  stock register beside the existing billing system.
* WhatsApp sending is by link from the user's own phone; automatic WhatsApp alerts
  need a WhatsApp Business API account.
