# Phase 5: state migration + dual-write 適用手順

`demo_runner.py` を **JSON primary + DB secondary** の dual-write 構成に切り替える手順。
SIM 稼働中の安全性を最優先。本ガイドは PR #10 (v2.5) と PR #11 (db-foundation)
の両方がマージされた後に適用する。

## 前提

- PR #10 マージ済 (demo_runner.py が v2.5、配分 35/35/30 + multi_lookback)
- PR #11 マージ済 (db/, scripts/, analysis/, state_repo.py が main にある)
- `python -m db.migrate` で SQLite スキーマ適用済
- `python scripts/migrate_state_to_db.py --skip-snapshots` で過去 state 取り込み済

## Phase 5 適用パッチ (demo_runner.py)

### 修正箇所 1: import 追加 (ファイル冒頭近く)

```python
# 既存 import の後に追加
try:
    from db.repositories.state_repo import sync_state_to_db
    DB_DUAL_WRITE_AVAILABLE = True
except ImportError:
    DB_DUAL_WRITE_AVAILABLE = False
```

### 修正箇所 2: save_state() に dual-write を追加

既存の `save_state()` の最後に追加 (JSON 書き込みは保持):

```python
def save_state(state):
    # 履歴を制限してファイルサイズを抑える
    state["trades"] = state["trades"][-MAX_TRADE_HISTORY:]
    state["equity_history"] = state["equity_history"][-MAX_EQUITY_HISTORY:]
    state["btc_price_history"] = state["btc_price_history"][-MAX_EQUITY_HISTORY:]

    # アトミック書き込み (既存 = primary)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str, ensure_ascii=False))
    tmp.replace(STATE_PATH)

    # ★ Phase 5 追加: DB dual-write (secondary、失敗しても継続)
    if DB_DUAL_WRITE_AVAILABLE:
        try:
            sync_state_to_db(state, mode='sim')
        except Exception as e:
            log(f"⚠️ DB dual-write failed (JSON は OK): {e}")
```

**重要**: DB 書き込みが失敗しても JSON は既に書かれているため、SIM は継続する。
これが「JSON primary + DB secondary」設計の安全性。

### 修正箇所 3: snapshot 周期で reconcile を呼ぶ (オプション)

`run_loop()` の SNAPSHOT_INTERVAL 処理付近で、毎時 1 回 reconcile を実行:

```python
# 1 時間ごとに reconcile (差分があれば log + Discord 通知)
if int(time.time()) % 3600 == 0:
    try:
        from scripts.reconcile_dual_write import reconcile
        from db.repositories.state_repo import read_latest_snapshot
        db_snap = read_latest_snapshot(mode='sim')
        if db_snap:
            res = reconcile(state, db_snap)
            if not res['ok']:
                log(f"🚨 reconcile mismatch: {res['issues']}")
    except Exception as e:
        log(f"reconcile error: {e}")
```

## デプロイ手順 (3 段階、各段階で動作確認)

### 段階 1: dual-write 開始 (このパッチ適用)

1. 現行 SIM 停止
   ```bash
   # PowerShell (管理者)
   Stop-Process -Name python -Force  # demo_runner.py のみピンポイントで止めたい場合は PID 指定
   ```
2. **state.json バックアップ** (既存に上書きしてしまわないよう)
   ```bash
   cp results/demo_state.json snapshots/pre_phase5/demo_state_$(date +%Y%m%d).json
   ```
3. パッチ適用 (上記 修正箇所 1, 2)
4. SIM 再起動
   ```bash
   PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv/Scripts/python demo_runner.py &
   ```
5. **30 分稼働確認**:
   - `tail -f demo_runner.log` で「⚠️ DB dual-write failed」が出ないこと
   - `python scripts/reconcile_dual_write.py` で diff 0%

### 段階 2: dual-write 監視期間 (Phase 6, 3-5 日)

cron で毎時 reconcile を回す:

```cron
# 1 時間ごとに reconcile、失敗時 exit code 1 → Slack/Discord
0 * * * * cd /path/to/kimochi-max && .venv/Scripts/python scripts/reconcile_dual_write.py --json > /tmp/reconcile.log 2>&1 || curl -X POST $SLACK_WEBHOOK -d "@/tmp/reconcile.log"
```

`scripts/backup_db.py` も毎日実行:
```cron
30 0 * * * cd /path/to/kimochi-max && .venv/Scripts/python scripts/backup_db.py
```

### 段階 3: 切替 (Phase 7、3-5 日後)

dual-write 期間で 0 件 reconcile mismatch を確認した後、JSON 書き込みを停止:

1. SIM 停止
2. 最終 reconcile を取る (`--json` で記録)
3. デプロイ用フラグを切替:
   ```python
   # demo_runner.py
   READ_SOURCE = 'db'  # それまで 'json'
   WRITE_JSON = False   # JSON 書き込み停止
   ```
4. SIM 再起動
5. **1 週間後**に JSON file (results/demo_state.json) を `legacy/` へ退避

## ロールバック

| シナリオ | 対応 |
|---------|------|
| 段階 1 後に DB エラー多発 | パッチを revert、元の demo_runner.py に戻す。JSON は無事 |
| 段階 1 で reconcile mismatch > 0.01% | パッチ revert、原因調査 (cent 化の round-off 等) |
| 段階 2 で 1 件でも mismatch | 原因特定するまで段階 3 に進まない |
| 段階 3 切替後 DB 障害 | `READ_SOURCE = 'json'` に戻す、JSON 復元は `cp legacy/...` |

## デバッグ tips

### DB だけ読んで現状把握
```bash
python -c "
from db.repositories.state_repo import read_latest_snapshot
import json
s = read_latest_snapshot(mode='sim')
print(json.dumps(s, indent=2, default=str))
"
```

### reconcile を JSON で取って解析
```bash
python scripts/reconcile_dual_write.py --json | jq '.issues'
```

### 過去 state を再構築
```bash
python -c "
from db.repositories.state_repo import query_snapshots
df = query_snapshots(mode='sim', limit=10)
print(df[['snapshot_id','ts','equity','cash','drawdown_pct']])
"
```

## 注意点

- **JSON が primary で、DB は secondary** (これが Phase 5 の根本設計)
- DB 障害時 SIM 停止しない (JSON は書かれる)
- reconcile mismatch は段階 2 で 0 件であること必須
- **Windows ↔ Linux 物理コピー禁止** (WAL ファイル破損)、必ず `scripts/backup_db.py` 経由
- PnL は cent 整数化 (round-off 防止)、表示時 /100
