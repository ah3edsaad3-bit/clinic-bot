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

BUFFER_DELAY = 10            # تجميع رسائل 10 ثواني
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


# ============= 5) REMINDER 30 MINUTES =============
def schedule_reminder(uid):
    time.sleep(1800)
    session = SESSIONS.get(uid)
    if session and session["state"] in ["waiting_name", "waiting_phone"]:
        send_message(uid, "بس أذكرك حبي، إذا تريد نكمل الحجز دز اسمك ورقمك ♥️")


# ============= 6) INTENT DETECTION =============
def detect_intent(txt: str) -> str:
    t = txt.lower().replace("أ", "ا")

    # 1) complaint (أعلى أولوية)
    complaint_words = [
        "افشل", "فاشل", "مو مضبوط", "مكسور", "مكسوره", "تنكسر",
        "حرام", "نصاب", "غلط", "الاطباق غلط", "خسرت", "مليون",
        "افلوس", "انضحك", "قهر", "ضايج", "مو نفس"
    ]
    if any(w in t for w in complaint_words):
        return "complaint"

    # 2) offers (شنو عروضكم؟)
    if "عروضكم" in t or "شنو عروضكم" in t or ("عروض" in t and "سعر" not in t and "كم" not in t):
        return "offers"

    # 3) price (بيش / ببيش / يبيش ...)
    if re.search(r"ب?ي+ش", t):
        return "price"

    if any(w in t for w in ["سعر", "اسعار", "شكد", "كم"]):
        return "price"

    # 4) booking
    if any(w in t for w in ["احجز", "حجز", "موعد"]):
        return "booking"

    # 5) medical
    if any(w in t for w in [
        "يوجع", "وجع", "الم", "ورم", "انتفاخ", "التهاب",
        "ينزف", "نزف", "ضرس", "سنه", "سن", "خراج"
    ]):
        return "medical"

    return "normal"


# ============= 7) SERVICE DETECTION =============
def detect_service(txt: str) -> str:
    t = txt.lower()

    # ابتسامة عامة
    if any(w in t for w in ["ابتسامة", "ابتسامه", "سمايل", "smile"]):
        return "ابتسامة زركون"

    # زركون / تغليف
    if any(w in t for w in ["زركون", "غلاف", "زر", "تلبيس", "تغليف"]):
        if "ايماكس" in t or "إيماكس" in t:
            return "تغليف زركون إيماكس"
        return "تغليف زركون"

    if "ايماكس" in t or "إيماكس" in t:
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
def extract_teeth_count(txt: str):
    txt = txt.replace("سنين", "2 سن").replace("سنان", "2 سن")
    txt = txt.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))

    m = re.search(r"(\d+)\s*(سن|اسنان|أسنان)", txt)
    if m:
        return int(m.group(1))

    m = re.search(r"(\d+)\s*(زركون|غلاف|تلبيس|تغليف)", txt)
    if m:
        return int(m.group(1))

    words = {
        "واحد": 1, "اثنين": 2, "ثنين": 2,
        "ثلاثة": 3, "ثلاث": 3,
        "اربعة": 4, "خمسة": 5,
        "ستة": 6, "سبعة": 7,
        "ثمانية": 8, "تسعة": 9, "عشرة": 10
    }
    for w, n in words.items():
        if w in txt:
            return n

    return None


# ============= 9) CORE LOGIC =============
def process_user_message(uid, text):
    session = get_session(uid)
    txt_clean = text.strip()
    t = txt_clean.lower()

    # إلغاء الحجز إذا كتب "عندي مشكلة / لحظة ..."
    if session["state"] in ["waiting_name", "waiting_phone"]:
        if any(w in t for w in ["مشكلة", "لحظة", "انتظر", "قبلها", "عندي", "سؤال"]):
            session["state"] = "idle"
            return "تفضل حبي، كللي شنو المشكلة؟ ❤️"

    # تعامل خاص بعد شكوى: إذا گال "اي" → نعتبره موافق على مراجعة
    if session.get("last_intent") == "complaint":
        if txt_clean.strip() in ["اي", "إي", "ايه", "نعم", "اوك", "اوكي", "تمام"]:
            session["state"] = "waiting_name"
            session["temp_service"] = "مراجعة شكوى"
            return "حاضر حبي، دزلي اسمك الثلاثي حتى أحجزلِك مراجعة ويشوفك الدكتور ❤️"

    # عدد الأسنان
    cnt = extract_teeth_count(txt_clean)
    if cnt:
        session["teeth_count"] = cnt

    st = session["state"]

    # ====== waiting_name ======
    if st == "waiting_name":
        phone = normalize_phone(txt_clean)
        name_candidate = re.sub(r"\d+", "", txt_clean).strip()

        # اسم + رقم بنفس الرسالة
        if phone and len(name_candidate.split()) >= 1:
            session["temp_name"] = name_candidate
            session["temp_phone"] = phone
            service = session["temp_service"] or "فحص واستشارة"
            send_to_whatsapp(name_candidate, phone, service)
            session.update({
                "temp_name": "",
                "temp_phone": "",
                "temp_service": "",
                "state": "idle",
                "last_intent": "booking"
            })
            return (
                "تم تأكيد الحجز ❤️\n\n"
                f"الاسم: {name_candidate}\n"
                f"الرقم: {phone}\n"
                f"الخدمة: {service}\n"
                "راح يتواصل ويّاك قسم المتابعة بعد شوي 🙏"
            )

        if phone and not name_candidate:
            return "حبي هذا شكل رقم، دزلي اسمك الثلاثي ❤️"

        # اسم فقط
        session["temp_name"] = txt_clean
        session["state"] = "waiting_phone"
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        session["last_intent"] = "booking"
        return "تمام حبي، هسه دز رقمك يبدي بـ07 حتى أكملك الحجز ❤️"

    # ====== waiting_phone ======
    if st == "waiting_phone":
        phone = normalize_phone(txt_clean)
        if not phone:
            return "حبي الرقم يبدي بـ07 وطوله 11 رقم — مثال: 07812345678 🙏"

        session["temp_phone"] = phone
        service = session["temp_service"] or "فحص واستشارة"
        send_to_whatsapp(session["temp_name"], phone, service)

        msg = (
            "تم تأكيد الحجز ❤️\n\n"
            f"الاسم: {session['temp_name']}\n"
            f"الرقم: {phone}\n"
            f"الخدمة: {service}\n"
            "راح يتواصل ويّاك قسم المتابعة بعد شوي 🙏"
        )

        session.update({
            "temp_name": "",
            "temp_phone": "",
            "temp_service": "",
            "state": "idle",
            "last_intent": "booking"
        })
        return msg

    # ====== detect intent ======
    intent = detect_intent(txt_clean)

    # ----- offers -----
    if intent == "offers":
        session["last_intent"] = "offers"
        return (
            "حبي عروضنا الحالية 🌟:\n"
            "• تغليف زركون للسن 75 ألف\n"
            "• ابتسامة زركون كاملة 16 سن 750 ألف\n"
            "• تبييض ليزر للجلسة 100 ألف\n"
            "• تنظيف أسنان 25 ألف\n"
            "• تقويم الأسنان 450 ألف للفك\n"
            "والسعر النهائي دائماً حسب الفحص ووضع الأسنان 🙏"
        )

    # ----- complaint -----
    if intent == "complaint":
        session["last_intent"] = "complaint"
        return (
            "حبي آسف إذا مرّيت بهيج تجربة وحقّك علينا 🌿\n"
            "خليني أفهم منك شنو اللي صار وبأي سن صارت المشكلة؟\n"
            "وإذا تحب أحجزلِك مراجعة مجانية ويشوفك الدكتور مباشرة حتى نحلها ❤️"
        )

    # ----- price -----
    if intent == "price":
        service = detect_service(txt_clean)
        session["last_service"] = service
        session["last_intent"] = "price"
        return get_price_answer(session)

    # ----- booking -----
    if intent == "booking":
        service = detect_service(txt_clean)
        session["temp_service"] = service
        session["state"] = "waiting_name"
        session["last_intent"] = "booking"
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "حاضر حبي، دزلي اسمك الثلاثي حتى أسجّلك ❤️"

    # ----- medical -----
    if intent == "medical":
        # إذا ذكر عدد أسنان + خدمة → نعطي سعر أول
        service = detect_service(txt_clean)
        if session.get("teeth_count") and service != "غير محددة":
            session["last_service"] = service
            session["last_intent"] = "price"
            return get_price_answer(session)

        session["last_intent"] = "medical"
        session["last_service"] = service
        r = medical_ai(uid, text)
        return r + "\n\nإذا تحب نحجزلك موعد حتى الدكتور يشوفها، دز اسمك ورقمك ♥️"

    # ----- normal -----
    session["last_intent"] = "normal"
    return ask_ai(uid, text)


# ============= 10) PRICE ENGINE =============
def get_price_answer(session):
    service = session.get("last_service")
    cnt = session.get("teeth_count")

    if service == "ابتسامة زركون":
        return "سعر ابتسامة الزركون الكاملة (16 سن) هو 750,000 دينار ❤️ يشمل كل شيء."

    if service == "تغليف زركون":
        if cnt:
            price = 75000 * cnt
            return f"حبي تغليف {cnt} أسنان زركون يطلع تقريباً {price:,} دينار (75 ألف للسن الواحد) ❤️"
        return "سعر تغليف الزركون 75 ألف للسن الواحد، والابتسامة الكاملة 16 سن 750 ألف ❤️"

    if service == "تغليف زركون إيماكس":
        if cnt:
            price = 100000 * cnt
            return f"تغليف {cnt} أسنان زركون إيماكس يطلع تقريباً {price:,} دينار ❤️"
        return "سعر تغليف الإيماكس 100 ألف للسن الواحد ❤️"

    if service == "تبييض الأسنان":
        return "تبييض الأسنان بالليزر تقريباً 100 ألف للجلسة ✨"

    if service == "تنظيف الأسنان":
        return "تنظيف الأسنان 25 ألف للجلسة 🌟"

    if service == "تقويم الأسنان":
        return "التقويم 450 ألف للفك 🙏"

    if service == "حشوة تجميلية":
        return "الحشوة التجميلية 35 ألف دينار ✨"

    if service == "حشوة جذر":
        return "حشوة الجذر تقريباً 125 ألف حسب حالة السن."

    if service == "قلع سن":
        return "القلع العادي 25 ألف والجراحي 75 ألف تقريباً."

    # default: إذا ما عرف الخدمة نهائياً
    return (
        "الأسعار الأساسية:\n"
        "• الزركون 75 ألف للسن\n"
        "• الإيماكس 100 ألف للسن\n"
        "• ابتسامة زركون كاملة 16 سن 750 ألف\n"
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
                {
                    "role": "system",
                    "content": (
                        "انت مساعد طبي لطبيب أسنان في عيادة كولدن لاين. "
                        "ممنوع تشخيص نهائي أو وصف أدوية. "
                        "جاوب باحتمالات عامة، وتهدئة، ونصيحة بمراجعة الطبيب عند الألم الشديد أو الورم."
                    )
                },
                {"role": "user", "content": text}
            ],
            max_tokens=230
        )
        return res.choices[0].message.content.strip()
    except Exception:
        return "الوصف يشير لمشكلة تحتاج فحص مباشر، إذا أكو ألم قوي أو ورم لازم تراجع طبيب أسنان بأقرب وقت 🙏"


# ============= 12) GENERAL CHAT AI =============
def ask_ai(uid, text):
    session = get_session(uid)

    conv = [
        {
            "role": "system",
            "content": (
                "انت علي موظف كولدن لاين لطب وتجميل الأسنان في بغداد. "
                "تحجي عراقي لبق، مختصر، وتهتم بالمراجع. "
                "لا تعطي تشخيص ولا أدوية، وركّز تشرح العروض والخدمات بهدوء."
            )
        }
    ]
    conv.extend(session["history"])
    conv.append({"role": "user", "content": text})

    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=conv,
            max_tokens=200
        )
        reply = res.choices[0].message.content.strip()
    except Exception:
        reply = "صار خلل بسيط، عيد الرسالة حبي 🙏"

    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": reply})

    if len(session["history"]) > MAX_HISTORY:
        session["history"] = session["history"][-MAX_HISTORY:]

    return reply


# ============= 13) PHONE NORMALIZER =============
def normalize_phone(t: str):
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
        url = (
            "https://api.callmebot.com/whatsapp.php"
            "?phone=9647818931201&apikey=8423339&text="
            + requests.utils.quote(msg)
        )
        requests.get(url, timeout=10)
    except Exception:
        pass


# ============= 15) FB SEND =============
def send_message(uid, text):
    if not PAGE_ACCESS_TOKEN:
        return
    url = "https://graph.facebook.com/v18.0/me/messages"
    payload = {"recipient": {"id": uid}, "message": {"text": text}}
    try:
        requests.post(url, params={"access_token": PAGE_ACCESS_TOKEN}, json=payload, timeout=10)
    except Exception:
        pass


# ============= 16) ROUTES =============
@app.route("/", methods=["GET"])
def home():
    return "Golden Line Bot v5.0 ✔️"


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
