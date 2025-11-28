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


# ============= 2) SESSIONS =============
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TTL = 6 * 60 * 60
BUFFER_DELAY = 2.5
MAX_HISTORY = 8


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


# ============= 3) BUFFER =============
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
        reply = process_user_message(uid, final_text)
        send_message(uid, reply)


def add_message(uid, text):
    now = time.time()
    session = get_session(uid)

    with session["lock"]:
        session["messages_buffer"].append(text)
        session["last_time"] = now

    th = threading.Thread(target=schedule_reply, args=(uid,))
    th.daemon = True
    th.start()


# ============= 4) REMINDER (30 MINUTES) =============
def schedule_reminder(uid):
    time.sleep(1800)  # 30 دقيقة

    session = SESSIONS.get(uid)
    if not session:
        return

    if session["state"] in ["waiting_name", "waiting_phone"]:
        send_message(uid, "بس أذكّرك حبي، إذا تريد نكمّل الحجز دزلي اسمك ورقمك ♥️")


# ============= 5) INTENT DETECTION =============
def detect_intent(txt: str) -> str:
    txt = txt.lower()

    # منع مشكلة "شغلكُم" = كم
    if re.search(r"\b(عرض|عروض|سعر|اسعار|شكد|كم)\b", txt):
        return "price"

    # التبييض → price
    if any(w in txt for w in ["تبييض", "تبيض", "يبيض", "يبيش"]):
        return "price"

    if any(w in txt for w in ["احجز", "موعد", "اريد احجز"]):
        return "booking"

    if any(w in txt for w in [
        "يوجع", "وجع", "ألم", "المال", "ورم", "انتفاخ",
        "التهاب", "ينزف", "نزف", "حساسية", "يحكني",
        "يلتهب", "خراج", "ضرس", "سنه", "سن", "انشلع", "طاح"
    ]):
        return "medical"

    return "normal"


# ============= 6) SERVICE DETECTION =============
def detect_service(txt: str) -> str:
    t = txt.lower()

    if "زر" in t or "زركون" in t or "غلاف" in t:
        if "ايماكس" in t:
            return "تغليف زركون إيماكس"
        return "تغليف زركون"

    if "ايماكس" in t:
        return "تغليف إيماكس"

    if "قلع" in t or "شلع" in t:
        return "قلع سن"

    if "حشوة" in t:
        if "جذر" in t or "عصب" in t:
            return "حشوة جذر"
        return "حشوة تجميلية"

    if any(w in t for w in ["تبييض", "تبيض", "يبيش", "يبيض"]):
        return "تبييض الأسنان"

    if "تنظيف" in t:
        return "تنظيف الأسنان"

    if "تقويم" in t:
        return "تقويم الأسنان"

    if "زراعة" in t:
        return "زراعة أسنان"

    return "غير محددة"


# ============= 7) TEETH COUNT DETECTOR =============
def extract_teeth_count(txt: str):
    txt = txt.replace("سنين", "2 سن")
    txt = txt.replace("سنان", "2 سن")

    arabic_to_en = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    cleaned = txt.translate(arabic_to_en)

    m = re.search(r"(\d+)\s*(سن|اسنان|سنة)", cleaned)
    if m:
        return int(m.group(1))

    words_map = {
        "سن": 1,
        "واحد": 1, "اثنين": 2, "ثنين": 2,
        "ثلاثة": 3, "اربعة": 4, "خمسة": 5,
        "ستة": 6, "سبعة": 7, "ثمانية": 8,
        "تسعة": 9, "عشرة": 10
    }

    for w, n in words_map.items():
        if w in txt:
            return n

    return None


# ============= 8) CORE LOGIC =============
def process_user_message(uid, text):
    session = get_session(uid)
    st = session["state"]
    txt_clean = text.strip()

    # ====== استخلاص عدد الأسنان ======
    count = extract_teeth_count(txt_clean)
    if count:
        session["teeth_count"] = count

    # ====== waiting_name ======
    if st == "waiting_name":
        if normalize_phone(txt_clean):
            return "حبي شكله رقم، دزلي اسمك الثلاثي حتى أسجّلك ❤️"

        session["temp_name"] = txt_clean
        session["state"] = "waiting_phone"

        th = threading.Thread(target=schedule_reminder, args=(uid,))
        th.daemon = True
        th.start()

        return "تمام حبي، هسه دزلي رقمك يبدي بـ07 حتى أكملك الحجز ❤️"

    # ====== waiting_phone ======
    if st == "waiting_phone":
        phone = normalize_phone(txt_clean)
        if not phone:
            return "حبي الرقم يبدي بـ 07 وطوله 11 رقم 🙏"

        session["temp_phone"] = phone

        service = session["temp_service"] or "فحص واستشارة"
        if service == "غير محددة":
            service = "فحص واستشارة"

        msg = (
            "تم تأكيد الحجز ❤️\n\n"
            f"الاسم: {session['temp_name']}\n"
            f"الرقم: {phone}\n"
            f"الخدمة: {service}\n\n"
            "راح يتواصل ويّاك قسم المتابعة بعد شوي 🙏"
        )

        send_to_whatsapp(session["temp_name"], phone, service)

        session["temp_name"] = ""
        session["temp_phone"] = ""
        session["temp_service"] = ""
        session["state"] = "idle"

        return msg

    # ====== detect intent ======
    intent = detect_intent(txt_clean)

    # ==== booking ====
    if intent == "booking":
        session["state"] = "waiting_name"
        service = detect_service(txt_clean)

        session["temp_service"] = service
        session["last_service"] = service

        th = threading.Thread(target=schedule_reminder, args=(uid,))
        th.daemon = True
        th.start()

        return "حاضر حبي، دزلي اسمك الثلاثي حتى أسجّلك الموعد ❤️"

    # ==== price ====
    if intent == "price":
        service = detect_service(txt_clean)
        if service != "غير محددة":
            session["last_service"] = service
        return get_price_answer(session)

    # ==== medical ====
    if intent == "medical":
        session["last_service"] = detect_service(txt_clean)
        response = medical_ai(uid, text)
        response += "\n\nإذا تحب أحجزلّك موعد حتى الطبيب يشوف وضع السن، دزلي اسمك ورقمك ♥️"
        return response

    # ==== normal ====
    return ask_ai(uid, text)


# ============= 9) PRICE ANSWER =============
def get_price_answer(session):
    service = session.get("last_service")
    count = session.get("teeth_count")

    est_text = ""
    if count and service in ["تغليف زركون", "تغليف زركون إيماكس"]:
        price = 75000 * count
        est_text = f"\n🔢 عدد الأسنان: {count}\n💰 التكلفة التقريبية: {price:,} دينار\n"

    if service == "تغليف زركون":
        return (
            "أسعار تغليف الزركون:\n"
            "• فل زركون: 75 ألف\n"
            "• زركون مدمج إيماكس: 100 ألف\n"
            "• زركون ثري دي: 125 ألف\n"
            + est_text +
            "كلها شغل مرتب ومع ضمان ❤️"
        )

    if service == "تبييض الأسنان":
        return "تبييض الأسنان بالليزر تقريباً 100 ألف للجلسة ✨"

    if service == "تقويم الأسنان":
        return "التقويم 450 ألف للفك 🙏"

    if service == "تنظيف الأسنان":
        return "تنظيف الأسنان 25 ألف للجلسة 🌟"

    if service == "حشوة جذر":
        return "حشوة الجذر تقريبا 125 ألف حسب حالة السن."

    if service == "حشوة تجميلية":
        return "الحشوة التجميلية 35 ألف دينار ✨"

    if service == "قلع سن":
        return "القلع العادي 25 ألف والجراحي 75 ألف."

    # default
    return (
        "الأسعار الأساسية:\n"
        "• الزركون 75 ألف\n"
        "• الإيماكس 100 ألف\n"
        "• القلع من 25 ألف\n"
        "• الحشوة 35 ألف\n"
        "• الجذر 125 ألف\n"
        "• التبييض 100 ألف\n"
        "• التنظيف 25 ألف\n"
        "• التقويم 450 ألف\n"
        "والسعر النهائي حسب الفحص 🙏"
    )


# ============= 10) MEDICAL AI =============
def medical_ai(uid, text):
    session = get_session(uid)

    system_prompt = """
انت مساعد افتراضي لطبيب أسنان في عيادة كولدن لاين.
ممنوع تشخيص، ممنوع أدوية.
جاوب باحتمالات، واهدّي المراجع، وخلّ الأسلوب عراقي.
"""

    user_prompt = f"""
المراجع يسأل عن مشكلة بالأسنان:
{text}

جاوبه بشكل:
- الاحتمالات
- شنو يسوي الطبيب عادة
- شي يطمئنه
- تنبيه: إذا أكو ورم/حرارة/ألم قوي لازم يراجع طبيب
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


# ============= 11) GENERAL AI =============
def ask_ai(uid, text):
    session = get_session(uid)

    system_prompt = """
انت "علي" موظف كولدن لاين.
تحجي عراقي، لبق، مختصر، وتهتم بالمراجع.
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
        reply = "صار خلل بسيط، عيد الرسالة حبي 🙏"

    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": reply})

    if len(session["history"]) > MAX_HISTORY:
        session["history"] = session["history"][-MAX_HISTORY:]

    return reply


# ============= 12) PHONE NORMALIZER =============
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


# ============= 13) WHATSAPP SEND =============
def send_to_whatsapp(name, phone, service):
    if not WHATSAPP_API:
        return

    msg = f"حجز جديد:\\nالاسم: {name}\\nرقم: {phone}\\nالخدمة: {service}"
    url = WHATSAPP_API + requests.utils.quote(msg)

    try:
        requests.get(url, timeout=10)
    except:
        pass


# ============= 14) FB SEND =============
def send_message(uid, text):
    if not PAGE_ACCESS_TOKEN:
        return

    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": uid}, "message": {"text": text}}

    try:
        requests.post(url, params=params, json=payload, timeout=10)
    except:
        pass


# ============= 15) ROUTES =============
@app.route("/", methods=["GET"])
def home():
    return "Golden Line bot v3.0 ✔️"


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
            if "message" in ev and "text" in ev["message"]:
                uid = ev["sender"]["id"]
                text = ev["message"]["text"]
                add_message(uid, text)

    return "OK", 200


# ============= 16) MAIN =============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
