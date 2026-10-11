-- ADR-0067 D5: 店で渡す閲覧リンク。リンクの乱数は保存せずハッシュだけ。1 人 1 本（発行し直すと前のものは取り消し）。
create table {schema}.viewer_links (
    link_id      text primary key,
    store_id     text not null references {schema}.stores (store_id),
    player_id    text not null,
    token_hash   text not null unique,
    created_at   timestamptz not null default now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);
create index viewer_links_by_player on {schema}.viewer_links (store_id, player_id);
alter table {schema}.viewer_links enable row level security;

-- 店舗ごとの最後の送り手の番号（読み取りの写しを作り直すかどうかの目印）
alter table {schema}.stores add column last_seq bigint;
