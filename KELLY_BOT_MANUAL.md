# 🤖 Kelly Bot 運用マニュアル

**BNB70 + BTC30 Kelly戦略自動売買ボット**

---

## 📋 目次
1. [概要](#概要)
2. [起動方法](#起動方法)
3. [監視とダッシュボード](#監視とダッシュボード)
4. [日常運用](#日常運用)
5. [緊急対応](#緊急対応)
6. [実運用（Live）への移行](#実運用への移行)
7. [よくあるトラブル](#よくあるトラブル)

---

## 📌 概要

### 戦略
- **通貨**: BNB 70% + BTC 30%
- **手法**: Kelly基準による動的レバレッジ
- **Kelly Fraction**: 0.5 (Half Kelly)
- **Lookback**: 60日
- **Max Leverage**: 10倍
- **Rebalance**: 30日ごと
- **Cooldown**: -25%で翌月スキップ
- **Min Leverage Threshold**: 1.0（Kelly<1なら取引せず）
- **Cash Buffer**: 5%

### 期待値 ($3,000スタート)
- **月次**: +10.49%
- **1年後**: 平均$9,707（3.2倍）
- **2年後**: 平均$45,047（15倍）
- **1年プラス率**: 100%（過去36期間全てプラス）
- **最大DD**: 42-49%

---

## 🚀 起動方法

### 初回起動（Paper トレード）
```bash
cd /Users/sanosano/projects/crypto-bot-pro
python3 kelly_bot.py --mode paper --capital 3000
```

### 既存状態を維持したまま再起動
```bash
python3 kelly_bot.py --mode paper --capital 3000
```
→ `kelly_bot_state.json` があれば継続運用

### リセットして最初から
```bash
rm kelly_bot_state.json
python3 kelly_bot.py --mode paper --capital 3000
```

---

## 👀 監視とダッシュボード

### 🌐 Safariダッシュボード
```bash
python3 dashboard.py
```
→ Safariで `http://localhost:8765` を開く

### 📊 自動監視（cron登録済み）
- **15分ごと**: `monitor_kelly_bot.py`（健全性チェック）
- **毎日9時**: `daily_snapshot.py`（日次記録）

### 📝 ログファイル
| ファイル | 内容 |
|---|---|
| `monitor.log` | 全監視ログ |
| `monitor_alerts.log` | 警告のみ |
| `monitor_cron.log` | Cron実行ログ |
| `snapshot.log` | 日次スナップショット |
| `snapshots/snap_YYYY-MM-DD.json` | 日次記録 |
| `kelly_bot_state.json` | 現在のボット状態 |

### 📈 日次レポート表示
```bash
python3 daily_snapshot.py --report
```

---

## 📅 日常運用

### 🌅 朝のチェック（推奨5分）
1. ダッシュボードで総資産確認
2. `tail -20 monitor.log` でエラーチェック
3. DD -20%以内を確認

### ⚡ リバランス日（30日ごと）
- 自動で実行される（`monitor_kelly_bot.py`が検知）
- リバランス後はログで新ポジション確認

### 🛑 Cooldown発動時
- 前月-25%以下の損失時に発動
- **何もしなくてOK** - 翌月自動で再開
- その間は現金100%で待機

---

## 🚨 緊急対応

### 🔥 DD-30%以上に達した時
```bash
# 1. 一旦状態確認
python3 monitor_kelly_bot.py

# 2. 市場環境を見る
#    - BTCが30%以上下落中ならベア相場の可能性
#    - 通常の調整なら継続推奨

# 3. 緊急停止する場合
pkill -f kelly_bot.py
# cronも停止
crontab -e で */15 と 0 9 の行をコメントアウト
```

### 💀 清算発生時
- 理論上は清算リスクほぼゼロだが、極端相場で発生の可能性
- ログ確認: `grep liquidation monitor.log`
- 資金を$3,000再投入するか判断

### 🔁 戦略リセット
```bash
# 完全リセット
rm kelly_bot_state.json
rm -rf snapshots/

# 再起動
python3 kelly_bot.py --mode paper --capital 3000
```

---

## 💰 実運用（Live）への移行

### 前提条件チェック
- [ ] Paper Trading 1ヶ月以上実施
- [ ] 日次スナップショットで +5%以上の実績
- [ ] 清算ゼロ、Cooldown1回以下
- [ ] Binance先物アカウント開設済み
- [ ] API Key取得済み（Futures Trading権限）
- [ ] **失っても良い$3,000のみ投入**

### 移行手順
```bash
# 1. APIキー設定 (.bashrc or .zshrcに追加)
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_api_secret"

# 2. .envファイル経由でも可
echo "BINANCE_API_KEY=xxxx" > .env
echo "BINANCE_API_SECRET=xxxx" >> .env

# 3. ペーパー状態をクリア
rm kelly_bot_state.json

# 4. 小額（$100）でテスト起動
python3 kelly_bot.py --mode live --capital 100

# 5. 1週間様子を見る

# 6. 問題なければ$3,000で本番
python3 kelly_bot.py --mode live --capital 3000
```

### ⚠️ Live運用での注意
- 実際の約定・スリッページはpaperより大きい
- 取引所の一時障害に備える
- 定期的にログを見る（週1以上）
- DDが予想を超えたら即停止

---

## 🔧 よくあるトラブル

### Q1: ダッシュボードが表示されない
```bash
# ポート確認
lsof -i:8765

# 停止してから再起動
pkill -f dashboard.py
python3 dashboard.py
```

### Q2: Kelly計算が0になってエントリーされない
- **Kelly < 1.0なら仕様通りスキップ**（これは正常動作）
- BNBが弱い市場ではBTCのみエントリー
- 両方弱ければ全スキップ（=今月取引しない）

### Q3: Cooldown永久に解除されない
```bash
# 状態ファイル編集
python3 -c "
import json
from pathlib import Path
p = Path('kelly_bot_state.json')
s = json.loads(p.read_text())
s['cooldown_active'] = False
p.write_text(json.dumps(s, indent=2))
print('Cooldown解除')
"
```

### Q4: Binance API エラー
```bash
# レート制限確認
grep "429\|rate" monitor.log

# APIキー確認
python3 -c "import os; print('KEY:', bool(os.environ.get('BINANCE_API_KEY')))"
```

---

## 📊 スコア評価基準

| 項目 | 合格ライン |
|---|---|
| プラス率（1年窓） | ≥ 90% |
| 月次平均 | ≥ +8% |
| 清算回数 | 0 |
| 最大DD | ≤ 50% |
| Cooldown発動 | ≤ 2回/年 |

---

## 📞 緊急連絡・ログ確認コマンド集

```bash
# 現在の状態を一行で
python3 -c "import json; s=json.load(open('kelly_bot_state.json')); print(f'残高: \${s[\"total_capital\"]:,.2f} | ポジション: {len(s[\"positions\"])}件')"

# 今日の動き
python3 daily_snapshot.py --report | tail -20

# 監視ログ末尾
tail -50 monitor.log

# エラーだけ抽出
grep -i "error\|liquid\|critical" monitor.log | tail -20

# Cron動作確認
crontab -l | grep -v "^#"
```

---

## 🎯 パフォーマンス目標

### Month 1-3
- プラス率60%以上
- DD 50%以内
- ボットが安定稼働

### Year 1
- 総リターン +100%以上
- 清算ゼロ
- 月次変動の慣れ

### Year 2
- $3,000 → $30,000-$50,000
- 運用ノウハウの蓄積
- 必要なら別戦略併用検討

---

**⚠️ 重要: 仮想通貨は高リスク資産です。失っても良い金額のみで運用してください。**

最終更新: 2026-04-18
戦略バージョン: Kelly v2 (改善版)
