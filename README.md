# VoiceDesk — AI Customer Support Agent

> Real-time voice-powered enterprise customer support agent  
> Built for the **AI Agent Olympics Hackathon 2026**

## Tech Stack
- **Speechmatics** — Real-time speech-to-text (WebSocket RT API)
- **Claude Sonnet** (Anthropic) — AI response generation  
- **FastAPI** — Backend WebSocket bridge
- **Vultr VM** — Cloud deployment

## How It Works
1. User clicks mic → browser captures audio at 16kHz
2. Audio streams via WebSocket to the FastAPI backend
3. Backend pipes audio to **Speechmatics RT API** → live transcripts
4. On final transcript → **Claude** generates a support response
5. Response appears in the UI in real time with latency metrics

## Features
- 🎤 Real-time partial + final transcription
- 🔊 Live audio waveform visualizer
- 🤖 Claude-powered intelligent support responses
- 📊 Session stats (exchanges, latency, word count)
- ☁️ Deployed on Vultr

## Setup

```bash
cd backend
pip install -r requirements.txt
export SPEECHMATICS_API_KEY="your_key"
export ANTHROPIC_API_KEY="your_key"
python main.py
# → open http://localhost:8000
```

## Vultr Deployment
```bash
chmod +x deploy_vultr.sh
# Edit keys in deploy_vultr.sh first, then:
./deploy_vultr.sh
```

## Tracks
- ✅ Speechmatics Challenge
- ✅ Vultr Challenge (deployed on Vultr VM)
