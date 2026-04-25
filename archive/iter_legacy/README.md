# Archived iter scripts (Phase 3.1)

このディレクトリは **過去 iter の HTML レポート生成スクリプト** を退避したもの。
バックテストロジックは含まれず、既存 JSON 結果から HTML を再生成するだけ。

## アーカイブ判定基準

`scripts/classify_iters.py` の判定で `archive_html` カテゴリ:
- `_html` で終わるファイル名
- HTML 生成あり、`run_bt` 関数定義/呼び出しなし

## 復元手順

必要になったら `git mv archive/iter_legacy/_iterNN_html.py .` で root に戻せる。

## 含まれるファイル

- _iter41_html.py 〜 _iter51_html.py (12 本)
  - iter41 〜 iter51 のレポート HTML 生成
  - 元 JSON は `results/iter*_html.json` 等に既に出力済
