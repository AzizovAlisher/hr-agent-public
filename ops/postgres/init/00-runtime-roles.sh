#!/bin/sh
set -eu

: "${HR_DB_API_PASSWORD:?HR_DB_API_PASSWORD is required}"
: "${HR_DB_WORKER_PASSWORD:?HR_DB_WORKER_PASSWORD is required}"

# These fixed login roles inherit only the permissions migrations grant to the
# reviewed NOLOGIN groups. Passwords are passed as quoted psql values and never
# interpolated into SQL text by the shell.
psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=api_password="$HR_DB_API_PASSWORD" \
  --set=worker_password="$HR_DB_WORKER_PASSWORD" <<'SQL'
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_runtime') THEN
    CREATE ROLE hr_agent_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_worker') THEN
    CREATE ROLE hr_agent_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_broker') THEN
    CREATE ROLE hr_agent_broker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION BYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_api_login') THEN
    CREATE ROLE hr_agent_api_login LOGIN INHERIT NOSUPERUSER NOCREATEDB
      NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_worker_login') THEN
    CREATE ROLE hr_agent_worker_login LOGIN INHERIT NOSUPERUSER NOCREATEDB
      NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END
$roles$;

SELECT format('ALTER ROLE hr_agent_api_login PASSWORD %L', :'api_password') \gexec
SELECT format('ALTER ROLE hr_agent_worker_login PASSWORD %L', :'worker_password') \gexec

GRANT hr_agent_runtime TO hr_agent_api_login;
GRANT hr_agent_worker TO hr_agent_worker_login;
ALTER ROLE hr_agent_api_login SET statement_timeout = '120s';
ALTER ROLE hr_agent_api_login SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE hr_agent_worker_login SET statement_timeout = '15min';
ALTER ROLE hr_agent_worker_login SET idle_in_transaction_session_timeout = '60s';
SQL
