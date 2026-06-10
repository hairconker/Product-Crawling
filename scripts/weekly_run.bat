@echo off
REM 阶段 13：Windows 计划任务入口
REM 用法：Windows 任务计划程序 → 创建任务 → 操作 → 启动程序 → 此 .bat
REM
REM 前置：
REM   pip install playwright pyyaml pydantic loguru
REM   python -m playwright install chromium
REM   python scripts\login_helper.py jd
REM 建议环境：Windows 家宽（非 WSL/机房 IP，避免 JD 软拦截）

setlocal
cd /d "%~dp0.."

REM 先跑字典三步 + 热门品类价格采集
python scripts\weekly_run.py %*
set EXITCODE=%ERRORLEVEL%

REM 每周日归档（Codex 审：必须把归档失败也传回 Task Scheduler）
for /f %%i in ('powershell -Command "(Get-Date).DayOfWeek.value__"') do set DAY=%%i
if "%DAY%"=="0" (
    echo [weekly.bat] Sunday: rolling archive
    python scripts\archive.py --keep-weeks 12
    if ERRORLEVEL 1 set EXITCODE=%ERRORLEVEL%
)

exit /b %EXITCODE%
