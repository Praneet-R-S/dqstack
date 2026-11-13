# Deploying DQ-Stack on Render

[render.yaml](../render.yaml) describes everything Render needs: a PostgreSQL 16
database, the web service, and the worker that runs the nightly forecasts. You create
all three in one step, then add email and a domain.

For your own server with Docker Compose instead (India regions, SFTP, WAL-G backups),
see [README.md](README.md) in this folder.

## What runs where

| Render resource | Name | What it does | Plan in render.yaml |
|---|---|---|---|
| PostgreSQL 16 | `dqstack-db` | All data, including uploads and trained models | Basic 1 GB |
| Web Service | `dqstack` | The application, API and website | Standard (2 GB) |
| Background Worker | `dqstack-worker` | Nightly forecasts, weekly retraining, alerts, summaries, Zoho syncs | Standard (2 GB) |

All three are in Singapore, the closest Render region to India.

## 1. Create everything (about 15 minutes, mostly the first build)

1. Sign in at [dashboard.render.com](https://dashboard.render.com) and connect your GitHub account.
2. **New → Blueprint**, pick the `dqstack` repository, and keep the branch `main`.
3. Render reads `render.yaml` and lists the database, the web service, the worker and the
   `dqstack-settings` group. It asks for one value:
   - **`DQ_PUBLIC_URL`** (for the worker): enter `https://dqstack.onrender.com`. If Render
     gives the web service a different address, you will correct it in step 3.
4. Check the monthly total Render shows, then **Apply**.

The first build takes 10 to 15 minutes, because it installs the forecasting libraries.
Before the web service starts, its pre-deploy command, `dq-entrypoint migrate`, creates the
database tables. The worker may restart once or twice while the tables are being made.
That is expected.

## 2. Check it is up

1. Open the **dqstack** web service. Its address is at the top, for example
   `https://dqstack.onrender.com`.
2. Open `https://<that address>/api/health`. It should answer with the database reachable.
3. In the web service, open **Shell** and run:

   ```bash
   python -m dqstack.app.manage check
   ```

   Every line should read well. Without email it warns that invitations and password
   resets will not be emailed; step 4 fixes that.

## 3. Make the addresses match

If the web service's address is not `https://dqstack.onrender.com`:

1. Go to **dqstack-worker → Environment**.
2. Set `DQ_PUBLIC_URL` to the web service's real address.
3. Save. The worker restarts.

The web service uses its own Render address automatically.

## 4. Email

Invitations, password resets, alerts, purchase orders and the owner's summaries go by
email. Use any SMTP service, for example Amazon SES, Brevo, Postmark, Zoho Mail or Google
Workspace.

Go to **Environment Groups → dqstack-settings** and add:

| Key | Example |
|---|---|
| `DQ_SMTP_HOST` | `smtp-relay.brevo.com` |
| `DQ_SMTP_PORT` | `587` |
| `DQ_SMTP_USER` | your SMTP login |
| `DQ_SMTP_PASSWORD` | your SMTP password |
| `DQ_MAIL_FROM` | `DQ-Stack <no-reply@yourdomain.in>` |

Both services restart with the new values.

## 5. Your first accounts

In the **dqstack** web service's **Shell**:

```bash
python -m dqstack.app.manage create-platform-admin --email you@yourcompany.in --name "Operations"
```

It asks for a password. This account signs in at `/platform`, where you create client
companies and invite their owners. Clients can also sign up themselves at `/app/#/signup`.

**Optional demo company** for showing people. This takes a few minutes, and the figures
are synthetic:

```bash
python -m dqstack.app.manage seed-demo --email demo@yourcompany.in
```

To show it to people without giving out passwords, add `DQ_DEMO_LOGINS=true` in
**dqstack-settings**. The sign-in page then lists the demo team, and one click signs in as
any of them. Anyone with the address can do this, so the demo company's people cannot
invite anyone, change roles, company settings or connections. Real client companies are
never offered. Remove the setting when you no longer want visitors in.

## 6. Your own domain

1. **dqstack → Settings → Custom Domains → Add**, for example `app.yourdomain.in`.
2. Add the DNS record Render shows at your domain provider. Render issues the HTTPS
   certificate itself.
3. Add `DQ_PUBLIC_URL=https://app.yourdomain.in` in **dqstack-settings**.
4. Remove the separate `DQ_PUBLIC_URL` from **dqstack-worker → Environment**, so both
   services use the group's value.

## 7. Updating

Push to `main`. Render deploys the web service and the worker after GitHub's checks pass
(`autoDeployTrigger: checksPass`). Migrations run before the new web service takes over.
To deploy without waiting for the checks, use **Manual Deploy** on each service.

## 8. Backups

Render's paid PostgreSQL plans keep daily backups and can restore the database to an
earlier point in time. Find these under **dqstack-db → Recovery**. Try a restore once
before your first client goes live. It creates a new database, so nothing in use is
touched.

## Optional settings

Add these in **dqstack-settings**. [.env.example](../.env.example) explains each one.

- **WhatsApp:** `DQ_WHATSAPP_*`. See section 11 of [README.md](README.md).
- **Text messages:** `DQ_SMS_*`. See section 11.
- **Google and Microsoft sign-in:** `DQ_GOOGLE_*` and `DQ_MICROSOFT_*`. See section 12.
- **Zoho Inventory:** each company connects its own account from the app. See section 14.

## Differences from your own server

- **SFTP file drop:** not available, because Render does not open the port it needs.
  Stores send sales through Checkout, the POS API, uploads or Zoho instead.
- **One database user.** The application connects as the table owner, so the append-only
  protection on the stock ledger and activity log is weaker than with the two roles in
  [postgres-init.sh](postgres-init.sh). To restore it, create the `dqstack_app` role and
  grants from that script with `psql`, using the database's External URL. Then point the
  services' `DATABASE_URL` at that role, and keep the owner's address for migrations.
- **Region:** Singapore, not India.
- **Memory:** a large company's first training needs more. If the worker restarts with
  "out of memory", change its plan to Pro (4 GB).

## If something goes wrong

| What you see | What to do |
|---|---|
| Web service: "Refusing to start in production" | The message names the setting. Usually `DQ_PUBLIC_URL` is missing on the worker or does not start with `https://` |
| Worker restarting right after the first deploy | Wait for the web service's migrations to finish; it settles by itself |
| Worker: "out of memory" during training | Change the worker's plan to Pro (4 GB) |
| No emails arrive | Check the `DQ_SMTP_*` values; until they are set, messages go to the service's Logs instead |
| Deploys never start after a push | GitHub's checks failed. Fix them, or use Manual Deploy |
