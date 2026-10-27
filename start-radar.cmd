@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Не найден Python проекта. Проверьте окружение по README.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -m dealradar --db data/live.sqlite3 desktop
echo.
echo Программа завершена. При неподтвержденном выходе отзовите сессию в Telegram.
pause
