from flask import Flask, request
import requests
from openai import OpenAI
import time
import os

app = Flask(__name__)

# 🔑 Tokens from Environment Variables
VERIFY_TOKEN = "goldenline_secret"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# 🧠 Simple memory system
SESSIONS = {}
SESSION_TIMEOUT = 600  # 10 minutes


def add_message_to_session(user_id, text):
    now = time.time()
    state = SESSIONS.get(user_id)

    if state is None or (now - state["last_time"] > SESSION_TIMEOUT):
        state = {"messages": [], "last_time": now}
        SESSIONS[user_id] = state

    state["messages"].append(text)
    state["last_time"] = now

    if len(state["messages"]) > 10:
        state["messages"] = state["messages"][-10:]

    return state["messages"]


def ask_openai(messages_list):
    combined = " | ".join(messages_list)

    system_prompt = (
        "انت مساعد ذكي ترد باللهجة العراقية وبشكل قصير ومقنع، "
        "واذا الزبون يريد يحجز اطلب الاسم والرقم."
    )

    rsp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": combined}
        ],
        max_tokens=200
    )

    return rsp.choices[0].message.content.strip()


@app.route("/", methods=["GET"])
def home():
    return "Render Bot is running! ✅"


@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            sender = ev["sender"]["id"]

            if "message" in ev and "text" in ev["message"]:
                text = ev["message"]["text"]

                msg_list = add_message_to_session(sender, text)

                try:
                    reply = ask_openai(msg_list)
                except Exception as e:
                    print("❌ OpenAI Error:", e)
                    reply = "صار خطأ بسيط… حاول مرة ثانية 🙏"

                send_message(sender, reply)

    return "EVENT_RECEIVED", 200


def send_message(receiver, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": receiver},
        "message": {"text": text}
    }
    r = requests.post(url, params=params, json=payload)
    print("Facebook response:", r.text)


# Run app for Render
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
