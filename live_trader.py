"""
気持ちマックス 実取引モジュール (Binance Spot)
====================================================
HMAC-SHA256 署名で Binance REST API を呼び、BTC/USDT の買い/売りを実行。

安全装置:
  1. 環境変数 BINANCE_API_KEY + BINANCE_SECRET が両方揃っていないと動かない
  2. 環境変数 LIVE_ENABLED=1 が無いと実発注しない (デフォルト dry_run)
  3. 1注文あたり最大 $MAX_ORDER_USD (デフォルト $100)
  4. 1日あたり最大 MAX_DAILY_TRADES 回 (デフォルト 5回)
  5. APIキーに "Read" + "Spot Trading" 権限のみ (出金権限は不要)
  6. IP制限推奨 (Binanceコンソールで設定)

起動時チェック:
  - サーバー時刻とBinance時刻の差 > 1000ms なら警告
  - 残高不足なら拒否
  - ペア BTCUSDT の最小取引単位以下なら拒否

使用例:
    from live_trader import LiveTrader, get_mode
    mode = get_mode()  # "sim" or "live" or "dry_run"
    if mode == "live":
        trader = LiveTrader()
        trader.market_buy("BTCUSDT", quote_usd=50)
"""
from __future__ import annotations
import os, sys, json, time, hmac, hashlib, urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Binance Spot API
BINANCE_API = "https://api.binance.com"

# 安全装置 (環境変数で上書き可)
MAX_ORDER_USD = float(os.environ.get("KM_MAX_ORDER_USD", "100"))      # 1注文上限
MAX_DAILY_TRADES = int(os.environ.get("KM_MAX_DAILY_TRADES", "5"))    # 1日上限
MIN_BTC_ORDER = 0.00001  # Binance BTC最小取引単位

STATE_PATH = Path("/Users/sanosano/projects/kimochi-max/results/demo_state.json")
TRADE_LOG = Path("/Users/sanosano/projects/kimochi-max/live_trades.jsonl")


def get_mode():
    """現在のモードを判定: 'sim' / 'dry_run' / 'live'

    - sim        : 環境変数 KM_MODE=sim or 未設定 (デフォルト)
    - dry_run    : KM_MODE=live かつ APIキーあり、LIVE_ENABLED 未設定
                    → シグナル生成だけして発注はスキップ
    - live       : KM_MODE=live かつ APIキー2つあり かつ LIVE_ENABLED=1
                    → 本番発注 (実資金動く)
    """
    mode = os.environ.get("KM_MODE", "sim").lower()
    if mode != "live":
        return "sim"
    key = os.environ.get("BINANCE_API_KEY", "")
    sec = os.environ.get("BINANCE_SECRET", "")
    if not key or not sec:
        return "sim"  # キー未設定なら強制SIM
    if os.environ.get("LIVE_ENABLED") == "1":
        return "live"
    return "dry_run"


def log_trade(event: dict):
    """取引イベントをJSONLに追記"""
    event["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


class LiveTrader:
    """Binance Spot 実取引クライアント (HMAC署名)"""

    def __init__(self):
        self.api_key = os.environ.get("BINANCE_API_KEY", "")
        self.secret = os.environ.get("BINANCE_SECRET", "")
        if not self.api_key or not self.secret:
            raise RuntimeError("BINANCE_API_KEY / BINANCE_SECRET 環境変数が未設定")
        self._verify_time_sync()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # HMAC 署名 + HTTP
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _sign(self, params: dict) -> str:
        q = urllib.parse.urlencode(params)
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return f"{q}&signature={sig}"

    def _public_get(self, path: str, params: dict = None) -> dict:
        url = f"{BINANCE_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Kimochimax/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _signed_request(self, method: str, path: str, params: dict = None) -> dict:
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = self._sign(params)
        url = f"{BINANCE_API}{path}?{query}"
        req = urllib.request.Request(
            url, method=method,
            headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "Kimochimax/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Binance API {e.code}: {body}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # サーバー時刻同期
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _verify_time_sync(self):
        server = self._public_get("/api/v3/time")
        local_ms = int(time.time() * 1000)
        diff = abs(server["serverTime"] - local_ms)
        if diff > 1000:
            raise RuntimeError(f"⚠️ サーバー時刻ズレ {diff}ms - Mac時刻を同期してください")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 残高確認 (読み取り専用)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def get_balance(self, asset: str = None) -> dict:
        """Spot残高を取得"""
        acc = self._signed_request("GET", "/api/v3/account")
        balances = {b["asset"]: {"free": float(b["free"]), "locked": float(b["locked"])}
                    for b in acc["balances"] if float(b["free"]) + float(b["locked"]) > 0}
        if asset:
            return balances.get(asset, {"free": 0, "locked": 0})
        return balances

    def get_spot_permissions(self) -> list:
        """APIキーの権限を確認"""
        acc = self._signed_request("GET", "/api/v3/account")
        perms = []
        if acc.get("canTrade"): perms.append("TRADE")
        if acc.get("canWithdraw"): perms.append("WITHDRAW")
        if acc.get("canDeposit"): perms.append("DEPOSIT")
        return perms

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 1日取引回数チェック
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _count_today_trades(self) -> int:
        if not TRADE_LOG.exists():
            return 0
        today = datetime.now(timezone.utc).date().isoformat()
        count = 0
        with open(TRADE_LOG) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("logged_at", "").startswith(today):
                        count += 1
                except Exception:
                    pass
        return count

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Market Buy / Sell
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def market_buy(self, symbol: str, quote_usd: float) -> dict:
        """成行買い: quote_usd (USDT) 分だけ購入"""
        if quote_usd > MAX_ORDER_USD:
            raise RuntimeError(f"注文サイズ ${quote_usd} が上限 ${MAX_ORDER_USD} 超過")
        daily = self._count_today_trades()
        if daily >= MAX_DAILY_TRADES:
            raise RuntimeError(f"本日の取引上限 {MAX_DAILY_TRADES}回 に到達 ({daily}回)")

        # USDT残高確認
        bal = self.get_balance("USDT")
        if bal["free"] < quote_usd:
            raise RuntimeError(f"USDT残高不足: 必要${quote_usd} / 保有${bal['free']:.2f}")

        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_usd:.2f}",  # USDT建てで指定
        }
        result = self._signed_request("POST", "/api/v3/order", params)
        log_trade({
            "mode": "LIVE", "action": "BUY", "symbol": symbol,
            "quote_usd": quote_usd, "result": result,
        })
        return result

    def market_sell_all(self, symbol: str, asset: str) -> dict:
        """成行売り: 指定アセットの全量を売却"""
        daily = self._count_today_trades()
        if daily >= MAX_DAILY_TRADES:
            raise RuntimeError(f"本日の取引上限 {MAX_DAILY_TRADES}回 に到達")

        bal = self.get_balance(asset)
        qty = bal["free"]
        if qty < MIN_BTC_ORDER and asset == "BTC":
            raise RuntimeError(f"{asset}保有量 {qty} が最小単位 {MIN_BTC_ORDER} 未満")
        if qty <= 0:
            raise RuntimeError(f"{asset}保有なし")

        # Binance step size 調整 (BTC: 8桁)
        qty_str = f"{qty:.6f}".rstrip("0").rstrip(".")

        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty_str,
        }
        result = self._signed_request("POST", "/api/v3/order", params)
        log_trade({
            "mode": "LIVE", "action": "SELL", "symbol": symbol,
            "asset": asset, "qty": qty, "result": result,
        })
        return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# スタンドアローンCLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cmd_check():
    """API設定・権限・残高の確認 (読み取り専用)"""
    print("=" * 70)
    print("🔐 Binance API 接続チェック")
    print("=" * 70)
    mode = get_mode()
    print(f"\n現在のモード: {mode.upper()}")

    key = os.environ.get("BINANCE_API_KEY", "")
    sec = os.environ.get("BINANCE_SECRET", "")
    live_enabled = os.environ.get("LIVE_ENABLED", "")
    print(f"  BINANCE_API_KEY: {'✅ 設定済み' if key else '❌ 未設定'} ({key[:8]+'...' if key else '-'})")
    print(f"  BINANCE_SECRET:  {'✅ 設定済み' if sec else '❌ 未設定'}")
    print(f"  LIVE_ENABLED:    {'✅ 1 (本番取引ON)' if live_enabled == '1' else '❌ 未設定 (dry_run)'}")
    print(f"  MAX_ORDER_USD:   ${MAX_ORDER_USD}")
    print(f"  MAX_DAILY_TRADES: {MAX_DAILY_TRADES}回/日")

    if not key or not sec:
        print("\n⚠️ APIキーが未設定です。以下を ~/.zshrc または ~/.bash_profile に追加:")
        print("   export BINANCE_API_KEY='your_api_key_here'")
        print("   export BINANCE_SECRET='your_secret_here'")
        print("   export KM_MODE='live'")
        print("   # 実取引を有効化するには最後に:")
        print("   export LIVE_ENABLED='1'")
        return

    print("\n🔌 Binance接続テスト...")
    try:
        trader = LiveTrader()
        perms = trader.get_spot_permissions()
        print(f"   ✅ 接続成功")
        print(f"   APIキー権限: {', '.join(perms)}")
        if "WITHDRAW" in perms:
            print(f"   ⚠️ 警告: 出金権限があります。セキュリティのためOFF推奨")

        print("\n💰 残高:")
        bal = trader.get_balance()
        if not bal:
            print("   (全残高ゼロ)")
        else:
            for asset, b in sorted(bal.items()):
                total = b["free"] + b["locked"]
                print(f"   {asset:8s}: free={b['free']:>12.6f} locked={b['locked']:>12.6f} total={total:>12.6f}")

        print(f"\n📊 本日の実取引回数: {trader._count_today_trades()}/{MAX_DAILY_TRADES}")
    except Exception as e:
        print(f"   ❌ エラー: {e}")


def cmd_test_buy():
    """最小テスト注文 ($1 BTCを成行買い) - 本当に発注される"""
    mode = get_mode()
    if mode != "live":
        print(f"❌ テスト注文は LIVE モード必須。現在: {mode}")
        print("   LIVE_ENABLED=1 BINANCE_API_KEY=xxx BINANCE_SECRET=xxx python3 live_trader.py test-buy")
        return
    trader = LiveTrader()
    print("⚠️ $10 BTCを本番発注します。3秒後に実行...")
    for i in range(3, 0, -1):
        print(f"   {i}...")
        time.sleep(1)
    try:
        result = trader.market_buy("BTCUSDT", quote_usd=10)
        print("✅ 発注成功:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"❌ 発注失敗: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "check":
        cmd_check()
    elif sys.argv[1] == "test-buy":
        cmd_test_buy()
    else:
        print(f"使い方:")
        print(f"  python3 live_trader.py check       # API接続・残高確認")
        print(f"  python3 live_trader.py test-buy    # $10テスト発注 (LIVE_ENABLED=1必須)")
