@echo off
set GIT="C:\Program Files\Git\cmd\git.exe"
cd /d C:\Users\haide\Trade-Bot-D\Trade-Bot
%GIT% add -A
%GIT% status --short
%GIT% commit -m "chore: remove temporary commit helper script"
%GIT% push origin main
%GIT% log --oneline -6
