@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
if exist "D:\Anaconda\python.exe" set "PY=D:\Anaconda\python.exe"
if "%PY%"=="" (
    where python >nul 2>nul
    if !errorlevel!==0 set "PY=python"
)
if "%PY%"=="" (
    echo [ERROR] 没找到 Python，请先装 Python 3.10+（或改成本机 python.exe 的路径）
    pause
    exit /b 1
)
echo 使用 Python: %PY%
"%PY%" -c "import PyQt6, live2d, PyInstaller" 2>nul
if errorlevel 1 (
    echo [ERROR] 缺依赖，先执行： "%PY%" -m pip install -i https://mirrors.aliyun.com/pypi/simple PyQt6 live2d-py pyinstaller
    pause
    exit /b 1
)

echo.
echo === 打包中（1-3 分钟，别关窗口）===
"%PY%" -m PyInstaller --noconfirm --noconsole --onedir ^
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
    echo [ERROR] 打包失败，看上面报错
    pause
    exit /b 1
)

copy /y "使用说明.txt" "dist\桌宠大肥鱼\使用说明.txt" >nul 2>nul
del /q "dist\桌宠大肥鱼\memory.db" >nul 2>nul
del /q "dist\桌宠大肥鱼\_trace.log" >nul 2>nul

echo.
echo === 生成 Release 压缩包 ===
"%PY%" "tools\make_release_zip.py" "dist\桌宠大肥鱼"
if errorlevel 1 (
    echo [ERROR] 压缩包生成失败
    pause
    exit /b 1
)

echo.
echo 完成。把桌面上那个 桌宠大肥鱼-windows-x64-*.zip 传到 Release 即可。
echo 想先本地试跑：dist\桌宠大肥鱼\桌宠大肥鱼.exe
start "" "dist\桌宠大肥鱼"
pause
