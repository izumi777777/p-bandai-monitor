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

# curl_cffi をフォールバック付きでインポート（AppRunner環境対応）
try:
    from curl_cffi import requests as cffi_requests
    USE_CFFI = True
except ImportError:
    import requests as _requests
    USE_CFFI = False

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

# LINE SDK: 環境変数未設定時のクラッシュを防ぐ
line_bot_api = None
handler = None
if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    try:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        handler = WebhookHandler(LINE_CHANNEL_SECRET)
        logger.info("✅ LINE Bot SDK 初期化成功")
    except Exception as e:
        logger.warning(f"⚠️ LINE Bot SDK 初期化スキップ: {e}")
else:
    logger.warning("⚠️ LINE環境変数が未設定のためLINE Bot SDK をスキップ")

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

# 環境変数（新API用）
AZURE_SUBSCRIPTION_ID = os.getenv("AZURE_SUBSCRIPTION_ID")
AZURE_RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP")
AZURE_PROJECT_NAME = os.getenv("AZURE_PROJECT_NAME")

try:
    if AGENT_ID:
        credential = DefaultAzureCredential()

        # 新API (azure-ai-projects >= 1.0.0b9 以降):
        #   AIProjectClient(credential, subscription_id, resource_group_name, project_name)
        # 旧API:
        #   AIProjectClient(credential=..., endpoint=...)
        # 両方試みて成功した方を使う
        if AZURE_SUBSCRIPTION_ID and AZURE_RESOURCE_GROUP and AZURE_PROJECT_NAME:
            try:
                project_client = AIProjectClient(
                    credential=credential,
                    subscription_id=AZURE_SUBSCRIPTION_ID,
                    resource_group_name=AZURE_RESOURCE_GROUP,
                    project_name=AZURE_PROJECT_NAME
                )
                logger.info("✅ Azure AI Project 初期化成功 (新API: subscription_id方式)")
            except Exception as e1:
                logger.warning(f"⚠️ 新API初期化失敗、旧APIにフォールバック: {e1}")
                if AZURE_PROJECT_ENDPOINT:
                    project_client = AIProjectClient(
                        credential=credential,
                        endpoint=AZURE_PROJECT_ENDPOINT
                    )
                    logger.info("✅ Azure AI Project 初期化成功 (旧API: endpoint方式)")
        elif AZURE_PROJECT_ENDPOINT:
            project_client = AIProjectClient(
                credential=credential,
                endpoint=AZURE_PROJECT_ENDPOINT
            )
            logger.info("✅ Azure AI Project 初期化成功 (旧API: endpoint方式)")

        # Agentの取得（メソッド名の揺らぎを吸収）
        if project_client:
            for _method in ["get_agent", "get"]:
                try:
                    if hasattr(project_client.agents, _method):
                        agent = getattr(project_client.agents, _method)(AGENT_ID)
                        if agent:
                            logger.info(f"✅ Azure Agent取得成功 (method: {_method})")
                            break
                except Exception as _e:
                    logger.warning(f"⚠️ agents.{_method}() 失敗: {_e}")
            if not agent:
                logger.warning("⚠️ Azure Agent取得できず。AIなしで起動します。")
    else:
        logger.warning("⚠️ Azure環境変数が未設定のためAIをスキップ")
except Exception as e:
    # ここで例外をキャッチしてgunicornのクラッシュを防ぐ
    logger.error(f"❌ Azure初期化エラー（サーバーは継続起動）: {e}")
    project_client = None
    agent = None

# ==========================
# 共通ユーティリティ
# ==========================
def http_get(url, timeout=15):
    """curl_cffi / requests フォールバック付きGET"""
    if USE_CFFI:
        return cffi_requests.get(url, impersonate="chrome120", timeout=timeout)
    else:
        return _requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

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

# ==========================
# スクレイピング ロジック
# ==========================
def scrape_yahuoku_closed(raw_keyword):
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote(keyword)
        url = f"https://auctions.yahoo.co.jp/closedsearch/closedsearch?p={encoded}&n=50"
        res = http_get(url, timeout=15)
        if res.status_code != 200: return None
        soup = BeautifulSoup(res.text, "html.parser")
        product_items = soup.find_all("li", class_="Product")
        fetched = []
        for item in product_items:
            try:
                title_tag = item.find("a", class_="Product__titleLink")
                price_tag = item.find("span", class_="Product__priceValue")
                img_tag = item.find("img")
                if title_tag and price_tag:
                    p_str = price_tag.text.replace(',', '').replace('円', '').strip()
                    if p_str.isdigit():
                        fetched.append({
                            "title": title_tag.text.strip(),
                            "url": urllib.parse.urljoin("https://auctions.yahoo.co.jp", title_tag.get("href", "#")),
                            "price": f"{int(p_str):,}円",
                            "raw_price": int(p_str),
                            "image": img_tag.get("src", "") if img_tag else ""
                        })
            except: continue
        if not fetched: return None
        prices = [i["raw_price"] for i in fetched]
        return {
            "max_price": f"{max(prices):,}",
            "avg_price": f"{sum(prices) // len(prices):,}",
            "sample_count": len(prices),
            "items": fetched
        }
    except Exception as e:
        logger.error(f"Yahoo Scrape Error: {e}")
        return None

def scrape_yahuoku_active(raw_keyword):
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote(keyword)
        url = f"https://auctions.yahoo.co.jp/search/search?p={encoded}&n=50"
        res = http_get(url, timeout=15)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.text, "html.parser")
        product_items = soup.find_all("li", class_="Product")
        items = []
        for item in product_items:
            try:
                title_tag = item.find("a", class_="Product__titleLink")
                price_tag = item.find("span", class_="Product__priceValue")
                img_tag = item.find("img")
                bids_tag = item.find("a", class_="Product__bid")
                time_tag = item.find("span", class_="Product__time")
                if title_tag and price_tag:
                    items.append({
                        "title": title_tag.text.strip(),
                        "url": urllib.parse.urljoin("https://auctions.yahoo.co.jp", title_tag.get("href", "#")),
                        "price": price_tag.text.strip(),
                        "image": img_tag.get("src", "") if img_tag else "",
                        "bids": bids_tag.text.strip() if bids_tag else "0",
                        "end_time": time_tag.text.strip() if time_tag else "---"
                    })
            except: continue
        return items
    except Exception as e:
        return []

def scrape_mercari_prices(raw_keyword: str):
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote(keyword)
        url = f"https://jp.mercari.com/search?keyword={encoded}&status=on_sale&order=price_asc"
        res = http_get(url, timeout=15)
        if res.status_code != 200: return None
        soup = BeautifulSoup(res.text, "html.parser")
        items_fetched = []
        price_tags = soup.find_all(attrs={"data-testid": "price"})
        if not price_tags: price_tags = soup.find_all("span", string=re.compile(r"¥|￥"))
        for i, tag in enumerate(price_tags):
            p_val = re.sub(r"\D", "", tag.text)
            if p_val:
                p_item = tag.find_parent("a")
                items_fetched.append({
                    "title": f"メルカリ商品 {i+1}",
                    "price": p_val,
                    "url": urllib.parse.urljoin("https://jp.mercari.com", p_item.get("href", "#")) if p_item else ""
                })
        if not items_fetched: return None
        prices = [int(i["price"]) for i in items_fetched]
        return {
            "min_price": min(prices),
            "avg_price": sum(prices) // len(prices),
            "sample_count": len(prices),
            "items": items_fetched
        }
    except Exception as e:
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
        "appId": os.getenv("FIREBASE_APP_ID")
    }
    return render_template("index.html", config=firebase_config)

@app.route("/api/profit-check", methods=["POST"])
@login_required
def api_profit_check():
    try:
        data = request.get_json() or {}
        keyword = (data.get("keyword") or "").strip()
        s_sell = int(data.get("shipping_sell") or 600)
        s_buy = int(data.get("shipping_buy") or 600)
        manual_buy = data.get("manual_buy_price")

        yahoo_data = scrape_yahuoku_closed(keyword)
        if not yahoo_data:
            return jsonify({"error": "ヤフオクで落札データが見つかりませんでした。キーワードを具体的にしてください。"}), 200

        mercari_data = None
        buy_price = 0
        if manual_buy is not None and str(manual_buy).isdigit():
            buy_price = int(manual_buy)
        else:
            mercari_data = scrape_mercari_prices(keyword)
            buy_price = mercari_data["min_price"] if mercari_data else 0

        def calc(sell_price_str):
            s_val = int(sell_price_str.replace(",", ""))
            fee = int(s_val * 0.088)
            total_cost = buy_price + s_buy + s_sell + fee
            profit = s_val - total_cost
            roi = round((profit / total_cost) * 100, 1) if total_cost > 0 else 0
            p_rate = round((profit / s_val) * 100, 1) if s_val > 0 else 0
            verdict = "LOSS"
            label = "仕入れ対象外"
            if buy_price > 0:
                if profit > 2000 and roi > 15:
                    verdict = "BUY"
                    label = "激アツ！仕入れ推奨"
                elif profit > 0:
                    verdict = "CONSIDER"
                    label = "検討の余地あり"
            else:
                label = "仕入れ価格不明"
            return {
                "profit": profit, "roi": roi, "profit_rate": p_rate,
                "buy_price": buy_price, "sell_price": s_val, "total_cost": total_cost,
                "shipping_buy": s_buy, "shipping_sell": s_sell, "yahoo_fee": fee,
                "verdict": verdict, "verdict_label": label
            }

        return jsonify({
            "keyword": keyword,
            "yahoo_data": yahoo_data,
            "mercari_data": mercari_data,
            "profit_avg": calc(yahoo_data["avg_price"]),
            "profit_max": calc(yahoo_data["max_price"])
        })
    except Exception as e:
        logger.error(f"Profit Check API Error: {e}")
        return jsonify({"error": f"内部エラーが発生しました: {str(e)}"}), 500

@app.route("/api/scout", methods=["POST"])
@login_required
def api_scout():
    try:
        keyword = request.json.get("keyword")
        market = scrape_yahuoku_closed(keyword)
        if not market: return jsonify({"error": "相場データが見つかりません"}), 200
        if not agent: return jsonify({"error": "AI Agentが初期化されていません"}), 503
        thread = project_client.agents.threads.create()
        prompt = f"「{keyword}」のヤフオク相場（平均{market['avg_price']}円）を元に鑑定して。JSON形式で回答。キー: target_buy_price, profitability, ai_advice"
        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        msgs = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
        for m in msgs:
            if m.role == "assistant" and m.text_messages:
                match = re.search(r"\{.*\}", m.text_messages[0].text.value, re.DOTALL)
                if match:
                    appraisal = json.loads(match.group())
                    return jsonify({"appraisal": appraisal, "market_data": market, "thread_id": thread.id})
        return jsonify({"error": "AI解析に失敗しました"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/scout/followup", methods=["POST"])
@login_required
def api_scout_followup():
    tid = request.json.get("thread_id")
    msg = request.json.get("message")
    try:
        project_client.agents.messages.create(thread_id=tid, role="user", content=msg)
        project_client.agents.runs.create_and_process(thread_id=tid, agent_id=agent.id)
        msgs = project_client.agents.messages.list(thread_id=tid, order=ListSortOrder.DESCENDING)
        for m in msgs:
            if m.role == "assistant" and m.text_messages:
                return jsonify({"answer": m.text_messages[0].text.value})
        return jsonify({"answer": "応答が得られませんでした"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auctions/active", methods=["POST"])
@login_required
def api_auctions_active():
    keyword = request.json.get("keyword")
    items = scrape_yahuoku_active(keyword)
    return jsonify({"items": items})

@app.route("/api/keyword-research", methods=["POST"])
@login_required
def api_keyword_research():
    try:
        seed = request.json.get("seed", "").strip()
        by_genre = request.json.get("by_genre", False)
        focus_profit = request.json.get("focus_profit", False)

        # 1. Yahooサジェスト取得
        web_kws = []
        try:
            res = http_get(
                f"https://sugg.search.yahoo.co.jp/sg/?output=fxjson&command={urllib.parse.quote(seed)}",
                timeout=5
            )
            if res.status_code == 200:
                web_kws = [r[0] for r in res.json()[1]]
        except: pass

        # 2. AIによる深掘り
        ai_kws = []
        genre_data = {}
        if agent:
            try:
                thread = project_client.agents.threads.create()
                prompt = f"「{seed}」に関連する、ヤフオクやメルカリで需要の高いキーワードを10個挙げてください。"
                if by_genre:
                    prompt += "ジャンル名: [キーワードリスト] のJSON形式(オブジェクト)で分類して。"
                else:
                    prompt += "JSON配列 ['kw1', 'kw2'...] 形式で出して。"
                project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
                project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
                msgs = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
                # ✅ 修正箇所: msgs はイテラブル。直接 .text_messages にアクセスするとエラーになる
                for m in msgs:
                    if m.role == "assistant" and m.text_messages:
                        text = m.text_messages[0].text.value
                        if by_genre:
                            match = re.search(r"\{.*\}", text, re.DOTALL)
                            if match: genre_data = json.loads(match.group())
                        else:
                            match = re.search(r"\[.*\]", text, re.DOTALL)
                            if match: ai_kws = json.loads(match.group())
                        break
            except Exception as e:
                logger.warning(f"AI keyword research failed: {e}")

        return jsonify({
            "web": web_kws,
            "ai": ai_kws,
            "profit": ai_kws if focus_profit else [],
            "by_genre": genre_data
        })
    except Exception as e:
        logger.error(f"keyword-research error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/keyword-research/seeds", methods=["POST"])
@login_required
def api_keyword_seeds():
    try:
        if not agent:
            return jsonify({"seeds": ["ガンプラ", "CSM", "ポケカ", "メタルビルド"]})
        thread = project_client.agents.threads.create()
        project_client.agents.messages.create(
            thread_id=thread.id, role="user",
            content="今の日本でせどりで稼ぎやすいジャンルや商品を15個、JSON配列 ['品名', '品名'...] 形式のみで出力して。"
        )
        project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        msgs = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
        for m in msgs:
            if m.role == "assistant" and m.text_messages:
                match = re.search(r"\[.*\]", m.text_messages[0].text.value, re.DOTALL)
                if match:
                    return jsonify({"seeds": json.loads(match.group())})
        return jsonify({"seeds": ["ガンプラ", "CSM", "ポケカ", "メタルビルド"]})
    except:
        return jsonify({"seeds": ["ガンプラ", "CSM", "ポケカ", "メタルビルド"]})

@app.route("/api/recommendations", methods=["GET"])
@login_required
def api_recs():
    return jsonify({"recommendations": [
        {"title": "METAL BUILD エヴァンゲリオン初号機", "url": "https://p-bandai.jp/item/item-1000210000/", "reason": "再販期待度が高く、中古相場も安定しています。"},
        {"title": "CSM ファイズギア ver.2", "url": "https://p-bandai.jp/item/item-1000190000/", "reason": "人気が高く、在庫復活時の争奪戦が予想されます。"}
    ]})

@app.route("/api/test-notification", methods=["POST"])
@login_required
def api_test_notify():
    try:
        if not line_bot_api:
            return jsonify({"error": "LINE Bot SDKが初期化されていません"}), 503
        uid = request.user["uid"]
        snap = db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("settings").document("line").get()
        if not snap.exists(): return jsonify({"error": "LINE IDが未設定です"}), 400
        line_id = snap.data().get("lineUserId")
        line_bot_api.push_message(line_id, TextSendMessage(text="✅ システム接続テスト完了\n通知設定は正常です。"))
        return jsonify({"message": "OK"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/watchlist", methods=["POST"])
@login_required
def api_add_watchlist():
    try:
        data = request.json
        uid = request.user["uid"]
        db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("watchlist").add({
            **data,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP
        })
        return jsonify({"message": "OK"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==========================
# APScheduler 起動 (gunicorn対応)
# gunicornは if __name__ ブロックを実行しないため、モジュールレベルで起動する。
# AppRunnerのデフォルトはworker=1なので多重起動は通常起きない。
# 万が一マルチワーカー構成にする場合は DISABLE_SCHEDULER=true で無効化すること。
# ==========================
if os.getenv("DISABLE_SCHEDULER") != "true":
    try:
        _scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
        # TODO: ジョブをここに追加する
        # _scheduler.add_job(check_watchlist_job, 'interval', minutes=5)
        _scheduler.start()
        logger.info("✅ APScheduler 起動完了")
    except Exception as e:
        logger.error(f"❌ APScheduler 起動失敗: {e}")

# ==========================
# 起動 (flask直接実行時のみ)
# ==========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 サーバー起動 (Port: {port})")
    app.run(host="0.0.0.0", port=port, debug=False)