import asyncio
import json
import os
import websockets
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

app = FastAPI(title="VoiceDesk AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
SPEECHMATICS_API_KEY = os.getenv("SPEECHMATICS_API_KEY", "YOUR_SPEECHMATICS_KEY")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY",    "YOUR_ANTHROPIC_KEY")
SPEECHMATICS_RT_URL  = "wss://eu2.rt.speechmatics.com/v2"

SYSTEM_PROMPT = """You are VoiceDesk, an elite AI customer support agent.
You receive real-time transcriptions of customer speech and respond concisely,
professionally, and helpfully. Rules:
- Keep responses under 3 sentences unless more detail is truly needed.
- Always acknowledge the customer's issue first.
- Suggest a clear next action.
- Tone: warm, confident, solution-focused."""

# ─── SPEECHMATICS CONFIG ───────────────────────────────────────────────────────
SM_START_MSG = {
    "message": "StartRecognition",
    "audio_format": {"type": "raw", "encoding": "pcm_f32le", "sample_rate": 16000},
    "transcription_config": {
        "language": "en",
        "enable_partials": True,
        "enable_entities": True,
        "diarization": "speaker",
        "operating_point": "enhanced",
    },
}

# ─── CLAUDE ────────────────────────────────────────────────────────────────────
async def ask_claude(conversation_history: list) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "system": SYSTEM_PROMPT,
                "messages": conversation_history,
            },
        )
        data = resp.json()
        return data["content"][0]["text"]

# ─── WEBSOCKET BRIDGE ──────────────────────────────────────────────────────────
@app.websocket("/ws/voice")
async def voice_bridge(client_ws: WebSocket):
    await client_ws.accept()
    conversation_history = []
    current_utterance = ""

    headers = {"Authorization": f"Bearer {SPEECHMATICS_API_KEY}"}

    try:
        async with websockets.connect(SPEECHMATICS_RT_URL, extra_headers=headers) as sm_ws:
            # Send StartRecognition
            await sm_ws.send(json.dumps(SM_START_MSG))

            async def receive_from_client():
                """Receive audio chunks from browser and forward to Speechmatics."""
                nonlocal current_utterance
                try:
                    while True:
                        data = await client_ws.receive_bytes()
                        await sm_ws.send(data)
                except WebSocketDisconnect:
                    # Signal end of audio
                    try:
                        await sm_ws.send(json.dumps({"message": "EndOfStream", "last_seq_no": 0}))
                    except Exception:
                        pass

            async def receive_from_speechmatics():
                """Receive transcripts from Speechmatics and forward to browser."""
                nonlocal current_utterance
                try:
                    async for raw in sm_ws:
                        msg = json.loads(raw)
                        msg_type = msg.get("message", "")

                        # Partial transcript — stream to UI for live feedback
                        if msg_type == "AddPartialTranscript":
                            partial = " ".join(
                                r.get("content", "") for r in msg.get("results", [])
                            )
                            await client_ws.send_json({
                                "type": "partial",
                                "text": partial,
                            })

                        # Final transcript — trigger Claude
                        elif msg_type == "AddTranscript":
                            final = " ".join(
                                r.get("content", "") for r in msg.get("results", [])
                            ).strip()
                            if not final:
                                continue

                            current_utterance += " " + final
                            await client_ws.send_json({
                                "type": "final",
                                "text": current_utterance.strip(),
                            })

                            # Build conversation & call Claude
                            conversation_history.append({
                                "role": "user",
                                "content": current_utterance.strip(),
                            })
                            current_utterance = ""

                            await client_ws.send_json({"type": "thinking"})
                            reply = await ask_claude(conversation_history)

                            conversation_history.append({
                                "role": "assistant",
                                "content": reply,
                            })

                            await client_ws.send_json({
                                "type": "agent_reply",
                                "text": reply,
                            })

                        elif msg_type == "EndOfTranscript":
                            await client_ws.send_json({"type": "end"})
                            break
                except Exception as e:
                    await client_ws.send_json({"type": "error", "text": str(e)})

            await asyncio.gather(
                receive_from_client(),
                receive_from_speechmatics(),
            )

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await client_ws.send_json({"type": "error", "text": str(e)})
        except Exception:
            pass


# ─── HEALTH CHECK ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "VoiceDesk AI"}


# Serve frontend
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
