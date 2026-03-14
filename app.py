import os
import json
import logging
import re
import urllib.parse
import unicodedata

# --- ヤフオクスクレイピング用 ---
from bs4 import BeautifulSoup

# CSV調査対象URL追加用ライブラリ
import csv
import io

from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template, abort, redirect, url_for
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

# 環境判定（本番かどうか）
IS_PRODUCTION = os.getenv("FLASK_ENV") == "production" or os.getenv("ENV") == "production"

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
# 認証デコレータ
# ==========================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 開発用: 環境変数で認証をスキップできるように設定可能（※本番では無効）
        if os.getenv("SKIP_AUTH") == "true":
            if IS_PRODUCTION:
                logger.warning("⚠️ 本番環境で SKIP_AUTH が有効になっていますが無視されました")
            else:
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
def is_allowed_p_bandai_or_test_url(url: str) -> bool:
    """
    プレミアムバンダイ or テスト用URL(/test-item) かどうかを厳格にチェック
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    hostname = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if "/test-item" in path:
        return True

    if hostname == "p-bandai.jp" or hostname.endswith(".p-bandai.jp"):
        return True

    return False


def is_allowed_yahoo_auction_url(url: str) -> bool:
    """
    ヤフオク個別ページURLかどうかを厳格にチェック
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False

    allowed_hosts = [
        "auctions.yahoo.co.jp",
        "page.auctions.yahoo.co.jp",
    ]
    if hostname in allowed_hosts or any(
        hostname.endswith("." + h) for h in allowed_hosts
    ):
        return True

    return False


def scrape_premium_bandai(url):
    try:
        if not is_allowed_p_bandai_or_test_url(url):
            logger.warning(f"⚠️ 許可されていないURLへのアクセス試行がブロックされました: {url}")
            return None

        res = requests.get(
            url,
            impersonate="chrome120",
            timeout=15,
            allow_redirects=False,
        )

        if res.status_code in (301, 302, 303, 307, 308):
            redirect_url = res.headers.get("Location")
            if redirect_url:
                redirect_url = urllib.parse.urljoin(url, redirect_url)
                if is_allowed_p_bandai_or_test_url(redirect_url):
                    logger.info(f"↪️ プレバンURLリダイレクト検知: {url} -> {redirect_url}")
                    res = requests.get(
                        redirect_url,
                        impersonate="chrome120",
                        timeout=15,
                        allow_redirects=False,
                    )
                    url = redirect_url
                else:
                    logger.warning(f"⚠️ ホワイトリスト外へのリダイレクトをブロック: {redirect_url}")
                    return None

        if res.status_code != 200:
            logger.warning(f"⚠️ サイトアクセス失敗: {res.status_code}")
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

    thread = project_client.agents.threads.create()
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
                match = re.search(r"\{.*\}", text, re.DOTALL)
                if match:
                    return json.loads(match.group()), thread.id
            except:
                pass
            return {**scraped, "agent_comment": text}, thread.id

    return scraped, thread.id


# ======================================================================
# ヤフオク・ラクマ検索キーワード最適化
# ======================================================================
def optimize_search_keyword(raw_keyword):
    """
    ユーザーの入力をヤフオク/ラクマでヒットしやすい「あいまい検索」用に最適化する
    """
    keyword = unicodedata.normalize('NFKC', raw_keyword)
    keyword = re.sub(r'([a-zA-Z0-9])([^\x01-\x7E])', r'\1 \2', keyword)
    keyword = re.sub(r'([^\x01-\x7E])([a-zA-Z0-9])', r'\1 \2', keyword)
    keyword = re.sub(r'\s+', ' ', keyword).strip()
    return keyword


# =======================================================================================
# ヤフオク落札相場取得 (__NEXT_DATA__ JSON ベース版)
# =======================================================================================
def _extract_next_data(html: str):
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            return None
    return None

def _get_listing(data: dict) -> dict:
    try:
        return data["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]
    except (KeyError, TypeError):
        return {}

def _build_closed_url(keyword: str, offset: int = 1, page_size: int = 50) -> str:
    params = {"p": keyword, "b": offset, "n": page_size}
    return "https://auctions.yahoo.co.jp/closedsearch/closedsearch?" + urllib.parse.urlencode(params)

def _fetch_closed_page(keyword: str, offset: int, page_size: int) -> tuple:
    url = _build_closed_url(keyword, offset, page_size)
    logger.info(f"📄 落札相場取得中: {url}")

    res = requests.get(url, impersonate="chrome120", timeout=15)
    if res.status_code != 200:
        logger.warning(f"⚠️ HTTP {res.status_code}")
        return [], 0

    data = _extract_next_data(res.text)
    if not data:
        logger.warning("⚠️ __NEXT_DATA__ が見つかりませんでした")
        return [], 0

    listing = _get_listing(data)
    items = listing.get("items", [])
    total = listing.get("totalResultsAvailable", 0)

    rows = []
    for item in items:
        try:
            auction_id = item.get("auctionId", "")
            title = (item.get("title") or "").strip()
            price = item.get("price")
            end_time = item.get("endTime", "")

            if not auction_id or not title or price is None:
                continue

            price_int = int(price)
            image_url = item.get("imageUrl", "")
            rows.append({
                "title": title,
                "url": f"https://page.auctions.yahoo.co.jp/jp/auction/{auction_id}",
                "price": f"{price_int:,}円",
                "raw_price": price_int,
                "end_time": end_time,
                "image": image_url,
            })
        except Exception:
            continue
    return rows, total

def scrape_yahuoku_closed(raw_keyword: str, max_pages: int = 3):
    try:
        keyword = optimize_search_keyword(raw_keyword)
        logger.info(f"🔍 検索キーワードを最適化: '{raw_keyword}' ➔ '{keyword}'")

        def collect(search_kw: str) -> list:
            all_items = []
            offset = 1
            page_size = 50
            total_available = None
            for _ in range(max_pages):
                rows, total = _fetch_closed_page(search_kw, offset, page_size)
                if total_available is None and total > 0:
                    total_available = total
                    logger.info(f"📊 総落札件数: {total_available:,} 件 (keyword={search_kw})")
                if not rows:
                    break
                all_items.extend(rows)
                offset += page_size
                if total_available and offset > total_available:
                    break
            return all_items

        items = collect(keyword)

        if not items and " " in keyword:
            looser_keyword = " ".join(keyword.split(" ")[:-1])
            logger.info(f"⚠️ ヒットなし。条件を緩めて再検索します: '{looser_keyword}'")
            items = collect(looser_keyword)

        if not items:
            logger.info(f"⚠️ 落札相場ゼロ件: keyword={keyword}")
            return None

        prices = [i["raw_price"] for i in items]
        logger.info(f"📊 落札サンプル数: {len(prices)} 件 (keyword={keyword})")
        return {
            "max_price": f"{max(prices):,}",
            "avg_price": f"{sum(prices) // len(prices):,}",
            "sample_count": len(prices),
            "items": items,
        }

    except Exception as e:
        logger.error(f"❌ ヤフオクスクレイピングエラー: {e}")
        return None


# ========================================================
# 開催中オークション取得処理
# ========================================================
def scrape_yahuoku_active(raw_keyword):
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded_keyword = urllib.parse.quote(keyword)
        url = f"https://auctions.yahoo.co.jp/search/search?p={encoded_keyword}&n=50"
        
        res = requests.get(url, impersonate="chrome120", timeout=15)
        if res.status_code != 200:
            logger.error(f"❌ ヤフオクアクセス失敗: {res.status_code}")
            return None
            
        soup = BeautifulSoup(res.text, "html.parser")
        product_items = soup.find_all("li", class_="Product")
        
        items = []
        for item in product_items:
            try:
                title_tag = item.find("a", class_="Product__titleLink")
                price_tag = item.find("span", class_="Product__priceValue")
                img_tag = item.find("img")
                
                buy_now_tag = item.find("span", class_="Product__priceValue Product__priceValue--buyNow")
                buy_now_price = buy_now_tag.text.strip() if buy_now_tag else None

                bid_tag = item.find(class_=re.compile(r"Product__bid"))
                bids = bid_tag.text.strip() if bid_tag else "0"

                time_tag = item.find(class_=re.compile(r"Product__time"))
                end_time = time_tag.text.strip() if time_tag else "-"

                if title_tag and price_tag:
                    title = title_tag.text.strip()
                    item_url = urllib.parse.urljoin(
                        "https://auctions.yahoo.co.jp",
                        title_tag.get("href", "#"),
                    )
                    price_str = price_tag.text.strip()
                    img_url = img_tag.get("src", "") if img_tag else ""
                    
                    items.append({
                        "title": title,
                        "url": item_url,
                        "price": price_str,
                        "buy_now_price": buy_now_price,
                        "image": img_url,
                        "bids": bids,
                        "end_time": end_time
                    })
            except Exception as e:
                continue

        if not items:
            return None
        return items
        
    except Exception as e:
        logger.error(f"❌ 開催中オークション取得エラー: {e}")
        return None

# ========================================================
# ラクマ(fril.jp) 検索価格取得処理 (新規追加・テスト済)
# ========================================================
def scrape_rakuma_prices(raw_keyword: str):
    """
    ラクマ(fril.jp)販売中価格取得
    """
    try:
        keyword = optimize_search_keyword(raw_keyword)
        encoded = urllib.parse.quote_plus(keyword)
        url = f"https://fril.jp/s?query={encoded}"

        logger.info(f"🛍️ ラクマ取得中: {url}")
        
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer":         "https://fril.jp/",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        
        res = requests.get(url, impersonate="chrome120", timeout=30, headers=headers)

        if res.status_code != 200:
            logger.warning(f"⚠️ ラクマ HTTP {res.status_code}")
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        items_fetched = []

        # ① item カード方式
        cards = soup.find_all("li", class_=re.compile(r"item"))
        if not cards:
            cards = soup.find_all("div", class_=re.compile(r"item.box|item-box|ItemBox"))
        if not cards:
            cards = soup.find_all(attrs={"data-price": True})

        logger.info(f"🔍 ラクマ カード数: {len(cards)}")

        for card in cards[:50]:
            try:
                price_str = ""
                if card.get("data-price"):
                    price_str = re.sub(r"\D", "", card["data-price"])
                if not price_str:
                    p_tag = card.find(class_=re.compile(r"price", re.I))
                    if p_tag:
                        price_str = re.sub(r"\D", "", p_tag.get_text())
                if not price_str:
                    for t in card.find_all(string=re.compile(r"[¥￥][\d,]+")):
                        price_str = re.sub(r"\D", "", t)
                        if price_str:
                            break
                if not price_str or not price_str.isdigit():
                    continue

                title = ""
                img_url = ""
                t_tag = card.find(class_=re.compile(r"name|title", re.I))
                title = t_tag.get_text(strip=True) if t_tag else ""
                img_tag = card.find("img")
                if img_tag:
                    img_url = img_tag.get("src") or img_tag.get("data-src") or img_tag.get("data-lazy-src") or ""
                if not title:
                    title = img_tag.get("alt", "") if img_tag else ""
                
                a_tag = card.find("a", href=True)
                item_url = urllib.parse.urljoin("https://fril.jp", a_tag["href"]) if a_tag else ""
                
                items_fetched.append({"title": title or "ラクマ商品", "price": price_str, "url": item_url, "image": img_url})
            except Exception:
                continue

        # ② フォールバック
        if not items_fetched:
            logger.info("🔄 ラクマ 最終フォールバック実行中...")
            for tag in soup.find_all(string=re.compile(r"[¥￥][\d,]{3,}")):
                p_val = re.sub(r"\D", "", tag)
                if p_val and p_val.isdigit() and 100 <= int(p_val) <= 9_999_999:
                    parent_a = tag.find_parent("a")
                    parent_img = tag.find_parent(lambda t: t.find("img"))
                    fb_img = ""
                    if parent_img:
                        fi = parent_img.find("img")
                        fb_img = fi.get("src") or fi.get("data-src") or "" if fi else ""
                    items_fetched.append({
                        "title": "ラクマ商品",
                        "price": p_val,
                        "url": urllib.parse.urljoin(
                            "https://fril.jp",
                            parent_a["href"] if parent_a and parent_a.get("href") else ""
                        ),
                        "image": fb_img,
                    })
                    if len(items_fetched) >= 30:
                        break

        if not items_fetched:
            logger.warning(f"⚠️ ラクマ価格ゼロ件: keyword={keyword}")
            return None

        prices = [int(i["price"]) for i in items_fetched if i["price"].isdigit()]
        if not prices:
            return None

        logger.info(f"📊 ラクマ取得完了: {len(prices)}件 min={min(prices):,}円 (keyword={keyword})")
        return {
            "min_price":    min(prices),
            "avg_price":    sum(prices) // len(prices),
            "sample_count": len(prices),
            "items":        items_fetched[:20]
        }
    except Exception as e:
        logger.error(f"Rakuma Scrape Error: {e}")
        return None

# ==========================
# API Routes
# ==========================

# --------------------------------------------------------
# 画像プロキシ（fril.jpなど Referer 制限があるサイト向け）
# --------------------------------------------------------
ALLOWED_IMAGE_HOSTS = {
    "fril.jp",
    "img.fril.jp",
    "static.fril.jp",
    "auctions.c.yimg.jp",
    "item-shopping.c.yimg.jp",
    "aucview.aucfan.com",
}

@app.route("/api/proxy-image")
def proxy_image():
    """
    クエリパラメータ ?url=<画像URL> を受け取り、
    サーバーサイドで画像を取得してクライアントに転送する。
    許可ホスト以外はブロックする。
    """
    from flask import Response
    img_url = request.args.get("url", "").strip()
    if not img_url:
        return ("url parameter required", 400)

    try:
        parsed = urllib.parse.urlparse(img_url)
    except Exception:
        return ("invalid url", 400)

    if parsed.scheme not in ("http", "https"):
        return ("invalid scheme", 400)

    hostname = (parsed.hostname or "").lower()
    # サブドメインも含めて許可ホストチェック
    allowed = any(
        hostname == h or hostname.endswith("." + h)
        for h in ALLOWED_IMAGE_HOSTS
    )
    if not allowed:
        logger.warning(f"⚠️ 画像プロキシ: 許可外ホスト {hostname}")
        return ("host not allowed", 403)

    try:
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        img_res = requests.get(
            img_url,
            impersonate="chrome120",
            timeout=10,
            headers={
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        if img_res.status_code != 200:
            return (f"upstream {img_res.status_code}", 502)

        content_type = img_res.headers.get("Content-Type", "image/jpeg")
        return Response(
            img_res.content,
            status=200,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "public, max-age=3600",
            },
        )
    except Exception as e:
        logger.error(f"❌ 画像プロキシエラー: {e}")
        return ("fetch error", 502)


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
    if not url:
        return jsonify({"error": "URLが指定されていません"}), 400

    logger.info(f"🔍 調査開始: {url}")
    result, thread_id = get_stock_status_via_agent(url)

    if not result:
        return jsonify({"error": "商品情報の取得に失敗しました。URLを確認してください。"}), 500

    return jsonify({"preview": result, "thread_id": thread_id})


# ========================================================
# 利ザヤチェッカー API (新規追加)
# ========================================================
@app.route("/api/profit-check", methods=["POST"])
@login_required
def api_profit_check():
    try:
        data = request.get_json() or {}
        keyword = (data.get("keyword") or "").strip()
        if not keyword:
            return jsonify({"error": "キーワードが指定されていません"}), 400

        s_sell     = int(data.get("shipping_sell") or 600)
        s_buy      = int(data.get("shipping_buy")  or 600)
        manual_buy = data.get("manual_buy_price")

        yahoo_data = scrape_yahuoku_closed(keyword)
        if not yahoo_data:
            return jsonify({"error": "ヤフオクで落札データが見つかりませんでした。"}), 404

        rakuma_data = None
        buy_price = 0
        if manual_buy is not None and str(manual_buy).lstrip('-').isdigit():
            buy_price = int(manual_buy)
        else:
            rakuma_data = scrape_rakuma_prices(keyword)
            buy_price = rakuma_data["min_price"] if rakuma_data else 0

        def calc(sell_price_str: str) -> dict:
            s_val      = int(str(sell_price_str).replace(",", ""))
            fee        = int(s_val * 0.088) # ヤフオク手数料 8.8%計算
            total_cost = buy_price + s_buy + s_sell + fee
            profit     = s_val - total_cost
            roi        = round((profit / total_cost) * 100, 1) if total_cost > 0 else 0
            p_rate     = round((profit / s_val)      * 100, 1) if s_val      > 0 else 0
            
            if buy_price <= 0:
                verdict, label = "UNKNOWN", "仕入れ価格不明"
            elif profit > 2000 and roi > 15:
                verdict, label = "BUY",     "激アツ！仕入れ推奨"
            elif profit > 0:
                verdict, label = "CONSIDER", "検討の余地あり"
            else:
                verdict, label = "LOSS",    "仕入れ対象外"
                
            return {
                "profit": profit, "roi": roi, "profit_rate": p_rate,
                "buy_price": buy_price, "sell_price": s_val,
                "total_cost": total_cost, "yahoo_fee": fee,
                "verdict": verdict, "verdict_label": label
            }

        return jsonify({
            "keyword":      keyword,
            "yahoo_data":   yahoo_data,
            "rakuma_data": rakuma_data,
            "profit_avg":   calc(yahoo_data["avg_price"]),
            "profit_max":   calc(yahoo_data["max_price"])
        })
    except Exception as e:
        logger.error(f"Profit Check API Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist", methods=["POST"])
@login_required
def api_watchlist_add():
    if not db:
        return jsonify({"error": "データベースに接続できません"}), 500
    uid = request.user["uid"]
    data = request.json
    try:
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
    

@app.route("/api/watchlist/csv", methods=["POST"])
@login_required
def api_watchlist_csv():
    if not db:
        return jsonify({"error": "データベースに接続できません"}), 500
    if 'file' not in request.files:
        return jsonify({"error": "ファイルが送信されていません"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "ファイルが選択されていません"}), 400
    try:
        stream = io.StringIO(file.stream.read().decode("utf-8"), newline=None)
        csv_input = csv.DictReader(stream)
        rows = list(csv_input)
        if len(rows) > 5:
            return jsonify({"error": "一度に登録できるのは最大5件までです"}), 400
        if not rows:
             return jsonify({"error": "CSVデータが空です"}), 400
        header_check = any("url" in key.lower() for key in rows[0].keys())
        if not header_check:
            return jsonify({"error": "CSVの一行目に 'url' という列が必要です"}), 400
    except Exception as e:
        return jsonify({"error": f"CSV解析エラー: {str(e)}"}), 400

    uid = request.user["uid"]
    results = {"success": [], "errors": []}
    watchlist_ref = db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("watchlist")

    for index, row in enumerate(rows):
        url = None
        for k, v in row.items():
            if k.strip().lower() == "url":
                url = v.strip()
                break
        if not url:
            results["errors"].append(f"{index+1}行目: URLが見つかりません")
            continue
        if not is_allowed_p_bandai_or_test_url(url):
            results["errors"].append(f"{index+1}行目: 対象外のURLです")
            continue

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

@app.route("/api/auctions/active", methods=["POST"])
@login_required
def api_auctions_active():
    keyword = request.json.get("keyword")
    if not keyword:
        return jsonify({"error": "検索キーワードを入力してください"}), 400

    results = scrape_yahuoku_active(keyword)
    if results is None:
        return jsonify({"error": "現在開催中のオークションは見つかりませんでした"}), 404

    return jsonify({"items": results})

@app.route("/api/scout", methods=["POST"])
@login_required
def api_scout_item():
    keyword = request.json.get("keyword")
    if not keyword:
        return jsonify({"error": "検索キーワードが指定されていません"}), 400

    logger.info(f"🔎 AI鑑定開始: {keyword}")

    market_data = scrape_yahuoku_closed(keyword)
    if not market_data:
        return jsonify({"error": "ヤフオクの落札相場データが見つかりませんでした。別のキーワードをお試しください。"}), 404

    try:
        thread = project_client.agents.threads.create()
        prompt = f"""
        あなたはプロの古物商・せどりアドバイザーです。
        ユーザーが検索した商品「{keyword}」のヤフオク直近落札データは以下の通りです。
        最高値: {market_data['max_price']}円, 平均値: {market_data['avg_price']}円, サンプル数: {market_data['sample_count']}件

        このデータをもとに、メルカリやリサイクルショップで仕入れる際の「推奨仕入れ上限価格（販売手数料や送料、利益を考慮）」と「検品時の注意点」をアドバイスしてください。
        必ず以下のJSONフォーマットのみを出力してください（Markdownの ```json 等の装飾は絶対に含めないでください）。
        {{
            "target_buy_price": "〇〇", (例: 15,000 ※数値とカンマのみの文字列)
            "profitability": "A(高利益) / B(普通) / C(薄利・リスク高) のいずれか",
            "ai_advice": "仕入れ時の注意点（例：『第何版か確認必須』『付属品の欠品に注意』など具体的なアドバイスを100〜150文字程度で）"
        }}
        """
        
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
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    if match:
                        appraisal = json.loads(match.group())
                        return jsonify({
                            "keyword": keyword,
                            "market_data": market_data,
                            "appraisal": appraisal,
                            "thread_id": thread.id
                        })
                except Exception as parse_err:
                    logger.error(f"JSONパースエラー: {parse_err} \nAIの生テキスト: {text}")
                    pass
        
        return jsonify({"error": "AIが正しいフォーマットで返答しませんでした"}), 500

    except Exception as e:
        logger.error(f"AI鑑定エラー: {e}")
        return jsonify({"error": str(e)}), 500
    
@app.route("/api/scout/followup", methods=["POST"])
@login_required
def api_scout_followup():
    thread_id = request.json.get("thread_id")
    user_message = request.json.get("message")

    if not thread_id or not user_message:
        return jsonify({"error": "必要な情報が不足しています"}), 400

    try:
        prompt = f"{user_message}\n(※この追加質問にはJSON形式ではなく、通常の日本語テキストで簡潔に回答してください)"
        project_client.agents.messages.create(
            thread_id=thread_id, role="user", content=prompt
        )
        project_client.agents.runs.create_and_process(
            thread_id=thread_id, agent_id=agent.id
        )
        messages = project_client.agents.messages.list(
            thread_id=thread_id, order=ListSortOrder.DESCENDING
        )

        for m in messages:
            if m.role == "assistant" and m.text_messages:
                text = m.text_messages[0].text.value
                return jsonify({"answer": text})

        return jsonify({"error": "AIからの応答がありませんでした"}), 500
    except Exception as e:
        logger.error(f"AI追加質問エラー: {e}")
        return jsonify({"error": str(e)}), 500
    

# ========================================================
# キーワードリサーチ API
# ========================================================

def fetch_yahoo_suggest(keyword: str) -> list:
    """Yahoo!検索オートコンプリートからサジェストキーワードを取得"""
    try:
        encoded = urllib.parse.quote(keyword)
        url = f"https://search.yahoo.co.jp/search?p={encoded}&ei=UTF-8"
        res = requests.get(url, impersonate="chrome120", timeout=10)
        if res.status_code != 200:
            return []
        soup = BeautifulSoup(res.text, "html.parser")
        suggestions = []
        # Yahoo検索の関連キーワードセクション
        for tag in soup.select("li.Suggestion__item, .RelatedSearches a, .suggestList li, [data-ylk*='suggest'] span"):
            text = tag.get_text(strip=True)
            if text and text != keyword and len(text) < 50:
                suggestions.append(text)
        if not suggestions:
            # フォールバック: Yahoo suggestAPI
            api_url = f"https://ff.search.yahoo.com/gossip?output=json&command={encoded}"
            api_res = requests.get(api_url, impersonate="chrome120", timeout=8)
            if api_res.status_code == 200:
                try:
                    api_data = api_res.json()
                    gossip = api_data.get("gossip", {}).get("results", [])
                    suggestions = [g.get("key", "") for g in gossip if g.get("key")]
                except Exception:
                    pass
        return list(dict.fromkeys(suggestions))[:15]
    except Exception as e:
        logger.error(f"Yahoo suggest エラー: {e}")
        return []


def ai_keyword_research(seed: str, by_genre: bool, focus_profit: bool) -> dict:
    """Azure AI Agent でキーワード候補・トレンド分析を生成"""
    try:
        thread = project_client.agents.threads.create()

        profit_instruction = ""
        if focus_profit:
            profit_instruction = """
さらに "profit_keywords" キーに、以下の条件を満たすキーワードを10件リストアップしてください。
- 2024〜2025年の日本で特にヤフオク・メルカリ・ラクマで取引が活発
- 仕入れ（リサイクルショップ・ラクマ等）と転売（ヤフオク等）の価格差が大きく利ザヤが取りやすい
- 需要が高く在庫が枯渇しやすいもの（プレ値がつきやすい）
例：CSM変身ベルト、METAL BUILD、初代ゲームボーイ、ガンプラ限定品 等
各キーワードには "reason"（なぜ今稼ぎやすいか・30文字以内）と "score"（A/B/C）も付けてください。
"""

        genre_instruction = ""
        if by_genre:
            genre_instruction = """
また "by_genre" キーに、AIキーワード候補をジャンル別にグルーピングして返してください。
例: {"ガンプラ": ["HG 水星の魔女", ...], "CSM": [...], "ゲーム機": [...]}
"""

        prompt = f"""
あなたはヤフオク・メルカリ・ラクマ専門のせどり・転売リサーチャーです。
シードキーワード「{seed}」をもとに、ヤフオクや中古市場で需要が高い関連キーワードを提案してください。

必ず以下のJSONフォーマットのみを出力してください（Markdownの ```json 等の装飾は絶対に含めないでください）。

{{
  "ai_keywords": ["キーワード1", "キーワード2", ...],  // 15件以内の関連キーワード候補
  "trend_analysis": {{
    "summary": "このジャンルの最近のトレンドについての短い説明（80文字以内）",
    "hot_items": [
      {{"name": "商品名", "reason": "注目理由（30文字以内）", "score": "A/B/C"}}
    ]  // 5件
  }},
  "profit_keywords": [],  // focus_profit=trueのときのみ使用
  "by_genre": {{}}         // by_genre=trueのときのみ使用
}}
{profit_instruction}
{genre_instruction}
"""

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
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    if match:
                        return json.loads(match.group())
                except Exception as e:
                    logger.error(f"キーワードリサーチ JSONパースエラー: {e}\n raw: {text[:300]}")
        return {}
    except Exception as e:
        logger.error(f"ai_keyword_research エラー: {e}")
        return {}


import xml.etree.ElementTree as ET

# ========================================================
# リアルタイムトレンドデータ取得（RSS/フィード）
# ========================================================

def fetch_google_trends_japan() -> list:
    """
    Google Trends Japan RSS から急上昇ワードを取得。
    戻り値: [{"keyword": str, "traffic": str}]
    """
    try:
        url = "https://trends.google.co.jp/trending/rss?geo=JP"
        res = requests.get(url, impersonate="chrome120", timeout=10,
                           headers={"Accept": "application/rss+xml,application/xml,*/*"})
        if res.status_code != 200:
            logger.warning(f"Google Trends RSS HTTP {res.status_code}")
            return []

        root = ET.fromstring(res.text)
        ns = {"ht": "https://trends.google.com/trending/rss"}
        items = root.findall(".//item")
        results = []
        for item in items[:20]:
            title   = (item.findtext("title") or "").strip()
            traffic = (item.findtext("ht:approx_traffic", namespaces=ns) or "").strip()
            if title:
                results.append({"keyword": title, "traffic": traffic})
        logger.info(f"📈 Google Trends JP: {len(results)}件取得")
        return results
    except Exception as e:
        logger.error(f"Google Trends 取得エラー: {e}")
        return []


def fetch_youtube_trending_japan() -> list:
    """
    YouTube 日本の急上昇動画フィード（公開RSS）からタイトルを取得。
    戻り値: [{"title": str, "url": str}]
    """
    try:
        url = "https://www.youtube.com/feeds/videos.xml?chart=mostpopular&regionCode=JP&hl=ja&gl=JP"
        res = requests.get(url, impersonate="chrome120", timeout=10,
                           headers={"Accept": "application/atom+xml,application/xml,*/*"})
        if res.status_code != 200:
            logger.warning(f"YouTube trending RSS HTTP {res.status_code}")
            return []

        root = ET.fromstring(res.text)
        ns = {"atom": "http://www.w3.org/2005/Atom",
              "media": "http://search.yahoo.com/mrss/"}
        entries = root.findall("atom:entry", ns)
        results = []
        for entry in entries[:20]:
            title_el = entry.find("atom:title", ns)
            link_el  = entry.find("atom:link", ns)
            if title_el is not None and title_el.text:
                results.append({
                    "title": title_el.text.strip(),
                    "url":   link_el.get("href", "") if link_el is not None else "",
                })
        logger.info(f"📺 YouTube急上昇JP: {len(results)}件取得")
        return results
    except Exception as e:
        logger.error(f"YouTube trending 取得エラー: {e}")
        return []


def fetch_nhk_news_topics() -> list:
    """
    NHK NEWS RSS から直近の主要ニュースタイトルを取得（TV話題の代替）。
    戻り値: [{"title": str}]
    """
    try:
        # カテゴリ: 0=主要, 5=社会
        results = []
        for cat in ["0", "5"]:
            url = f"https://www3.nhk.or.jp/rss/news/cat{cat}.xml"
            res = requests.get(url, impersonate="chrome120", timeout=8,
                               headers={"Accept": "application/rss+xml,*/*"})
            if res.status_code != 200:
                continue
            root = ET.fromstring(res.text)
            for item in root.findall(".//item")[:10]:
                title = (item.findtext("title") or "").strip()
                if title:
                    results.append({"title": title})
        logger.info(f"📰 NHKニュース: {len(results)}件取得")
        return results[:15]
    except Exception as e:
        logger.error(f"NHK RSS 取得エラー: {e}")
        return []


def ai_media_keyword_analysis(
    google_trends: list,
    youtube_items: list,
    nhk_news: list,
    category_hint: str,
) -> dict:
    """
    リアルタイムで取得したトレンドデータをAIに渡し、
    ヤフオクで売れそうなキーワードを抽出・分類させる。
    """
    try:
        google_text  = "\n".join([f"・{t['keyword']}（{t['traffic']}）" for t in google_trends]) or "取得なし"
        youtube_text = "\n".join([f"・{v['title']}" for v in youtube_items]) or "取得なし"
        nhk_text     = "\n".join([f"・{n['title']}" for n in nhk_news]) or "取得なし"

        thread = project_client.agents.threads.create()
        prompt = f"""
あなたはヤフオク・メルカリ・ラクマの中古市場に精通したせどりリサーチャーです。
対象カテゴリ: {category_hint}

以下は今日のリアルタイムトレンドデータです。

【Google 急上昇ワード（日本）】
{google_text}

【YouTube 急上昇動画（日本）タイトル】
{youtube_text}

【NHKニュース 最新トピック】
{nhk_text}

これらのトレンドデータを分析し、ヤフオクや中古市場で需要が上がりそうな商品・キーワードを抽出してください。

判断基準:
- アニメ・ゲーム・映画に関連するグッズ・フィギュア
- 芸能人・インフルエンサーが紹介した商品
- ニュースで取り上げられたことで需要が増えた商品
- 対象カテゴリ外でも「今買われそうな中古品」があれば含める

必ず以下のJSONフォーマットのみを出力してください（Markdownの ```json 等の装飾は絶対に含めないでください）。
{{
  "media_keywords": [
    {{
      "keyword": "ヤフオク検索用キーワード（具体的な商品名・型番・シリーズ名）",
      "source": "Google/YouTube/NHK/TV のどれか",
      "reason": "なぜ今注目か（25文字以内）",
      "heat": "🔥 or 🔥🔥 or 🔥🔥🔥"
    }}
  ]
}}
media_keywords は最大15件。関係ないトレンドは無視し、中古市場に関係しそうなものだけを厳選してください。
"""
        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        messages = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)

        for m in messages:
            if m.role == "assistant" and m.text_messages:
                text = m.text_messages[0].text.value
                try:
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    if match:
                        return json.loads(match.group())
                except Exception as e:
                    logger.error(f"media_keyword_analysis JSONパースエラー: {e}")
        return {}
    except Exception as e:
        logger.error(f"ai_media_keyword_analysis エラー: {e}")
        return {}


@app.route("/api/keyword-suggestions", methods=["POST"])
@login_required
def api_keyword_suggestions():
    """
    シードキーワードが思いつかないユーザー向け。
    ① AIの知識ベース（trending/discontinued）
    ② リアルタイムトレンドデータ（Google/YouTube/NHK）→ AIで分析
    の2系統を並列取得してまとめて返す。
    """
    try:
        data = request.get_json() or {}
        category = (data.get("category") or "all").strip()

        category_hint = {
            "toy":   "おもちゃ・フィギュア・プラモデル（ガンプラ・CSM変身ベルト・METAL BUILD等）",
            "game":  "ゲーム機・レトロゲーム・ゲームソフト",
            "audio": "オーディオ・音響機器・レコード・ヴィンテージ家電",
            "anime": "アニメグッズ・キャラクター商品・同人誌",
            "other": "雑貨・ブランド品・スポーツ用品・楽器",
        }.get(category, "ジャンル不問（幅広い中古市場全般）")

        # ① AIの知識ベース（trending / discontinued）
        def ai_knowledge_based() -> dict:
            thread = project_client.agents.threads.create()
            prompt = f"""
あなたはヤフオク・メルカリ・ラクマの中古市場に精通したせどりリサーチャーです。
対象カテゴリ: {category_hint}

以下の2種類のキーワードセットをJSONで出力してください。

1. "trending" : 現在（2024〜2025年）のトレンドで取引が活発・需要が高いキーワード
   - 最近アニメ化・映画化・リメイクされた関連グッズ
   - 限定品・コラボ商品でプレ値がついているもの

2. "discontinued" : 生産終了・廃盤・旧モデルで希少価値が上がっているキーワード
   - メーカー終了・シリーズ完結した商品
   - 旧型だが性能・人気が高く中古需要があるもの

各リストは8〜12件。キーワードは「ヤフオクで実際に検索されるような具体的な商品名・型番・シリーズ名」にしてください。

必ず以下のJSONフォーマットのみを出力してください（Markdownの ```json 等の装飾は絶対に含めないでください）。
{{
  "trending": [
    {{"keyword": "商品名・キーワード", "tag": "タグ（例: アニメ化・SNS話題・限定品）", "heat": "🔥/🔥🔥/🔥🔥🔥"}}
  ],
  "discontinued": [
    {{"keyword": "商品名・キーワード", "tag": "タグ（例: 生産終了・廃番・旧型）", "note": "希少な理由（20文字以内）"}}
  ]
}}
"""
            project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
            project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
            messages = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
            for m in messages:
                if m.role == "assistant" and m.text_messages:
                    text = m.text_messages[0].text.value
                    try:
                        match = re.search(r"\{.*\}", text, re.DOTALL)
                        if match:
                            return json.loads(match.group())
                    except Exception:
                        pass
            return {}

        # ② リアルタイムデータ取得 + AI分析
        def realtime_media_based() -> dict:
            import concurrent.futures as cf
            with cf.ThreadPoolExecutor(max_workers=3) as ex:
                f_google  = ex.submit(fetch_google_trends_japan)
                f_youtube = ex.submit(fetch_youtube_trending_japan)
                f_nhk     = ex.submit(fetch_nhk_news_topics)
                google_data  = f_google.result(timeout=15)
                youtube_data = f_youtube.result(timeout=15)
                nhk_data     = f_nhk.result(timeout=12)

            logger.info(f"リアルタイムデータ収集完了: Google={len(google_data)} YouTube={len(youtube_data)} NHK={len(nhk_data)}")

            if not google_data and not youtube_data and not nhk_data:
                logger.warning("⚠️ リアルタイムデータが全て取得失敗")
                return {"media_keywords": [], "sources_available": False}

            result = ai_media_keyword_analysis(google_data, youtube_data, nhk_data, category_hint)
            result["sources_available"] = True
            result["raw_counts"] = {
                "google": len(google_data),
                "youtube": len(youtube_data),
                "nhk": len(nhk_data),
            }
            return result

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f_kb = executor.submit(ai_knowledge_based)
            f_rt = executor.submit(realtime_media_based)
            kb_result = f_kb.result(timeout=90)
            rt_result = f_rt.result(timeout=90)

        return jsonify({
            "trending":       kb_result.get("trending", []),
            "discontinued":   kb_result.get("discontinued", []),
            "media_keywords": rt_result.get("media_keywords", []),
            "sources_available": rt_result.get("sources_available", False),
            "raw_counts":     rt_result.get("raw_counts", {}),
        })

    except Exception as e:
        logger.error(f"keyword-suggestions エラー: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/keyword-research", methods=["POST"])
@login_required
def api_keyword_research():
    try:
        data = request.get_json() or {}
        seed = (data.get("seed") or "").strip()
        if not seed:
            return jsonify({"error": "シードキーワードが指定されていません"}), 400

        by_genre    = bool(data.get("by_genre", False))
        focus_profit = bool(data.get("focus_profit", False))

        # 並列取得（Yahoo suggest + AI）
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_web = executor.submit(fetch_yahoo_suggest, seed)
            future_ai  = executor.submit(ai_keyword_research, seed, by_genre, focus_profit)
            web_suggestions = future_web.result(timeout=15)
            ai_result       = future_ai.result(timeout=60)

        ai_keywords    = ai_result.get("ai_keywords", [])
        trend_analysis = ai_result.get("trend_analysis", {})
        profit_kws_raw = ai_result.get("profit_keywords", [])
        by_genre_data  = ai_result.get("by_genre", {})

        # profit_keywords は文字列リストまたはオブジェクトリストどちらも許容
        profit_keywords = []
        profit_details  = []
        for item in profit_kws_raw:
            if isinstance(item, str):
                profit_keywords.append(item)
            elif isinstance(item, dict):
                name = item.get("name", "")
                if name:
                    profit_keywords.append(name)
                    profit_details.append({
                        "name":   name,
                        "reason": item.get("reason", ""),
                        "score":  item.get("score", "B"),
                    })

        logger.info(
            f"✅ キーワードリサーチ完了: seed={seed} "
            f"web={len(web_suggestions)} ai={len(ai_keywords)} profit={len(profit_keywords)}"
        )

        return jsonify({
            "web":            web_suggestions,
            "ai":             ai_keywords,
            "profit":         profit_keywords,
            "profit_details": profit_details,
            "trend_analysis": trend_analysis,
            "by_genre":       by_genre_data,
        })

    except Exception as e:
        logger.error(f"キーワードリサーチAPI エラー: {e}")
        return jsonify({"error": str(e)}), 500


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

@app.route("/api/test-notification", methods=["POST"])
@login_required
def api_test_notification():
    if not db:
        return jsonify({"error": "DB not initialized"}), 500
    uid = request.user["uid"]
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
    message = """🧪 テスト通知
PB Stock Monitor Pro です。

このメッセージが届いていれば、
LINE通知設定は正常に動作しています 👍
"""
    send_line_notification(line_user_id, message)
    return jsonify({"status": "ok"})

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    reply_text = (
                   f"あなたの LINE User ID はこちらです：\n\n"
                   f"{user_id}\n\n"
                   f"この値をコピーしてアプリの設定画面に貼り付けてください。"
    )
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
            )
    except Exception as e:
        app.logger.error(f"Error sending reply: {e}")

def scrape_yahuoku_item_page(url):
    try:
        if not is_allowed_yahoo_auction_url(url):
            logger.warning(f"⚠️ 許可されていないヤフオクURLへのアクセス試行がブロックされました: {url}")
            return None

        res = requests.get(
            url,
            impersonate="chrome120",
            timeout=15,
            allow_redirects=False,
        )
        if res.status_code != 200:
            return None
            
        soup = BeautifulSoup(res.text, "html.parser")
        price_tag = soup.find("dd", class_="Price__value")
        if not price_tag:
            return None
            
        price_str = price_tag.text.strip()
        price_int = int(re.sub(r"\D", "", price_str))
        
        time_tag = soup.find("li", class_="Count__item--time")
        time_rem = "不明"
        if time_tag:
            time_value = time_tag.find("dd", class_="Count__number")
            if time_value:
                time_rem = time_value.text.strip()
                
        return {
            "price_int": price_int,
            "price_str": price_str,
            "time_remaining": time_rem
        }
    except Exception as e:
        logger.error(f"❌ ヤフオク個別取得エラー: {e}")
        return None

def check_watchlist_job():
    logger.info("⏰ 統合在庫・相場監視ジョブ開始")
    users_ref = db.collection("artifacts").document(APP_ID).collection("users")
    user_refs = list(users_ref.list_documents())

    for user_ref in user_refs:
        uid = user_ref.id
        line_ref = users_ref.document(uid).collection("settings").document("line").get()
        if not line_ref.exists: continue

        line_user_id = line_ref.to_dict().get("lineUserId")
        if not line_user_id: continue

        watchlist_ref = users_ref.document(uid).collection("watchlist")
        items = list(watchlist_ref.stream())

        for item_doc in items:
            item = item_doc.to_dict()
            url = item.get("url", "")
            title = item.get("title", "名称不明")

            # --- プレバン監視 ---
            if is_allowed_p_bandai_or_test_url(url):
                scraped = scrape_premium_bandai(url)
                if not scraped: continue
                prev_status = item.get("inStock", False)
                current_status = scraped["inStock"]
                if prev_status != current_status:
                    item_doc.reference.update({
                        "inStock": current_status,
                        "statusText": scraped["statusText"],
                        "lastChecked": firestore.SERVER_TIMESTAMP,
                    })
                    msg = f"📦 プレバン在庫変動\n{title}\n状態: {scraped['statusText']}\n{url}"
                    send_line_notification(line_user_id, msg)

            # --- ヤフオク監視 ---
            else:
                if url and url.startswith("/"):
                    fixed_url = urllib.parse.urljoin("[https://auctions.yahoo.co.jp](https://auctions.yahoo.co.jp)", url)
                    url = fixed_url
                    try:
                        item_doc.reference.update({"url": fixed_url})
                    except Exception as e:
                        logger.error(f"❌ URL自動補正エラー: {e}")

                if is_allowed_yahoo_auction_url(url):
                    scraped = scrape_yahuoku_item_page(url)
                    if not scraped:
                        continue

                    time_rem = scraped["time_remaining"]
                    if "終了" in time_rem:
                        try:
                            archive_ref = users_ref.document(uid).collection("yahoo_archive")
                            archive_data = {
                                **item,
                                "finalPrice": scraped["price_int"],
                                "finalPriceText": scraped["price_str"],
                                "endedAt": firestore.SERVER_TIMESTAMP,
                                "sourceUrl": url,
                            }
                            archive_ref.add(archive_data)
                            logger.info(f"📦 ヤフオク終了オークションをアーカイブ: {title} ({url})")
                            item_doc.reference.delete()
                        except Exception as e:
                            logger.error(f"❌ 終了オークションアーカイブ/削除エラー: {e}")
                        continue

                    updates = {}
                    msgs = []
                    current_price = scraped["price_int"]

                    my_limit = item.get("my_target_price")
                    if my_limit and current_price > my_limit:
                        if item.get("last_notified_price") != current_price:
                            msgs.append(f"⚠️ 予算超過通知\n設定上限: {my_limit:,}円\n現在価格: {current_price:,}円に更新されました。")
                            updates["last_notified_price"] = current_price

                    if "分" in time_rem:
                        try:
                            mins = int(re.sub(r"\D", "", time_rem))
                            if mins <= 10 and not item.get("notified_10min"):
                                msgs.append(f"⏳ 終了間近通知\n残り時間: {time_rem}\n現在価格: {current_price:,}円")
                                updates["notified_10min"] = True
                        except ValueError:
                            pass

                    updates["statusText"] = f"現在:{scraped['price_str']} / 残り:{time_rem}"
                    updates["lastChecked"] = firestore.SERVER_TIMESTAMP
                    item_doc.reference.update(updates)

                    if msgs:
                        combined_msg = f"🔨 ヤフオク監視\n{title}\n\n" + "\n---\n".join(msgs) + f"\n\n{url}"
                        send_line_notification(line_user_id, combined_msg)

@app.route("/api/recommendations", methods=["GET"])
@login_required
def api_recommendations():
    if not project_client or not agent:
        return jsonify({"error": "AI Agentが設定されていません"}), 500
    try:
        thread = project_client.agents.threads.create()
        prompt = """
        あなたはプレミアムバンダイ（ガンプラ、METAL BUILD、仮面ライダーCSM、アニメグッズなど）の専門家であり、転売対策やコレクター向けの在庫監視のアドバイザーです。
        現在、需要が高く、在庫監視をしておくべきプレミアムバンダイの商品を3つ提案してください。
        必ず以下のJSON配列フォーマットのみを出力してください（Markdownの ```json 等の装飾は絶対に含めないでください）。
        [ { "title": "正確な商品名", "url": "https://p-bandai.jp/item/item-で始まるもの", "reason": "おすすめの理由" } ]
        """
        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        messages = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)

        for m in messages:
            if m.role == "assistant" and m.text_messages:
                text = m.text_messages[0].text.value
                try:
                    match = re.search(r"\[.*\]", text, re.DOTALL)
                    if match:
                        recommendations = json.loads(match.group())
                        return jsonify({"recommendations": recommendations})
                except Exception as parse_err:
                    pass
        return jsonify({"error": "AIが正しいフォーマットで返答しませんでした"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route("/api/watchlist/bulk", methods=["POST"])
@login_required
def api_watchlist_bulk():
    if not db:
        return jsonify({"error": "データベースに接続できません"}), 500
    urls = request.json.get("urls", [])
    if not urls:
        return jsonify({"error": "URLが指定されていません"}), 400
    if len(urls) > 5:
        return jsonify({"error": "一度に登録できるのは最大5件までです"}), 400

    uid = request.user["uid"]
    results = {"success": [], "errors": []}
    watchlist_ref = db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("watchlist")

    for index, url in enumerate(urls):
        if not url: continue
        if not is_allowed_p_bandai_or_test_url(url):
            results["errors"].append(f"{index+1}件目: 対象外のURLです")
            continue
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
                results["errors"].append(f"{index+1}件目: DB保存エラー {str(e)}")
        else:
            results["errors"].append(f"{index+1}件目: 商品情報の取得に失敗しました")
    return jsonify({
        "message": f"{len(results['success'])}件 登録しました",
        "results": results
    })

if not IS_PRODUCTION:
    MOCK_ITEM_IN_STOCK = False

    @app.route("/test-item")
    def test_item_page():
        global MOCK_ITEM_IN_STOCK
        stock_mark = "○" if MOCK_ITEM_IN_STOCK else "×"
        status_text = "🟢 在庫あり" if MOCK_ITEM_IN_STOCK else "🔴 在庫なし"
        html = f"""
        <!DOCTYPE html>
        <html lang="ja">
        <head>
            <meta charset="UTF-8">
            <title>【テスト用】擬似プレバン商品 | プレミアムバンダイ</title>
            <meta property="og:image" content="https://dummyimage.com/400x400/2563eb/ffffff&text=TEST+ITEM">
            <style>
                body {{ font-family: sans-serif; text-align: center; padding: 50px; background: #f3f4f6; }}
                .card {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); display: inline-block; }}
                button {{ background: #2563eb; color: white; border: none; padding: 15px 30px; font-size: 16px; font-weight: bold; border-radius: 5px; cursor: pointer; transition: 0.2s; }}
                button:hover {{ background: #1d4ed8; transform: translateY(-2px); }}
            </style>
        </head>
        <body>
            <div class="card">
                <h2 style="color: #333;">【テスト用】擬似プレバン商品</h2>
                <p style="font-size: 32px; font-weight: bold; margin: 20px 0;">{status_text}</p>
                <form action="/test-item/toggle" method="POST">
                    <button type="submit">在庫状態を切り替える</button>
                </form>
                <p style="margin-top:20px; font-size: 12px; color: #666;">
                    このページのURLを監視リストに登録して、システム全体の動作テストを行えます。
                </p>
            </div>
            <script>
                var data = {{ price: '9999' }};
                var orderstock_list = {{"item_id_123":"{stock_mark}"}};
            </script>
        </body>
        </html>
        """
        return html

    @app.route("/test-item/toggle", methods=["POST"])
    def toggle_test_item():
        global MOCK_ITEM_IN_STOCK
        MOCK_ITEM_IN_STOCK = not MOCK_ITEM_IN_STOCK
        return redirect(url_for('test_item_page'))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    scheduler.add_job(
        check_watchlist_job,
        trigger="interval",
        minutes=5,
        id="watchlist_checker",
        replace_existing=True,
    )
    scheduler.start()
    app.run(host="0.0.0.0", port=port, debug=False)