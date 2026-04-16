#!/bin/bash
# File Scheduler, đặt tại: /home/khue/process_queue.sh

# --- THAY ĐỔI: Cấu hình user 'khue' ---
QUEUE_DIR="/home/khue/ktb_tmp_uploads"
FAILED_DIR="/home/khue/ktb_tmp_uploads/failed"
IMPORT_SCRIPT="/home/khue/ktbimport" # Đường dẫn đến Worker
LOG_FILE="/home/khue/worker.log"      
LOCKFILE="/home/khue/ktb_tmp_uploads/ktb_queue.lock"       
# --- KẾT THÚC THAY ĐỔI ---

# Tạo thư mục 'failed' nếu chưa có
mkdir -p "$FAILED_DIR"

# --- Hàm ghi log ---
log_msg() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# --- Logic Khóa (Dùng flock) ---
(
    flock -n 200 || { log_msg "Worker da dang chay. Bo qua lan nay."; exit 1; }
    
    log_msg "--- Bat dau quet hang doi ---"
    
    # Tìm MỘT thư mục job CŨ NHẤT (bỏ qua 'failed', 'uploadlog', và 'tmp_*')
    JOB_DIR=$(find "$QUEUE_DIR" -mindepth 1 -maxdepth 1 -type d -not -name "failed" -not -name "uploadlog" -not -name "tmp_*" | sort | head -n 1)
    
    if [ -z "$JOB_DIR" ]; then
        log_msg "Hang doi rong. Ket thuc."
        exit 0
    fi
    
    log_msg "Phat hien Job: $(basename "$JOB_DIR")"
    
    # --- Xử lý Meta file (tên cố định là meta.json) ---
    META_FILE="$JOB_DIR/meta.json"
    
    if [ ! -f "$META_FILE" ]; then
        log_msg "LOI: Khong tim thay $META_FILE. Di chuyen Job vao '$FAILED_DIR'."
        mv "$JOB_DIR" "$FAILED_DIR/"
        exit 1
    fi
    
    # --- THAY ĐỔI: Đọc thêm 2 trường Telegram ---
    WP_AUTHOR=$(jq -r '.wp_author' "$META_FILE")
    WP_PATH=$(jq -r '.wp_path' "$META_FILE")
    ZIP_FILENAME=$(jq -r '.zip_filename' "$META_FILE")
    WP_PREFIX=$(jq -r '.prefix' "$META_FILE")
    TELEGRAM_BOT_TOKEN=$(jq -r '.telegram_bot_token' "$META_FILE")
    TELEGRAM_CHAT_ID=$(jq -r '.telegram_chat_id' "$META_FILE")
    # --- KẾT THÚC THAY ĐỔI ---

    # --- THAY ĐỔI: Kiểm tra 2 trường Telegram ---
	if [ -z "$WP_AUTHOR" ] || [ -z "$WP_PATH" ] || [ -z "$ZIP_FILENAME" ] || [ -z "$WP_PREFIX" ] || [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ] || \
       [ "$WP_AUTHOR" = "null" ] || [ "$WP_PATH" = "null" ] || [ "$ZIP_FILENAME" = "null" ] || [ "$WP_PREFIX" = "null" ] || [ "$TELEGRAM_BOT_TOKEN" = "null" ] || [ "$TELEGRAM_CHAT_ID" = "null" ]; then
		 log_msg "LOI: file meta.json bi loi hoac thieu truong (bao gom ca truong telegram). Di chuyen Job vao '$FAILED_DIR'."
		 mv "$JOB_DIR" "$FAILED_DIR/"
		 exit 1
	fi
    # --- KẾT THÚC THAY ĐỔI ---
    
    # Đường dẫn đầy đủ đến file zip
    ZIP_FILE_PATH="$JOB_DIR/$ZIP_FILENAME"
    
    if [ ! -f "$ZIP_FILE_PATH" ]; then
        log_msg "LOI: Tim thay meta.json nhung khong tim thay $ZIP_FILENAME. Di chuyen Job vao '$FAILED_DIR'."
        mv "$JOB_DIR" "$FAILED_DIR/"
        exit 1
    fi

    log_msg "Thong tin: Author=$WP_AUTHOR, Path=$WP_PATH, File=$ZIP_FILENAME, ChatID=$TELEGRAM_CHAT_ID"
    
    # --- THAY ĐỔI: Kích hoạt Worker (ktb-import) với 7 tham số ---
    bash "$IMPORT_SCRIPT" "$ZIP_FILE_PATH" "$WP_AUTHOR" "$WP_PATH" "$WP_PREFIX" "$ZIP_FILENAME" "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" >> "$LOG_FILE" 2>&1
    # --- KẾT THÚC THAY ĐỔI ---
    
    log_msg "Xu ly xong Job. Don dep thu muc: $(basename "$JOB_DIR")"
    rm -rf "$JOB_DIR"
    
    log_msg "--- Ket thuc xu ly ---"
    
) 200>"$LOCKFILE"
