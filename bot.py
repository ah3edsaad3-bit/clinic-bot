from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading
import re

app = Flask(__name__)

# ==============================
# Tokens
# ==============================

VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================
# Session Memory
# ==============================

SESSIONS = {}
BUFFER_DELAY = 15
MEMORY_TIMEOUT = 900  # 15 minutes


# ==============================
# SEND TO WHATSAPP (CallMeBot)
# ==============================

def send_to_whatsapp(name, phone, service, history_text):
    try:
        message = f"""
🔥 حجز جديد:

الاسم: {name}
الرقم: {phone}
الخدمة: {service}

محادثة الزبون:
{history_text}
        """

        msg = message.replace("\n", "%0A").replace(" ", "+")

        url = f"https://api.callmebot.com/whatsapp.php?phone=9647818931201&text={msg}&apikey=8423339"

        r = requests.get(url)
        print("📤 WhatsApp sent:", r.text)
    except Exception as e:
        print("❌ WhatsApp Error:", e)


# ==============================
# Utility Functions
# ==============================

def detect_booking_intent(text):
    words = ["احجز", "اريد احجز", "موعد", "احتاج", "booking", "اجي"]
    return any(w in text.lower() for w in words)


def detect_service(text):
    t = text.lower()
    if "ايماكس" in t and "زاركون" in t:
        return "تغليف زاركون أيماكس"
    if "ايماكس" in t:
        return "تغليف أيماكس"
    if "زاركون" in t:
        return "تغليف زاركون"
    if "حشوة" in t:
        return "حشوة تجميلية"
    if "جذر" in t:
        return "حشوة جذر"
    if "تبييض" in t or "تبيض" in t:
        return "تبييض الأسنان"
    if "تنظيف" in t or "تنضيف" in t:
        return "تنظيف الأسنان"
    if "قلع" in t:
        return "قلع"
    return "غير محددة"


def extract_phone(text):
    digits = re.sub(r"\D", "", text)
    if digits.startswith("07") and len(digits) == 11:
        return digits
    return None


def extract_name(text):
    if any(c.isdigit() for c in text):
        return None
    if len(text) < 3:
        return None
    return text.strip()


# ==============================
# 15-Second Processing
# ==============================

def schedule_reply(user_id):
    time.sleep(BUFFER_DELAY)

    state = SESSIONS.get(user_id)
    if not state:
        return

    now = time.time()

    if (now - state["last_message_time"]) < BUFFER_DELAY:
        return

    messages = state["messages"]
    history_text = " | ".join(messages[:-1]) if len(messages) > 1 else ""
    last_msg = messages[-1]

    # ------------------------------------------
    # 1) BOOKING PHASE FIX – NEW INTELLIGENT LOGIC
    # ------------------------------------------

    # إذا الزبون يريد يحجز
    if state["booking_step"] is None and detect_booking_intent(last_msg):
        state["booking_service"] = detect_service(history_text + " " + last_msg)
        state["booking_step"] = "ask_name"
        send_message(user_id, "تمام حبيبي، حتى أكملك الحجز دزلي اسمك الكامل.")
        state["messages"] = []
        return

    # إذا ننتظر اسم
    if state["booking_step"] == "ask_name":
        name = extract_name(last_msg)
        if name:
            state["booking_name"] = name
            state["booking_step"] = "ask_phone"
            send_message(user_id, "تمام حبيبي، هسه دزلي رقمك حتى أكمل الحجز.")
            state["messages"] = []
            return
        else:
            send_message(user_id, "دزلي اسمك بدون أرقام حبيبي.")
            state["messages"] = []
            return

    # إذا ننتظر رقم
    if state["booking_step"] == "ask_phone":
        phone = extract_phone(last_msg)
        if phone:
            state["booking_phone"] = phone
            state["booking_step"] = "done"

            # إرسال للواتساب
            send_to_whatsapp(
                state["booking_name"],
                state["booking_phone"],
                state["booking_service"],
                history_text
            )

            confirmation = f"""
تم تأكيد الحجز ❤️

الاسم: {state['booking_name']}
الرقم: {state['booking_phone']}
الخدمة: {state['booking_service']}

راح يتواصل وياك قسم المتابعة خلال لحظات 🙏
            """
            send_message(user_id, confirmation)
            state["messages"] = []
            return
        else:
            send_message(user_id, "حبيبي الرقم لازم يبدأ بـ 07 ويكون 11 رقم.")
            state["messages"] = []
            return

    # ------------------------------------------
    # 2) NORMAL AI REPLY
    # ------------------------------------------

    reply = ask_ai(history_text, last_msg)
    send_message(user_id, reply)

    state["messages"] = []


# ==============================
# Add User Message
# ==============================

def add_user_message(user_id, text):
    now = time.time()

    if user_id not in SESSIONS or (now - SESSIONS[user_id]["last_message_time"]) > MEMORY_TIMEOUT:
        SESSIONS[user_id] = {
            "messages": [],
            "last_message_time": now,
            "booking_step": None,
            "booking_name": None,
            "booking_phone": None,
            "booking_service": None
        }

    SESSIONS[user_id]["messages"].append(text)
    SESSIONS[user_id]["last_message_time"] = now

    threading.Thread(target=schedule_reply, args=(user_id,)).start()


# ==============================
# AI Response
# ==============================

def ask_ai(history, last_msg):
    system = """
انت اسمك "علي" موظّف الكول سنتر في عيادة كولدن لاين.
تحجي باللهجة العراقية، محترم، وبدون مبالغة.
تجاوب فقط على آخر رسالة، وتستخدم الرسائل القديمة للفهم فقط.

معلومات العيادة:
- بغداد / زيونة / الربيعي – داخل كراج مجمع إسطنبول
- الدوام: 4 المساء – 9 المساء / الجمعة عطلة
- رقم الحجز: 07728802820

الأسعار:
الزاركون 75 – الايماكس 100 – القلع 25 – الحشوة 35 – الجذر 125
تبييض 100 – تنظيف 25 – تقويم 450
زراعة (كوري 350 / ألماني 450)
الزرعات الفورية الكاملة 1,750,000
ابتسامة زاركون 1,200,000
ابتسامة ايماكس 1,600,000

خلك طيب، مختصر، تطمن المراجع.
    """

    rsp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "assistant", "content": f"خلفية المحادثة: {history}"},
            {"role": "user", "content": last_msg}
        ],
        max_tokens=200
    )

    return rsp.choices[0].message.content.strip()


# ==============================
# Facebook Send
# ==============================

def send_message(receiver, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "message": {"text": text}}
    requests.post(url, params=params, json=payload)


# ==============================
# Webhook
# ==============================

@app.route("/")
def home():
    return "GoldenLine Smart Bot Running"


@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Error", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("📩 Incoming:", data)

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            if "message" in event and "text" in event["message"]:
                add_user_message(event["sender"]["id"], event["message"]["text"])

    return "OK", 200


# ==============================
# RUN SERVER
# ==============================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
