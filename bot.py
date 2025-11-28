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
WHATSAPP_API = os.getenv("WHATSAPP_API")  # مثال: https://api.callmebot.com/whatsapp.php?phone=9647818931201&apikey=8423339&text=

if not PAGE_ACCESS_TOKEN:
    print("⚠️ WARNING: PAGE_ACCESS_TOKEN is not set!")
if not OPENAI_API_KEY:
    print("⚠️ WARNING: OPENAI_API_KEY is not set!")

client = OpenAI(api_key=OPENAI_API_KEY)

# ============= 2) SESSIONS =============
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()  # قفل عام لحماية القاموس الرئيسي
SESSION_TTL = 6 * 60 * 60         # 6 ساعات عمر الجلسة
BUFFER_DELAY = 7                  # ثانية واحدة أنسب للتجربة الواقعية
MAX_HISTORY = 8                   # عدد الرسائل اللي نخليها بالذاكرة


def new_session():
    """إنشاء جلسة جديدة للمراجع."""
    return {
        "messages_buffer": [],
        "history": [],
        "state": "idle",        # idle | waiting_name | waiting_phone
        "temp_name": "",
        "temp_phone": "",
        "temp_service": "",
        "last_time": time.time(),
        "last_active": time.time(),
        "lock": threading.Lock()
    }


def get_session(uid):
    """جلب جلسة المستخدم بأمان مع إعادة تهيئة إذا كانت قديمة."""
    now = time.time()
    with SESSIONS_LOCK:
        sess = SESSIONS.get(uid)
        if not sess or (now - sess.get("last_active", now)) > SESSION_TTL:
            sess = new_session()
            SESSIONS[uid] = sess
        sess["last_active"] = now
        return sess


# ============= 3) BUFFER / TIMER =============
def schedule_reply(uid):
    """ينتظر شوية حتى يكمل المراجع كتابة رسائله، بعدها يرسل رد واحد."""
    time.sleep(BUFFER_DELAY)

    # نجيب الجلسة بدون إعادة تهيئة حتى لا نمسح شيء بالخطأ
    with SESSIONS_LOCK:
        session = SESSIONS.get(uid)

    if not session:
        return

    now = time.time()
    with session["lock"]:
        last_time = session.get("last_time", now)
        # إذا إجت رسالة جديدة خلال فترة البفر → نخلي الثريد الجديد يتصرف
        if (now - last_time) < BUFFER_DELAY:
            return

        if not session["messages_buffer"]:
            return

        final_text = " ".join(session["messages_buffer"]).strip()
        session["messages_buffer"] = []

    if not final_text:
        return

    reply = process_user_message(uid, final_text)
    send_message(uid, reply)


def add_message(uid, text):
    """تجميع رسائل المستخدم ضمن buffer قبل الرد."""
    now = time.time()
    session = get_session(uid)

    with session["lock"]:
        session["messages_buffer"].append(text)
        session["last_time"] = now

    th = threading.Thread(target=schedule_reply, args=(uid,))
    th.daemon = True
    th.start()


# ============= 4) INTENT + SERVICE =============
def detect_intent(txt: str) -> str:
    txt = txt.lower()

    # نية الحجز
    if any(w in txt for w in ["احجز", "اريد احجز", "حجز", "موعد", "احجزلي"]):
        return "booking"

    # سعر / عروض
    if any(w in txt for w in ["عرض", "عروض", "سعر", "اسعار", "كم", "شكد"]):
        return "price"

    # ألم / انتفاخ / نزف / خراج / ورم...
    if any(w in txt for w in [
        "يوجع", "يموتني", "ألم", "المال", "ورم", "انتفاخ", "التهاب",
        "ينزف", "نزف", "حساسية", "يحكني", "يلتهب", "خراج",
        "ضرس", "سنه", "سن", "انكسر", "انشلع", "طاح", "وقع"
    ]):
        return "medical"

    return "normal"


def detect_service(txt: str) -> str:
    t = txt.lower()
    if "زاركون" in t or "غلاف" in t or "قبق" in t or "تقبيق" in t:
        if "ايماكس" in t:
            return "تغليف زاركون ايماكس"
        return "تغليف زاركون"
    if "ايماكس" in t:
        return "تغليف ايماكس"
    if "قلع" in t or "شلع" in t or "اقتلاع" in t or "انشلع" in t or "طاح السن" in t:
        return "قلع سن"
    if "حشوة" in t or "تحشية" in t or "تحشاه" in t:
        if "جذر" in t or "عصب" in t:
            return "حشوة جذر"
        return "حشوة تجميلية"
    if "جذر" in t or "عصب" in t:
        return "حشوة جذر"
    if "تبييض" in t or "تبيض" in t:
        return "تبييض الأسنان"
    if "تنظيف" in t or "تنضيف" in t:
        return "تنظيف الأسنان"
    if "تقويم" in t:
        return "تقويم الأسنان"
    if "زراعة" in t:
        return "زراعة أسنان"
    return "غير محددة"


# ============= 5) PHONE PARSING =============
def normalize_phone(txt: str) -> str | None:
    """تنظيف وتحويل الرقم لصيغة عراقية 07xxxxxxxxx إن أمكن."""
    digits = re.sub(r"\D+", "", txt)

    # 00964xxxxxxxxx → 07xxxxxxxxx
    if digits.startswith("00964") and len(digits) >= 14:
        digits = "0" + digits[5:]
    # 964xxxxxxxxx → 07xxxxxxxxx
    elif digits.startswith("964") and len(digits) >= 13:
        digits = "0" + digits[3:]

    # نأخذ أول 11 رقم فقط إذا أطول
    if len(digits) > 11:
        digits = digits[:11]

    if digits.startswith("07") and len(digits) == 11:
        return digits

    return None


# ============= 6) CORE LOGIC =============
def process_user_message(uid, text):
    session = get_session(uid)
    st = session["state"]
    txt_clean = text.strip()

    # ---------- حالات الحجز ----------
    if st == "waiting_name":
        # هنا نعتبر أي نص هو اسم، إلا إذا واضح أنه رقم
        if normalize_phone(txt_clean):
            return "حبي، هذا شكله رقم مو اسم 🙂 دزلي اسمك الثلاثي حتى أسجّل إلك الحجز 🙏"

        session["temp_name"] = txt_clean
        session["state"] = "waiting_phone"
        return "تمام حبيبي، هسه دزلي رقمك حتى أكملك الحجز ❤️ (لازم يبدي بـ 07 ويكون 11 رقم)"

    if st == "waiting_phone":
        phone = normalize_phone(txt_clean)
        if not phone:
            return "حبيبي، الرقم لازم يكون عراقي، يبدي بـ 07 وطوله 11 رقم 🙏 جرب تكتبه مرة ثانية."

        session["temp_phone"] = phone
        session["state"] = "idle"

        service = session["temp_service"] or "غير محددة"

        confirm_msg = (
            "تم تأكيد الحجز ❤️\n\n"
            f"الاسم: {session['temp_name']}\n"
            f"الرقم: {session['temp_phone']}\n"
            f"الخدمة: {service}\n\n"
            "راح يتواصل وياك قسم المتابعة خلال فترة قصيرة 🙏"
        )

        # إرسال واتساب
        send_to_whatsapp(session["temp_name"], session["temp_phone"], service)

        # تنظيف المتغيرات المؤقتة
        session["temp_name"] = ""
        session["temp_phone"] = ""
        session["temp_service"] = ""

        return confirm_msg

    # ---------- وضع طبيعي (idle) ----------
    intent = detect_intent(txt_clean)

    # حجز
    if intent == "booking":
        session["state"] = "waiting_name"
        session["temp_service"] = detect_service(txt_clean)
        return "تمام حبيبي، حتى أسجّل إلك الحجز دزلي اسمك الثلاثي 🙏"

    # أسعار / عروض
    if intent == "price":
        return get_price_answer(txt_clean)

    # استفسار طبي
    if intent == "medical":
        return medical_ai_answer(uid, text)

    # أي شي ثاني → يروح لـ AI العام
    return ask_ai(uid, text)


# ============= 7) PRICE ANSWERS =============
def get_price_answer(txt: str) -> str:
    t = txt.lower()

    if "زاركون" in t:
        return (
            "عروض الزركون حاليّاً:\n"
            "• فل زركون: 75 ألف دينار للسن\n"
            "• زركون مدمج إيماكس: 100 ألف للسن\n"
            "• زركون ثري دي: 125 ألف للسن\n"
            "كلها شغل مرتب مع ضمان جودة العمل ❤️"
        )

    if "قلع" in t:
        return "القلع العادي 25 ألف دينار، والقلع الجراحي تقريباً 75 ألف دينار حسب حالة السن 🙏"

    if "حشوة" in t:
        return "الحشوة التجميلية تقريباً 35 ألف للسن، وحشوة الجذر توصل لـ 125 ألف حسب حالة العصب ☑️"

    if "تبييض" in t or "تبيض" in t:
        return "تبييض الأسنان بالليزر حوالي 100 ألف للجلسة، وغالباً يترافق مع تنظيف إذا يحتاج 😁"

    if "تنظيف" in t or "تنضيف" in t:
        return "تنظيف الأسنان الاحترافي تقريباً 25 ألف دينار للجلسة 🌟"

    return (
        "الأسعار الأساسية التقريبية:\n"
        "• الزركون 75 ألف للسن\n"
        "• الزركون إيماكس 100 ألف\n"
        "• القلع من 25 ألف وفوك حسب الحالة\n"
        "• الحشوة التجميلية 35 ألف\n"
        "• حشوة الجذر تقريباً 125 ألف\n"
        "• التبييض 100 ألف\n"
        "• التنظيف 25 ألف\n"
        "• التقويم 450 ألف للفك\n"
        "• الزراعة حسب نوع الزرعة\n"
        "وتبقى الأسعار النهائية حسب فحص الطبيب ووضع الأسنان 🙏"
    )


# ============= 8) MEDICAL AI (متقدم) =============
def medical_ai_answer(uid, text):
    """تحليل طبي مبدئي، بدون تشخيص أو وصف علاج."""
    session = get_session(uid)

    history_user_parts = [
        h["content"] for h in session["history"]
        if h["role"] == "user"
    ]
    history_text = " | ".join(history_user_parts[-3:]) if history_user_parts else ""

    system_prompt = """
انت مساعد افتراضي لطبيب اسنان في عيادة كولدن لاين.
عندك خبرة قوية بطب الاسنان، لكن *ممنوع* تعطي تشخيص قطعي أو وصف دواء أو جرعات.
وظيفتك:

1) تشرح للمراجع بشكل مبسط شنو الاحتمالات العامة للمشكلة حسب الأعراض اللي يذكرها.
2) توضّح متى الحالة عادةً تحتاج حشوة، متى غالباً تحتاج حشوة عصب، متى ممكن تحتاج قلع أو علاج لثة.. لكن بصيغة (ممكن / غالباً / احتمال).
3) ما تذكر أسماء أدوية ولا فيتامينات ولا مضادات حيوية، فقط تقول مثلاً: "الطبيب ممكن يختار لك علاج يناسب حالتك".
4) دائماً تنبّه بالنهاية:
   - إن الكلام عبارة عن توضيح عام مو بديل عن زيارة طبيب.
   - إذا أكو انتفاخ قوي، صعوبة فتح الفم، حرارة عالية، ألم قوي مستمر → لازم يراجع طبيب بأقرب وقت.
5) تجاوب باللهجة العراقية، وبأسلوب محترم، مهدّي، مختصر نسبيّاً (من 4 إلى 7 أسطر).
"""

    user_prompt = f"""
سياق المحادثة السابقة (إذا موجود): {history_text}

شكوى المراجع الأخيرة (أهم شي تعتمد عليها):
{text}

حلّل شكواه وفق النقاط التالية:
- شنو الاحتمالات العامة للمشكلة (بدون كلمة تشخيص)؟
- شنو الشغلة اللي ممكن يسويها طبيب الاسنان بالعيادة؟
- شنو الشي التطميني اللي يهدّي المراجع؟
- متى تنصحه يراجع العيادة بأقرب وقت أو المستشفى؟
"""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=260
        )
        reply = rsp.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Medical AI Error:", e)
        reply = (
            "من الوصف مالتك باين أكو مشكلة بالسن أو اللثة، "
            "بس بصراحة ميننطي تشخيص دقيق بدون فحص سريري أو صورة شعاعية.\n"
            "أنصحك تزور طبيب الأسنان حتى يشوف السن مباشرة ويحدد العلاج الأنسب، "
            "وإذا أكو انتفاخ قوي أو حرارة عالية أو ألم ما يهدأ، المراجعة تكون ضرورية بأقرب وقت 🙏"
        )

    # نحفظ جوابه بالـ history
    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": reply})
    if len(session["history"]) > MAX_HISTORY:
        session["history"] = session["history"][-MAX_HISTORY:]

    return reply


# ============= 9) GENERAL AI ANSWER =============
def ask_ai(uid, text):
    session = get_session(uid)

    system_prompt = """
انت "علي" موظف كول سنتر بعيادة كولدن لاين لطب وتجميل الأسنان.
تحجي باللهجة العراقية الواضحة، مختصر، لبق، بدون مبالغة.
تركّز على راحة المراجع، وتجاوب باختصار مفيد.

ممنوع:
- تعطي تشخيص قطعي.
- تذكر أدوية أو جرعات.
- تهوّل الحالة أو تخوف المراجع.

معلومات العيادة:
- بغداد – زيونة – شارع الربيعي الخدمي – داخل كراج مجمع إسطنبول
- الدوام: يومياً 4 مساءً – 9 مساءً (الجمعة عطلة)
- رقم الحجز والاستفسار: 07728802820

إذا حسّيت السائل يريد يحجز، شجّعه بلطافة يرسل اسمه ورقمه، بس لا تسوي حجز بنفسك.
"""

    conv = [{"role": "system", "content": system_prompt}]
    for h in session["history"]:
        conv.append(h)
    conv.append({"role": "user", "content": text})

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conv,
            max_tokens=220
        )
        reply = rsp.choices[0].message.content.strip()
    except Exception as e:
        print("❌ General AI Error:", e)
        reply = (
            "حبيبي، صار خلل بسيط بالمعالجة التلقائية، "
            "تكلّفك تشرفنا رسالة ثانية أو تتواصل مباشرة على رقم العيادة 07728802820 🙏"
        )

    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": reply})
    if len(session["history"]) > MAX_HISTORY:
        session["history"] = session["history"][-MAX_HISTORY:]

    return reply


# ============= 10) WHATSAPP =============
def send_to_whatsapp(name, phone, service):
    if not WHATSAPP_API:
        print("⚠️ WHATSAPP_API not set, skip sending.")
        return

    msg = f"حجز جديد من البوت:\\nالاسم: {name}\\nالرقم: {phone}\\nالخدمة: {service}"
    url = WHATSAPP_API + requests.utils.quote(msg)
    try:
        r = requests.get(url, timeout=10)
        print("📤 WhatsApp status:", r.status_code, r.text)
    except Exception as e:
        print("❌ WhatsApp send error:", e)


# ============= 11) FACEBOOK SEND =============
def send_message(uid, text):
    if not PAGE_ACCESS_TOKEN:
        print("❌ Cannot send FB message: PAGE_ACCESS_TOKEN not set.")
        return

    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": uid}, "message": {"text": text}}
    try:
        r = requests.post(url, params=params, json=payload, timeout=10)
        print("📤 FB send:", r.status_code, r.text)
    except Exception as e:
        print("❌ FB send error:", e)


# ============= 12) ROUTES =============
@app.route("/", methods=["GET"])
def home():
    return "Golden Line smart medical booking bot ✅"


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

    if not data:
        return "No data", 400

    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            if "message" in ev and "text" in ev["message"]:
                uid = ev["sender"]["id"]
                text = ev["message"]["text"]
                add_message(uid, text)

    return "OK", 200


# ============= 13) MAIN (للتست المحلي، Render يتجاهله) =============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
