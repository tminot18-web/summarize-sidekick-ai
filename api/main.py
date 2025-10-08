# main.py
import os
import asyncio
import textwrap
from typing import Optional, Literal, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from openai import OpenAI  # OpenAI Python SDK v1+

# --------------------------- Config ---------------------------

# Default small/cost-effective model; change if you prefer.
DEFAULT_MODEL = "gpt-4o-mini"

# Very safe character-based chunking so big selections don't 413/timeout
MAX_CHARS_PER_CHUNK = 3500
REQUEST_TIMEOUT_S = 60

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set")

client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI(title="Summarize API", version="1.2.0")

# Lock CORS to your Chrome extension in production.
# Find your ID at chrome://extensions and set EXT_ID env var in Render.
EXT_ID = os.getenv("EXT_ID", "").strip()
if EXT_ID:
    ALLOWED_ORIGINS = [f"chrome-extension://{EXT_ID}"]
else:
    # While developing, it's fine to stay open. Tighten before shipping.
    ALLOWED_ORIGINS = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------- Schemas ---------------------------

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
    # You can add metadata fields if you like (model, chunks, tokens_used, etc.)

# --------------------------- Utils ---------------------------

def tone_instruction(tone: Tone, n: int) -> str:
    if tone == "bullet":
        return f"Summarize the content in at most {n} concise bullet points."
    return f"Summarize the content in at most {n} sentences with a {tone} tone."

def chunk_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> List[str]:
    """Split text on paragraph boundaries first, then hard-wrap long paragraphs."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: List[str] = []
    buf = ""

    for p in paragraphs:
        if len(buf) + len(p) + 1 <= max_chars:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= max_chars:
                buf = p
            else:
                # Extremely long single paragraph: hard-slice
                for i in range(0, len(p), max_chars):
                    pieces = p[i : i + max_chars]
                    if buf:
                        chunks.append(buf)
                        buf = ""
                    chunks.append(pieces)

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
        # Map common issues to friendlier HTTP errors
        msg = str(e)
        if "insufficient_quota" in msg or "Rate limit" in msg:
            raise HTTPException(status_code=429, detail="OpenAI quota or rate limit")
        if "invalid_api_key" in msg.lower():
            raise HTTPException(status_code=401, detail="OpenAI authentication failed")
        raise HTTPException(status_code=502, detail="OpenAI upstream error")

# --------------------------- Routes ---------------------------

@app.get("/health")
async def health():
    return {"ok": True, "model": DEFAULT_MODEL}

@app.post("/summarize", response_model=SummarizeResponse)
async def summarize(req: SummarizeRequest) -> SummarizeResponse:
    model = req.model or DEFAULT_MODEL
    instr = tone_instruction(req.tone, req.maxSentences)

    parts = chunk_text(req.text)

    # Single-shot for short inputs
    if len(parts) == 1:
        prompt = f"{instr}\n\n---\n{parts[0]}"
        out = await call_openai_with_timeout(prompt, model)
        return SummarizeResponse(summary=out)

    # Multi-stage for long inputs: summarize each chunk, then synthesize
    partials: List[str] = []
    for i, ch in enumerate(parts, start=1):
        prompt = f"{instr}\n\n(Part {i} of {len(parts)})\n\n---\n{ch}"
        partials.append(await call_openai_with_timeout(prompt, model))

    synthesis = textwrap.dedent(
        f"""
        {instr}
        You are given partial summaries of several parts of a longer text.
        Merge them into ONE cohesive summary. Eliminate redundancy and keep it tight.

        PARTIAL SUMMARIES:
        {"".join(f"- {p}\n" for p in partials)}
        """
    ).strip()

    final = await call_openai_with_timeout(synthesis, model)
    return SummarizeResponse(summary=final)
