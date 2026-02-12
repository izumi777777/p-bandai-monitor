import os
import time
import json
import logging
import re
import urllib.parse
import warnings
from datetime import datetime
from typing import Dict, List, Optional
from statistics import mean

# プレミアムバンダイのBot対策回避スタイルに合わせ、curl_cffi を使用
try:
    from curl_cffi import requests
except ImportError:
    import requests

from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore

# Azure AI Agent
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
from azure.ai.agents.models import ListSortOrder

# ========================================================
# 1. 初期設定・環境変数
# ========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# 外部ライブラリの冗長なログを抑制 (AzureのHTTPログなど)
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Firestoreの警告を抑制
warnings.filterwarnings("ignore", category=UserWarning, message="Detected filter using positional arguments")

logger = logging.getLogger(__name__)

# .envファイルから環境変数を読み込み
load_dotenv(".env")

AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")
FIREBASE_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
FIRESTORE_COLLECTION = "daily_goods_discontinued_monitor"
FIRESTORE_PURCHASE_LIST_COLLECTION = "daily_goods_purchase_list" # 仕入れリスト用コレクション

# 監視対象の日用品キーワード
DAILY_GOODS_KEYWORDS = [
    "シャンプー", "洗剤", "歯磨き粉", "化粧水",
    "乳液", "ボディソープ", "柔軟剤", "日用品"
]

# 製造終了情報のソースURL（花王）
KAO_URL = "https://www.kao-kirei.com/ja/expire-item/khg/?tw=khg"

# 花王の主要ブランドリスト（商品名判定用）
KAO_BRANDS = [
    "ビオレ", "ニベア", "クイックル", "アタック", "ハミング", "メリーズ", 
    "ロリエ", "サクセス", "ケープ", "リーゼ", "エッセンシャル", "セグレタ", 
    "キュキュット", "マジックリン", "ハイター", "リセッシュ", "クリアクリーン", 
    "ピュオーラ", "ディープクリーン", "バブ", "８ｘ４", "８×４", "ines", "イネス", 
    "めぐりズム", "ワイドハイター", "ホーミング", "ファミリー", "エマール", 
    "アジエンス", "ガードハロー", "アトリックス", "IROKA", "ＩＲＯＫＡ",
    "ブローネ", "プリマヴィスタ", "ソフィーナ", "カネボウ", "アルブラン", "エスト"
]

# 抽出用のキーワードパターン
DISCONTINUE_PATTERN = re.compile(r"(生産終了|終売|販売終了|在庫切れ|供給停止)")

# 除外リスト
IGNORE_DOMAINS = ["youtube.com", "twitter.com", "x.com", "instagram.com"]
IGNORE_TEXTS = ["マイページ", "ログイン", "カート", "お問い合わせ", "閉じる", "詳細はこちら", "すべて", "ご利用ガイド", "ショッピングガイド"]
IGNORE_TAGS = ["限定品", "医薬部外品", "除菌", "eco", "企画品", "指定医薬部外品", "医薬費控除対象品", "つめかえ用", "本体"]

# ========================================================
# 2. Firebase / Azure AI Agent 初期化
# ========================================================
db = None
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
        logger.info("✅ Firebase 初期化成功")
    db = firestore.client()
except Exception as e:
    logger.error(f"❌ Firebase 初期化失敗: {e}")

def init_agent_client():
    try:
        project_client = AIProjectClient(
            credential=AzureCliCredential(),
            endpoint=AZURE_PROJECT_ENDPOINT
        )
        agent = project_client.agents.get_agent(AGENT_ID)
        logger.info("✅ Azure AI Agent 初期化成功")
        return project_client, agent
    except Exception as e:
        logger.error(f"❌ Agent 初期化失敗: {e}")
        return None, None

project_client, agent = init_agent_client()

# ========================================================
# 3. ユーティリティ (共通処理)
# ========================================================
def fetch_text(url: str) -> str:
    """指定URLの本文テキストを取得（User-Agent偽装付き）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        res = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        for script in soup(["script", "style"]):
            script.decompose()
        text = soup.get_text(" ", strip=True)
        return text[:5000]
    except Exception:
        return ""

# ========================================================
# 4. データ収集 (Source Gathering)
# ========================================================
def check_kao_website() -> List[Dict]:
    """花王サイトから製造終了品を取得（タグ分割・行単位解析版）"""
    logger.info("🧴 花王公式サイト解析開始...")
    try:
        res = requests.get(KAO_URL, impersonate="chrome120", timeout=15)
        if res.status_code != 200:
            logger.error(f"❌ サイトアクセス失敗: {res.status_code}")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        
        # 【重要】separator='\n' を指定して、タグの境界で必ず改行させる
        # これにより「製造終了予定品」と「商品名」が連結されるのを防ぐ
        all_text = soup.get_text(separator='\n', strip=True)
        
        # 行ごとに分割してリスト化
        lines = [line.strip() for line in all_text.split('\n') if line.strip()]

        items = []
        now = datetime.now()
        start_date = datetime(now.year - 1, now.month, 1) # 1年前
        
        current_period = None
        is_period_valid = False
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            # --- 1. 日付見出しの判定 ---
            date_match = re.search(r'(\d{4})年(\d{1,2})月', line)
            if date_match and len(line) < 20:
                year = int(date_match.group(1))
                month = int(date_match.group(2))
                try:
                    check_date = datetime(year, month, 1)
                    if check_date >= start_date:
                        current_period = f"{year}年{month}月"
                        is_period_valid = True
                    else:
                        is_period_valid = False
                except ValueError:
                    is_period_valid = False
                i += 1
                continue

            if not is_period_valid:
                i += 1
                continue

            # --- 2. 商品抽出ロジック ---
            if "製造終了" in line:
                found_product = False
                for offset in range(1, 6):
                    if i + offset >= len(lines): break
                    candidate = lines[i + offset]
                    
                    if "製造終了" in candidate or re.search(r'\d{4}年', candidate):
                        break
                    
                    if candidate in IGNORE_TAGS: continue
                    if any(ignore in candidate for ignore in IGNORE_TEXTS): continue
                    
                    is_category = False
                    if "・" in candidate: is_category = True
                    if candidate.endswith(("洗剤", "ハンドソープ", "シート", "用品", "ケア", "マスク", "オムツ", "パッド", "剤")):
                        if not any(brand in candidate for brand in KAO_BRANDS):
                            is_category = True
                    
                    if is_category:
                        continue

                    is_likely_product = False
                    if any(brand in candidate for brand in KAO_BRANDS):
                        is_likely_product = True
                    elif len(candidate) > 5:
                        is_likely_product = True

                    if is_likely_product:
                        name = candidate
                        name = re.sub(r'限定品|医薬部外品|eco|つめかえ用|本体|除菌', '', name).strip()
                        
                        title = f"【花王公式】{name} ({current_period}終了)"
                        
                        if not any(item['title'] == title for item in items):
                            items.append({
                                "title": title,
                                "link": KAO_URL,
                                "pub_date": current_period,
                                "raw_name": name
                            })
                        found_product = True
                        break 
                
                if found_product:
                    pass
            
            i += 1

        logger.info(f"✅ 花王から {len(items)} 件の対象商品を検出")
        return items

    except Exception as e:
        logger.error(f"❌ スクレイピングエラー: {e}")
        return []

def get_google_news_topics(keyword: str) -> List[Dict]:
    """GoogleニュースRSSからキーワードに合致するニュースを取得"""
    encoded_query = urllib.parse.quote(keyword)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ja&gl=JP&ceid=JP:ja"
    news_items = []
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.content, "xml")
        for item in soup.find_all("item"):
            title = item.title.text if item.title else ""
            if any(k in title for k in DAILY_GOODS_KEYWORDS):
                news_items.append({
                    "title": title,
                    "link": item.link.text if item.link else "",
                    "pub_date": item.pubDate.text if item.pubDate else ""
                })
            if len(news_items) >= 5: break
    except Exception as e:
        logger.error(f"❌ Googleニュースエラー: {e}")
    return news_items

# ========================================================
# 5. 詳細調査 (Market Enrichment)
# ========================================================
def get_yahoo_auction_stats(keyword: str) -> Dict:
    """ヤフオクで落札相場(closedsearch)を検索し、上位5件の価格統計を取得"""
    # 検索用にキーワードをクリーニング
    clean_keyword = re.sub(r"【.*?】|\(.*?\)|製造終了.*?品|限定品|医薬部外品|指定医薬部外品|除菌|eco", "", keyword)
    clean_keyword = re.sub(r"\d+(ml|mL|g|G|枚|個)", "", clean_keyword)
    clean_keyword = clean_keyword.strip()

    if len(clean_keyword) < 2:
        return {"error": "キーワード不十分", "keyword": clean_keyword}

    logger.info(f"💰 ヤフオク落札相場チェック: {clean_keyword}")
    encoded = urllib.parse.quote_plus(clean_keyword)
    # 落札相場URLに変更: closedsearch
    url = f"https://auctions.yahoo.co.jp/closedsearch/closedsearch?p={encoded}&b=1&n=50&mode=1"

    items_data = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 落札相場ページでも商品リストのクラス名は概ね "Product" 系
        products = soup.find_all("li", class_="Product")

        for product in products[:5]:
            title_tag = product.find("a", class_="Product__titleLink")
            if not title_tag: continue
            
            # 落札価格 (開催中とクラス名は共通の場合が多いが、念のため)
            price_tag = product.find("span", class_="Product__priceValue")
            price_str = price_tag.text.strip().replace("円", "").replace(",", "") if price_tag else "0"
            try:
                price = int(float(price_str))
            except ValueError:
                price = 0

            items_data.append({"name": title_tag.text.strip(), "price": price})

    except Exception as e:
        logger.error(f"❌ ヤフオクエラー: {e}")
        return {"error": str(e)}

    prices = [i["price"] for i in items_data if i["price"] > 0]
    avg_price = int(mean(prices)) if prices else 0
    
    return {
        "keyword": clean_keyword,
        "total_hits": len(products),
        "avg_price": avg_price,
        "items": items_data
    }

# ========================================================
# 6. 分析 (AI Analysis)
# ========================================================
def analyze_profit_margin(item: Dict, auction_data: Dict) -> Dict:
    """Azure AI Agentによる定価調査と利ザヤ判定"""
    if not project_client or not agent: return {"error": "Agent未初期化"}
    
    product_query = item.get('raw_name') or item['title']
    logger.info(f"🤖 利ザヤ分析開始: {product_query[:30]}")
    
    try:
        thread = project_client.agents.threads.create()
        prompt = f"""
以下の製造終了商品について、Bing Searchを使用して「希望小売価格（定価）」を調査し、ヤフオク相場と比較して「利ザヤ」が出るか判定してください。

【調査対象】
商品名: {product_query}
ヤフオク落札平均価格: {auction_data.get('avg_price', 0)}円
ヤフオク落札データ数: {auction_data.get('total_hits', 0)}件

【判定手順】
1. Bing Searchでこの商品の正確な「定価(税込)」を特定してください。
2. 定価とヤフオク落札平均価格を比較してください。
3. 販売手数料(10%)と送料を考慮し、利益が出るか評価してください。

JSONのみで出力:
{{
  "product_name": "特定した正式名称",
  "retail_price": "調査した定価(円)",
  "market_price": "ヤフオク落札平均(円)",
  "profit_margin": "推定利益額(円)",
  "judgment": "高 / 中 / 低 / なし",
  "analysis": "理由（例：定価の2倍で取引されており需要過多）"
}}
"""
        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        
        messages = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
        for m in messages:
            if m.role == "assistant" and m.text_messages:
                content = m.text_messages[0].text.value
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if match: return json.loads(match.group())
        return {"error": "AI分析失敗"}
    except Exception as e:
        return {"error": str(e)}

# ========================================================
# 7. 保存 (Storage)
# ========================================================
def save_to_firestore(source: str, topic: Dict, report: Dict, auction_data: Dict):
    """結果を保存"""
    if not db: return
    try:
        # 1. 監視ログとして保存
        ref = db.collection(FIRESTORE_COLLECTION)
        existing = ref.where("title", "==", topic["title"]).limit(1).get()
        if list(existing): return

        doc = {
            "source": source,
            "title": topic["title"],
            "url": topic["link"],
            "analysis": report,
            "market_stats": {
                "avg_price": auction_data.get("avg_price"),
                "total_hits": auction_data.get("total_hits")
            },
            "created_at": datetime.utcnow()
        }
        ref.add(doc)
        logger.info(f"💾 監視ログ保存完了: {topic['title'][:20]}")

        # 2. 利益が高い場合は「仕入れリスト」にも保存
        judgment = report.get("judgment", "")
        if judgment == "高":
            purchase_ref = db.collection(FIRESTORE_PURCHASE_LIST_COLLECTION)
            # 仕入れリスト側でも重複チェック
            existing_purchase = purchase_ref.where("title", "==", topic["title"]).limit(1).get()
            
            if not list(existing_purchase):
                purchase_doc = {
                    "source": source,
                    "title": topic["title"],
                    "product_name": report.get("product_name"),
                    "url": topic["link"],
                    "analysis": report,
                    "market_stats": {
                        "avg_price": auction_data.get("avg_price"),
                        "total_hits": auction_data.get("total_hits")
                    },
                    "profit_estimate": report.get("profit_margin"),
                    "status": "未仕入れ", # ステータス管理用
                    "created_at": datetime.utcnow()
                }
                purchase_ref.add(purchase_doc)
                logger.info(f"💰 仕入れリストに追加: {topic['title'][:20]}")

    except Exception as e:
        logger.error(f"❌ 保存失敗: {e}")

# ========================================================
# 8. メイン実行 (Main)
# ========================================================
def main():
    logger.info("🚀 監視・利ザヤ分析エンジン起動")

    # 1. 花王公式サイト
    kao_list = check_kao_website()
    total_kao = len(kao_list)
    logger.info(f"📋 花王リスト取得完了: 全{total_kao}件")

    for i, item in enumerate(kao_list, 1):
        logger.info(f"▶️ 処理中 [{i}/{total_kao}]: {item['title'][:20]}...")
        
        # 相場取得 (落札相場)
        stats = get_yahoo_auction_stats(item['raw_name'])
        # AI分析
        report = analyze_profit_margin(item, stats)
        # 保存
        save_to_firestore("花王公式", item, report, stats)
        
        print(f"\n【花王】{report.get('product_name')}")
        print(f"定価: {report.get('retail_price')}円 / ヤフオク落札平均: {report.get('market_price')}円")
        print(f"利益判定: {report.get('judgment')} ({report.get('profit_margin')}円)")
        time.sleep(2)

    # 2. Googleニュース
    for keyword in DAILY_GOODS_KEYWORDS:
        query = f"{keyword} 生産終了 OR 終売"
        news_list = get_google_news_topics(query)
        total_news = len(news_list)
        logger.info(f"📋 ニュース取得完了 ({keyword}): 全{total_news}件")

        for i, topic in enumerate(news_list, 1):
            if any(d in topic["link"] for d in IGNORE_DOMAINS): continue
            
            logger.info(f"▶️ 処理中 [{i}/{total_news}]: {topic['title'][:20]}...")

            # 本文取得
            body = fetch_text(topic["link"])
            if not DISCONTINUE_PATTERN.search(topic["title"] + body): continue

            # 相場取得 (落札相場)
            stats = get_yahoo_auction_stats(topic['title'])
            # AI分析
            report = analyze_profit_margin(topic, stats)
            # 保存
            save_to_firestore("ニュース", topic, report, stats)
            
            print(f"\n【ニュース】{topic['title'][:30]}")
            print(f"判定: {report.get('judgment')}")
            time.sleep(2)

    logger.info("✅ 監視完了")

if __name__ == "__main__":
    main()