"""
気持ちマックス ヘルスモニター (常時監視)
====================================================
60秒ごとに以下をチェックし、異常があれば Discord/Slack 通知 + 自動修復を試みる:

  1. demo_runner プロセス生存確認
  2. http.server (8080) プロセス生存確認
  3. demo_state.json 更新鮮度 (5分以内)
  4. WebSocket接続状態 (state.ws_connected)
  5. WebSocket鮮度 (state.ws_age_sec < 30秒)

異常時の動作:
  - 初回検出: Discord/Slack警告通知
  - 継続3分: launchctl kickstart で自動再起動
  - 継続10分: 重大アラート (Discord DANGER通知)

ログ: /tmp/kimochi_health.log
状態: /tmp/kimochi_health_state.json

起動:
  python3 health_monitor.py
"""
from __future__ import annotations
import sys, json, time, subprocess, os
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")
try:
    import discord_notify
    NOTIFY_AVAILABLE = True
except ImportError:
    NOTIFY_AVAILABLE = False

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
STATE_PATH = PROJECT / "results/demo_state.json"
HEALTH_STATE = Path("/tmp/kimochi_health_state.json")
LOG_PATH = Path("/tmp/kimochi_health.log")

CHECK_INTERVAL = 60        # 60秒ごとチェック
STALE_STATE_SEC = 300      # state.json 5分以上古いとstale判定
STALE_WS_SEC = 30          # ws_age 30秒以上でWS stale判定
# 自動再起動までの猶予 (デフォルト 5分)
#   旧値(3分=180秒)はstate.json書き込み中の再起動でデータ破損リスクあり
#   5分なら一時的な遅延を吸収しつつ、本当に異常な場合は確実に再起動
AUTO_RESTART_AFTER = 300
DANGER_AFTER = 600         # 10分連続異常で重大アラート


def log(msg, also_print=True):
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}"
    if also_print:
        print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_health_state():
    if not HEALTH_STATE.exists():
        return {"first_error_at": None, "last_restart_at": None,
                "last_danger_notified_at": None, "last_check_at": None,
                "consecutive_errors": 0}
    try:
        return json.loads(HEALTH_STATE.read_text())
    except Exception:
        return {"first_error_at": None, "last_restart_at": None,
                "last_danger_notified_at": None, "last_check_at": None,
                "consecutive_errors": 0}


def save_health_state(s):
    HEALTH_STATE.write_text(json.dumps(s, indent=2, default=str))


def check_process(name_pattern: str) -> dict:
    """pgrep でプロセス生存確認"""
    try:
        r = subprocess.run(["pgrep", "-f", name_pattern],
                            capture_output=True, text=True, timeout=5)
        pids = [p for p in r.stdout.strip().split("\n") if p and p != str(os.getpid())]
        return {"running": len(pids) > 0, "pids": pids}
    except Exception as e:
        return {"running": False, "error": str(e)}


def check_state_file() -> dict:
    """demo_state.json 鮮度とWS状態"""
    if not STATE_PATH.exists():
        return {"exists": False}
    try:
        mtime = datetime.fromtimestamp(STATE_PATH.stat().st_mtime, tz=timezone.utc)
        age = (datetime.now(timezone.utc) - mtime).total_seconds()
        state = json.loads(STATE_PATH.read_text())
        ws_age = state.get("ws_age_sec")
        return {
            "exists": True,
            "file_age_sec": round(age, 1),
            "file_fresh": age < STALE_STATE_SEC,
            "ws_connected": state.get("ws_connected", False),
            "ws_age_sec": ws_age,
            "ws_fresh": ws_age is not None and ws_age < STALE_WS_SEC,
            "total_equity": state.get("total_equity"),
            "last_update": state.get("last_update"),
            "signal": state.get("btc_part", {}).get("last_signal"),
        }
    except Exception as e:
        return {"exists": True, "error": str(e)}


def launchctl_restart(service: str):
    """launchctl kickstart でサービス再起動"""
    try:
        uid = os.getuid()
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{service}"],
            capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as e:
        return False, str(e)


def notify_warning(title: str, detail: str, level: str = "warn"):
    """Discord/Slack通知 (level: warn/danger/ok)"""
    if not NOTIFY_AVAILABLE:
        return False
    try:
        cfg = discord_notify.load_config()
        if not cfg.get("enabled"):
            return False
    except Exception:
        return False

    colors = {"warn": 0xffca28, "danger": 0xf44336, "ok": 0x00e676}
    icons = {"warn": "⚠️", "danger": "🚨", "ok": "✅"}
    try:
        discord_notify.send_raw(embeds=[{
            "title": f"{icons.get(level, '📡')} [ヘルス監視] {title}",
            "description": detail,
            "color": colors.get(level, 0xffca28),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }])
        return True
    except Exception as e:
        log(f"⚠️ 通知失敗: {e}", also_print=False)
        return False


def run_check():
    """1回のヘルスチェック実行"""
    hs = load_health_state()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec='seconds')

    # ① demo_runner
    demo = check_process("demo_runner.py")
    # ② Webサーバー
    server = check_process("http.server 8080")
    # ③ state.json
    state = check_state_file()

    errors = []
    if not demo["running"]:
        errors.append("demo_runner プロセス停止")
    if not server["running"]:
        errors.append("Webサーバー停止")
    if state.get("exists"):
        if not state.get("file_fresh"):
            errors.append(f"state.json が {state.get('file_age_sec')}秒更新されていない")
        if not state.get("ws_fresh") and state.get("ws_age_sec") is not None:
            errors.append(f"WebSocket stale ({state.get('ws_age_sec')}秒前)")
    else:
        errors.append("state.json が存在しない")

    has_error = len(errors) > 0
    hs["last_check_at"] = now_iso

    if has_error:
        # 異常状態
        hs["consecutive_errors"] += 1
        if hs["first_error_at"] is None:
            hs["first_error_at"] = now_iso
            log(f"⚠️ 異常検出: {'; '.join(errors)}")
            notify_warning("異常検出", "\n".join(f"- {e}" for e in errors), "warn")

        error_duration = (now - datetime.fromisoformat(hs["first_error_at"])).total_seconds()

        # 3分継続 → 自動再起動
        if error_duration >= AUTO_RESTART_AFTER:
            last_restart = hs.get("last_restart_at")
            if not last_restart or (now - datetime.fromisoformat(last_restart)).total_seconds() > 300:
                log(f"🔄 自動再起動 (異常継続 {int(error_duration)}秒)")
                if not demo["running"]:
                    ok, msg = launchctl_restart("com.sanosano.kimochimax.demo")
                    log(f"   demo再起動: {'✅' if ok else '❌'} {msg}")
                if not server["running"]:
                    ok, msg = launchctl_restart("com.sanosano.kimochimax.server")
                    log(f"   server再起動: {'✅' if ok else '❌'} {msg}")
                hs["last_restart_at"] = now_iso
                notify_warning("自動再起動実行",
                                f"{int(error_duration)}秒連続異常のため、launchctl kickstart を実行",
                                "warn")

        # 10分継続 → 重大アラート
        if error_duration >= DANGER_AFTER:
            last_danger = hs.get("last_danger_notified_at")
            if not last_danger or (now - datetime.fromisoformat(last_danger)).total_seconds() > 600:
                log(f"🚨 重大アラート (異常 {int(error_duration)}秒継続)")
                notify_warning("重大: 自動修復失敗",
                                f"{int(error_duration)}秒以上異常が継続しています。手動対応を検討してください。\n\n"
                                + "\n".join(f"- {e}" for e in errors),
                                "danger")
                hs["last_danger_notified_at"] = now_iso
    else:
        # 正常復帰
        if hs["first_error_at"] is not None:
            duration = int((now - datetime.fromisoformat(hs["first_error_at"])).total_seconds())
            log(f"✅ 正常復帰 (異常継続 {duration}秒)")
            notify_warning("正常復帰",
                            f"{duration}秒の異常から自動復帰しました。現在は正常稼働中。",
                            "ok")
        hs["first_error_at"] = None
        hs["consecutive_errors"] = 0

    save_health_state(hs)

    # 詳細ログ (正常時は簡易、異常時は詳細)
    if has_error:
        log(f"📊 demo={demo.get('running')} server={server.get('running')} "
            f"state_fresh={state.get('file_fresh')} ws={state.get('ws_fresh')} | "
            f"errors={len(errors)}")
    else:
        # 15分に1回だけ正常ログ (静か)
        if hs.get("consecutive_errors", 0) == 0 and int(time.time()) % 900 < CHECK_INTERVAL:
            log(f"✅ 正常: total=${state.get('total_equity', 0):,.2f} "
                f"signal={state.get('signal')} ws_age={state.get('ws_age_sec')}s")


def run_loop():
    log("=" * 60)
    log("🏥 気持ちマックス ヘルスモニター 起動")
    log(f"   チェック間隔: {CHECK_INTERVAL}秒")
    log(f"   stale閾値: state {STALE_STATE_SEC}秒 / WS {STALE_WS_SEC}秒")
    log(f"   自動再起動: {AUTO_RESTART_AFTER}秒連続異常")
    log(f"   重大アラート: {DANGER_AFTER}秒継続")
    log("=" * 60)
    while True:
        try:
            run_check()
        except Exception as e:
            log(f"⚠️ ヘルスチェック例外: {e}")
            import traceback
            log(traceback.format_exc(), also_print=False)
        try:
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log("⚠️ 中断されました")
            break


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_check()
        print(f"\n現在の状態: {json.dumps(load_health_state(), indent=2, default=str)}")
    elif "--status" in sys.argv:
        print(json.dumps(load_health_state(), indent=2, default=str, ensure_ascii=False))
    else:
        run_loop()
