import os
import time
import json
import logging
import re
import urllib.parse
from datetime import datetime
from dotenv import load_dotenv

# 通信ライブラリ (Bot対策)
try:
    from curl_cffi import requests
except ImportError:
    import requests

from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

# Azure AI Agent
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

# ========================================================
# 設定エリア
# ========================================================
load_dotenv()

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# ログ抑制 (Azureなどの通信ログを消す)
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# 文字コード対策 (Windows)
for handler in logging.root.handlers:
    if hasattr(handler, 'setFormatter'):
        handler.setStream(open(1, mode='w', encoding='utf-8', closefd=False))

logger = logging.getLogger(__name__)

FIREBASE_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
FIRESTORE_COLLECTION = "sales_candidates"
AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")

# メルカリ用ヘッダー
MERCARI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://jp.mercari.com/",
}

# ========================================================
# 初期化
# ========================================================
db = None
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    logger.error(f"Firebase 初期化失敗: {e}")

# Azure Agent 初期化
project_client = None
agent = None
if AZURE_PROJECT_ENDPOINT and AGENT_ID:
    try:
        project_client = AIProjectClient.from_connection_string(
            credential=AzureCliCredential(),
            conn_str=AZURE_PROJECT_ENDPOINT
        )
        agent = project_client.agents.get_agent(AGENT_ID)
    except Exception as e:
        logger.warning(f"Azure Agent Init Error: {e}")

# ========================================================
# 1. メルカリ検索 (再帰的JSON探索版)
# ========================================================
def find_items_recursive(data):
    """
    JSONデータの中から、商品リスト('items')を再帰的に探し出す関数
    """
    if isinstance(data, dict):
        for k, v in data.items():
            # "items" というキーで、リストであり、中身があり、要素に "price" があるものを探す
            if k == 'items' and isinstance(v, list) and len(v) > 0:
                first_item = v[0]
                if isinstance(first_item, dict) and ('price' in first_item or 'price_yen' in first_item):
                    return v
            
            # さらに深く探す
            result = find_items_recursive(v)
            if result: return result
            
    elif isinstance(data, list):
        for item in data:
            result = find_items_recursive(item)
            if result: return result
            
    return None

def search_mercari_cheapest(keyword):
    """
    メルカリで「販売中」の最安値アイテムを取得する (Bot対策対応版)
    """
    encoded_kw = urllib.parse.quote(keyword)
    # status=on_sale (販売中), sort=price_asc (安い順)
    url = f"https://jp.mercari.com/search?keyword={encoded_kw}&status=on_sale&sort=price_asc"

    try:
        if 'curl_cffi' in requests.__name__:
            res = requests.get(url, headers=MERCARI_HEADERS, impersonate="chrome110", timeout=20)
        else:
            res = requests.get(url, headers=MERCARI_HEADERS, timeout=20)

        if res.status_code != 200:
            logger.warning(f"メルカリ アクセス制限 (Status: {res.status_code})")
            return None

        soup = BeautifulSoup(res.content, "html.parser")
        
        # __NEXT_DATA__ を取得
        script = soup.find("script", id="__NEXT_DATA__")
        if not script: 
            # データがない場合はHTMLからクラス名で探す予備ロジックを入れても良いが、
            # 最近のメルカリはJSレンダリングが主なので厳しい
            logger.warning("メルカリ: 商品データ(JSON)が見つかりません")
            return None

        data = json.loads(script.string)
        
        # 万能検索ロジック実行
        items = find_items_recursive(data)

        if not items:
            return None

        # 最安の1件を取得
        cheapest_item = items[0]
        
        # IDや価格のキー揺らぎに対応
        item_id = cheapest_item.get("id") or cheapest_item.get("mer_id")
        price = cheapest_item.get("price") or cheapest_item.get("price_yen")
        
        if not item_id or not price:
            return None
            
        return {
            "price": int(price),
            "name": cheapest_item.get("name", ""),
            "url": f"https://jp.mercari.com/item/{item_id}",
            "desc": cheapest_item.get("description", "")
        }

    except Exception as e:
        logger.error(f"Mercari Search Error: {e}")
        return None

# ========================================================
# 2. ネットオフ検索機能 (技術書用)
# ========================================================
def search_netoff_tech(title):
    url = "https://www.netoff.co.jp/cmdtyallsearch/result"
    try:
        # ネットオフもBot対策気味なのでヘッダー流用
        if 'curl_cffi' in requests.__name__:
            res = requests.get(url, params={"q": title}, headers=MERCARI_HEADERS, impersonate="chrome110", timeout=15)
        else:
            res = requests.get(url, params={"q": title}, headers=MERCARI_HEADERS, timeout=15)
        
        soup = BeautifulSoup(res.content, "html.parser")
        items = soup.select(".list_area li")
        
        lowest = 999999
        found = False
        for item in items:
            price_el = item.select_one(".price")
            if price_el:
                p = int(re.sub(r'\D', '', price_el.text))
                if p < lowest:
                    lowest = p
                    found = True
        return lowest if found else None
    except:
        return None

# ========================================================
# 3. Azure AI 検証ロジック
# ========================================================
def validate_deal_with_ai(target_title, target_edition, mercari_item):
    if not project_client or not agent:
        return {"is_match": True, "reason": "AI未設定のためスキップ"}

    # logger.info(f"  🤖 AI検証中: {mercari_item['name'][:20]}...")

    prompt = f"""
あなたは古本せどりの検品担当です。
「探している本（ヤフオク落札品）」と「見つけた本（メルカリ出品）」が、
同一の価値を持つ商品（同じ版、同じ巻数）かどうか判定してください。

【探している本】
タイトル: {target_title}
版（Edition）: {target_edition}

【見つけた本（メルカリ）】
出品タイトル: {mercari_item['name']}
価格: {mercari_item['price']}円

【判定ルール】
- 版数が違う（例：探しているのが第3版なのに、出品が第2版）場合は false。
- 明らかにジャンク品（裁断済み、ボロボロ）なら false。
- 版数が不明だが、タイトルが一致しているなら true。

【出力形式 JSON】
{{
  "is_match": true,
  "reason": "判定理由"
}}
"""
    try:
        thread = project_client.agents.threads.create()
        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        run = project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        
        messages = project_client.agents.messages.list(thread_id=thread.id, order="desc")
        for m in messages:
            if m.role == "assistant" and m.content:
                text_val = m.content[0].text.value
                match = re.search(r"\{.*\}", text_val, re.DOTALL)
                if match:
                    return json.loads(match.group())
        
        return {"is_match": True, "reason": "AI解析失敗"}
    
    except Exception as e:
        logger.warning(f"AI Validation Error: {e}")
        return {"is_match": True, "reason": "エラー"}

# ========================================================
# 4. メイン処理
# ========================================================
def check_profits():
    if db is None: return

    docs = db.collection(FIRESTORE_COLLECTION).where("status", "==", "unprocessed").stream()
    
    logger.info("利益判定プロセス開始 (Mercari対応版)...")
    count = 0

    for doc in docs:
        data = doc.to_dict()
        title = data.get("title")
        edition = data.get("edition", "通常版")
        yahoo_price = data.get("yahoo_price", 0)
        volume = data.get("volume_count", 1)
        cat_type = data.get("category_type", "COMIC")
        
        logger.info(f"調査: {title} ({edition})")

        estimated_cost = 0
        note = ""
        found_url = ""
        
        # --- 技術書モード ---
        if cat_type == "TECH":
            time.sleep(2) 
            
            # 1. ネットオフ確認
            netoff_price = search_netoff_tech(title)
            
            if netoff_price:
                estimated_cost = netoff_price
                note = f"NetOffあり: {netoff_price}円"
                found_url = f"https://www.netoff.co.jp/cmdtyallsearch/?word={title}"
            else:
                # 2. メルカリ確認
                logger.info(f"  -> NetOff在庫なし。Mercari検索実行...")
                time.sleep(2)
                
                # ★修正箇所: 関数名を正しいものに変更
                mercari_data = search_mercari_cheapest(title)
                
                if mercari_data:
                    m_price = mercari_data["price"]
                    m_url = mercari_data["url"]
                    
                    # 利益が出そうな場合のみAIチェック
                    potential_profit = yahoo_price - (m_price + int(yahoo_price*0.1) + 500)
                    
                    if potential_profit > 1000:
                        validation = validate_deal_with_ai(title, edition, mercari_data)
                        if validation.get("is_match"):
                            estimated_cost = m_price
                            note = f"Mercari: {m_price}円 (AI認証済)"
                            found_url = m_url
                        else:
                            logger.warning(f"  -> AI却下: {validation.get('reason')}")
                            note = f"AI却下: {validation.get('reason')}"
                            estimated_cost = 999999 # 仕入れ対象外にする
                    else:
                        estimated_cost = m_price
                        note = f"Mercari: {m_price}円"
                        found_url = m_url
                else:
                    estimated_cost = 1000 # 仮定
                    note = "在庫なし(仮定1000円)"

        # --- 漫画モード ---
        else:
            estimated_cost = volume * 350
            note = "漫画(仮定)"

        # 利益計算
        fee = int(yahoo_price * 0.1)
        shipping = max(800, volume * 100) 
        profit = yahoo_price - (estimated_cost + fee + shipping)

        status = "unprofitable"
        if profit > 1000:
            status = "profitable"
            logger.info(f"  ★ 利益候補！ +{profit}円 [{note}]")
        else:
            logger.info(f"  -> 利益なし {profit}円")

        # Firestore更新
        doc.reference.update({
            "estimated_cost": estimated_cost,
            "url": found_url if found_url else data.get("url"), # URLを仕入れ先に更新
            "profit": profit,
            "status": status,
            "calc_note": note,
            "checked_at": datetime.now()
        })
        count += 1

    logger.info(f"完了: {count}件")

if __name__ == "__main__":
    check_profits()