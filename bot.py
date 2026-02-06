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
MEMORY_TIMEOUT = 1800   # 30 دقيقة
HISTORY_LIMIT = 12      # limit للـ history (structured)
REQUEST_TIMEOUT = 10    # seconds (Meta + OpenAI)
SESSION_CLEAN_AFTER = 3600   # ساعة
DUP_MSG_CLEAN_AFTER = 600    # 10 دقائق
CLEANER_SLEEP = 600          # كل 10 دقائق

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

    if (not st) or (now - st.get("last_message_time", 0) > MEMORY_TIMEOUT):
        SESSIONS[user_id] = {
            "history": [],          # structured
            "last_message_time": now,
            "msg_version": 0,       # لمنع الرد المزدوج
            "last_reply": ""
        }
    else:
        st["last_message_time"] = now

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
- الزراعة التقليدية: السن الواحد 350 الف الكوري و 450 الف الالماني
- الزراعة الفورية: السن الواحد 200 التركي , 275 الالماني.
(عروض الزراعة للفك الواحد مليون وربع للفكين مليونين ونص )

اذا العميل كال ( مثال , عندي سنين زراعة و 8 تغليفات , تجمع اله سعر زرعتين 500 والتغليف 600 وهكذا ) 

ملاحظات:
- التغليف يحتاج برد خفيف.
- صحح الأخطاء الإملائية الشائعة باللهجة.
- لا تذكر عمليات حسابية، أعطِ السعر النهائي فقط.
- ضمان جودة العمل مدى الحياة.
- الزراعة الفورية بدون فتح لثة ويتم انجازها خلال 72 ساعة فقط.
- تغليف الاسنان بجلستين , حشوة الجذر من جلستين الى ثلاثة.
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

    send_typing(user_id)

    msg = last_user_message(user_id)
    if not msg:
        return

    reply = ask_openai_chat(user_id, msg)
    if not reply:
        return

    # منع تكرار نفس الرد حرفياً
    if reply.strip() == (st.get("last_reply") or "").strip():
        return

    append_history(user_id, "assistant", reply)
    st["last_reply"] = reply
    send_message(user_id, reply)

# =======================================================
# 🧾 add_user_message (كاملة)
# =======================================================
def add_user_message(user_id, text):
    ensure_session(user_id)
    st = SESSIONS[user_id]

    append_history(user_id, "user", text)
    st["last_message_time"] = time.time()

    # version counter يلغي أي Thread قديم
    st["msg_version"] += 1
    current_version = st["msg_version"]

    threading.Thread(
        target=schedule_reply,
        args=(user_id, current_version),
        daemon=True
    ).start()

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
    data = request.get_json(silent=True) or {}

    try:
        for entry in data.get("entry", []):
            for ev in entry.get("messaging", []):
                sender = ev.get("sender", {})
                user_id = sender.get("id")
                if not user_id:
                    continue

                msg = ev.get("message", {})
                msg_id = msg.get("mid")

                # منع تكرار نفس الرسالة
                if msg_id:
                    if msg_id in PROCESSED_MESSAGES:
                        continue
                    PROCESSED_MESSAGES[msg_id] = time.time()

                # نص
                if "text" in msg:
                    add_user_message(user_id, msg.get("text", ""))

                # مرفقات
                elif "attachments" in msg:
                    send_message(
                        user_id,
                        "عاشت ايدك، وصلت الصورة وراح ندزها للدكتور. راح يطلع عليها ونطيك التفاصيل بأقرب وقت إن شاء الله 🌹"
                    )

    except Exception as e:
        print("Webhook error:", e)

    return "OK", 200

if __name__ == "__main__":
    # Render/Hosting Platforms غالباً يمررون PORT بالـ env
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
