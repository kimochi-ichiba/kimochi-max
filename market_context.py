"""
market_context.py — リアルタイム市場センチメント取得モジュール
=============================================================
5分ごとに外部の無料APIから市場全体の雰囲気（センチメント）を取得し、
売買判断の精度を高めるためのデータを提供する。

取得するデータ:
  1. Fear & Greed Index（恐怖&強欲指数）— alternative.me 無料API
       0  = Extreme Fear（極度の恐怖 = 皆が売っている）
       100 = Extreme Greed（極度の強欲 = 皆が買っている）
       → 極度の強欲ではロングのポジションサイズを縮小（過熱相場）
       → 極度の恐怖ではショートのポジションサイズを縮小（売られすぎ）

  2. Binance先物ファンディングレート（上位10銘柄の平均）
       正の値 = ロングポジションが多い → ロングは資金調達コストがかかる
       負の値 = ショートポジションが多い → ショートは資金調達コストがかかる
       → 高いポジティブファンディング = 過熱LONGシグナル → 新規ロング控えめ
       → 高いネガティブファンディング = 過熱SHORTシグナル → 新規ショート控えめ

  3. 市場全体のトレンドスコア（ウォッチリスト全体の騰落率）
       自前のdataFetcherから毎スキャンで更新
"""

import time
import threading
import urllib.request
import json
import logging

from utils import setup_logger

logger = setup_logger("market_context")


# Binance先物でファンディングレートを確認する主要銘柄
_FUNDING_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
]


class MarketContext:
    """
    外部APIから市場センチメントを5分ごとに取得するクラス。

    使い方:
        ctx = MarketContext()
        # ... ボット起動 ...
        mult = ctx.get_long_size_multiplier()  # 0.5〜1.0: ロングサイズ調整係数
    """

    FETCH_INTERVAL_S: float = 300.0   # 5分ごとに更新

    def __init__(self):
        # Fear & Greed
        self.fear_greed: int       = 50          # 0〜100
        self.fear_greed_label: str = "Neutral"

        # Binanceファンディングレート（小数。0.0001 = 0.01%）
        self.avg_funding_rate: float = 0.0
        self.funding_details: dict   = {}        # symbol → rate

        # 市場全体スコア（ウォッチリストのうち何%がロングシグナル）
        self.market_bullish_pct: float = 50.0    # 0〜100

        # 更新タイムスタンプ
        self.last_update: float = 0.0
        self.is_ready: bool     = False

        self._lock    = threading.Lock()
        self._stopped = False
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="market-ctx"
        )
        self._thread.start()
        logger.info("📡 マーケットコンテキスト起動（5分ごとにFear&Greed・ファンディング取得）")

    # ── バックグラウンドループ ─────────────────────────
    def _loop(self):
        while not self._stopped:
            try:
                self._fetch_all()
            except Exception as e:
                logger.warning(f"マーケットコンテキスト取得エラー: {e}")
            time.sleep(self.FETCH_INTERVAL_S)

    def _fetch_all(self):
        """全データを取得してキャッシュを更新する"""
        self._fetch_fear_greed()
        self._fetch_funding_rates()
        with self._lock:
            self.last_update = time.time()
            self.is_ready    = True

    # ── Fear & Greed Index ────────────────────────────
    def _fetch_fear_greed(self):
        """
        Crypto Fear & Greed Index を alternative.me から取得する（無料・キー不要）。

        Fear & Greed とは:
          SNSの投稿量・価格変動・出来高・市場支配率などを合算して
          「今の市場が恐怖か強欲か」を0〜100の数字で表した指標。
          Warren Buffettの名言「皆が恐れているときに貪欲になれ」の
          定量化バージョン。
        """
        try:
            url = "https://api.alternative.me/fng/?limit=1&format=json"
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-bot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            value = int(data["data"][0]["value"])
            label = data["data"][0]["value_classification"]
            with self._lock:
                self.fear_greed       = value
                self.fear_greed_label = label
            logger.info(
                f"📊 Fear & Greed: {value}/100 ({label}) "
                f"{'🔴 下落トレンド→ショート有利' if value <= 30 else '🟢 上昇トレンド→ロング有利' if value >= 70 else '🟡 中立'}"
            )
        except Exception as e:
            logger.debug(f"Fear&Greed取得失敗（スキップ）: {e}")

    # ── Binanceファンディングレート ────────────────────
    def _fetch_funding_rates(self):
        """
        Binance先物のファンディングレートを主要10銘柄分取得する（無料・キー不要）。

        ファンディングレートとは:
          先物取引では定期的（8時間ごと）にロングとショートの間でお金が動く。
          + = ロングがショートにお金を払う（ロングが多すぎる = 過熱）
          - = ショートがロングにお金を払う（ショートが多すぎる = 売られすぎ）

        0.01%以上 = ロング過熱 → 新規ロングのサイズを縮小
        -0.01%以下 = ショート過熱 → 新規ショートのサイズを縮小
        """
        rates  = {}
        errors = 0
        for sym in _FUNDING_SYMBOLS:
            try:
                url = (
                    f"https://fapi.binance.com/fapi/v1/fundingRate"
                    f"?symbol={sym}&limit=1"
                )
                req = urllib.request.Request(
                    url, headers={"User-Agent": "crypto-bot/1.0"}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read())
                if data:
                    rates[sym] = float(data[0]["fundingRate"])
            except Exception:
                errors += 1

        if rates:
            avg = sum(rates.values()) / len(rates)
            with self._lock:
                self.avg_funding_rate = avg
                self.funding_details  = dict(rates)
            logger.info(
                f"📊 ファンディングレート平均: {avg*100:.4f}% "
                f"({len(rates)}/{len(_FUNDING_SYMBOLS)}銘柄取得)"
                f" {'⚠️ ロング過熱' if avg > 0.0001 else '⚠️ ショート過熱' if avg < -0.0001 else '正常'}"
            )
        else:
            logger.debug(f"ファンディングレート取得失敗（全{errors}件エラー）")

    # ── 市場全体スコア更新（ボット内部から呼ぶ）────────
    def update_market_breadth(self, bullish_count: int, total_count: int):
        """
        ウォッチリスト全体のうち何%がロングシグナルを出しているかを更新する。
        TradingBotのスキャン完了時に呼ばれる。
        """
        if total_count > 0:
            pct = bullish_count / total_count * 100
            with self._lock:
                self.market_bullish_pct = pct

    # ── エントリーサイズ調整係数 ─────────────────────
    def get_long_size_multiplier(self) -> float:
        """
        LONGエントリーのポジションサイズを調整する係数を返す（0.35〜1.0）。

        【トレンドフォロー型: ベア相場ではロングを大幅縮小】
        F&G < 30（恐怖〜極度の恐怖）= ベア相場 = ロングは負けやすい → 縮小
        実績: F&G=23時のLONG勝率は31%と低く、ショート60%より大幅に劣る
        """
        with self._lock:
            fg = self.fear_greed
            fr = self.avg_funding_rate

        # Fear&Greedによる調整（トレンドフォロー型: 恐怖=下落=ロング縮小）
        if fg <= 20:
            fg_mult = 0.35   # 極度の恐怖 = 強烈な下落トレンド → ロング35%
        elif fg <= 30:
            fg_mult = 0.45   # 恐怖 = ベア相場 → 45%（旧70%→縮小）
        elif fg <= 40:
            fg_mult = 0.70   # やや恐怖 → 70%
        elif fg >= 80:
            fg_mult = 0.80   # 極度の強欲 = 過熱反落リスク → 少し縮小
        elif fg >= 70:
            fg_mult = 0.90   # 強欲 → 90%
        else:
            fg_mult = 1.00   # 中立〜やや強欲（40〜69） = 最適環境

        # ファンディングレートによる調整（ロング過熱は危険）
        if fr >= 0.00015:    # 0.015% 以上 = ロングかなり過熱
            fr_mult = 0.70
        elif fr >= 0.0001:   # 0.01% 以上 = ロングやや過熱
            fr_mult = 0.85
        else:
            fr_mult = 1.00

        return fg_mult * fr_mult

    def get_short_size_multiplier(self) -> float:
        """
        SHORTエントリーのポジションサイズを調整する係数を返す（0.40〜0.70）。

        【ベア相場ではショートを適切に許容する版】
        実績: F&G=23時のSHORT勝率60%（LONG勝率31%より大幅に優秀）
        対策: ベア相場（F&G<40）では従来の「ショート禁止20%」を廃止し適切に許容。

        F&G < 30（極度の恐怖） = 強い下落トレンド = ショート有利 → 50%サイズ
        F&G 30〜50（恐怖〜中立） = やや有利 → 40〜45%サイズ
        F&G > 70（強欲〜極度の強欲） = 上昇過熱→ショート有効 → 65〜70%サイズ
        """
        with self._lock:
            fg = self.fear_greed
            fr = self.avg_funding_rate

        # Fear&Greedによる調整
        if fg >= 80:
            fg_mult = 0.70   # 極度の強欲 = 上昇過熱→ショート有効
        elif fg >= 70:
            fg_mult = 0.65   # 強欲 → 65%
        elif fg >= 60:
            fg_mult = 0.55   # やや強欲 → 55%
        elif fg >= 50:
            fg_mult = 0.45   # 中立 → 45%
        elif fg >= 40:
            fg_mult = 0.40   # やや恐怖 → 40%
        elif fg >= 30:
            fg_mult = 0.45   # 恐怖 → 45%（ベア相場中盤: ショート有利）
        else:
            fg_mult = 0.50   # 極度の恐怖 → 50%（強い下落トレンド: ショート最も有利）
        # 理由: F&G<30は強い下落トレンドを示す。LONG勝率31%に対しSHORT勝率60%の実績あり。

        # ファンディングレートによる調整（ショート過熱は反発リスク）
        if fr <= -0.00015:
            fr_mult = 0.70   # ショート過熱 → 縮小
        elif fr <= -0.0001:
            fr_mult = 0.85
        elif fr >= 0.00015:
            fr_mult = 1.10   # ロング過熱 = ショート有利 → 少し拡大
        else:
            fr_mult = 1.00

        return min(1.0, fg_mult * fr_mult)

    def get_snapshot(self) -> dict:
        """現在のマーケットコンテキストを辞書で返す（API・ダッシュボード用）"""
        # ロックを1回だけ取って全データを読み出してから計算する（デッドロック防止）
        with self._lock:
            fg   = self.fear_greed
            fg_l = self.fear_greed_label
            fr   = self.avg_funding_rate
            fd   = dict(self.funding_details)
            mbp  = self.market_bullish_pct
            lu   = self.last_update
            rdy  = self.is_ready

        age_s = time.time() - lu if lu > 0 else -1

        # ロック外でサイズ係数を計算（get_long/short_size_multiplier はロックを取るため外で呼ぶ）
        long_mult  = self.get_long_size_multiplier()
        short_mult = self.get_short_size_multiplier()

        return {
            "fear_greed":        fg,
            "fear_greed_label":  fg_l,
            "fear_greed_emoji":  (
                "😱" if fg <= 25 else
                "😨" if fg <= 40 else
                "😐" if fg <= 60 else
                "😏" if fg <= 75 else "🤑"
            ),
            "avg_funding_rate":       round(fr * 100, 5),
            "funding_details":        {k: round(v * 100, 5) for k, v in fd.items()},
            "market_bullish_pct":     round(mbp, 1),
            "long_size_mult":         round(long_mult, 2),
            "short_size_mult":        round(short_mult, 2),
            "last_update_age_s":      round(age_s),
            "is_ready":               rdy,
        }

    def stop(self):
        self._stopped = True
