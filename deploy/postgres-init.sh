#!/bin/sh
# Runs once, when the database volume is first created. Two roles:
#   dqstack_owner  owns the schema; migrations run as it
#   dqstack_app    what the application connects as: reads and writes rows, nothing more
# The application role does not own the tables, so it cannot disable the triggers
# that keep the stock ledger and the activity log append-only.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE dqstack_owner LOGIN PASSWORD '${DQ_OWNER_PASSWORD}';
CREATE ROLE dqstack_app LOGIN PASSWORD '${DQ_APP_PASSWORD}';
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO dqstack_owner, dqstack_app;
ALTER SCHEMA public OWNER TO dqstack_owner;
GRANT USAGE ON SCHEMA public TO dqstack_app;
ALTER DEFAULT PRIVILEGES FOR ROLE dqstack_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dqstack_app;
ALTER DEFAULT PRIVILEGES FOR ROLE dqstack_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO dqstack_app;
SQL
