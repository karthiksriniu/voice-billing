-- Bolo Bill schema — the single source of truth.
--
-- Paste this whole file into the Supabase SQL editor for project ayvlfhrparwncnxsrvvw.
-- Every statement is idempotent, so re-running it is safe and is the correct way to apply
-- changes. There are deliberately no separate migration files: having two made it possible
-- to re-run the wrong one, which succeeds silently and leaves the schema half-applied.
--
-- Shape follows DECISIONS.md D8: things that change over time are append-only event logs
-- with client-generated ids, so a later device-to-server sync is append-mostly rather than
-- a merge problem. Retrofitting that after there is live shop data is not cheap.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- Shops and people
-- ---------------------------------------------------------------------------

create table if not exists shops (
  id             text primary key,          -- normalised 10-digit mobile number
  name           text not null,
  upi_vpa        text not null default '',
  lang           text not null default 'ta-en',
  created_at     timestamptz not null default now()
);

-- Passcodes are stored as PBKDF2 hashes, never the digits. A 6-digit code is a counter
-- convenience, not security — see api/_lib/auth.py for what that does and does not buy.
alter table shops add column if not exists passcode_hash text not null default '';

-- Owner vs staff. The owner sees the billing/admin switch; staff only ever bill.
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

-- ---------------------------------------------------------------------------
-- Catalog
-- ---------------------------------------------------------------------------

create table if not exists products (
  id          text primary key,              -- '<shop_id>:<slug>'
  shop_id     text not null,
  sku         text,
  name        text not null,
  name_ta     text default '',
  short_desc  text default '',
  long_desc   text default '',
  unit        text not null default 'piece', -- UOM
  unit_price  numeric(10,2) not null default 0,
  stock       numeric(12,3) not null default 0,
  aliases     text[] not null default '{}',
  updated_at  timestamptz not null default now()
);
create index if not exists products_shop on products (shop_id);
alter table products add column if not exists description text default '';

-- ---------------------------------------------------------------------------
-- Trade
-- ---------------------------------------------------------------------------

create table if not exists bills (
  id             uuid primary key default gen_random_uuid(),
  shop_id        text not null,
  total          numeric(10,2) not null,
  items          jsonb not null default '[]',
  payment_state  text not null default 'pending',   -- pending | confirmed
  upi_ref        text default '',
  created_at     timestamptz not null default now()
);
create index if not exists bills_shop_time on bills (shop_id, created_at desc);

-- Transcripts only. Never audio (D9). `was_corrected` marks the utterances worth the most
-- to Phase 1: the shopkeeper has already looked at them and told us the truth.
create table if not exists utterances (
  id             uuid primary key default gen_random_uuid(),
  shop_id        text not null,
  transcript     text not null,
  parsed         jsonb,
  was_corrected  boolean not null default false,
  created_at     timestamptz not null default now()
);
create index if not exists utterances_shop_time on utterances (shop_id, created_at desc);

-- Phase 4 groundwork; unused by the PoC. Append-only by design.
create table if not exists stock_movements (
  id           uuid primary key default gen_random_uuid(),
  shop_id      text not null,
  product_id   text not null,
  delta        numeric(12,3) not null,
  reason       text not null,               -- sale | inward | count | wastage
  bill_id      uuid,
  occurred_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Access
-- ---------------------------------------------------------------------------

-- RLS on with no public policy: anon traffic gets nothing, the service key bypasses it.
-- The service key is used only from the serverless function. Never ship it to a client.
alter table shops           enable row level security;
alter table staff           enable row level security;
alter table products        enable row level security;
alter table bills           enable row level security;
alter table utterances      enable row level security;
alter table stock_movements enable row level security;

insert into shops (id, name) values ('demo', 'Demo Shop')
  on conflict (id) do nothing;

-- PostgREST caches the schema. Without this, a change that applied cleanly still 404s from
-- the API until the cache expires, which is indistinguishable from it never having run.
notify pgrst, 'reload schema';
