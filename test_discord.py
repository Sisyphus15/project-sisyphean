import os
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TEST_CHANNEL_ID = os.getenv("DISCORD_TEST_CHANNEL") or os.getenv("DISCORD_GENERAL_CHAT")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set in .env")

if not TEST_CHANNEL_ID:
    raise RuntimeError("DISCORD_TEST_CHANNEL or DISCORD_GENERAL_CHAT not set in .env")

def send_test_message():
    url = f"https://discord.com/api/v10/channels/{TEST_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }
    json_data = {
        "content": ":muscle: Hello from sisyphean-core! Bot token + channel are working."
    }

    resp = requests.post(url, headers=headers, json=json_data, timeout=10)
    print("Status:", resp.status_code)
    print("Response:", resp.text)

if __name__ == "__main__":
    send_test_message()

