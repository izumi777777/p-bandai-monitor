import os
import json
import logging
import re
import urllib.parse
import unicodedata
from typing import Optional

# --- スクレイピング・データ処理用 ---
from bs4 import BeautifulSoup
import csv
import io
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template, abort, redirect, url_for

# --- LINE Messages API ---
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import LineBotApiError, InvalidSignatureError

# --- 定期監視用 ---
from apscheduler.schedulers.background import BackgroundScheduler

# --- Azure AI / Identity ---
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import ListSortOrder

# --- その他 ---
from dotenv import load_dotenv
from curl_cffi import requests
import firebase_admin
from firebase_admin import credentials, auth, firestore

load_dotenv()

# ==========================
# 基本設定と環境変数
# ==========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

# LINE SDK v2
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app = Flask(__name__)

AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")
APP_ID = os.getenv("APP_ID", "pb-stock-monitor-pro")
IS_PRODUCTION = os.getenv("FLASK_ENV") == "production" or os.getenv("ENV") == "production"

# ==========================
# Firebase 初期化
# ==========================
db = None
try:
    if not firebase_admin._apps:
        cred_path = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            logger.info(f"✅ Firebase Admin SDK 連携成功 (File: {cred_path})")
    db = firestore.client()
except Exception as e:
    logger.error(f"❌ Firebase初期化エラー: {e}")

# ==========================
# Azure 初期化
# ==========================
project_client = None
agent = None

try:
    if AZURE_PROJECT_ENDPOINT and AGENT_ID:
        project_client = AIProjectClient(
            credential=DefaultAzureCredential(), 
            endpoint=AZURE_PROJECT_ENDPOINT
        )
        agent = project_client.agents.get_agent(AGENT_ID)
        logger.info("✅ Azure AI Project 連携成功")
    else:
        logger.warning("⚠️ Azure関連の環境変数 (AZURE_PROJECT_ENDPOINT, AGENT_ID) が未設定です")
except Exception as e:
    logger.error(f"❌ Azure初期化エラー: {e}")

# ==========================
# 共通ユーティリティ
# ==========================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if os.getenv("SKIP_AUTH") == "true" and not IS_PRODUCTION:
            request.user = {"uid": "debug_user"}
            return f(*args, **kwargs)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        token = auth_header.split("Bearer ")[1]
        try:
            decoded = auth.verify_id_token(token)
            request.user = decoded
        except Exception as e:
            return jsonify({"error": f"Invalid token: {e}"}), 401
        return f(*args, **kwargs)
    return wrapper

def optimize_search_keyword(raw_keyword):
    if not raw_keyword: return ""
    keyword = unicodedata.normalize('NFKC', raw_keyword)
    keyword = re.sub(r'([a-zA-Z0-9])([^\x01-\x7E])', r'\1 \2', keyword)
    keyword = re.sub(r'([^\x01-\x7E])([a-zA-Z0-9])', r'\1 \2', keyword)
    return re.sub(r'\s+', ' ', keyword).strip()

def is_allowed_p_bandai_or_test_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if "/test-item" in parsed.path: return True
        return hostname == "p-bandai.jp" or hostname.endswith(".p-bandai.jp")
    except: return False

def is_allowed_yahoo_auction_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
        allowed = ["auctions.yahoo.co.jp", "page.auctions.yahoo.co.jp"]
        return any(hostname == h or hostname.endswith("." + h) for h in allowed)
    except: return False

# ==========================
# スクレイピング ロジック
# ==========================
def scrape_premium_bandai(url):
    try:
        if not is_allowed_p_bandai_or_test_url(url): return None
        res = requests.get(url, impersonate="chrome120", timeout=15)
        if res.status_code != 200: return None
        
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
        logger.error(f"❌ プレバンスクレイピングエラー: {e}")
        return None

def scrape_yahuoku_closed(raw_keyword):
    """ヤフオク落札相場取得"""
    try:
        keyword = optimize_search_keyword(raw_keyword)
        logger.info(f"🔍 検索キーワードを最適化: '{raw_keyword}' ➔ '{keyword}'")
        encoded = urllib.parse.quote(keyword)
        url = f"https://auctions.yahoo.co.jp/closedsearch/closedsearch?p={encoded}&n=50"
        
        res = requests.get(url, impersonate="chrome120", timeout=15)
        if res.status_code != 200:
            logger.error(f"❌ ヤフオクアクセス失敗: {res.status_code}")
            return None
        
        soup = BeautifulSoup(res.text, "html.parser")
        product_items = soup.find_all("li", class_="Product")
        logger.info(f"📊 取得件数: {len(product_items)}件")

        fetched = []
        for item in product_items:
            try:
                title_tag = item.find("a", class_="Product__titleLink")
                price_tag = item.find("span", class_="Product__priceValue")
                img_tag = item.find("img")
                
                if title_tag and price_tag:
                    p_str = price_tag.text.replace(',', '').replace('円', '').strip()
                    if p_str.isdigit():
                        item_url = urllib.parse.urljoin("https://auctions.yahoo.co.jp", title_tag.get("href", "#"))
                        fetched.append({
                            "title": title_tag.text.strip(),
                            "url": item_url,
                            "price": f"{int(p_str):,}円",
                            "raw_price": int(p_str),
                            "image": img_tag.get("src", "") if img_tag else ""
                        })
            except: continue
        
        if not fetched:
            logger.warning("⚠️ 条件に合致する落札データが見つかりませんでした。")
            return None

        prices = [i["raw_price"] for i in fetched]
        return {
            "max_price": f"{max(prices):,}",
            "avg_price": f"{sum(prices) // len(prices):,}",
            "sample_count": len(prices),
            "items": fetched  # 詳細データをリストに含めて返す
        }
    except Exception as e:
        logger.error(f"❌ ヤフオク相場取得エラー: {e}")
        return None

def scrape_yahuoku_active(raw_keyword):
    """ヤフオク開催中オークション取得"""
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote(keyword)
        url = f"https://auctions.yahoo.co.jp/search/search?p={encoded}&n=50"
        res = requests.get(url, impersonate="chrome120", timeout=15)
        if res.status_code != 200: return None
            
        soup = BeautifulSoup(res.text, "html.parser")
        product_items = soup.find_all("li", class_="Product")
        
        items = []
        for item in product_items:
            try:
                title_tag = item.find("a", class_="Product__titleLink")
                price_tag = item.find("span", class_="Product__priceValue")
                img_tag = item.find("img")
                if title_tag and price_tag:
                    items.append({
                        "title": title_tag.text.strip(),
                        "url": urllib.parse.urljoin("https://auctions.yahoo.co.jp", title_tag.get("href", "")),
                        "price": price_tag.text.strip(),
                        "image": img_tag.get("src", "") if img_tag else ""
                    })
            except: continue
        return items
    except Exception as e:
        logger.error(f"❌ ヤフオク開催中取得エラー: {e}")
        return None

def scrape_mercari_prices(raw_keyword: str):
    """メルカリ販売中価格取得"""
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote(keyword)
        url = f"https://jp.mercari.com/search?keyword={encoded}&status=on_sale&order=price_asc"
        res = requests.get(url, impersonate="chrome120", timeout=15)
        if res.status_code != 200: return None
        
        soup = BeautifulSoup(res.text, "html.parser")
        price_tags = soup.find_all(attrs={"data-testid": "price"})
        prices = []
        for tag in price_tags:
            p_str = re.sub(r"\D", "", tag.text)
            if p_str: prices.append(int(p_str))
        
        if not prices: return None
        return {"min_price": min(prices), "avg_price": sum(prices) // len(prices)}
    except Exception as e:
        logger.error(f"❌ メルカリスクレイピングエラー: {e}")
        return None

# ==========================
# API Routes
# ==========================
@app.route("/")
def index():
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
    if not url: return jsonify({"error": "URL未指定"}), 400
    res = scrape_premium_bandai(url)
    if not res: return jsonify({"error": "取得失敗"}), 500
    return jsonify({"preview": res})

@app.route("/api/profit-check", methods=["POST"])
@login_required
def api_profit_check():
    data = request.get_json() or {}
    keyword = data.get("keyword", "").strip()
    manual_buy = data.get("manual_buy_price")
    ship_sell = int(data.get("shipping_sell") or 600)
    ship_buy = int(data.get("shipping_buy") or 600)

    yahoo = scrape_yahuoku_closed(keyword)
    if not yahoo: return jsonify({"error": "ヤフオク相場なし"}), 404

    buy_price = 0
    mercari_data = None
    if manual_buy is not None:
        buy_price = int(manual_buy)
    else:
        mercari_data = scrape_mercari_prices(keyword)
        if not mercari_data: return jsonify({"error": "仕入れ価格不明", "yahoo_data": yahoo}), 404
        buy_price = mercari_data["min_price"]

    def calc(sell):
        s_val = int(sell.replace(",", ""))
        fee = int(s_val * 0.088)
        profit = s_val - (buy_price + ship_buy + ship_sell + fee)
        roi = round((profit / (buy_price + ship_buy + ship_sell + fee)) * 100, 1) if buy_price > 0 else 0
        return {"profit": profit, "roi": roi, "sell": s_val}

    return jsonify({
        "keyword": keyword,
        "yahoo_data": yahoo,
        "mercari_data": mercari_data,
        "profit_avg": calc(yahoo["avg_price"]),
        "profit_max": calc(yahoo["max_price"])
    })

@app.route("/api/scout", methods=["POST"])
@login_required
def api_scout_item():
    keyword = request.json.get("keyword")
    logger.info(f"🔎 AI鑑定開始: {keyword}")
    
    market_data = scrape_yahuoku_closed(keyword)
    if not market_data:
        return jsonify({"error": "ヤフオクの落札相場データが見つかりませんでした。キーワードを具体的にしてみてください。"}), 404

    if not agent or not project_client:
        return jsonify({"error": "AI機能が現在利用できません"}), 500

    try:
        thread = project_client.agents.threads.create()
        # プロンプトをより構造化し、厳格に
        prompt = f"""
        あなたはプロの古物商鑑定士です。
        以下のヤフオク相場データを分析し、日本国内のフリマアプリ等での仕入れ判断を行ってください。
        
        【対象商品】: {keyword}
        【ヤフオク平均落札額】: {market_data['avg_price']}円
        【最高落札額】: {market_data['max_price']}円
        【データ件数】: {market_data['sample_count']}件
        
        以下のJSONフォーマットのみで回答してください。Markdownの装飾(```jsonなど)や解説文は一切含めないでください。
        {{
          "target_buy_price": "推奨される仕入れ上限価格(数値と単位)",
          "profitability": "利益期待度(A, B, Cのいずれか)",
          "ai_advice": "具体的な仕入れ・検品時のアドバイス(150文字程度)"
        }}
        """
        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        
        msgs = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
        for m in msgs:
            if m.role == "assistant" and m.text_messages:
                text = m.text_messages[0].text.value
                logger.info(f"🤖 AI Response: {text}")
                
                # JSON部分を抽出 (より寛容な正規表現)
                match = re.search(r"\{[\s\S]*\}", text)
                if match:
                    try:
                        res = json.loads(match.group())
                        # キーの揺らぎを吸収するためのマッピング
                        appraisal = {
                            "target_buy_price": str(res.get("target_buy_price") or res.get("推奨仕入れ価格") or "---"),
                            "profitability": str(res.get("profitability") or res.get("利益期待度") or "C"),
                            "ai_advice": str(res.get("ai_advice") or res.get("アドバイス") or "鑑定アドバイスを取得できませんでした。")
                        }
                        return jsonify({"appraisal": appraisal, "market_data": market_data, "thread_id": thread.id})
                    except Exception as parse_e:
                        logger.error(f"❌ JSONパースエラー: {parse_e}")
                        
        return jsonify({"error": "AIからの適切な回答を解析できませんでした。"}), 500
    except Exception as e:
        logger.error(f"❌ AI鑑定プロセスエラー: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/auctions/active", methods=["POST"])
@login_required
def api_auctions_active():
    keyword = request.json.get("keyword")
    res = scrape_yahuoku_active(keyword)
    if not res: return jsonify({"error": "開催中オークションなし"}), 404
    return jsonify({"items": res})

@app.route("/api/watchlist", methods=["POST"])
@login_required
def api_watchlist_add():
    uid = request.user["uid"]
    db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("watchlist").add(request.json)
    return jsonify({"status": "ok"})

# LINE Webhook
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"Your ID: {event.source.user_id}"))

# ==========================
# 起動
# ==========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
    scheduler.start()
    
    logger.info(f"🚀 サーバー起動準備完了 (Port: {port})")
    app.run(host="0.0.0.0", port=port, debug=False)