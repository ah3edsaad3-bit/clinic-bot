from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading

app = Flask(__name__)

# ==============================
# 1) Tokens
# ==============================

VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================
# 2) Sessions Memory
# ==============================

SESSIONS = {}
BUFFER_DELAY = 15
MEMORY_TIMEOUT = 900   # 15 minutes


# ==============================
# 3) Schedule (15 sec merge)
# ==============================

def schedule_reply(user_id):
    time.sleep(BUFFER_DELAY)

    state = SESSIONS.get(user_id)
    if state is None:
        return

    now = time.time()

    # إذا ما وصلت رسالة جديدة خلال 15 ثانية → نرد
    if (now - state["last_message_time"]) >= BUFFER_DELAY:
        try:
            reply = ask_openai(user_id)
        except Exception as e:
            print("❌ OpenAI Error:", e)
            reply = "صار خلل بسيط، جرب مرة ثانية 🙏"

        send_message(user_id, reply)


# ==============================
# 4) Add Message + Memory 15 min
# ==============================

def add_user_message(user_id, text):
    now = time.time()

    # إنشاء جلسة جديدة إذا قديمة أو غير موجودة
    if user_id not in SESSIONS or (now - SESSIONS[user_id]["last_message_time"] > MEMORY_TIMEOUT):
        SESSIONS[user_id] = {
            "messages": [],
            "last_message_time": now
        }

    SESSIONS[user_id]["messages"].append(text)
    SESSIONS[user_id]["last_message_time"] = now

    # تشغيل مؤقت الدمج
    t = threading.Thread(target=schedule_reply, args=(user_id,))
    t.start()


# ==============================
# 5) AI — آخر رسالة فقط + سياق خلفي
# ==============================

def ask_openai(user_id):
    msgs = SESSIONS[user_id]["messages"]

    # آخر رسالة فقط
    last_message = msgs[-1]

    # التاريخ السابق كخلفية فقط
    if len(msgs) > 1:
        history = " | ".join(msgs[:-1])
    else:
        history = ""

    system_prompt = """
انت "علي" المساعد الذكي الرسمي لعيادة كولدن لاين لطب وتجميل الأسنان.

▪️ ترد على **آخر رسالة فقط**.
▪️ تستخدم الرسائل السابقة فقط لفهم السياق، بدون ما تعيد الشرح.
▪️ ردودك قصيرة، لبقة، باللهجة العراقية الواضحة.

▪️ إذا الزبون يريد يحجز، تطلب منه الاسم والرقم:
   "تمام حبيبي، حتى أكملك الحجز دزلي اسمك ورقمك."

▪️ لا تكرر نفس المعلومات بنفس المحادثة.
▪️ لا ترجع تعيد الأسعار إلا إذا طلبها صراحة.

▪️ رقم العيادة: 07728802820
▪️ العنوان: بغداد – زيونة – شارع الربيعي
"""

    rsp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "assistant",
                "content": f"خلفية المحادثة السابقة: {history}"
            },
            {
                "role": "user",
                "content": last_message
            }
        ],
        max_tokens=200
    )

    return rsp.choices[0].message.content.strip()


# ==============================
# 6) Webhook Endpoints
# ==============================

@app.route("/", methods=["GET"])
def home():
    return "GoldenLine bot — Reply only to last message — Memory OK"


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

    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            sender = ev["sender"]["id"]

            if "message" in ev and "text" in ev["message"]:
                text = ev["message"]["text"]
                add_user_message(sender, text)

    return "OK", 200


# ==============================
# 7) Facebook Reply
# ==============================

def send_message(receiver, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": receiver},
        "message": {"text": text}
    }

    requests.post(url, params=params, json=payload)


# ==============================
# Render Server
# ==============================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
