-- ADR-0067 D7: クラウドの保存（Supabase の Postgres を「ただの Postgres」として使う）。
-- {schema} は cloud/postgres.py が安全な名前に置き換える（既定 pokerapp = Supabase の自動 API に出ないスキーマ）。

create table {schema}.stores (
    store_id    text primary key,
    name        text not null,
    status      text not null default 'active',
    created_at  timestamptz not null default now()
);

create table {schema}.store_devices (
    device_id    text primary key,
    store_id     text not null references {schema}.stores (store_id),
    token_hash   text not null unique,            -- トークンそのものは保存しない
    label        text not null default '',
    created_at   timestamptz not null default now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);

-- 店舗 PC から届いたレコード（契約の JSON をそのまま body に）。主キーは店舗 ID + 種類 + キー（D3）。
create table {schema}.records (
    store_id    text not null references {schema}.stores (store_id),
    kind        text not null,
    key         text not null,
    body        jsonb not null,
    src_seq     bigint not null,                  -- 送り手（店舗 PC）の番号。ハンドなどは新しい方を採る
    player_id   text,
    session_id  text,
    updated_at  timestamptz not null default now(),
    primary key (store_id, kind, key)
);
create index records_by_session on {schema}.records (store_id, kind, session_id);
create index records_by_player on {schema}.records (store_id, kind, player_id);

-- 念のため RLS を有効にし、ポリシーは作らない（Supabase の匿名キー・自動 API からは何も読めない）。
-- テーブルの持ち主（クラウドのアプリが使う専用ユーザー）は RLS の対象外。
alter table {schema}.stores enable row level security;
alter table {schema}.store_devices enable row level security;
alter table {schema}.records enable row level security;
