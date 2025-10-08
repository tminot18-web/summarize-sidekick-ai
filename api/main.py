from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os, httpx

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Payload(BaseModel):
    text: str
    tone: str = "precise"
    maxSentences: int = 3
    model: Optional[str] = "gpt-4o-mini"

def style(tone: str, n: int) -> str:
    if tone == "quick":
        return "2 short sentences, plain language."
    if tone == "creative":
        return "punchy and vivid, 3 sentences max."
    return f"concise, neutral; {n} sentences max."

@app.get("/ping")
async def ping():
    return {"ok": True}

@app.post("/summarize")
async def summarize(p: Payload):
    if not OPENAI_API_KEY:
        return JSONResponse({"error": "missing_api_key"}, status_code=500)
    prompt = f"Summarize faithfully. Output {style(p.tone, p.maxSentences)}\n\n---\n{p.text}\n---"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": p.model,
                    "messages": [
                        {"role": "system", "content": "You are a precise summarizer."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.2
                }
            )
        data = r.json()
        if r.status_code != 200:
            return JSONResponse({"error": "openai_error", "status": r.status_code, "body": data}, status_code=502)
        summary = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        if not summary:
            return JSONResponse({"error": "empty_summary", "body": data}, status_code=502)
        return {"summary": summary}
    except Exception as e:
        return JSONResponse({"error": "server_exception", "detail": str(e)}, status_code=500)
