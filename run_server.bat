@echo off
cd /d "%~dp0"
set PYTHONPATH=%CD%
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
