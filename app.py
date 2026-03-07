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
# 認証デコレータ (修正版)
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
    SSRF対策のため、スキーム・ホスト名を検証する
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False

    # スキーム制限
    if parsed.scheme not in ("http", "https"):
        return False

    hostname = (parsed.hostname or "").lower()
    path = parsed.path or ""

    # 自アプリ用テストページを許可
    if "/test-item" in path:
        return True

    # プレミアムバンダイのみ許可
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

    # ヤフオク関連ドメインのみ許可
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
        # URLバリデーション（SSRF対策）
        if not is_allowed_p_bandai_or_test_url(url):
            logger.warning(f"⚠️ 許可されていないURLへのアクセス試行がブロックされました: {url}")
            return None

        # プレミアムバンダイのBot対策を回避するために impersonate を使用
        res = requests.get(
            url,
            impersonate="chrome120",
            timeout=15,
            allow_redirects=False,  # リダイレクト先もホワイトリスト外に飛ばさない
        )
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
        # if "p-bandai.jp" not in url:
        #     results["errors"].append(f"{index+1}行目: プレミアムバンダイのURLではありません")
        #     continue
        
        # プレバンURLか簡易チェック（テスト用URLも許可） ※SSRF対策で厳格判定
        if not is_allowed_p_bandai_or_test_url(url):
            results["errors"].append(f"{index+1}行目: 対象外のURLです")
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


# ======================================================================
# ヤフオク検索キーワード最適化
# ======================================================================
def optimize_search_keyword(raw_keyword):
    """
    ユーザーの入力をヤフオクでヒットしやすい「あいまい検索」用に最適化する
    """
    # 1. 全角英数字を半角に統一（例：ＣＳＭ ➔ CSM）
    keyword = unicodedata.normalize('NFKC', raw_keyword)
    
    # 2. 英語/数字と日本語の境界に自動でスペースを入れる（例：CSMファイズギア ➔ CSM ファイズギア）
    keyword = re.sub(r'([a-zA-Z0-9])([^\x01-\x7E])', r'\1 \2', keyword)
    keyword = re.sub(r'([^\x01-\x7E])([a-zA-Z0-9])', r'\1 \2', keyword)
    
    # 3. 余分なスペースを1つにまとめる
    keyword = re.sub(r'\s+', ' ', keyword).strip()
    
    return keyword

# =======================================================================================
# ヤフオク高速落札相場取得
# =======================================================================================
def scrape_yahuoku_closed(raw_keyword):
    """
    ヤフオクの落札相場検索（あいまい検索対応版）
    """
    try:
        # 入力を自動補正（例: "CSMファイズギア" -> "CSM ファイズギア"）
        keyword = optimize_search_keyword(raw_keyword)
        logger.info(f"🔍 検索キーワードを最適化: '{raw_keyword}' ➔ '{keyword}'")

        # 検索処理を内部関数化（リトライできるようにするため）
        def fetch_items(search_kw):
            encoded = urllib.parse.quote(search_kw)
            url = f"https://auctions.yahoo.co.jp/closedsearch/closedsearch?p={encoded}&n=50"
            res = requests.get(url, impersonate="chrome120", timeout=15)
            if res.status_code != 200:
                return []
                
            soup = BeautifulSoup(res.text, "html.parser")
            product_items = soup.find_all("li", class_="Product")
            
            fetched = []
            for item in product_items:
                try:
                    title_tag = item.find("a", class_="Product__titleLink")
                    price_tag = item.find("span", class_="Product__priceValue")
                    img_tag = item.find("img")
                    
                    if title_tag and price_tag:
                        price_str = price_tag.text.strip().replace(',', '').replace('円', '')
                        if price_str.isdigit():
                            # ヤフオクのリンクが相対パスの場合でも絶対URLに正規化する
                            item_url = urllib.parse.urljoin(
                                "https://auctions.yahoo.co.jp",
                                title_tag.get("href", "#"),
                            )
                            fetched.append({
                                "title": title_tag.text.strip(),
                                "url": item_url,
                                "price": f"{int(price_str):,}",
                                "raw_price": int(price_str),
                                "image": img_tag.get("src", "") if img_tag else ""
                            })
                except Exception:
                    continue
            return fetched

        # 1回目の検索（最適化キーワード）
        items = fetch_items(keyword)

        # 2回目の検索（ヒットしなかった場合の自動フォールバック）
        # 複数単語でヒットゼロなら、最後の単語を削って条件を緩める (例: CSM ファイズギア ver2 ➔ CSM ファイズギア)
        if not items and " " in keyword:
            looser_keyword = " ".join(keyword.split(" ")[:-1])
            logger.info(f"⚠️ ヒットなし。条件を緩めて再検索します: '{looser_keyword}'")
            items = fetch_items(looser_keyword)

        if not items:
            return None

        # 価格計算用
        raw_prices = [i["raw_price"] for i in items]
        return {
            "max_price": f"{max(raw_prices):,}",
            "avg_price": f"{sum(raw_prices) // len(raw_prices):,}",
            "sample_count": len(items),
            "items": items 
        }
        
    except Exception as e:
        logger.error(f"❌ ヤフオクスクレイピングエラー: {e}")
        return None


# ========================================================
# 開催中オークション取得処理 (新規追加)
# ========================================================
def scrape_yahuoku_active(raw_keyword):
    """
    ヤフオクの現在開催中の検索結果をスクレイピングする
    """
    try:
        # 入力を自動補正（あいまい検索対応）
        keyword = optimize_search_keyword(raw_keyword)
        encoded_keyword = urllib.parse.quote(keyword)
        
        # 開催中の検索URL
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
                
                # 即決価格がある場合は取得
                buy_now_tag = item.find("span", class_="Product__priceValue Product__priceValue--buyNow")
                buy_now_price = buy_now_tag.text.strip() if buy_now_tag else None

                # ▼ 追加：入札件数の取得（Product__bid 系のクラス名から取得）
                bid_tag = item.find(class_=re.compile(r"Product__bid"))
                bids = bid_tag.text.strip() if bid_tag else "0"

                # ▼ 追加：終了時間（残り時間）の取得（Product__time 系のクラス名から取得）
                time_tag = item.find(class_=re.compile(r"Product__time"))
                end_time = time_tag.text.strip() if time_tag else "-"

                if title_tag and price_tag:
                    title = title_tag.text.strip()
                    # ヤフオクのリンクが相対パスの場合でも絶対URLに正規化する
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
                        "bids": bids,            # フロントに渡すデータに追加
                        "end_time": end_time     # フロントに渡すデータに追加
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
# API: 開催中オークション追跡
# ========================================================
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

# ==============================================================================================
# AIせどり鑑定士 (ヤフオク相場 ➔ AI判定) API
# ==============================================================================================
@app.route("/api/scout", methods=["POST"])
@login_required
def api_scout_item():
    keyword = request.json.get("keyword")
    if not keyword:
        return jsonify({"error": "検索キーワードが指定されていません"}), 400

    logger.info(f"🔎 AI鑑定開始: {keyword}")

    # 1. ヤフオクの落札相場を高速スクレイピング
    market_data = scrape_yahuoku_closed(keyword)
    if not market_data:
        return jsonify({"error": "ヤフオクの落札相場データが見つかりませんでした。別のキーワードをお試しください。"}), 404

    # 2. Azure AI Agent による鑑定依頼
    try:
        thread = project_client.agents.threads.create()
        
        # 古物商としてのノウハウをAIにプロンプトで指示
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
                    # AIの返答からJSON部分だけを抽出
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
    

# ========================================================
# AIせどり鑑定士 (追加質問 API)
# ========================================================
@app.route("/api/scout/followup", methods=["POST"])
@login_required
def api_scout_followup():
    thread_id = request.json.get("thread_id")
    user_message = request.json.get("message")

    if not thread_id or not user_message:
        return jsonify({"error": "必要な情報が不足しています"}), 400

    try:
        # AIが再びJSONで返してこないように、裏でこっそり指示を追加
        prompt = f"{user_message}\n(※この追加質問にはJSON形式ではなく、通常の日本語テキストで簡潔に回答してください)"

        # 既存のスレッド(文脈を記憶している)にメッセージを追加
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
                # AIからの最新のテキスト回答をそのまま返す
                text = m.text_messages[0].text.value
                return jsonify({"answer": text})

        return jsonify({"error": "AIからの応答がありませんでした"}), 500

    except Exception as e:
        logger.error(f"AI追加質問エラー: {e}")
        return jsonify({"error": str(e)}), 500
    

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
# ヤフオク個別ページ専用のスクレイピング関数
# ========================================================================================
def scrape_yahuoku_item_page(url):
    """
    ヤフオク個別商品ページの現在価格と残り時間を取得する
    """
    try:
        # URLバリデーション（SSRF対策）
        if not is_allowed_yahoo_auction_url(url):
            logger.warning(f"⚠️ 許可されていないヤフオクURLへのアクセス試行がブロックされました: {url}")
            return None

        res = requests.get(
            url,
            impersonate="chrome120",
            timeout=15,
            allow_redirects=False,  # リダイレクトチェーンを制限
        )
        if res.status_code != 200:
            return None
            
        soup = BeautifulSoup(res.text, "html.parser")
        
        # 現在価格の取得 (クラス名はヤフオクの仕様変更で変わる可能性あり)
        price_tag = soup.find("dd", class_="Price__value")
        if not price_tag:
            return None
            
        price_str = price_tag.text.strip()
        price_int = int(re.sub(r"\D", "", price_str)) # 数字だけ抽出
        
        # 残り時間の取得
        time_tag = soup.find("li", class_="Count__item--time")
        time_rem = "不明"
        if time_tag:
            time_value = time_tag.find("dd", class_="Count__number")
            if time_value:
                time_rem = time_value.text.strip() # 例: "8分", "12時間", "終了"
                
        return {
            "price_int": price_int,
            "price_str": price_str,
            "time_remaining": time_rem
        }
    except Exception as e:
        logger.error(f"❌ ヤフオク個別取得エラー: {e}")
        return None


# ========================================================================================
# 監視ジョブ本体 (デバッグログ強化版)
# ========================================================================================
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

            # ==========================================
            # プレバン監視ロジック
            # ==========================================
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

            # ==========================================
            # ヤフオク監視ロジック (新規追加)
            # ==========================================
            else:
                # ヤフオクの相対パスで保存されている既存データを自動補正
                if url and url.startswith("/"):
                    fixed_url = urllib.parse.urljoin(
                        "https://auctions.yahoo.co.jp",
                        url,
                    )
                    url = fixed_url
                    try:
                        item_doc.reference.update({"url": fixed_url})
                    except Exception as e:
                        logger.error(f"❌ URL自動補正エラー: {e}")

            if is_allowed_yahoo_auction_url(url):
                scraped = scrape_yahuoku_item_page(url)
                if not scraped:
                    continue

                # 期限切れ（終了）したオークションはアーカイブしてから監視リストから削除
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

                # ① 高値更新チェック（自分が設定した上限を超えたか）
                my_limit = item.get("my_target_price")
                if my_limit and current_price > my_limit:
                    # 同じ価格で何度も通知しないためのフラグチェック
                    if item.get("last_notified_price") != current_price:
                        msgs.append(f"⚠️ 予算超過通知\n設定上限: {my_limit:,}円\n現在価格: {current_price:,}円に更新されました。")
                        updates["last_notified_price"] = current_price

                # ② 終了10分前チェック
                # 「分」が含まれていて、かつ10以下の場合に通知
                if "分" in time_rem:
                    try:
                        mins = int(re.sub(r"\D", "", time_rem))
                        if mins <= 10 and not item.get("notified_10min"):
                            msgs.append(f"⏳ 終了間近通知\n残り時間: {time_rem}\n現在価格: {current_price:,}円")
                            updates["notified_10min"] = True # 一度通知したらフラグを立てる
                    except ValueError:
                        pass

                # 変更や通知すべき事象があればFirestore更新とLINE送信
                updates["statusText"] = f"現在:{scraped['price_str']} / 残り:{time_rem}"
                updates["lastChecked"] = firestore.SERVER_TIMESTAMP
                item_doc.reference.update(updates)

                if msgs:
                    combined_msg = f"🔨 ヤフオク監視\n{title}\n\n" + "\n---\n".join(msgs) + f"\n\n{url}"
                    send_line_notification(line_user_id, combined_msg)


# ========================================================
# AIによるオススメ商品提案 API
# ========================================================
@app.route("/api/recommendations", methods=["GET"])
@login_required
def api_recommendations():
    if not project_client or not agent:
        return jsonify({"error": "AI Agentが設定されていません"}), 500

    logger.info("🤖 AIにおすすめ商品をリクエスト中...")

    try:
        thread = project_client.agents.threads.create()
        
        # AIへのプロンプト（JSON形式で確実に出力させる）
        prompt = """
        あなたはプレミアムバンダイ（ガンプラ、METAL BUILD、仮面ライダーCSM、アニメグッズなど）の専門家であり、転売対策やコレクター向けの在庫監視のアドバイザーです。
        現在、需要が高く、在庫監視をしておくべき（再販が期待される、または人気で即完売した）プレミアムバンダイの商品を3つ提案してください。
        
        必ず以下のJSON配列フォーマットのみを出力してください（Markdownの ```json 等の装飾は絶対に含めないでください）。
        [
          {
            "title": "正確な商品名",
            "url": "プレミアムバンダイの実際のURL ([https://p-bandai.jp/item/item-で始まるもの](https://p-bandai.jp/item/item-で始まるもの))",
            "reason": "おすすめの理由（50文字程度。なぜ監視すべきか）"
          }
        ]
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
                    # AIの返答からJSON配列部分だけを抽出
                    match = re.search(r"\[.*\]", text, re.DOTALL)
                    if match:
                        recommendations = json.loads(match.group())
                        return jsonify({"recommendations": recommendations})
                except Exception as parse_err:
                    logger.error(f"JSONパースエラー: {parse_err} \nAIの生テキスト: {text}")
                    pass
        
        return jsonify({"error": "AIが正しいフォーマットで返答しませんでした"}), 500

    except Exception as e:
        logger.error(f"AI提案エラー: {e}")
        return jsonify({"error": str(e)}), 500
    
    
# ========================================================
# URL一括登録エンドポイント (JSON版・AI提案一括登録用)
# ========================================================
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
    results = {
        "success": [],
        "errors": []
    }
    
    watchlist_ref = db.collection("artifacts").document(APP_ID).collection("users").document(uid).collection("watchlist")

    for index, url in enumerate(urls):
        if not url:
            continue

        if not is_allowed_p_bandai_or_test_url(url):
            results["errors"].append(f"{index+1}件目: 対象外のURLです")
            continue

        # AIは使わず高速にスクレイピングのみ
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


# ========================================================
# テスト用ダミーページ (E2Eテスト用)
# 本番環境では自動的に無効化する
# ========================================================
if not IS_PRODUCTION:
    # メモリ上で擬似在庫状態を管理
    MOCK_ITEM_IN_STOCK = False

    @app.route("/test-item")
    def test_item_page():
        global MOCK_ITEM_IN_STOCK
        stock_mark = "○" if MOCK_ITEM_IN_STOCK else "×"
        status_text = "🟢 在庫あり" if MOCK_ITEM_IN_STOCK else "🔴 在庫なし"
        
        # scrape_premium_bandai() の正規表現に引っかかるように変数を配置
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
        minutes=5,
        id="watchlist_checker",
        replace_existing=True,
    )
    scheduler.start()
    # 開発環境でVSCodeなどから実行する場合
    app.run(host="0.0.0.0", port=port, debug=False)