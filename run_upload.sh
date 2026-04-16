#!/bin/bash

# ==============================================================================
# KỊCH BẢN UPLOAD FILE ZIP LÊN VPS
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ZIP_SOURCE_DIR="$SCRIPT_DIR/ktb-pod-output-zips"
UPLOAD_DIR="$SCRIPT_DIR/ktb-upload"

WAIT_ON_SUCCESS=120  # 2 phút
WAIT_ON_IDLE=120     # 2 phút

trap 'echo ""; echo "🛑 Đã thoát an toàn!"; exit 0' SIGINT

echo "🚀 BẮT ĐẦU DỊCH VỤ UPLOAD..."

while true; do
  echo "================================================================"
  echo "🔎 [$(date)] Kiểm tra thư mục '$ZIP_SOURCE_DIR'..."

  zip_file_found=$(find "$ZIP_SOURCE_DIR" -maxdepth 1 -name "*.zip" -print -quit 2>/dev/null)

  if [ -n "$zip_file_found" ]; then
    echo "👍 Tìm thấy file ZIP. Bắt đầu quá trình upload."
    cd "$UPLOAD_DIR"
    
    python main.py
    
    echo "✅ Hoàn thành chu kỳ Upload. Sẽ kiểm tra lại sau 2 phút."
    cd "$SCRIPT_DIR"
    sleep $WAIT_ON_SUCCESS
  else
    echo "☕ Không tìm thấy file ZIP nào. Sẽ kiểm tra lại sau 2 phút."
    sleep $WAIT_ON_IDLE
  fi
done