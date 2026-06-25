-- Migration: add per-app owner_role for multi-tenant operator isolation.
-- Run once via `snow sql -f` BEFORE deploying the controller image that enforces
-- owner_role. Idempotent: the ADD COLUMN is guarded, the backfill only touches
-- NULLs. Replace YOUR_DB.PUBLIC with your database.schema before running.

ALTER TABLE YOUR_DB.PUBLIC.MENDIX_APPS
  ADD COLUMN IF NOT EXISTS owner_role VARCHAR;

UPDATE YOUR_DB.PUBLIC.MENDIX_APPS
  SET owner_role = 'MENDIX_ADMIN_OPERATOR_ROLE'
  WHERE owner_role IS NULL;
