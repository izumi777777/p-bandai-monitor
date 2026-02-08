import os
import json
import logging
import re
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

# ==========================
# 初期設定
# ==========================
load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
MY_LINE_ID = os.getenv("MY_LINE_USER_ID")
FIREBASE_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
APP_ID = os.getenv("APP_ID", "pb-stock-monitor-pro")

# ==========================
# Firebase 初期化
# ==========================
db = None
if not firebase_admin._apps:
    try:
        # 環境変数にパスがない場合はデフォルト名を使用
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("✅ Firebase Admin SDK 連携成功")
    except Exception as e:
        logger.error(f"❌ Firebase初期化エラー: {e}")

# ==========================
# LINE / Azure 初期化
# ==========================
line_bot_api = LineBotApi(LINE_TOKEN) if LINE_TOKEN else None

# Azure AI Agent Client
project_client = AIProjectClient(
    credential=DefaultAzureCredential(),
    endpoint=AZURE_PROJECT_ENDPOINT
)
agent = project_client.agents.get_agent(AGENT_ID)

# ==========================
# Firebase Auth デコレータ
# ==========================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        token = auth_header.split("Bearer ")[1]
        try:
            # フロントエンドから送られてきたIDトークンを検証
            decoded = auth.verify_id_token(token)
            request.user = decoded
        except Exception as e:
            logger.error(f"❌ Token error: {e}")
            return jsonify({"error": "Invalid token"}), 401

        return f(*args, **kwargs)
    return wrapper

# ==========================
# 補助関数
# ==========================
def scrape_premium_bandai(url):
    """プレミアムバンダイのサイトをスクレイピングして基本情報を取得"""
    try:
        res = requests.get(url, impersonate="chrome120", timeout=15)
        if res.status_code != 200:
            return None

        html = res.text

        # タイトル・価格・在庫・画像を抽出
        title_match = re.search(r"<title>(.*?) \|", html)
        price_match = re.search(r"price: '(\d+)'", html)
        stock_match = re.search(r'orderstock_list = \{.*?"(.*?)":"(.*?)"', html, re.DOTALL)
        image_match = re.search(r'<meta property="og:image" content="(.*?)"', html)

        available = stock_match and stock_match.group(2) == "○"

        return {
            "title": title_match.group(1) if title_match else "不明な商品",
            "price": f"{price_match.group(1)}円" if price_match else "不明",
            "inStock": bool(available),
            "statusText": "在庫あり" if available else "在庫なし",
            "imageUrl": image_match.group(1) if image_match else None,
            "url": url
        }
    except Exception as e:
        logger.error(f"❌ Scrape error: {e}")
        return None

def get_stock_status_via_agent(url):
    """Azure AI Agent を使用して解析"""
    scraped = scrape_premium_bandai(url)
    if not scraped:
        return None, None

    thread = project_client.agents.threads.create()

    # エージェントへのプロンプト作成
    prompt = f"以下の商品情報を解析して、最終的な在庫状況をJSON形式で要約してください: {json.dumps(scraped, ensure_ascii=False)}"

    project_client.agents.messages.create(
        thread_id=thread.id,
        role="user",
        content=prompt
    )

    project_client.agents.runs.create_and_process(
        thread_id=thread.id,
        agent_id=agent.id
    )

    messages = project_client.agents.messages.list(
        thread_id=thread.id,
        order=ListSortOrder.DESCENDING
    )

    for m in messages:
        if m.role == "assistant" and m.text_messages:
            text = m.text_messages[0].text.value
            try:
                # エージェントの回答からJSON部分を抽出
                json_str = re.search(r"\{.*\}", text, re.DOTALL).group()
                return json.loads(json_str), thread.id
            except:
                # JSON抽出に失敗した場合は生テキストを返す
                return {"summary": text, **scraped}, thread.id

    return scraped, thread.id

# ==========================
# Routes
# ==========================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/monitor", methods=["POST"])
@login_required
def api_monitor():
    url = request.json.get("url")
    if not url:
        return jsonify({"error": "URL required"}), 400

    result, thread_id = get_stock_status_via_agent(url)
    if not result:
        return jsonify({"error": "解析失敗"}), 500

    return jsonify({
        "preview": result,
        "thread_id": thread_id
    })

@app.route("/api/watchlist", methods=["POST"])
@login_required
def api_watchlist_add():
    if not db:
        return jsonify({"error": "DB error"}), 500

    uid = request.user["uid"]
    data = request.json

    # 指定された構造 /artifacts/{appId}/users/{userId}/watchlist に保存
    try:
        doc_ref = db.collection("artifacts").document(APP_ID)\
          .collection("users").document(uid)\
          .collection("watchlist").add({
              **data,
              "createdAt": firestore.SERVER_TIMESTAMP,
              "lastChecked": firestore.SERVER_TIMESTAMP
          })
        return jsonify({"status": "ok", "id": doc_ref[1].id})
    except Exception as e:
        logger.error(f"❌ Firestore error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/query", methods=["POST"])
@login_required
def api_query():
    data = request.json
    thread_id = data.get("thread_id")
    query = data.get("query")

    if not thread_id or not query:
        return jsonify({"error": "invalid params"}), 400

    project_client.agents.messages.create(
        thread_id=thread_id,
        role="user",
        content=query
    )

    project_client.agents.runs.create_and_process(
        thread_id=thread_id,
        agent_id=agent.id
    )

    messages = project_client.agents.messages.list(
        thread_id=thread_id,
        order=ListSortOrder.DESCENDING
    )

    for m in messages:
        if m.role == "assistant" and m.text_messages:
            return jsonify({"reply": m.text_messages[0].text.value})

    return jsonify({"reply": "回答なし"})

# ==========================
# 起動
# ==========================
if __name__ == "__main__":
    app.run(debug=True, port=5000)