@echo off
cd /d C:\Users\kreye\Documents\invoice-reconciliation-agent
git add -A
"C:\Program Files\GitHub CLI\gh.exe" repo create real-estate-recon-agent --public --source . --remote origin --push
echo create_push_exit=%ERRORLEVEL%
git remote -v
git log --oneline
