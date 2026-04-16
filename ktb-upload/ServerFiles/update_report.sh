#!/bin/bash
set -e
# File: /home/khue/update_report.sh

# --- THAY ĐỔI: Cấu hình user 'khue' ---
LOG_DIR="/home/khue/ktb_tmp_uploads/uploadlog"
# --- KẾT THÚC THAY ĐỔI ---

# --- Nhận tham số ---
if [ "$#" -ne 4 ]; then
    echo "Usage: $0 <wp_author> <wp_prefix> <image_count> <zip_filename>"
    exit 1
fi

WP_AUTHOR="$1"
WP_PREFIX="$2"
IMAGE_COUNT="$3"
ZIP_FILENAME="$4"
LOG_FILE="$LOG_DIR/upload.${WP_AUTHOR}.log"
TMP_LOG_FILE="${LOG_FILE}.tmp"

mkdir -p "$LOG_DIR"

# --- Lấy ngày giờ (QUAN TRỌNG: Đặt múi giờ VN) ---
CURRENT_DATE=$(TZ="Asia/Ho_Chi_Minh" date '+%Y-%m-%d')
CURRENT_TIMESTAMP=$(TZ="Asia/Ho_Chi_Minh" date '+%Y-%m-%d %H:%M:%S %z')

# ... (Logic đọc log cũ và ghi log mới giữ nguyên) ...
declare -a prefixes
declare -a totals  

LOG_DATE=""
if [ -f "$LOG_FILE" ]; then
    LOG_DATE=$(grep "^Timestamp:" "$LOG_FILE" | sed -n 's/^Timestamp: \([0-9-]*\).*/\1/p')

    if [ "$LOG_DATE" == "$CURRENT_DATE" ]; then
        while read -r line; do
            prefix=$(echo "$line" | awk '{print $1}' | sed 's/://')
            total=$(echo "$line" | awk '{print $NF}')
            if [ -n "$prefix" ]; then
                prefixes+=("$prefix") 
                totals+=("$total")   
            fi
        done < <(grep "Total" "$LOG_FILE")
    fi
fi

NEW_LOG_CONTENT="--- Summary of User Upload ---\n"
NEW_LOG_CONTENT+="Timestamp: $CURRENT_TIMESTAMP\n"
NEW_LOG_CONTENT+="User: $WP_AUTHOR\n\n"
NEW_LOG_CONTENT+="File: $ZIP_FILENAME\n\n"

found=0 
for i in "${!prefixes[@]}"; do
    prefix=${prefixes[$i]}
    old_total=${totals[$i]}
    
    if [ "$prefix" == "$WP_PREFIX" ]; then
        new_count=$IMAGE_COUNT
        current_total=$((old_total + new_count))
        found=1
    else
        new_count=0
        current_total=$old_total
    fi
    NEW_LOG_CONTENT+="$prefix: $new_count images: Total $current_total\n"
done

if [ $found -eq 0 ]; then
    NEW_LOG_CONTENT+="$WP_PREFIX: $IMAGE_COUNT images: Total $IMAGE_COUNT\n"
fi

NEW_LOG_CONTENT+="\nUpload thanh cong."

echo -e "$NEW_LOG_CONTENT" > "$TMP_LOG_FILE"
mv "$TMP_LOG_FILE" "$LOG_FILE"

echo -e "$NEW_LOG_CONTENT"