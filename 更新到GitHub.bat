@echo off
cd /d "%~dp0"
set GITURL=https://github.com/nxxx666/dsh-chat-mp3-pet

echo ============================================
echo  1/3  检查本地改动
echo ============================================
git status --short
echo.

echo ============================================
echo  2/3  提交
echo ============================================
set "MSG="
set /p MSG=请输入本次更新说明（直接回车用默认）: 
if "%MSG%"=="" set "MSG=更新桌宠功能"
git add -A
git commit -m "%MSG%"
if errorlevel 1 echo [提示] 没有新改动需要提交，直接推送已有提交。

echo.
echo ============================================
echo  3/3  推送到 GitHub
echo ============================================
git push origin main
if errorlevel 1 (
    echo.
    echo [失败] 推送没成功。常见原因：
    echo   1. 网络/代理不通 —— 关掉代理或换个网络再试
    echo   2. 没登录 —— 打开 GitHub Desktop 登录一次，或在弹窗里登录
    echo   3. 仓库还没建 —— 先去 %GITURL% 建好空仓库
    echo.
    pause
    exit /b 1
)

echo.
echo [成功] 代码已同步到 GitHub。
echo.
echo 如果这次也改了程序本身，还需要更新 Release 压缩包：
echo   1. 双击「打包Release.bat」生成新 zip
echo   2. 打开 Release 页面，编辑最新版本，替换那个 zip 资产
start "" "%GITURL%/releases"
pause
