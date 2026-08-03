-- Vaakku PoC schema. Paste into the Supabase SQL editor.
--
-- Shape follows DECISIONS.md D8: things that change over time are append-only event logs
-- with client-generated UUIDs, so a later device-to-server sync is append-mostly rather
-- than a merge problem. The PoC does not need that; the product does, and retrofitting a
-- schema after there is live shop data is not something you get to do cheaply.

create extension if not exists "pgcrypto";

create table if not exists shops (
  id          text primary key,
  name        text not null,
  upi_vpa     text not null default '',
  lang        text not null default 'ta-en',
  created_at  timestamptz not null default now()
);

create table if not exists products (
  id          text primary key,
  shop_id     text not null,
  sku         text,
  name        text not null,
  name_ta     text default '',
  short_desc  text default '',
  long_desc   text default '',
  unit        text not null default 'piece',
  unit_price  numeric(10,2) not null default 0,
  stock       numeric(12,3) not null default 0,
  aliases     text[] not null default '{}',
  updated_at  timestamptz not null default now()
);
create index if not exists products_shop on products (shop_id);

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

-- Transcripts only. Never audio (D9). `was_corrected` marks the utterances that are worth
-- the most to Phase 1: the shopkeeper has already looked at them and told us the truth.
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

-- The PoC talks to PostgREST with the service key from a server-side function only, so RLS
-- is enabled with no public policy: anon traffic gets nothing, the service key bypasses it.
-- Do not ship a client that holds this key.
alter table shops           enable row level security;
alter table products        enable row level security;
alter table bills           enable row level security;
alter table utterances      enable row level security;
alter table stock_movements enable row level security;

insert into shops (id, name) values ('demo', 'Demo Shop')
  on conflict (id) do nothing;
