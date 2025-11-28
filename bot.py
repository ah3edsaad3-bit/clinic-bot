from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading
import re

app = Flask(__name__)

# ==============================
# 1) Tokens
# ==============================

VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================
# 2) Session Memory
# ==============================

SESSIONS = {}
BUFFER_DELAY = 15          # لتجميع الرسائل
MEMORY_TIMEOUT = 900       # 15 دقيقة ذاكرة


# ==============================
# 3) WhatsApp Sender (CallMeBot)
# ==============================

def send_to_whatsapp(name, phone, service, history_text):
    """إرسال الحجز إلى واتساب أحمد عبر CallMeBot"""
    try:
        message = f"""حجز جديد من البوت:

الاسم: {name}
الرقم: {phone}
الخدمة: {service}

مقتطف من المحادثة:
{history_text}
"""
        # تجهيز النص للرابط
        msg = message.replace("\n", "%0A").replace(" ", "+")
        url = (
            "https://api.callmebot.com/whatsapp.php"
            f"?phone=9647818931201&text={msg}&apikey=8423339"
        )

        r = requests.get(url, timeout=10)
        print("📤 WhatsApp sent status:", r.status_code, r.text)
    except Exception as e:
        print("❌ WhatsApp Error:", e)


# ==============================
# 4) Helpers (نيات + خدمة + اسم + رقم)
# ==============================

def detect_booking_intent(text: str) -> bool:
    t = text.lower()
    intents = ["احجز", "اريد احجز", "موعد", "حجز", "booking", "اريد اجي", "اريد اجيكم"]
    return any(w in t for w in intents)


def detect_service(text: str) -> str:
    t = text.lower()

    # كلمات لها علاقة بالقلع
    if any(w in t for w in ["قلع", "اقتلاع", "شلع", "انشلع", "طاح السن", "وقع السن", "وكع السن"]):
        return "قلع سن"

    if "ايماكس" in t and "زاركون" in t:
        return "تغليف زاركون أيماكس"
    if "ايماكس" in t:
        return "تغليف أيماكس"
    if "زاركون" in t or "قبق" in t or "غلاف" in t or "تقبيق" in t:
        return "تغليف زاركون"
    if "حشوة" in t or "تحشية" in t or "تحشاه" in t:
        if "جذر" in t or "عصب" in t:
            return "حشوة جذر"
        return "حشوة تجميلية"
    if "جذر" in t or "عصب" in t:
        return "حشوة جذر"
    if "تبييض" in t or "تبيض" in t:
        return "تبييض الأسنان"
    if "تنضيف" in t or "تنظيف" in t:
        return "تنظيف الأسنان"
    if "تقويم" in t:
        return "تقويم الأسنان"
    if "زراعة" in t:
        return "زراعة أسنان"

    return "غير محددة"


def extract_phone(text: str):
    digits = re.sub(r"\D", "", text)
    if digits.startswith("07") and len(digits) == 11:
        return digits
    return None


def looks_like_name(text: str):
    # اسم بسيط: ما بي أرقام، وطوله معقول
    if any(c.isdigit() for c in text):
        return False
    t = text.strip()
    if len(t) < 3:
        return False
    # كلمتين أو أكثر أحسن
    return True


# ==============================
# 5) 15-second buffer processor
# ==============================

def schedule_reply(user_id):
    time.sleep(BUFFER_DELAY)

    state = SESSIONS.get(user_id)
    if not state:
        return

    now = time.time()
    if (now - state["last_time"]) < BUFFER_DELAY:
        # إجت رسالة جديدة خلال الـ 15 ثانية → نخلي المؤقت الجديد يعالجها
        return

    messages = state["messages"]
    if not messages:
        return

    last_msg = messages[-1]
    prev_msg = messages[-2] if len(messages) > 1 else ""
    history_text = " | ".join(messages[:-1]) if len(messages) > 1 else ""

    print(f"🧩 Buffer for {user_id}: {messages}")

    # ------------------------------------------------
    # A) لو الحجز مكتمل سابقاً (ما نعيد من الصفر)
    # ------------------------------------------------
    if state.get("booking_step") == "completed":
        # هنا نعتبر أي كلام عن "موعد" هو فقط استفسار، مو حجز جديد
        if "موعد" in last_msg or "الخميس" in last_msg or "الاحد" in last_msg:
            send_message(
                user_id,
                "تمام حبيبي، موعدك نثبته من 4 للـ 9 المساء، قسم المتابعة يتواصل وياك يحددلك الساعة الأنسب 👍"
            )
        else:
            # جواب طبيعي بالذكاء
            reply = ask_ai(history_text, last_msg)
            send_message(user_id, reply)

        state["messages"] = []
        return

    # ------------------------------------------------
    # B) محاولة ذكية لاكتشاف (اسم + رقم) من آخر رسالتين
    # ------------------------------------------------
    name_candidate_prev = prev_msg if looks_like_name(prev_msg) else None
    phone_in_last = extract_phone(last_msg)

    if state.get("booking_step") is None and name_candidate_prev and phone_in_last:
        # نمط: (اسم) ثم (رقم) → حجز كامل بدون ما ندخل بحوار طويل
        state["booking_name"] = name_candidate_prev.strip()
        state["booking_phone"] = phone_in_last
        state["booking_service"] = detect_service(" ".join(messages))
        state["booking_step"] = "completed"

        send_to_whatsapp(
            state["booking_name"],
            state["booking_phone"],
            state["booking_service"],
            history_text
        )

        confirmation = (
            "تم تأكيد الحجز ❤️\n\n"
            f"الاسم: {state['booking_name']}\n"
            f"الرقم: {state['booking_phone']}\n"
            f"الخدمة: {state['booking_service']}\n\n"
            "راح يتواصل وياك قسم المتابعة خلال لحظات 🙏"
        )
        send_message(user_id, confirmation)

        state["messages"] = []
        return

    # ------------------------------------------------
    # C) بدء حجز صريح إذا ظهرت نية حجز
    # ------------------------------------------------
    if state.get("booking_step") is None:
        # نية حجز واضحة بالكلام
        if detect_booking_intent(last_msg):
            state["booking_step"] = "ask_name"
            state["booking_service"] = detect_service(" ".join(messages))
            send_message(user_id, "تمام حبيبي، حتى أكملك الحجز دزلي اسمك الكامل.")
            state["messages"] = []
            return

        # إذا آخر رسالة شكلها اسم، وقبلها كلام عن خدمة أو سعر → نعتبرها بداية حجز
        if looks_like_name(last_msg) and detect_service(" ".join(messages)) != "غير محددة":
            state["booking_name"] = last_msg.strip()
            state["booking_service"] = detect_service(" ".join(messages))
            state["booking_step"] = "ask_phone"
            send_message(user_id, "تمام حبيبي، هسه دزلي رقمك حتى أكمل الحجز.")
            state["messages"] = []
            return

    # ------------------------------------------------
    # D) خطوات الحجز التدرجية
    # ------------------------------------------------

    # مرحلة: نطلب اسم
    if state.get("booking_step") == "ask_name":
        if looks_like_name(last_msg):
            state["booking_name"] = last_msg.strip()
            state["booking_step"] = "ask_phone"
            send_message(user_id, "تمام حبيبي، هسه دزلي رقمك حتى أكمل الحجز.")
        else:
            send_message(user_id, "حبيبي دزلي اسمك الكامل بدون أرقام 🙏")
        state["messages"] = []
        return

    # مرحلة: نطلب رقم
    if state.get("booking_step") == "ask_phone":
        phone = extract_phone(last_msg)
        if phone:
            state["booking_phone"] = phone
            state["booking_step"] = "completed"
            if not state.get("booking_service"):
                state["booking_service"] = detect_service(" ".join(messages))

            send_to_whatsapp(
                state["booking_name"],
                state["booking_phone"],
                state["booking_service"],
                history_text
            )

            confirmation = (
                "تم تأكيد الحجز ❤️\n\n"
                f"الاسم: {state['booking_name']}\n"
                f"الرقم: {state['booking_phone']}\n"
                f"الخدمة: {state['booking_service']}\n\n"
                "راح يتواصل وياك قسم المتابعة خلال لحظات 🙏"
            )
            send_message(user_id, confirmation)
        else:
            send_message(user_id, "حبيبي الرقم لازم يبدي بـ 07 ويكون 11 رقم 🙏")

        state["messages"] = []
        return

    # ------------------------------------------------
    # E) رد طبيعي (بدون حجز)
    # ------------------------------------------------
    reply = ask_ai(history_text, last_msg)
    send_message(user_id, reply)
    state["messages"] = []


# ==============================
# 6) تخزين الرسالة
# ==============================

def add_user_message(user_id, text):
    now = time.time()

    if user_id not in SESSIONS or (now - SESSIONS[user_id]["last_time"]) > MEMORY_TIMEOUT:
        SESSIONS[user_id] = {
            "messages": [],
            "last_time": now,
            "booking_step": None,
            "booking_name": None,
            "booking_phone": None,
            "booking_service": None,
        }

    SESSIONS[user_id]["messages"].append(text)
    SESSIONS[user_id]["last_time"] = now

    threading.Thread(target=schedule_reply, args=(user_id,)).start()


# ==============================
# 7) AI الرد العادي
# ==============================

def ask_ai(history, last_msg):
    system_prompt = """
انت اسمك "علي" موظف الكول سنتر بعيادة كولدن لاين لطب وتجميل الأسنان.
تحجي باللهجة العراقية الواضحة، مختصر، لبق، وتهدّي المراجع.
ترد فقط على آخر رسالة، وتستخدم الكلام السابق للفهم مو للتكرار.

تفاصيل العيادة:
- بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع إسطنبول
- الدوام: من 4 المساء لحد 9 المساء – الجمعة عطلة
- رقم الحجز: 07728802820

الأسعار والعروض:
- تغليف زاركون: 75 ألف
- تغليف زاركون أيماكس: 100 ألف
- القلع: 25 ألف
- الحشوة التجميلية: 35 ألف
- حشوة الجذر: 125 ألف
- تبييض بالليزر: 100 ألف
- تنظيف الأسنان: 25 ألف
- تقويم الأسنان: 450 ألف للفك
- زراعة الأسنان (كوري 350 / ألماني 450)
- زراعة الفك الكامل للزرعات الفورية: 1,750,000 زرعات ألمانية
- ابتسامة المشاهير زاركون (16 سن): 1,200,000
- ابتسامة المشاهير زاركون أيماكس (16 سن): 1,600,000

قواعد الرد:
- لا تكثر ترحيب، مرة وحده تكفي.
- لا تبالغ ولا تستخدم حجي تجاري قوي.
- إذا اشتكى من العيادة أو عنده مشكلة: تنطيه الرقم 07728802820 حتى يتواصلون وياه.
- إذا حسّيت عنده نية حجز، شجّعه بلطف ودلّه على إرسال الاسم والرقم.
"""

    rsp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "assistant", "content": f"خلفية المحادثة السابقة: {history}"},
            {"role": "user", "content": last_msg},
        ],
        max_tokens=220,
    )

    return rsp.choices[0].message.content.strip()


# ==============================
# 8) Facebook send
# ==============================

def send_message(receiver_id, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": receiver_id},
        "message": {"text": text},
    }
    r = requests.post(url, params=params, json=payload)
    print("📤 FB send:", r.status_code, r.text)


# ==============================
# 9) Webhook routes
# ==============================

@app.route("/", methods=["GET"])
def home():
    return "GoldenLine smart booking bot is running ✅"


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
            if "message" in ev and "text" in ev["message"]:
                sender = ev["sender"]["id"]
                text = ev["message"]["text"]
                add_user_message(sender, text)

    return "OK", 200


# ==============================
# 10) Run (لـ Render لو تشغيل محلي)
# ==============================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
