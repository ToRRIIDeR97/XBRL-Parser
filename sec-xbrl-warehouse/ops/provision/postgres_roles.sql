-- Least-privilege PostgreSQL logins (run as the database owner; idempotent).
-- psql variables: owner, db, worker_password, api_password.
--
--   xbrl_worker  DML on the control-plane tables; no DDL, no superuser.
--   xbrl_api     CONNECT only (the API's health check); reads nothing.

SELECT format('CREATE ROLE xbrl_worker LOGIN PASSWORD %L', :'worker_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'xbrl_worker') \gexec
SELECT format('CREATE ROLE xbrl_api LOGIN PASSWORD %L', :'api_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'xbrl_api') \gexec

ALTER ROLE xbrl_worker WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'worker_password';
ALTER ROLE xbrl_api WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'api_password';

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO xbrl_worker, xbrl_api;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO xbrl_worker;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO xbrl_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO xbrl_worker;
REVOKE INSERT, UPDATE, DELETE ON TABLE alembic_version FROM xbrl_worker;
-- Tables and sequences that later migrations create.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO xbrl_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO xbrl_worker;
