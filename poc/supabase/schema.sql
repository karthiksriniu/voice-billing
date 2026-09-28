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
  lang           text not null default 'en',   -- language CODE (en/ta/hi/ml/te/kn)
  created_at     timestamptz not null default now()
);

-- Passcodes are stored as PBKDF2 hashes, never the digits. A 6-digit code is a counter
-- convenience, not security — see api/_lib/auth.py for what that does and does not buy.
alter table shops add column if not exists passcode_hash text not null default '';

-- The number receipts are sent FROM, and the registration they are issued under.
-- `wa_number` is the shop's WhatsApp Business line — not the sign-in mobile, which is only
-- an identifier and is often a personal number.
-- `gstin` is optional and stays optional: most tier-1 paper-billing shops are under the
-- registration threshold, and a receipt that invents a GST number is a worse document than
-- one that has none.
-- The shopkeeper's own name, not the business's. Spoken back on every wake, because the
-- phone sits behind him and a beep does not tell him it heard the right person.
alter table shops add column if not exists owner_name text not null default '';

alter table shops add column if not exists wa_number text not null default '';
alter table shops add column if not exists gstin     text not null default '';

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

-- Receipt delivery. The number is captured at the counter — by the customer on the
-- shopkeeper's phone, which is what makes it acceptable at all (Principle 3: the
-- shopkeeper never types). `receipt_status` is honest about what actually happened:
--   none      — not asked for
--   requested — number captured, nothing sent (no messaging provider is wired up)
--   sent      — actually delivered
alter table bills add column if not exists customer_mobile text default '';
alter table bills add column if not exists receipt_status  text not null default 'none';

-- How the bill was settled, and when. `payment_method` is the honest record of what we
-- actually know: 'cash' is the shopkeeper telling us so, which is first-hand. 'upi' is
-- only ever set by a server-side confirmation from the payment provider — never by
-- watching the phone, which is both outside a web app's reach and a Play policy
-- violation for our use case (see CLAUDE.md and DECISIONS.md D5).
alter table bills add column if not exists payment_method text not null default '';
alter table bills add column if not exists paid_at        timestamptz;

-- The issued document. `receipt_no` is allotted once, at finalise, and never reused: a
-- receipt series with a gap in it is an audit question, and one with a duplicate is worse.
-- `receipt` holds the document as issued rather than as recomputed — prices and even the
-- shop's name change, and a receipt reprinted next year must say what it said on the day.
alter table bills add column if not exists receipt_no text not null default '';
alter table bills add column if not exists receipt    jsonb;

-- Serial numbers, one series per shop per financial year.
create table if not exists receipt_counters (
  shop_id  text not null,
  fy       text not null,          -- "2026-27", April to March
  next_no  integer not null default 0,
  primary key (shop_id, fy)
);

-- Allotted by the database, not by the application. Two finalises landing in the same
-- millisecond on two serverless instances would otherwise read the same number and both
-- use it; an atomic upsert-and-return is the only way this stays gapless under concurrency.
-- search_path is pinned rather than inherited. A function that resolves its tables through
-- whatever search_path the caller happens to have set can be pointed at a different table
-- of the same name, and this one hands out invoice numbers.
create or replace function next_receipt_no(p_shop text, p_fy text)
returns integer
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare n integer;
begin
  insert into receipt_counters (shop_id, fy, next_no) values (p_shop, p_fy, 1)
  on conflict (shop_id, fy) do update set next_no = receipt_counters.next_no + 1
  returning next_no into n;
  return n;
end;
$$;

-- Applying a movement has to be an increment, not a write. Two sales of the same item in
-- the same second would otherwise each read the old figure and each store their own
-- answer, and one of them would be lost — which is exactly the sort of quiet arithmetic
-- error that makes a shrinkage number worthless.
create or replace function bump_stock(p_id text, p_delta numeric)
returns void
language sql
security invoker
set search_path = public, pg_temp
as $$
  update products set stock = coalesce(stock, 0) + p_delta, updated_at = now()
  where id = p_id;
$$;

create index if not exists stock_moves_shop on stock_movements (shop_id, occurred_at desc);
create index if not exists stock_moves_prod on stock_movements (shop_id, product_id);

-- Repeat-order history: "phone number 98400 12345" at the start of a bill pulls this
-- customer's last few purchases. Scoped by shop, newest first.
create index if not exists bills_customer
  on bills (shop_id, customer_mobile, created_at desc);

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
alter table shops             enable row level security;
alter table staff             enable row level security;
alter table products          enable row level security;
alter table bills             enable row level security;
alter table utterances        enable row level security;
alter table stock_movements   enable row level security;
-- Added late and initially forgotten, which is exactly the kind of omission the Supabase
-- linter exists to catch: a table without RLS is readable and writable by anyone holding
-- the project's anon key, and a receipt counter that a stranger can advance puts gaps in
-- a shop's invoice series.
alter table receipt_counters  enable row level security;

insert into shops (id, name) values ('demo', 'Demo Shop')
  on conflict (id) do nothing;

-- PostgREST caches the schema. Without this, a change that applied cleanly still 404s from
-- the API until the cache expires, which is indistinguishable from it never having run.
notify pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- What a thing is, and what it is made of
-- ---------------------------------------------------------------------------

-- A coffee shop's shelf holds four different kinds of thing, and lumping them together is
-- why its stock screen never made sense:
--   raw        beans, milk, ice — bought by weight, consumed to make something else
--   consumable cups, straws, tissue — bought by the box, leave with the customer
--   menu       Espresso, Americano — sold, but never sat on a shelf as itself
--   resale     a French press, a bag of beans — bought and sold unchanged
-- Only the first two can meaningfully shrink. A menu item has no stock of its own, which
-- is exactly why its sales have to be exploded into components before the ledger sees them.
alter table products add column if not exists category text not null default 'resale';

-- The recipe, one row per component: [{"component_id": "...", "qty": 0.02}]
-- Quantities are in the *component's own* stock unit, never the recipe's. Twenty grams of
-- beans is stored as 0.02 against a product priced per kg, so the consumption path stays a
-- multiplication and cannot get a conversion wrong at the one moment nobody is watching.
alter table products add column if not exists recipe jsonb not null default '[]'::jsonb;

create index if not exists products_shop_cat on products (shop_id, category);

-- ---------------------------------------------------------------------------
-- Sales aggregation
-- ---------------------------------------------------------------------------

-- Summing in the API meant pulling every bill of the month over HTTP into a serverless
-- function to add up a single number. Postgres already has the rows and the index; this
-- returns the whole KPI block and the daily series in one round trip.
--
-- Day boundaries are IST, computed here rather than in Python. A bill rung up at 00:30
-- IST is 19:00 UTC the previous day, so a naive UTC date would file a shop's late evening
-- under yesterday and quietly understate every single-day figure.
create or replace function sales_report(p_shop text, p_from timestamptz, p_to timestamptz)
returns jsonb language sql stable security invoker
set search_path = public, pg_temp as $$
  with b as (
    select total, payment_state, payment_method, created_at
      from bills
     where shop_id = p_shop and created_at >= p_from and created_at < p_to
  )
  select jsonb_build_object(
    'count', (select count(*) from b),
    'total', (select coalesce(sum(total), 0) from b),
    'paid',  (select coalesce(sum(total) filter (where payment_state = 'confirmed'), 0) from b),
    'cash',  (select coalesce(sum(total) filter (where payment_method = 'cash'), 0) from b),
    'upi',   (select coalesce(sum(total) filter (where payment_method = 'upi'), 0) from b),
    'days',  (select coalesce(jsonb_agg(x order by x->>'day'), '[]'::jsonb) from (
                select jsonb_build_object(
                         'day',   to_char((created_at at time zone 'Asia/Kolkata')::date, 'YYYY-MM-DD'),
                         'total', sum(total),
                         'count', count(*),
                         -- Carried per day so a caller can slice a week or a month out of
                         -- one query. A split that only existed on the envelope could not
                         -- be narrowed to a window and would have to be quietly mislabelled.
                         'paid',  coalesce(sum(total) filter (where payment_state = 'confirmed'), 0),
                         'cash',  coalesce(sum(total) filter (where payment_method = 'cash'), 0),
                         'upi',   coalesce(sum(total) filter (where payment_method = 'upi'), 0)) as x
                  from b
                 group by (created_at at time zone 'Asia/Kolkata')::date) s)
  );
$$;

notify pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- Orders taken before the customer is at the counter
-- ---------------------------------------------------------------------------

-- A bill is what the shopkeeper hands over. An order is what somebody asked for, and the
-- two are not the same thing: an order can be refused, can sit for an hour, and may name
-- something the shop has run out of. Kept separate so a refused order never has to be
-- explained as a cancelled bill, and so an order that has not been accepted cannot take a
-- receipt number out of the shop's series.
create table if not exists orders (
  id               uuid primary key default gen_random_uuid(),
  shop_id          text not null,
  source           text not null default 'api',   -- api | counter
  customer_mobile  text default '',
  customer_name    text default '',
  items            jsonb not null default '[]',
  note             text default '',
  total            numeric(10,2) not null default 0,
  status           text not null default 'pending',  -- pending | accepted | rejected
  bill_id          uuid,                             -- set when accepted
  reject_reason    text default '',
  created_at       timestamptz not null default now(),
  settled_at       timestamptz
);
-- The pending list is the query this table exists for: one shop, oldest first, because a
-- queue is served in the order it arrived.
create index if not exists orders_pending on orders (shop_id, status, created_at);

-- The key an automated caller uses to place orders. Stored as a SHA-256 digest and looked
-- up by digest, so the raw key exists only in the caller's configuration and is shown to
-- the owner exactly once, at the moment it is generated.
--
-- SHA-256 rather than the PBKDF2 used for passcodes, deliberately: a passcode is six digits
-- and needs the slow hash to survive a brute force, whereas this is 32 random bytes, where
-- an attacker gains nothing from speed and the endpoint needs a fast lookup on every call.
alter table shops add column if not exists order_key_hash text default '';
create index if not exists shops_order_key on shops (order_key_hash);

alter table orders enable row level security;

notify pgrst, 'reload schema';
