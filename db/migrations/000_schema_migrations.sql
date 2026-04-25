-- 000_schema_migrations.sql
-- 全 migration を管理するメタテーブル。最初に必ず適用される。

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    TEXT PRIMARY KEY,        -- ファイル名先頭の番号 + 名前 (例: "000_schema_migrations")
  applied_at INTEGER NOT NULL,        -- UTC ms
  checksum   TEXT NOT NULL            -- migration ファイル本文の SHA256 (改竄検知用)
);
