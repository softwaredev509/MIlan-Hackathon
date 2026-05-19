#!/bin/bash
# VoiceDesk — Vultr deployment script
# Run this ON your Vultr Ubuntu VM

set -e

echo "=== VoiceDesk Vultr Deployment ==="

# 1. Update & install Python
apt-get update -q
apt-get install -y python3-pip python3-venv

# 2. Create app directory
mkdir -p /opt/voicedesk
cd /opt/voicedesk

# 3. Copy files (run this script from the backend/ folder)
# Files needed: main.py, requirements.txt, static/index.html

# 4. Virtual env
python3 -m venv venv
source venv/bin/activate

# 5. Install deps
pip install -r requirements.txt

# 6. Set API keys (replace with your actual keys)
export SPEECHMATICS_API_KEY="YOUR_SPEECHMATICS_KEY"
export ANTHROPIC_API_KEY="YOUR_ANTHROPIC_KEY"

# 7. Open firewall port
ufw allow 8000/tcp || true

# 8. Launch (background)
nohup python3 main.py > voicedesk.log 2>&1 &
echo "PID: $!"
echo ""
echo "✅ VoiceDesk running at: http://$(curl -s ifconfig.me):8000"
echo "   Logs: tail -f /opt/voicedesk/voicedesk.log"
