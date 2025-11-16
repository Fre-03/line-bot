import os
import json
import logging
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    MessageAction
)
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
import psycopg2
from datetime import datetime
import time
import numpy as np
import requests

# 配置日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# LINE Bot Configuration - Use environment variables
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('V1sufJIBSKrWjgrLXco7MwlF6nTfUezSNoaWXGp56FYTt9439aLLNutzglbQgkABmwuSQ9M944XUzsWh6ZGMdyXlDQ3VMhVcUfLRB7Q9wcE+HqdK2NA/fr4VOvwKb3xDXAQaaKhaVdHSsizqgeanjgdB04t89/1O/w1cDnyilFU=', '')
LINE_CHANNEL_SECRET = os.environ.get('d2a475a09075ee8842452113564322de', '')
# LINE SDK v3 configuration
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# PostgreSQL Configuration - Use environment variable
POSTGRES_CONNECTION_STRING = os.environ.get('postgresql://postgres:Sa151120@localhost:5432/chatbot_db', '')

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

# === RAG 組件 ===
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=200,
    length_function=len
)

# === 免費本地嵌入模型 ===
try:
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-mpnet-base-v2",
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

# === 核心功能函數 (Include all your existing functions here) ===
# === 資料庫初始化函數 ===
def init_line_postgresql_database():
    try:
        conn = psycopg2.connect(POSTGRES_CONNECTION_STRING)
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
        
        # Create RAG knowledge base table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                content_vector VECTOR(768),
                category TEXT DEFAULT 'general',
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
        ''')
        
        # 導師知識庫表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS teacher_knowledge_base (
                id SERIAL PRIMARY KEY,
                teacher_id TEXT NOT NULL,
                teacher_name TEXT NOT NULL,
                content TEXT NOT NULL,
                content_vector VECTOR(768),
                context TEXT,
                category TEXT DEFAULT 'general',
                source_type TEXT CHECK(source_type IN ('manual', 'auto_captured')),
                captured_at TIMESTAMP DEFAULT NOW(),
                created_at TIMESTAMP DEFAULT NOW()
            );
        ''')
        
        # 導師資料表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS teacher_profiles (
                teacher_id TEXT PRIMARY KEY,
                teacher_name TEXT NOT NULL,
                department TEXT,
                contact_info TEXT,
                office_location TEXT,
                expertise TEXT,
                teaching_style TEXT,
                personal_notes TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
        ''')
        
        # 創建向量索引
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS knowledge_base_vector_idx 
            ON knowledge_base USING ivfflat (content_vector vector_cosine_ops);
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS teacher_knowledge_vector_idx 
            ON teacher_knowledge_base USING ivfflat (content_vector vector_cosine_ops);
        ''')
        
        logger.info("✅ 資料庫初始化完成！")
        cursor.close()
        conn.close()
        
    except Exception as e:
        logger.error(f"❌ 資料庫初始化錯誤: {e}")

# 初始化資料庫
init_line_postgresql_database()

# === 核心功能函數 ===
def create_teacher_profile(teacher_id, teacher_name, department=None, contact_info=None, 
                          office_location=None, expertise=None, teaching_style=None, personal_notes=None):
    try:
        conn = psycopg2.connect(POSTGRES_CONNECTION_STRING)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO teacher_profiles 
            (teacher_id, teacher_name, department, contact_info, office_location, expertise, teaching_style, personal_notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (teacher_id) 
            DO UPDATE SET 
                teacher_name = EXCLUDED.teacher_name,
                department = EXCLUDED.department,
                contact_info = EXCLUDED.contact_info,
                office_location = EXCLUDED.office_location,
                expertise = EXCLUDED.expertise,
                teaching_style = EXCLUDED.teaching_style,
                personal_notes = EXCLUDED.personal_notes,
                updated_at = NOW()
        ''', (teacher_id, teacher_name, department, contact_info, office_location, expertise, teaching_style, personal_notes))
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"✅ 導師資料已儲存: {teacher_name} ({teacher_id})")
        return True
        
    except Exception as e:
        logger.error(f"❌ 儲存導師資料錯誤: {e}")
        return False

def add_teacher_knowledge(teacher_id, teacher_name, content, context=None, category="general", source_type="manual"):
    try:
        conn = psycopg2.connect(POSTGRES_CONNECTION_STRING)
        cursor = conn.cursor()
        
        content_vector = embeddings.embed_query(content)
        vector_str = "[" + ",".join(map(str, content_vector)) + "]"
        
        cursor.execute('''
            INSERT INTO teacher_knowledge_base 
            (teacher_id, teacher_name, content, content_vector, context, category, source_type)
            VALUES (%s, %s, %s, %s::vector, %s, %s, %s)
        ''', (teacher_id, teacher_name, content, vector_str, context, category, source_type))
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"✅ 已添加導師知識: {teacher_name}")
        return True
        
    except Exception as e:
        logger.error(f"❌ 添加導師知識錯誤: {e}")
        return False

def retrieve_teacher_knowledge(query, teacher_id=None, k=3):
    try:
        conn = psycopg2.connect(POSTGRES_CONNECTION_STRING)
        cursor = conn.cursor()
        
        query_vector = embeddings.embed_query(query)
        query_vector_str = "[" + ",".join(map(str, query_vector)) + "]"
        
        if teacher_id:
            sql = '''
                SELECT teacher_name, content, context, category,
                       1 - (content_vector <=> %s::vector) as similarity
                FROM teacher_knowledge_base 
                WHERE teacher_id = %s
                ORDER BY content_vector <=> %s::vector
                LIMIT %s;
            '''
            params = (query_vector_str, teacher_id, query_vector_str, k)
        else:
            sql = '''
                SELECT teacher_name, content, context, category,
                       1 - (content_vector <=> %s::vector) as similarity
                FROM teacher_knowledge_base 
                ORDER BY content_vector <=> %s::vector
                LIMIT %s;
            '''
            params = (query_vector_str, query_vector_str, k)
        
        cursor.execute(sql, params)
        results = cursor.fetchall()
        cursor.close()
        conn.close()
        
        documents = []
        for teacher_name, content, context, category, similarity in results:
            documents.append({
                "teacher_name": teacher_name,
                "content": content,
                "context": context,
                "category": category,
                "similarity": round(similarity, 3)
            })
        
        logger.info(f"👨‍🏫 檢索到 {len(documents)} 個導師知識片段")
        return documents
        
    except Exception as e:
        logger.error(f"❌ 檢索導師知識錯誤: {e}")
        return []

def add_to_knowledge_base(title, content, category="general", metadata=None):
    try:
        conn = psycopg2.connect(POSTGRES_CONNECTION_STRING)
        cursor = conn.cursor()
        
        content_vector = embeddings.embed_query(content)
        vector_str = "[" + ",".join(map(str, content_vector)) + "]"
        
        cursor.execute('''
            INSERT INTO knowledge_base (title, content, content_vector, category, metadata)
            VALUES (%s, %s, %s::vector, %s, %s)
        ''', (title, content, vector_str, category, json.dumps(metadata or {})))
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"✅ 已添加知識庫文件: {title}")
        return True
        
    except Exception as e:
        logger.error(f"❌ 添加知識庫錯誤: {e}")
        return False

def retrieve_relevant_documents(query, category_filter=None, k=3):
    try:
        conn = psycopg2.connect(POSTGRES_CONNECTION_STRING)
        cursor = conn.cursor()
        
        query_vector = embeddings.embed_query(query)
        query_vector_str = "[" + ",".join(map(str, query_vector)) + "]"
        
        if category_filter:
            sql = '''
                SELECT title, content, 
                       1 - (content_vector <=> %s::vector) as similarity
                FROM knowledge_base 
                WHERE category = %s
                ORDER BY content_vector <=> %s::vector
                LIMIT %s;
            '''
            params = (query_vector_str, category_filter, query_vector_str, k)
        else:
            sql = '''
                SELECT title, content, 
                       1 - (content_vector <=> %s::vector) as similarity
                FROM knowledge_base 
                ORDER BY content_vector <=> %s::vector
                LIMIT %s;
            '''
            params = (query_vector_str, query_vector_str, k)
        
        cursor.execute(sql, params)
        results = cursor.fetchall()
        cursor.close()
        conn.close()
        
        documents = []
        for title, content, similarity in results:
            documents.append({
                "title": title,
                "content": content,
                "similarity": round(similarity, 3)
            })
        
        logger.info(f"📚 檢索到 {len(documents)} 個相關文件")
        return documents
        
    except Exception as e:
        logger.error(f"❌ 檢索錯誤: {e}")
        return []

# === 初始化範例資料 ===
def initialize_sample_data():
    # 知識庫資料
    sample_data = [
        {
            "title": "圖書館位置與開放時間",
            "content": "學校圖書館位於行政大樓旁邊的紅色建築物。開放時間：週一至週五 8:00-22:00，週六日 9:00-17:00。從校門口進入後直走，看到行政大樓後左轉，圖書館就在右手邊。",
            "category": "campus_navigation"
        },
        {
            "title": "請假流程說明",
            "content": "學生請假流程：1. 向導師請假獲得同意 2. 填寫學校請假單 3. 送至系辦核准 4. 將核准單交給課程助教。緊急情況可先口頭請假，事後補辦手續。",
            "category": "student_affairs"
        }
    ]
    
    # 導師資料
    sample_teachers = [
        {
            "teacher_id": "T001",
            "teacher_name": "張老師",
            "department": "計算機科學系",
            "contact_info": "分機: 1234, Email: chang@school.edu",
            "office_location": "工程大樓 301室",
            "expertise": "人工智慧, 機器學習, 資料庫系統",
            "teaching_style": "注重實作，鼓勵學生提問",
            "personal_notes": "辦公室時間: 週二、四 14:00-16:00"
        }
    ]
    
    # 導師知識
    sample_knowledge = [
        {
            "teacher_id": "T001",
            "teacher_name": "張老師",
            "content": "程式作業的評分標準主要看程式邏輯正確性、程式碼風格和註解完整性。遲交一週內扣20%，超過一週不予計分。",
            "context": "關於作業評分標準的說明",
            "category": "grading"
        }
    ]
    
    # 添加資料
    for data in sample_data:
        add_to_knowledge_base(data["title"], data["content"], data["category"])
    
    for teacher in sample_teachers:
        create_teacher_profile(**teacher)
    
    for knowledge in sample_knowledge:
        add_teacher_knowledge(**knowledge)
    
    logger.info("✅ 範例資料初始化完成")

initialize_sample_data()

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
    
    elif any(word in message_lower for word in ['張老師', 't001']):
        return "👨‍🏫 張老師資訊：\n🏫 系所：計算機科學系\n📍 辦公室：工程大樓 301室\n📞 聯絡：分機 1234"
    
    elif any(word in message_lower for word in ['hi', 'hello', '你好', '嗨']):
        role_text = "同學" if user_profile.get('role') == 'student' else "老師" if user_profile.get('role') == 'teacher' else "朋友"
        return f"👋 你好{role_text}！我是 Freya 學伴！"
    
    return None

def generate_rag_response(user_message, line_user_id, user_profile):
    """RAG 增強版回應生成"""
    logger.info(f"🔍 RAG 處理訊息: {user_message}")
    
    # 1. 首先嘗試規則引擎
    simple_response = generate_simple_response(user_message, user_profile)
    if simple_response:
        return simple_response
    
    # 2. 檢索相關文件
    relevant_docs = []
    teacher_knowledge = []
    
    try:
        relevant_docs = retrieve_relevant_documents(user_message, k=3, similarity_threshold=0.3)
        teacher_knowledge = retrieve_teacher_knowledge(user_message, k=2, similarity_threshold=0.3)
    except Exception as e:
        logger.error(f"❌ 檢索過程錯誤: {e}")
    
    # 3. 準備上下文
    context = ""
    if relevant_docs or teacher_knowledge:
        context += "📚 相關資訊：\n\n"
        
        if relevant_docs:
            context += "🏫 校園資訊：\n"
            for i, doc in enumerate(relevant_docs, 1):
                context += f"{i}. 【{doc['title']}】\n"
                context += f"   {doc['content']}\n\n"
        
        if teacher_knowledge:
            context += "👨‍🏫 導師說明：\n"
            for i, knowledge in enumerate(teacher_knowledge, 1):
                context += f"{i}. 【{knowledge['teacher_name']}】\n"
                context += f"   {knowledge['content']}\n\n"
    
    if context:
        return f"""🤖 關於「{user_message}」，我找到以下資訊：

{context}

如果這沒有完全解答您的問題，建議直接聯繫相關系辦！😊"""
    else:
        return f"""🤖 我了解您想詢問「{user_message}」

目前我的知識庫中沒有相關的詳細資訊。建議您：
• 直接聯繫相關系辦
• 詢問課程導師

我會持續學習，未來為您提供更好的服務！📚"""

# === Message Queue System ===
def store_pending_message(line_user_id, user_message, reply_token=None):
    """Store message in database for processing"""
    try:
        conn = get_db_connection()
        if conn is None:
            return False
            
        cursor = conn.cursor()
        
        # Create pending_messages table if not exists
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_messages (
                id SERIAL PRIMARY KEY,
                line_user_id TEXT NOT NULL,
                user_message TEXT NOT NULL,
                reply_token TEXT,
                received_at TIMESTAMP DEFAULT NOW(),
                processed BOOLEAN DEFAULT FALSE
            )
        ''')
        
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

def send_line_message(user_id, message_text):
    """Send message to LINE user"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=message_text)]
                )
            )
        logger.info(f"✅ Sent message to {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to send message: {e}")
        return False

def send_line_reply(reply_token, message_text):
    """Send reply using reply token"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=message_text)]
                )
            )
        logger.info(f"✅ Sent reply to {reply_token}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to send reply: {e}")
        return False

# === Webhook Endpoint ===
@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    
    if not signature:
        abort(400)
    
    try:
        handler.handle(body, signature)
        return 'OK'
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        logger.error(f"❌ Webhook 錯誤: {e}")
        abort(500)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    try:
        message_id = event.message.id
        line_user_id = event.source.user_id
        user_message = event.message.text.strip()
        reply_token = event.reply_token
        
        logger.info(f"💬 Received message from {line_user_id}: {user_message}")
        
        # Store message for processing
        store_pending_message(line_user_id, user_message, reply_token)
        
        # Send immediate acknowledgment
        immediate_response = "⏳ 已收到您的訊息，正在處理中..."
        send_line_reply(reply_token, immediate_response)
        
    except Exception as e:
        logger.error(f"❌ Error handling message: {e}")

@app.route("/")
def home():
    return "🚀 LINE Bot is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)