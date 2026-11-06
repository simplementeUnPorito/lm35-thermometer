@echo off
setlocal
cd /d "%~dp0" || exit /b 1
set "VPY=%~dp0.venv\Scripts\python.exe"

if not exist "%VPY%" goto :install
"%VPY%" -c "import sys, serial, matplotlib, numpy; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 goto :run

:install
echo Entorno no listo; ejecutando instalar.bat...
call "%~dp0instalar.bat"
if errorlevel 1 goto :fail
"%VPY%" -c "import sys, serial, matplotlib, numpy; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :fail

:run
"%VPY%" -m lm35 gui %*
exit /b %errorlevel%

:fail
echo ERROR: no se pudo preparar el entorno.
exit /b 1
