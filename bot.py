from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading

app = Flask(__name__)

# Tokens from Environment Variables
VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# Sessions memory
SESSIONS = {}
BUFFER_DELAY = 15  # 15 seconds لتجميع الرسائل في محادثة واحدة
MAX_HISTORY_TURNS = 10 # عدد الأدوار (user/assistant) التي يتم الاحتفاظ بها في الذاكرة


def schedule_reply(user_id):
    """Wait 15 seconds — if no new messages, process."""
    time.sleep(BUFFER_DELAY)

    state = SESSIONS.get(user_id)
    if state is None:
        return

    now = time.time()

    # If no new messages in last 15 sec → process
    if (now - state["last_message_time"]) >= BUFFER_DELAY:
        
        # 1. Prepare user message and add to history
        messages_buffer = state["messages"] 
        
        if not messages_buffer:
            return

        final_user_text = " ".join(messages_buffer)

        # إضافة رسالة المستخدم المجمعة إلى الـ history قبل الإرسال
        state["history"].append({"role": "user", "content": final_user_text})

        # 2. Call OpenAI with the full history
        reply = ""
        try:
            # تمرير سجل المحادثة بالكامل (history) للحفاظ على السياق
            reply = ask_openai(state["history"]) 
            
            # 3. Append assistant's reply to history
            state["history"].append({"role": "assistant", "content": reply})
            
        except Exception as e:
            print("❌ OpenAI Error:", e)
            reply = "صار خلل بسيط، حاول مرة ثانية 🙏"
            
            # إذا حدث خطأ، نحذف آخر رسالة مستخدم أضفناها لتجنب استهلاك سياق خاطئ
            state["history"].pop()

        send_message(user_id, reply)

        # 4. Truncate history to prevent large context window (and high cost)
        if len(state["history"]) > MAX_HISTORY_TURNS:
            # نحتفظ بآخر MAX_HISTORY_TURNS فقط
            state["history"] = state["history"][-MAX_HISTORY_TURNS:]
        
        # 5. Reset the temporary buffer only, KEEPING the conversation history
        SESSIONS[user_id]["messages"] = []
        SESSIONS[user_id]["last_message_time"] = 0


def add_user_message(user_id, text):
    now = time.time()

    if user_id not in SESSIONS:
        # Initializing the session with 'history' list for context memory
        SESSIONS[user_id] = {
            "messages": [], 
            "history": [], # سجل المحادثة
            "last_message_time": now
        }

    SESSIONS[user_id]["messages"].append(text)
    SESSIONS[user_id]["last_message_time"] = now

    # Start timer thread
    t = threading.Thread(target=schedule_reply, args=(user_id,))
    t.start()


def ask_openai(conversation_history):
    system_prompt = (
        "انت مساعد ذكي ترد باللهجة العراقية وباختصار، "
        "وإذا الزبون يريد يحجز اطلب الاسم والرقم."
    )
    
    # دمج الـ system prompt مع سجل المحادثة
    messages_with_system = [{"role": "system", "content": system_prompt}] + conversation_history

    rsp = client.chat.completions.create(
        model="gpt-4o-mini", # استخدام نموذج جديد لدعم أفضل
        messages=messages_with_system,
        max_tokens=200
    )

    return rsp.choices[0].message.content.strip()


@app.route("/", methods=["GET"])
def home():
    return "Render bot running with 15s buffer ⏳"


@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Error", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("📩 Incoming:", data)

    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            sender = ev["sender"]["id"]

            if "message" in ev and "text" in ev["message"]:
                text = ev["message"]["text"]

                add_user_message(sender, text)

    return "OK", 200


def send_message(receiver, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": receiver},
        "message": {"text": text}
    }
    requests.post(url, params=params, json=payload)


# Render server
if __name__ == "__main__":
    # تأكد من أن المنفذ 10000 هو المنفذ الذي تستخدمه في Render أو منصة الاستضافة
    app.run(host="0.0.0.0", port=10000)
