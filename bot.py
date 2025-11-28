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
            drop = []
            for uid, sess in SESSIONS.items():
                if (now - sess["last_active"]) > SESSION_MAX_AGE:
                    drop.append(uid)
            for uid in drop:
                del SESSIONS[uid]


# ============= 4) BUFFER SYSTEM =============
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

    reply = process_user_message(uid, final_text)
    if reply:
        send_message(uid, reply)


def add_message(uid, text):
    session = get_session(uid)
    now = time.time()
    with session["lock"]:
        session["messages_buffer"].append(text)
        session["last_time"] = now

    t = threading.Thread(target=schedule_reply, args=(uid,))
    t.daemon = True
    t.start()


# ============= 5) REMINDER 30 MINUTES =============
def schedule_reminder(uid):
    time.sleep(1800)

    session = SESSIONS.get(uid)
    if session and session["state"] in ["waiting_name", "waiting_phone"]:
        send_message(uid, "بس أذكرك حبي، إذا تريد نكمل الحجز دز اسمك ورقمك ♥️")


# ============= 6) INTENT DETECTION =============
def detect_intent(txt):
    t = txt.lower().replace("أ", "ا")

    # ---- كلمات تعني سعر = بيش / ببيش / يبيش / ييييش
    if re.search(r"ب?ي+ش", t):
        return "price"

    price_words = [
        "عرض", "عروض", "سعر", "اسعار", "شكد", "كم", "بيش", "ببيش"
    ]
    if any(w in t for w in price_words):
        return "price"

    if any(w in t for w in ["احجز", "موعد", "اريد احجز", "حجز"]):
        return "booking"

    if any(w in t for w in [
        "يوجع", "وجع", "ألم", "ورم", "انتفاخ", "التهاب",
        "ينزف", "نزف", "يحكني", "خراج", "ضرس", "سنه", "سن"
    ]):
        return "medical"

    return "normal"


# ============= 7) SERVICE DETECTION =============
def detect_service(txt):
    t = txt.lower()

    if any(w in t for w in ["زركون", "زر", "غلاف", "تلبيس", "تغليف"]):
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

    if "تبييض" in t or "تبيض" in t:
        return "تبييض الأسنان"

    if "تنظيف" in t:
        return "تنظيف الأسنان"

    if "تقويم" in t:
        return "تقويم الأسنان"

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
        "واحد": 1, "وحد": 1,
        "اثنين": 2, "ثنين": 2,
        "ثلاثة": 3, "ثلاث": 3,
        "اربعة": 4, "أربعة": 4,
        "خمسة": 5, "ستة": 6, "سبعة": 7,
        "ثمانية": 8, "تسعة": 9, "عشرة": 10
    }
    for w, n in words.items():
        if w in txt:
            return n

    return None


# ============= 9) CORE LOGIC =============
def process_user_message(uid, text):
    session = get_session(uid)
    t = text.strip().lower()

    # ----- إلغاء الحجز عند كلمات معينة -----
    if session["state"] in ["waiting_name", "waiting_phone"]:
        if any(w in t for w in ["مشكلة", "سؤال", "لحظة", "انتظر", "قبلها", "خل", "عندي"]):
            session["state"] = "idle"
            return "تفضل حبي، كللي شنو المشكلة؟ ❤️"

    # ----- عدد الأسنان -----
    cnt = extract_teeth_count(t)
    if cnt:
        session["teeth_count"] = cnt

    st = session["state"]

    # ====== waiting_name ======
    if st == "waiting_name":
        if normalize_phone(t):
            return "حبي هذا شكل رقم، دزلي اسمك الثلاثي ❤️"

        session["temp_name"] = text.strip()
        session["state"] = "waiting_phone"
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "تمام حبي، هسه دز رقمك يبدي بـ07 حتى أكملك الحجز ❤️"

    # ====== waiting_phone ======
    if st == "waiting_phone":
        phone = normalize_phone(t)
        if not phone:
            return "حبي الرقم يبدي بـ07 وطوله 11 رقم — مثال: 07812345678 🙏"

        session["temp_phone"] = phone
        service = session["temp_service"] or "فحص واستشارة"

        msg = f"تم تأكيد الحجز ❤️\n\nالاسم: {session['temp_name']}\nالرقم: {phone}\nالخدمة: {service}\n\nراح يتواصل ويّاك قسم المتابعة بعد شوي 🙏"

        send_to_whatsapp(session["temp_name"], phone, service)

        session.update({
            "temp_name": "",
            "temp_phone": "",
            "temp_service": "",
            "state": "idle",
            "last_intent": "booking"
        })
        return msg

    # ----- detect intent -----
    intent = detect_intent(t)

    # ====== price ======
    if intent == "price":
        service = detect_service(t)
        if service != "غير محددة":
            session["last_service"] = service
        return get_price_answer(session)

    # ====== booking ======
    if intent == "booking":
        session["state"] = "waiting_name"
        service = detect_service(t)
        session["temp_service"] = service
        session["last_service"] = service
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "حاضر حبي، دزلي اسمك الثلاثي حتى أسجّلك ❤️"

    # ====== medical ======
    if intent == "medical":
        # لكن إذا أكو عدد أسنان + خدمة → نحسب سعر مو طب
        if session["teeth_count"] and detect_service(t) != "غير محددة":
            return get_price_answer(session)

        session["last_service"] = detect_service(t)
        session["last_intent"] = "medical"
        r = medical_ai(uid, text)
        return r + "\n\nإذا تحب أفحصك وأسجّلك موعد، دز اسمك ورقمك ♥️"

    # ====== normal ======
    session["last_intent"] = "normal"
    return ask_ai(uid, text)


# ============= 10) PRICE ENGINE =============
def get_price_answer(session):
    service = session.get("last_service")
    cnt = session.get("teeth_count")

    if service == "تغليف زركون":
        if cnt:
            cost = 75000 * cnt
            return f"حبي تغليف {cnt} أسنان يطلع تقريباً {cost:,} دينار (75 ألف للسن الواحد) ❤️"
        return "سعر تغليف الزركون 75 ألف للسن الواحد ❤️"

    if service == "تغليف زركون إيماكس":
        if cnt:
            cost = 100000 * cnt
            return f"تغليف {cnt} أسنان إيماكس يطلع تقريباً {cost:,} دينار ❤️"
        return "الإيماكس 100 ألف للسن الواحد ❤️"

    if service == "تبييض الأسنان":
        return "تبييض الأسنان بالليزر تقريباً 100 ألف للجلسة ✨"

    if service == "تقويم الأسنان":
        return "التقويم 450 ألف للفك 🙏"

    if service == "تنظيف الأسنان":
        return "تنظيف الأسنان 25 ألف 🌟"

    if service == "حشوة جذر":
        return "حشوة الجذر تقريباً 125 ألف حسب حالة السن."

    if service == "حشوة تجميلية":
        return "الحشوة التجميلية 35 ألف ✨"

    if service == "قلع سن":
        return "القلع العادي 25 ألف والجراحي 75 ألف."

    # Default
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
        rsp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content":
                 "انت مساعد طبي لطبيب أسنان. ممنوع تشخيص أو وصف دواء. جاوب باحتمالات وطمأنة فقط."},
                {"role": "user", "content": text}
            ],
            max_tokens=200
        )
        return rsp.choices[0].message.content.strip()
    except:
        return "الوصف يشير لمشكلة ممكن تكون بسيطة، بس نحتاج فحص حتى نحدد بالضبط 🙏"


# ============= 12) CHAT AI =============
def ask_ai(uid, text):
    session = get_session(uid)

    conv = [{"role": "system", "content":
             "انت علي موظف كولدن لاين، تحجي لبق ومختصر بالعراقي."}]
    conv.extend(session["history"])
    conv.append({"role": "user", "content": text})

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o",
            messages=conv,
            max_tokens=200
        )
        out = rsp.choices[0].message.content.strip()
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


# ============= 14) WHATSAPP SEND (DIRECT) =============
def send_to_whatsapp(name, phone, service):
    try:
        msg = f"حجز جديد:\\nالاسم: {name}\\nرقم: {phone}\\nالخدمة: {service}"
        url = "https://api.callmebot.com/whatsapp.php?phone=9647818931201&apikey=8423339&text=" + requests.utils.quote(msg)
        requests.get(url, timeout=10)
    except:
        pass


# ============= 15) SEND TO FB =============
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


# ============= 16) ROUTES =============
@app.route("/", methods=["GET"])
def home():
    return "Golden Line Bot v4.7 ✔️"


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
