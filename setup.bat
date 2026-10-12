@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
echo.
echo === Контент-завод: установка ===
echo.

rem --- Python 3.12 ---
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY (
  python --version 2>nul | findstr /b /c:"Python 3.12" >nul && set "PY=python"
)
if not defined PY (
  echo [ОШИБКА] Не найден Python 3.12.
  echo Установите его: winget install -e --id Python.Python.3.12
  echo или с python.org, отметив галочку "Add python.exe to PATH". Потом запустите setup.bat ещё раз.
  pause & exit /b 1
)
echo Python: & %PY% --version

rem --- FFmpeg ---
where ffmpeg >nul 2>&1 && where ffprobe >nul 2>&1
if errorlevel 1 (
  echo [ВНИМАНИЕ] Не найден ffmpeg/ffprobe. Без него завод не сможет обрабатывать видео.
  echo Установите: winget install -e --id Gyan.FFmpeg  , затем закройте и заново откройте окно.
) else (
  echo FFmpeg: найден
)

rem --- виртуальное окружение ---
if not exist ".venv\Scripts\python.exe" (
  echo Создаю .venv ...
  %PY% -m venv .venv || (echo [ОШИБКА] не получилось создать .venv & pause & exit /b 1)
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip
echo Ставлю зависимости (несколько минут) ...
"%VPY%" -m pip install -r requirements.txt || (echo [ОШИБКА] зависимости не установились, см. текст выше & pause & exit /b 1)

echo Ставлю локальную нейросеть llama-cpp-python (готовая сборка для процессора) ...
"%VPY%" -m pip install llama-cpp-python --prefer-binary --only-binary=llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
if errorlevel 1 (
  echo [ВНИМАНИЕ] llama-cpp-python не установился. Завод будет работать, но анализ пойдёт по простым правилам.
  echo Можно переключить ai.provider на openai в config\config.yaml и указать ключ в .env.
)

rem --- конфиги ---
if not exist ".env" copy /y ".env.example" ".env" >nul && echo Создан .env - заполните ключи при необходимости
if not exist "config\config.yaml" copy /y "config\config.example.yaml" "config\config.yaml" >nul && echo Создан config\config.yaml

rem --- проверка Chrome ---
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (echo Google Chrome: найден) else if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" (echo Google Chrome: найден) else (echo [ВНИМАНИЕ] Google Chrome не найден: он нужен для публикации в Instagram. winget install -e --id Google.Chrome)

echo.
set /p DL="Скачать модели сейчас (нейросеть ~4.7 ГБ, распознавание речи ~0.5 ГБ, голоса ~0.25 ГБ)? [Y/N] "
if /i "%DL%"=="Y" "%VPY%" -m app.cli download-models

echo.
echo Готово. Запуск: start.bat , затем откройте http://127.0.0.1:8765
pause
