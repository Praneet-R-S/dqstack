# Security and reliability: what is built, and what is not yet

Status against the targets in section 15 of the product specification. "Tested" means
an automated test in `tests/app/` proves it, on SQLite and on PostgreSQL 16 in CI.

| Target | Status | Where |
|---|---|---|
| Company separation: every query filtered by company on the server; reading another company's records by id must fail | Built and tested. Another company's record answers "not found", the same as an id that does not exist. Tests try stores, people, products, imports, stock, orders, sales, recommendations, reports, attachments and API keys across companies. | `scope.py`, `test_step1_foundations.py` and the separation test in every step |
| Hashed passwords | Built and tested (Argon2id) | `security.py` |
| Lockout after 5 failures for 15 minutes; admin can unlock | Built and tested | `security.py`, `routes/org.py` |
| Sessions that expire; signed out everywhere on disable | Built and tested. Only a SHA-256 of the session token is stored. | `security.py` |
| CSRF protection | Built and tested: every change needs the session's token in a header | `deps.py` |
| Roles checked on the server for every action | Built and tested (the section 2 matrix) | `permissions.py`, `scope.py` |
| Activity log: who, device, time, before and after; append-only | Built and tested, in the ORM and by database triggers. The application's database role does not own the tables, so it cannot disable the triggers. | `audit.py`, `guards.py`, `deploy/postgres-init.sh` |
| Stock ledger cannot be rewritten | Built and tested the same way | `ledger.py`, `guards.py` |
| Platform admin cannot read client data without a temporary, logged grant | Built and tested; support sessions are read-only | `routes/platform.py`, `scope.py` |
| HTTPS everywhere | Caddy with automatic certificates; production refuses insecure cookies or a non-https address; HSTS sent | `deploy/Caddyfile`, `config.py`, `main.py` |
| Security headers | nosniff, frame denial, referrer policy, permissions policy, HSTS, no caching of API answers | `main.py` |
| Rate limiting on sign-in, reset, invitation and the POS API | Built, per process. With several web processes the limits multiply; put a shared limit in the proxy (Caddy or a CDN) for public deployments. | `ratelimit.py` |
| Errors never show a stack trace | Built and tested | `main.py` |
| Database and backups encrypted at rest | Database: depends on the host (use encrypted disks, the default on the major clouds). Backups: WAL-G encrypts every file on the server with libsodium (`WALG_LIBSODIUM_KEY`) before it is sent, and/or object storage encrypts at rest (`WALG_S3_SSE`); the `pitr` health check reports object-storage backups with neither. The key must also be kept off the server. The nightly `pg_dump` copy is not encrypted by the tool: keep its volume on an encrypted disk. | `deploy/README.md` section 5, `deploy/postgres/dq-pitr.sh` |
| Backups restorable to any point in the last 7 days; at most 15 minutes lost | **Built; proven by the CI job `pitr-drill` once it runs** (it needs Docker, which the development machine did not have). WAL-G 3.0.9 in the bundled database: every WAL segment archived, forced at least every 5 minutes (`archive_timeout=300`), so losing the whole server loses about 5 minutes; a nightly base backup; 8 days kept. `deploy/restore-to-time.sh` restores to a chosen time into a fresh volume, pauses there for checking, and switches only on confirmation, keeping the old volume. CI job `pitr-drill` (S3-compatible storage and the local folder, encryption on) writes rows, notes T, writes more, kills the database, restores to T and fails unless exactly the rows before T return, the last write reached storage within `archive_timeout`, and archiving resumes. The nightly `pg_dump` (30 days) stays as a second, independent copy. **The operator must** provide object storage away from the server (the default local folder is not a backup), keep the encryption key off the server, and watch `dq-pitr check`. Restore time on real data is only known from the operator's drill. | `docker-compose.yml` (`db`, `pitr`, `backup`), `deploy/postgres/`, `deploy/restore-to-time.sh`, `deploy/README.md` section 5, `.github/workflows/ci.yml` |
| Restore drill every quarter | Steps written, and non-disruptive: the drill restores to a time an hour ago beside the live system and is thrown away. The same drill runs in CI on every push; the quarterly one on the real storage is for the operator, who records the time against the 4-hour target. | `deploy/README.md` section 5 |
| Overnight forecast finished by 06:00, or yesterday's stays with its age shown | Built and tested; failures alert the Admin | `forecast/jobs.py`, `worker.py` |
| Scale: 100 stores, 10,000 products per store, 200 tills at once | **Built; checked by the CI job `scale` once it runs** (on pushes to the main branches and on demand): PostgreSQL 16 with 100 stores, 10,000 products and 1,000,000 stock rows seeded in bulk; the application with 4 worker processes; 200 tills at once through the POS API, two per store with the store's own key, three items a bill with shared best-sellers. Fails unless no request errs, the 95th percentile is under 2 s at a busy pace (about a bill a second per till), every bill is stored exactly once with one sale movement per line, and every stock row equals its ledger; a flat-out run is checked the same way with latency reported only. The figures are those of a GitHub runner, not of the client's server: repeat on staging before go-live. A key's last use is recorded in its own short transaction, so tills sharing one key do not wait for each other; a bill locks its stock rows in one fixed order, so two tills cannot deadlock (`test_step4_ledger.py`). | `scripts/scale_seed.py`, `scripts/loadtest.py`, `.github/workflows/ci.yml`, `test_step4_ledger.py` |
| Availability 99.5% monthly | An operations target. A single server is the first step; add a second app server behind a load balancer and a managed database for this. | `deploy/README.md` |
| Optional OTP and Google or Microsoft sign-in | Built and tested. A company can ask for a 6-digit code after the password (for Owners and Admins, or everyone), sent by text or email; codes last 10 minutes, die after 5 wrong tries, are stored only as a keyed hash, and a dead code counts toward the lockout. Password reset by a code to the phone, with the same answer whether or not the number has an account. Google and Microsoft sign-in (OpenID Connect with PKCE; ID token signature, issuer, audience, expiry and nonce checked) for existing, active people only; a Microsoft sign-in counts only from the Microsoft organisation the company named. Not yet tried against the real Google, Microsoft or SMS services. | `signin.py`, `oidc.py`, `routes/auth.py`, `test_signin_extras.py` |
| A cashier's account is for the till only | Built and tested: beyond the role matrix, the server answers a cashier only on the till's endpoints (checkout, catalogue and prices, alerts) | `deps.py`, `test_screen_apis.py` |
| DPDP Act readiness: notice to staff, retention and deletion, breach response | Privacy notice drafted (needs legal review); export and deletion after a confirmed period built and tested. A breach response plan is a document for the operator to write. | `site/privacy.html`, `routes/platform.py` |

## Other notes

* Uploaded files are parsed in memory with size limits (25 MB, 1,000,000 rows); old
  `.xls` files are refused. Photos are limited to 2 MB and image types.
* API keys are shown once and stored as a hash; they can be limited to one store and
  revoked.
* Saved forecasting models are Python pickles. They are only ever loaded from the
  company's own database, written by the scheduler; never from an upload.
* Secrets come only from the environment. `.env` is ignored by git; `.env.example`
  holds placeholders.
* Each POS API key is limited to 600 requests a minute per web process; a large chain
  is better served by one key per store (or per till server), as the scale test does.
* Dependencies are pinned exactly (`requirements-app.txt`, `constraints.txt`) and
  tested in CI; review them for security releases at least monthly.
