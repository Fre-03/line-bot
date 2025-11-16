import os
import sys
import psycopg2
from linebot import LineBotApi
from linebot.models import TextSendMessage
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def process_pending_messages():
    try:
        # Database connection
        database_url = os.getenv('DATABASE_URL')
        if not database_url:
            logger.error("DATABASE_URL environment variable not set")
            return
            
        conn = psycopg2.connect(database_url)
        cursor = conn.cursor()
        
        # LINE API setup
        line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
        
        # Get pending messages
        cursor.execute("""
            SELECT id, user_id, message_text, message_type 
            FROM user_messages 
            WHERE processed = FALSE 
            LIMIT 10
        """)
        
        pending_messages = cursor.fetchall()
        logger.info(f"Found {len(pending_messages)} pending messages")
        
        for msg_id, user_id, message_text, message_type in pending_messages:
            try:
                # Process the message (your existing logic here)
                response_text = f"Processed: {message_text}"
                
                # Send response via LINE
                line_bot_api.push_message(
                    user_id,
                    TextSendMessage(text=response_text)
                )
                
                # Mark as processed
                cursor.execute(
                    "UPDATE user_messages SET processed = TRUE WHERE id = %s",
                    (msg_id,)
                )
                conn.commit()
                
                logger.info(f"Processed message {msg_id} for user {user_id}")
                
            except Exception as e:
                logger.error(f"Error processing message {msg_id}: {str(e)}")
                continue
                
    except Exception as e:
        logger.error(f"Error in process_pending_messages: {str(e)}")
        sys.exit(1)
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    process_pending_messages()
