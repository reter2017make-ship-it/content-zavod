@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите setup.bat
  pause & exit /b 1
)
echo Контент-завод запускается. Не закрывайте это окно, пока работаете. Остановить: Ctrl+C
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8765"
".venv\Scripts\python.exe" -m app.cli web
pause
