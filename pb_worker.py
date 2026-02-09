import time
import random
import re
import os
from curl_cffi import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# LINE Messaging API SDK
from linebot import LineBotApi
from linebot.models import TextSendMessage
from linebot.exceptions import LineBotApiError

# .envの読み込み
load_dotenv()

# --- 設定の読み込み ---
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
FIREBASE_KEY = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
APP_ID = os.getenv("APP_ID", "pb-watcher-app")
MY_ID = os.getenv("MY_LINE_USER_ID")

# Firebase初期化
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_KEY)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# LINE API初期化
line_bot_api = LineBotApi(LINE_TOKEN)


def send_line_push(to_user_id, message):
    """Messaging APIを使用してプッシュ通知を送信"""
    if not to_user_id or not LINE_TOKEN:
        print("⚠️ 送信先IDまたはトークンがありません")
        return
    try:
        line_bot_api.push_message(to_user_id, TextSendMessage(text=message))
        print(f"✅ LINE通知を送信しました: {to_user_id[:8]}...")
    except LineBotApiError as e:
        print(f"❌ LINE送信エラー: {e.status_code} - {e.message}")


def get_tasks():
    """Firestoreから全ユーザーの監視タスクを取得"""
    tasks = []
    users_ref = db.collection("artifacts").document(APP_ID).collection("users")
    for user_doc in users_ref.stream():
        uid = user_doc.id
        # ユーザー設定からLINE IDを取得 (App.jsx側で保存する想定)
        settings = (
            users_ref.document(uid).collection("profile").document("settings").get()
        )
        line_id = settings.to_dict().get("lineUserId") if settings.exists() else MY_ID

        # 監視リストを取得
        watchlist = users_ref.document(uid).collection("watchlist").stream()
        for item in watchlist:
            tasks.append(
                {
                    "ref": item.reference,
                    "url": item.to_dict().get("url"),
                    "line_id": line_id,
                    "prev_status": item.to_dict().get("lastStatus", ""),
                }
            )
    return tasks


def scrape_pb(url):
    """プレバンの在庫チェック"""
    try:
        resp = requests.get(url, impersonate="chrome120", timeout=15)
        if resp.status_code != 200:
            return "Error", False, 0

        html = resp.text
        stock_match = re.search(
            r'orderstock_list = \{.*?"(.*?)":"(.*?)"', html, re.DOTALL
        )
        max_match = re.search(r'ordermax_list = \{.*?"(.*?)":(\d+)', html, re.DOTALL)

        is_stock = stock_match.group(2) == "○" if stock_match else False
        qty = max_match.group(2) if max_match else "0"
        return "Success", is_stock, qty
    except Exception as e:
        print(f"Scrape Error: {e}")
        return "Exception", False, 0


def main():
    print("🚀 PB Watcher Messaging API Engine Started")
    # 起動テスト
    if MY_ID:
        send_line_push(MY_ID, "【システム】監視エンジンが起動しました。")

    while True:
        tasks = get_tasks()
        print(f"--- 巡回開始 ({len(tasks)}件) ---")

        for task in tasks:
            print(f"Checking: {task['url']}")
            res, is_stock, qty = scrape_pb(task["url"])

            if res == "Success":
                status_text = f"{'在庫あり' if is_stock else '在庫なし'}({qty})"

                # 在庫ありへの変化を検知
                if is_stock and "在庫あり" not in task["prev_status"]:
                    send_line_push(
                        task["line_id"], f"🔥在庫復活！\n最大{qty}個\n{task['url']}"
                    )

                # Firestore更新
                task["ref"].update(
                    {
                        "lastStatus": status_text,
                        "lastChecked": firestore.SERVER_TIMESTAMP,
                    }
                )

            time.sleep(random.randint(10, 20))  # BAN回避用

        wait = random.randint(300, 600)
        print(f"巡回終了。{wait // 60}分待機します...")
        time.sleep(wait)


if __name__ == "__main__":
    main()
