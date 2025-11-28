from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading

app = Flask(__name__)

# ==============================
# 1) Tokens
# ==============================

VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================
# 2) Sessions Memory
# ==============================

SESSIONS = {}  
BUFFER_DELAY = 15
MEMORY_TIMEOUT = 900  # 15 min


# ==============================
# 3) WhatsApp Sender
# ==============================

def send_to_whatsapp(name, phone, service, history_text):
    try:
        message = f"""
🔥 حجز جديد من البوت:

الاسم: {name}
الرقم: {phone}
الخدمة: {service}

الرسائل السابقة:
{history_text}
        """

        msg = message.replace("\n", "%0A").replace(" ", "+")

        url = f"https://api.callmebot.com/whatsapp.php?phone=9647818931201&text={msg}&apikey=8423339"

        r = requests.get(url)
        print("📤 WhatsApp sent:", r.text)

    except Exception as e:
        print("❌ WhatsApp Error:", e)


# ==============================
# 4) Detect booking keywords
# ==============================

def detect_booking_intent(text):
    words = ["احجز", "اريد احجز", "موعد", "احتاج حجز", "اريد اجي", "booking"]
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
    if "تبيض" in t or "تبييض" in t:
        return "تبييض الأسنان"
    if "تنظيف" in t or "تنضيف" in t:
        return "تنظيف الأسنان"
    if "تقويم" in t:
        return "تقويم الأسنان"
    if "قلع" in t:
        return "قلع"
    if "زراعة" in t:
        return "زراعة أسنان"
    return "غير محددة"


# ==============================
# 5) 15-sec reply buffer
# ==============================

def schedule_reply(user_id):
    time.sleep(BUFFER_DELAY)

    state = SESSIONS.get(user_id)
    if not state:
        return

    now = time.time()

    if (now - state["last_message_time"]) >= BUFFER_DELAY:
        try:
            reply = ask_openai(user_id)
        except Exception as e:
            print("❌ AI Error:", e)
            reply = "صار خلل بسيط، جرب مرة ثانية 🙏"

        send_message(user_id, reply)


# ==============================
# 6) Add message + memory logic
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
# 7) AI Response + Booking System
# ==============================

def ask_openai(user_id):
    state = SESSIONS[user_id]
    msgs = state["messages"]
    last_msg = msgs[-1]
    history = " | ".join(msgs[:-1]) if len(msgs) > 1 else ""

    # ==================================================
    #          BOOKING LOGIC
    # ==================================================

    # 1) نية الحجز
    if state["booking_step"] is None and detect_booking_intent(last_msg):
        state["booking_step"] = "asking_name"
        state["booking_service"] = detect_service(history + " " + last_msg)
        return "تمام حبيبي، حتى أكملك الحجز دزلي اسمك الكامل."

    # 2) استلام الاسم
    if state["booking_step"] == "asking_name":
        state["booking_name"] = last_msg.strip()
        state["booking_step"] = "asking_phone"
        return "تمام حبيبي، هسه دزلي رقمك حتى أكمل الحجز."

    # 3) استلام الرقم + تحقق
    if state["booking_step"] == "asking_phone":
        phone = last_msg.replace(" ", "")
        if not (phone.startswith("07") and len(phone) == 11):
            return "حبيبي الرقم غير صحيح. لازم يبدأ بـ 07 ويكون 11 رقم."

        state["booking_phone"] = phone
        state["booking_step"] = "done"

        # إرسال واتساب
        send_to_whatsapp(
            state["booking_name"],
            state["booking_phone"],
            state["booking_service"],
            history
        )

        return f"""
تأكيد الحجز:
الاسم: {state['booking_name']}
الرقم: {state['booking_phone']}
الخدمة: {state['booking_service']}
راح يتم التواصل وياك من قسم المتابعة خلال لحظات ❤️
        """

    # ==================================================
    #               NORMAL AI REPLY
    # ==================================================

    system_prompt = """
انت اسمك "علي" موظّف الكول سنتر في عيادة كولدن لاين لطب وتجميل الأسنان.
تحجي باللهجة العراقية، باحترام، وبدون مبالغة. ردودك قصيرة (سطرين أو 3)، 
وتجاوب فقط على **آخر رسالة**. الرسائل القديمة تستخدمها فقط كخلفية للفهم.

✔️ إذا عنده مشكلة ويه العيادة: 
   تكوله: "حبيبي هذا رقم العيادة حتى يتواصلون وياك مباشرة: 07728802820"

✔️ إذا يريد يحجز:
   تطلب منه الاسم ثم الرقم.

✔️ تفهم الكلمات العامية:
(قبق/غلاف/تقبيق = تغليف)
(طاح/وكع/انشلع = انقلع)
(تحشاه/تحشية = حشوة)
(يوجعني/يموتني = ألم)

معلومات العيادة:
- بغداد، زيونة، شارع الربيعي – داخل كراج مجمع إسطنبول
- الدوام: 4 مساءً – 9 مساءً (الجمعة عطلة)
- رقم الحجز: 07728802820

الأسعار:
- زاركون 75
- زاركون أيماكس 100
- القلع 25
- الحشوة 35
- حشوة الجذر 125
- تبييض 100
- تنظيف 25
- تقويم 450
- زراعة كوري 350
- زراعة ألماني 450
- زراعة فورية كاملة 1,750
- ابتسامة زاركون 1,200,000 (16 سن)
- ابتسامة إيماكس 1,600,000 (16 سن)

لا تكرر، لا تبالغ، طمّن المراجع، وخليك صديق إله.
"""

    rsp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "assistant", "content": f"خلفية المحادثة السابقة: {history}"},
            {"role": "user", "content": last_msg}
        ],
        max_tokens=250
    )

    return rsp.choices[0].message.content.strip()


# ==============================
# 8) Webhook Endpoints
# ==============================

@app.route("/", methods=["GET"])
def home():
    return "GoldenLine bot with Smart Booking — Running"


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
                add_user_message(sender, ev["message"]["text"])

    return "OK", 200


# ==============================
# 9) Facebook Send
# ==============================

def send_message(receiver, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "message": {"text": text}}

    requests.post(url, params=params, json=payload)


# ==============================
# RUN (Render)
# ==============================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
