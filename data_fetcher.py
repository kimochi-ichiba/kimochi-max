"""
data_fetcher.py — マーケットデータ取得モジュール
================================================
CCXTライブラリを使ってBinanceから価格データを取得する。
・APIキーなし → 公開エンドポイントでOHLCVを取得（シミュレーション用）
・APIキーあり → 残高・注文情報も取得可能（本番用）

キャッシュ機能付き（同じ時間軸のデータを何度も取得しないようにする）
"""

import time
import logging
from typing import Optional
import pandas as pd

try:
    import ccxt
except ImportError:
    raise ImportError("pip install ccxt を実行してください")

try:
    import requests as _requests_lib
except ImportError:
    raise ImportError("pip install requests を実行してください")

from config import Config, Mode
from utils import setup_logger, timeframe_to_seconds

logger = setup_logger("data_fetcher")


# ════════════════════════════════════════════════════
# OHLCV データキャッシュ
# ════════════════════════════════════════════════════

class OHLCVCache:
    """
    同じ時間軸のOHLCVデータをメモリに保持するキャッシュ。
    毎回APIを叩かなくて済むのでレート制限（1分間のリクエスト上限）に引っかかりにくくなる。
    """
    def __init__(self):
        self._cache: dict[str, pd.DataFrame] = {}
        self._last_fetch: dict[str, float] = {}

    def get(self, key: str, max_age_s: float) -> Optional[pd.DataFrame]:
        """キャッシュからデータを取得。max_age_s秒以上古い場合はNoneを返す。"""
        if key not in self._cache:
            return None
        age = time.time() - self._last_fetch.get(key, 0)
        if age > max_age_s:
            return None
        return self._cache[key]

    def set(self, key: str, df: pd.DataFrame):
        """データをキャッシュに保存"""
        self._cache[key] = df
        self._last_fetch[key] = time.time()


# ════════════════════════════════════════════════════
# メインデータ取得クラス
# ════════════════════════════════════════════════════

class DataFetcher:
    """
    CCXTを使ってBinanceからマーケットデータを取得するクラス。

    使い方:
        fetcher = DataFetcher(config)
        df_1m  = fetcher.fetch_ohlcv("BTC/USDT", "1m")  # 1分足
        df_5m  = fetcher.fetch_ohlcv("BTC/USDT", "5m")  # 5分足
        price  = fetcher.fetch_current_price("BTC/USDT")
    """

    def __init__(self, config: Config):
        self.config = config
        self.cache  = OHLCVCache()
        self._exchange = self._create_exchange()
        # v35.0b: requests.Session でBinance直接HTTPを使う（keep-alive接続プール）
        # requests.get() は毎回新規SSL接続 → macOS LibreSSLで遅延/タイムアウト頻発。
        # Session は接続を再利用（keep-alive）するため2回目以降はSSLハンドシェイク不要 → 高速・安定。
        # ccxt内部も requests.Session を使っているため同じ理由で ccxt は安定していた。
        self._session = _requests_lib.Session()
        # v36.0f: ネットワーク不安定時のシンボルクールダウン管理
        # 全timeframeが失敗した銘柄は90秒スキップしてスキャン速度を維持
        self._fetch_cooldown: dict = {}   # symbol → skip_until (timestamp)

    def _create_exchange(self) -> ccxt.Exchange:
        """
        設定に応じたCCXT取引所インスタンスを生成する。
        本番モード+APIキーあり → 認証済みインスタンス
        それ以外 → 公開APIのみ（認証なし）
        """
        exchange_class = getattr(ccxt, self.config.exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"取引所 '{self.config.exchange_id}' はCCXTでサポートされていません")

        params: dict = {
            "enableRateLimit": True,   # レート制限を自動で守る
            "options": {
                "defaultType": self.config.market_type,  # future or spot
            },
        }

        # 本番モードでAPIキーがある場合だけ認証情報をセット
        from config import is_live_mode
        if is_live_mode(self.config):
            params["apiKey"]    = self.config.api_key
            params["secret"]    = self.config.api_secret
            logger.info("🔑 本番モード: APIキーで認証します")
        else:
            logger.info(f"🧪 {self.config.mode}モード: 公開APIのみ使用します（お金は動きません）")

        exchange = exchange_class(params)

        if self.config.testnet:
            try:
                exchange.set_sandbox_mode(True)
                logger.info("🛡️ テストネット（サンドボックス）モードを有効化しました")
            except Exception:
                pass  # テストネット未対応の取引所は無視

        # v32.0: exchangeInfoの自動ロードをバイパス
        # 問題: ccxtはfetch_ohlcv/fetch_ticker呼び出し時に自動でload_markets()を実行。
        #      load_markets()はexchangeInfoエンドポイントを呼び出すが、このエンドポイントは
        #      大容量レスポンスのためタイムアウト/地域制限でブロックされる場合がある。
        # 解決策: markets_loaded=Trueを設定してauto-load-marketsをバイパス。
        #       ccxtはシンボル変換(BTC/USDT→BTCUSDT)を内部ルールで実行できるため
        #       marketsデータなしでも基本的なAPI呼び出しは動作する。
        try:
            exchange.markets_loaded = True
            if not hasattr(exchange, 'markets') or not exchange.markets:
                exchange.markets = {}
            logger.debug("v32.0 exchangeInfoバイパス: markets_loaded=True設定完了")
        except Exception:
            pass  # バイパス設定失敗は無視（ccxtが更新された場合など）

        # v35.0: タイムアウトを5秒に短縮（デフォルト~10秒より素早く失敗してスキップ）
        # 理由: ccxt fetch_ohlcv は keep-alive セッションを使うため requests.get() より安定。
        #       ただしデフォルト10秒タイムアウトでは遅延銘柄1つで10秒ロスするため5秒に短縮。
        try:
            exchange.timeout = 5000  # ミリ秒単位（5秒）
        except Exception:
            pass

        return exchange

    # ── OHLCV取得（v35.0b: requests.Session で直接HTTP）────
    # v33.0b は requests.get() を使ったがタイムアウト頻発 → 原因は毎回新規SSL接続。
    # v35.0b は requests.Session を使う → keep-alive接続プールで SSL ハンドシェイク再利用。
    # 同じ理由で ccxt も安定していた（内部で requests.Session を使用）。
    # ccxt 経由は load_markets()→exchangeInfo の自動呼び出しを防ぐバイパスが無効(v4.5.48)。
    # Session 版は ccxt を完全に迂回し、Binance klines API に直接アクセスする。
    _TF_MAP = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h",
        "12h": "12h", "1d": "1d", "3d": "3d", "1w": "1w",
    }

    def _fetch_ohlcv_http(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        """
        requests.Session（keep-alive）でBinance /api/v3/klines を呼び出す。
        v36.0e: タイムアウト10→5秒に短縮（タイムアウト時の待機を半減させスキャン速度を改善）。
        Session keep-aliveで接続再利用時は実質数ミリ秒。タイムアウトは接続切れのみ。
        """
        sym_clean = symbol.replace("/", "")
        interval  = self._TF_MAP.get(timeframe, timeframe)
        url = (
            f"https://api.binance.com/api/v3/klines"
            f"?symbol={sym_clean}&interval={interval}&limit={limit}"
        )
        resp = self._session.get(url, timeout=5)
        resp.raise_for_status()
        raw = resp.json()

        if not raw:
            return pd.DataFrame()

        # Binance klines レスポンス形式:
        # [openTime, open, high, low, close, volume, closeTime, ...]
        df = pd.DataFrame(raw, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "num_trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("timestamp").sort_index()
        df = df.astype(float)
        return df

    def fetch_ohlcv(self, symbol: str, timeframe: str,
                    limit: int = 200, use_cache: bool = True) -> pd.DataFrame:
        """
        指定した銘柄・時間軸のOHLCVデータを取得してDataFrameで返す。

        OHLCVとは:
        O = Open（始値）, H = High（高値）, L = Low（安値）,
        C = Close（終値）, V = Volume（出来高）

        引数:
            symbol:    銘柄シンボル（例: "BTC/USDT"）
            timeframe: 時間軸（例: "1m", "5m", "15m", "1h"）
            limit:     取得するローソク足の本数
            use_cache: Trueのとき、直近のデータはキャッシュから返す
        """
        tf_secs = timeframe_to_seconds(timeframe)
        cache_key = f"{symbol}:{timeframe}"

        # キャッシュ有効期限 = 時間軸の1本分の長さ
        if use_cache:
            cached = self.cache.get(cache_key, max_age_s=tf_secs)
            if cached is not None:
                return cached

        # v35.0b: requests.Session直接HTTP（2回まで再試行）
        for attempt in range(2):
            try:
                df = self._fetch_ohlcv_http(symbol, timeframe, limit=limit)
                if not df.empty:
                    self.cache.set(cache_key, df)
                    return df
                return pd.DataFrame()
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"OHLCV取得失敗(試行1/2): {symbol} {timeframe} → {e}")
                    time.sleep(0.5)
                else:
                    logger.warning(f"OHLCV取得失敗(試行2/2): {symbol} {timeframe} → {e}")

        return pd.DataFrame()

    def fetch_multi_timeframe(self, symbol: str) -> dict[str, pd.DataFrame]:
        """
        設定で指定した全時間軸のOHLCVデータを一括取得する。
        戻り値: {"1m": DataFrame, "5m": DataFrame, "15m": DataFrame}

        v36.0f: ネットワーク不安定時の自動スキップ機能
        - 全timeframeが失敗した銘柄は90秒クールダウン（タイムアウト待機を回避）
        - これにより188銘柄スキャン中の大量タイムアウトによる詰まりを防ぐ

        キャッシュがある場合はAPIコールをスキップ。
        APIコールが発生したときだけ0.2秒待機する（レート制限対策）。
        これにより2回目以降のスキャンが大幅に高速化される。
        """
        # v36.0f: クールダウン中の銘柄はスキップ（ネットワーク不安定対策）
        _now = time.time()
        if _now < self._fetch_cooldown.get(symbol, 0):
            return {}  # クールダウン中: スキャンをスキップして他の銘柄に移る

        result = {}
        _api_call_count = 0  # APIコール数カウント（クールダウン判定用）
        for tf in self.config.timeframes:
            # まずキャッシュを確認
            tf_secs = timeframe_to_seconds(tf)
            cache_key = f"{symbol}:{tf}"
            cached = self.cache.get(cache_key, max_age_s=tf_secs)
            if cached is not None:
                # キャッシュヒット: APIコールなし、スリープなし
                result[tf] = cached
            else:
                # キャッシュミス: APIコールあり → 取得後にスリープ
                _api_call_count += 1
                df = self.fetch_ohlcv(symbol, tf, limit=self.config.ohlcv_limit, use_cache=False)
                if not df.empty:
                    result[tf] = df
                time.sleep(0.2)  # APIコールした場合だけレート制限対策でスリープ

        # v36.0f: 全timeframeがAPIコール失敗 → 90秒クールダウン設定
        if _api_call_count > 0 and not result:
            self._fetch_cooldown[symbol] = time.time() + 90
            logger.debug(f"{symbol} 全timeframeタイムアウト → 90秒クールダウン設定")
        elif result and symbol in self._fetch_cooldown:
            del self._fetch_cooldown[symbol]  # 成功 → クールダウン解除

        return result

    # ── 現在価格取得 ─────────────────────────────────
    def fetch_current_price(self, symbol: str) -> Optional[float]:
        """最新の取引価格をリアルタイムで取得する"""
        # v35.0b: requests.Session で /api/v3/ticker/price を直接呼び出す
        # v36.0e: timeout=5秒に短縮 + 1回リトライ（オープンポジション管理の安定性向上）
        sym_clean = symbol.replace("/", "")
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={sym_clean}"
        for attempt in range(2):
            try:
                resp = self._session.get(url, timeout=5)
                resp.raise_for_status()
                data = resp.json()
                price = float(data.get("price", 0))
                if price > 0:
                    return price
            except Exception as e:
                if attempt == 1:  # 2回目の失敗のみログ出力
                    logger.warning(f"現在価格の取得に失敗: {symbol} → {e}")
        return None

    # ── 残高取得（本番モードのみ）────────────────────
    def fetch_balance(self) -> dict:
        """
        取引所の口座残高を取得する。
        本番モードでAPIキーがある場合のみ実際の残高を返す。
        シミュレーションモードではPortfolioクラスが管理するのでここは使わない。
        """
        from config import is_live_mode
        if not is_live_mode(self.config):
            logger.warning("シミュレーションモードでは取引所の残高は取得できません")
            return {}
        try:
            return self._exchange.fetch_balance()
        except Exception as e:
            logger.error(f"残高取得エラー: {e}")
            return {}

    # ── Binance出来高ランキングからトップ銘柄を取得 ─────
    def fetch_top_symbols(self, limit: int = 100) -> list:
        """
        Binanceの24時間出来高ランキング上位からUSDTペアを取得する。

        市場の出来高ランキングは時価総額ランキングと高い相関があり、
        APIキー不要の公開エンドポイントで取得できる。

        除外するもの:
          - ステーブルコイン（USDT/USDC/BUSD等）
          - レバレッジトークン（BTCUPなどの特殊商品）
        """
        import re as _re
        # ステーブルコインの除外リスト（USD建て・金建て等）
        STABLECOINS = {
            "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "USDD",
            "FDUSD", "PYUSD", "GUSD", "SUSD", "LUSD", "FRAX", "USTC",
            "UST", "USDN", "CUSD", "HUSD", "RSR", "USD1", "RLUSD",
            "XAUT", "PAXG",  # 金トークン（価格変動が株・仮想通貨と異なる）
        }
        # レバレッジトークン・特殊トークンのパターン
        EXCLUDE_PATTERNS = ["UP", "DOWN", "BULL", "BEAR", "3L", "3S", "2L", "2S"]
        # 正常な銘柄名のパターン（英数字のみ、2〜10文字）
        VALID_BASE_RE = _re.compile(r'^[A-Z0-9]{2,12}$')

        try:
            logger.info(f"🔄 Binance 出来高ランキング上位{limit}銘柄を取得中...")
            # v36.0: Session直接HTTP（ccxt.fetch_tickers()はexchangeInfoを呼ぶため廃止）
            # /api/v3/ticker/24hr?type=MINI → symbol/quoteVolume を軽量に一括取得
            # v36.0c: timeout=30（全2000銘柄レスポンスは約300KB。15sでは間に合わない場合がある）
            url = "https://api.binance.com/api/v3/ticker/24hr?type=MINI"
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
            tickers_raw = resp.json()

            candidates = []
            for data in tickers_raw:
                symbol_raw = data.get("symbol", "")
                if not symbol_raw.endswith("USDT"):
                    continue
                base = symbol_raw[:-4]  # "BTCUSDT" → "BTC"

                # 銘柄名が英数字のみかチェック（中国語・記号等の詐欺コインを除外）
                if not VALID_BASE_RE.match(base):
                    continue
                # ステーブルコイン除外
                if base in STABLECOINS:
                    continue
                # レバレッジトークン除外（UP/DOWN/BULL/BEAR 等を含む銘柄名）
                if any(pat in base for pat in EXCLUDE_PATTERNS):
                    continue

                # 24時間出来高（USDT建て）を取得
                try:
                    quote_vol = float(data.get("quoteVolume", 0))
                except (TypeError, ValueError):
                    quote_vol = 0.0

                if quote_vol > 0:
                    symbol_fmt = base + "/USDT"  # "BTC/USDT" 形式に変換
                    candidates.append((symbol_fmt, quote_vol))

            if not candidates:
                logger.warning("出来高データが取得できませんでした")
                return []

            # 出来高の多い順（≒時価総額ランキング上位）に並べ替えて上位limitを返す
            candidates.sort(key=lambda x: x[1], reverse=True)
            top_symbols = [s[0] for s in candidates[:limit]]

            logger.info(
                f"✅ {len(top_symbols)}銘柄取得完了 "
                f"（1位: {top_symbols[0]}  {limit}位: {top_symbols[-1] if len(top_symbols) >= limit else '?'}）"
            )
            return top_symbols

        except Exception as e:
            logger.error(f"トップ銘柄リスト取得エラー: {e}")
            return []

    # ── バックテスト用の過去データ一括取得 ────────────
    def fetch_historical_ohlcv(self, symbol: str, timeframe: str,
                                since: str, until: str) -> pd.DataFrame:
        """
        バックテスト用に指定期間の全OHLCVデータを取得する。
        Binance公式APIから直接取得（本番データのみ・合成データ禁止）。

        引数:
            since: 開始日時 "2024-01-01"
            until: 終了日時 "2024-12-31"
        """
        from datetime import datetime

        # ── 本番データ強制ガード ──
        # 取引所が Binance であることを確認し、非Binanceソースからの取得を禁止
        exchange_id = getattr(self._exchange, 'id', None)
        if exchange_id != 'binance':
            raise RuntimeError(
                f"本番データ以外の取得を禁止: exchange={exchange_id} "
                f"(binance以外のデータ源は使用できません)"
            )

        since_ms = int(datetime.fromisoformat(since).timestamp() * 1000)
        until_ms = int(datetime.fromisoformat(until).timestamp() * 1000)
        tf_ms    = timeframe_to_seconds(timeframe) * 1000

        all_ohlcv = []
        current_since = since_ms

        logger.info(f"📥 {symbol} {timeframe} {since}〜{until} の過去データを取得中...（Binance公式API）")

        while current_since < until_ms:
            try:
                batch = self._exchange.fetch_ohlcv(
                    symbol, timeframe, since=current_since, limit=1000
                )
                if not batch:
                    break

                # until以降のデータは除外
                batch = [c for c in batch if c[0] < until_ms]
                all_ohlcv.extend(batch)

                if len(batch) < 1000:
                    break  # データの末端に到達

                current_since = batch[-1][0] + tf_ms
                time.sleep(0.5)  # レート制限対策

            except Exception as e:
                logger.error(f"過去データ取得エラー: {e}")
                break

        # 空なら空を返す（フェイク補完せず、呼び出し側でスキップさせる）
        if not all_ohlcv:
            logger.warning(f"⚠️ {symbol} {timeframe}: データ取得0件 → この銘柄をスキップ（合成データで埋めません）")
            return pd.DataFrame()

        df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("timestamp").drop_duplicates().sort_index()
        df = df.astype(float)

        # ── データ健全性チェック（本物検証） ──
        # 価格0以下、NaN、異常値は本物データとしてあり得ない
        if (df[["open", "high", "low", "close"]] <= 0).any().any():
            logger.error(f"❌ {symbol}: 価格0以下の異常値検出 → この銘柄をスキップ")
            return pd.DataFrame()
        if df[["open", "high", "low", "close"]].isna().any().any():
            logger.error(f"❌ {symbol}: NaN検出 → この銘柄をスキップ")
            return pd.DataFrame()

        logger.info(f"✅ {len(df)}本のBinance本番データを取得しました")
        return df
