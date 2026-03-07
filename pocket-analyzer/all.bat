
@echo off
set PYTHONUTF8=1
cd /d "C:\Users\izumi\work\IT\Žï–¡\app\p-bandai-monitor\pocket-analyzer" || (
  echo cd Ž¸”s
  pause
  exit /b
)

set LOGFILE=execution_log.txt

echo ------------------------------------------ >> %LOGFILE%
echo [%date% %time%] START PROCESS >> %LOGFILE%

python yahuoku_search.py >> %LOGFILE% 2>&1
python check_profit.py >> %LOGFILE% 2>&1

echo [%date% %time%] END PROCESS >> %LOGFILE%
pause
