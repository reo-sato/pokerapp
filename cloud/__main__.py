"""`python -m cloud` — クラウドのアプリを起動する（Cloud Run の入口, ADR-0067 D8）。

環境変数（秘密の値は Secret Manager から渡す。リポジトリには置かない）:
- `DATABASE_URL`       Postgres の接続文字列（Supabase の transaction モード 6543）。必須
- `CLOUD_DB_SCHEMA`    テーブルを置くスキーマ（既定 pokerapp）
- `CLOUD_ADMIN_TOKEN`  管理トークン（32 文字以上。無ければ管理の受け口は無効）
- `CLOUD_MAX_BODY_BYTES` push の本文の上限（既定 4 MB）
- `CLOUD_TOKEN_SECRET` お客さんのログインの署名の鍵（固定する。無いと再起動のたびに全員ログアウト）
- `CLOUD_PUBLIC_URL`   閲覧リンクの URL の頭（例 https://pokerapp-xxxx.a.run.app。無ければ要求の URL から）
- `PORT`               待ち受けるポート（Cloud Run が渡す。既定 8080）
- `CLOUD_DEV_MEMORY=1` 手元の確認用: Postgres の代わりにメモリに保存（再起動で消える）

起動のたびに migration を当ててから待ち受ける。
"""
from __future__ import annotations

import logging
import os
import sys

from cloud.app import DEFAULT_MAX_BODY, PLAYER_WEB_DIR, CloudSettings, create_cloud_app


def build_app():
    if os.environ.get("CLOUD_DEV_MEMORY") == "1":
        from cloud.storage import MemoryStorage

        storage = MemoryStorage()
        logging.getLogger("cloud").warning("メモリに保存しています（手元の確認用。再起動で消えます）")
    else:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise SystemExit("DATABASE_URL がありません（手元で試すなら CLOUD_DEV_MEMORY=1）")
        from cloud.postgres import PostgresStorage

        storage = PostgresStorage(dsn, schema=os.environ.get("CLOUD_DB_SCHEMA", "pokerapp"))
        applied = storage.migrate()
        if applied:
            logging.getLogger("cloud").info("migration を当てました: %s", applied)
    settings = CloudSettings(
        admin_token=os.environ.get("CLOUD_ADMIN_TOKEN") or None,
        max_body_bytes=int(os.environ.get("CLOUD_MAX_BODY_BYTES", DEFAULT_MAX_BODY)),
        token_secret=os.environ.get("CLOUD_TOKEN_SECRET") or None,
        public_url=os.environ.get("CLOUD_PUBLIC_URL") or None,
        player_web_dir=PLAYER_WEB_DIR,
    )
    return create_cloud_app(storage, settings)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    app = build_app()
    # proxy_headers: Cloud Run の前段が付ける X-Forwarded-Proto などを信じる（https の判定）
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips="*", access_log=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
