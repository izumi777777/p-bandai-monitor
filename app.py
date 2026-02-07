import os
import json
import logging
import re
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import ListSortOrder
from dotenv import load_dotenv
from curl_cffi import requests # 追加
from bs4 import BeautifulSoup # 追加

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Azure設定 (変更なし) ---
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")

project_client = AIProjectClient(credential=DefaultAzureCredential(), endpoint=AZURE_PROJECT_ENDPOINT)
agent = project_client.agents.get_agent(AGENT_ID)

# ==========================
# 1. 一次情報取得ロジック（マージ部分）
# ==========================
def scrape_premium_bandai(url):
    try:
        response = requests.get(url, impersonate="chrome120", timeout=10)
        if response.status_code != 200: return None
        html = response.text

        # 1. 商品名の抽出 (titleタグから取得するのが最も確実です)
        title_match = re.search(r'<title>(.*?) \|', html)
        product_name = title_match.group(1) if title_match else "不明"

        # 2. 価格の抽出 (dataLayer内から取得)
        price_match = re.search(r"price: '(\d+)'", html)
        price = price_match.group(1) if price_match else "不明"

        # 3. 画像URLの抽出
        img_match = re.search(r'"0000000000_img":"(.*?)"', html)
        img_url = img_match.group(1) if img_match else None

        # 4. 在庫状況
        stock_match = re.search(r'orderstock_list = \{.*?"(.*?)":"(.*?)"', html, re.DOTALL)
        available = (stock_match and stock_match.group(2) == "○")

        # 5. 最大購入数
        max_match = re.search(r'ordermax_list = \{.*?"(.*?)":(\d+)', html, re.DOTALL)
        max_qty = max_match.group(2) if max_match else "不明"

        return {
            "product_name": product_name,
            "price": f"{price}円",
            "available": available,
            "max_qty": max_qty,
            "image_url": img_url,
            "raw_status": "在庫あり" if available else "在庫なし"
        }
    except Exception as e:
        logger.error(f"Scraping Error: {e}")
        return None
# ==========================
# 2. Agent呼び出し（スクレイピング結果を渡すよう修正）
# ==========================
def get_stock_status_via_agent(url: str):
    item_id = re.search(r'item-\d+', url).group(0) if re.search(r'item-\d+', url) else "Unknown"
    
    # --- 先にスクレイピングを実行 ---
    scraped_data = scrape_premium_bandai(url)
    scraped_info_str = "情報取得失敗"
    img_url = ""
    if scraped_data:
        img_url = scraped_data['image_url']
        scraped_info_str = f"在庫状況: {scraped_data['raw_status']}, 最大選択可能数: {scraped_data['max_qty']}, 画像URL: {img_url}"

    try:
        thread = project_client.agents.threads.create()
        
        # プロンプトにスクレイピングした「一次情報」を注入
        prompt = f"""
       以下のシステム取得情報を【そのまま】JSONに反映してください。
検索結果よりも、この情報を最優先してください。

- 商品名: {scraped_data['product_name']}
- 価格: {scraped_data['price']}
- 在庫: {scraped_data['raw_status']}
- 最大数: {scraped_data['max_qty']}
- 画像: {scraped_data['image_url']}

【回答形式】必ず以下のJSONのみ
{{
  "調査日時": "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
  "available": {str(scraped_data['available']).lower()},
  "商品名": "{scraped_data['product_name']}",
  "価格（税込）": "{scraped_data['price']}",
  "発送予定月": "商品ページを確認してください",
  "現在のステータス": "{scraped_data['raw_status']}",
  "最大在庫数": "{scraped_data['max_qty']}",
  "商品画像": "{scraped_data['image_url']}",
  "商品URL": "{url}"
}}
        """

        project_client.agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        run = project_client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)

        # (中略: エージェントの応答取得ロジックは元のコードと同じ)
        messages = project_client.agents.messages.list(thread_id=thread.id, order=ListSortOrder.DESCENDING)
        raw_text = ""
        for message in messages:
            if message.role == "assistant" and message.text_messages:
                raw_text = message.text_messages[0].text.value
                break

        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group()), thread.id
        
        return None, None
    except Exception as e:
        logger.error(f"Agent Error: {e}")
        return None, None

# --- Flask Routes ---
@app.route("/api/monitor", methods=["POST"])
def monitor_item():
    data = request.json
    url = data.get("url")
    result, thread_id = get_stock_status_via_agent(url)
    
    if not result:
        return jsonify({"error": "解析失敗"}), 500

    # UIに画像URLも渡すように追加
    response_data = {
        "item_name": result.get("商品名"),
        "status": result.get("現在のステータス"),
        "shipping": result.get("発送予定月"),
        "available": result.get("available"),
        "image_url": result.get("商品画像"), # 追加
        "max_qty": result.get("最大在庫数"), # 追加
        "thread_id": thread_id,
        "result": result
    }
    return jsonify(response_data)

# ==========================
# 3. 画面表示と追加質問のルート
# ==========================

@app.route("/")
def index():
    # templates/index.html を読み込んで表示する
    return render_template("index.html")

@app.route("/api/query", methods=["POST"])
def query_agent():
    data = request.json or {}
    thread_id = data.get("thread_id")
    user_query = data.get("query")

    if not thread_id or not user_query:
        return jsonify({"error": "Thread ID and query are required"}), 400

    try:
        project_client.agents.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_query
        )

        run = project_client.agents.runs.create_and_process(
            thread_id=thread_id,
            agent_id=agent.id,
        )

        messages = project_client.agents.messages.list(thread_id=thread_id, order=ListSortOrder.DESCENDING)

        reply_text = ""
        for message in messages:
            if message.role == "assistant" and message.text_messages:
                reply_text = message.text_messages[0].text.value
                break

        return jsonify({"reply": reply_text or "回答が得られませんでした。"})
    except Exception as e:
        logger.error(f"Query Agent Error: {e}")
        return jsonify({"error": str(e)}), 500

# ==========================
# 4. サーバー起動
# ==========================
if __name__ == "__main__":
    # debug=True にすると、コードを書き換えたときに自動で再起動してくれるので便利です
    app.run(debug=True, port=5000)