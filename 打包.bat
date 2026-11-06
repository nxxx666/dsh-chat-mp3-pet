@echo off
rem 一键打包：产物在 dist\桌宠大肥鱼\（PyInstaller onedir）
rem 依赖：PATH 上的 Python 3.10+ + pyinstaller + PyQt6 + live2d-py
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    pause
    exit /b 1
)
echo Building (takes 1-3 minutes)...
python -m PyInstaller --noconfirm --noconsole --onedir ^
  --name "桌宠大肥鱼" ^
  --distpath dist ^
  --workpath build ^
  --specpath . ^
  --add-data "assets;assets" ^
  --add-data "personas;personas" ^
  --add-data "music;music" ^
  --add-data "live2d/model;live2d/model" ^
  --add-data "live2d/model_st1280;live2d/model_st1280" ^
  --collect-all live2d ^
  --hidden-import "PyQt6.QtMultimedia" ^
  --hidden-import "PyQt6.QtOpenGLWidgets" ^
  --exclude-module numpy ^
  --exclude-module scipy ^
  --exclude-module PIL ^
  --exclude-module mkl ^
  setup_and_run.py

if errorlevel 1 (
    echo [ERROR] build failed
    pause
    exit /b 1
)

copy /y "使用说明.txt" "dist\桌宠大肥鱼\使用说明.txt" >nul 2>nul
del /q "dist\桌宠大肥鱼\memory.db" >nul 2>nul

echo.
echo Done: dist\桌宠大肥鱼\桌宠大肥鱼.exe
start "" "dist\桌宠大肥鱼"
pause
