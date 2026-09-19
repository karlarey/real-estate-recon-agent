@echo off
set NGROK=C:\Users\kreye\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe
"%NGROK%" http 8000 --log=stdout
