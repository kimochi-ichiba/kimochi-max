# 二段構成デプロイ設計書 (v3.2)

選択肢 B「法人化 + 二段構成」を SIM/実運用で実装する場合の設計書。

## ⚠️ 検証結果: バックテストでは二段構成は失敗

`results/wf_validate_v24/v3_2_two_tier_wf.md` で検証済:
- 100% v2.5_chop: 40.1 万円
- 75/25 default: 30.1 万円 (**-10 万円**)
- 75/25 diversified: 30.9 万円 (-9 万円)
- 75/25 aggressive: 29.1 万円 (-11 万円)

**バックテスト可能な範囲では 100% chop が最強**。

## それでも実装する価値がある場合

バックテストは **Binance 上場済の生存銘柄のみ** が対象。
真の memecoin (Pump.fun 等) は **データセット外**で:

- 月 1-数回の TP 5x チャンス
- 99% rug pull リスク
- 取引所未上場、オンチェーン直接取引

→ 「バックテスト不可能だが、現実には存在する利益機会」を狙う場合のみ二段構成を検討。

## アーキテクチャ

```
┌─────────────────────────────────────────────────┐
│ 法人 / 個人口座 (初期 10 万円 USDT 想定)            │
└────┬──────────────────────────┬─────────────────┘
     │ 75% (7.5 万円)             │ 25% (2.5 万円)
     ▼                            ▼
┌──────────────────────┐   ┌──────────────────────┐
│ Tier 1: メイン Bot    │   │ Tier 2: Sniper Bot    │
│ demo_runner.py (v2.5) │   │ tier2_sniper_runner   │
│  - BTC EMA200 戦略   │   │  - 新規上場検知       │
│  - ACH multi_lookback│   │  - DEX (Pump.fun) API │
│  - chop ATR filter   │   │  - 5x TP / -50% SL   │
│  - state.json #1     │   │  - state.json #2     │
└──────────┬───────────┘   └──────────┬───────────┘
           │                          │
           ▼                          ▼
       Binance API #1            Solana RPC + Phantom
       (実 spot 取引)             (DEX 取引)
```

**重要**: 2 つの Bot は資金的に**完全分離**。一方が破綻しても他方に影響しない。

## デプロイ手順 (法人口座想定)

### 1. 法人設立 (税務最適化)
- 仮想通貨個人運用は雑所得 (累進最大 55%)
- 法人運用は法人税 (実効 25-33%)
- 1 億円超える規模になったら検討
- 初期 10-100 万円規模なら個人で OK

### 2. 口座準備
| 用途 | 取引所 / プラットフォーム | 資金 |
|------|------------------------|------|
| Tier 1 メイン | Binance / Bybit | 75% (7.5 万円) |
| Tier 2 Sniper | Phantom Wallet (Solana) | 25% (2.5 万円) |

### 3. Bot デプロイ

#### Tier 1: メイン Bot (現状のまま)
```bash
# 既存の v2.5 Bot を 75% 資金で運用
INITIAL=7500 python demo_runner.py
```
state は `results/demo_state.json`、PR #10 + ATR chop filter (PR #12) 適用済の v2.5_chop。

#### Tier 2: Sniper Bot (新規実装)
```bash
# 新規 sniper bot を別プロセスで
INITIAL=2500 python tier2_sniper_runner.py
```
state は `results/sniper_state.json`、Solana DEX API 経由。

### 4. リスク隔離

| 項目 | Tier 1 | Tier 2 |
|------|--------|--------|
| 初期資金 | 75 万円 | 25 万円 |
| 最悪ケース | DD 50% (37.5 万円) | 全損 (0 円) |
| 取引所 | Binance | Solana DEX |
| API キー | 別 | 別 |
| プロセス | 別 | 別 |
| log file | demo_runner.log | sniper.log |
| 資金移動 | **なし** (独立運用) | **なし** |

### 5. 監視

cron で 1 時間ごとに reconcile:
```bash
0 * * * * python scripts/reconcile_two_tier.py >> /var/log/twotier.log
```

reconcile スクリプトは:
- Tier 1 / Tier 2 の equity
- Tier 1 メインの DD (50% 超で warning)
- Tier 2 sniper の TP 件数 (月 1 件以上を目標、ゼロが続けば設定見直し)

### 6. 税務処理 (重要)

#### 個人 (累進課税最大 55%)
```
年間損益 = Tier 1 損益 + Tier 2 損益 (合算可)
雑所得として確定申告
```

#### 法人 (実効税率 25-33%)
```
法人決算で計上、繰越欠損金可
分離課税ではなく総合課税
```

**資金 1 億円超**で法人化を検討。それ未満は個人で運用。

## 終了基準 (撤退条件)

| トリガー | 対応 |
|---------|------|
| Tier 1 DD > 60% | 全 Bot 停止、状況確認 |
| Tier 2 全損 (-100%) | Tier 2 撤去、Tier 1 だけで継続 |
| 1 年以上 Tier 2 で TP ゼロ | Tier 2 設計見直し or 撤去 |
| Tier 1 + Tier 2 合計 DD > 70% | 全資金引き上げ、見直し |

## 実装優先度

1. **Tier 1 のみ運用 (現状)** ← 推奨
   - v2.5_chop で 4-5 倍を堅実に狙う
   - Tier 2 はバックテストで不採用判定
2. **将来: Pump.fun データが取れたら** Tier 2 検討
   - 実 memecoin の listing_date / TP/SL データを蓄積
   - 6 ヶ月以上の実観察後に判断
3. **法人化は資金 5,000 万円超でから**
   - それ以下は税優遇のメリット < 法人運営コスト

## 関連ファイル

- `_wf_validate_v3_2.py`: 二段構成 WF 検証スクリプト
- `_sniper_backtest.py`: スナイパー シミュレータ
- `tier2_sniper_runner.py`: 実 SIM 用 sniper bot 雛形 (本セッションで作成)
- `results/wf_validate_v24/v3_2_two_tier_wf.md`: 検証結果

## 結論

**バックテスト範囲で実装するなら 100% v2.5_chop が最強**。
二段構成は **真の memecoin sniper (Pump.fun 等)** が実現できた場合のみ意味がある。
それまでは Tier 1 だけで運用、Tier 2 はオプション枠として残す。
