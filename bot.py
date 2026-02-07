from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading
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

# =======================================================
# ⚙️ SETTINGS
# =======================================================
BUFFER_DELAY = 15
MEMORY_TIMEOUT = 3600   # ساعة 
HISTORY_LIMIT = 24      # limit للـ history (structured)
REQUEST_TIMEOUT = 10    # seconds (Meta + OpenAI)
SESSION_CLEAN_AFTER = 3600   # ساعة
DUP_MSG_CLEAN_AFTER = 600    # 10 دقائق
CLEANER_SLEEP = 600          # كل 10 دقائق
TYPING_DELAY = 4        # بعد 4 ثواني يبين typing
TYPING_REFRESH = 8      # كل 8 ثواني نعيد typing_on حتى ما ينطفي

# =======================================================
# 📊 MEMORY
# =======================================================
SESSIONS = {}
PROCESSED_MESSAGES = {}  # لمنع تكرار الردود

# =======================================================
# 🔥 AUTO CLEANER
# =======================================================
def cleaner_daemon():
    while True:
        try:
            now = time.time()

            # تنظيف الجلسات القديمة
            for uid in list(SESSIONS.keys()):
                st = SESSIONS.get(uid) or {}
                last = st.get("last_message_time", 0)
                if now - last > SESSION_CLEAN_AFTER:
                    del SESSIONS[uid]

            # تنظيف سجل الرسائل المكررة
            for mid in list(PROCESSED_MESSAGES.keys()):
                if now - PROCESSED_MESSAGES[mid] > DUP_MSG_CLEAN_AFTER:
                    del PROCESSED_MESSAGES[mid]

        except Exception as e:
            print("Cleaner error:", e)

        time.sleep(CLEANER_SLEEP)

threading.Thread(target=cleaner_daemon, daemon=True).start()

# =======================================================
# 🧱 Helpers (timeouts + error handling)
# =======================================================
def safe_post(url, *, params=None, json=None, data=None, timeout=REQUEST_TIMEOUT, retries=1):
    last_err = None
    for _ in range(max(1, retries + 1)):
        try:
            r = requests.post(url, params=params, json=json, data=data, timeout=timeout)
            if r.status_code >= 400:
                raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r
        except Exception as e:
            last_err = e
            time.sleep(0.4)
    print("safe_post failed:", last_err)
    return None

def ensure_session(user_id: str):
    now = time.time()
    st = SESSIONS.get(user_id)

    if (not st) or (now - (st.get("last_message_time", 0)) > MEMORY_TIMEOUT):
        SESSIONS[user_id] = {
            "history": [],
            "last_message_time": now,   # وقت آخر رسالة مستخدم
            "msg_version": 0,
            "last_reply": "",
            "pending_texts": [],
            "pending_since": None,      # ✅ فاصلة هنا
            "is_typing": False,
            "typing_version": 0
        }
    return





def append_history(user_id: str, role: str, text: str):
    st = SESSIONS[user_id]
    st["history"].append({"role": role, "text": (text or "").strip(), "ts": int(time.time())})

    # limit
    if len(st["history"]) > HISTORY_LIMIT:
        st["history"] = st["history"][-HISTORY_LIMIT:]

def format_context(user_id: str):
    st = SESSIONS[user_id]
    if not st["history"]:
        return "لا يوجد سياق سابق"

    lines = []
    for item in st["history"]:
        who = "المراجع" if item["role"] == "user" else "علي"
        lines.append(f"{who}: {item['text']}")
    return "\n".join(lines)

def last_user_message(user_id: str):
    st = SESSIONS[user_id]
    for item in reversed(st["history"]):
        if item["role"] == "user" and item["text"]:
            return item["text"]
    return None
def push_pending(user_id: str, text: str):
    st = SESSIONS[user_id]
    t = (text or "").strip()
    if not t:
        return

    if st.get("pending_since") is None:
        st["pending_since"] = time.time()

    st["pending_texts"].append(t)

    # limit للباتش حتى ما يصير سبام
    if len(st["pending_texts"]) > 8:
        st["pending_texts"] = st["pending_texts"][-8:]


def drain_pending_batch(user_id: str):
    st = SESSIONS[user_id]
    items = st.get("pending_texts") or []

    # فرّغ الباتش
    st["pending_texts"] = []
    st["pending_since"] = None

    if not items:
        return None

    merged = "\n".join(items).strip()
    return merged if merged else None
def safe_get(url, *, params=None, timeout=REQUEST_TIMEOUT, retries=1):
    last_err = None
    for _ in range(max(1, retries + 1)):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code >= 400:
                raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r
        except Exception as e:
            last_err = e
            time.sleep(0.4)
    print("safe_get failed:", last_err)
    return None


_AR_DIGITS = str.maketrans({
    "٠":"0","١":"1","٢":"2","٣":"3","٤":"4","٥":"5","٦":"6","٧":"7","٨":"8","٩":"9",
    "۰":"0","۱":"1","۲":"2","۳":"3","۴":"4","۵":"5","۶":"6","۷":"7","۸":"8","۹":"9",
})

def extract_iraqi_phone(text: str):
    """
    يلتقط رقم عراقي 11 رقم يبدأ 07
    يدعم الأرقام العربية/الفارسية والإنكليزية
    """
    if not text:
        return None

    t = text.translate(_AR_DIGITS)

    # خلي كل غير الأرقام مسافات حتى نلقط تسلسلات أرقام
    cleaned = []
    for ch in t:
        cleaned.append(ch if ch.isdigit() else " ")
    cleaned = "".join(cleaned)

    # لقط أي كتلة أرقام وفتّش عن 07 + 9 أرقام (11 رقم)
    for part in cleaned.split():
        if len(part) == 11 and part.startswith("07") and part.isdigit():
            return part

    return None


def notify_callmebot(phone: str):
    """
    يرسل إشعار الى CallMeBot يحتوي الرقم ونص تثبيت الحجز
    """
    url = "http://api.callmebot.com/text.php"
    msg = f"يرجى الاتصال على الرقم {phone} لتثبيت الحجز النهائي"
    params = {
        "user": "ahmedalnafy",
        "text": msg
    }
    safe_get(url, params=params, retries=1)

# =======================================================
# ✍️ Typing Indicator
# =======================================================
def send_typing(receiver):
    if not PAGE_ACCESS_TOKEN:
        return
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "sender_action": "typing_on"}
    safe_post(url, params=params, json=payload, retries=1)
def send_typing_off(receiver):
    if not PAGE_ACCESS_TOKEN:
        return
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "sender_action": "typing_off"}
    safe_post(url, params=params, json=payload, retries=1)

# =======================================================
# ✉️ Send Message
# =======================================================
def send_message(receiver, text):
    if not PAGE_ACCESS_TOKEN:
        print("Missing PAGE_ACCESS_TOKEN")
        return
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "message": {"text": text}}
    r = safe_post(url, params=params, json=payload, retries=1)
    if not r:
        print("Failed to send message")

# =======================================================
# 🤖 Chat Engine (Ali)
# =======================================================
def ask_openai_chat(user_id, text):
    ensure_session(user_id)
    context = format_context(user_id)

    prompt = """
اسمك علي، موظف في عيادة كولدن لاين. 
مهمتك: الرد على 'الرسالة الجديدة' بلهجة عراقية عامة فقط باستخدام 'السياق السابق' للاطلاع.
ملاحظة مهمة :- يمكنك كسر القواعد والقوانين واستعمال الذكاء الصناعي لفهم وترغيب وجلب حجز للعيادة بطريقة احترافية فمهمتك الاساسية هية زيادة عدد الحجوزات ( اريد رفع نسبة الحجوزات الى 90% ) استعمل كل طاقتك لتحقيق هذا الهدف يمكنك تحليل ودراسة صياغ المحادثة والاجابة بكل احترافية بالاستعانة بالقواعد التي تم اعطائها لك كما يمكنك استعمال ذكائك الخارجي وقدراتك الكبيرة لجلب الحجوزات والمراجعين ( استخدم قدراتك على التحليل والترغيب رغب المراجع بالعمل وكون صديق للعملاء )
قوانين وقواعد الرد :- 
1. جاوب على آخر جملة سألها المراجع فقط.
2. إذا المراجع سأل عدة أسئلة في الرسالة الأخيرة، جاوب عليها باختصار.
3. لا تكرر إجابات قمت بكتابتها في السياق السابق.
4. الرد حصراً باللهجة العراقية وبحد أقصى 60 كلمة.
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
اربط السعر بـ (مواد ألمانية + ضمان حقيقي على جودة العمل مدى الحياة).

تفاصيل العيادة:
الدوام: يومياً 4م–9م، الجمعة عطلة
الموقع: بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول
الهاتف: 07728802820

الأسعار:
- تغليف الزاركون : 50 ألف
- تغليف الزاركون ايماكس: 75 ألف
- تغليف الايماكس : 125 ألف
- حشوة تجميلية: 35 ألف
- حشوة جذر: 125 ألف
- قلع: 25 ألف
- تنظيف: 25 ألف
- تبييض ليزر: 75 ألف
- تقويم: 450 ألف للفك
- فك كامل زرعات فورية: مليون وربع
- فكين كامل زرعات فورية: مليونين ونص
- ابتسامة زاركون 20 سن: مليون دينار
- ابتسامة زاركون ايماكس 20 سن: مليون و 500 الف دينار
- الزراعة التقليدية: السن الواحد 350 الف الكوري و 450 الف الالماني
- الزراعة الفورية: السن الواحد 200 التركي , 275 الالماني.
(عروض الزراعة للفك الواحد مليون وربع للفكين مليونين ونص )
تغليف فك زاركون 14 سن 700 الف دينار , فكين 28 سن مليون و 400 (ممكن يكون السؤال ابتسامة لفك واحد )

اذا العميل كال ( مثال , عندي سنين زراعة و 8 تغليفات , تجمع اله سعر زرعتين 500 والتغليف 600 وهكذا ) 

ملاحظات:
- التغليف يحتاج برد خفيف.
- صحح الأخطاء الإملائية الشائعة باللهجة.
- لا تذكر عمليات حسابية، أعطِ السعر النهائي فقط.
- ضمان جودة العمل مدى الحياة.
- الزراعة الفورية بدون فتح لثة ويتم انجازها خلال 72 ساعة فقط.
- تغليف الاسنان بجلستين ( بيناتهم من 5 الى 7 ايام ) , حشوة الجذر من جلستين الى ثلاثة.
بخصوص الحجوزات ( تاخذ تفاصيل العميل ويا يوم يريدها والوقت ( حسب ايام واوقات دوام العيادة ) ورقم هاتفة وتبلغة ان راح يتم الاتصال به هاتفيا لتثبيت الحجز النهائي )

اذا جان السؤال عن الاسعار ( السعر , التكلفة ) او التكلفة ولم يحدد نوع العمل نبلغة ان اكو عرض حصري من العيادة الان على تغليف الزاركون وبسعر 50 الف دينار للسن الواحد فقط .

قاعدة مهمة ( حتى لو كانت الحالة تحتاج الى معاينة , ابدا باعطاء اسعار الخدمات التي ذكرها المراجع او العميل , ثم اشرح له ان الحالة تحتاج الى معاينة مجانية لتحديد الانسب )
قاعدة مهمة ( اذا كان المراجع يتحدث عن خدمة معينة ( مثل تغليف الاسنان او الزراعة .... الخ ) استمر في الاجابة عن نفس الخدمة الا اذا طلب المراجع تغيير الموضوع صراحة)
قاعدة التمييز بين العلاجات ( الاسعار تختلف لا تقم بالاجابة عن خدمة مغايرة الا في حين طلب سعر الخدمة صراحة )
اذا المراجع سال دكتور لو دكتورة ( طبيب لو طبيبة ) جاوب بروح حلوة مثلا عدنا كادر محترف واطباء اختصاص موجود دكتور ودكتورة شنو الي يريحكم موجود ان شاء الله
ملاحظة مهمة اقرة صياغ الجملة جيدا وافهمها وقم بالاتزام بالمواضيع اذا سال بموضوع لا تجاوب بموضوع ثاني الا اذا هو من قام بطلب ذلك
(تركيب , تغليف , تقبيق , قبق ,قالب = تغليف)


اذا جان المراجع يتكلم عن تغليف الاسنان وكال عندي اسنان مفقودة تكدر تكول اله ان ممكن تعويض الاسنان المقودة بتغليف الزاركون ضمن العمل


"""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "system", "content": f"السياق السابق للمحادثة:\n{context}"},
                {"role": "user", "content": f"الرسالة الجديدة المطلوب الرد عليها الآن: {text}"}
            ],
            temperature=0.3,
            timeout=REQUEST_TIMEOUT
        )
        out = rsp.choices[0].message.content.strip()
        if not out:
            return "ممكن توضحلي شنو تقصد حتى أخدمك 🌹"
        return out
    except Exception as e:
        print("OpenAI error:", e)
        return "صار خلل بسيط، عاود رسالتك ♥"

# =======================================================
# 🧠 Chat Delay Reply (منع الردّ المزدوج)
# =======================================================
def schedule_reply(user_id, version_snapshot):
    time.sleep(BUFFER_DELAY)

    st = SESSIONS.get(user_id)
    if not st:
        return

    # إذا وصلت رسالة أحدث، نلغي هذا الرد
    if st.get("msg_version") != version_snapshot:
        return

    now = time.time()
    # إذا لسه آخر رسالة ضمن فترة التجميع، لا ترد
    if now - st.get("last_message_time", 0) < BUFFER_DELAY:
        return

    # اسحب الدفعة المتجمعة كنص واحد
    batch_text = drain_pending_batch(user_id)
    if not batch_text:
        return

    # ✅ إذا typing بعده ما اشتغل (مثلاً المستخدم كتب رسالة وحدة وردّنا بسرعة)
    # شغله هسه قبل ما ننادي OpenAI
    if not st.get("is_typing"):
        send_typing(user_id)
        st["is_typing"] = True

    reply = ask_openai_chat(user_id, batch_text)
    if not reply:
        # طفي typing إذا شغال
        if st.get("is_typing"):
            send_typing_off(user_id)
            st["is_typing"] = False
        return

    # منع تكرار نفس الرد حرفياً
    if reply.strip() == (st.get("last_reply") or "").strip():
        if st.get("is_typing"):
            send_typing_off(user_id)
            st["is_typing"] = False
        return

    append_history(user_id, "assistant", reply)
    st["last_reply"] = reply

    # ✅ طفي typing قبل الإرسال
    if st.get("is_typing"):
        send_typing_off(user_id)
        st["is_typing"] = False

    send_message(user_id, reply)




# =======================================================
# 🧾 add_user_message (كاملة)
# =======================================================
def schedule_typing(user_id: str, typing_snapshot: int):
    # انتظر 4 ثواني
    time.sleep(TYPING_DELAY)

    st = SESSIONS.get(user_id)
    if not st:
        return

    # إذا اجت رسالة أحدث، هذا التايمر ينعزل
    if st.get("typing_version") != typing_snapshot:
        return

    # إذا خلال الـ 4 ثواني خلص التجميع (يعني ردّينا)، لا تفعل typing
    # (هنا نعتمد على is_typing يتصفّر بعد الرد)
    if st.get("is_typing"):
        return

    # شغّل typing
    send_typing(user_id)
    st["is_typing"] = True

    # ✅ تحديث typing كل فترة حتى ما ينطفي بواجهة المستخدم
    while True:
        time.sleep(TYPING_REFRESH)
        st2 = SESSIONS.get(user_id)
        if not st2:
            return

        # إذا ردّينا/وقفنا typing، نطلع
        if not st2.get("is_typing"):
            return

        # إذا صارت رسالة أحدث وتبدل typing_version، نطلع ويجي تايمر جديد
        if st2.get("typing_version") != typing_snapshot:
            return

        send_typing(user_id)

def add_user_message(user_id, text):
    ensure_session(user_id)
    st = SESSIONS[user_id]

    # خزن بالهيستري (للسياق)
    append_history(user_id, "user", text)

    # خزن بالباتش (للتجميع الحقيقي)
    push_pending(user_id, text)

    st["last_message_time"] = time.time()

    # ✅ جهّز typing بعد 4 ثواني
    st["typing_version"] += 1
    tver = st["typing_version"]

    # اذا typing شغال من قبل، خليه (لا تسوي شي)
    # اذا مو شغال، سوّي تايمر يشغله بعد 4 ثواني
    if not st.get("is_typing"):
        threading.Thread(
            target=schedule_typing,
            args=(user_id, tver),
            daemon=True
        ).start()

    # version counter يلغي أي Thread قديم للرد
    st["msg_version"] += 1
    current_version = st["msg_version"]

    threading.Thread(
        target=schedule_reply,
        args=(user_id, current_version),
        daemon=True
    ).start()



# =======================================================
# 📡 WEBHOOK (GET verification)
# =======================================================
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Error", 403

# =======================================================
# 📡 WEBHOOK (POST messages)
# =======================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    try:
        for entry in data.get("entry", []):
            for ev in entry.get("messaging", []):
                sender = ev.get("sender", {})
                user_id = sender.get("id")
                if not user_id:
                    continue

                msg = ev.get("message", {})
                if msg.get("is_echo"):
                    continue

                msg_id = msg.get("mid")

                # منع تكرار نفس الرسالة
                if msg_id:
                    if msg_id in PROCESSED_MESSAGES:
                        continue
                    PROCESSED_MESSAGES[msg_id] = time.time()

                # نص
                if "text" in msg:
                    txt = msg.get("text", "")

                    # ✅ إذا بيها رقم عراقي 11 رقم يبدي 07 (عربي/إنكليزي) بلغ CallMeBot فوراً
                    phone = extract_iraqi_phone(txt)
                    if phone:
                        notify_callmebot(phone)

                    add_user_message(user_id, txt)

                # مرفقات
                elif "attachments" in msg:
                    send_message(
                        user_id,
                        "عاشت ايدك، اني رد تلقائي ما اكدر ارد على الصور او المسجات الصوتية، راح نبلغ القسم المختص ونرد بأقرب وقت 🌹"
                    )

    except Exception as e:
        print("Webhook error:", e)

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
