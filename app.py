import os
import json
import logging
import re
import urllib.parse
import unicodedata
from typing import Optional
from functools import wraps

# --- スクレイピング・データ処理用 ---
from bs4 import BeautifulSoup
import csv
import io
from datetime import datetime
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

import requests
import time

# curl_cffi をフォールバック付きでインポート（AppRunner環境対応）
try:
    from curl_cffi import requests as cffi_requests
    USE_CFFI = True
except ImportError:
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

# LINE SDK 初期化
line_bot_api = None
handler = None
if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    try:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        handler = WebhookHandler(LINE_CHANNEL_SECRET)
        logger.info("✅ LINE Bot SDK 初期化成功")
    except Exception as e:
        logger.warning(f"⚠️ LINE Bot SDK 初期化スキップ: {e}")

app = Flask(__name__)

AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")
APP_ID = os.getenv("APP_ID", "pb-stock-monitor-pro")
IS_PRODUCTION = os.getenv("FLASK_ENV") == "production" or os.getenv("ENV") == "production"

# ==========================
# Firebase 初期化 (堅牢化版)
# ==========================
db = None
try:
    if not firebase_admin._apps:
        cred_path = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            logger.info(f"✅ Firebase Admin SDK 連携成功 (File: {cred_path})")
        else:
            logger.warning(f"⚠️ Firebaseキーファイルが見つかりません: {cred_path}")
    db = firestore.client()
except Exception as e:
    logger.error(f"❌ Firebase初期化エラー（サーバー起動は継続）: {e}")

# ==========================
# Azure 初期化 (AppRunner互換)
# ==========================
project_client = None
agent = None

try:
    if AZURE_PROJECT_ENDPOINT and AGENT_ID:
        project_client = AIProjectClient(
            credential=DefaultAzureCredential(),
            endpoint=AZURE_PROJECT_ENDPOINT
        )
        # メソッド名変更に対応した取得ロジック
        for method_name in ["get_agent", "get"]:
            if hasattr(project_client.agents, method_name):
                try:
                    agent = getattr(project_client.agents, method_name)(AGENT_ID)
                    if agent:
                        logger.info(f"✅ Azure AI Agent 連携成功 (method: {method_name})")
                        break
                except: continue
    else:
        logger.warning("⚠️ Azure環境変数が不足しているためAI機能をスキップします")
except Exception as e:
    logger.error(f"❌ Azure初期化エラー（サーバー起動は継続）: {e}")

# ==========================
# 共通ユーティリティ
# ==========================
def http_get(url, timeout=15):
    """curl_cffi / requests 自動切替"""
    if USE_CFFI:
        return cffi_requests.get(url, impersonate="chrome120", timeout=timeout)
    else:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        return _requests.get(url, timeout=timeout, headers=headers)

def scrape_yahuoku_closed(raw_keyword, max_pages: int = 3):
    """
    ヤフオク落札相場取得 (最新のReactランダムクラス名に対応した最強堅牢版)
    """
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote(keyword)
        base_url = f"https://auctions.yahoo.co.jp/closedsearch/closedsearch?p={encoded}&n=100"

        visited = set()
        urls = [base_url]
        all_items = []

        page_count = 0
        while urls and page_count < max_pages:
            url = urls.pop(0)
            if url in visited: continue
            visited.add(url)
            page_count += 1

            if page_count > 1:
                time.sleep(1)

            try:
                res = requests.get(url, timeout=10)
            except Exception:
                res = http_get(url, timeout=15)

            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            
            # クラス名に依存せず、商品リンクから親ブロックをたどって情報を抽出する
            visited_item_urls = set()
            
            for a_tag in soup.find_all("a", href=re.compile(r"auctions\.yahoo\.co\.jp/jp/auction/[a-zA-Z0-9]+")):
                item_url = a_tag.get("href")
                if item_url in visited_item_urls:
                    continue
                
                # 親要素をたどって商品ブロック全体（テキストに「落札」「円」が含まれる塊）を見つける
                block = a_tag.parent
                is_valid_block = False
                for _ in range(10): # 最大10階層上まで
                    if not block: break
                    text = block.get_text(strip=True)
                    if "落札" in text and "円" in text and len(text) < 1000:
                        is_valid_block = True
                        break
                    block = block.parent
                
                if not is_valid_block or not block:
                    continue
                
                # テキストがくっつくのを防ぐために separator を入れる
                text = block.get_text(separator=" ", strip=True) 
                
                # 落札価格の抽出 (例: "落札 2,900 円" または "落札2,900円")
                price_match = re.search(r"落札\s*([\d,]+)\s*円", text)
                if not price_match:
                    price_match = re.search(r"落札.*?([\d,]+)\s*円", text)
                    
                if not price_match:
                    continue
                    
                price_int = int(price_match.group(1).replace(",", ""))
                
                # タイトルの抽出
                title = a_tag.get("title")
                if not title:
                    # このブロック内のすべてのテキストリンクを探す
                    title_a = block.find("a", href=item_url, string=True)
                    if title_a and title_a.text.strip():
                        title = title_a.text.strip()
                        
                img = block.find("img")
                if not title and img:
                    title = img.get("alt", "").strip()
                    
                if not title:
                    continue
                    
                image_url = img.get("src", "") if img else ""
                
                all_items.append({
                    "title": title,
                    "url": item_url,
                    "price": f"{price_int:,}円",
                    "raw_price": price_int,
                    "image": image_url,
                })
                visited_item_urls.add(item_url)

            # 「次へ」リンクの探索もテキストベースで
            try:
                nxt = soup.find("a", string=re.compile(r"次へ"))
                if nxt and "href" in nxt.attrs:
                    next_url = urllib.parse.urljoin("https://auctions.yahoo.co.jp", nxt.get("href", ""))
                    if next_url and next_url not in visited:
                        urls.append(next_url)
            except Exception:
                pass

        if not all_items:
            logger.info(f"⚠ 落札相場ゼロ件: keyword={keyword}")
            return None

        prices = [i["raw_price"] for i in all_items]
        logger.info(f"📊 落札サンプル数: {len(prices)}件 (keyword={keyword})")
        return {
            "max_price": f"{max(prices):,}",
            "avg_price": f"{sum(prices) // len(prices):,}",
            "sample_count": len(prices),
            "items": all_items,
        }
    except Exception as e:
        logger.error(f"Yahoo Scrape Error: {e}")
        return None

def scrape_yahuoku_active(raw_keyword):
    """ヤフオク現在開催中検索 (最新のReactランダムクラス名に対応した最強堅牢版)"""
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote(keyword)
        url = f"https://auctions.yahoo.co.jp/search/search?p={encoded}&n=50"
        res = http_get(url)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.text, "html.parser")
        
        items = []
        visited_item_urls = set()
        
        for a_tag in soup.find_all("a", href=re.compile(r"auctions\.yahoo\.co\.jp/jp/auction/[a-zA-Z0-9]+")):
            item_url = a_tag.get("href")
            if item_url in visited_item_urls:
                continue
            
            block = a_tag.parent
            is_valid_block = False
            for _ in range(10):
                if not block: break
                text = block.get_text(strip=True)
                # 開催中のブロックには「円」と「終了」または入札履歴リンクが含まれる
                if "円" in text and ("終了" in text or block.find("a", href=re.compile(r"bid_hist"))) and len(text) < 1000:
                    is_valid_block = True
                    break
                block = block.parent
                
            if not is_valid_block or not block:
                continue
                
            text = block.get_text(separator=" ", strip=True)
            
            # 価格の抽出 (最初に出現する「xxx円」を現在価格、もし「即決」があれば即決価格)
            prices = re.findall(r"([\d,]+)\s*円", text)
            if not prices:
                continue
                
            current_price = f"{prices[0]}円"
            buy_now_price = f"{prices[1]}円" if len(prices) > 1 and "即決" in text else None
            
            # 入札件数
            bids = "0"
            bid_link = block.find("a", href=re.compile(r"bid_hist"))
            if bid_link:
                bids_text = re.sub(r"\D", "", bid_link.get_text(strip=True))
                if bids_text: bids = bids_text
            else:
                m_bids = re.search(r"入札\s*(\d+)", text)
                if m_bids: bids = m_bids.group(1)
                
            # 終了時間
            end_time = "---"
            m_end = re.search(r"(\d{1,2}/\d{1,2}\s+\d{1,2}:\d{1,2})\s*終了", text)
            if m_end:
                end_time = m_end.group(1)
            else:
                m_left = re.search(r"残り\s*([0-9]+[日時間分]+)", text)
                if m_left: end_time = f"残り{m_left.group(1)}"

            # タイトルと画像
            title = a_tag.get("title")
            if not title:
                title_a = block.find("a", href=item_url, string=True)
                if title_a and title_a.text.strip():
                    title = title_a.text.strip()
                    
            img = block.find("img")
            if not title and img:
                title = img.get("alt", "").strip()
                
            if not title:
                continue
                
            image_url = img.get("src", "") if img else ""

            items.append({
                "title": title,
                "url": item_url,
                "price": current_price,
                "buy_now_price": buy_now_price,
                "image": image_url,
                "bids": bids,
                "end_time": end_time
            })
            visited_item_urls.add(item_url)
            
        return items
    except Exception as e:
        logger.error(f"Active Scrape Error: {e}")
        return []

def scrape_mercari_prices(raw_keyword: str):
    """メルカリ販売中価格取得"""
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote(keyword)
        url = f"https://jp.mercari.com/search?keyword={encoded}&status=on_sale&order=price_asc"
        res = http_get(url)
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
    except: return None

# ==========================
# API Routes
# ==========================
@app.route("/")
def index():
    firebase_config = {
        "apiKey": os.getenv("FIREBASE_API_KEY"),
        "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN"),
        "projectId": os.getenv("FIREBASE_PROJECT_ID"),
        "appId": os.getenv("FIREBASE_APP_ID"),
    }
    return render_template("index.html", config=firebase_config)

@app.route("/api/monitor", methods=["POST"])
@login_required
def api_monitor():
    url = request.json.get("url")
    res = scrape_premium_bandai(url)
    if not res: return jsonify({"error": "商品情報の取得に失敗しました"}), 500
    return jsonify({"preview": res})

@app.route("/api/watchlist", methods=["POST"])
@login_required
def api_add_watchlist():
    if not db: return jsonify({"error": "Firebaseが初期化されていません"}), 503
    data = request.json
    uid = request.user["uid"]
    db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("watchlist").add({
        **data, "createdAt": firestore.SERVER_TIMESTAMP, "updatedAt": firestore.SERVER_TIMESTAMP
    })
    return jsonify({"message": "OK"})

@app.route("/api/profit-check", methods=["POST"])
@login_required
def api_profit_check():
    data = request.get_json() or {}
    keyword = (data.get("keyword") or "").strip()
    s_sell = int(data.get("shipping_sell") or 600)
    s_buy = int(data.get("shipping_buy") or 600)
    manual_buy = data.get("manual_buy_price")
    yahoo_data = scrape_yahuoku_closed(keyword)
    if not yahoo_data: return jsonify({"error": "ヤフオク相場なし"}), 200
    
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
        verdict = "BUY" if profit > 2000 and roi > 15 else ("CONSIDER" if profit > 0 else "LOSS")
        label = "激アツ！" if verdict == "BUY" else ("検討あり" if verdict == "CONSIDER" else "対象外")
        return {
            "profit": profit, "roi": roi, "profit_rate": p_rate, "buy_price": buy_price, 
            "sell_price": s_val, "total_cost": total_cost, "yahoo_fee": fee,
            "verdict": verdict, "verdict_label": label
        }

    return jsonify({
        "keyword": keyword, "yahoo_data": yahoo_data, "mercari_data": mercari_data,
        "profit_avg": calc(yahoo_data["avg_price"]), "profit_max": calc(yahoo_data["max_price"])
    })

@app.route("/api/scout", methods=["POST"])
@login_required
def api_scout():
    keyword = request.json.get("keyword")
    market = scrape_yahuoku_closed(keyword)
    if not market: return jsonify({"error": "データなし"}), 200
    if not agent: return jsonify({"error": "AI機能が現在利用できません。環境変数を確認してください。"}), 503
    try:
        thread = project_client.agents.threads.create()
        prompt = f"「{keyword}」のヤフオク相場（平均{market['avg_price']}円）を元に鑑定。JSON形式で: target_buy_price, profitability, ai_advice"
        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        msgs = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
        for m in msgs:
            if m.role == "assistant" and m.text_messages:
                match = re.search(r"\{.*\}", m.text_messages[0].text.value, re.DOTALL)
                if match: return jsonify({"appraisal": json.loads(match.group()), "market_data": market, "thread_id": thread.id})
        return jsonify({"error": "AI解析失敗"}), 500
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/keyword-research", methods=["POST"])
@login_required
def api_keyword_research():
    seed = request.json.get("seed", "").strip()
    web_kws = []
    try:
        res = http_get(f"https://sugg.search.yahoo.co.jp/sg/?output=fxjson&command={urllib.parse.quote(seed)}", timeout=5)
        if res.status_code == 200: web_kws = [r[0] for r in res.json()[1]]
    except: pass
    return jsonify({"web": web_kws, "ai": [], "profit": [], "by_genre": {}})

@app.route("/api/recommendations", methods=["GET"])
@login_required
def api_recs():
    return jsonify({"recommendations": [
        {"title": "METAL BUILD エヴァンゲリオン初号機", "url": "https://p-bandai.jp/item/item-1000210000/", "reason": "再販需要高"},
        {"title": "CSM ファイズギア ver.2", "url": "https://p-bandai.jp/item/item-1000190000/", "reason": "固定ファン多"}
    ]})

@app.route("/api/test-notification", methods=["POST"])
@login_required
def api_test_notify():
    if not line_bot_api or not db: return jsonify({"error": "システム連携が不完全です"}), 503
    try:
        uid = request.user["uid"]
        snap = db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("settings").document("line").get()
        if not snap.exists(): return jsonify({"error": "LINE ID未設定"}), 400
        line_id = snap.data().get("lineUserId")
        line_bot_api.push_message(line_id, TextSendMessage(text="✅ 通知テスト成功"))
        return jsonify({"message": "OK"})
    except Exception as e: return jsonify({"error": str(e)}), 500

# ==========================
# 定期監視ジョブ (Scheduler)
# ==========================
# AppRunner (Gunicorn) 等で確実に起動するよう、外側で start させる
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")

def update_inventory_job():
    """30分おきに実行されるメインジョブ（実装予定）"""
    logger.info("🕒 定期更新ジョブ実行中...")
    pass

scheduler.add_job(
    func=update_inventory_job,
    trigger="interval",
    minutes=30,
    id='inventory_update_task',
    replace_existing=True
)

if not scheduler.running:
    scheduler.start()
    logger.info("🚀 バックグラウンドスケジューラ起動")

# ==========================
# 起動設定
# ==========================
if __name__ == "__main__":
    # ローカル実行用
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 ローカルサーバー起動 (Port: {port})")
    app.run(host="0.0.0.0", port=port, debug=False)