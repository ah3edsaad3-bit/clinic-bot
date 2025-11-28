from flask import Flask, request
import requests
from openai import OpenAI
import time
import threading
import os
import re

app = Flask(__name__)

# ============= 1) TOKENS =============
VERIFY_TOKEN = "goldenline_secret"

PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WHATSAPP_API = os.getenv("WHATSAPP_API")

client = OpenAI(api_key=OPENAI_API_KEY)

DEBUG = True  # Log toggle


# ============= 2) SESSIONS + CLEANER CONFIG =============
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

SESSION_TTL = 6 * 60 * 60    # 6 hours
SESSION_MAX_AGE = 24 * 3600  # 24 hours
BUFFER_DELAY = 2.0
MAX_HISTORY = 8

CLEANER_INTERVAL = 3600  # 1 hour


def log(*args):
    if DEBUG:
        print("[BOT]", *args, flush=True)


def new_session():
    return {
        "messages_buffer": [],
        "history": [],
        "state": "idle",
        "temp_name": "",
        "temp_phone": "",
        "temp_service": "",
        "last_intent": None,
        "last_service": None,
        "teeth_count": None,
        "last_time": time.time(),
        "last_active": time.time(),
        "lock": threading.Lock()
    }


def get_session(uid):
    now = time.time()
    with SESSIONS_LOCK:
        sess = SESSIONS.get(uid)
        if not sess or (now - sess["last_active"]) > SESSION_TTL:
            sess = new_session()
            SESSIONS[uid] = sess
        sess["last_active"] = now
        return sess


# ============= 3) AUTO SESSION CLEANER (v4.1) =============
def cleaner_job():
    while True:
        time.sleep(CLEANER_INTERVAL)

        now = time.time()
        removed = 0

        with SESSIONS_LOCK:
            old_ids = [
                uid for uid, sess in SESSIONS.items()
                if (now - sess.get("last_active", 0)) > SESSION_MAX_AGE
            ]

            for uid in old_ids:
                del SESSIONS[uid]
                removed += 1

        if removed > 0:
            print(f"[CLEANER] Removed {removed} sessions older than 24h", flush=True)


th_cleaner = threading.Thread(target=cleaner_job)
th_cleaner.daemon = True
th_cleaner.start()


# ============= 4) BUFFER HANDLER =============
def schedule_reply(uid):
    time.sleep(BUFFER_DELAY)

    with SESSIONS_LOCK:
        session = SESSIONS.get(uid)
    if not session:
        return

    now = time.time()

    with session["lock"]:
        if (now - session["last_time"]) < BUFFER_DELAY:
            return

        if not session["messages_buffer"]:
            return

        final_text = " ".join(session["messages_buffer"]).strip()
        session["messages_buffer"] = []

    if final_text:
        log("Processing merged message:", final_text)
        reply = process_user_message(uid, final_text)
        send_message(uid, reply)


def add_message(uid, text):
    session = get_session(uid)
    now = time.time()

    with session["lock"]:
        session["messages_buffer"].append(text)
        session["last_time"] = now

    th = threading.Thread(target=schedule_reply, args=(uid,))
    th.daemon = True
    th.start()


# ============= 5) REMINDER AFTER 30 MINUTES =============
def schedule_reminder(uid):
    time.sleep(1800)
    session = SESSIONS.get(uid)
    if not session:
        return
    if session["state"] in ["waiting_name", "waiting_phone"]:
        send_message(uid, "بس أذكّرك حبي، إذا تريد نكمّل الحجز دزلي اسمك ورقمك ♥️")


# ============= 6) IMPROVED detect_intent (v4.2) =============
def detect_intent(txt: str) -> str:
    t = txt.lower().strip()

    # 1) Booking → أعلى أولوية دائماً
    booking_words = [
        "احجز", "حجز", "اريد احجز", "اريد موعد",
        "موعد", "سجلني", "ثبت الحجز", "خلي احجز"
    ]
    if any(w in t for w in booking_words):
        return "booking"

    # 2) Price
    if re.search(r"\b(عرض|عروض|سعر|اسعار|شكد|كم|التكلفة|الكلفة)\b", t):
        return "price"
    if any(w in t for w in ["تبييض", "تبيض", "يبيض", "يبيش"]):
        return "price"

    # 3) Medical
    medical_words = [
        "يوجع", "وجع", "ألم", "ورم", "انتفاخ",
        "التهاب", "يلتهب", "ينزف", "نزف",
        "خراج", "ضرس", "سني", "اسناني"
    ]
    if any(w in t for w in medical_words):
        return "medical"

    return "normal"


# ============= 7) SERVICE DETECTION =============
def detect_service(txt: str) -> str:
    t = txt.lower()

    if any(w in t for w in ["زركون", "غلاف", "تلبيسة", "تلبيسات", "crown", "جسر"]):
        if "ايماكس" in t or "emax" in t:
            return "تغليف زركون إيماكس"
        return "تغليف زركون"

    if "ايماكس" in t or "emax" in t:
        return "تغليف إيماكس"

    if any(w in t for w in ["قلع", "خلع", "شلع"]):
        return "قلع سن"

    if "حشوة" in t:
        if any(w in t for w in ["جذر", "عصب"]):
            return "حشوة جذر"
        return "حشوة تجميلية"

    if any(w in t for w in ["تبييض", "تبيض"]):
        return "تبييض الأسنان"

    if "تنظيف" in t:
        return "تنظيف الأسنان"

    if "تقويم" in t:
        return "تقويم الأسنان"

    if "زراعة" in t or "implant" in t:
        return "زراعة أسنان"

    return "غير محددة"


# ============= 8) IMPROVED TEETH COUNT (v4.2) =============
def extract_teeth_count(txt: str):
    # تحسين اللهجة العراقية
    txt = txt.replace("سنين", "2 سن").replace("سنان", "2 سن")

    # تحويل أرقام عربية إلى انجليزية
    arabic_to_en = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    cleaned = txt.translate(arabic_to_en)

    # 1) رقم + سن
    m = re.search(r"(\d+)\s*(سن|سنة|اسنان)", cleaned)
    if m:
        return int(m.group(1))

    # 2) كلمات
    words = {
        "واحد": 1, "واحدة": 1,
        "اثنين": 2, "ثنين": 2,
        "ثلاث": 3, "ثلاثة": 3,
        "اربعة": 4, "خمسة": 5,
        "ستة": 6, "سبعة": 7,
        "ثمانية": 8, "عشرة": 10
    }
    for w, n in words.items():
        if w in txt:
            return n

    # 3) رقم + خدمة (جديد)
    service_keywords = [
        "زركون", "تغليف", "تلبيسة", "تلبيسات",
        "crown", "جسر", "ايماكس", "emax"
    ]

    m2 = re.search(r"(\d+)\s*([A-Za-z\u0600-\u06FF]+)", cleaned)
    if m2:
        number = int(m2.group(1))
        word = m2.group(2)
        for kw in service_keywords:
            if kw in word:
                return number

    return None


# ============= 9) CORE LOGIC =============
def process_user_message(uid, text):
    session = get_session(uid)
    st = session["state"]
    txt_clean = text.strip()

    # اغلاق الجلسة
    count = extract_teeth_count(txt_clean)
    if count:
        session["teeth_count"] = count

    # waiting_name
    if st == "waiting_name":
        if normalize_phone(txt_clean):
            return "هذا شكله رقم، دزلي اسمك الثلاثي حبي ♥️"
        session["temp_name"] = txt_clean
        session["state"] = "waiting_phone"
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "تمام، دزلي رقمك يبدي بـ07 حتى نكملك الحجز ❤️"

    # waiting_phone
    if st == "waiting_phone":
        phone = normalize_phone(txt_clean)
        if not phone:
            return "الرقم يبدي بـ07 وطوله 11 رقم 🙏"
        session["temp_phone"] = phone

        service = session["temp_service"] or "فحص واستشارة"
        if service == "غير محددة":
            service = "فحص واستشارة"

        msg = (
            "تم تأكيد الحجز ❤️\n\n"
            f"الاسم: {session['temp_name']}\n"
            f"الرقم: {phone}\n"
            f"الخدمة: {service}\n\n"
            "راح يتواصل قسم المتابعة وياك بعد شوي 🙏"
        )

        send_to_whatsapp(session["temp_name"], phone, service)

        session["temp_name"] = ""
        session["temp_phone"] = ""
        session["temp_service"] = ""
        session["state"] = "idle"

        return msg

    # detect intent
    intent = detect_intent(txt_clean)
    session["last_intent"] = intent

    if intent == "booking":
        session["state"] = "waiting_name"
        service = detect_service(txt_clean)
        session["temp_service"] = service
        session["last_service"] = service
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "حاضر، دزلي اسمك الثلاثي حتى أسجّلك ❤️"

    if intent == "price":
        service = detect_service(txt_clean)
        if service != "غير محددة":
            session["last_service"] = service
        return get_price_answer(session)

    if intent == "medical":
        session["last_service"] = detect_service(txt_clean)
        ans = medical_ai(uid, text)
        ans += "\n\nإذا تريد نثبتلك موعد حتى الطبيب يشوف حالتك، دزلي اسمك ورقمك ♥️"
        return ans

    return ask_ai(uid, text)


# ============= 10) PRICE =============
def get_price_answer(session):
    service = session.get("last_service")
    count = session.get("teeth_count")

    est = ""
    if count and service in ["تغليف زركون", "تغليف زركون إيماكس"]:
        est = f"\n🔢 عدد الأسنان: {count}\n💰 تقدير السعر: {count * 75000:,} دينار\n"

    prices = {
        "تغليف زركون": (
            "أسعار تغليف الزركون:\n"
            "• فل زركون: 75 ألف\n"
            "• زركون مدمج إيماكس: 100 ألف\n"
            "• زركون ثري دي: 125 ألف\n" + est +
            "السعر النهائي حسب الفحص ❤️"
        ),
        "تغليف زركون إيماكس": "الزركون المدمج إيماكس حوالي 100 ألف للسن الواحد ✨",
        "تغليف إيماكس": "الإيماكس يوصل تقريباً 100 ألف للسن ✨",
        "تبييض الأسنان": "تبييض الأسنان حوالي 100 ألف للجلسة ✨",
        "تقويم الأسنان": "التقويم تقريباً 450 ألف للفك 🙏",
        "تنظيف الأسنان": "تنظيف الأسنان 25 ألف 🌟",
        "حشوة جذر": "حشوة الجذر تقريباً 125 ألف حسب الحالة.",
        "حشوة تجميلية": "الحشوة التجميلية تقريباً 35 ألف ✨",
        "قلع سن": "القلع العادي 25 ألف والجراحي 75 ألف."
    }

    return prices.get(service, (
        "الأسعار الأساسية:\n"
        "• الزركون 75 ألف\n"
        "• الإيماكس 100 ألف\n"
        "• القلع 25 ألف\n"
        "• الحشوة 35 ألف\n"
        "• الجذر 125 ألف\n"
        "• التبييض 100 ألف\n"
        "• التنظيف 25 ألف\n"
        "• التقويم 450 ألف\n"
        "والسعر النهائي يحدد حسب الفحص 🙏"
    ))


# ============= 11) MEDICAL AI =============
def medical_ai(uid, text):
    system_prompt = """
انت مساعد افتراضي لطبيب أسنان في عيادة كولدن لاين.
ممنوع تشخيص، ممنوع أدوية.
حجي عراقي مختصر، وطمّن المراجع.
"""

    user_prompt = f"""
المراجع يسأل عن مشكلة بالأسنان:
{text}

جاوب بشكل:
- الاحتمالات
- شنو يسوي الطبيب عادة
- شنو التصرف الصحيح
- متى لازم يراجع مستعجل
"""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=230
        )
        return rsp.choices[0].message.content.strip()

    except:
        return "من الوصف باين أكو مشكلة، بس مستحيل نحدد بدون فحص. إذا أكو ورم أو ألم قوي لازم تراجع طبيب 🙏"


# ============= 12) GENERAL AI =============
def ask_ai(uid, text):
    session = get_session(uid)

    system_prompt = """
انت علي، موظف كولدن لاين.
لهجتك عراقية لطيفة، رد مختصر وواضح، بلا تشخيص طبي.
"""

    conv = [{"role": "system", "content": system_prompt}]
    conv.extend(session["history"])
    conv.append({"role": "user", "content": text})

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o",
            messages=conv,
            max_tokens=200
        )
        reply = rsp.choices[0].message.content.strip()

    except:
        reply = "صار خلل بسيط، عيد رسالتك حبي 🙏"

    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": reply})

    if len(session["history"]) > MAX_HISTORY:
        session["history"] = session["history"][-MAX_HISTORY:]

    return reply


# ============= 13) PHONE NORMALIZER =============
def normalize_phone(txt: str):
    arabic_to_en = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    digits = re.sub(r"\D+", "", txt.translate(arabic_to_en))

    if digits.startswith("00964"):
        digits = "0" + digits[5:]
    elif digits.startswith("964"):
        digits = "0" + digits[3:]

    if len(digits) > 11:
        digits = digits[:11]

    if digits.startswith("07") and len(digits) == 11:
        return digits

    return None


# ============= 14) WHATSAPP SEND =============
def send_to_whatsapp(name, phone, service):
    if not WHATSAPP_API:
        log("No WHATSAPP_API configured")
        return

    msg = f"حجز جديد:\\nالاسم: {name}\\nرقم: {phone}\\nالخدمة: {service}"
    url = WHATSAPP_API + requests.utils.quote(msg)

    try:
        requests.get(url, timeout=10)
    except Exception as e:
        log("WhatsApp error:", e)


# ============= 15) FACEBOOK SEND =============
def send_message(uid, text):
    if not PAGE_ACCESS_TOKEN:
        log("PAGE_ACCESS_TOKEN not set")
        return

    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": uid}, "message": {"text": text}}

    try:
        requests.post(url, params=params, json=payload, timeout=10)
    except Exception as e:
        log("FB send error:", e)


# ============= 16) ROUTES =============
@app.route("/", methods=["GET"])
def home():
    return "Golden Line bot v4.2 ✔️"


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

    if not data:
        return "NO DATA", 400

    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            uid = ev.get("sender", {}).get("id")
            if not uid:
                continue

            msg = ev.get("message", {})

            if "text" in msg:
                add_message(uid, msg["text"])
            else:
                send_message(uid, "حتى أگدر أساعدك مضبوط، دز استفسارك كتابة حبي 🙏")

    return "OK", 200


# ============= 17) MAIN =============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
