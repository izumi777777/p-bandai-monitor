import requests
from bs4 import BeautifulSoup

def search_netoff(keyword):
    base_url = "https://www.netoff.co.jp/cmdtyallsearch/"
    
    params = {
        "word": keyword,
        "cat": "",
        "cname": "すべてのカテゴリー"
    }

    # ヘッダーを少し強化（PCからのアクセスに見せる）
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
    }

    try:
        print(f"🔍 「{keyword}」をネットオフで検索中...")
        response = requests.get(base_url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            
            # --- 修正ポイント: ご提示いただいたHTML構造に合わせたセレクタ ---
            
            # 商品名 (aタグのクラス c-cassette__title)
            title_tags = soup.select("a.c-cassette__title")
            
            # 価格 (divタグのクラス c-cassette__price)
            price_tags = soup.select("div.c-cassette__price")
            
            if not title_tags:
                print("⚠️ 商品情報が見つかりませんでした。")
                print("デバッグ情報: HTML内に 'c-cassette__title' が含まれていません。")
                return

            print(f"\n--- 検索結果 ({len(title_tags)}件) ---")
            
            # タイトルと価格をセットで表示
            for i, (t_tag, p_tag) in enumerate(zip(title_tags, price_tags)):
                if i >= 5: break # 上位5件だけ
                
                # タイトルの余分な空白を除去
                title = t_tag.get_text(strip=True)
                
                # 価格の「円」やカンマを除去して数値にする
                price_text = p_tag.get_text(strip=True).replace("円", "").replace(",", "")
                
                print(f"📘 {title}")
                print(f"💰 {price_text} 円")
                print("-" * 20)
                
        else:
            print(f"❌ アクセス失敗: {response.status_code}")

    except Exception as e:
        print(f"❌ エラー: {e}")

if __name__ == "__main__":
    search_netoff("推しの子")