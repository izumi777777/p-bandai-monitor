import os
import json
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import ListSortOrder

app = Flask(__name__)

load_dotenv()  # これで .env の内容が os.environ に読み込まれます
# ==========================
# 認証・設定
# ==========================
# Entra ID (Service Principal) 認証用の環境変数設定
# ローカル実行時は .env や直接環境変数にセットしてください
AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# DefaultAzureCredential は自動で以下の環境変数を読み取ります
# os.getenv("AZURE_TENANT_ID")
# os.getenv("AZURE_CLIENT_ID")
# os.getenv("AZURE_CLIENT_SECRET") 


# --- ロック機能の設定 ---
TRIAL_DAYS = 7
TRIAL_FILE = "trial_info.json"

# ==========================
# Azure AI Project Client (初期化)
# ==========================
# DefaultAzureCredential は環境変数 (AZURE_CLIENT_ID等) を自動的に読み込みます
project = AIProjectClient(
    credential=DefaultAzureCredential(),
    endpoint=AZURE_PROJECT_ENDPOINT,
)

# エージェントの取得はリクエスト毎、あるいはグローバルで行う
agent = project.agents.get_agent(AGENT_ID)

# ==========================
# ユーティリティ
# ==========================

def check_trial_status():
    """7日間の試用期間をチェックするロジック"""
    now = datetime.now()
    if not os.path.exists(TRIAL_FILE):
        # 初回起動時に開始日を保存
        start_info = {"start_date": now.strftime("%Y-%m-%d %H:%M:%S")}
        with open(TRIAL_FILE, "w") as f:
            json.dump(start_info, f)
        return True, 0

    with open(TRIAL_FILE, "r") as f:
        start_info = json.load(f)
    
    start_date = datetime.strptime(start_info["start_date"], "%Y-%m-%d %H:%M:%S")
    expiry_date = start_date + timedelta(days=TRIAL_DAYS)
    
    if now > expiry_date:
        return False, (now - expiry_date).days
    return True, (expiry_date - now).days

def get_grounding_info(url):
    """Geminiを使用してURLの最新情報を検索する (一次情報の取得)"""
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [{
                "text": f"現在の日時: {datetime.now().strftime('%Y年%m月%d日')}。以下のプレミアムバンダイURLの商品詳細（商品名、作品名、価格、現在の在庫状況、最新の発送月）を検索して、テキストで詳しく報告してください: {url}"
            }]
        }],
        "tools": [{"google_search": {}}]
    }
    try:
        response = requests.post(gemini_url, json=payload, timeout=30)
        data = response.json()
        return data['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        return f"Search error: {str(e)}"

# ==========================
# 画面
# ==========================
@app.route("/")
def index():
    return render_template("index.html")

# ==========================
# API
# ==========================
@app.route("/api/monitor", methods=["POST"])
def monitor_item():
    # 1. トライアル期間のチェック
    is_active, diff = check_trial_status()
    if not is_active:
        return jsonify({
            "error": "TRIAL_EXPIRED",
            "message": f"試用期間（{TRIAL_DAYS}日間）が終了しました。継続利用にはライセンスキーが必要です。",
            "expired_days_ago": diff
        }), 403

    data = request.json or {}
    target_url = data.get("url")

    if not target_url:
        return jsonify({"error": "URL is required"}), 400

    try:
        # 2. 検索実行（Grounding一次情報の取得）
        search_context = get_grounding_info(target_url)

        # 3. Thread 作成
        thread = project.agents.threads.create()

        # 4. Agent への指示
        prompt = f"""
あなたは「プレミアムバンダイ」の在庫データ抽出器です。
以下のルールに従って、Bing検索（Grounding）を必ず使用し、今日現在のリアルタイム最新情報のみで回答してください。

-------------------------------------------------------------------
【対象URL】
{target_url}

まず URL から商品ID（例: item-1000230286）を正確に抽出し、
その商品IDのみを使って Bing検索を行うこと。

【必須条件（厳守）】
1. 過去の学習データや一般的な説明は一切含めない。
2. URL内の商品IDをキーに Bing検索を実施すること。
3. 見た目やシリーズ名で決め打ちしない。
4. 古い情報があれば再検索すること。
5. 在庫状況は「予約受付中」または「在庫なし」で断定する。
6. 以下のクエリをベースに検索すること：
    "<商品ID> プレミアムバンダイ 在庫 発送予定"

【提供された検索のヒント（Grounding Data）】
{search_context}
-------------------------------------------------------------------

【出力形式（JSONのみ）】
{{
  "調査日時": "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
  "商品名": "<特定された正確な商品名>",
  "作品名": "<特定された正確な作品名>",
  "価格（税込）": "<数値+円>",
  "現在のステータス": "<予約受付中 / 在庫なし>",
  "発送予定月": "<特定された最新の発送月>",
  "商品URL": "{target_url}"
}}

※ JSON以外は一切出力しないこと。
※ 情報が見つからない場合はすべて "不明" にすること。
"""

        # メッセージ送信
        project.agents.messages.create(
            thread_id=thread.id,
            role="user",
            content=prompt,
        )

        # 実行
        run = project.agents.runs.create_and_process(
            thread_id=thread.id,
            agent_id=agent.id,
        )

        if run.status == "failed":
            return jsonify({"error": "Agent execution failed", "detail": run.last_error}), 500

        # 結果取得
        messages = project.agents.messages.list(
            thread_id=thread.id,
            order=ListSortOrder.DESCENDING,
        )

        raw_text = None
        for message in messages:
            if message.role == "assistant" and message.text_messages:
                raw_text = message.text_messages[0].text.value
                break

        if not raw_text:
            return jsonify({"error": "Assistant returned no message"}), 500

        # JSON抽出処理
        try:
            start_idx = raw_text.find('{')
            end_idx = raw_text.rfind('}')
            if start_idx != -1 and end_idx != -1:
                clean_json = raw_text[start_idx:end_idx+1]
                result = json.loads(clean_json)
                return jsonify({
                    "thread_id": thread.id,
                    "result": result
                })
            else:
                return jsonify({"error": "Invalid format", "raw": raw_text}), 500
        except json.JSONDecodeError as e:
            return jsonify({
                "error": "JSON parse error",
                "raw": raw_text,
                "detail": str(e)
            }), 500

    except Exception as e:
        return jsonify({
            "error": "Agent execution error",
            "detail": str(e),
        }), 500

@app.route("/api/query", methods=["POST"])
def query_agent():
    """既存のスレッドに対して追加の質問を行うエンドポイント"""
    data = request.json or {}
    thread_id = data.get("thread_id")
    user_query = data.get("query")

    if not thread_id or not user_query:
        return jsonify({"error": "Thread ID and query are required"}), 400

    try:
        # メッセージの作成
        project.agents.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"以下の質問について、これまでの文脈とBing検索を用いて回答してください: {user_query}"
        )

        # 実行
        run = project.agents.runs.create_and_process(
            thread_id=thread_id,
            agent_id=agent.id,
        )

        if run.status == "failed":
            return jsonify({"error": "Query execution failed", "detail": run.last_error}), 500

        # 回答の取得
        messages = project.agents.messages.list(
            thread_id=thread_id,
            order=ListSortOrder.DESCENDING,
        )

        reply_text = None
        for message in messages:
            if message.role == "assistant" and message.text_messages:
                reply_text = message.text_messages[0].text.value
                break

        if not reply_text:
            return jsonify({"error": "No reply from agent"}), 500

        return jsonify({"reply": reply_text})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)