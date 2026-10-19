@echo off
REM Windows 打包脚本（owner 移植到 Windows 机后运行）。
cd /d "%~dp0\.."

python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r backend\requirements.txt pyinstaller

cd frontend
call npm install
call npm run build
cd ..

pyinstaller --clean --noconfirm packaging\anvil.spec
echo 产物在 dist\anvil-dist\（anvil.exe + anvil-server.exe）
