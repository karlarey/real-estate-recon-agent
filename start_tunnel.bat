@echo off
REM QUIC/UDP is blocked on this network, so force HTTP/2 over TCP.
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8000 --protocol http2 --no-autoupdate
