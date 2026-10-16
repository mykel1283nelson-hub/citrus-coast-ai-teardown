#!/bin/bash
# Citrus Coast AI — Model B server launcher.
# Usage: ./run.sh  (serves on 127.0.0.1:8899; expose publicly via tunnel/hosting)
cd "$(dirname "$0")"
exec .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8899
