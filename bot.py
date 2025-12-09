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

# Google Sheet API URL (booking sheet)
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
BUFFER_DELAY = 15          # seconds before replying
MEMORY_TIMEOUT = 900       # 15 minutes for session reset

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
# 🔢 Normalize Arabic Digits
# =======================================================
def normalize_numbers(text):
    arabic = "٠١٢٣٤٥٦٧٨٩"
    english = "0123456789"
    table = str.maketrans(arabic, english)
    return text.translate(table)

# =======================================================
# 🔢 Extract Phone (Arabic + English)
# =======================================================
def extract_phone(text):
    text = normalize_numbers(text)
    m = re.findall(r"07\d{9}", text)
    return m[0] if m else None

# =======================================================
# 🧾 Extract Name (simple heuristic)
# =======================================================
def extract_name(text):
    txt = normalize_numbers(text)
    cleaned = ''.join([c if not c.isdigit() else ' ' for c in txt])
    return cleaned.strip() if len(cleaned.strip()) > 1 else None

# =======================================================
# ☎️ Send WhatsApp Booking (simple notification)
# =======================================================
def send_whatsapp_booking(name, phone, date, time_):
    global DAILY_BOOKINGS
    DAILY_BOOKINGS += 1
    msg = (
        "حجز جديد من البوت:\n"
        f"الاسم: {name}\n"
        f"الرقم: {phone}\n"
        f"الخدمة: معاينة مجانية\n"
        f"التاريخ: {date}\n"
        f"الوقت: {time_}\n"
    )
    url = WHATSAPP_URL + requests.utils.quote(msg)
    requests.get(url)

# =======================================================
# 📊 DAILY REPORT GENERATION
# =======================================================
def generate_report_text():
    return (
        "📊 تقرير اليوم – عيادة كولدن لاين\n\n"
        f"🟢 عدد الحجوزات: {DAILY_BOOKINGS}\n"
        f"✉️ عدد الرسائل: {DAILY_MESSAGES}\n"
        f"⏳ طلبات غير مكتملة: {DAILY_INCOMPLETE}\n"
    )

# =======================================================
# 📱 Send Report to WhatsApp
# =======================================================
def send_whatsapp_report():
    text = generate_report_text()
    url = WHATSAPP_URL + requests.utils.quote(text)
    requests.get(url)

# =======================================================
# ⏰ Daily 9 PM Report
# =======================================================
def report_daemon():
    global DAILY_BOOKINGS, DAILY_MESSAGES, DAILY_INCOMPLETE
    while True:
        now = time.localtime()
        if now.tm_hour == 21 and now.tm_min == 0:
            send_whatsapp_report()
            DAILY_BOOKINGS = 0
            DAILY_MESSAGES = 0
            DAILY_INCOMPLETE = 0
            SESSIONS.clear()
            time.sleep(60)
        time.sleep(5)

threading.Thread(target=report_daemon, daemon=True).start()

# =======================================================
# ⏳ 30-MIN FOLLOW UP
# =======================================================
def follow_up_checker(user_id, snapshot_time):
    time.sleep(1800)  # 30 minutes
    st = SESSIONS.get(user_id)
    if not st:
        return
    if (
        st["last_message_time"] == snapshot_time
        and st["phone"] == ""
        and not st["followup_sent"]
    ):
        global DAILY_INCOMPLETE
        DAILY_INCOMPLETE += 1
        send_message(
            user_id,
            "إذا بعدك تحتاج تحجز، كلّي حتى أكملك الموعد ❤️\n"
            "الفحص مجاني وما ياخذ وقت."
        )
        st["followup_sent"] = True

# =======================================================
# 🧠 BUFFER (15 SECONDS) – Chat Engine
# =======================================================
def schedule_reply(user_id):
    time.sleep(BUFFER_DELAY)
    st = SESSIONS.get(user_id)
    if not st:
        return
    now = time.time()
    if now - st["last_message_time"] >= BUFFER_DELAY:
        send_typing(user_id)
        user_text = st["history"][-1] if st["history"] else ""
        reply = ask_openai_chat(user_id, user_text)
        if reply:
            send_message(user_id, reply)

# =======================================================
# 📥 Get last N messages
# =======================================================
def get_last_messages(user_id, limit=10):
    st = SESSIONS.get(user_id, {})
    history = st.get("history", [])
    return history[-limit:]

# =======================================================
# 📅 Default Appointment Date (Tomorrow; if Friday → Saturday)
# =======================================================
def get_default_date():
    today = datetime.now()
    tomorrow = today + timedelta(days=1)
    # weekday(): Monday=0 ... Sunday=6; assume Friday=4
    if tomorrow.weekday() == 4:  # Friday
        tomorrow = tomorrow + timedelta(days=1)
    return tomorrow.strftime("%Y-%m-%d")

# =======================================================
# 🤖 GPT Booking Engine (separate from chat)
# =======================================================
def analyze_booking(name, phone, last_msgs_text):
    """
    Uses GPT to:
    - Infer patient name from history if possible
    - Detect requested date/time if user specified
    - Fallback: tomorrow at 16:00, skipping Friday -> Saturday
    - Always service = معاينة مجانية
    Returns dict with:
      patient_name, patient_phone, service, date, time, ai_message
    """
    # Default values in case GPT fails
    fallback_date = get_default_date()
    fallback_time = "16:00"

    history_snippet = "\n".join(last_msgs_text) if isinstance(last_msgs_text, list) else str(last_msgs_text)

    system_prompt = f"""
أنت موظف حجز في عيادة كولدن لاين لطب وتجميل الأسنان.
مهمتك أن تقرأ تاريخ المحادثة وتستخرج تفاصيل الحجز.

المعلومات:
- إذا المراجع ما محدد موعد → خلي الموعد يكون غداً الساعة 4:00 عصراً.
- إذا غداً يصادف جمعة، خلي الموعد يوم السبت بعدها.
- إذا كال اليوم، خلي الموعد بتاريخ اليوم.
- إذا كال باچر، خلي الموعد بتاريخ الغد (مع مراعاة الجمعة).
- إذا ذكر يوم محدد مثل السبت الجاي أو الأحد القادم، حاول تستنتج التاريخ بالميلادي حسب المنطق.
- أوقات الدوام من 4:00 مساءً إلى 9:00 مساءً. إذا طلب وقت خارج هذا النطاق تجاهله وخلي 4:00.
- الخدمة دائماً "معاينة مجانية".

اسم المراجع:
- إذا هو كاتبه بالمحادثة، استخرجه.
- إذا مو واضح، استخدم الاسم القادم من النظام إذا موجود، وإذا هم مو موجود خليه "بدون اسم".

رجّع الناتج بصيغة JSON فقط بدون أي نص زائد، بالشكل التالي بالضبط:

{{
  "patient_name": "اسم المراجع",
  "patient_phone": "{phone}",
  "service": "معاينة مجانية",
  "date": "YYYY-MM-DD",
  "time": "HH:MM",
  "ai_message": "نص الرسالة التي سترسل للمراجع لتأكيد الحجز، باللهجة العراقية وبأسلوب لطيف مع ذكر الاسم والرقم والخدمة والتاريخ والوقت والعنوان."
}}

العنوان الثابت داخل الرسالة يكون:
"بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول / عيادة كولدن لاين لطب وتجميل الأسنان".
"""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": history_snippet},
            ],
            max_tokens=500,
            temperature=0
        )
        raw = rsp.choices[0].message.content.strip()
        data = json.loads(raw)

        # Basic validation / fallback
        patient_name = data.get("patient_name") or name or "بدون اسم"
        patient_phone = data.get("patient_phone") or phone
        service = data.get("service") or "معاينة مجانية"
        date = data.get("date") or fallback_date
        time_str = data.get("time") or fallback_time
        ai_message = data.get("ai_message") or (
            f"تم تثبيت موعدك ❤\n"
            f"الاسم: {patient_name}\n"
            f"رقم الهاتف: {patient_phone}\n"
            f"الخدمة: {service}\n"
            f"التاريخ: {date}\n"
            f"الوقت: {time_str}\n"
            "عنواننا: بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول / عيادة كولدن لاين لطب وتجميل الأسنان"
        )

        return {
            "patient_name": patient_name,
            "patient_phone": patient_phone,
            "service": service,
            "date": date,
            "time": time_str,
            "ai_message": ai_message,
        }
    except Exception:
        # Fallback if GPT or JSON parsing fails
        patient_name = name or "بدون اسم"
        patient_phone = phone
        service = "معاينة مجانية"
        date = fallback_date
        time_str = fallback_time
        ai_message = (
            f"تم تثبيت موعدك ❤\n"
            f"الاسم: {patient_name}\n"
            f"رقم الهاتف: {patient_phone}\n"
            f"الخدمة: {service}\n"
            f"التاريخ: {date}\n"
            f"الوقت: {time_str}\n"
            "عنواننا: بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول / عيادة كولدن لاين لطب وتجميل الأسنان"
        )
        return {
            "patient_name": patient_name,
            "patient_phone": patient_phone,
            "service": service,
            "date": date,
            "time": time_str,
            "ai_message": ai_message,
        }

# =======================================================
# 📤 Save booking to Google Sheet (booking sheet)
# =======================================================
def save_booking_to_sheet(booking):
    try:
        payload = {
            "action": "addBooking",
            "name": booking["patient_name"],
            "phone": booking["patient_phone"],
            "service": booking["service"],
            "date": booking["date"],
            "time": booking["time"],
            "status": "Pending",
        }
        requests.post(BOOKING_API_URL, json=payload, timeout=10)
    except Exception:
        pass

# =======================================================
# 📩 Send booking confirmation to Messenger
# =======================================================
def send_booking_confirmation(user_id, booking):
    send_message(user_id, booking["ai_message"])

# =======================================================
# 🤖 GPT Chat Engine — reply to last message only
# =======================================================
def ask_openai_chat(user_id, text):
    st = SESSIONS[user_id]
    history_text = ""
    if len(st["history"]) > 1:
        history_text = " | ".join(st["history"][:-1])

    big_prompt = """ 
انت اسمك علي موظف الكول سنتر بعيادة كولدن لاين لطب الاسنان،
وضيفتك ترد على الرسائل باللهجة العراقية، وبدون مبالغة وتجاوب على جميع استفساراتهم بطريقة تطمن المراجع

نموذج الرد المقترح (جواب السؤال فقط من 2 الى 15 كلمة كحد اقصى) 
 
ملاحظة ١ :- تأخذ بعين الاعتبار تاريخ المحادثة المرسل مع المحادثة وترد على أخير رسالة فقط.
ملاحظة ٢ :- اذا المراجع عندة شكوة او عصبي او يشتكي من عمل العيادة ، تعتذر منه بطريقة مهذبة وتطلب منه الاسم ورقم التلفون حتى نتصل بيه واذا استمر بالتذمر ( مباشرة بلغة يتصل على رقم العيادة وتنيطه الرقم )

وهاي بعض الملاحظات الي راح تستفاد منها عند الرد على المراجعين :-

تفاصيل العيادة :-
الاسم : عيادة كولدن لاين لطب وتجميل الاسنان.
وقت الدوام : يوميا من الساعة ٤م الى الساعة ٩م عدى يوم الجمعة عطلة العيادة
العنوان : بغداد زيونة شارع الربيعي الخدمي داخل كراج مجمع اسطنبول 
رقم الهاتف :- 07728802820

الحشوة التجميلية جلسة وحدة
حشوة الجذر من جلسة الى ثلاثة جلسات حسب التهاب السن
تغليف الاسنان ( زاركون ، ايماكس ) خلال جلستين وبيناتهم من ٥ الى ٧ أيام
ضمان العيادة جودة العمل مدى الحياة
اذا كال المراجع ماكو تخفيضات ويطلب تخفيض للسعر تكول اله هاي أسعار عروض ، بس الطبيب ميقصر وياك ان شاء الله
حاول تفهم الاغلاط الاملائية وتصحيحها حسب صياغ الجملة
تقوم بتحليل الطلب الخاص للمراجع مثل تقوم بجمع المبلغ الكلي للمراجع حسب عدد الاسنان الي يريدها بدون ذكر تفاصيل العملية الحسابية.
اذا سالك ان لازم حجز او راد يحجز تأخذ منه الاسم والرقم وبعدها تبلغه ان راح يتم التواصل وياه من قبل قسم المتابعة من العيادة لتحديد موعد الحجز
لا تقم بالترحيب فقط عندما يقوم بالتحيب بك الأول
اي نوع تغليف ( زاركون ، زاركون ايماكس ) يحتاج الى برد خفيف حتى متسبب مشاكل باللثة بالمستقبل
اكو اغلاط املائية ومرادفات راح تصعب عليك الفهم مثل ( قبق ، غلاف ، تقبيق = تغليف ) ، ( طاح ، وكع ، انشلع = انقلع ) ، ( تحشاه ، تحشية = حشوة ) ، ( ما بيها مجال , هلا هلا بالفقير , على كيفكم ويانه , منين اجيب\نجيب\تجيب , نزل النه من السعر = المراجع يطلب تخفيض )، ( يوجعني ، توجع ، يموتني = الم )
اذا كال منو الدكتور او اسم الدكتور كله احنة مركز وموجود اكثر من دكتور وكلهم اكفاء بالعمل , اذا كال دكتور لو دكتورة كول اكو دكتور واكو دكتورة

قواعد الرد الذكية (مهم جداً):

سياسة الإقناع: واربط السعر بـ (المواد الألمانية + الضمان الحقيقي). حسسه إنه ماخذ صفقة ممتازة.

الاسعار والعروض :-
( عرض تغليف الزاركون كل تغليفين الثالث مجاني )
١: الزاركون 100 الف دينار كل اثنين الثالث مجاني
٢: الزاركون ايماكس 150 الف دينار
٣: القلع 25 الف دينار
٤: الحشوة التجميلة 35 الف دينار
٥: حشوة الجذر 125 الف دينار
٦: تبييض الاسنان بالليزر 100 الف دينار
٧: تنضيف الاسنان 25 الف دينار
٨: تقويم الاسنان 450 الف للفك
٩: زراعة الاسنان التقليديه ( الكوري 350 ، الالماني 450 )
١٠: زراعة الفك الكامل للزرعات الفورية مليون و 750 الف دينار زرعات المانية
١١: ابتسامة المشاهير زاركون 20 سن مليون و 400 الف 
ابتسامة المشاهير زاركون ايماكس 16 سن مليونين
12: نظام الاقساط متوفر للموظفين والمتقاعدين على مصرف الرافدين ( كي كارد , ماستر كارت ) أقساط لمدة 10 اشهر وفوائد عشرين بالمية للمصرف
13: اذا سال عن ابتسامة الدايركت فنير او كال ابتسامة بدون برد كول اله متوفره ابتسامة النانو فنير قشور مختبريه سعر السن الواحد 50 الف دينار
14: الايماكس سعرة 175 الف للسن الواحد ( قشور الايماكس )
15: اذا سال عن الضمان الصحي ( على سبيل المثال تشتغلون بالضمان ) او ( عندكم ضمان صحي ) هنا يقصد الضمان الصحي الخاص بالحكومه فالجواب يكون اي نشتغل بالضمان ننطيكم تقرير تصدقوه بنقابة اطباء الاسنان وتودوه للدائرة الخاصة بيكم
16: قلع سن العقل الجراحي ( السن المطمور ) سعره 75 الف دينار
17: اذا سال موجود البنج الالكتروني اي متوفر البنج الالكتروني بالعيادة
18: اذا سال شنو عروض الزاركون تجاوبة على سعر الزاركون ونفس الشي على كل خدمة
19: لا تجاوب بصورة عشوائية اذا شي متعرف تجاوبة وماعندك سعر الخدمة المطلوبة بلغه هاي الخدمة نحدد سعرها بعد المعاينة المجانية
20: سعر الفك المتحرك او التركيب المتحرك يكون 40 الف للسن الواحد ( لا تعطي اي معلومة تخص الفك المتحرك الى في حين السؤال المباشر عن التعويض المتحرك )
21: اذا محدد نوع الخدمة دائما اعتبر الخدمة المباشرة هية خدمة الزاركون ( مثلا كال شكد سعر الفك الكامل = تغليف فك كال زاركون وتنطي السعر )
22: انطي السعر بدون عملية حسابية دز الناتج النهائي
23: اي معلومة ما فهمتها او ملكيت جواب الها ترسل اله رقم العيادة وتبلغة يتواصل عبر الواتساب لتفاصيل اكثر
"""

    restrain_history = """
هذه الرسائل السابقة لفهم طريقة الكلام فقط.
يجب أن ترد على آخر رسالة فقط.
تجاهل جميع الرسائل السابقة حتى لو تحتوي أسئلة.
"""

    try:
        messages = [
            {"role": "system", "content": big_prompt},
            {"role": "system", "content": restrain_history},
            {"role": "system", "content": f"History:\n{history_text}"},
            {"role": "user", "content": text},
        ]

        rsp = client.chat.completions.create(
            model="gpt-4.1",
            messages=messages,
            max_tokens=300,
            temperature=0.4,
        )

        return rsp.choices[0].message.content.strip()
    except Exception:
        return "أعتذر صار خلل بسيط، كلّي شتحتاج أعيد أجاوبك من جديد ♥"

# =======================================================
# 📥 Add Message (Entry point for each user message)
# =======================================================
def add_user_message(user_id, text):
    global DAILY_MESSAGES
    DAILY_MESSAGES += 1
    now = time.time()

    # Secret code to send instant report
    if text.strip() == "Faty2000":
        send_whatsapp_report()
        return

    # New or expired session
    if (
        user_id not in SESSIONS
        or (now - SESSIONS[user_id]["last_message_time"] > MEMORY_TIMEOUT)
    ):
        SESSIONS[user_id] = {
            "history": [],
            "name": "",
            "phone": "",
            "last_message_time": now,
            "followup_sent": False,
        }

    st = SESSIONS[user_id]
    st["history"].append(text)
    st["last_message_time"] = now

    # launch follow-up checker snapshot
    threading.Thread(target=follow_up_checker, args=(user_id, now), daemon=True).start()

    # Try to update name heuristically
    possible_name = extract_name(text)
    if possible_name:
        st["name"] = possible_name

    # Detect phone → booking engine
    phone = extract_phone(text)
    if phone:
        st["phone"] = phone

        last_msgs = get_last_messages(user_id, limit=10)
        booking = analyze_booking(st.get("name", ""), phone, last_msgs)

        # confirm to user
        send_booking_confirmation(user_id, booking)

        # save to sheet
        save_booking_to_sheet(booking)

        # WhatsApp notification
        send_whatsapp_booking(
            booking["patient_name"],
            booking["patient_phone"],
            booking["date"],
            booking["time"],
        )

        st["followup_sent"] = True
        return

    # No phone → normal chat reply with buffer
    threading.Thread(target=schedule_reply, args=(user_id,), daemon=True).start()

# =======================================================
# ✉️ Send Message to Messenger
# =======================================================
def send_message(receiver, text):
    if not PAGE_ACCESS_TOKEN:
        return
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "message": {"text": text}}
    requests.post(url, params=params, json=payload)

# =======================================================
# 📡 WEBHOOK
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
# 🚀 Run Server
# =======================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
