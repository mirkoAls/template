@echo off
cd /d "%~dp0"

if not exist build\venv\Scripts\python.exe python -m venv build\venv || exit /b 1
build\venv\Scripts\python -m pip install --quiet --disable-pip-version-check -r requirements.txt pyinstaller || exit /b 1

build\venv\Scripts\python -m PyInstaller --onefile --noconfirm --log-level WARN ^
    --name selector_templates --python-option "X utf8" ^
    --hidden-import "seach template" ^
    --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
    --distpath dist --workpath build\pyinstaller --specpath build ^
    selector_templates.py || exit /b 1

echo.
echo Listo: dist\selector_templates.exe
