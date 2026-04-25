# DB 移行セットアップ手順

`groovy-sprouting-origami.md` v4 ベースの SQLite 移行に伴う手動セットアップ。

## 1. Windows Defender 除外 (admin 権限要)

`data/` ディレクトリと Python 実行ファイルをリアルタイム保護から除外:

PowerShell (管理者として実行):

```powershell
Add-MpPreference -ExclusionPath "C:\Users\9626s\projects\kimochi-max\data"
Add-MpPreference -ExclusionPath "C:\Users\9626s\projects\kimochi-max\.venv\Scripts\python.exe"
Get-MpPreference | Select-Object -ExpandProperty ExclusionPath
```

理由: SQLite WAL モードはファイルを頻繁に書き換え (kimochi.db, kimochi.db-wal, kimochi.db-shm)、
Defender のリアルタイムスキャンで遅延・破損のリスクがある。

## 2. SQLite バージョン確認

generated columns (JSON1) が必須のため SQLite 3.31 以上が必要:

```bash
python -c "import sqlite3; print(sqlite3.sqlite_version)"
# 期待: 3.31.0 以上 (Python 3.12 同梱は十分新しい)
```

不足時は Python 3.12+ をインストール (Python の SQLite が更新される)。

## 3. ulid-py インストール

```bash
.venv/Scripts/pip install ulid-py
```

## 4. 初期マイグレーション

```bash
PYTHONIOENCODING=utf-8 python -m db.migrate
# data/kimochi.db が作成され、schema_migrations テーブルが初期化される
```

## 5. バックアップ動作確認

```bash
python scripts/backup_db.py
# data/backups/kimochi_YYYYMMDD_HHMMSS.db.gz が生成される
```

## 6. cron / タスクスケジューラ設定 (将来)

本セッションでは省略。Phase 5 完了後、以下を設定推奨:

- `python scripts/backup_db.py` を 1 日 1 回実行
- 失敗時 Slack/Discord 通知 (`hallucination_monitor` 経由)

## 7. WAL ファイル取り扱いの注意

**Windows ↔ Linux 物理コピーは禁止**。
WAL モードの DB を別 OS にコピーすると破損リスクがある。
移行時は必ず `python scripts/backup_db.py` (= sqlite3 .backup API) 経由で gzip を生成し、
復元時は gunzip → そのまま使用。

## トラブルシュート

### sqlite3.OperationalError: database is locked
- `PRAGMA busy_timeout = 5000` で 5 秒待機。さらに長いトランザクションがあれば、
  `with begin_immediate(conn): ...` パターンで明示的にロックを取得する。

### WAL ファイル肥大化
- `PRAGMA wal_autocheckpoint = 1000` で 1000 ページごとに自動チェックポイント。
- 長時間実行のリーダーがあると停滞するので、conn 短命化が原則。

### integrity_check が ok 以外を返した
- `python scripts/backup_db.py` で復旧不可と判断、最新バックアップから gunzip で復元。
