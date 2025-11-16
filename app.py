import os
import json
import logging
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from langchain_community.embeddings import HuggingFaceEmbeddings
import psycopg2
from datetime import datetime
import numpy as np

# 配置日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# ✅ CORRECT: LINE Bot Configuration - Use environment variables ONLY
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# ✅ CORRECT: PostgreSQL Configuration
POSTGRES_CONNECTION_STRING = os.environ.get('DATABASE_URL', '')

# === AI 服務配置 ===
AI_SERVICE = "rule_engine"

# === 防止重複處理的機制 ===
processed_messages = set()
MAX_PROCESSED_MESSAGES = 1000

def is_message_processed(message_id):
    return message_id in processed_messages

def mark_message_processed(message_id):
    if len(processed_messages) >= MAX_PROCESSED_MESSAGES:
        processed_messages.clear()
    processed_messages.add(message_id)

# === 免費本地嵌入模型 ===
try:
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",  # ✅ Lighter model
        model_kwargs={'device': 'cpu'},
        encode_kwargs={'normalize_embeddings': True}
    )
    logger.info("✅ 本地嵌入模型加載成功！")
except Exception as e:
    logger.error(f"❌ 嵌入模型加載失敗: {e}")
    embeddings = None

# === 資料庫連接函數 ===
def get_db_connection():
    """獲取資料庫連接，包含錯誤處理"""
    try:
        conn = psycopg2.connect(POSTGRES_CONNECTION_STRING)
        return conn
    except Exception as e:
        logger.error(f"❌ 資料庫連接失敗: {e}")
        return None

# === 資料庫初始化函數 ===
def init_line_postgresql_database():
    try:
        conn = get_db_connection()
        if not conn:
            return
            
        cursor = conn.cursor()
        
        # Enable pgvector extension
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        
        # Create LINE users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS line_users (
                line_user_id TEXT PRIMARY KEY,
                username TEXT,
                role TEXT CHECK(role IN ('student', 'teacher', 'unknown')),
                department TEXT,
                teacher_id TEXT,
                created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                last_active DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
            );
        ''')
        
        # Create LINE chat history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS line_chat_history (
                id SERIAL PRIMARY KEY,
                line_user_id TEXT REFERENCES line_users(line_user_id),
                user_message TEXT,
                bot_response TEXT,
                is_teacher_knowledge BOOLEAN DEFAULT FALSE,
                timestamp DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
            );
        ''')
        
        # Create pending messages table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_messages (
                id SERIAL PRIMARY KEY,
                line_user_id TEXT NOT NULL,
                user_message TEXT NOT NULL,
                reply_token TEXT,
                received_at TIMESTAMP DEFAULT NOW(),
                processed BOOLEAN DEFAULT FALSE
            );
        ''')
        
        logger.info("✅ 資料庫初始化完成！")
        cursor.close()
        conn.close()
        
    except Exception as e:
        logger.error(f"❌ 資料庫初始化錯誤: {e}")

# 初始化資料庫
init_line_postgresql_database()

# === 核心功能函數 ===
def get_line_user_role(line_user_id):
    try:
        conn = get_db_connection()
        if conn is None:
            return {'role': 'unknown', 'username': None, 'department': None, 'teacher_id': None}
            
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT role, username, department, teacher_id FROM line_users WHERE line_user_id = %s",
            (line_user_id,)
        )
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            return {
                'role': result[0],
                'username': result[1],
                'department': result[2],
                'teacher_id': result[3]
            }
        
        return {'role': 'unknown', 'username': None, 'department': None, 'teacher_id': None}
        
    except Exception as e:
        logger.error(f"❌ 獲取用戶角色錯誤: {e}")
        return {'role': 'unknown', 'username': None, 'department': None, 'teacher_id': None}

def update_line_user_role(line_user_id, role, username=None, department=None, teacher_id=None):
    try:
        conn = get_db_connection()
        if conn is None:
            return False
            
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO line_users (line_user_id, username, role, department, teacher_id, last_active)
            VALUES (%s, %s, %s, %s, %s, EXTRACT(EPOCH FROM NOW()))
            ON CONFLICT (line_user_id) 
            DO UPDATE SET 
                username = EXCLUDED.username,
                role = EXCLUDED.role,
                department = EXCLUDED.department,
                teacher_id = EXCLUDED.teacher_id,
                last_active = EXCLUDED.last_active
        ''', (line_user_id, username, role, department, teacher_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        return True
        
    except Exception as e:
        logger.error(f"❌ 更新用戶角色錯誤: {e}")
        return False

def generate_simple_response(user_message, user_profile):
    """規則引擎 - 處理常見問題"""
    message_lower = user_message.lower()
    
    if any(word in message_lower for word in ['圖書館', 'library']):
        return "🏫 圖書館資訊：\n📍 位置：行政大樓旁邊的紅色建築物\n⏰ 開放時間：週一至週五 8:00-22:00，週六日 9:00-17:00"
    
    elif any(word in message_lower for word in ['請假', '請假流程', '缺課', '怎麼請假']):
        return "📝 請假流程：\n1. 向導師請假獲得同意\n2. 填寫學校請假單\n3. 送至系辦核准\n4. 將核准單交給課程助教"
    
    elif any(word in message_lower for word in ['hi', 'hello', '你好', '嗨']):
        role_text = "同學" if user_profile.get('role') == 'student' else "老師" if user_profile.get('role') == 'teacher' else "朋友"
        return f"👋 你好{role_text}！我是 Freya 學伴！"
    
    return None

def store_pending_message(line_user_id, user_message, reply_token=None):
    """Store message in database for processing"""
    try:
        conn = get_db_connection()
        if conn is None:
            return False
            
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO pending_messages (line_user_id, user_message, reply_token)
            VALUES (%s, %s, %s)
        ''', (line_user_id, user_message, reply_token))
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"📥 Stored pending message from {line_user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error storing pending message: {e}")
        return False

def send_line_reply(reply_token, message_text):
    """Send reply using reply token"""
    try:
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=message_text)
        )
        logger.info(f"✅ Sent reply")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to send reply: {e}")
        return False

# === Webhook Endpoint ===
@app.route("/")
def home():
    return "LINE Bot is running 24/7!"

@app.route("/callback", methods=['POST'])
def callback():
    # Get request signature and body
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    
    logger.info(f"📨 Received webhook request")
    
    # Handle webhook request
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature error")
        abort(400)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        abort(500)
    
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        line_user_id = event.source.user_id
        user_message = event.message.text
        reply_token = event.reply_token
        
        logger.info(f"💬 Received message from {line_user_id}: {user_message}")
        
        # Get user profile
        user_profile = get_line_user_role(line_user_id)
        
        # Generate immediate response
        immediate_response = generate_simple_response(user_message, user_profile)
        if not immediate_response:
            immediate_response = "⏳ 已收到您的訊息，正在為您處理中..."
        
        # Send immediate reply
        send_line_reply(reply_token, immediate_response)
        
        # Store for async processing (if needed)
        if immediate_response.startswith("⏳"):
            store_pending_message(line_user_id, user_message, reply_token)
        
    except Exception as e:
        logger.error(f"❌ Error handling message: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)