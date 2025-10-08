# main.py — FastAPI backend for Render
import os
import asyncio
import textwrap
import hashlib
import time
from typing import Optional, Literal, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from openai import OpenAI  # v1 SDK

# ---------- Config ----------
DEFAULT_MODEL = "gpt-4o-mini"
MAX_CHARS_PER_CHUNK = 3500
REQUEST_TIMEOUT_S = 60

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set")

client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI(title="Summarize API", version="1.3.0")

# Lock CORS to your extension in prod by setting EXT_ID in Render env
EXT_ID = os.getenv("EXT_ID", "").strip()
ALLOWED_ORIGINS = [f"chrome-extension://{EXT_ID}"] if EXT_ID else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory cache (per instance)
CACHE_TTL_S = 600
_cache: dict[str, tuple[float, str]] = {}

def cache_get(key: str) -> Optional[str]:
    item = _cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    return val

def cache_set(key: str, value: str) -> None:
    _cache[key] = (time.time(), value)

# ---------- Models ----------
Tone = Literal["precise", "casual", "bullet", "academic"]

class SummarizeRequest(BaseModel):
    text: str = Field(..., min_length=1)
    tone: Tone = "precise"
    maxSentences: int = Field(3, ge=1, le=10)
    model: Optional[str] = None

    @validator("text")
    def trimmed(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text cannot be empty")
        return v

class SummarizeResponse(BaseModel):
    summary: str

# ---------- Helpers ----------
def tone_instruction(tone: Tone, n: int) -> str:
    if tone == "bullet":
        return f"Summarize the content in at most {n} concise bullet points."
    return f"Summarize the content in at most {n} sentences with a {tone} tone."

def chunk_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> List[str]:
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 1 <= max_chars:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= max_chars:
                buf = p
            else:
                # very long single paragraph
                for i in range(0, len(p), max_chars):
                    piece = p[i : i + max_chars]
                    if buf:
                        chunks.append(buf)
                        buf = ""
                    chunks.append(piece)
    if buf:
        chunks.append(buf)
    return chunks or [""]

async def call_openai_with_timeout(prompt: str, model: str) -> str:
    async def _call() -> str:
        comp = await asyncio.to_thread(
            client.chat.completions.create,
            model=model,
            messages=[
                {"role": "system", "content": "You are a careful summarizer. Avoid adding facts not present."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return comp.choices[0].message.content.strip()

    try:
        return await asyncio.wait_for(_call(), timeout=REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Upstream model timed out")
    except Exception as e:
        msg = str(e)
        if "insufficient_quota" in msg or "Rate limit" in msg:
            raise HTTPException(status_code=429, detail="OpenAI quota or rate limit")
        if "invalid_api_key" in msg.lower():
            raise HTTPException(status_code=401, detail="OpenAI authentication failed")
        raise HTTPException(status_code=502, detail="OpenAI upstream error")

# ---------- Routes ----------
@app.get("/health")
async def health():
    return {"ok": True, "model": DEFAULT_MODEL, "cache_size": len(_cache)}

@app.post("/summarize", response_model=SummarizeResponse)
async def summarize(req: SummarizeRequest) -> SummarizeResponse:
    model = req.model or DEFAULT_MODEL
    instr = tone_instruction(req.tone, req.maxSentences)

    # cache key
    digest_input = f"{req.tone}|{req.maxSentences}|{req.text}"
    cache_key = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        return SummarizeResponse(summary=cached)

    parts = chunk_text(req.text)

    if len(parts) == 1:
        prompt = f"{instr}\n\n---\n{parts[0]}"
        out = await call_openai_with_timeout(prompt, model)
        cache_set(cache_key, out)
        return SummarizeResponse(summary=out)

    # Multi-stage: summarize chunks, then synthesize
    partials: List[str] = []
    for i, ch in enumerate(parts, start=1):
        prompt = f"{instr}\n\n(Part {i} of {len(parts)})\n\n---\n{ch}"
        partials.append(await call_openai_with_timeout(prompt, model))

    synthesis = textwrap.dedent(
        f"""
        {instr}
        You are given partial summaries of several parts of a longer text.
        Merge them into one cohesive summary. Remove redundancy.

        PARTIAL SUMMARIES:
        {"".join(f"- {p}\n" for p in partials)}
        """
    ).strip()

    final = await call_openai_with_timeout(synthesis, model)
    cache_set(cache_key, final)
    return SummarizeResponse(summary=final)
