#!/bin/bash
cd /home/alphabot/app
/usr/bin/screen -dmS alphabot python3 -m app.main
/usr/bin/screen -dmS dashboard bash -c 'cd /home/alphabot/app && uvicorn app.dashboard:app --host 0.0.0.0 --port 8080'
/usr/bin/screen -dmS agent bash -c 'cd /home/alphabot/app && uvicorn ai_debug.main:app --host 0.0.0.0 --port 8000'
echo "AlphaBot + Dashboard + Agent started"
