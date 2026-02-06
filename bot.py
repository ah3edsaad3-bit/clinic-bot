from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading
import random
from threading import Lock

app = Flask(__name__)

# =======================================================
# 🔑 TOKENS & CONFIG
# =======================================================
VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# =======================================================
# 📊 MEMORY + ANTI DUP
# =======================================================
SESSIONS = {}
PROCESSED_MESSAGES = {}  # لمنع تكرار نفس mid
MEMORY_TIMEOUT = 1800  # 30 دقيقة

# =======================================================
# 🧠 SMART BATCHING + "TYPING" SMART WAIT
# =======================================================
BUFFER_DELAY = 15        # زمن التجميع: إذا ماكو رسائل جديدة خلاله نرد
TYPING_ON_AFTER = 4      # لا تشغل typing_on فوراً
TYPING_GRACE = 3         # فحص أخير قبل الإرسال: إذا وصلت رسالة جديدة، نلغي

USER_LOCKS = {}
USER_SEQ = {}  # رقم تسلسلي لكل مستخدم حتى آخر thread بس يرد


def get_user_lock(uid):
    if uid not in USER_LOCKS:
        USER_LOCKS[uid] = Lock()
    return USER_LOCKS[uid]


def build_batched_text(history, since_index):
    batch = history[since_index:]
    return "\n".join([f"- {m}" for m in batch if m and m.strip()])


# =======================================================
# 🔥 AUTO CLEANER
# =======================================================
def cleaner_daemon():
    while True:
        now = time.time()

        # تنظيف الجلسات القديمة
        for uid in list(SESSIONS.keys()):
            try:
                if now - SESSIONS[uid]["last_message_time"] > MEMORY_TIMEOUT:
                    del SESSIONS[uid]
                    USER_SEQ.pop(uid, None)
                    USER_LOCKS.pop(uid, None)
            except Exception:
                # إذا صارت مشكلة ببيانات جلسة معينة، احذفها
                del SESSIONS[uid]
                USER_SEQ.pop(uid, None)
                USER_LOCKS.pop(uid, None)

        # تنظيف سجل الرسائل المكررة
        for mid in list(PROCESSED_MESSAGES.keys()):
            if now - PROCESSED_MESSAGES[mid] > 600:
                del PROCESSED_MESSAGES[mid]

        time.sleep(600)


threading.Thread(target=cleaner_daemon, daemon=True).start()

# =======================================================
# ✍️ Typing Indicator
# =======================================================
def send_typing_on(receiver):
    if not PAGE_ACCESS_TOKEN:
        return
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "sender_action": "typing_on"}
    try:
        requests.post(url, params=params, json=payload, timeout=10)
    except:
        pass


def send_typing_off(receiver):
    if not PAGE_ACCESS_TOKEN:
        return
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": receiver}, "sender_action": "typing_off"}
    try:
        requests.post(url, params=params, json=payload, timeout=10)
    except:
        pass

# =======================================================
# ✉️ Send Message
# =======================================================
def send_message(receiver, text):
    if not PAGE_ACCESS_TOKEN:
        return
    params = {"access_token": PAGE_ACCESS_TOKEN}
    url = "https://graph.facebook.com/v18.0/me/messages"
    payload = {"recipient": {"id": receiver}, "message": {"text": text}}
    try:
        requests.post(url, params=params, json=payload, timeout=10)
    except:
        pass

# =======================================================
# 🤖 Chat Engine (Ali)
# =======================================================
def ask_openai_chat(user_id, batched_text):
    st = SESSIONS[user_id]

    # ✅ context الصحيح: فقط قبل الدفعة الحالية
    batch_start = st.get("batch_start_index", 0)
    old_context_list = st["history"][:batch_start]
    context = " | ".join(old_context_list) if old_context_list else "لا يوجد سياق سابق"

    # ✅ ذاكرة ردود: آخر رد/ردين
    recent_replies = st.get("recent_replies", [])
    last_replies_text = " | ".join(recent_replies[-2:]) if recent_replies else "لا يوجد ردود سابقة"

    prompt = """
اسمك علي، موظف في عيادة كولدن لاين. 
مهمتك: الرد على 'الرسالة الجديدة' بلهجة عراقية عامة فقط باستخدام 'السياق السابق' للاطلاع.

⚠️ قوانين صارمة:
1. جاوب على آخر جملة سألها المراجع فقط.
2. إذا المراجع سأل عدة أسئلة في الرسالة الأخيرة، جاوب عليها باختصار.
3. لا تكرر إجابات قمت بكتابتها في السياق السابق.
4. الرد حصراً باللهجة العراقية وبحد أقصى 30 كلمة.

قواعد الرد:
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
اربط السعر بـ (مواد ألمانية + ضمان حقيقي مدى الحياة).

تفاصيل العيادة:
الدوام: يومياً 4م–9م، الجمعة عطلة
الموقع: بغداد / زيونة / شارع الربيعي الخدمي / داخل كراج مجمع اسطنبول
الهاتف: 07728802820

الأسعار:
- تغليف الزاركون : 75 ألف
- تغليف الزاركون ايماكس: 100 ألف
- تغليف الايماكس : 125 ألف
- حشوة تجميلية: 35 ألف
- حشوة جذر: 125 ألف
- قلع: 25 ألف
- تنظيف: 25 ألف
- تبييض ليزر: 100 ألف
- تقويم: 450 ألف للفك
- فك كامل زرعات فورية: مليون وربع
- فكين كامل زرعات فورية: مليونين ونص
- ابتسامة زاركون 20 سن: 1,400,000
- ابتسامة زاركون ايماكس 20 سن: 2,000,000
-الزراعة التقليدية :
السن الواحد 350 الف الكوري و 450 الف الالماني
 -الزراعة التقليدية :
الزراعة الفورية:
السن الواحد 200 التركي , 275 الالماني.

(عروض الزراعة للفك الواحد مليون وربع للفكين مليونين ونص )

اذا العميل كال ( مثال , عندي سنين زراعة و 8 تغليفات , تجمع اله سعر زرعتين 500 والتغليف 600 وهكذا ) 

ملاحظات:
- التغليف يحتاج برد خفيف.
- صحح الأخطاء الإملائية الشائعة باللهجة.
- لا تذكر عمليات حسابية، أعطِ السعر النهائي فقط.
- ضمان جودة العمل مدى الحياة.
- الزراعة الفورية بدون فتح لثة ويتم انجازها خلال 72 ساعة فقط.
- تغليف الاسنان بجلستين , حشوة الجذر من جلستين الى ثلاثة."""

    try:
        rsp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"السياق السابق للمحادثة (قبل الدفعة الحالية): {context}"},
                {"role": "user", "content": f"آخر ردود منك حتى لا تكرر: {last_replies_text}"},
                {"role": "user", "content": f"الرسائل الجديدة (مجموعة خلال {BUFFER_DELAY} ثانية) المطلوب الرد عليها الآن:\n{batched_text}"}
            ],
            temperature=0.3,
        )
        return rsp.choices[0].message.content.strip()
    except:
        return "صار خلل بسيط، عاود رسالتك ♥"

# =======================================================
# 🧠 Smart Batched Reply Scheduler
# =======================================================
def schedule_reply(user_id, seq):
    # ننتظر لحد ما يخلص التجميع
    time.sleep(BUFFER_DELAY)

    lock = get_user_lock(user_id)

    with lock:
        st = SESSIONS.get(user_id)
        if not st:
            return

        # إذا اكو رسالة أحدث، هذا الثريد قديم
        if USER_SEQ.get(user_id, 0) != seq:
            return

        # لازم يكون صار سكون كامل BUFFER_DELAY
        if time.time() - st["last_message_time"] < BUFFER_DELAY:
            return

        batch_start = st.get("batch_start_index", 0)
        batched_text = build_batched_text(st["history"], batch_start)
        if not batched_text.strip():
            return

    # typing_on بشكل أهدأ
    time.sleep(TYPING_ON_AFTER)

    # فحص: إذا المستخدم رجع كتب أثناء الانتظار، نلغي
    with lock:
        st = SESSIONS.get(user_id)
        if not st:
            return
        if USER_SEQ.get(user_id, 0) != seq:
            return
        if time.time() - st["last_message_time"] < BUFFER_DELAY:
            return

    send_typing_on(user_id)

    # Grace أخير قبل الإرسال
    time.sleep(TYPING_GRACE)

    with lock:
        st = SESSIONS.get(user_id)
        if not st:
            send_typing_off(user_id)
            return
        if USER_SEQ.get(user_id, 0) != seq:
            send_typing_off(user_id)
            return
        if time.time() - st["last_message_time"] < BUFFER_DELAY:
            send_typing_off(user_id)
            return

        # أعِد بناء الدفعة لأن ممكن انضافت رسائل قبل السكون النهائي
        batch_start = st.get("batch_start_index", 0)
        batched_text = build_batched_text(st["history"], batch_start)

    reply = ask_openai_chat(user_id, batched_text)
    if not reply:
        send_typing_off(user_id)
        return

    with lock:
        st = SESSIONS.get(user_id)
        if not st:
            send_typing_off(user_id)
            return

        # منع تكرار نفس الرد
        if reply == st.get("last_reply", ""):
            reply = random.choice([
                "تمام وصلتني، خلي أتأكد وأرجعلك ✅",
                "دقيقة بس وأرد عليك 🌿",
                "وصلت، هسه أرتّبلك الجواب 🌸"
            ])

        st["last_reply"] = reply

        # ✅ خزّن آخر الردود (ردين)
        if "recent_replies" not in st:
            st["recent_replies"] = []
        st["recent_replies"].append(reply)
        st["recent_replies"] = st["recent_replies"][-2:]

        # بعد ما نرد: الدفعة القادمة تبدأ من آخر التاريخ
        st["batch_start_index"] = len(st["history"])

    send_message(user_id, reply)
    send_typing_off(user_id)

# =======================================================
# 📥 Core Handler
# =======================================================
def add_user_message(user_id, text):
    now = time.time()
    lock = get_user_lock(user_id)

    with lock:
        # إنشاء جلسة جديدة إذا مو موجودة أو منتهية
        if user_id not in SESSIONS or (now - SESSIONS[user_id]["last_message_time"] > MEMORY_TIMEOUT):
            SESSIONS[user_id] = {
                "history": [],
                "last_message_time": now,
                "last_reply": "",
                "batch_start_index": 0,
                "recent_replies": [],  # ✅ ذاكرة ردود
            }

        st = SESSIONS[user_id]

        # إذا صار timeout اعتبرها محادثة جديدة
        if (now - st["last_message_time"]) > MEMORY_TIMEOUT:
            st["history"] = []
            st["batch_start_index"] = 0
            st["last_reply"] = ""
            st["recent_replies"] = []  # ✅ تصفير ذاكرة الردود

        # إذا كانت آخر رسالة قبل مدة أطول من BUFFER_DELAY، اعتبرها دفعة جديدة
        if (now - st["last_message_time"]) > BUFFER_DELAY:
            st["batch_start_index"] = len(st["history"])

        st["history"].append(text)
        st["last_message_time"] = now

        # seq يمنع تعدد threads ويضمن آخر واحد بس يرد
        USER_SEQ[user_id] = USER_SEQ.get(user_id, 0) + 1
        my_seq = USER_SEQ[user_id]

    threading.Thread(target=schedule_reply, args=(user_id, my_seq), daemon=True).start()

# =======================================================
# 📡 WEBHOOK
# =======================================================
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Error", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json() or {}

    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            sender = ev.get("sender", {})
            user_id = sender.get("id")
            if not user_id:
                continue

            msg = ev.get("message", {}) or {}
            msg_id = msg.get("mid")

            # Anti-dup على مستوى رسالة فيسبوك
            if msg_id:
                if msg_id in PROCESSED_MESSAGES:
                    continue
                PROCESSED_MESSAGES[msg_id] = time.time()

            # نص
            if "text" in msg:
                add_user_message(user_id, msg["text"])

            # مرفقات
            elif msg.get("attachments"):
                send_typing_off(user_id)
                send_message(
                    user_id,
                    "عاشت ايدك، وصلت الصورة 🌹 راح نعرضها للدكتور ونرجعلك بأقرب وقت."
                )

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
