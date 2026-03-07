import time
import os
import logging
import json
from typing import List, Dict

# 外部ライブラリ
import tweepy
from dotenv import load_dotenv
from openai import AzureOpenAI

# --- 設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# .env または直接指定の読み込み
load_dotenv("x.env")

# --- X API (Tweepy) 設定 ---
X_API_KEY = os.getenv("X_API_KEY", "LfTfTFl5UF63JLZqNSrGVtdi2")
X_API_SECRET = os.getenv("X_API_SECRET", "rI6rGlm9JyysO8GtWLNTW4qOPowy8Ob6vM2yvLj9RHHMggE4dT")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "1954903214811496448-vfP8TvJ9J1bv25e9TWkczaAQeRMSF7")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "UmYIP3ojK4X7OaHwie2y68hiGFi9GjwygJhGISTwJIoZC")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "") # 検索にはBearer Tokenが推奨される場合が多い

# --- Azure OpenAI 設定 ---
AOAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "https://test-ms-ai-japaneast-structure.openai.azure.com/")
AOAI_KEY = os.getenv("AZURE_OPENAI_API_KEY", "65c19de3f0d14f5290bd3eb167da655e")
AOAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# 各クライアントの初期化
def init_clients():
    try:
        # X Client (v2)
        x_client = tweepy.Client(
            bearer_token=X_BEARER_TOKEN if X_BEARER_TOKEN else None,
            consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET
        )
        logger.info("✅ X API クライアント初期化成功")
    except Exception as e:
        logger.error(f"❌ X API 初期化エラー: {e}")
        x_client = None

    try:
        aoai_client = AzureOpenAI(
            azure_endpoint=AOAI_ENDPOINT,
            api_key=AOAI_KEY,
            api_version="2024-05-01-preview"
        )
        logger.info("✅ Azure OpenAI クライアント初期化成功")
    except Exception as e:
        logger.error(f"❌ Azure OpenAI 初期化エラー: {e}")
        aoai_client = None
    
    return x_client, aoai_client

x_client, aoai_client = init_clients()

# --- 検索ロジック ---

def search_x_tweets(keyword: str, max_results: int = 10) -> List[Dict]:
    """
    X API v2を使用してツイートを検索する
    """
    if not x_client:
        logger.error("X API クライアントが利用不可能です。")
        return []

    logger.info(f"🔍 X(Twitter)検索実行: {keyword}")
    
    try:
        # search_recent_tweets: 直近7日間のツイートを検索
        # queryに '-is:retweet' を加えることでリツイートを除外
        query = f"{keyword} -is:retweet"
        
        tweets = x_client.search_recent_tweets(
            query=query,
            max_results=max_results,
            tweet_fields=['id', 'text', 'created_at', 'author_id'],
            expansions=['author_id']
        )

        results = []
        if tweets.data:
            for tweet in tweets.data:
                results.append({
                    "text": tweet.text,
                    "url": f"https://x.com/i/status/{tweet.id}",
                    "id": tweet.id,
                    "reliability": "high_official_api" # 公式APIなので信頼度最高
                })
        
        logger.info(f"✅ {len(results)}件のツイートを取得しました。")
        return results

    except tweepy.errors.Forbidden as e:
        logger.error(f"❌ 権限エラー: {e} (Freeプランの制限や権限設定を確認してください)")
    except Exception as e:
        logger.error(f"❌ X検索エラー: {e}")
    
    return []

# --- AI分析セクション (Azure Grounding with Bing Search) ---

def analyze_with_ai(item: Dict) -> str:
    """
    Azure OpenAIのBing検索グラウンディング機能を使用して高度な解析を実施
    """
    if not aoai_client:
        return json.dumps({"error": "Azure OpenAI クライアントが初期化されていません"})

    system_prompt = (
        "あなたは市場調査と在庫状況の専門分析官です。"
        "提供されたSNSの投稿内容を分析し、必要に応じてBing Searchを使用して最新の在庫状況や公式発表を確認してください。"
        "回答は必ず以下のJSON形式で出力してください：\n"
        "{\"summary\": \"投稿の要約\", \"verification\": \"検索で確認できた最新の事実\", \"status\": \"在庫/生産状況の判定\", \"urgency\": \"重要度(高/中/低)\"}"
    )

    user_content = (
        f"以下のX(Twitter)投稿の内容を分析し、Bing検索で最新情報を裏取りしてください。\n\n"
        f"内容: {item['text']}\n"
        f"投稿URL: {item['url']}"
    )

    max_retries = 5
    for i in range(max_retries):
        try:
            response = aoai_client.chat.completions.create(
                model=AOAI_DEPLOYMENT,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                tools=[{"type": "web_search"}], 
                response_format={"type": "json_object"},
                temperature=0.2
            )
            return response.choices[0].message.content
        except Exception as e:
            delay = 2 ** i
            logger.warning(f"⚠️ AI分析リトライ中 ({i+1}/{max_retries}): {e}")
            if i == max_retries - 1:
                return json.dumps({"error": f"AI分析失敗: {e}"})
            time.sleep(delay)

# --- 実行 ---

def main():
    logger.info("--- X API 連携分析テスト開始 ---")
    
    # 検索したいキーワード
    test_keywords = ["在庫切れ", "販売再開"]
    
    for kw in test_keywords:
        results = search_x_tweets(kw, max_results=10)
        
        if not results:
            logger.warning(f"データが取得できませんでした: {kw}")
            continue
            
        for i, item in enumerate(results, 1):
            print(f"\n--- 投稿分析 [{i}] ---")
            print(f"URL: {item['url']}")
            print(f"本文: {item['text'][:100]}...")
            
            logger.info(f"🧠 AI分析中 (Bing検索実行)...")
            analysis_json = analyze_with_ai(item)
            
            try:
                analysis = json.loads(analysis_json)
                print(f"【要約】: {analysis.get('summary')}")
                print(f"【裏取り】: {analysis.get('verification')}")
                print(f"【状況】: {analysis.get('status')}")
                print(f"【重要度】: {analysis.get('urgency')}")
            except:
                print(f"分析結果(raw):\n{analysis_json}")
            
            # APIレート制限を考慮して少し待機
            time.sleep(2)
            
    logger.info("--- テスト終了 ---")

if __name__ == "__main__":
    main()