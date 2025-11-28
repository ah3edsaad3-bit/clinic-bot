from flask import Flask, request
import requests
from openai import OpenAI
import time
import threading
import os
import re

app = Flask(__name__)

# ============= 1) CONFIG =============
VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

BUFFER_DELAY = 10
MAX_HISTORY = 8
SESSION_TTL = 6 * 3600
SESSION_MAX_AGE = 24 * 3600
CLEANER_INTERVAL = 3600

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


# ============= 2) SESSION CREATION =============
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


# ============= 3) CLEANER =============
def session_cleaner():
    while True:
        time.sleep(CLEANER_INTERVAL)
        now = time.time()
        with SESSIONS_LOCK:
            remove = []
            for uid, sess in SESSIONS.items():
                if (now - sess["last_active"]) > SESSION_MAX_AGE:
                    remove.append(uid)
            for uid in remove:
                del SESSIONS[uid]


# ============= 4) BUFFER =============
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
        if reply:
            send_message(uid, reply)


def add_message(uid, text):
    session = get_session(uid)
    with session["lock"]:
        session["messages_buffer"].append(text)
        session["last_time"] = time.time()

    t = threading.Thread(target=schedule_reply, args=(uid,))
    t.daemon = True
    t.start()


# ============= 5) 30-MIN REMINDER =============
def schedule_reminder(uid):
    time.sleep(1800)
    session = SESSIONS.get(uid)
    if session and session["state"] in ["waiting_name", "waiting_phone"]:
        send_message(uid, "بس أذكرك حبي، إذا تريد نكمل الحجز دز اسمك ورقمك ♥️")


# ============= 6) INTENT DETECTION =============
def detect_intent(txt):
    t = txt.lower().replace("أ", "ا")

    # ---- complaint detection (أعلى أولوية)
    complaint_words = [
        "افشل", "فاشل", "مو مضبوط", "مكسور", "تنكسر", "مكسوره",
        "حرام", "نصاب", "غلط", "الاطباق غلط", "خسرت", "مليون",
        "افلوس", "انضحك", "قهر", "ضايج", "مو نفس"
    ]
    if any(w in t for w in complaint_words):
        return "complaint"

    # ---- price intent (بيش / يبيش / ببيش)
    if re.search(r"ب?ي+ش", t):
        return "price"

    if any(w in t for w in ["عرض", "سعر", "اسعار", "شكد", "كم"]):
        return "price"

    # ---- booking
    if any(w in t for w in ["احجز", "حجز", "موعد"]):
        return "booking"

    # ---- medical
    if any(w in t for w in [
        "يوجع", "وجع", "ألم", "ورم", "انتفاخ", "التهاب",
        "ينزف", "نزف", "ضرس", "سنه", "سن", "خراج"
    ]):
        return "medical"

    return "normal"


# ============= 7) SERVICE DETECTION =============
def detect_service(txt):
    t = txt.lower()

    if any(w in t for w in ["زركون", "غلاف", "زر", "تلبيس", "تغليف"]):
        if "ايماكس" in t:
            return "تغليف زركون إيماكس"
        return "تغليف زركون"

    if "ايماكس" in t:
        return "تغليف إيماكس"

    if "حشوة" in t:
        if "جذر" in t or "عصب" in t:
            return "حشوة جذر"
        return "حشوة تجميلية"

    if "قلع" in t or "شلع" in t:
        return "قلع سن"

    if "تنظيف" in t:
        return "تنظيف الأسنان"

    if "تقويم" in t:
        return "تقويم الأسنان"

    if "تبييض" in t or "تبيض" in t:
        return "تبييض الأسنان"

    if "زراعة" in t:
        return "زراعة أسنان"

    return "غير محددة"


# ============= 8) TEETH COUNT =============
def extract_teeth_count(txt):
    txt = txt.replace("سنين", "2 سن").replace("سنان", "2 سن")
    txt = txt.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))

    m = re.search(r"(\d+)\s*(سن|اسنان|أسنان)", txt)
    if m:
        return int(m.group(1))

    m = re.search(r"(\d+)\s*(زركون|غلاف|تلبيس|تغليف)", txt)
    if m:
        return int(m.group(1))

    words = {
        "واحد": 1, "اثنين": 2, "ثلاثة": 3, "ثلاث": 3,
        "اربعة": 4, "خمسة": 5, "ستة": 6, "سبعة": 7,
        "ثمانية": 8, "تسعة": 9, "عشرة": 10
    }
    for w, n in words.items():
        if w in txt:
            return n

    return None


# ============= 9) CORE LOGIC =============
def process_user_message(uid, text):
    session = get_session(uid)
    t = text.lower()

    # إلغاء الحجز إذا كتب (عندي مشكلة / لحظة / قبلها)
    if session["state"] in ["waiting_name", "waiting_phone"]:
        if any(w in t for w in ["مشكلة", "لحظة", "انتظر", "قبلها", "عندي", "سؤال"]):
            session["state"] = "idle"
            return "تفضل حبي، كللي شنو المشكلة؟ ❤️"

    # عدد الأسنان
    cnt = extract_teeth_count(text)
    if cnt:
        session["teeth_count"] = cnt

    st = session["state"]

    # ====== waiting_name ======
    if st == "waiting_name":
        phone = normalize_phone(text)
        name_candidate = re.sub(r"\d+", "", text).strip()

        # اسم + رقم سوا
        if phone and len(name_candidate.split()) >= 1:
            session["temp_name"] = name_candidate
            session["temp_phone"] = phone
            service = session["temp_service"] or "فحص واستشارة"
            send_to_whatsapp(name_candidate, phone, service)
            session.update({"temp_name": "", "temp_phone": "", "temp_service": "", "state": "idle"})
            return f"تم تأكيد الحجز ❤️\n\nالاسم: {name_candidate}\nالرقم: {phone}\nالخدمة: {service}"

        if phone:
            return "حبي هذا شكل رقم، دزلي اسمك الثلاثي ❤️"

        session["temp_name"] = text
        session["state"] = "waiting_phone"
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "تمام حبي، هسه دز رقمك يبدي بـ07 حتى أكملك الحجز ❤️"

    # ====== waiting_phone ======
    if st == "waiting_phone":
        phone = normalize_phone(text)
        if not phone:
            return "حبي الرقم يبدي بـ07 وطوله 11 رقم — مثال: 07812345678 🙏"

        session["temp_phone"] = phone
        service = session["temp_service"] or "فحص واستشارة"

        send_to_whatsapp(session["temp_name"], phone, service)

        msg = (
            "تم تأكيد الحجز ❤️\n\n"
            f"الاسم: {session['temp_name']}\n"
            f"الرقم: {phone}\n"
            f"الخدمة: {service}"
        )

        session.update({"temp_name": "", "temp_phone": "", "temp_service": "", "state": "idle"})
        return msg

    # ----- detect intent -----
    intent = detect_intent(text)

    # ====== complaint ======
    if intent == "complaint":
        return (
            "حبي آسف إذا مرّيت بهيج تجربة وحقّك علينا 🌿\n"
            "خليني أفهم منك شنو اللي صار وبأي سن صارت المشكلة؟\n"
            "وإذا تحب أحجزلك مراجعة مجانية ويشوفك الدكتور مباشرة ❤️"
        )

    # ====== price ======
    if intent == "price":
        session["last_service"] = detect_service(text)
        return get_price_answer(session)

    # ====== booking ======
    if intent == "booking":
        service = detect_service(text)
        session["temp_service"] = service
        session["state"] = "waiting_name"
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "حاضر حبي، دزلي اسمك الثلاثي حتى أسجّلك ❤️"

    # ====== medical ======
    if intent == "medical":
        # إذا بيها عدد أسنان وخدمة → سعر مو طب
        if session.get("teeth_count") and detect_service(text) != "غير محددة":
            return get_price_answer(session)

        r = medical_ai(uid, text)
        return r + "\n\nإذا تحب نحجزلك موعد حتى الدكتور يشوفها، دز اسمك ورقمك ♥️"

    # ====== normal ======
    return ask_ai(uid, text)


# ============= 10) PRICE ENGINE =============
def get_price_answer(session):
    service = session.get("last_service")
    cnt = session.get("teeth_count")

    if service == "تغليف زركون":
        if cnt:
            return f"حبي تغليف {cnt} أسنان يطلع تقريباً {cnt * 75000:,} دينار ❤️"
        return "سعر تغليف الزركون 75 ألف للسن ❤️"

    if service == "تغليف زركون إيماكس":
        if cnt:
            return f"تغليف {cnt} أسنان إيماكس يطلع تقريباً {cnt * 100000:,} دينار ❤️"
        return "سعر الإيماكس 100 ألف للسن ❤️"

    if service == "تبييض الأسنان":
        return "تبييض الأسنان 100 ألف للجلسة ✨"

    if service == "تنظيف الأسنان":
        return "تنظيف الأسنان 25 ألف 🌟"

    if service == "تقويم الأسنان":
        return "التقويم 450 ألف للفك 🙏"

    if service == "حشوة تجميلية":
        return "الحشوة التجميلية 35 ألف ✨"

    if service == "حشوة جذر":
        return "حشوة الجذر تقريباً 125 ألف حسب الحالة."

    if service == "قلع سن":
        return "القلع من 25 إلى 75 ألف حسب الحالة."

    return (
        "الأسعار الأساسية:\n"
        "• الزركون 75 ألف\n"
        "• الإيماكس 100 ألف\n"
        "• القلع 25–75 ألف\n"
        "• الحشوة 35 ألف\n"
        "• الجذر 125 ألف\n"
        "• التبييض 100 ألف\n"
        "• التنظيف 25 ألف\n"
        "• التقويم 450 ألف\n"
        "والسعر النهائي حسب الفحص 🙏"
    )


# ============= 11) MEDICAL AI =============
def medical_ai(uid, text):
    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "انت مساعد طبي. ممنوع تشخيص أو أدوية. جاوب باحتمالات وتهدئة."},
                {"role": "user", "content": text}
            ],
            max_tokens=200
        )
        return res.choices[0].message.content.strip()
    except:
        return "الوصف يشير لمشكلة تحتاج فحص، إذا أكو ألم قوي أو ورم لازم تراجع طبيب 🙏"


# ============= 12) CHAT AI =============
def ask_ai(uid, text):
    session = get_session(uid)

    conv = [{"role": "system", "content": "انت علي موظف كولدن لاين، تحجي لبق ومختصر."}]
    conv.extend(session["history"])
    conv.append({"role": "user", "content": text})

    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=conv,
            max_tokens=200
        )
        out = res.choices[0].message.content.strip()
    except:
        out = "صار خلل بسيط، عيد الرسالة حبي 🙏"

    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": out})

    if len(session["history"]) > MAX_HISTORY:
        session["history"] = session["history"][-MAX_HISTORY:]

    return out


# ============= 13) PHONE NORMALIZER =============
def normalize_phone(t):
    t = t.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    digits = re.sub(r"\D+", "", t)

    if digits.startswith("00964"):
        digits = "0" + digits[5:]
    elif digits.startswith("964"):
        digits = "0" + digits[3:]

    if len(digits) == 11 and digits.startswith("07"):
        return digits
    return None


# ============= 14) WHATSAPP SEND =============
def send_to_whatsapp(name, phone, service):
    try:
        msg = f"حجز جديد:\\nالاسم: {name}\\nرقم: {phone}\\nالخدمة: {service}"
        url = "https://api.callmebot.com/whatsapp.php?phone=9647818931201&apikey=8423339&text=" + requests.utils.quote(msg)
        requests.get(url, timeout=10)
    except:
        pass


# ============= 15) FB SEND =============
def send_message(uid, text):
    if not PAGE_ACCESS_TOKEN:
        return
    url = "https://graph.facebook.com/v18.0/me/messages"
    payload = {"recipient": {"id": uid}, "message": {"text": text}}
    try:
        requests.post(url, params={"access_token": PAGE_ACCESS_TOKEN}, json=payload, timeout=10)
    except:
        pass


# ============= 16) ROUTES =============
@app.route("/", methods=["GET"])
def home():
    return "Golden Line Bot v4.8 ✔️"


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
    data = request.get_json() or {}
    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            if "message" in ev and "text" in ev["message"]:
                uid = ev["sender"]["id"]
                text = ev["message"]["text"]
                add_message(uid, text)
    return "OK", 200


if __name__ == "__main__":
    threading.Thread(target=session_cleaner, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
