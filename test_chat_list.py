import requests
import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID")
CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET")

def get_token():
    url = "https://oapi.dingtalk.com/gettoken"
    params = {"appkey": CLIENT_ID, "appsecret": CLIENT_SECRET}
    resp = requests.get(url, params=params)
    return resp.json().get("access_token")

def get_chat_list(token):
    url = f"https://oapi.dingtalk.com/chat/list?access_token={token}"
    resp = requests.get(url)
    print(f"Status: {resp.status_code}")
    print(f"Body: {resp.text}")

if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Please set DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET in .env")
    else:
        token = get_token()
        if token:
            print(f"Token: {token[:10]}...")
            get_chat_list(token)
        else:
            print("Failed to get token")