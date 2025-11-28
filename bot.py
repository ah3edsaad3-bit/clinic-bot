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
client = OpenAI(api_key=OPENAI_API_KEY)

# واتساب مباشر
WHATSAPP_URL = "https://api.callmebot.com/whatsapp.php?phone=9647818931201&apikey=8423339&text="

# ============= 2) SESSIONS =============
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TTL = 6 * 60 * 60
BUFFER_DELAY = 10     # تجميع 10 ثواني
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
        if reply:
            send_message(uid, reply)

def add_message(uid, text):
    if len(text.strip()) <= 1:
        return
    now = time.time()
    session = get_session(uid)
    with session["lock"]:
        session["messages_buffer"].append(text)
        session["last_time"] = now

    th = threading.Thread(target=schedule_reply, args=(uid,))
    th.daemon = True
    th.start()

# ============= 4) REMINDER 30min =============
def schedule_reminder(uid):
    time.sleep(1800)
    session = SESSIONS.get(uid)
    if session and session["state"] in ["waiting_name", "waiting_phone"]:
        send_message(uid, "بس أذكّرك حبي، إذا تريد نكمّل الحجز دزلي اسمك ورقمك ♥️")

# ============= 5) INTENT DETECTOR =============
def detect_intent(txt):
    t = txt.lower()

    if "عروضكم" in t:
        return "offers"

    if any(w in t for w in ["سعر", "بيش", "شكد", "كم"]):
        return "price"

    if "احجز" in t or "موعد" in t:
        return "booking"

    if any(w in t for w in [
        "يوجع", "وجع", "ألم", "ورم", "انتفاخ",
        "التهاب", "ينزف", "نزف", "حساسية",
        "يحكني", "يلتهب", "خراج"
    ]):
        return "medical"

    return "normal"

# ============= 6) SERVICE DETECTOR =============
def detect_service(txt):
    t = txt.lower()

    if any(w in t for w in ["ابتسامة", "ابتسامه", "سمايل"]):
        return "ابتسامة زركون"

    if "زركون" in t:
        return "تغليف زركون"

    if "ايماكس" in t:
        return "تغليف إيماكس"

    if "حشوة" in t:
        return "حشوة تجميلية"

    if "جذر" in t or "عصب" in t:
        return "حشوة جذر"

    if "قلع" in t or "شلع" in t:
        return "قلع سن"

    if "تنظيف" in t:
        return "تنظيف الأسنان"

    if "تبييض" in t or "تبيض" in t:
        return "تبييض الأسنان"

    if "تقويم" in t:
        return "تقويم الأسنان"

    return "غير محددة"

# ============= 7) TEETH COUNT =============
def extract_teeth_count(txt):
    txt = txt.replace("سنين", "2 سن").replace("سنان", "2 سن")

    arabic_to_en = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    cleaned = txt.translate(arabic_to_en)

    m = re.search(r"(\d+)\s*", cleaned)
    if m:
        return int(m.group(1))

    return None

# ============= 8) CORE ======================
def process_user_message(uid, text):
    session = get_session(uid)
    st = session["state"]
    txt = text.strip()

    # Service tracking
    service_now = detect_service(txt)
    if service_now == "غير محددة" and session["last_service"]:
        service_now = session["last_service"]
    else:
        session["last_service"] = service_now

    # Teeth count
    count = extract_teeth_count(txt)
    if count:
        session["teeth_count"] = count

    # ------------- Booking flow -------------
    if st == "waiting_name":
        if normalize_phone(txt):
            return "حبي هذا رقم، دزلي اسمك الثلاثي ❤️"
        session["temp_name"] = txt
        session["state"] = "waiting_phone"
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "تمام حبي، هسه دزلي رقمك يبدي بـ07 حتى أكملك الحجز ❤️"

    if st == "waiting_phone":
        phone = normalize_phone(txt)
        if not phone:
            return "حبي الرقم يبدي بـ07 وطوله 11 رقم 🙏"

        session["temp_phone"] = phone

        service = session["temp_service"] or "معاينة واستشارة مجانية"

        msg = (
            "تم تأكيد الحجز ❤️\n\n"
            f"الاسم: {session['temp_name']}\n"
            f"الرقم: {phone}\n"
            f"الخدمة: {service}\n"
            "سوف يتم التواصل معك من قبل خدمة العملاء خلال دقائق لتثبيت الحجز وتحديد الموعد المناسب لحضرتكم 🙏"
        )

        send_whatsapp(session["temp_name"], phone, service)

        session["temp_name"] = ""
        session["temp_phone"] = ""
        session["temp_service"] = ""
        session["state"] = "idle"
        return msg

    # ------------ Intent detection --------------
    intent = detect_intent(txt)

    # Offers
    if intent == "offers":
        return (
            "حبي عروضنا الحالية:\n"
            "• تغليف زركون 75 ألف\n"
            "• تبييض ليزر 100 ألف\n"
            "• تقويم 450 ألف\n"
            "• تنظيف 25 ألف\n"
            "والمعاينة مجانية ❤️"
        )

    # Booking
    if intent == "booking":
        session["state"] = "waiting_name"
        session["temp_service"] = service_now
        threading.Thread(target=schedule_reminder, args=(uid,), daemon=True).start()
        return "حاضر حبي، دزلي اسمك الثلاثي حتى أسجّلك الموعد ❤️"

    # Price
    if intent == "price" or (count and service_now != "غير محددة"):
        return get_price(service_now, session.get("teeth_count"))

    # Medical
    if intent == "medical":
        session["last_intent"] = "medical"
        resp = medical_ai(text)
        return resp + "\n\nإذا تحب أحجزلّك معاينة مجانية هنا، دزلي اسمك ورقمك ♥️"

    # Normal
    return ask_ai(uid, txt)

# ============= 9) PRICE SYSTEM =============
def get_price(service, count):
    if service == "ابتسامة زركون":
        return "سعر ابتسامة الزركون الكاملة 16 سن هو 750,000 دينار ♥️"

    if service == "تغليف زركون":
        if count:
            return f"تغليف {count} أسنان زركون يطلع تقريباً {count * 75000:,} دينار ❤️"
        return "سعر تغليف الزركون 75 ألف للسن الواحد ❤️"

    if service == "تغليف إيماكس":
        return "سعر الإيماكس 100 ألف للسن ❤️"

    if service == "تنظيف الأسنان":
        return "تنظيف الأسنان 25 ألف ❤️"

    if service == "تبييض الأسنان":
        return "تبييض الأسنان 100 ألف ❤️"

    if service == "تقويم الأسنان":
        return "التقويم 450 ألف ❤️"

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

# ============= 10) MEDICAL AI =============
def medical_ai(text):
    system = """
انت مساعد افتراضي لطبيب أسنان.
ممنوع تشخيص مباشر.
جاوب باحتمالات وتهدئة وبأسلوب عراقي.
"""
    user = f"المراجع يكول: {text}"

    try:
        r = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            max_tokens=200
        )
        return r.choices[0].message.content
    except:
        return "حبي من الوصف واضح أكو مشكلة بسيطة، ويحتاج فحص حتى نحددها ❤️"

# ============= 11) GENERAL AI =============
def ask_ai(uid, text):
    session = get_session(uid)
    system_prompt = "إنت (علي) موظف كولدن لاين، تحچي عراقي وباختصار وتهتم بالمراجع."

    conv = [{"role": "system", "content": system_prompt}]
    conv.extend(session["history"])
    conv.append({"role": "user", "content": text})

    try:
        r = client.chat.completions.create(
            model="gpt-4o",
            messages=conv,
            max_tokens=200
        )
        reply = r.choices[0].message.content
    except:
        reply = "صار خلل بسيط، عيد الرسالة حبي 🙏"

    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": reply})

    if len(session["history"]) > MAX_HISTORY:
        session["history"] = session["history"][-MAX_HISTORY:]

    return reply

# ============= 12) PHONE NORMALIZER =============
def normalize_phone(txt):
    d = re.sub(r"\D+", "", txt)
    if d.startswith("00964"):
        d = "0" + d[5:]
    elif d.startswith("964"):
        d = "0" + d[3:]
    if len(d) == 11 and d.startswith("07"):
        return d
    return None

# ============= 13) SEND WHATSAPP =============
def send_whatsapp(name, phone, service):
    msg = f"حجز جديد:\nالاسم: {name}\nالرقم: {phone}\nالخدمة: {service}"
    try:
        requests.get(WHATSAPP_URL + requests.utils.quote(msg), timeout=10)
    except:
        pass

# ============= 14) SEND FB MESSAGE =============
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
    return "Golden Line bot v5.2 ✔️"

@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Error", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            if "message" in ev and "text" in ev["message"]:
                uid = ev["sender"]["id"]
                add_message(uid, ev["message"]["text"])
    return "OK", 200

# ============= 16) MAIN =============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
