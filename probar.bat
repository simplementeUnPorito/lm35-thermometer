@echo off
cd /d "%~dp0"
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
%PY% -m unittest discover -s tests -t . && %PY% -m lm35 demo --seconds 4 --inject-faults
