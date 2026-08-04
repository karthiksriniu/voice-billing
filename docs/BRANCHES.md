# Branches and deployments

    main      -> https://bolo-bill.vercel.app          the shops use this
    staging   -> https://bolo-bill-staging.vercel.app  everything else

Both are the same Vercel project, so there is one set of settings and one place to look
when something is wrong.

## Working

    git checkout staging
    ...
    ./bolo staging          # deploy the branch, re-point the staging alias

When it has been used and is good:

    git checkout main && git merge staging && git push
    ./bolo deploy           # refuses from any other branch, or with uncommitted work

`./bolo deploy` will not run from a branch other than main and will not run with a dirty
tree. Production is what is in git on main, and nothing else — there is no undo that the
shopkeeper does not see.

## The database is shared, and that matters

Both deployments talk to the same Supabase project. Shop data is partitioned by `shop_id`
everywhere, so the safe way to use staging is to **sign up a separate test business with a
different mobile number**. Nothing then touches a real shop's catalog, bills or receipt
series.

What is NOT partitioned is the schema. A destructive migration run against that project
hits production immediately, staging or no staging. When that becomes a real risk — the
first time a column needs dropping rather than adding — staging wants its own Supabase
project and its own `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` in the Preview environment.

## Environment variables

Vercel keeps these per environment. Anything set only for Production leaves staging with
no speech key and no database, which shows up as `asr_backend: echo` and
`db: seed-csv (in memory)` on `/api/health` — working, but not working on anything real.

    ./bolo env              # prompts, and asks which environments to apply to
