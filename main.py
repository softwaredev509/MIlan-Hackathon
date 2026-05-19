import asyncio
import base64
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from speechmatics.voice import (
    VoiceAgentClient, VoiceAgentConfig,
    EndOfUtteranceMode, AgentServerMessageType,
)
from speechmatics.tts import AsyncClient as TtsClient, Voice, OutputFormat
import uvicorn

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("voicedesk")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
SPEECHMATICS_API_KEY = os.getenv("SPEECHMATICS_API_KEY", "YOUR_SPEECHMATICS_KEY")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY",    "YOUR_ANTHROPIC_KEY")
MAX_HISTORY_TURNS    = 20

claude_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are VoiceDesk, an elite AI customer support agent.
You receive real-time transcriptions of customer speech and respond concisely,
professionally, and helpfully. Rules:
- Keep responses under 3 sentences unless more detail is truly needed.
- Always acknowledge the customer's issue first.
- Suggest a clear next action.
- Tone: warm, confident, solution-focused."""

# ─── STARTUP VALIDATION ────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = []
    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_KEY":
        missing.append("ANTHROPIC_API_KEY")
    if SPEECHMATICS_API_KEY == "YOUR_SPEECHMATICS_KEY":
        missing.append("SPEECHMATICS_API_KEY")
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")
    logger.info("Config validated — all API keys present")
    yield

app = FastAPI(title="VoiceDesk AI", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def safe_task(coro):
    task = asyncio.create_task(coro)
    def _on_done(t):
        if not t.cancelled() and t.exception():
            logger.error(f"task error: {t.exception()}")
    task.add_done_callback(_on_done)
    return task

def trim_history(history: list) -> list:
    if len(history) > MAX_HISTORY_TURNS * 2:
        return history[-(MAX_HISTORY_TURNS * 2):]
    return history

# ─── CLAUDE ────────────────────────────────────────────────────────────────────
async def ask_claude(conversation_history: list):
    for attempt in range(3):
        try:
            async with asyncio.timeout(15):
                return await claude_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=300,
                    system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    messages=trim_history(conversation_history),
                )
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
        except TimeoutError:
            raise RuntimeError("Claude API timeout after 15s")

# ─── TTS ───────────────────────────────────────────────────────────────────────
async def text_to_speech(text: str) -> bytes:
    async with asyncio.timeout(10):
        async with TtsClient(api_key=SPEECHMATICS_API_KEY) as client:
            async with await client.generate(
                text=text, voice=Voice.SARAH, output_format=OutputFormat.WAV_16000
            ) as resp:
                return b"".join([chunk async for chunk in resp.content.iter_chunked(4096)])

# ─── SPEECHMATICS CONNECT WITH RETRY ──────────────────────────────────────────
async def connect_with_retry(sm_client, session_id: str, max_attempts: int = 3):
    for attempt in range(max_attempts):
        try:
            await sm_client.connect()
            return
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            logger.warning(f"[{session_id}] SM connect failed ({e}), retry {attempt + 1}/{max_attempts}")
            await asyncio.sleep(2 ** attempt)

# ─── WEBSOCKET BRIDGE ──────────────────────────────────────────────────────────
@app.websocket("/ws/voice")
async def voice_bridge(client_ws: WebSocket):
    await client_ws.accept()
    session_id = str(uuid.uuid4())[:8]
    logger.info(f"[{session_id}] connected")

    conversation_history = []
    utterance_parts = []
    turn_lock = asyncio.Lock()

    config = VoiceAgentConfig(
        language="en",
        end_of_utterance_mode=EndOfUtteranceMode.ADAPTIVE,
    )
    sm_client = VoiceAgentClient(api_key=SPEECHMATICS_API_KEY, config=config)

    @sm_client.on(AgentServerMessageType.ADD_PARTIAL_SEGMENT)
    def on_partial(msg):
        text = " ".join(s["text"] for s in msg["segments"])
        safe_task(client_ws.send_json({"type": "partial", "text": text}))

    @sm_client.on(AgentServerMessageType.ADD_SEGMENT)
    def on_segment(msg):
        for seg in msg["segments"]:
            utterance_parts.append(seg["text"])
        safe_task(client_ws.send_json({
            "type": "final",
            "text": " ".join(utterance_parts),
        }))

    async def handle_turn():
        if turn_lock.locked():
            return
        async with turn_lock:
            full_text = " ".join(utterance_parts).strip()
            utterance_parts.clear()
            if not full_text:
                return

            logger.info(f"[{session_id}] turn: {full_text[:60]!r}")
            conversation_history.append({"role": "user", "content": full_text})
            await client_ws.send_json({"type": "thinking"})

            t0 = time.monotonic()
            try:
                message = await ask_claude(conversation_history)
            except Exception as e:
                logger.warning(f"[{session_id}] Claude error: {e}")
                await client_ws.send_json({"type": "error", "text": str(e)})
                conversation_history.pop()
                return

            latency_ms = round((time.monotonic() - t0) * 1000)
            reply = message.content[0].text
            logger.info(f"[{session_id}] reply in {latency_ms}ms, {message.usage.output_tokens} tokens")

            conversation_history.append({"role": "assistant", "content": reply})
            await client_ws.send_json({"type": "agent_reply", "text": reply})
            await client_ws.send_json({
                "type": "metrics",
                "latency_ms": latency_ms,
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
            })

            try:
                audio_bytes = await text_to_speech(reply)
                await client_ws.send_json({
                    "type": "tts_audio",
                    "data": base64.b64encode(audio_bytes).decode(),
                    "mime": "audio/wav",
                })
            except Exception as e:
                logger.warning(f"[{session_id}] TTS error: {e}")

    @sm_client.on(AgentServerMessageType.END_OF_TURN)
    def on_turn_end(_msg):
        safe_task(handle_turn())

    try:
        await connect_with_retry(sm_client, session_id)
        try:
            while True:
                data = await client_ws.receive_bytes()
                await sm_client.send_audio(data)
        except WebSocketDisconnect:
            pass
        finally:
            await sm_client.disconnect()
            logger.info(f"[{session_id}] disconnected")
            try:
                await client_ws.send_json({"type": "end"})
            except Exception:
                pass
    except Exception as e:
        logger.error(f"[{session_id}] fatal: {e}", exc_info=True)
        try:
            await client_ws.send_json({"type": "error", "text": str(e)})
        except Exception:
            pass


# ─── HEALTH CHECK ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    claude_ok = False
    try:
        async with asyncio.timeout(5):
            await claude_client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=1,
                messages=[{"role": "user", "content": "ping"}]
            )
            claude_ok = True
    except Exception:
        pass
    sm_ok = SPEECHMATICS_API_KEY != "YOUR_SPEECHMATICS_KEY"
    ok = claude_ok and sm_ok
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "claude": claude_ok, "speechmatics_key": sm_ok},
        status_code=200 if ok else 503,
    )


# Serve frontend
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
