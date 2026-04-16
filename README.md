# ktb-pod — Hệ Thống Tự Động Hóa Print-on-Demand

## Mô tả

Hệ thống tự động quét các website bán áo thun (WordPress), tìm sản phẩm mới, cắt lấy thiết kế từ ảnh mockup gốc, tách nền, ghép vào mockup riêng, đóng gói thành file ZIP, và tự động upload lên VPS — tất cả chạy hoàn toàn tự động bằng 1 script duy nhất.

## Luồng hoạt động

```
Quét website (API / HTML / PrevNext)
        │
        ▼
Tải ảnh sản phẩm về (temp)
        │
        ▼
Khớp URL với "rules" trong config → lấy tọa độ cắt
        │
        ▼
Phát hiện nền trắng hay đen (pixel sampling)
        │
        ▼
Cắt vùng thiết kế bằng OpenCV + kiểm tra viền (edge variance ≤ 25 = OK)
        │
        ▼
Tách nền thiết kế (Magic Wand AI) → xoay → trim alpha
        │
        ▼
Ghép lên mockup riêng (chọn ngẫu nhiên từ pool) + watermark + EXIF
        │
        ▼
Nén vào file ZIP theo domain → ktb-pod-output-zips/
        │
        ▼
Cập nhật url-lists/ + downloaded_history.txt → push lên GitHub (amend)
        │
        ▼
Tự động kích hoạt upload ZIP lên VPS (nếu có ZIP mới)
```

Hệ thống chạy **20 luồng đồng thời**, mỗi luồng xử lý 1 domain từ đầu đến cuối.

## Khởi chạy

```bash
# Cài đặt dependencies
pip install -r requirements.txt

# Chạy hệ thống (quét + upload + push, lặp lại mỗi 15 phút)
bash run_ktb_pod.sh

# Hoặc chạy riêng upload
bash run_upload.sh
```

## Ý nghĩa từng file và folder

### Files gốc

| File | Ý nghĩa |
|------|---------|
| `main.py` | Script chính — chứa toàn bộ logic: 3 engine quét web, pipeline cắt-tách nền-ghép mockup, đóng ZIP, thống kê domain, cập nhật url-lists, quản lý history, push GitHub, gửi báo cáo Telegram. Có crash protection ghi log lỗi vào `crash_log.txt` |
| `config.json` | File cấu hình duy nhất — chứa 3 phần: `defaults` (cài đặt chung), `mockup_sets` (danh sách mockup trắng/đen), `domains` (cấu hình từng website + rules cắt ảnh) |
| `run_ktb_pod.sh` | Script bash chính — chạy `main.py` trong vòng lặp mỗi 15 phút, tự động kích hoạt upload nếu có ZIP mới, ghi stderr vào `crash_log.txt` |
| `run_upload.sh` | Script bash chạy module upload riêng (chạy standalone nếu cần) |
| `requirements.txt` | Danh sách tất cả Python dependencies cần thiết |
| `.gitignore` | Loại trừ các folder nặng/tạm khỏi git |
| `downloaded_history.txt` | Lưu URL/Product URL đã xử lý (URL mới nhất ở đầu file, tối đa 5,000 dòng). Được nạp vào RAM khi khởi động để kiểm tra trùng lặp O(1) |
| `error_report.txt` | Lưu URL ảnh bị lỗi cắt để review thủ công |
| `crash_log.txt` | Log lỗi crash với timestamp + traceback đầy đủ |

### Folders

| Folder | Ý nghĩa |
|--------|---------|
| `utils/` | Thư viện tiện ích cốt lõi. `image_processing.py`: tách nền AI, ghép mockup, watermark, cắt tọa độ, xoay ảnh, phát hiện màu nền. `file_io.py`: làm sạch tên file, tạo EXIF metadata, tìm file mockup. `crop_validator.py`: cắt ảnh bằng OpenCV và kiểm tra phương sai viền. `telegram_bot.py`: gửi cảnh báo/báo cáo qua Telegram Bot |
| `mockup/` | Chứa ảnh nền mockup áo thun (trắng/đen). Mỗi mockup set có thể có nhiều phiên bản, hệ thống chọn ngẫu nhiên 1 cái mỗi lần ghép |
| `watermark/` | Chứa ảnh watermark để chèn góc ảnh thành phẩm |
| `fonts/` | Chứa font chữ (`verdanab.ttf`) dùng cho text watermark |
| `ktb-pod-output-zips/` | **Thư mục output** — chứa file ZIP thành phẩm. Tên file: `{mockup_set}.{domain}.{YYYYMMDD_HHMMSS}.{số ảnh}.zip` |
| `url-lists/` | Chứa file `domain.com.txt` cho mỗi domain (tối đa 500 URL, URL mới nằm trên cùng). Tự động push lên GitHub |
| `temp_downloads/` | Thư mục tạm chứa ảnh đang tải. Tự động dọn sạch sau mỗi phiên |
| `ktb-upload/` | Module upload sản phẩm lên VPS qua SSH/SFTP |

## 3 Engine quét web

| Engine | Khi nào dùng | Cách hoạt động |
|--------|-------------|----------------|
| **API** (`source_type: "api"`) | Website WordPress có REST API | Gọi `/wp-json/wp/v2/product`, đọc JSON, lấy ảnh từ content HTML hoặc og:image |
| **Product List** (`source_type: "product-list"`) | Trang danh mục sản phẩm | Vào `base_url`, dùng CSS selector lấy link từng sản phẩm, rồi vào từng trang tìm ảnh |
| **PrevNext** (`source_type: "prevnext"`) | Trang có nút "Sản phẩm tiếp theo" | Vào sản phẩm đầu tiên, bấm nút Next liên tục để duyệt tuần tự |

## Cơ chế chống trùng lặp

- **API**: Gặp URL đã có trong history → bỏ qua. Gặp ảnh quá cũ (`Last-Modified` > `max_days_old`) → dừng pagination
- **Product List**: Gặp Product URL đã có trong history → bỏ qua, tiếp tục
- **PrevNext**: Gặp Product URL đã có trong history → dừng luôn (tất cả phía sau đều cũ hơn)
- **History file**: Giới hạn tối đa 5,000 dòng (URL mới nhất lên đầu, cắt URL cũ nhất)

## Cấu trúc rules trong config

Mỗi domain có mảng `rules`, mỗi rule khớp theo **đuôi URL** (`pattern`):

- `"action": "generate"` → xử lý bình thường (cắt + ghép mockup)
- `"action": "skip"` → bỏ qua loại ảnh này
- `"coords"` → tọa độ cắt `{x, y, w, h}` (pixel, góc trên-trái)
- `"coords_white"` / `"coords_black"` → tọa độ khác nhau tùy nền trắng/đen
- `"color_sample_coords"` → vùng lấy mẫu màu để xác định nền trắng hay đen
- `"mockup_sets_to_use"` → danh sách mockup set để ghép
- `"angle"` → góc xoay design trước khi ghép
- `"skipWhite"` / `"skipBlack"` → bỏ qua nếu detect ra nền trắng/đen

## Chống crash

**Python (`main.py`):**
- Bọc toàn bộ `main()` trong `try/except` — crash không bao giờ làm script tự tắt
- Log lỗi crash vào `crash_log.txt` kèm timestamp + traceback đầy đủ
- Thoát mã lỗi `1` để shell script biết

**Shell (`run_ktb_pod.sh`):**
- Chạy trong `while true` — dù Python crash, script vẫn chờ 15 phút rồi chạy lại
- `/dev/stderr` của Python được ghi vào `crash_log.txt`
- Kiểm tra exit code sau mỗi lần chạy và in cảnh báo

## Thống kê & Báo cáo

- Sau mỗi lượt quét: in bảng thống kê URL mới theo từng domain (chỉ hiển thị domain có URL mới)
- Cuối mỗi phiên chạy: gửi bảng thống kê qua Telegram
- Nếu 1 domain có ≥ 50% ảnh lỗi (trên tổng ≥ 3): gửi cảnh báo đỏ

## Tích hợp GitHub

- Sau mỗi lượt quét có URL mới:
  1. Cập nhật `url-lists/*.txt` (URL mới dán lên đầu, max 500 dòng mỗi domain)
  2. Sắp xếp lại `downloaded_history.txt` (URL mới lên đầu, max 5,000 dòng)
  3. `git add` → `git commit --amend` → `git push --force`
- Yêu cầu: Đã `git init` + `git remote add origin <url>` + commit đầu tiên
