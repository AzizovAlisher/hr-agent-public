-- Cluster-level prerequisites for an HR Agent disaster-recovery target.
-- Run as a PostgreSQL role with CREATEROLE before restoring a full archive.
-- These group roles cannot log in and carry no credentials.

DO $roles$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_runtime'
  ) THEN
    CREATE ROLE hr_agent_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  ELSE
    ALTER ROLE hr_agent_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_worker'
  ) THEN
    CREATE ROLE hr_agent_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  ELSE
    ALTER ROLE hr_agent_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_broker'
  ) THEN
    CREATE ROLE hr_agent_broker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION BYPASSRLS;
  ELSE
    ALTER ROLE hr_agent_broker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION BYPASSRLS;
  END IF;
END
$roles$;
