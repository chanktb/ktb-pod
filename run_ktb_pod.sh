#!/bin/bash
# ==============================================================================
# KỊCH BẢN CHẠY KTB POD (All-in-One Engine)
# Tự động quét → xử lý ảnh → đóng ZIP → kích hoạt upload nếu có ZIP mới
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONUTF8=1
export PYTHONPATH="$SCRIPT_DIR"
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/crash_log.txt"
ZIP_DIR="$SCRIPT_DIR/ktb-pod-output-zips"
UPLOAD_SCRIPT="$SCRIPT_DIR/run_upload.sh"

trap 'echo ""; echo "🛑 Tín hiệu Shutdown nhận được. Đã thoát hệ thống an toàn!"; exit 0' SIGINT

while true; do
    echo "================================================================="
    echo "🚀 KHỞI ĐỘNG KTB POD 🚀"
    echo "================================================================="
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Đang chạy Máy xúc tự động..."
    
    # Chạy crawler với error logging (stderr ghi vào log, stdout vẫn hiển thị)
    py main.py 2>> "$LOG_FILE"
    EXIT_CODE=$?
    
    if [ $EXIT_CODE -ne 0 ]; then
        echo "⚠️ [$(date +'%Y-%m-%d %H:%M:%S')] main.py thoát với mã lỗi: $EXIT_CODE" | tee -a "$LOG_FILE"
        echo "📋 Xem chi tiết lỗi tại: $LOG_FILE"
    fi
    
    # --- TỰ ĐỘNG KÍCH HOẠT UPLOAD NẾU CÓ ZIP MỚI ---
    zip_count=$(find "$ZIP_DIR" -maxdepth 1 -name "*.zip" 2>/dev/null | wc -l)
    if [ "$zip_count" -gt 0 ]; then
        echo ""
        echo "📦 Phát hiện $zip_count file ZIP mới! Kích hoạt Upload..."
        UPLOAD_DIR="$SCRIPT_DIR/ktb-upload"
        cd "$UPLOAD_DIR"
        python main.py 2>> "$LOG_FILE"
        UPLOAD_EXIT=$?
        cd "$SCRIPT_DIR"
        if [ $UPLOAD_EXIT -ne 0 ]; then
            echo "⚠️ [$(date +'%Y-%m-%d %H:%M:%S')] Upload thoát với mã lỗi: $UPLOAD_EXIT" | tee -a "$LOG_FILE"
        else
            echo "✅ Upload hoàn tất!"
        fi
    fi
    
    echo "================================================================="
    echo "🔄 Đang đồng bộ tự động lên GitHub..."
    echo "================================================================="
    git add .
    git commit -m "Auto-sync ktb-pod by Linux Script"
    git pull origin main --no-edit
    git push origin main
    
    echo "================================================================="
    echo "⏳ Hoàn tất vòng lặp! Sẽ tự động dò lại sau 15 phút..."
    echo "💻 Bấm [Ctrl + C] để thoát an toàn."
    echo "================================================================="
    sleep 900
done
