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
| Database and backups encrypted at rest | Depends on the host: use encrypted disks (the default on the major clouds) and encrypted backup storage | `deploy/README.md` |
| Backups restorable to any point in the last 7 days; at most 15 minutes lost | **Not met by the bundled setup**, which keeps nightly dumps for 30 days. Needs a managed PostgreSQL with point-in-time recovery, or WAL-G. | `deploy/README.md` section 5 |
| Restore drill every quarter | Steps written; the drill itself is for the operator | `deploy/README.md` |
| Overnight forecast finished by 06:00, or yesterday's stays with its age shown | Built and tested; failures alert the Admin | `forecast/jobs.py`, `worker.py` |
| Scale: 100 stores, 10,000 products per store, 200 tills at once | Load test script written and checked at small scale; **the 200-till run must be made on staging (PostgreSQL)**. The ledger locks per product row, so concurrent tills do not lose sales (tested with 6 threads on PostgreSQL). | `scripts/loadtest.py`, `test_step4_ledger.py` |
| Availability 99.5% monthly | An operations target. A single server is the first step; add a second app server behind a load balancer and a managed database for this. | `deploy/README.md` |
| Optional OTP and Google or Microsoft sign-in | **Not built** | |
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
* Dependencies are pinned exactly (`requirements-app.txt`, `constraints.txt`) and
  tested in CI; review them for security releases at least monthly.
