"""
Binance WebSocket BTC Ticker ストリーム受信
==================================================
wss://stream.binance.com:9443/ws/btcusdt@ticker から24h ticker情報を常時受信。
レート制限なし (WebSocket接続を常時維持)。
自動再接続、スレッドセーフ。

使用例:
    from ws_ticker import BTCTickerStream
    stream = BTCTickerStream()
    stream.start()
    while True:
        data = stream.get()
        print(data["price"], data["change_24h_pct"])
        time.sleep(1)
"""
from __future__ import annotations
import threading, json, time, sys

try:
    import websocket  # from websocket-client package
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False


class BTCTickerStream:
    """Binance WebSocket BTCUSDT 24h ticker ストリーム"""
    WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"

    def __init__(self, log_fn=None):
        """log_fn: オプションのログ出力関数 (msg: str) -> None"""
        self.latest = {
            "price": None,
            "change_24h_pct": None,
            "volume_24h_usdt": None,
            "high_24h": None,
            "low_24h": None,
            "ts": 0,           # 最後の受信 Unix 秒
            "connected": False,
        }
        self.lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._log = log_fn or (lambda msg: print(msg, flush=True))
        self._ws = None
        self._reconnect_count = 0

    def _on_message(self, ws, msg):
        try:
            d = json.loads(msg)
            with self.lock:
                self.latest = {
                    "price": float(d["c"]),                   # 最終取引価格
                    "change_24h_pct": float(d["P"]),          # 24h % change
                    "volume_24h_usdt": float(d["q"]),         # 24h 出来高 (USDT)
                    "high_24h": float(d["h"]),
                    "low_24h": float(d["l"]),
                    "ts": time.time(),
                    "connected": True,
                }
        except Exception as e:
            self._log(f"[WS] on_message エラー: {e}")

    def _on_error(self, ws, error):
        self._log(f"[WS] エラー: {error}")
        with self.lock:
            self.latest["connected"] = False

    def _on_close(self, ws, close_status_code, close_msg):
        self._log(f"[WS] 切断 (code={close_status_code})")
        with self.lock:
            self.latest["connected"] = False

    def _on_open(self, ws):
        self._reconnect_count = 0
        with self.lock:
            self.latest["connected"] = True
        self._log(f"[WS] 接続成功 {self.WS_URL}")

    def _run(self):
        if not WEBSOCKET_AVAILABLE:
            self._log("[WS] ⚠️ websocket-client 未インストール → WebSocket無効")
            return
        while not self._stop:
            try:
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                # 30秒ごとにping、15秒でtimeout → 切断検出
                # (旧10秒はモバイル/不安定回線で誤切断する場合あり、15秒に拡大)
                self._ws.run_forever(ping_interval=30, ping_timeout=15)
            except Exception as e:
                self._log(f"[WS] 接続失敗: {e}")
            if self._stop:
                break
            # 再接続バックオフ (最大60秒)
            self._reconnect_count += 1
            backoff = min(5 * self._reconnect_count, 60)
            self._log(f"[WS] {backoff}秒後に再接続 (試行 {self._reconnect_count} 回目)")
            time.sleep(backoff)

    def start(self):
        """バックグラウンドスレッドでWebSocket接続開始"""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def get(self):
        """最新データをdictで取得 (コピー)"""
        with self.lock:
            return dict(self.latest)

    def is_fresh(self, max_age_seconds=30):
        """最後の受信から max_age_seconds 以内なら True"""
        with self.lock:
            if self.latest["ts"] == 0:
                return False
            return (time.time() - self.latest["ts"]) <= max_age_seconds

    def stop(self):
        """ストリーム停止"""
        self._stop = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# スタンドアローン動作確認
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    print("=" * 60)
    print("Binance BTC WebSocket Ticker スモークテスト")
    print(f"websocket-client 利用可能: {WEBSOCKET_AVAILABLE}")
    print("=" * 60)

    if not WEBSOCKET_AVAILABLE:
        print("❌ websocket-client がインストールされていません")
        print("   解決: /usr/bin/python3 -m pip install --user websocket-client")
        sys.exit(1)

    stream = BTCTickerStream()
    stream.start()
    print("🔌 接続中... 10秒間受信テスト")
    for i in range(10):
        time.sleep(1)
        d = stream.get()
        if d["price"] is not None:
            print(f"  [{i+1}秒] BTC: ${d['price']:>10,.2f} | 24h: {d['change_24h_pct']:>+.2f}% | "
                  f"接続: {'🟢' if d['connected'] else '🔴'} | fresh: {stream.is_fresh()}")
        else:
            print(f"  [{i+1}秒] まだデータなし... 接続: {'🟢' if d['connected'] else '🔴'}")
    stream.stop()
    print("テスト完了")
