from flask import Flask, request
import requests
from openai import OpenAI
import time
import os
import threading

app = Flask(__name__)

# Tokens from Environment Variables
VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# Sessions memory
SESSIONS = {}
BUFFER_DELAY = 15  # 15 seconds


# ---------------------------------------
#  1) 15-second Message Buffer System
# ---------------------------------------

def schedule_reply(user_id):
    """Wait 15 seconds — if no new messages, process."""
    time.sleep(BUFFER_DELAY)

    state = SESSIONS.get(user_id)
    if state is None:
        return

    now = time.time()

    if (now - state["last_message_time"]) >= BUFFER_DELAY:
        messages = state["messages"]
        final_text = " ".join(messages)

        try:
            reply = ask_openai(final_text)
        except Exception as e:
            print("❌ OpenAI Error:", e)
            reply = "صار خلل بسيط، حاول مرة ثانية 🙏"

        send_message(user_id, reply)

        # Reset session
        SESSIONS[user_id] = {
            "messages": [],
            "last_message_time": 0
        }


def add_user_message(user_id, text):
    now = time.time()

    if user_id not in SESSIONS:
        SESSIONS[user_id] = {"messages": [], "last_message_time": now}

    SESSIONS[user_id]["messages"].append(text)
    SESSIONS[user_id]["last_message_time"] = now

    # Start timer thread
    t = threading.Thread(target=schedule_reply, args=(user_id,))
    t.start()


# ---------------------------------------
#  2) OpenAI Handler with NEW PROMPT
# ---------------------------------------

def ask_openai(user_input):
    system_prompt = """
انت "علي" المساعد الذكي الرسمي لعيادة كولدن لاين لطب وتجميل الأسنان.

▪️ تحجي باللهجة العراقية الواضحة، مختصرة، محترمة، وبدون تعقيد.
▪️ ردودك قصيرة، مباشرة، لطيفة، ومقنعة، وتركّز على راحة المراجع.

▪️ مهمتك الأساسية:
1) تفهّم سؤال المراجع وتشرح له ببساطة وطمأنينة.
2) إذا راد يحجز، تطلب منه (الاسم + رقم الهاتف) بصيغة مهذبة:
   "تمام حبيبي، حتى أكملك الحجز دزلي اسمك ورقمك."
3) من يدز الاسم والرقم، ترجعله رسالة جاهزة:
   "تأكيد الحجز:
    الاسم: …
    الرقم: …
    الخدمة المطلوبة: (حسب سياق كلام المراجع)
    راح نتواصل وياك خلال لحظات."
4) تمثل عيادة طبية محترفة:
   • بدون مزاح ثقيل  
   • بدون مبالغة  
   • بدون أسلوب تجاري زايد  
5) إذا السؤال طبي، جاوبه وطمّنه. 
   وإذا كانت حالة معقدة أو طارئة، وجّهه بهدوء إلى واتساب العيادة: 07728802820
6) إذا كلام المراجع مو واضح، ساعده وتعرّف على خدمته المناسبة.

▪️ معلومات العيادة:
- الاسم: عيادة كولدن لاين لطب وتجميل الأسنان
- الموقع: بغداد – زيونة – شارع الربيعي الخدمي – داخل كراج مجمع إسطنبول
- الدوام: يوميًا من 4 مساءً إلى 9 مساءً (الجمعة عطلة)
- رقم الحجز: 07728802820

▪️ أسعار وخدمات العيادة:
1) تغليف الأسنان (زاركون):
   • فل زاركون: 75,000 د.ع للسن
   • زاركون مدمج أيماكس: 100,000 د.ع للسن
   • زاركون 3D: 125,000 د.ع للسن
   • نوع الزاركون ألماني – ضمان جودة العمل مدى الحياة
   • التجهيز جلستين بينهن 5–7 أيام
   • تركيب أسنان مؤقتة ثاني يوم

2) الحشوة التجميلية:
   • 35,000 د.ع – جلسة وحدة

3) حشوة الجذر:
   • 125,000 د.ع – عادة 3 جلسات
   • بعض الحالات جلسة وحدة إذا السن غير ملتهب

4) القلع:
   • القلع العادي: 25,000 د.ع
   • القلع الجراحي: 75,000 د.ع

▪️ أسلوب الرد:
- مختصر جدًا
- لبق
- بدون مبالغة
- يخفف القلق
- يحترم المراجع
- يشرح المعلومة ببساطة وسلاسة

تذكّر: أنت تمثل عيادة طبية، ومهمتك الأساسية هي مساعدة المراجع وتسهيل الحجز.
"""

    rsp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input}
        ],
        max_tokens=250
    )

    return rsp.choices[0].message.content.strip()


# ---------------------------------------
#  3) Webhook + Facebook sender
# ---------------------------------------

@app.route("/", methods=["GET"])
def home():
    return "Render bot running with 15s buffer + GoldenLine Prompt ⏳"


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


def send_message(receiver, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": receiver},
        "message": {"text": text}
    }

    r = requests.post(url, params=params, json=payload)
    print("📤 Facebook:", r.text)


# Render server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
