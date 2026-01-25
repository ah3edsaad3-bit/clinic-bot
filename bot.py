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
# 🔑 TOKENS & CONFIG
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
# 📊 MEMORY
# =======================================================
SESSIONS = {}
PROCESSED_MESSAGES = {}  # لمنع تكرار الردود
BUFFER_DELAY = 15
MEMORY_TIMEOUT = 1800  # 30 دقيقة
DAILY_MESSAGES = 0
# =======================================================
# 🔥 AUTO CLEANER
# =======================================================
def cleaner_daemon():
    while True:
        now = time.time()
        # تنظيف الجلسات القديمة
        for uid in list(SESSIONS.keys()):
            if now - SESSIONS[uid]["last_message_time"] > 3600:
                del SESSIONS[uid]
        # تنظيف سجل الرسائل المكررة (لحماية الذاكرة)
        for mid in list(PROCESSED_MESSAGES.keys()):
            if now - PROCESSED_MESSAGES[mid] > 600: # حذف بعد 10 دقائق
                del PROCESSED_MESSAGES[mid]
        time.sleep(600)

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
def analyze_booking(phone, last_msgs):
    history = "\n".join(last_msgs)

    prompt = f"""
اقرأ المحادثة بتركيز واستخرج معلومات الموعد.
المهمة الأساسية: استخراج اسم المراجع (الشخص الذي يريد العلاج) وليس اسم الدكتور أو العيادة.

المخرجات JSON فقط:
{{
 "patient_name": "اسم المراجع الصريح فقط",
 "patient_phone": "{phone}",
 "service": "معاينة مجانية",
 "day_name": "اليوم المذكور",
 "time": "HH:MM"
}}

ملاحظات:
- إذا لم يذكر المراجع اسمه صراحة (مثلاً: "اسمي أحمد" أو "أحجز لـ سارة")، اجعل قيمة patient_name "غير محدد".
- لا تستخدم عبارات مثل "أريد أحجز" كاسم.
"""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"المحادثة:\n{history}"}
            ],
            temperature=0
        )
        
        # تنظيف الرد من أي علامات Markdown
        clean_content = re.sub(r"```json|```", "", rsp.choices[0].message.content).strip()
        data = json.loads(clean_content)

        # إذا لم ينجح GPT في معرفة الاسم، نتركه "بدون اسم"
        patient_name = data.get("patient_name")
        if not patient_name or patient_name == "غير محدد":
            patient_name = "بدون اسم"
        day_name = data.get("day_name", "").strip()
        time_str = "16:00"

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
    # نأخذ آخر 5 رسائل فقط للسياق لتقليل التشتت
    context = " | ".join(st["history"][:-1]) if len(st["history"]) > 1 else "لا يوجد سياق سابق"

    prompt = """
اسمك علي، موظف في عيادة كولدن لاين. 
مهمتك: الرد على 'الرسالة الجديدة' بلهجة عراقية عامة فقط باستخدام 'السياق السابق' للاطلاع.

⚠️ قوانين صارمة:
1. جاوب على آخر جملة سألها المراجع فقط.
2. إذا المراجع سأل عدة أسئلة في الرسالة الأخيرة، جاوب عليها باختصار.
3. لا تكرر إجابات قمت بكتابتها في السياق السابق.
4. الرد حصراً باللهجة العراقية وبحد أقصى 30 كلمة.

قواعد الرد:
- اقرأ سياق المحادثة فقط للفهم، وجاوب حصراً على السؤال الموجود بآخر رسالة، ولا تعيد أسعار أو معلومات ذُكرت سابقاً إلا إذا طُلبت صراحة.
- لا ترحب إلا إذا المراجع رحّب.
- إذا ما عندك معلومة دقيقة: كُول "نحددها بعد المعاينة المجانية".
(( تعامل كشخص عادي مو ريبوت وخليك انساني وراعي مشاعر المقابيلك بالجواب , ورد بطريقة محترمة , وحاول تكتسب المقابيلك بردود علمية حتى وان لم اقم بذكرها لك ))
( بيش , شكد , ابيش , السعر = يطلب سعر ))
( هواي , مابيها مجال , غالي , شدعوة , هله هلة بينة , ماعندي = يطلب تخفيض )
نظام الاقساط متوفر على مصرف الرافدين تقسيط لمدة 10 اشهر بدون مقدمة وبدون فوائد
إذا المراجع:
- عصبي أو يشتكي → اعتذر بلطف واطلب الاسم والرقم، وإذا استمر وجّهه للاتصال: 07728802820
- يريد حجز → اطلب الاسم ويا يوم يناسبة للحجز ورقم الهاتف ، ولا تثبّت موعد بنفسك.
- يطلب تخفيض →هاي اسعار عروض، والطبيب ميقصر وياكم ان شاء الله.

سياسة الإقناع:
اربط السعر بـ (مواد ألمانية + ضمان حقيقي مدى الحياة).

تفاصيل العيادة:
الدوام: يومياً 4م–9م، الجمعة عطلة
الموقع: بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول
الهاتف: 07728802820

الأسعار:
- تغليف الزاركون : 75 ألف
- تغليف الزاركون ايماكس: 100 ألف
- تغليف الايماكس : 125 ألف
- حشوة تجميلية: 35 ألف
- حشوة جذر: 125 ألف
- قلع: 25 ألف
- تنظيف: 25 ألف
- تبييض ليزر: 100 ألف
- تقويم: 450 ألف للفك
- فك كامل زرعات فورية: مليون وربع
- فكين كامل زرعات فورية: مليونين ونص
- ابتسامة زاركون 20 سن: 1,400,000
- ابتسامة زاركون ايماكس 20 سن: 2,000,000
-الزراعة التقليدية :
السن الواحد 350 الف الكوري و 450 الف الالماني
 -الزراعة التقليدية :
الزراعة الفورية:
السن الواحد 200 التركي , 275 الالماني.

(عروض الزراعة للفك الواحد مليون وربع للفكين مليونين ونص )

اذا العميل كال ( مثال , عندي سنين زراعة و 8 تغليفات , تجمع اله سعر زرعتين 500 والتغليف 600 وهكذا ) 

ملاحظات:
- التغليف يحتاج برد خفيف.
- صحح الأخطاء الإملائية الشائعة باللهجة.
- لا تذكر عمليات حسابية، أعطِ السعر النهائي فقط.
- ضمان جودة العمل مدى الحياة.
- الزراعة الفورية بدون فتح لثة ويتم انجازها خلال 72 ساعة فقط.
- تغليف الاسنان بجلستين , حشوة الجذر من جلستين الى ثلاثة.
"""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o", # أنصحك باستخدام gpt-4o للسرعة والدقة
            messages=[
                {"role": "system", "content": prompt},
                {"role": "assistant", "content": f"السياق السابق للمحادثة: {context}"},
                {"role": "user", "content": f"الرسالة الجديدة المطلوب الرد عليها الآن: {text}"}
            ],
            temperature=0.3 # تقليل الـ temperature يجعل الرد رزيناً ومباشراً
        )
        return rsp.choices[0].message.content.strip()
    except:
        return "صار خلل بسيط، عاود رسالتك ♥"


# =======================================================
# 📥 Core Handler
# =======================================================
def add_user_message(user_id, text):
    now = time.time()

    if user_id not in SESSIONS or (now - SESSIONS[user_id]["last_message_time"] > MEMORY_TIMEOUT):
        SESSIONS[user_id] = {
            "history": [],
            "last_message_time": now,
            "booking_step": None,
            "temp_phone": None,
            "temp_name": None,
            "temp_day": None,
        }

    st = SESSIONS[user_id]
    st["history"].append(text)
    st["last_message_time"] = now

    phone = extract_phone(text)
    name = extract_name(text)
    day = any(d in text for d in ["السبت","الأحد","الاثنين","الثلاثاء","الأربعاء","الخميس"])

    # 🟢 مرحلة انتظار التفاصيل
    if st["booking_step"] == "waiting_details":

        if name:
            st["temp_name"] = name

        if day:
            st["temp_day"] = text

        # ✅ إذا اكتملت كل المعلومات
        if st["temp_phone"] and st["temp_name"] and st["temp_day"]:
            msgs = get_last_messages(user_id)
            booking = analyze_booking(st["temp_phone"], msgs)

            send_message(user_id, booking["ai_message"])
            save_booking_to_sheet(booking)
            send_whatsapp_booking(
                booking["patient_name"],
                booking["patient_phone"],
                booking["date"],
                booking["time"]
            )

            st["booking_step"] = None
            st["temp_phone"] = None
            st["temp_name"] = None
            st["temp_day"] = None
            return

        send_message(
            user_id,
            "تمام 🌹 بعد نحتاج الاسم واليوم حتى نثبت الحجز"
        )
        return

    # ✅ معلومات كاملة من أول رسالة
    if phone and (name and day):
        msgs = get_last_messages(user_id)
        booking = analyze_booking(phone, msgs)

        send_message(user_id, booking["ai_message"])
        save_booking_to_sheet(booking)
        send_whatsapp_booking(
            booking["patient_name"],
            booking["patient_phone"],
            booking["date"],
            booking["time"]
        )
        return

    # 🟡 رقم فقط
    if phone:
        st["temp_phone"] = phone
        st["booking_step"] = "waiting_details"
        send_message(
            user_id,
            "تمام 🌹 وصلنا رقمك، شنو اسم المراجع؟ وأي يوم يناسبك للحجز؟"
        )
        return

    # 🔵 دردشة عادية
    threading.Thread(
        target=schedule_reply,
        args=(user_id,),
        daemon=True
    ).start()



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
            user_id = ev["sender"]["id"]
            msg_id = ev.get("message", {}).get("mid")
            
            if msg_id:
                if msg_id in PROCESSED_MESSAGES: continue
                PROCESSED_MESSAGES[msg_id] = time.time()

            if "message" in ev and "text" in ev["message"]:
                add_user_message(user_id, ev["message"]["text"])
            elif "message" in ev and "attachments" in ev["message"]:
                send_message(user_id, "عاشت ايدك، وصلت الصورة وراح ندزها للدكتور. راح يطلع عليها ونطيك التفاصيل باقرب وقت إن شاء الله 🌹")
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
