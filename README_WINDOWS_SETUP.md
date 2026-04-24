# Windows セットアップガイド — 気持ちマックス v2.2

macOS 前提のコードベースを Windows で SIM 常駐稼働させる手順書。

## 前提
- Windows 10/11、bash (Git Bash / WSL) もしくは PowerShell
- winget 利用可能
- Python 3.12 (winget で導入)

## 1. 依存導入

```bash
winget install -e --id Python.Python.3.12 --silent
gh repo clone kimochi-ichiba/kimochi-max ~/projects/kimochi-max
cd ~/projects/kimochi-max
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m pip install websocket-client   # 非同梱
```

## 2. 起動

```bash
# Bot
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 ./.venv/Scripts/python.exe demo_runner.py

# 静的 UI (別ターミナル)
./.venv/Scripts/python.exe -m http.server 8767 --directory results --bind 0.0.0.0
```

アクセス: **http://localhost:8767/demo.html**

## 3. 常駐化 (Startup VBS 方式、admin 不要)

`startup-*.vbs` を `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` に配置すると、ログイン時に自動起動する。配布している VBS は以下:

| VBS | 起動対象 | 役割 |
|---|---|---|
| `startup-demo.vbs` | `demo_runner.py` | 気持ちマックス Bot 本体 |
| `startup-demo-ui.vbs` | `http.server 8767` | UI 配信 |
| `startup-halluc.vbs` | `hallucination_monitor_v2.py --daemon` | 5分毎13項目監視 |
| `startup-alert-watcher.vbs` | `alert_watcher.py` | 異常通知 + ログローテ + 日次バックアップ |

各 `.bat` には `:loop / timeout 5 / goto loop` でクラッシュ時 5 秒後自動再起動が入っている。

### 環境変数
- `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1`: Windows 既定 cp932 での絵文字 print クラッシュ回避 (**必須**)

### admin 権限でさらに堅くするなら (任意)
`nssm install kimochi-max-bot ...` で Windows サービス化すればログイン前から起動可能。現状は UAC ハングのため未適用。

## 4. 監視・アラート

### hallucination_monitor_v2.py の 13 項目
- [1-4] 外部データ整合性 (BTC/ETH/SOL/BNB 多取引所クロス、時刻同期、ユニバース維持)
- [5] demo_runner プロセス稼働
- [6-9] state 鮮度・version・ach_config・WS 接続
- [10-11] 取引履歴 OHLC 内 / 残高整合
- [12] プロセス重複検知 (Windows venv の launcher+child 親子ペアはデデュープ)
- [13] **バックテスト乖離監視**: SIM 30 日以降、月率 ±5% を超えたら警告

異常検出で `HALLUCINATION_DETECTED.flag` 作成 → `alert_watcher.py` が検知して:
1. BurntToast モジュール有れば Windows Toast
2. 無ければ `msg.exe` でメッセージボックス
3. `logs/alerts.log` に常時記録

### state.json の日次バックアップ
`state_backups/demo_state_YYYY-MM-DD.json` に1日1回コピー、**7日超は自動削除**。

### ログローテーション
`logs/*.log` は 10MB 超えたら `.1` にリネーム（1世代保持）。`alert_watcher` が1時間毎チェック。

## 5. 停止

```bash
cmd.exe //c "C:\\Users\\9626s\\projects\\kimochi-max\\stop-all.bat"
```

`stop-all.bat` は:
- `cmd.exe` の `:loop` プロセスを PowerShell で CommandLine マッチして kill
- ポート 8080/8766/8767 の python を taskkill
- 全 kimochi-max 配下の python を Stop-Process

## 6. トラブルシュート

| 現象 | 原因 | 対処 |
|---|---|---|
| `UnicodeEncodeError: 'cp932'` | デフォルトコードページ | `PYTHONIOENCODING=utf-8` を設定 |
| port already in use | 過去プロセス残存 | `stop-all.bat` を実行 |
| halluc monitor が FAIL=2 | Windows venv 重複 | 本 PR の `_find_pids_by_cmdline` で解決済 |
| demo.html に v2.1 表示 | upstream の更新漏れ | 本 PR の表記統一で解決済 |
| Discord 通知が来ない | `discord_config.json` 未設定 | `python discord_notify.py setup` で対話設定 |

## 7. Linux / macOS でも動く？

はい。`_find_pids_by_cmdline` は `os.name != "nt"` 側で `pgrep -f` にフォールバック。`Path(__file__).resolve().parent` 方式なのでどの OS でも動作。
