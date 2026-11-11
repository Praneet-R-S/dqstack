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
docker compose ps                         # db, app, worker, caddy, pitr, backup: running
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

Two independent copies, made by different tools in different formats, so one fault
cannot spoil both:

| | What | Restore to | Most data lost |
|---|---|---|---|
| `db` + `pitr` (WAL-G) | Every change, as PostgreSQL's write-ahead log (WAL), shipped to backup storage at least every 5 minutes; a base backup every night at 01:30 (a full one weekly, changes-only in between); kept 8 days | any moment in the last 7 days | about 5 minutes (`DQ_PITR_ARCHIVE_TIMEOUT=300`) |
| `backup` (pg_dump) | A compressed dump every night at about 02:30 in the `backups` volume; kept 30 days | that night | a day |

The specification's targets are "at most 15 minutes of server-side data" lost and a
restore to a chosen time "within 4 hours". Tills and phones lose nothing either way:
their unsent work stays on the device until the server accepts it.

### Where backups go

Set these in `.env` (`.env.example` explains each):

* **Object storage, somewhere other than this server.** This is the real backup.
  `WALG_S3_PREFIX=s3://bucket/dqstack`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`; for a non-AWS S3-compatible store also `AWS_ENDPOINT` and
  usually `AWS_S3_FORCE_PATH_STYLE=true`. Use a bucket in another region or with
  another provider, give the key access to that bucket only, and turn on the
  bucket's versioning or object lock if it has them, so a stolen server key cannot
  delete the history.
* **The local folder** (`WALG_FILE_PREFIX=/walg/dqstack`, the default when no bucket
  is set) puts backups in the `walg` volume on this same server. It undoes mistakes
  (a dropped table, a bad import) but **is not a backup**: a lost disk or server takes
  it too. For a small install only, and copy the volume off the server daily.
* **Encryption.** `WALG_LIBSODIUM_KEY` (`openssl rand -hex 32`, with
  `WALG_LIBSODIUM_KEY_TRANSFORM=hex`) encrypts every file on the server before it is
  sent. Keep the key in a password manager as well as in `.env`: without it the
  backups cannot be read by anyone. Object storage can also encrypt at rest
  (`WALG_S3_SSE=AES256`, or `aws:kms` with `WALG_S3_SSE_KMS_ID`). Backups sent to
  object storage with neither are reported as a problem.

Pinned versions: PostgreSQL 16.15 (Debian image, by digest) and WAL-G 3.0.9, whose
download is checked against its published SHA-256 when the image is built
(`deploy/postgres/Dockerfile`).

**Moving an existing install.** Earlier versions of this setup ran
`postgres:16-alpine`. Do not point the new image at that data volume: the two sort
text differently, which leaves indexes on text columns out of order. Take a dump
with the old setup, start the new one on an empty volume, and restore the dump into
it (the pg_dump steps below).

### Is it working?

```bash
docker compose ps pitr                        # "healthy"
docker compose exec pitr dq-pitr check        # storage, encryption, backups, WAL chain; ends OK
```

`dq-pitr check` reports a problem when WAL archiving is failing (WAL then piles up on
the database server's disk until it fills), when the newest base backup is more than
26 hours old, or when there is no storage or no encryption. The `pitr` service runs
the same check every 10 minutes and turns unhealthy on a problem; point your
monitoring at `docker compose ps` or its logs.

### Restore to a chosen time

```bash
bash deploy/restore-to-time.sh "2026-11-09 14:30:00+05:30"
```

Run it on the server from the DQ-Stack folder. Give the time with its offset
(`+05:30` for India, `Z` for UTC). It:

1. makes a fresh volume and fetches into it the newest base backup that finished
   before that time;
2. starts a private PostgreSQL on it (no ports, archiving off) that replays the WAL
   up to that time and pauses there, read-only, and prints what it holds (bills, the
   last stock movement, the last activity) and how long fetch and replay took;
3. asks you to type `yes`. Until you do, **nothing in use is touched**: the live
   database, its volume and the backup storage are as they were. Look first if you
   like: `docker exec -it dqstack_pgdata_restore_...-check psql -U postgres -d dqstack`.
4. On `yes` it stops app, worker, pitr, backup and db, points `DQ_PGDATA_VOLUME` in
   `.env` at the restored volume (the old `.env` is kept beside it), lets the
   database finish recovery and open on a new timeline, takes a fresh base backup,
   and starts everything again.

The old volume is kept. To go back, set `DQ_PGDATA_VOLUME` to the old name it
printed and run `docker compose up -d` (anything written since the switch is then
lost). Remove the old volume only when sure: `docker volume rm <name>`.

If you answer anything but `yes`, it prints how to switch later
(`bash deploy/restore-to-time.sh --switch-to <volume>`) or throw the restore away.

If it says the recovery target was not reached, the archive does not yet hold WAL up
to that time (it is shipped within 5 minutes). If the old database still runs, ship
it now with `docker compose exec db psql -U postgres -c "SELECT pg_switch_wal()"`
and try again a minute later, or choose an earlier time.

### Restore from a nightly dump

The independent copy, for when the WAL-G backups themselves are unusable:

```bash
docker compose exec backup ls -1 /backups                       # pick a dump
docker compose stop app worker pitr
docker compose exec db dropdb -U postgres dqstack
docker compose exec db createdb -U postgres -O dqstack_owner dqstack
docker compose exec backup pg_restore -h db -U postgres -d dqstack --no-owner \
  --role=dqstack_owner /backups/dqstack-YYYY-MM-DD-HHMM.dump
docker compose run --rm migrate                                 # no-op if current
docker compose start app worker pitr
docker compose exec pitr dq-pitr backup                         # a base backup of it
docker compose run --rm app manage check
```

Copy the `backups` volume off the server every day as well (for example to the same
object storage, under another prefix).

### The quarterly drill (section 15)

Every quarter, and after any change to the server or the backup settings:

1. Pick a time about an hour ago and note what you did around then (a bill, a stock
   count) so you can recognise it.
2. Run `bash deploy/restore-to-time.sh "<that time>+05:30"` and answer **no** at the
   question. The live system keeps running throughout.
3. Check the printed summary and look inside the restored copy: your change from
   just before the time is there, nothing from after it.
4. Record the fetch and replay times it printed, and the database size
   (`docker compose exec db psql -U postgres -c "SELECT pg_size_pretty(pg_database_size('dqstack'))"`).
   The target is the service back within 4 hours; if the drill takes more than about
   an hour, plan before the database grows further (a faster store, more frequent
   full backups with `WALG_DELTA_MAX_STEPS`).
5. Throw the restore away with the two commands it printed.
6. Once a year, also restore a nightly dump into a scratch database the same way.

CI runs the same drill on every push (job `pitr-drill`): it writes rows, notes a time
T, writes more, kills the database, restores to T with this script, and fails unless
exactly the rows from before T are back, the old volume is untouched, the last write
reached storage within `archive_timeout`, and archiving carries on afterwards. That
proves the mechanism; only your drill proves your storage, your key and your timing.

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
* WhatsApp works by link from the user's own phone until a WhatsApp Business API
  account is set up (section 11); then orders, alerts and receipts go from the server.

## 11. WhatsApp and text messages (optional)

Without these settings nothing changes: WhatsApp works by link (the person's own
phone opens WhatsApp with the order or receipt filled in and they press send),
alerts come in the app and by email, and the till's "Send by SMS" button says text
messages are not set up.

### WhatsApp Business Cloud API

With it, the server sends purchase orders (the PDF itself), alerts to people who
chose WhatsApp, and receipts from the till.

1. In Meta Business Manager, create a WhatsApp Business account and add the
   business phone number (it cannot stay registered in the WhatsApp phone app).
2. Make a system user with the `whatsapp_business_messaging` permission and a
   permanent access token.
3. Set in `.env`:

   | Variable | What |
   |---|---|
   | `DQ_WHATSAPP_TOKEN` | the permanent token |
   | `DQ_WHATSAPP_PHONE_NUMBER_ID` | the number's id from WhatsApp Manager (not the number) |
   | `DQ_WHATSAPP_API_VERSION` | optional, default `v21.0` |
   | `DQ_WHATSAPP_TEMPLATE_PO` | approved template for purchase orders |
   | `DQ_WHATSAPP_TEMPLATE_ALERT` | approved template for alerts |
   | `DQ_WHATSAPP_TEMPLATE_RECEIPT` | approved template for receipts |
   | `DQ_WHATSAPP_LANGUAGE` | the templates' language code, default `en` |

4. Restart (`docker compose up -d`).

**Templates must be approved by Meta before use.** WhatsApp delivers a message the
business starts only through an approved template; plain text is delivered only
within 24 hours of the other person's last message to the business. Leave a
template name empty and that message goes as plain text (a PO as a PDF with a
caption), which in practice reaches only suppliers or customers who wrote recently.
Create the templates in WhatsApp Manager (category Utility) with exactly these
blanks, in this order; wording is yours:

| Template | Header | Body blanks | Example body |
|---|---|---|---|
| PO | Document | `{{1}}` order number, `{{2}}` company and store, `{{3}}` total | New purchase order {{1}} from {{2}}. Total {{3}}. The order is attached. |
| Alert | none | `{{1}}` person's name, `{{2}}` the alert (several are joined with semicolons) | Hello {{1}}, DQ-Stack alert: {{2}} |
| Receipt | none | `{{1}}` store (with GSTIN), `{{2}}` bill number, `{{3}}` date and time, `{{4}}` items, `{{5}}` total and payment | Thank you for shopping at {{1}}. Bill {{2}} on {{3}}: {{4}}. Total {{5}}. |

A refusal from WhatsApp (expired token, unapproved template, number not on
WhatsApp, outside the 24-hour window) is shown on screen in plain words; for alerts,
which are sent by the worker, it is written to the log (`docker compose logs
worker`) and the alert still shows in the app. Each alert is sent once; several
raised together reach a person as one message. People need a mobile number on their
account for WhatsApp alerts; the alert settings screen says when one is missing.

Receipts are limited to 60 a store per 10 minutes. Meta charges per conversation;
see its current price list for India.

### Text messages (SMS)

Used for receipts. Set `DQ_SMS_PROVIDER`:

* `twilio` with `DQ_TWILIO_ACCOUNT_SID`, `DQ_TWILIO_AUTH_TOKEN` and `DQ_SMS_FROM`;
* `webhook` with `DQ_SMS_WEBHOOK_URL` (https) and optionally `DQ_SMS_WEBHOOK_TOKEN`,
  for an Indian gateway (MSG91, Gupshup, Exotel ...) behind a small adapter that
  takes `{"to": "+9198...", "text": "..."}` as JSON with a bearer token.

**Indian SMS needs DLT registration before anything is delivered.** Register the
business as a principal entity on a DLT portal (Jio, Vodafone Idea, Airtel or BSNL),
register a sender id (header), and register the receipt text as a transactional
content template, with its variable parts marked, before turning SMS on. Messages
that do not match a registered template are dropped by the operators without an
error to the sender. Amounts are written "Rs." so a receipt stays in the plain SMS
alphabet.

## 12. Sign-in codes, and Google and Microsoft sign-in (optional)

**Codes by text message.** "Forgot password" can send a 6-digit code to the phone
on the person's account, and a company can ask for a code after the password
(Settings, Company, *Sign-in security*: off, for Owners and Admins, or for everyone).
Codes go by text message through the SMS provider in section 11 (`DQ_SMS_PROVIDER`
and its variables, documented in `dqstack/app/sms.py`); without one, a production
server does not offer reset by phone and sends sign-in codes by email instead. In
India, register the two code messages as DLT templates as well as the receipt:

* `Your DQ-Stack sign-in code is {#var#}. It works for 10 minutes. Do not share it with anyone.`
* `Your DQ-Stack password reset code is {#var#}. It works for 10 minutes. If you did not ask for it, ignore this message.`

A code works for 10 minutes and five tries; only a keyed hash of it is stored.

**Google and Microsoft.** Each sign-in button appears only when the server has that
provider's keys. It signs in people who already have an account here with the same
email; it never creates a company or an account. Owners and Admins can turn it off
for their company (Settings, Company). A Google or Microsoft sign-in counts as
strong, so the company's extra code is not asked for.

| Variable | What |
|---|---|
| `DQ_GOOGLE_CLIENT_ID`, `DQ_GOOGLE_CLIENT_SECRET` | Google Cloud console, APIs and services, Credentials: an OAuth client of type *Web application* |
| `DQ_MICROSOFT_CLIENT_ID`, `DQ_MICROSOFT_CLIENT_SECRET` | Microsoft Entra admin centre, App registrations: the application (client) id and a client secret |
| `DQ_MICROSOFT_TENANT` | optional. `organizations` (default: any work or school account), `common` (also personal accounts), `consumers`, or one tenant id (a GUID) to admit only that organisation |

Register this redirect address with each provider (Google: *Authorised redirect
URIs*; Microsoft: *Authentication*, platform *Web*):

    https://YOUR-DOMAIN/api/auth/oidc/google/callback
    https://YOUR-DOMAIN/api/auth/oidc/microsoft/callback

It is built from `DQ_PUBLIC_URL`, so that must be the exact address people use.
Google accounts must have a verified email. For Microsoft the email is taken from
the `email` claim or, failing that, `preferred_username`; with a multi-tenant
setting any Microsoft organisation can present any address there, so for a client
on Microsoft 365 prefer setting `DQ_MICROSOFT_TENANT` to that client's tenant id.
Restart after changing these (`docker compose up -d`).

## 13. The nightly file drop by SFTP (optional)

Section 14 of the specification asks for "a nightly file drop (SFTP or email-in) for
systems that cannot call an API". SFTP is built; email-in is not. A billing system
uploads the same CSV that `POST /api/pos/v1/files` takes, one row per bill line:

    type,store_code,bill_number,occurred_at,sku,qty,price,discount,payment_method,original_bill_number

Each folder acts as one API key: that key's company, and that key's store if the key
is limited to one. The folder's name grants nothing. The worker looks every 30
seconds and records each finished file through the same code as the API: the same
parsing, the same repeat protection (a bill already recorded is recognised by type,
store and bill number, whichever way it came), and the same activity-log entries for
each bill, plus one entry for the file. Then it moves the file aside:

| Folder (as the billing system sees it) | What is there |
|---|---|
| `/upload/` | where files go; a name ending `.part`, `.tmp` or `.filepart`, or starting with `.`, is left alone until renamed |
| `/processing/` | a file being recorded (seconds) |
| `/processed/` | files whose bills were all recorded (now or before), each with a `.report.txt` |
| `/failed/` | files with anything not recorded, each with a `.errors.txt` saying which bills and why |

The billing system can write only in `/upload/`; the other three are the worker's and
read-only to it. That is why each SFTP account has a user id of its own
(`DQ_SFTP_UID`, 10002) in the worker's group (10001), never the worker's id: a
client able to write where the worker writes could swap a folder for a link into
another company's folder. Files must be uploaded readable by others (mode 644, what
SFTP clients do by default); one readable by its owner only goes to `/failed/` with
that reason.

A failed file raises a "Nightly file not recorded" alert for the company's Owners and
Admins (in the app, and by email or WhatsApp for those who chose them). The bills
that could be recorded were recorded; correct the file and upload all of it again,
and only the missing bills are added. A file is read only once nobody has written to
it for 60 seconds (`DQ_SFTP_SETTLE_SECONDS`), so upload under a temporary name and
rename, or simply upload: either works.

**Turn it on.** The `sftp` service is an optional profile in `docker-compose.yml`:
`atmoz/sftp`, pinned by digest to one build. atmoz publishes it for linux/amd64 only,
so on an ARM server use another OpenSSH image with the same `sshd_config`.
Logins are by SSH key only (`deploy/sftp/sshd_config`);
each folder is its own account, locked into its own folder, that can only use SFTP.
Open the port (`DQ_SFTP_PORT`, 2222 by default) in the firewall, preferably only to
the client's addresses.

1. The company's Owner or an Admin makes an API key for the billing system under
   Settings, Integrations (limited to one store if the system serves one store), and
   tells you the beginning the screen shows (`dqk_ab12cd34…`).
2. The client sends you the billing system's OpenSSH public key (`ssh-ed25519 AAAA...`;
   `ssh-keygen -t ed25519` makes one). Never accept a password or a private key.
3. Add the folder, as root (root owns the drop's folders, and the chroot needs that):

   ```bash
   docker compose run --rm --user root worker manage sftp-add-folder \
       --folder acme-pos --api-key-id dqk_ab12cd34 \
       --public-key "ssh-ed25519 AAAA... pos@acme"
   docker compose --profile sftp up -d --force-recreate sftp
   ```

   A folder name is 3 to 32 lower-case letters, digits and hyphens. The command
   checks the key is live, writes the folder list (`/srv/sftp/.dqstack/folders.json`,
   outside every account's reach) and the accounts list for the SFTP server, and
   notes the change in the company's activity log. The SFTP server makes its accounts
   when it is created, hence `--force-recreate` after every change.
4. The billing system connects to `sftp://acme-pos@YOUR-DOMAIN:2222/` with its private
   key (it lands in `/upload`). Check the server's host key fingerprint with the
   client the first time: `docker compose exec sftp ssh-keygen -lf /etc/ssh/hostkeys/ssh_host_ed25519_key.pub`.

To stop a folder at once, revoke its API key under Settings, Integrations: its files
then go to `failed/` unrecorded, with an alert. To remove it,
`docker compose run --rm --user root worker manage sftp-remove-folder --folder acme-pos`
and recreate the service; its files are left in the volume. The host key is kept in
the `sftp-hostkeys` volume, so upgrades do not change it. Files are not deleted
automatically; `processed/` grows by a few kilobytes a night per store.

Upgrading the SFTP image: look up the new digest of the `alpine` tag
(`docker buildx imagetools inspect atmoz/sftp:alpine`), put it in
`docker-compose.yml`, and recreate the service.
