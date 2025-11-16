import os
import requests
import psycopg2
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration from environment variables
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
POSTGRES_CONNECTION_STRING = os.environ['POSTGRES_CONNECTION_STRING']

def get_db_connection():
    """Get database connection"""
    return psycopg2.connect(POSTGRES_CONNECTION_STRING)

# Import your existing functions (simplified for GitHub Actions)
def generate_response(user_message, user_profile):
    """Simplified response generation for GitHub Actions"""
    # Your existing response logic here
    responses = {
        'hi': '👋 你好！我是學伴機器人',
        '你好': '👋 你好！需要什麼協助嗎？',
        '圖書館': '🏫 圖書館開放時間：週一至週五 8:00-22:00',
        '請假': '📝 請假流程：向導師請假 → 填寫請假單 → 送至系辦',
        '張老師': '👨‍🏫 張老師 - 計算機科學系，辦公室：工程大樓301',
        '計算機概論': '💻 計算機概論由張老師授課，週一三 9:00-10:30',
        '實習': '🎯 實習在大三第二學期，至少320小時'
    }
    
    # Check for exact matches first
    if user_message.lower() in responses:
        return responses[user_message.lower()]
    
    # Check for partial matches
    for key, response in responses.items():
        if key in user_message:
            return response
    
    return f"🤖 關於「{user_message}」，建議您直接聯繫相關系辦或導師獲取詳細資訊。"

def send_line_message(user_id, message_text):
    """Send message via LINE API"""
    headers = {
        'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
        'Content-Type': 'application/json'
    }
    data = {
        "to": user_id,
        "messages": [{"type": "text", "text": message_text}]
    }
    
    try:
        response = requests.post(
            'https://api.line.me/v2/bot/message/push',
            headers=headers,
            json=data,
            timeout=10
        )
        if response.status_code == 200:
            logger.info(f"✅ Message sent to {user_id}")
            return True
        else:
            logger.error(f"❌ LINE API error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Failed to send message: {e}")
        return False

def process_pending_messages():
    """Process all pending messages from database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get unprocessed messages (last 10 minutes)
        cursor.execute('''
            SELECT id, line_user_id, user_message, reply_token
            FROM pending_messages 
            WHERE processed = FALSE 
            AND received_at > NOW() - INTERVAL '10 minutes'
            ORDER BY received_at ASC
            LIMIT 20
        ''')
        
        messages = cursor.fetchall()
        processed_count = 0
        
        for msg_id, user_id, user_message, reply_token in messages:
            try:
                # Get user profile
                cursor.execute(
                    "SELECT role, username FROM line_users WHERE line_user_id = %s",
                    (user_id,)
                )
                user_result = cursor.fetchone()
                user_profile = {
                    'role': user_result[0] if user_result else 'unknown',
                    'username': user_result[1] if user_result else None
                }
                
                # Generate response
                response_text = generate_response(user_message, user_profile)
                
                # Send response
                success = send_line_message(user_id, response_text)
                
                if success:
                    # Mark as processed
                    cursor.execute(
                        'UPDATE pending_messages SET processed = TRUE WHERE id = %s',
                        (msg_id,)
                    )
                    processed_count += 1
                    logger.info(f"✅ Processed message {msg_id} for user {user_id}")
                else:
                    logger.error(f"❌ Failed to process message {msg_id}")
                    
            except Exception as e:
                logger.error(f"❌ Error processing message {msg_id}: {e}")
                # Mark as processed to avoid infinite retry
                cursor.execute(
                    'UPDATE pending_messages SET processed = TRUE WHERE id = %s',
                    (msg_id,)
                )
        
        conn.commit()
        cursor.close()
        conn.close()
        
        logger.info(f"🎯 Processed {processed_count} messages")
        return processed_count
        
    except Exception as e:
        logger.error(f"❌ Database error: {e}")
        return 0

if __name__ == "__main__":
    logger.info("🚀 Starting message processor...")
    count = process_pending_messages()
    logger.info(f"✅ Completed! Processed {count} messages")