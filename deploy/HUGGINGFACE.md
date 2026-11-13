# Running DQ-Stack free: Hugging Face Spaces + Neon

No card is needed for either. The app and its scheduler run in a Hugging Face Space
(Docker, 2 CPUs, 16 GB of memory, HTTPS included); the database is a free PostgreSQL
on Neon. Good for showing DQ-Stack to people and for trials. For real stores, use your
own server ([README.md](README.md)) or Render ([RENDER.md](RENDER.md)).

**What to know first**

- A free Space **goes to sleep after about 48 hours without visitors**. The next visit
  wakes it, which takes a minute or two. While it sleeps, the nightly forecast does not
  run; it catches up when the Space wakes.
- Neon's free plan has 0.5 GB of storage (the demo company uses about 25 MB) and a
  monthly allowance of database running time. If that runs out, the database pauses
  until the next month. Neon's dashboard shows how much is left.
- The app opens at its own address, `https://<owner>-<space>.hf.space`, not inside the
  Space's page on huggingface.co (it refuses to be framed by another site, on purpose).
- Every page is public. Anyone with the address can see the sign-in page, and, with
  demo logins on, enter the demo company.

## 1. The database: Neon (5 minutes)

1. Sign up at **https://neon.tech** with GitHub or Google.
2. Create a project: name `dqstack`, **Postgres 16**, region **AWS Asia Pacific
   (Singapore)**, the closest to India.
3. On the project's dashboard, click **Connect**. Turn **Connection pooling off**, and
   copy the connection string. It starts with `postgresql://` and ends with
   `?sslmode=require` (sometimes `&channel_binding=require` as well). Keep it private:
   it contains the database password.

## 2. Hugging Face: account and token (5 minutes)

1. Sign up at **https://huggingface.co/join**. Your username becomes part of the
   address, for example `your-name-dqstack.hf.space`.
2. **Settings > Access Tokens > Create new token**: type **Write**, name `dqstack-deploy`.
   Copy the token (it starts with `hf_`).

## 3. Publish the app (5 minutes, then about 15 minutes of building)

**Automatically from GitHub (recommended).** It republishes after every green CI run.

1. On GitHub, open the repository: **Settings > Secrets and variables > Actions**.
2. **Secrets** tab > **New repository secret**: name `HF_TOKEN`, value: your `hf_` token.
3. **Variables** tab > **New repository variable**: name `HF_SPACE`, value
   `your-name/dqstack` (your Hugging Face username, a slash, then `dqstack`).
4. **Actions** tab > **hugging face space** > **Run workflow**. It creates the Space and
   uploads the app.

**Or from your computer, once:**

```bash
pip install huggingface_hub
```

```bash
set HF_TOKEN=hf_your_token_here
python scripts/deploy_hf.py --space your-name/dqstack
```

## 4. The Space's settings (5 minutes)

Open **https://huggingface.co/spaces/your-name/dqstack > Settings > Variables and
secrets**. The first build fails to start until these exist; that is expected.

Add as **secrets** (hidden):

| Name | Value |
|---|---|
| `DATABASE_URL` | the Neon connection string from step 1 |
| `DQ_SECRET_KEY` | 64 random characters. Make them with `python -m dqstack.app.config`, or any password generator. Never change it afterwards: everyone would be signed out |
| `DQ_ADMIN_EMAIL` | your email, for the operator's portal at `/platform` |
| `DQ_ADMIN_PASSWORD` | a password of at least 10 characters for that account |
| `DQ_DEMO_EMAIL` | an email for the demo company's Owner (it can be made up, e.g. `owner@demo.example`) |
| `DQ_DEMO_PASSWORD` | a password of at least 10 characters for every demo account |

Add as a **variable** (visible):

| Name | Value |
|---|---|
| `DQ_DEMO_LOGINS` | `true`, so the sign-in page offers one-click demo logins |

Saving restarts the Space. On the first start it creates the tables, your operator
account and the demo company, then trains the demo's first forecast (a few minutes, in
the background; the site is up meanwhile).

## 5. Open it

`https://your-name-dqstack.hf.space` (the Space's README links it as well).

- `/app/`: the application. Under the password form, **or try the demo** lists the
  demo team; click anyone to go straight in.
- `/platform`: the operator's portal, with `DQ_ADMIN_EMAIL` and `DQ_ADMIN_PASSWORD`.

## If something goes wrong

Open the Space's **Logs** tab (Build and Container).

| What you see | What to do |
|---|---|
| `Add DATABASE_URL ...` or `Add DQ_SECRET_KEY ...` | Add that secret (step 4); the Space restarts by itself |
| `Refusing to start in production: DQ_SECRET_KEY must be ...` | The key needs at least 32 characters |
| `could not connect` or `password authentication failed` | Copy Neon's connection string again, with pooling off |
| `too many connections` | Add the variable `DQ_DB_POOL_SIZE` = `3` and `DQ_DB_MAX_OVERFLOW` = `3` |
| The page says it is building or sleeping | Wait a minute or two |
| The demo buttons do not show | Check the variable `DQ_DEMO_LOGINS` is `true`, and give the first start a few minutes |

## Updating

With the GitHub workflow set up, every `git push` that passes CI republishes the Space.
Otherwise run `python scripts/deploy_hf.py --space your-name/dqstack` again.
