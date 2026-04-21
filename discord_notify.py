"""
通知モジュール (Discord / Slack 両対応)
==========================================
Webhook URL を自動判定して、Discord または Slack に通知する。

対応:
  - Discord: https://discord.com/api/webhooks/...
  - Slack:   https://hooks.slack.com/services/...

通知タイミング:
  🟢 BUY発動   : BTC EMA200を上抜け、BUY自動実行
  🔴 SELL発動  : BTC EMA200を下抜け、SELL自動実行（損益付き）
  📉 DD警告    : 5% / 10% / 20% ドローダウン到達時
  📊 日次サマリー: 毎日1回、資産状況を報告

設定:
  python3 discord_notify.py setup     # 対話的に Webhook URL を設定
  python3 discord_notify.py test      # テスト通知を送る
  python3 discord_notify.py status    # 現在の設定確認
"""
from __future__ import annotations
import sys, json, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

CONFIG_PATH = Path("/Users/sanosano/projects/kimochi-max/discord_config.json")

DEFAULT_CONFIG = {
    "webhook_url": "",
    "enabled": False,
    "notify_trades": True,
    "notify_dd_alerts": True,
    "notify_daily_summary": True,
    # DD通知閾値 (デフォルト [10, 20, 30]%)
    # 仮想通貨は日次5%変動が日常的なため、5%だと通知スパムになる
    # 10%=注意、20%=警戒、30%=危険レベル
    "dd_alert_thresholds": [10, 20, 30],
    "last_dd_threshold_fired": 0,
    "last_daily_summary_date": None,
    "test_sent": False,
}


def load_config():
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        # デフォルト値で補完
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def detect_platform(url):
    """Webhook URLから送信先プラットフォームを判定"""
    if not url:
        return "unknown"
    if "discord.com" in url or "discordapp.com" in url:
        return "discord"
    if "hooks.slack.com" in url:
        return "slack"
    return "unknown"


def _int_color_to_hex(color_int):
    """Discord embed の int カラーを #RRGGBB に変換"""
    if isinstance(color_int, str):
        return color_int if color_int.startswith("#") else "#" + color_int
    return "#{:06x}".format(int(color_int) & 0xFFFFFF)


def _embed_to_slack_attachment(embed):
    """Discord embed を Slack attachment に変換"""
    att = {}
    if "color" in embed:
        att["color"] = _int_color_to_hex(embed["color"])
    if "title" in embed:
        att["title"] = embed["title"]
    if "description" in embed:
        att["text"] = embed["description"]
    fields = []
    for f in embed.get("fields", []):
        fields.append({
            "title": f.get("name", ""),
            "value": f.get("value", ""),
            "short": bool(f.get("inline", False)),
        })
    if fields:
        att["fields"] = fields
    att["footer"] = "気持ちマックス"
    att["ts"] = int(datetime.now(timezone.utc).timestamp())
    att["mrkdwn_in"] = ["text", "fields"]
    return att


def _post_webhook(webhook_url, payload):
    """Webhookにペイロードを送信。成功ならTrue"""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        print(f"⚠️ Webhook HTTPエラー {e.code}: {e.reason}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"⚠️ Webhook通知失敗: {e}", file=sys.stderr)
        return False


def send_raw(content="", embeds=None, username="気持ちマックス"):
    """生メッセージ送信 (enabled でも送る。設定チェックなし)
       Discord/Slack 両対応 - URLから自動判定"""
    cfg = load_config()
    url = cfg.get("webhook_url")
    if not url:
        return False

    platform = detect_platform(url)

    if platform == "discord":
        payload = {"username": username}
        if content:
            payload["content"] = content
        if embeds:
            payload["embeds"] = embeds
        return _post_webhook(url, payload)

    elif platform == "slack":
        payload = {}
        if content:
            payload["text"] = content
        if embeds:
            attachments = [_embed_to_slack_attachment(e) for e in embeds]
            payload["attachments"] = attachments
            # Slackは text が空だと警告出るので、最初のembedのtitleをtextに
            if not content and embeds:
                payload["text"] = embeds[0].get("title", "気持ちマックス通知")
        payload["username"] = username
        return _post_webhook(url, payload)

    else:
        print(f"⚠️ 不明なWebhook URL形式: {url[:50]}...", file=sys.stderr)
        return False


def send(content="", embeds=None, username="気持ちマックス"):
    """設定チェック付き送信"""
    cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("webhook_url"):
        return False
    return send_raw(content, embeds, username)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 通知イベント
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def notify_startup(initial_capital):
    embed = {
        "title": "🚀 気持ちマックス起動",
        "description": f"デモトレード開始しました (SIMモード)",
        "fields": [
            {"name": "初期資金", "value": f"${initial_capital:,.0f}", "inline": True},
            {"name": "構成", "value": "BTC 40% + ACH 40% + USDT 20%", "inline": True},
            {"name": "実資金", "value": "❌ 使用しません", "inline": True},
        ],
        "color": 0x4fc3f7,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return send_raw(embeds=[embed])


def notify_trade(action, part, price, qty, value_usd, pnl_usd=None, ema200=None):
    """BUY/SELL通知"""
    cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("notify_trades"):
        return False

    is_buy = action == "BUY"
    emoji = "🟢" if is_buy else "🔴"
    color = 0x00e676 if is_buy else 0xf44336
    action_jp = "買い" if is_buy else "売り"

    fields = [
        {"name": "銘柄", "value": part, "inline": True},
        {"name": "価格", "value": f"${price:,.2f}", "inline": True},
        {"name": "数量", "value": f"{qty:.6f}", "inline": True},
        {"name": "金額", "value": f"${value_usd:,.2f}", "inline": True},
    ]
    if ema200 is not None:
        fields.append({"name": "EMA200", "value": f"${ema200:,.2f}", "inline": True})
    if pnl_usd is not None:
        pnl_sign = "+" if pnl_usd >= 0 else ""
        fields.append({
            "name": "今回の損益",
            "value": f"{pnl_sign}${pnl_usd:,.2f}",
            "inline": True
        })

    embed = {
        "title": f"{emoji} {action_jp}シグナル発動",
        "description": f"**{part}** を{action_jp}ました",
        "fields": fields,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return send_raw(embeds=[embed])


def notify_dd_alert(dd_pct, total, peak, initial):
    """DD警告 (閾値を超えた時のみ)"""
    cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("notify_dd_alerts"):
        return False

    thresholds = sorted(cfg.get("dd_alert_thresholds", [5, 10, 20]))
    last_fired = cfg.get("last_dd_threshold_fired", 0)

    # 現在突破している最大閾値
    crossed = None
    for t in thresholds:
        if dd_pct >= t and t > last_fired:
            crossed = t

    # 回復 (DD < 1%) で fired をリセット
    if dd_pct < 1.0 and last_fired > 0:
        cfg["last_dd_threshold_fired"] = 0
        save_config(cfg)
        embed = {
            "title": "✅ ドローダウン回復",
            "description": f"資産がピーク水準に戻りました",
            "fields": [
                {"name": "現在資産", "value": f"${total:,.2f}", "inline": True},
                {"name": "ピーク", "value": f"${peak:,.2f}", "inline": True},
            ],
            "color": 0x00e676,
        }
        return send_raw(embeds=[embed])

    if crossed is None:
        return False

    # 通知送信
    if crossed >= 20:
        emoji = "🚨"; color = 0xf44336; level = "危険"
    elif crossed >= 10:
        emoji = "⚠️"; color = 0xff9800; level = "警戒"
    else:
        emoji = "📉"; color = 0xffca28; level = "注意"

    loss_usd = peak - total
    loss_from_initial = total - initial

    embed = {
        "title": f"{emoji} ドローダウン {crossed}% 突破 ({level})",
        "description": f"資産がピークから **{dd_pct:.2f}%** 下落しました",
        "fields": [
            {"name": "現在資産", "value": f"${total:,.2f}", "inline": True},
            {"name": "ピーク", "value": f"${peak:,.2f}", "inline": True},
            {"name": "下落額", "value": f"-${loss_usd:,.2f}", "inline": True},
            {"name": "元本比", "value": f"{'+' if loss_from_initial >= 0 else ''}${loss_from_initial:,.2f}", "inline": True},
        ],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    ok = send_raw(embeds=[embed])
    if ok:
        cfg["last_dd_threshold_fired"] = crossed
        save_config(cfg)
    return ok


def notify_daily_summary(total, initial, pnl, pnl_pct, dd_max, btc_price, ema200,
                          signal, btc_v, ach_v, usdt_v, n_trades):
    """日次サマリー (1日1回)"""
    cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("notify_daily_summary"):
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    last = cfg.get("last_daily_summary_date")
    if last == today:
        return False  # 既に送信済み

    signal_jp = {"BUY":"🟢 買い","SELL":"🔴 売り","HOLD-IN":"🔵 保有中","HOLD-OUT":"⚪ 現金待機"}.get(signal, signal)
    color = 0x00e676 if pnl >= 0 else 0xf44336 if pnl < -initial * 0.1 else 0xffca28

    embed = {
        "title": "📊 本日の気持ちマックス サマリー",
        "description": f"**{today}**",
        "fields": [
            {"name": "💰 総資産", "value": f"${total:,.2f}", "inline": True},
            {"name": "📈 損益", "value": f"{'+' if pnl >= 0 else ''}${pnl:,.2f} ({pnl_pct:+.2f}%)", "inline": True},
            {"name": "📉 最大DD", "value": f"-{dd_max:.2f}%", "inline": True},
            {"name": "₿ BTC枠", "value": f"${btc_v:,.0f}", "inline": True},
            {"name": "⚡ ACH枠", "value": f"${ach_v:,.0f}", "inline": True},
            {"name": "💵 USDT枠", "value": f"${usdt_v:,.0f}", "inline": True},
            {"name": "BTC価格", "value": f"${btc_price:,.0f}", "inline": True},
            {"name": "EMA200", "value": f"${ema200:,.0f}", "inline": True},
            {"name": "シグナル", "value": signal_jp, "inline": True},
            {"name": "累計取引", "value": f"{n_trades}回", "inline": True},
        ],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    ok = send_raw(embeds=[embed])
    if ok:
        cfg["last_daily_summary_date"] = today
        save_config(cfg)
    return ok


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# セットアップCLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cmd_setup():
    print("=" * 70)
    print("🔧 通知設定 (Discord / Slack 両対応)")
    print("=" * 70)
    print()
    print("Webhook URL を入力してください。Discord または Slack に対応します。")
    print()
    print("【Discord の場合】 取得方法:")
    print("  1. Discord アプリで通知を受け取りたいチャンネルを開く")
    print("  2. チャンネル名を右クリック → 「チャンネルの編集」")
    print("  3. 左メニュー「連携サービス」→「ウェブフック」→「新しいウェブフック」")
    print("  4. 「ウェブフックURLをコピー」")
    print("  URL形式: https://discord.com/api/webhooks/...")
    print()
    print("【Slack の場合】 取得方法:")
    print("  1. https://api.slack.com/apps を開き「Create New App」")
    print("  2. 「From scratch」→アプリ名入力→ワークスペース選択")
    print("  3. 「Incoming Webhooks」→「Activate」→「Add New Webhook to Workspace」")
    print("  4. 通知したいチャンネルを選択→「許可」→URLをコピー")
    print("  URL形式: https://hooks.slack.com/services/...")
    print()
    print("※ URLは secret です。外部に公開しないでください。")
    print()

    url = input("Webhook URL: ").strip()
    platform = detect_platform(url)
    if platform == "unknown":
        print("❌ 認識できないURL形式です")
        print("   Discord: https://discord.com/api/webhooks/ で始まる")
        print("   Slack:   https://hooks.slack.com/services/ で始まる")
        return

    cfg = load_config()
    cfg["webhook_url"] = url
    cfg["enabled"] = True
    cfg["platform"] = platform
    save_config(cfg)

    print()
    platform_name = {"discord": "Discord", "slack": "Slack"}[platform]
    print(f"✅ 設定を保存しました（プラットフォーム: {platform_name}）")
    print(f"✅ テスト通知を送ります...")
    ok = send_raw(embeds=[{
        "title": f"✅ {platform_name}通知 セットアップ完了",
        "description": "気持ちマックスからの通知が届くようになりました🎉",
        "fields": [
            {"name": "通知対象", "value": "🟢 BUY / 🔴 SELL / 📉 DD警告 / 📊 日次サマリー", "inline": False},
        ],
        "color": 0x00e676,
    }])
    if ok:
        print(f"✅ テスト通知を送信しました！{platform_name}を確認してください。")
    else:
        print("⚠️ 送信失敗。URLが正しいか再度確認してください。")


def cmd_test():
    cfg = load_config()
    if not cfg.get("webhook_url"):
        print("❌ まず setup で Webhook URL を設定してください: python3 discord_notify.py setup")
        return
    print("📨 テスト通知を送信中...")
    ok = send_raw(embeds=[{
        "title": "🧪 テスト通知",
        "description": f"気持ちマックスからのテストメッセージです\n時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "color": 0x4fc3f7,
    }])
    print("✅ 送信成功" if ok else "❌ 送信失敗")


def cmd_status():
    cfg = load_config()
    url = cfg.get("webhook_url", "")
    platform = detect_platform(url) if url else "unknown"
    platform_icon = {"discord": "💬 Discord", "slack": "💼 Slack", "unknown": "❓ 不明"}[platform]
    print("=" * 60)
    print("📋 通知設定状況")
    print("=" * 60)
    print(f"  有効:               {'✅ Yes' if cfg.get('enabled') else '❌ No'}")
    print(f"  プラットフォーム:   {platform_icon}")
    print(f"  Webhook URL:        {'設定済み (' + url[:50] + '...)' if url else '未設定'}")
    print(f"  取引通知:           {'✅' if cfg.get('notify_trades') else '❌'}")
    print(f"  DD警告:             {'✅' if cfg.get('notify_dd_alerts') else '❌'}")
    print(f"  日次サマリー:       {'✅' if cfg.get('notify_daily_summary') else '❌'}")
    print(f"  DD閾値:             {cfg.get('dd_alert_thresholds')}")
    print(f"  最終DD閾値発動:     {cfg.get('last_dd_threshold_fired')}%")
    print(f"  最終日次サマリー:   {cfg.get('last_daily_summary_date', '未送信')}")


def cmd_disable():
    cfg = load_config()
    cfg["enabled"] = False
    save_config(cfg)
    print("🔕 Discord通知を無効化しました")


def cmd_enable():
    cfg = load_config()
    if not cfg.get("webhook_url"):
        print("❌ まず setup で Webhook URL を設定してください")
        return
    cfg["enabled"] = True
    save_config(cfg)
    print("🔔 Discord通知を有効化しました")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("\n使い方:")
        print("  python3 discord_notify.py setup     # 初期設定")
        print("  python3 discord_notify.py test      # テスト通知")
        print("  python3 discord_notify.py status    # 設定確認")
        print("  python3 discord_notify.py enable    # 通知を有効化")
        print("  python3 discord_notify.py disable   # 通知を無効化")
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "setup": cmd_setup()
    elif cmd == "test": cmd_test()
    elif cmd == "status": cmd_status()
    elif cmd == "enable": cmd_enable()
    elif cmd == "disable": cmd_disable()
    else:
        print(f"❌ 不明なコマンド: {cmd}")
