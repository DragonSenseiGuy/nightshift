import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://ai.hackclub.com/proxy/v1")
API_KEY = os.getenv("OPENROUTER_API_KEY")

MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.5-flash")

if not API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be set in your .env file.")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

SYSTEM = """You are an assistant that writes a concise morning email digest.
You receive a JSON array of emails (sender, subject, snippet, body).
Produce a single self-contained HTML email body that:
- Opens with a one-line overview (how many emails, key themes).
- Groups emails by importance/topic.
- For each, gives a one-sentence summary and flags anything needing a reply or action.
- Uses simple inline-styled HTML (no external CSS, no <html>/<head> wrapper — just the body content).
Return ONLY the HTML. No markdown, no code fences, no commentary."""


def _trim(emails, max_body=3000):
    """Cap each body so huge HTML emails don't blow up the token budget."""
    return [{**e, "body": (e.get("body") or "")[:max_body]} for e in emails]


def _strip_fence(text: str) -> str:
    """Drop a leading/trailing ```html ... ``` fence if the model adds one."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def summarise_emails(detailed: list) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=16000,
        messages=[
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": f"Here are the emails:\n\n{json.dumps(_trim(detailed))}",
            },
        ],
    )
    return _strip_fence(response.choices[0].message.content or "")
