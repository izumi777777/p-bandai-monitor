import os
import time
import json
import logging
import re
import datetime
import warnings
from dotenv import load_dotenv

# 通信ライブラリ
try:
    from curl_cffi import requests
except ImportError:
    import requests

from bs4 import BeautifulSoup

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore

# Azure AI Agent
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

# ========================================================
# 1. 設定エリア
# ========================================================
# 動作モード: "TECH" (技術書・オライリー) または "COMIC" (漫画全巻)
# SEARCH_MODE = "TECH"
SEARCH_MODE = "COMIC"

load_dotenv()

# --- ログ設定 (ここを修正してノイズを消しました) ---
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# Azureや通信ライブラリの大量のログを黙らせる設定
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Windowsでの文字化け対策 (UTF-8強制)
for handler in logging.root.handlers:
    if hasattr(handler, 'setFormatter'):
        handler.setStream(open(1, mode='w', encoding='utf-8', closefd=False))

logger = logging.getLogger(__name__)

# 環境変数
AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")
FIREBASE_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
FIRESTORE_COLLECTION = "sales_candidates"

# ========================================================
# 2. 初期化処理
# ========================================================
db = None
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    logger.error(f"Firebase 初期化失敗: {e}")

project_client = None
agent = None

def init_agent_client():
    try:
        if not AZURE_PROJECT_ENDPOINT or not AGENT_ID:
            return None, None
        client = AIProjectClient(
            credential=AzureCliCredential(),
            endpoint=AZURE_PROJECT_ENDPOINT
        )
        ag = client.agents.get_agent(AGENT_ID)
        # 初期化成功ログも邪魔ならコメントアウトしてください
        # logger.info(f"Azure AI Agent 準備OK (Mode: {SEARCH_MODE})")
        return client, ag
    except Exception as e:
        logger.error(f"Agent 初期化失敗: {e}")
        return None, None

project_client, agent = init_agent_client()

# ========================================================
# 3. AI解析ロジック
# ========================================================
def analyze_title_with_ai(raw_title: str) -> dict:
    if not project_client or not agent:
        return {"clean_title": None, "volume_count": 0, "edition": "通常版"}

    # 解析開始ログを削除 (スッキリさせるため)
    # logger.info(f"AI解析開始: {raw_title[:30]}...")

    try:
        thread = project_client.agents.threads.create()
        
        # モードに応じたプロンプト生成
        if SEARCH_MODE == "TECH":
            prompt = f"""
あなたは技術書せどりのプロです。以下のヤフオク出品タイトルを分析してください。
技術書は「第何版か」で価格が大きく変わるため、版数を正確に特定してください。

【対象タイトル】
{raw_title}

【抽出ルール】
1. clean_title: 書名のみ（"オライリー"などの出版社名は含めるが、"まとめ"等は除く）。
2. volume_count: 出品されている冊数。
3. edition: 最重要。「第2版」「3rd Edition」「改訂版」などを抽出。不明なら「不明」。
4. is_complete: まとめ売りの場合は false。単一書籍の場合は true。

【出力形式: JSONのみ】
{{
  "clean_title": "書名",
  "volume_count": 1,
  "edition": "第3版",
  "is_complete": true
}}
"""
        else:
            prompt = f"""
あなたは古本せどりのプロです。以下のヤフオク出品タイトルを分析してください。
特に「ワイド版」「文庫版」などの版の違いを厳格に判定してください。

【対象タイトル】
{raw_title}

【抽出ルール】
1. clean_title: 作品名のみ。
2. volume_count: 出品されている冊数。
3. edition: 「通常版」「ワイド版」「文庫版」「愛蔵版」などを抽出。不明なら「通常版」。
4. is_complete: 全巻揃いなら true、不揃いなら false。

【出力形式: JSONのみ】
{{
  "clean_title": "作品名",
  "volume_count": 0,
  "edition": "通常版",
  "is_complete": true
}}
"""

        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        run = project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        
        # 完了待ちループはSDKが処理済みと仮定
        messages = project_client.agents.messages.list(thread_id=thread.id, order="desc")
        
        for m in messages:
            if m.role == "assistant" and m.content:
                text_val = m.content[0].text.value
                match = re.search(r"\{.*\}", text_val, re.DOTALL)
                if match:
                    return json.loads(match.group())
        
        return {"clean_title": None}

    except Exception as e:
        logger.error(f"AI解析エラー: {e}")
        return {"clean_title": None}

# ========================================================
# 4. ヤフオク検索パラメータ
# ========================================================
def get_search_params():
    if SEARCH_MODE == "TECH":
        # オライリー等はカテゴリ「すべて(0)」で検索するのが最も確実
        return {
            "p": "オライリー", 
            "auccat": "0",  # すべてのカテゴリ
            "istatus": "2", # 落札相場
            "n": "50"       # 取得件数
        }
    else:
        # 漫画(21600)カテゴリ
        return {"p": "全巻セット", "auccat": "21600", "istatus": "2", "n": "20"}

# ========================================================
# 5. メイン処理
# ========================================================
def fetch_and_save():
    if db is None: return

    base_url = "https://auctions.yahoo.co.jp/closedsearch/closedsearch"
    params = get_search_params()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://auctions.yahoo.co.jp/"
    }

    # logger.info(f"ヤフオク検索開始 (Mode: {SEARCH_MODE})...")
    
    try:
        # curl_cffi対応
        if 'curl_cffi' in requests.__name__:
            res = requests.get(base_url, params=params, headers=headers, impersonate="chrome110", timeout=15)
        else:
            res = requests.get(base_url, params=params, headers=headers, timeout=15)

        soup = BeautifulSoup(res.content, "html.parser")
        
        # セレクタ（複数のパターンに対応）
        items = soup.select(".Product") or soup.select(".ProductItem") or soup.select("li.Product")
        
        # logger.info(f"ヒット件数: {len(items)}件")
        
        if len(items) == 0:
            logger.warning("商品が見つかりませんでした (0件)")
            return

        save_count = 0

        for item in items:
            title_tag = item.select_one(".Product__titleLink") or item.select_one(".ProductItem__titleLink") or item.select_one("a.Product__title")
            price_tag = item.select_one(".Product__priceValue") or item.select_one(".ProductItem__priceValue") or item.select_one(".Product__price")

            if not title_tag or not price_tag: continue
            
            full_title = title_tag.text.strip()
            item_url = title_tag.get("href")
            
            price_text = price_tag.text.replace(",", "").strip()
            match = re.search(r'\d+', price_text)
            if not match: continue
            price = int(match.group())

            if price < 1500: continue 

            # AI解析
            if project_client:
                ai_res = analyze_title_with_ai(full_title)
                
                if ai_res.get("clean_title"):
                    final_title = ai_res["clean_title"]
                    final_vol = ai_res.get("volume_count", 0)
                    edition = ai_res.get("edition", "通常版")
                    is_complete = ai_res.get("is_complete", False)
                    
                    # 保存用ID生成
                    safe_title = re.sub(r'[\\/:*?"<>| ]', '', final_title)
                    doc_id = f"{SEARCH_MODE}_{safe_title}_{final_vol}"
                    
                    data = {
                        "title": final_title,
                        "edition": edition,
                        "volume_count": final_vol,
                        "is_complete": is_complete,
                        "yahoo_price": price,
                        "original_title": full_title,
                        "url": item_url,
                        "category_type": SEARCH_MODE,
                        "status": "unprocessed",
                        "updated_at": datetime.datetime.now()
                    }
                    
                    db.collection(FIRESTORE_COLLECTION).document(doc_id).set(data, merge=True)
                    
                    # ★ここでログ出力（ここだけ出す）
                    logger.info(f"保存: {final_title} [{edition}] -> {price}円")
                    
                    save_count += 1
                    time.sleep(1) 

        # 最後に合計件数だけ出す
        if save_count > 0:
            logger.info(f"処理完了: 計{save_count}件を保存しました")
        else:
            logger.info("保存対象はありませんでした")

    except Exception as e:
        logger.error(f"エラー: {e}")

if __name__ == "__main__":
    fetch_and_save()