from dotenv import load_dotenv
import os
import requests

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError("GEMINI_API_KEY is not loaded")

url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
}

payload = {
    "model": "gemini-3.7-flash",
    "reasoning_effort": "low",
    "messages": [
        {
            "role": "user",
            "content": "Say hello in one sentence.",
        }
    ],
    "stream": False,
}

response = requests.post(
    url,
    headers=headers,
    json=payload,
    timeout=20,
)

print("Status:", response.status_code)
print("Response:", response.text[:2000])