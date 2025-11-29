from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading

app = Flask(__name__)

# =======================================================
#   🔑 TOKENS
# =======================================================
VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

WHATSAPP_URL = "https://api.callmebot.com/whatsapp.php?phone=9647818931201&apikey=8423339&text="

# =======================================================
#   📊 DAILY STATS
# =======================================================
DAILY_BOOKINGS = 0
DAILY_MESSAGES = 0
DAILY_INCOMPLETE = 0
SERVICE_COUNTER = {}

# =======================================================
#   🧠 SESSIONS
# =======================================================
SESSIONS = {}

BUFFER_DELAY = 15
MEMORY_TIMEOUT = 900  # 15 minutes


# =======================================================
#   🔥 AUTO CLEANER (EVERY 1 HOUR)
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
#   ✍️ Typing Indicator
# =======================================================
def send_typing(receiver):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "sender_action": "typing_on"}
    requests.post(url, params=params, json=payload)


# =======================================================
#   🔢 Extract Phone Number
# =======================================================
def extract_phone(text):
    for w in text.split():
        if w.startswith("07") and len(w) == 11 and w.isdigit():
            return w
    return None


# =======================================================
#   🧾 Extract Name
# =======================================================
def extract_name(text):
    cleaned = ''.join([c if not c.isdigit() else ' ' for c in text])
    if any('\u0600' <= c <= '\u06FF' for c in cleaned) and " " in cleaned:
        return cleaned.strip()
    return None


# =======================================================
#   ☎️ Send WhatsApp booking
# =======================================================
def send_whatsapp_booking(name, phone):
    global DAILY_BOOKINGS
    DAILY_BOOKINGS += 1

    msg = f"حجز جديد:\nالاسم: {name}\nالرقم: {phone}\nالخدمة: معاينة مجانية"
    url = WHATSAPP_URL + requests.utils.quote(msg)
    requests.get(url)


# =======================================================
#   📊 Generate Daily Report (TEXT)
# =======================================================
def generate_report_text():
    top_service = max(SERVICE_COUNTER, key=SERVICE_COUNTER.get) if SERVICE_COUNTER else "غير محدد"

    report = (
        "📊 تقرير اليوم – عيادة كولدن لاين\n\n"
        f"🟢 عدد الحجوزات: {DAILY_BOOKINGS}\n"
        f"✉️ عدد الرسائل: {DAILY_MESSAGES}\n"
        f"⏳ طلبات غير مكتملة: {DAILY_INCOMPLETE}\n"
        f"⭐ أكثر خدمة مطلوبة: {top_service}\n"
    )
    return report


# =======================================================
#   📱 Send Report to WhatsApp
# =======================================================
def send_whatsapp_report():
    report = generate_report_text()
    url = WHATSAPP_URL + requests.utils.quote(report)
    requests.get(url)


# =======================================================
#   ⏰ Daily Report at 9 PM
# =======================================================
def report_daemon():
    global DAILY_BOOKINGS, DAILY_MESSAGES, DAILY_INCOMPLETE, SERVICE_COUNTER

    while True:
        now = time.localtime()
        if now.tm_hour == 21 and now.tm_min == 0:   # الساعة 9 مساءً
            send_whatsapp_report()

            # تصفير اليوم
            DAILY_BOOKINGS = 0
            DAILY_MESSAGES = 0
            DAILY_INCOMPLETE = 0
            SERVICE_COUNTER = {}

            # تنظيف السيشنات
            SESSIONS.clear()

            time.sleep(60)
        time.sleep(10)

threading.Thread(target=report_daemon, daemon=True).start()


# =======================================================
#   ⏳ 30-MIN FOLLOW UP (ONCE ONLY)
# =======================================================
def follow_up_checker(user_id, snapshot_time):
    time.sleep(1800)  # 30 minutes

    st = SESSIONS.get(user_id)
    if not st:
        return

    if st["last_message_time"] == snapshot_time and st["phone"] == "" and st["followup_sent"] is False:
        global DAILY_INCOMPLETE
        DAILY_INCOMPLETE += 1

        send_message(
            user_id,
            "حبي إذا بعدك تحتاج تحجز، كلّي حتى أكملك الموعد ❤️\n"
            "الخدمة مجانية والفحص سريع وما ياخذ وقت."
        )
        st["followup_sent"] = True


# =======================================================
#   🧠 Buffer 15 sec
# =======================================================
def schedule_reply(user_id):
    time.sleep(BUFFER_DELAY)

    st = SESSIONS.get(user_id)
    if not st:
        return

    now = time.time()
    if now - st["last_message_time"] >= BUFFER_DELAY:

        send_typing(user_id)

        text = st["history"][-1] if st["history"] else ""
        reply = ask_openai(user_id, text)
        send_message(user_id, reply)


# =======================================================
#   📥 Add Message
# =======================================================
def add_user_message(user_id, text):
    global DAILY_MESSAGES
    DAILY_MESSAGES += 1

    now = time.time()

    # كلمة سرّية: Faty2000
    if text.strip() == "Faty2000":
        send_whatsapp_report()  # يرسلها على الواتساب فقط
        return  # ما يجاوب الزبون نهائيًا

    # جلسة جديدة
    if user_id not in SESSIONS or (now - SESSIONS[user_id]["last_message_time"] > MEMORY_TIMEOUT):
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

    # متابعة بعد 30 دقيقة
    threading.Thread(target=follow_up_checker, args=(user_id, now)).start()

    # رقم = حجز
    phone = extract_phone(text)
    name = extract_name(text)

    if phone:
        final_name = name if name else "بدون اسم"

        st["phone"] = phone
        st["name"] = final_name
        st["followup_sent"] = True

        send_whatsapp_booking(final_name, phone)

        send_message(
            user_id,
            f"تم تثبيت موعدك مباشرة 🌟\n"
            f"الرقم: {phone}\n"
            "الخدمة: معاينة مجانية\n"
            "قسم المتابعة راح يتواصل وياك خلال لحظات ❤️"
        )
        return

    threading.Thread(target=schedule_reply, args=(user_id,)).start()


# =======================================================
#   🤖 GPT Handler (History as System)
# =======================================================
def ask_openai(user_id, text):
    st = SESSIONS[user_id]
    history_text = " | ".join(st["history"][:-1])

    # 🔥 بدون أي تغيير بالبرومبت
    big_prompt = """
انت اسمك علي موضف الكول سنتر بعيادة كولدن لاين،
وضيفتك ترد على الرسائل باللهجة العراقية ، وبدون مبالغة وتجاوب على جميع استفساراتهم بطريقة تطمن المراجع ويكون جواب وافي عن كل شي يخص طب الاسنان ، 
ملاحظة ١ :- تاخذ بعين الاعتبار تاريخ المحادثة المرسل مع المحادثة وترد على اخير رسالة فقط .
ملاحظة ٢ :- اذا المراجع عندة شكوة او عصبي او يشتكي من عمل العيادة ، تعتذر منه بطريقة مهذبة وتطلب منه الاسم ورقم التلفون حتى نتصل بيه واذا استمر بالتذمر ( مباشرة بلغة يتصل على رقم العيادة وتنطيه الرقم )

وهاي بعض الملاحظات الي راح تستفاد منها عند الرد على المراجعين :-

تفاصيل العيادة :-
الاسم : عيادة كولدن لاين لطب وتجميل الاسنان.
وقت الدوام : يوميا من الساعة ٤م الى الساعة ٩م عدى يوم الجمعة عطلة العيادة
العنوان : بغداد زيونة شارع الربيعي الخدمي داخل كراج مجمع اسطنبول 
رقم الهاتف :- 07728802820

الحشوة التجميلية جلسة وحدة
حشوة الجذر من جلسة الى ثلاثة جلسات حسب التهاب السن
تغليف الاسنان ( زاركون ، ايماكس ) خلال جلستين وبيناتهم من ٥ الى ٧ ايام
ضمان العيادة جودة العمل مدى الحياة
اذا كال المراجع ماكو تخفيضات تكول اله هاي اسعار عروض ، بس الطبيب ميقصر وياك ان شاء الله
حاول تفهم الاغلاط املائية وتصحيحها
"""

    messages = [
        {"role": "system", "content": big_prompt},
        {"role": "system", "content": f"هذا history لفهم السياق فقط:\n{history_text}"},
        {"role": "user", "content": text}
    ]

    rsp = client.chat.completions.create(
        model="gpt-4.1",
        messages=messages,
        max_tokens=300
    )
    return rsp.choices[0].message.content.strip()


# =======================================================
#   📡 WEBHOOK ROUTES
# =======================================================
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

    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            uid = ev["sender"]["id"]

            if "message" in ev and "text" in ev["message"]:
                add_user_message(uid, ev["message"]["text"])

    return "OK", 200


# =======================================================
#   ✉️ Send Message
# =======================================================
def send_message(receiver, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "message": {"text": text}}

    requests.post(url, params=params, json=payload)


# =======================================================
#   🚀 Run Server
# =======================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
