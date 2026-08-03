-- Vaakku migration 002: accounts, roles, richer SKUs.
-- Paste into the Supabase SQL editor. Safe to re-run.

-- ---------------------------------------------------------------------------
-- Migration: accounts, roles and richer SKUs. Safe to re-run.
-- ---------------------------------------------------------------------------

-- Passcodes are stored as PBKDF2 hashes, never as the digits themselves. A 6-digit code
-- is a counter-convenience gate, not security — see api/_lib/auth.py.
alter table shops add column if not exists passcode_hash text not null default '';

-- Owner vs staff. The owner sees the admin/billing switch; staff only ever bill.
create table if not exists staff (
  id             text primary key,          -- '<shop_id>:<mobile>'
  shop_id        text not null,
  mobile         text not null,
  passcode_hash  text not null default '',
  role           text not null default 'user',   -- owner | user
  name           text default '',
  created_at     timestamptz not null default now()
);
create index if not exists staff_shop   on staff (shop_id);
create index if not exists staff_mobile on staff (mobile);

-- UOM is `unit`, already present. Description is what the shopkeeper dictates alongside.
alter table products add column if not exists description text default '';

alter table staff enable row level security;
