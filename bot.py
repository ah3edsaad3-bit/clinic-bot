from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading
import re
import json
from datetime import datetime, timedelta

app = Flask(__name__)

# =======================================================
# 🔑 TOKENS
# =======================================================
VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

WHATSAPP_URL = (
    "https://api.callmebot.com/whatsapp.php?"
    "phone=9647818931201&apikey=8423339&text="
)

BOOKING_API_URL = "https://script.google.com/macros/s/AKfycbznSh6PeJodzuAqObqo9_kWIfgLoZHhrJ97C4pEXCXwD9JD4s3wZ9I93MRl0ot6d36-1g/exec"

# =======================================================
# 📊 DAILY STATS
# =======================================================
DAILY_BOOKINGS = 0
DAILY_MESSAGES = 0
DAILY_INCOMPLETE = 0

# =======================================================
# 🧠 SESSIONS
# =======================================================
SESSIONS = {}
BUFFER_DELAY = 15
MEMORY_TIMEOUT = 900


# =======================================================
# 🔥 AUTO CLEANER
# =======================================================
def cleaner_daemon():
    while True:
        now = time.time()
        for uid in list(SESSIONS.keys()):
            if now - SESSIONS[uid]["last_message_time"] > 3600:
                del SESSIONS[uid]
        time.sleep(3600)

threading.Thread(target=cleaner_daemon, daemon=True).start()


# =======================================================
# ✍️ Typing Indicator
# =======================================================
def send_typing(receiver):
    if not PAGE_ACCESS_TOKEN:
        return

    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "sender_action": "typing_on"}
    requests.post(url, params=params, json=payload)


# =======================================================
# 🔢 Utility Functions
# =======================================================
def normalize_numbers(text):
    arabic = "٠١٢٣٤٥٦٧٨٩"
    english = "0123456789"
    return text.translate(str.maketrans(arabic, english))


def extract_phone(text):
    text = normalize_numbers(text)
    m = re.findall(r"07\d{9}", text)
    return m[0] if m else None


def extract_name(text):
    t = normalize_numbers(text)
    cleaned = ''.join([c if not c.isdigit() else ' ' for c in t])
    return cleaned.strip() if len(cleaned.strip()) > 1 else None


# =======================================================
# 📅 Next weekday name → date
# =======================================================
def next_weekday_by_name(day_name):
    days = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
        "الاثنين": 0, "الثلاثاء": 1, "الاربعاء": 2, "الأربعاء": 2,
        "الخميس": 3, "الجمعة": 4, "السبت": 5, "الاحد": 6, "الأحد": 6,
    }

    dn = day_name.strip().lower()
    if dn not in days:
        return None

    target = days[dn]
    today = datetime.now()
    diff = target - today.weekday()
    if diff <= 0:
        diff += 7

    result = today + timedelta(days=diff)
    return result.strftime("%Y-%m-%d")


# =======================================================
# 📅 Default date = tomorrow unless Friday → Saturday
# =======================================================
def get_default_date():
    today = datetime.now()
    d = today + timedelta(days=1)

    if d.weekday() == 4:  # Friday
        d += timedelta(days=1)

    return d.strftime("%Y-%m-%d")


# =======================================================
# 🧠 Chat Delay Reply
# =======================================================
def schedule_reply(user_id):
    time.sleep(BUFFER_DELAY)
    st = SESSIONS.get(user_id)
    if not st:
        return

    now = time.time()
    if now - st["last_message_time"] >= BUFFER_DELAY:
        send_typing(user_id)
        last_msg = st["history"][-1]
        reply = ask_openai_chat(user_id, last_msg)
        if reply:
            send_message(user_id, reply)


# =======================================================
# 📥 Last Messages
# =======================================================
def get_last_messages(user_id, limit=10):
    return SESSIONS.get(user_id, {}).get("history", [])[-limit:]


# =======================================================
# 🤖 Booking Engine
# =======================================================
def convert_to_12h(time_str):
    try:
        t = datetime.strptime(time_str, "%H:%M")
        return t.strftime("%I:%M").lstrip("0")  # مثال → 4:00
    except:
        return time_str
def analyze_booking(name, phone, last_msgs):
    history = "\n".join(last_msgs)

    prompt = f"""
اقرأ آخر رسائل المراجع وحدد تفاصيل الموعد بدون حساب التاريخ.
المخرجات يجب أن تكون JSON فقط.

مثال الإخراج:

{{
 "patient_name": "الاسم",
 "patient_phone": "{phone}",
 "service": "معاينة مجانية",
 "day_name": "الخميس أو Thursday أو فارغة إذا لم يذكر يوم",
 "time": "HH:MM" (إذا لم يُذكر وقت يكون 16:00)
}}

❗ لا تحسب التاريخ. فقط أرجع day_name.
"""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": history}
            ],
            max_tokens=250,
            temperature=0
        )

        data = json.loads(rsp.choices[0].message.content)

        patient_name = data.get("patient_name") or name or "بدون اسم"
        day_name = data.get("day_name", "").strip()
        time_str = data.get("time") or "16:00"

        # 🔥 تحويل الوقت إلى صيغة 12 ساعة بدون AM/PM
        time_12h = convert_to_12h(time_str)

        # 🔥 حساب التاريخ
        if day_name:
            date = next_weekday_by_name(day_name)
            if not date:
                date = get_default_date()
        else:
            date = get_default_date()

        # 🔥 استخراج اسم اليوم بالعربي
        day_name_ar = {
            0: "الاثنين",
            1: "الثلاثاء",
            2: "الأربعاء",
            3: "الخميس",
            4: "الجمعة",
            5: "السبت",
            6: "الأحد"
        }

        day_index = datetime.strptime(date, "%Y-%m-%d").weekday()
        day_label = day_name_ar[day_index]

        # 🔥 صياغة الرسالة النهائية
        ai_msg = (
            "تم تثبيت موعدك ❤\n"
            f"الاسم: {patient_name}\n"
            f"رقم الهاتف: {phone}\n"
            f"الخدمة: معاينة مجانية\n"
            f"التاريخ: {date} ({day_label})\n"
            f"الوقت: {time_12h}\n"
            "العنوان: بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول / عيادة كولدن لاين"
        )

        return {
            "patient_name": patient_name,
            "patient_phone": phone,
            "service": "معاينة مجانية",
            "date": date,
            "time": time_str,
            "ai_message": ai_msg
        }

    except:
        fallback_date = get_default_date()
        fallback_time = "16:00"
        fallback_time12 = convert_to_12h(fallback_time)

        return {
            "patient_name": name or "بدون اسم",
            "patient_phone": phone,
            "service": "معاينة مجانية",
            "date": fallback_date,
            "time": fallback_time,
            "ai_message":
                f"تم تثبيت موعدك ❤\n"
                f"الاسم: {name or 'بدون اسم'}\n"
                f"رقم الهاتف: {phone}\n"
                f"التاريخ: {fallback_date} ({day_name_ar[datetime.strptime(fallback_date, '%Y-%m-%d').weekday()]})\n"
                f"الوقت: {fallback_time12}\n"
                "العنوان: بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول / عيادة كولدن لاين"
        }



# =======================================================
# 🧾 Save Booking into Sheet
# =======================================================
def save_booking_to_sheet(b):
    payload = {
        "action": "addBooking",
        "name": b["patient_name"],
        "phone": b["patient_phone"],
        "service": b["service"],
        "date": b["date"],
        "time": b["time"],
        "status": "Pending"
    }
    requests.post(BOOKING_API_URL, json=payload)


# =======================================================
# 📤 WhatsApp Booking Notification
# =======================================================
def send_whatsapp_booking(name, phone, date, time_):
    msg = (
        "حجز جديد:\n"
        f"الاسم: {name}\n"
        f"الرقم: {phone}\n"
        f"التاريخ: {date}\n"
        f"الوقت: {time_}"
    )
    url = WHATSAPP_URL + requests.utils.quote(msg)
    requests.get(url)


# =======================================================
# 🤖 Chat Engine (Ali)
# =======================================================
def ask_openai_chat(user_id, text):
    st = SESSIONS[user_id]
    history_text = " | ".join(st["history"][:-1]) if len(st["history"]) > 1 else ""

    prompt = """ 
اسمك علي، موظف كول سنتر بعيادة كولدن لاين لطب وتجميل الأسنان.
ترد باللهجة العراقية، ردودك قصيرة، واضحة، تطمّن المراجع، بدون مبالغة.

قواعد الرد:
- جاوب على آخر رسالة فقط.
- لا ترحب إلا إذا المراجع رحّب.
- الرد من 5 إلى 25 كلمة حسب الحاجة.
- إذا ما عندك معلومة دقيقة: كُل "نحددها بعد المعاينة المجانية".

إذا المراجع:
- عصبي أو يشتكي → اعتذر بلطف واطلب الاسم والرقم، وإذا استمر وجّهه للاتصال: 07728802820
- يريد حجز → اطلب الاسم والرقم فقط، ولا تثبّت موعد بنفسك.
- يطلب تخفيض → الأسعار عروض، والطبيب ميقصر وياه.

سياسة الإقناع:
اربط السعر بـ (مواد ألمانية + ضمان حقيقي مدى الحياة).

تفاصيل العيادة:
الدوام: يومياً 4م–9م، الجمعة عطلة
الموقع: بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول
الهاتف: 07728802820

الأسعار (مختصر):
- زاركون: 100 ألف
- زاركون إيماكس: 150 ألف
- حشوة تجميلية: 35 ألف
- حشوة جذر: 125 ألف
- قلع: 25 ألف
- تنظيف: 25 ألف
- تبييض ليزر: 100 ألف
- تقويم: 450 ألف للفك
- نانو فنير: 50 ألف للسن
- زراعة ألماني: 450 ألف
- فك كامل زرعات فورية: 1,750,000
- ابتسامة زاركون 20 سن: 1,400,000
- ابتسامة إيماكس 16 سن: 2,000,000

ملاحظات:
- التغليف يحتاج برد خفيف.
- صحح الأخطاء الإملائية الشائعة باللهجة.
- لا تذكر عمليات حسابية، أعطِ السعر النهائي فقط.
"""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ],
            max_tokens=250,
            temperature=0.4
        )

        return rsp.choices[0].message.content.strip()

    except:
        return "صار خلل بسيط، عاود رسالتك ♥"


# =======================================================
# 📥 Core Handler
# =======================================================
def add_user_message(user_id, text):
    global DAILY_MESSAGES
    DAILY_MESSAGES += 1
    now = time.time()

    if text.strip() == "Faty2000":
        return

    if (
        user_id not in SESSIONS
        or (now - SESSIONS[user_id]["last_message_time"] > MEMORY_TIMEOUT)
    ):
        SESSIONS[user_id] = {
            "history": [],
            "name": "",
            "phone": "",
            "last_message_time": now,
            "followup_sent": False
        }

    st = SESSIONS[user_id]
    st["history"].append(text)
    st["last_message_time"] = now

    # Extract name
    n = extract_name(text)
    if n:
        st["name"] = n

    # Detect phone → booking mode
    phone = extract_phone(text)
    if phone:
        st["phone"] = phone
        msgs = get_last_messages(user_id)
        booking = analyze_booking(st["name"], phone, msgs)

        send_message(user_id, booking["ai_message"])
        save_booking_to_sheet(booking)
        send_whatsapp_booking(
            booking["patient_name"], booking["patient_phone"],
            booking["date"], booking["time"]
        )
        return

    # otherwise → chat engine
    threading.Thread(target=schedule_reply, args=(user_id,), daemon=True).start()


# =======================================================
# ✉️ Send Message
# =======================================================
def send_message(receiver, text):
    params = {"access_token": PAGE_ACCESS_TOKEN}
    url = "https://graph.facebook.com/v18.0/me/messages"
    payload = {"recipient": {"id": receiver}, "message": {"text": text}}
    requests.post(url, params=params, json=payload)


# =======================================================
# 📡 WEBHOOK
# =======================================================
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Error", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            if "message" in ev and "text" in ev["message"]:
                add_user_message(ev["sender"]["id"], ev["message"]["text"])
    return "OK", 200


# =======================================================
# RUN
# =======================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
