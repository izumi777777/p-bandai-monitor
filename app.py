import os
import json
import logging
import re
import time
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template

# Azure SDK
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import ListSortOrder
from dotenv import load_dotenv
from curl_cffi import requests

# LINE Messaging API SDK
from linebot import LineBotApi
from linebot.models import TextSendMessage
from linebot.exceptions import LineBotApiError

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, auth, firestore

# .envの読み込み
load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 設定の読み込み ---
AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
MY_LINE_ID = os.getenv("MY_LINE_USER_ID")
FIREBASE_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
APP_ID = os.getenv("APP_ID", "pb-stock-monitor-pro")

# --- 初期化処理 ---

# Firebase初期化
if not firebase_admin._apps:
    try:
        # サービスアカウントキーを使用して初期化
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("✅ Firebase Admin SDK 連携成功")
    except Exception as e:
        logger.error(f"❌ Firebase初期化エラー: {e}")
        db = None

# LINE API初期化
line_bot_api = LineBotApi(LINE_TOKEN) if LINE_TOKEN else None

# Azure Agent初期化
# DefaultAzureCredentialは環境変数（AZURE_TENANT_ID等）から認証情報を取得します
project_client = AIProjectClient(credential=DefaultAzureCredential(), endpoint=AZURE_PROJECT_ENDPOINT)
agent = project_client.agents.get_agent(AGENT_ID)

# ==========================
# 0. 認証用デコレータ (Firebase Auth)
# ==========================

def login_required(f):
    """
    Firebase IDトークンを検証するデコレータ
    フロントエンドのfetchリクエストで 'Authorization: Bearer <ID_TOKEN>' を要求します
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        id_token = None
        auth_header = request.headers.get("Authorization")
        
        if auth_header and auth_header.startswith("Bearer "):
            id_token = auth_header.split("Bearer ")[1]
        
        if not id_token:
            return jsonify({"error": "Unauthorized: No token provided"}), 401
        
        try:
            # トークンの検証。有効期限や署名をチェックします
            decoded_token = auth.verify_id_token(id_token)
            # ユーザー情報をリクエストオブジェクトに格納（エンドポイント内でuidを利用可能にする）
            request.user = decoded_token
        except Exception as e:
            logger.error(f"❌ Token Verification Error: {e}")
            return jsonify({"error": "Unauthorized: Invalid token"}), 401
        
        return f(*args, **kwargs)
    return decorated_function

# ==========================
# 1. 外部サービス連携ロジック
# ==========================

def send_line_notification(to_user_id, message):
    """LINE Messaging API経由で通知送信"""
    if not line_bot_api or not to_user_id:
        return
    try:
        line_bot_api.push_message(to_user_id, TextSendMessage(text=message))
        logger.info(f"✅ LINE通知送信完了")
    except LineBotApiError as e:
        logger.error(f"❌ LINE送信エラー: {e.message}")

def scrape_premium_bandai(url):
    """プレミアムバンダイのページを解析して基本情報を抽出"""
    try:
        # curl_cffiを使用してブラウザの挙動を模倣
        response = requests.get(url, impersonate="chrome120", timeout=15)
        if response.status_code != 200: return None
        html = response.text

        # 正規表現による簡易パース
        title_match = re.search(r'<title>(.*?) \|', html)
        product_name = title_match.group(1) if title_match else "不明な商品"

        price_match = re.search(r"price: '(\d+)'", html)
        price = price_match.group(1) if price_match else "不明"

        img_match = re.search(r'"0000000000_img":"(.*?)"', html)
        img_url = img_match.group(1) if img_match else None

        # 在庫フラグの抽出
        stock_match = re.search(r'orderstock_list = \{.*?"(.*?)":"(.*?)"', html, re.DOTALL)
        available = (stock_match and stock_match.group(2) == "○")

        max_match = re.search(r'ordermax_list = \{.*?"(.*?)":(\d+)', html, re.DOTALL)
        max_qty = max_match.group(2) if max_match else "0"

        return {
            "product_name": product_name,
            "price": f"{price}円",
            "available": available,
            "max_qty": max_qty,
            "image_url": img_url,
            "raw_status": "在庫あり" if available else "在庫なし"
        }
    except Exception as e:
        logger.error(f"❌ Scraping Error: {e}")
        return None

# ==========================
# 2. Azure AI Agent 処理
# ==========================

def get_stock_status_via_agent(url: str):
    """スクレイピングデータをAI Agentに渡し、構造化された回答を得る"""
    scraped_data = scrape_premium_bandai(url)
    if not scraped_data: return None, None

    try:
        # スレッドの作成
        thread = project_client.agents.threads.create()
        
        prompt = f"""
        以下の情報を読み取り、指定のJSON形式で返答してください。
        - 商品名: {scraped_data['product_name']}
        - 価格: {scraped_data['price']}
        - 在庫: {scraped_data['raw_status']}
        - 最大数: {scraped_data['max_qty']}
        - 画像: {scraped_data['image_url']}

        回答はJSONブロックのみとし、以下のキーを含めてください:
        {{
          "調査日時": "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
          "available": {str(scraped_data['available']).lower()},
          "商品名": "{scraped_data['product_name']}",
          "価格（税込）": "{scraped_data['price']}",
          "現在のステータス": "{scraped_data['raw_status']}",
          "最大在庫数": "{scraped_data['max_qty']}",
          "商品画像": "{scraped_data['image_url']}",
          "商品URL": "{url}"
        }}
        """

        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)

        # 最新のメッセージを取得
        messages = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
        raw_text = ""
        for message in messages:
            if message.role == "assistant" and message.text_messages:
                raw_text = message.text_messages[0].text.value
                break

        # JSONの抽出
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group()), thread.id
        
        return None, None
    except Exception as e:
        logger.error(f"❌ Azure Agent Error: {e}")
        return None, None

# --- Flask Routes ---

@app.route("/")
def index():
    """メイン画面のレンダリング"""
    return render_template("index.html")

@app.route("/api/monitor", methods=["POST"])
@login_required
def monitor_item():
    """URLを解析し、情報をFirestoreに保存（要認証）"""
    data = request.json
    url = data.get("url")
    uid = request.user['uid'] # ログインユーザーのID
    line_id = data.get("line_id", MY_LINE_ID)
    
    if not url:
        return jsonify({"error": "URLが必要です"}), 400

    result, thread_id = get_stock_status_via_agent(url)
    if not result:
        return jsonify({"error": "解析に失敗しました"}), 500

    # Firestoreへの保存 (規定のパス構造に従う)
    # パス: /artifacts/{APP_ID}/users/{uid}/history/{doc_id}
    if db:
        try:
            history_ref = db.collection('artifacts').document(APP_ID)\
                           .collection('users').document(uid)\
                           .collection('history')
            
            history_ref.add({
                "product_name": result.get("商品名"),
                "url": url,
                "status": result.get("現在のステータス"),
                "available": result.get("available"),
                "image_url": result.get("商品画像"),
                "createdAt": firestore.SERVER_TIMESTAMP
            })
        except Exception as e:
            logger.error(f"❌ Firestore Save Error: {e}")

    # 在庫検知時のLINE通知
    if result.get("available") and line_id:
        notification_msg = f"🔔【在庫検知】\n{result.get('商品名')}\nステータス: {result.get('現在のステータス')}\n{url}"
        send_line_notification(line_id, notification_msg)

    return jsonify({
        "item_name": result.get("商品名"),
        "status": result.get("現在のステータス"),
        "available": result.get("available"),
        "image_url": result.get("商品画像"),
        "thread_id": thread_id,
        "result": result
    })

@app.route("/api/query", methods=["POST"])
@login_required
def query_agent():
    """Agentに対する追加質問エンドポイント（要認証）"""
    data = request.json or {}
    thread_id = data.get("thread_id")
    user_query = data.get("query")

    if not thread_id or not user_query:
        return jsonify({"error": "Thread IDと質問内容が必要です"}), 400

    try:
        project_client.agents.messages.create(thread_id=thread_id, role="user", content=user_query)
        project_client.agents.runs.create_and_process(thread_id=thread_id, agent_id=agent.id)
        
        messages = project_client.agents.messages.list(thread_id=thread_id, order=ListSortOrder.DESCENDING)
        reply_text = ""
        for message in messages:
            if message.role == "assistant" and message.text_messages:
                reply_text = message.text_messages[0].text.value
                break

        return jsonify({"reply": reply_text or "回答を生成できませんでした。"})
    except Exception as e:
        logger.error(f"❌ Query Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # 本番環境では環境変数からポートを取得するか、WSGIサーバーを使用してください
    app.run(debug=True, port=5000)