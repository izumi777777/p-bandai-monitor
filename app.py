import os
import json
import logging
import re

# CSV調査対象URL追加用ライブラリ
import csv
import io

from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template, abort
# LINE MessagesAPI
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import LineBotApiError, InvalidSignatureError


# -------- 定期監視機能のために追加 --------------------------------
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")

# Azure SDK
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import ListSortOrder

from dotenv import load_dotenv
load_dotenv()

from curl_cffi import requests

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, auth, firestore

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

# ==========================
# 初期設定
# ==========================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
FIREBASE_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
APP_ID = os.getenv("APP_ID", "pb-stock-monitor-pro")

# ==========================
# Firebase 初期化
# ==========================
db = None
try:
    if not firebase_admin._apps:
        # ファイルパスを環境変数から取得（デフォルトは "service-account-key.json"）
        cred_path = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        logger.info(f"✅ Firebase Admin SDK 連携成功 (File: {cred_path})")
    db = firestore.client()
except Exception as e:
    logger.error(f"❌ Firebase初期化エラー: {e}")


# ==========================
# Azure 初期化
# ==========================
# DefaultAzureCredentialはローカル環境では Azure CLI 等でのログインが必要です
project_client = AIProjectClient(
    credential=DefaultAzureCredential(), endpoint=AZURE_PROJECT_ENDPOINT
)
agent = project_client.agents.get_agent(AGENT_ID)


# ==========================
# 認証デコレータ (修正版)
# ==========================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 開発用: 環境変数で認証をスキップできるように設定可能
        if os.getenv("SKIP_AUTH") == "true":
            request.user = {"uid": "debug_user"}
            return f(*args, **kwargs)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            logger.warning("⚠️ 認証ヘッダーが不足しています")
            return jsonify({"error": "Unauthorized: No token provided"}), 401

        token = auth_header.split("Bearer ")[1]
        try:
            decoded = auth.verify_id_token(token)
            request.user = decoded
        except Exception as e:
            logger.error(f"❌ トンクン検証エラー: {e}")
            return jsonify({"error": f"Invalid token: {str(e)}"}), 401

        return f(*args, **kwargs)

    return wrapper


# ==========================
# ロジック関数
# ==========================
def scrape_premium_bandai(url):
    try:
        # プレミアムバンダイのBot対策を回避するために impersonate を使用
        res = requests.get(url, impersonate="chrome120", timeout=15)
        if res.status_code != 200:
            logger.error(f"❌ サイトアクセス失敗: {res.status_code}")
            return None

        html = res.text
        title = re.search(r"<title>(.*?) \|", html)
        price = re.search(r"price: '(\d+)'", html)
        stock = re.search(r'orderstock_list = \{.*?"(.*?)":"(.*?)"', html, re.DOTALL)
        image = re.search(r'<meta property="og:image" content="(.*?)"', html)

        available = stock and stock.group(2) == "○"

        return {
            "title": title.group(1) if title else "不明な商品",
            "price": f"{price.group(1)}円" if price else "---",
            "inStock": bool(available),
            "statusText": "在庫あり" if available else "在庫なし",
            "imageUrl": image.group(1) if image else None,
            "url": url,
        }
    except Exception as e:
        logger.error(f"❌ スクレイピングエラー: {e}")
        return None


def get_stock_status_via_agent(url):
    scraped = scrape_premium_bandai(url)
    if not scraped:
        return None, None

    # Azure AI Agent のスレッド作成
    thread = project_client.agents.threads.create()

    # 解析依頼
    prompt = f"以下の商品情報を解析してJSONで返してください。特に在庫が復活しているか判断してください: {json.dumps(scraped, ensure_ascii=False)}"

    project_client.agents.messages.create(
        thread_id=thread.id, role="user", content=prompt
    )

    project_client.agents.runs.create_and_process(
        thread_id=thread.id, agent_id=agent.id
    )

    messages = project_client.agents.messages.list(
        thread_id=thread.id, order=ListSortOrder.DESCENDING
    )

    for m in messages:
        if m.role == "assistant" and m.text_messages:
            text = m.text_messages[0].text.value
            try:
                # エージェントが返したテキストからJSON部分を抽出
                match = re.search(r"\{.*\}", text, re.DOTALL)
                if match:
                    return json.loads(match.group()), thread.id
            except:
                pass
            return {**scraped, "agent_comment": text}, thread.id

    return scraped, thread.id


# ==========================
# API Routes
# ==========================
@app.route("/")
def index():
    # Secrets Managerから取得した、または環境変数にある値を渡す
    firebase_config = {
        "apiKey": os.getenv("FIREBASE_API_KEY"),
        "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN"),
        "projectId": os.getenv("FIREBASE_PROJECT_ID"),
        "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET"),
        "messagingSenderId": os.getenv("FIREBASE_MESSAGING_SENDER_ID"),
        "appId": os.getenv("FIREBASE_APP_ID"),
    }
    return render_template("index.html", config=firebase_config)


@app.route("/api/monitor", methods=["POST"])
@login_required
def api_monitor():
    url = request.json.get("url")
    if not url:
        return jsonify({"error": "URLが指定されていません"}), 400

    logger.info(f"🔍 調査開始: {url}")
    result, thread_id = get_stock_status_via_agent(url)

    if not result:
        return jsonify({"error": "商品情報の取得に失敗しました。URLを確認してください。"}), 500

    return jsonify({"preview": result, "thread_id": thread_id})


@app.route("/api/watchlist", methods=["POST"])
@login_required
def api_watchlist_add():
    if not db:
        return jsonify({"error": "データベースに接続できません"}), 500

    uid = request.user["uid"]
    data = request.json

    try:
        # パス規則: /artifacts/{appId}/users/{userId}/watchlist
        db.collection("artifacts").document(APP_ID).collection("users").document(
            uid
        ).collection("watchlist").add(
            {
                **data,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "lastChecked": firestore.SERVER_TIMESTAMP,
            }
        )
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    

# ==========================
# CSV 一括登録エンドポイント (新規追加)
# ==========================
@app.route("/api/watchlist/csv", methods=["POST"])
@login_required
def api_watchlist_csv():
    if not db:
        return jsonify({"error": "データベースに接続できません"}), 500

    # 1. ファイルチェック
    if 'file' not in request.files:
        return jsonify({"error": "ファイルが送信されていません"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "ファイルが選択されていません"}), 400

    # 2. CSV読み込みとバリデーション
    try:
        # バイナリデータをテキストとして読み込む
        stream = io.StringIO(file.stream.read().decode("utf-8"), newline=None)
        csv_input = csv.DictReader(stream)
        
        # リスト化して件数チェック
        rows = list(csv_input)
        
        if len(rows) > 5:
            return jsonify({"error": "一度に登録できるのは最大5件までです"}), 400
        
        if not rows:
             return jsonify({"error": "CSVデータが空です"}), 400
             
        # ヘッダーチェック (BOM付きUTF-8対策で、キーの中に'url'が含まれるか探す)
        header_check = any("url" in key.lower() for key in rows[0].keys())
        if not header_check:
            return jsonify({"error": "CSVの一行目に 'url' という列が必要です"}), 400

    except Exception as e:
        return jsonify({"error": f"CSV解析エラー: {str(e)}"}), 400

    # 3. ループ処理
    uid = request.user["uid"]
    results = {
        "success": [],
        "errors": []
    }
    
    # ユーザーのコレクション参照
    watchlist_ref = db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("watchlist")

    for index, row in enumerate(rows):
        # キーの揺らぎ吸収（'URL', 'url ' などに対応）
        url = None
        for k, v in row.items():
            if k.strip().lower() == "url":
                url = v.strip()
                break
        
        if not url:
            results["errors"].append(f"{index+1}行目: URLが見つかりません")
            continue

        # プレバンURLか簡易チェック
        if "p-bandai.jp" not in url:
            results["errors"].append(f"{index+1}行目: プレミアムバンダイのURLではありません")
            continue

        # スクレイピング実行 (AIは使わず高速に)
        scraped = scrape_premium_bandai(url)
        
        if scraped:
            try:
                watchlist_ref.add({
                    "url": url,
                    "title": scraped["title"],
                    "price": scraped["price"],
                    "imageUrl": scraped["imageUrl"],
                    "inStock": scraped["inStock"],
                    "statusText": scraped["statusText"],
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "lastChecked": firestore.SERVER_TIMESTAMP,
                    "lastNotifiedStatus": scraped["inStock"]
                })
                results["success"].append(scraped["title"])
            except Exception as e:
                results["errors"].append(f"{index+1}行目: DB保存エラー {str(e)}")
        else:
            results["errors"].append(f"{index+1}行目: 商品情報の取得に失敗しました")

    return jsonify({
        "message": f"{len(results['success'])}件 登録しました",
        "results": results
    })


# =======================================================================================
# LINE通知機能
# =======================================================================================
def send_line_notification(line_user_id: str, message: str):
    if not LINE_TOKEN or not line_user_id:
        logger.warning("⚠️ LINE通知スキップ（設定不足）")
        return

    try:
        line_bot_api = LineBotApi(LINE_TOKEN)
        line_bot_api.push_message(
            line_user_id,
            TextSendMessage(text=message),
        )
        logger.info("✅ LINE通知送信完了")
    except LineBotApiError as e:
        logger.error(f"❌ LINE送信エラー: {e}")


# =======================================================================================
# LINE通知テスト機能(非本番向け)
# =======================================================================================
@app.route("/api/test-notification", methods=["POST"])
@login_required
def api_test_notification():
    if not db:
        return jsonify({"error": "DB not initialized"}), 500

    uid = request.user["uid"]

    # LINE設定取得
    line_doc = (
        db.collection("artifacts")
        .document(APP_ID)
        .collection("users")
        .document(uid)
        .collection("settings")
        .document("line")
        .get()
    )

    if not line_doc.exists:
        return jsonify({"error": "LINE USER ID が未設定です"}), 400

    line_user_id = line_doc.to_dict().get("lineUserId")
    if not line_user_id:
        return jsonify({"error": "LINE USER ID が不正です"}), 400

    # テスト通知送信
    message = """🧪 テスト通知
PB Stock Monitor Pro です。

このメッセージが届いていれば、
LINE通知設定は正常に動作しています 👍
"""

    send_line_notification(line_user_id, message)

    return jsonify({"status": "ok"})

# ========================================================
# Webhook エンドポイント
# ========================================================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ========================================================
#  自動返信ロジック: User ID を返却する 
# ========================================================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    
    user_id = event.source.user_id
                            
    # ユーザーに送るメッセージを作成
    reply_text = (
                   f"あなたの LINE User ID はこちらです：\n\n"
                   f"{user_id}\n\n"
                   f"この値をコピーしてアプリの設定画面に貼り付けてください。"
    )
                                        
    # LINEで返信
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
            )
    except Exception as e:
        app.logger.error(f"Error sending reply: {e}")

# ========================================================================================
# 監視ジョブ本体
# ========================================================================================
def check_watchlist_job():
    logger.info("⏰ 在庫監視ジョブ開始")

    users_ref = db.collection("artifacts").document(APP_ID).collection("users")
    for user_doc in users_ref.stream():
        uid = user_doc.id

        # LINE設定取得
        line_ref = users_ref.document(uid).collection("settings").document("line").get()
        if not line_ref.exists:
            continue

        line_user_id = line_ref.to_dict().get("lineUserId")
        if not line_user_id:
            continue

        watchlist_ref = users_ref.document(uid).collection("watchlist")
        for item_doc in watchlist_ref.stream():
            item = item_doc.to_dict()
            url = item.get("url")

            scraped = scrape_premium_bandai(url)
            if not scraped:
                continue

            prev_status = item.get("inStock", False)
            current_status = scraped["inStock"]

            # 状態変化チェック
            if prev_status != current_status:
                logger.info(f"🔔 在庫変化検知: {item.get('title')}")

                # Firestore 更新
                item_doc.reference.update(
                    {
                        "inStock": current_status,
                        "statusText": scraped["statusText"],
                        "lastChecked": firestore.SERVER_TIMESTAMP,
                        "lastNotifiedStatus": current_status,
                    }
                )

                # LINE 通知
                msg = f"""📦 在庫変動通知
{item.get("title")}
状態: {scraped["statusText"]}
{url}"""
                send_line_notification(line_user_id, msg)


# ==========================
# 起動
# ==========================
if __name__ == "__main__":
    import os

    # 環境変数PORTがあればそれを使う（App Runner用）
    # なければ8080を使う（ローカル・EC2テスト用）
    port = int(os.environ.get("PORT", 8080))

    scheduler.add_job(
        check_watchlist_job,
        trigger="interval",
        minutes=10,
        id="watchlist_checker",
        replace_existing=True,
    )
    scheduler.start()
    # 開発環境でVSCodeなどから実行する場合
    app.run(host="0.0.0.0", port=port, debug=False)