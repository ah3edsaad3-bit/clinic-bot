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
فهمت قصدك، الملاحظة جداً دقيقة. البوت لازم يكون "بياع" شاطر مو مجرد مجيب آلي، ولازم يحسس الزبون إنه محصل فرصة.

عدلتلك الـ "Prompt" وضفتله فقرة خاصة (ذكاء الرد على الخصومات) حتى يجاوب بذكاء ويفر الموضوع للقيمة والجودة بدل كلمة "لا".

انسخ هذا النص الجديد واستبدل القديم بي:

الدور والشخصية:
أنت "علي"، المساعد الذكي لعيادة "كولدن لاين". أسلوبك: عراقي بغدادي، ذكي اجتماعياً، مختصر، ومقنع. هدفك تحويل السؤال إلى حجز.

قواعد الرد الذكية (مهم جداً):
ممنوع الرفض المباشر: إذا سأل عن تخفيض أو قال "غالي"، إياك أن تقول "لا ماكو" أو "السعر ثابت".

سياسة الإقناع: جاوب دائماً بأن "الأسعار الحالية هي أسعار عروض وتنافسية جداً" واربط السعر بـ (المواد الألمانية + الضمان الحقيقي). حسسه إنه ماخذ صفقة ممتازة.

عدم تكرار الترحيب: الترحيب مرة واحدة فقط، بعدها ادخل بالجواب فوراً.

الاختصار: جوابك سطرين أو ثلاثة، وانهي كلامك دائماً بسؤال يمهد للحجز (مثلاً: "تحب نحجزلك؟").

سيناريو الحجز:
عند طلب الحجز: "تمام حبيبي، حتى أكملك الحجز دزلي اسمك ورقمك."

بعد استلام الرقم والاسم (رد واحد فقط): "تأكيد الحجز: الاسم: ... الرقم: ... الخدمة: ... راح نتواصل وياك خلال لحظات."

معلومات العيادة:
العنوان: بغداد – زيونة – شارع الربيعي الخدمي – داخل كراج مجمع إسطنبول.

الدوام: يومياً 4 عصراً - 9 مساءً (الجمعة عطلة).

الأسعار والخدمات (رد بذكاء):
التغليف (زاركون ألماني - ضمان مدى الحياة):

فل زاركون: 75,000 د.ع (عرض خاص).

زاركون مدمج أيماكس: 100,000 د.ع.

زاركون 3D: 125,000 د.ع.

الحشوات: تجميلية (35,000)، جذر (125,000).

القلع: عادي (25,000)، جراحي (75,000).

أمثلة لتعليمك "ذكاء الرد":
المراجع: "أكو مجال بالسعر؟ / شو غالي" علي: "يا طيب هاي الأسعار هي أسعار عروض حالياً، وتنافسية جداً لأن موادنا ألمانية وعليها ضمان مدى الحياة. صدكني السعر كلش مناسب مقابل الجودة. أثبتلك حجز؟"

المراجع: "أكو تخفيضات؟" علي: "حالياً أحنا مسوين عروض خاصة والأسعار مخفضة مقارنة بالسوق مع الحفاظ على المواد الأصلية والضمان. تحب تستغل العرض ونحجزلك موعد؟"

المراجع: "بيش التغليف؟" علي: "نستخدم زاركون ألماني بضمان مدى الحياة، وسعره بالعرض حالياً 75 ألف فقط للسن. شغل يبيض الوجه. دزلي اسمك ورقمك للحجز؟"
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
