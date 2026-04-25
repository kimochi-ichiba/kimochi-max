# iter 書き換えガイド (Phase 3.4)

40 本の `_iter*.py` を `record_run` ベースに書き換える手順。
依存 DAG (`tests/fixtures/iter_dependency_graph.json`) と
分類結果 (`tests/fixtures/iter_classification.json`) を参照しつつ進める。

## 全体方針

1. **葉ノード (34 本)** から書き換え (基盤 iter を破壊しないため)
2. **基盤 6 本** (`_iter43_rethink`, `_iter54_comprehensive`, `_iter49_rigorous`,
   `_iter42_improve`, `_iter52_risk_mgmt`, `_iter60_all_defenses`) は最後
3. **dual-write モード**: 既存 JSON 出力は保持しつつ DB にも書く
4. **数値同一性**: snapshot test で書換前後の同一性確認

## カテゴリ別の対応 (`scripts/classify_iters.py` ベース)

| カテゴリ | 件数 | 対応 |
|---------|------|------|
| `archive_html` | 12 | `archive/iter_legacy/` に移動済 (本 PR で完了) |
| `rewrite_base` | 6 | 最後に書き換え (依存に注意) |
| `rewrite_backtest` | 13 | 順次 `record_run` 化 |
| `rewrite_results` | 7 | JSON dump 部分のみ DB 化 |
| `review` | 3 | 個別判断 |

## パイロット例: `_iter59_v22_verify.py`

本 PR で実施済み。元の `OUT_JSON.write_text(...)` の直後に
`record_run(...)` を追加する dual-write パターン:

```python
# 元の JSON 出力 (保持)
OUT_JSON.write_text(json.dumps(summary, ...))

# DB dual-write (追加)
try:
    from db.repositories.runs_repo import normalize_metrics, record_run
    import ulid as _ulid
    trial_group_id = str(_ulid.new())
    rid = record_run(
        strategy_id="iter59::v21::full_2020_2024",
        run_type="single_backtest",
        params={"BTC_W": 0.35, "ACH_W": 0.35, ...},
        universe=universe,
        period=("2020-01-01", "2024-12-31"),
        metrics=normalize_metrics({
            "cagr": v21["cagr"] / 100,
            "max_dd": v21["max_dd"] / 100,
            "sharpe": v21.get("sharpe", 0),
            ...
        }),
        yearly={int(y): float(r) for y, r in v21["yearly"].items()},
        trial_group_id=trial_group_id,
        cost_model_id="binance_spot_taker_v1",
        script_name="_iter59_v22_verify.py",
    )
except Exception as e:
    print(f"⚠️ DB dual-write skipped: {e}")
```

## 残り iter の書換手順

### Step 1: 対象 iter を選ぶ
```bash
python scripts/iter_dualwrite_patcher.py --list-targets
# 葉ノード (依存解析参照) から選ぶ
```

### Step 2: 既存 main() の構造を読む
- どこで JSON 出力しているか (`OUT_JSON.write_text`、`results_dir / "X.json"` 等)
- どんな metrics dict が作られているか
- universe, period, params がどこに定義されているか

### Step 3: dual-write パッチを当てる

ヘルパスクリプトで雛形挿入:
```bash
python scripts/iter_dualwrite_patcher.py --target _iterNN_xxx --apply
# .pre_patch にバックアップ作成
# 雛形が main() の最後に挿入される
```

挿入された TODO を埋める:
- `params={...}`: iter 内で使われている定数を JSON 化
- `metrics=normalize_metrics({...})`: 既存 JSON 出力の cagr/max_dd 等を渡す
- `period=(...)`: ファイル冒頭の START/END 定数等

### Step 4: snapshot test を書く

`tests/regression/test_iterNN_snapshot.py`:
```python
def test_iterNN_metrics_unchanged(tmp_path):
    """書換前後で出力 JSON が一致することを確認."""
    pre = json.loads((PROJECT / "tests" / "snapshots" / "iterNN_pre.json").read_text())
    # iter 実行
    subprocess.run([sys.executable, "_iterNN_xxx.py"])
    post = json.loads((PROJECT / "results" / "iterNN_xxx.json").read_text())
    # 主要メトリクスが一致 (絶対 1e-4 / 相対 1e-3)
    assert abs(pre["cagr"] - post["cagr"]) < 1e-4
```

事前 snapshot 取得:
```bash
git stash               # パッチ前に戻す
python _iterNN_xxx.py   # 旧コードで実行
cp results/iterNN_xxx.json tests/snapshots/iterNN_pre.json
git stash pop           # パッチを戻す
pytest tests/regression/test_iterNN_snapshot.py
```

### Step 5: 1 本ずつコミット

```bash
git add _iterNN_xxx.py tests/regression/test_iterNN_snapshot.py tests/snapshots/iterNN_pre.json
git commit -m "リファクタ: _iterNN_xxx.py に DB dual-write 追加 (record_run)"
```

## 基盤 iter (rewrite_base) の特殊事項

`_iter43_rethink` が 15 個から import されているなど、書換時にグローバル定数の
書換 (`M.CORR_THRESHOLD = 0.80` 等) が依存先に伝播する。書換時は:

1. 葉ノード書換完了後に基盤を書換
2. グローバル定数書換は破棄、関数引数として渡すように refactor
3. 各依存 iter を再実行して数値同一性確認

## 完了後

全 40 (実質 27 本、archive 除く) の書換が終われば:
- `RunsAnalyzer().sharpe_distribution()` で全 iter の横断比較可能
- `RunsAnalyzer.is_promotable(run_id)` で新採用候補を CI gate
- Phase 4 の PBO/DSR 計算が全 iter に対して有効化

## 進捗トラッキング

```bash
# 現在の record_run 化済 iter を確認
python -c "
from analysis.runs_analyzer import RunsAnalyzer
df = RunsAnalyzer().to_df()
print(df['strategy_id'].str.split('::').str[0].unique())
"
```

## 参考: パイロット (_iter59_v22_verify.py)

本 PR 実施済。`grep -A 50 'Phase 3.3' _iter59_v22_verify.py` で実例参照。
