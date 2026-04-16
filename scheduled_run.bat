@echo off
REM =================================================================
REM  KTB POD - Scheduled Task Runner (loop 30 phut)
REM  Tuong duong run_ktb_pod.sh nhung chay tren Windows
REM =================================================================
chcp 65001 >nul
set PYTHONUTF8=1
setlocal EnableDelayedExpansion

REM Di chuyen vao thu muc chua script
cd /d "%~dp0"

echo =================================================================
echo   KTB POD - DANG CHAY (Loop moi 30 phut, Ctrl+C de dung)
echo =================================================================

:LOOP
echo =================================================================
echo [%date% %time%] === BAT DAU VONG LAP KTB POD ===
echo =================================================================

python main.py 2>> crash_log.txt
set EXIT_CODE=!ERRORLEVEL!

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] main.py thoat voi ma loi: !EXIT_CODE! >> crash_log.txt
)

REM --- Tu dong upload neu co ZIP moi (giong run_ktb_pod.sh dong 33-48) ---
set ZIP_COUNT=0
for %%f in ("ktb-pod-output-zips\*.zip") do set /a ZIP_COUNT+=1

echo [%date% %time%] Tim thay !ZIP_COUNT! file ZIP trong ktb-pod-output-zips

if !ZIP_COUNT! GTR 0 (
    echo.
    echo [%date% %time%] Phat hien !ZIP_COUNT! file ZIP! Kich hoat Upload...
    cd /d "%~dp0ktb-upload"
    python main.py 2>> "%~dp0crash_log.txt"
    set UPLOAD_EXIT=!ERRORLEVEL!
    cd /d "%~dp0"
    if !UPLOAD_EXIT! NEQ 0 (
        echo [%date% %time%] Upload thoat voi ma loi: !UPLOAD_EXIT! >> crash_log.txt
    ) else (
        echo [%date% %time%] Upload hoan tat!
    )
) else (
    echo [%date% %time%] Khong co file ZIP. Bo qua Upload.
)

echo =================================================================
echo [%date% %time%] Auto pushing to GitHub...
echo =================================================================
git add .
git commit -m "Auto-sync ktb-pod by Windows Task Scheduler"
git pull origin main --no-edit
git push origin main

echo =================================================================
echo [%date% %time%] Hoan tat! Se chay lai sau 30 phut...
echo Bam Ctrl+C de thoat.
echo =================================================================

REM --- Doi 30 phut (1800 giay) ---
timeout /t 1800 /nobreak >nul

goto LOOP
