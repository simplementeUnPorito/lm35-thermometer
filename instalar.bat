@echo off
setlocal
cd /d "%~dp0" || exit /b 1
set "VPY=%~dp0.venv\Scripts\python.exe"
set "PYCMD="

if not exist "%VPY%" goto :detect
"%VPY%" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 goto :pip
echo El entorno .venv existente no es utilizable; se conservara una copia y se recreara.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0respaldar_entorno.ps1"
if errorlevel 1 goto :err_rm

:detect
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto :create
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto :create
echo ERROR: no se encontro Python 3.10 o superior.
echo Instale Python 3.10+ desde https://www.python.org/downloads/ y vuelva a ejecutar instalar.bat
exit /b 1

:create
echo Creando entorno virtual con: %PYCMD%
%PYCMD% -m venv ".venv"
if errorlevel 1 goto :err_venv
if not exist "%VPY%" goto :err_venv

:pip
"%VPY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :err_pip
echo Listo. Ejecute iniciar.bat
exit /b 0

:err_rm
echo ERROR: no se pudo respaldar el entorno .venv
exit /b 1
:err_venv
echo ERROR: no se pudo crear el entorno virtual .venv
exit /b 1
:err_pip
echo ERROR: fallo la instalacion de dependencias
exit /b 1
