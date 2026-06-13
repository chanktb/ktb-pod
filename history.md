# HISTORY — Nhật Ký Thay Đổi

## 2026-06-09

### Cập nhật coord_bot.py — hỗ trợ multi-URL caption + thêm domain mới

#### Bối cảnh
Phát hiện site `thegiftio.uk` host ảnh trên CDN bên thứ 3 (`cloudfable.net`) thay vì domain chính.
Bot cũ chỉ nhận 1 URL trong caption và không detect được domain khi URL ảnh từ CDN khác domain store.

#### Thay đổi 1: Multi-URL caption + thêm domain mới tự động
File: `coord_bot.py`

**Luồng mới khi caption chứa nhiều URL:**
1. URL đầu tiên = ảnh gốc để template matching (không đổi)
2. Các URL tiếp theo → check từng domain xem đã có trong config chưa
3. Nếu có domain chưa tồn tại → bot hiển thị danh sách + nút "✅ Thêm domain mới"
4. Bot hỏi `source_type` cho từng domain mới (api / product-list / prevnext) qua inline keyboard
5. Sau khi thu thập đủ → thêm vào config.json + git push
6. Tiếp tục flow bình thường để cập nhật coords cho domain chính

**Cấu trúc domain mới được tạo tự động:**
```json
{
  "base_url": "https://newdomain.com/",
  "source_type": "<user chọn>",
  "max_days_old": 1,
  "rules": [{
    "pattern": "<infer từ filename URL>",
    "action": "generate",
    "mockup_sets_to_use": ["<kế thừa từ primary domain hoặc 'printiment'>"],
    "coords": { "<coords từ template matching>" }
  }]
}
```

**Pattern inference:** regex `(-[A-Za-z][A-Za-z0-9-]*\.[a-z]{2,5})$` lấy suffix `-producttype.ext` từ filename.

**Mockup sets:** kế thừa từ rule generate đầu tiên của primary domain; fallback `["printiment"]`.

**Helper functions mới:**
- `extract_urls_from_caption()` — regex tìm tất cả `https?://` URL
- `infer_pattern_from_url()` — suy ra pattern suffix từ filename
- `add_new_domain()` — ghi domain mới vào config.json
- `get_default_mockup_sets()` — lấy mockup_sets từ primary domain
- `_ask_source_type()` — hiển thị keyboard chọn source_type
- `_finish_add_new_domains()` — hoàn tất thêm domain + git push + tiếp tục flow

#### Thay đổi 2: image_domains — map CDN về store domain
File: `coord_bot.py` → `detect_domain_from_url()`

**Vấn đề:** `thegiftio.uk` dùng `cloudfable.net` làm CDN, URL ảnh không chứa domain store.

**Giải pháp:** Thêm field `image_domains` vào config domain:
```json
"thegiftio.uk": {
    "image_domains": ["cloudfable.net"],
    "base_url": "...",
    ...
}
```
Bot kiểm tra cả `image_domains` khi detect domain từ URL ảnh.
Hỗ trợ subdomain (e.g. `i10.cloudfable.net` match `cloudfable.net`).

#### Ghi chú về thegiftio.uk
- Platform: Shopify (URL pattern `/products/[slug]`)
- Site trả 403 cho mọi request tự động → chưa xác nhận product listing URL hoạt động
- Cần user verify URL listing trong browser trước khi thêm vào config
- Đề xuất `base_url`: `https://www.thegiftio.uk/collections/all` (product-list) hoặc `https://www.thegiftio.uk/` (api/products.json)

## 2026-05-19

### Upload retry
- 4 ZIPs bị kẹt trong `ktb-upload/Processing/` từ ngày 18/05 đã được upload thủ công:
  - `whatwillwear.dalatshirt.20260518_210326.1.zip`
  - `whatwillwear.dalatshirt.20260519_084420.1.zip`
  - `whatwillwear.designatshop.20260518_210406.135.zip`
  - `whatwillwear.designatshop.20260519_084454.135.zip`
- Lệnh: `cd ktb-upload && python3 main.py`
- SSH key fix: `chmod 600 ktb-upload/id_ed25519` (permissions sai → sửa 600)

### Xóa domain chết khỏi config.json
- Xóa `arjomany.com` — không kết nối được
- Xóa `newshirtstore.com` — không kết nối được
- Tổng domains còn lại: 25

### Health check (02:56)
- 0 domain chết
- 10 domain chưa có url-list (chưa quét trên máy này)
- eteeshirts.com báo crop lỗi do bug trong health_check.py (dùng coords_white cho ảnh đen)
  → Pipeline thực (main.py) hoạt động đúng

### STATUS.md cập nhật
- Phản ánh kết quả health check mới nhất
- Ghi chú eteeshirts.com là false alarm của health_check

### Fix coords eteeshirts.com
- `-shirt.webp`: xóa `coords_white` + `coords_black` + `color_sample_coords`
- Thay bằng `coords: { x:346, y:803, w:315, h:388 }`
- Lý do: toàn bộ 370 URL trong url-list đều là áo đen, coords_white không bao giờ được dùng
  nhưng health_check luôn pick coords_white trước → false alarm variance=86.1
- Sau fix: health check ✅ variance=11.0
