# 気持ちマックス (kimochi-max)

仮想通貨トレーディングボット「気持ちマックス」。

## 概要

- 元 `crypto-bot-pro` から分岐した独立ボット
- 売買Pro モード（SIM/実運用対応）
- 1時間足 + AI信頼度ベースのシグナル判定
- レバレッジ 3倍・最大同時20ポジション

## 起動方法

```bash
# SIMモード（シミュレーション）
python main.py --mode simulation --port 8080

# ダッシュボード
python dashboard.py
```

## バックテスト成績（v79-v85 最適化結果）

| 設定 | 月率 | 年率 | DD | 備考 |
|---|---|---|---|---|
| v81l（攻撃型） | +3.6% | +52.7% | 9.3% | 現行ロジック上限 |
| v81d（バランス） | +2.5% | +35.1% | 7.3% | スコア87 |
| v83a（安全型） | +1.9% | +26.3% | 6.0% | 推奨・低DD |

## ファイル構成

- `main.py` - メインのトレーディングエンジン
- `config.py` - 設定ファイル
- `entry_scorer.py` - エントリースコアリングロジック
- `risk_manager.py` - リスク管理
- `dashboard.py` - 監視ダッシュボード
- `backtester.py` - バックテストエンジン

## 関連

元リポジトリ: [crypto-bot-pro](https://github.com/kimochi-ichiba/crypto-bot-pro)
