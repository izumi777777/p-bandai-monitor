import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for

import firebase_admin
from firebase_admin import credentials, firestore

# 初期設定
load_dotenv()
app = Flask(__name__)

# Firestore初期化（既存コードと同じロジック）
FIREBASE_KEY_PATH = os.getenv("FIREBASE_KEY_PATH", "service-account-key.json")
FIRESTORE_COLLECTION = "sales_candidates"

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_KEY_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- ルーティング ---

@app.route('/')
def index():
    """
    メイン画面：利益が出る商品リストを表示
    """
    # Firestoreから「利益が出る(profitable)」データだけを取得
    docs = db.collection(FIRESTORE_COLLECTION)\
             .where("status", "==", "profitable")\
             .order_by("profit", direction=firestore.Query.DESCENDING)\
             .stream()

    items = []
    for doc in docs:
        data = doc.to_dict()
        data['id'] = doc.id # ドキュメントIDも持たせる（更新用）
        items.append(data)

    return render_template('index.html', items=items)

@app.route('/archive/<item_id>')
def archive_item(item_id):
    """
    商品を「仕入れ済み」または「無視」ステータスに変更して一覧から消す
    """
    # statusを 'archived' に更新
    db.collection(FIRESTORE_COLLECTION).document(item_id).update({
        "status": "archived",
        "archived_at": datetime.now()
    })
    return redirect(url_for('index'))

@app.route('/all')
def all_items():
    """
    デバッグ用：全データ表示（ステータス関係なく）
    """
    docs = db.collection(FIRESTORE_COLLECTION).limit(50).stream()
    items = [doc.to_dict() for doc in docs]
    return render_template('index.html', items=items, show_all=True)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)