import os
import json
import requests
import shutil
import uuid
import re
import signal
import sys
import subprocess
import concurrent.futures
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from datetime import datetime, timedelta, timezone
import pytz
from dateutil import parser
import zipfile
from io import BytesIO
from PIL import Image

# =========================================================================
# SYSTEM DYNAMICS & SHUTDOWN CONFIG
# =========================================================================
shutdown_flag = False

def signal_handler(sig, frame):
    global shutdown_flag
    print("\n⏳ Đã nhận lệnh dừng (Ctrl+C). Đang đóng các luồng an toàn. Vui lòng đợi...")
    shutdown_flag = True

signal.signal(signal.SIGINT, signal_handler)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from utils.telegram_bot import send_alert
from utils.crop_validator import process_image
from utils.image_processing import (
    remove_background_advanced,
    trim_transparent_background,
    apply_mockup,
    add_watermark,
    determine_color_from_sample_area,
    rotate_image,
    crop_by_coords
)
from utils.file_io import (
    clean_title,
    pre_clean_filename,
    create_exif_data,
    find_mockup_image
)

# =========================================================================
# PATH CONFIGURATION
# =========================================================================
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMP_DIR = os.path.join(BASE_DIR, "temp_downloads")
MOCKUP_DIR = os.path.join(BASE_DIR, "mockup")
WATERMARK_DIR = os.path.join(BASE_DIR, "watermark")
FONT_FILE = os.path.join(BASE_DIR, "fonts", "verdanab.ttf")
OUTPUT_ZIP_DIR = os.path.join(BASE_DIR, "ktb-pod-output-zips")
URL_LISTS_DIR = os.path.join(BASE_DIR, "url-lists")

HISTORY_FILE = os.path.join(BASE_DIR, "downloaded_history.txt")
ERROR_LOG_FILE = os.path.join(BASE_DIR, "error_report.txt")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
MAX_HISTORY_LINES = 5000
MAX_URL_LIST_LINES = 500

for d in [TEMP_DIR, OUTPUT_ZIP_DIR, URL_LISTS_DIR]: 
    os.makedirs(d, exist_ok=True)

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f: 
        config_doc = json.load(f)
    TITLE_CLEAN_KEYWORDS = config_doc.get("defaults", {}).get("title_clean_keywords", [])
    EXIF_DEFAULTS = config_doc.get("defaults", {}).get("exif_defaults", {})
    OUTPUT_FORMAT = config_doc.get("defaults", {}).get("global_output_format", "webp")
    MOCKUP_SETS_CONFIG = config_doc.get("mockup_sets", {})
except Exception as e:
    print(f"Lỗi đọc config: {e}"); raise

processed_history = set()
if os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        for l in f:
            if l.strip(): processed_history.add(l.strip())

# Lock cho việc ghi history file (thread-safe)
import threading
_history_lock = threading.Lock()
_session_new_urls = []  # Thu thập URL mới trong session này

def mark_as_processed(unique_id):
    processed_history.add(unique_id)
    with _history_lock:
        _session_new_urls.append(unique_id)
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f: f.write(unique_id + "\n")

def log_error(url):
    with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as f: f.write(f"{url}\n")

# =========================================================================
# SCRAPING ENGINES
# =========================================================================

def check_image_recent(url, max_days):
    if not url: return False
    try:
        r = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if r.status_code != 200: return False
        last_mod = r.headers.get("Last-Modified")
        if last_mod:
            mod_date = parser.parse(last_mod)
            if mod_date.tzinfo is None: mod_date = mod_date.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - mod_date) > timedelta(days=max_days): return False
        return True
    except: return False

def get_prioritized_patterns(domain_config):
    patterns = set()
    replacements = domain_config.get("replacements", {})
    if isinstance(replacements, list):
        patterns.update(replacements)
    elif isinstance(replacements, dict):
        patterns.update(replacements.keys())
    for r in domain_config.get("rules", []):
        pat = r.get("pattern")
        if pat: patterns.add(pat)
    return patterns

def extract_image_from_wp_api(item, domain_config):
    patterns = get_prioritized_patterns(domain_config)
    html = item.get('content', {}).get('rendered', '')
    if html:
        soup = BeautifulSoup(html, 'html.parser')
        all_imgs = soup.find_all('img')
        if patterns:
            for img in all_imgs:
                src = img.get('src')
                if src and any(p in src for p in patterns): return src
    og_url = item.get('yoast_head_json', {}).get('og_image', [{}])[0].get('url')
    if og_url: return og_url
    if html and all_imgs: return all_imgs[0].get('src')
    return None

def find_best_image_on_product_page(product_url, domain_config):
    try:
        r = requests.get(product_url, headers=HEADERS, timeout=20)
        if r.status_code != 200: return None
        soup = BeautifulSoup(r.text, "html.parser")
        patterns = get_prioritized_patterns(domain_config)
        all_imgs = soup.find_all('img')
        if patterns:
            for img in all_imgs:
                src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
                if src and any(p in src for p in patterns): return src
        img_sel = domain_config.get("image_url_selector")
        if img_sel:
            imgs = soup.select(img_sel)
            if imgs: return imgs[0].get('src') or imgs[0].get('data-src') or imgs[0].get('data-lazy-src')
        og = soup.find('meta', property='og:image')
        if og and og.get('content'): return og.get('content')
        for img in all_imgs:
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
            if src: return src
    except: pass
    return None

def scrape_api(domain_config, domain_netloc):
    items_to_process = []
    page = 1
    max_pages = domain_config.get("max_api_pages", 5)
    max_days = domain_config.get("max_days_old", 1)
    
    while page <= max_pages and not shutdown_flag:
        api_url = f"https://{domain_netloc}/wp-json/wp/v2/product?per_page=100&page={page}&orderby=date&order=desc"
        try:
            r = requests.get(api_url, headers=HEADERS, timeout=20)
            if r.status_code != 200: break
            products = r.json()
            if not products or not isinstance(products, list): break
            
            for p in products:
                img_url = extract_image_from_wp_api(p, domain_config)
                if not img_url: continue
                unique_id = img_url 
                if unique_id in processed_history: continue
                if domain_config.get("check_recency", True):
                    if not check_image_recent(img_url, max_days): return items_to_process
                items_to_process.append({"id": unique_id, "image_url": img_url})
            page += 1
        except: break
    return items_to_process

def scrape_html_list(domain_config, domain_netloc):
    items_to_process = []
    base_url = domain_config.get("base_url", f"https://{domain_netloc}/shop")
    product_selector = domain_config.get("product_url_selector", "a.woocommerce-LoopProduct-link")
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            for node in soup.select(product_selector):
                if shutdown_flag: break
                product_url = node.get('href')
                if not product_url: continue
                product_url = urljoin(base_url, product_url)
                unique_id = product_url
                if unique_id in processed_history: continue
                img_url = find_best_image_on_product_page(product_url, domain_config)
                if img_url: items_to_process.append({"id": unique_id, "image_url": img_url})
    except: pass
    return items_to_process

def scrape_prevnext(domain_config, domain_netloc):
    items_to_process = []
    base_url = domain_config.get("base_url", f"https://{domain_netloc}/shop")
    first_sel = domain_config.get("first_product_selector", ".product-small a.woocommerce-LoopProduct-link")
    next_sel = domain_config.get("next_product_selector", "a:has(i.icon-angle-right)")
    max_items = 100
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=20)
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.text, "html.parser")
        first_a = soup.select_one(first_sel)
        if not first_a or not first_a.get('href'): return []
        current_url = urljoin(base_url, first_a.get('href'))
        count = 0
        while current_url and count < max_items and not shutdown_flag:
            unique_id = current_url
            if unique_id in processed_history: break
            r2 = requests.get(current_url, headers=HEADERS, timeout=20)
            if r2.status_code != 200: break
            img_url = find_best_image_on_product_page(current_url, domain_config)
            if img_url: items_to_process.append({"id": unique_id, "image_url": img_url})
            soup2 = BeautifulSoup(r2.text, "html.parser")
            next_tag = soup2.select_one(next_sel)
            if not next_tag or not next_tag.get('href'): break
            current_url = urljoin(current_url, next_tag.get('href'))
            count += 1
    except: pass
    return items_to_process

# =========================================================================
# THE ULTIMATE FULL-STACK PIPELINE (CRAWL -> REMBG -> PIL MOCKUP -> ZIP)
# =========================================================================

def execute_pipeline(domain, item, domain_config, images_for_domain):
    unique_id = item["id"]
    original_img_url = item["image_url"] 
    url_to_fetch = original_img_url.split("?")[0]
    
    rules = domain_config.get("rules", [])
    matched_rule = None
    for rule in rules:
        pat = rule.get("pattern", "")
        if pat and url_to_fetch.endswith(pat): matched_rule = rule; break
        elif pat == "": matched_rule = rule; break
        
    temp_filepath = os.path.join(TEMP_DIR, f"temp_{uuid.uuid4().hex}.jpg")
    try:
        r = requests.get(url_to_fetch, stream=True, timeout=10)
        r.raise_for_status()
        with open(temp_filepath, 'wb') as f:
            for chunk in r.iter_content(8192): f.write(chunk)
    except: return "DOWNLOAD_ERROR", original_img_url
        
    raw_name = url_to_fetch.split('/')[-1].split('.')[0]
    
    if matched_rule and matched_rule.get("action", "") == "skip":
        os.remove(temp_filepath)
        mark_as_processed(unique_id)
        return "SKIPPED_RULE", original_img_url

    # --- 1. PHÁT HIỆN MÀU TRẮNG HAY ĐEN CỦA NỀN MOCKUP GỐC ---
    is_white = True
    try:
        img = Image.open(temp_filepath).convert("RGBA")
        sample_coords = matched_rule.get("color_sample_coords") if matched_rule else None
        if sample_coords:
            is_white = determine_color_from_sample_area(img, sample_coords)
        else:
            rect_coords_for_color = matched_rule.get("coords") if matched_rule else None
            if rect_coords_for_color:
                temp_crop = crop_by_coords(img, rect_coords_for_color)
                if temp_crop:
                    try:
                        pixel = temp_crop.getpixel((1, temp_crop.height - 2))
                        is_white = sum(pixel[:3]) / 3 > 128
                    except: pass
        img.close()
    except: pass

    # --- 2. XÁC ĐỊNH TỌA ĐỘ CẮT VÀ VALIDATION OPENCV CHỐNG LỆCH ---
    rect_coords = None
    if matched_rule:
        if is_white and "coords_white" in matched_rule: rect_coords = matched_rule["coords_white"]
        elif not is_white and "coords_black" in matched_rule: rect_coords = matched_rule["coords_black"]
        else: rect_coords = matched_rule.get("coords")
        
    if not rect_coords:
        os.remove(temp_filepath)
        mark_as_processed(unique_id)
        log_error(original_img_url)
        return "ERROR_UNKNOWN", original_img_url
        
    is_safe, status, var_score = process_image(temp_filepath, rect_coords)
    if not is_safe:
        os.remove(temp_filepath)
        log_error(original_img_url)
        mark_as_processed(unique_id) 
        return "ERROR_MOCKUP", original_img_url

    # --- 3. TIẾN HÀNH TÁCH NỀN VÀ GHÉP VÀO MỘT/NHIỀU MOCKUPS ---
    try:
        initial_crop = Image.open(temp_filepath).convert("RGBA")
        
        if matched_rule:
            if (matched_rule.get("skipWhite") and is_white) or (matched_rule.get("skipBlack") and not is_white):
                os.remove(temp_filepath)
                mark_as_processed(unique_id)
                return "SKIPPED_RULE", original_img_url

        angle = matched_rule.get("angle", 0) if matched_rule else 0
        bg_removed = remove_background_advanced(initial_crop)
        final_design = rotate_image(bg_removed, angle)
        trimmed_img = trim_transparent_background(final_design)
        
        if not trimmed_img:
            os.remove(temp_filepath)
            return "ERROR_UNKNOWN", original_img_url
            
        mockup_names_to_use = matched_rule.get("mockup_sets_to_use", []) if matched_rule else []
        if not mockup_names_to_use:
            os.remove(temp_filepath)
            mark_as_processed(unique_id)
            return "SKIPPED_RULE", original_img_url
            
        for mockup_name in mockup_names_to_use:
            mockup_config = MOCKUP_SETS_CONFIG.get(mockup_name)
            if not mockup_config: continue
            
            mockup_path, mockup_coords = find_mockup_image(MOCKUP_DIR, mockup_config, is_white)
            if not mockup_path or not mockup_coords: continue
            
            with Image.open(mockup_path) as mockup_img:
                final_mockup = apply_mockup(trimmed_img, mockup_img, mockup_coords)
                watermark_desc = mockup_config.get("watermark_text")
                final_mockup_with_wm = add_watermark(final_mockup, watermark_desc, WATERMARK_DIR, FONT_FILE)
                
            pre_clean_pattern = matched_rule.get("pre_clean_regex") if matched_rule else None
            base_filename = pre_clean_filename(raw_name, pre_clean_pattern)
            cleaned_title = clean_title(base_filename, TITLE_CLEAN_KEYWORDS)
            
            prefix = mockup_config.get("title_prefix_to_add", "")
            suffix = mockup_config.get("title_suffix_to_add", "")
            final_filename_base = f"{prefix} {cleaned_title} {suffix}".strip().replace('  ', ' ')
            
            ext = f".{OUTPUT_FORMAT}"
            if len(final_filename_base) + len(ext) > 120:
                final_filename_base = final_filename_base[:120-len(ext)]
            
            final_filename = f"{final_filename_base}{ext}"
            image_to_save = final_mockup_with_wm.convert('RGB')
            exif_bytes = create_exif_data(mockup_name, final_filename, EXIF_DEFAULTS)
            
            img_byte_arr = BytesIO()
            save_format = "WEBP" if OUTPUT_FORMAT == "webp" else "JPEG"
            image_to_save.save(img_byte_arr, format=save_format, quality=90, exif=exif_bytes)
            
            images_for_domain.setdefault(mockup_name, []).append((final_filename, img_byte_arr.getvalue()))
            
        os.remove(temp_filepath)
        mark_as_processed(unique_id)
        print(f"✅ [MOCKUP GEN] Thành công URL của {domain}: {raw_name} (S: {var_score:.1f})")
        return "PASS", original_img_url
        
    except Exception as e:
        print(f"Lỗi ghép Mockup: {e}")
        try: os.remove(temp_filepath)
        except: pass
        return "ERROR_UNKNOWN", original_img_url

def process_domain(domain):
    if shutdown_flag: return domain, 0, 0, []
    domain_config = config_doc.get("domains", {}).get(domain, {})
    source_type = domain_config.get("source_type", "api")
    print(f"-> [Worker] Đang cào {domain} qua Engine: {source_type.upper()}")
    
    items = []
    if source_type in ["api", "api-attachment"]: items = scrape_api(domain_config, domain)
    elif source_type == "product-list": items = scrape_html_list(domain_config, domain)
    elif source_type == "prevnext": items = scrape_prevnext(domain_config, domain)
    else: items = scrape_api(domain_config, domain)
        
    p_c, e_c = 0, 0
    last_err_url = None
    images_for_domain = {}
    new_urls_this_domain = []  # Thu thập URL mới cho url-lists
    
    for item in items:
        if shutdown_flag: break
        st, o_url = execute_pipeline(domain, item, domain_config, images_for_domain)
        if st == "PASS":
            p_c += 1
            new_urls_this_domain.append(o_url)
        elif st in ["ERROR_MOCKUP"]:
            e_c += 1
            last_err_url = o_url
            
    tot_valid = p_c + e_c
    if tot_valid > 0: print(f"[{domain}] Xong: {p_c} Pass | {e_c} Lỗi.")
    
    if tot_valid >= 3 and e_c/tot_valid >= 0.5:
        msg = f"🟥 <b>BÁO ĐỘNG DOMAIN: {domain}</b>\nTỉ lệ lỗi/cắt lẹm {e_c}/{tot_valid} cực cao! Vui lòng rà soát tọa độ!\n🔗 <b>URL Ảnh gốc:</b> {last_err_url}"
        send_alert(msg)
        
    if images_for_domain:
        now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        domain_short = domain.split('.')[0]
        for mockup_name, image_list in images_for_domain.items():
            base_filename = f"{mockup_name}.{domain_short}.{now.strftime('%Y%m%d_%H%M%S')}.{len(image_list)}"
            zip_filename_final = f"{base_filename}.zip"
            zip_filename_tmp = f"{base_filename}.zip.tmp"
            
            zip_path_final = os.path.join(OUTPUT_ZIP_DIR, zip_filename_final)
            zip_path_tmp = os.path.join(OUTPUT_ZIP_DIR, zip_filename_tmp)
            
            try:
                with zipfile.ZipFile(zip_path_tmp, 'w') as zf:
                    for filename, data in image_list: zf.writestr(filename, data)
                os.rename(zip_path_tmp, zip_path_final)
                print(f"📦 Đã tạo thành công ZIP: {zip_filename_final}")
            except Exception as e:
                print(f"❌ Lỗi ghi ZIP: {e}")
                if os.path.exists(zip_path_tmp): os.remove(zip_path_tmp)
                
    return domain, p_c, e_c, new_urls_this_domain

# =========================================================================
# POST-SESSION TASKS: URL LISTS, HISTORY CLEANUP, GIT PUSH
# =========================================================================

def update_url_lists(domain_new_urls):
    """
    Cập nhật folder url-lists/ với URL mới từ mỗi domain.
    Mỗi domain => 1 file domain.com.txt, URL mới dán lên trên cùng, max 500 dòng.
    """
    for domain, new_urls in domain_new_urls.items():
        if not new_urls:
            continue
        filepath = os.path.join(URL_LISTS_DIR, f"{domain}.txt")
        
        # Đọc URL cũ (nếu file đã tồn tại)
        existing_urls = []
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                existing_urls = [line.strip() for line in f if line.strip()]
        
        # Prepend URL mới lên đầu, giữ thứ tự mới nhất trước
        combined = new_urls + existing_urls
        
        # Loại bỏ trùng lặp, giữ thứ tự
        seen = set()
        unique_urls = []
        for url in combined:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        
        # Cắt tối đa MAX_URL_LIST_LINES dòng
        final_urls = unique_urls[:MAX_URL_LIST_LINES]
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(final_urls) + '\n')
        
        print(f"📝 [{domain}] Đã cập nhật {len(new_urls)} URL mới → {filepath} (tổng: {len(final_urls)})")


def update_global_image_urls(domain_new_urls):
    """
    Tạo một file duy nhất 'all_image_urls.txt' lưu toàn bộ link ảnh chuẩn (tối đa 5000 dòng).
    URL mới nhất để ở trên cùng.
    """
    global_file = os.path.join(BASE_DIR, "all_image_urls.txt")
    all_new = []
    # Gom tất cả URL mới của mọi domain
    for new_urls in domain_new_urls.values():
        all_new.extend(new_urls)
        
    if not all_new: return
    
    existing = []
    if os.path.exists(global_file):
        with open(global_file, 'r', encoding='utf-8') as f:
            existing = [l.strip() for l in f if l.strip()]
            
    combined = all_new + existing
    seen = set()
    final_urls = []
    for u in combined:
        if u not in seen:
            seen.add(u)
            final_urls.append(u)
            
    final_urls = final_urls[:MAX_HISTORY_LINES]
    with open(global_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(final_urls) + '\n')
    print(f"📝 Đã tổng hợp {len(all_new)} link ảnh mới vào 'all_image_urls.txt' (Tổng: {len(final_urls)})")


def rebuild_history_file():
    """
    Đọc lại downloaded_history.txt, đưa URL mới lên đầu, cắt max 5000 dòng.
    Gọi 1 lần duy nhất cuối session.
    """
    if not _session_new_urls:
        return
    
    # Đọc toàn bộ file hiện tại
    all_urls = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            all_urls = [line.strip() for line in f if line.strip()]
    
    # Loại các URL mới ra khỏi danh sách cũ (sẽ prepend lên đầu)
    new_set = set(_session_new_urls)
    old_urls = [u for u in all_urls if u not in new_set]
    
    # Prepend URL mới lên đầu (thứ tự mới nhất trước)
    reversed_new = list(reversed(_session_new_urls))  # URL cuối session = mới nhất
    final_list = reversed_new + old_urls
    
    # Cắt max MAX_HISTORY_LINES dòng
    final_list = final_list[:MAX_HISTORY_LINES]
    
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(final_list) + '\n')
    
    print(f"📋 Đã sắp xếp lại history: {len(_session_new_urls)} URL mới lên đầu, tổng {len(final_list)} dòng (max {MAX_HISTORY_LINES})")


def push_to_github():
    """
    Đẩy url-lists/ và downloaded_history.txt lên GitHub dạng amend.
    Yêu cầu: repo đã được git init + remote add trước đó.
    """
    try:
        # Kiểm tra xem có phải git repo không
        result = subprocess.run(
            ["git", "status"], cwd=BASE_DIR,
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print("⚠️ [Git] Chưa khởi tạo git repo. Bỏ qua push.")
            return
        
        # Stage các file cần push
        subprocess.run(
            ["git", "add", "url-lists/", "all_image_urls.txt"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=10
        )
        
        # Kiểm tra có thay đổi gì không
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=10
        )
        if diff_result.returncode == 0:
            print("📡 [Git] Không có thay đổi mới để push.")
            return
        
        # Kiểm tra đã có commit nào chưa
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=10
        )
        
        if log_result.returncode == 0 and log_result.stdout.strip():
            # Đã có commit trước → amend
            subprocess.run(
                ["git", "commit", "--amend", "--no-edit"],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=15
            )
            print("📡 [Git] Đã amend commit.")
        else:
            # Chưa có commit → tạo commit đầu tiên
            subprocess.run(
                ["git", "commit", "-m", "Update url-lists and history"],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=15
            )
            print("📡 [Git] Đã tạo commit đầu tiên.")
        
        # Push force (vì amend thay đổi history)
        push_result = subprocess.run(
            ["git", "push", "--force"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=30
        )
        if push_result.returncode == 0:
            print("✅ [Git] Đã push thành công lên GitHub!")
        else:
            print(f"⚠️ [Git] Push thất bại: {push_result.stderr.strip()}")
            
    except subprocess.TimeoutExpired:
        print("⚠️ [Git] Timeout khi push.")
    except FileNotFoundError:
        print("⚠️ [Git] Không tìm thấy lệnh git. Bỏ qua push.")
    except Exception as e:
        print(f"⚠️ [Git] Lỗi: {e}")


# =========================================================================
# MAIN
# =========================================================================

def main():
    print("=========================================================")
    print("🚀 THE ULTIMATE KTB PIPELINE (CRAWL -> TÁCH NỀN -> MOCKUP -> ZIP) 🚀")
    print("=========================================================")
    domains = list(config_doc.get("domains", {}).keys())
    
    pass_sys, err_sys = 0, 0
    domain_stats = {}
    domain_new_urls = {}  # {domain: [list_of_new_urls]}
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_domain = {executor.submit(process_domain, d): d for d in domains}
        for future in concurrent.futures.as_completed(future_to_domain):
            try:
                dom, p, e, new_urls = future.result()
                if p > 0 or e > 0:
                    domain_stats[dom] = {"pass": p, "error": e}
                    pass_sys += p
                    err_sys += e
                if new_urls:
                    domain_new_urls[dom] = new_urls
            except Exception as exc:
                print(f"[Error Thread] {exc}")
                
    try:
        for f in os.listdir(TEMP_DIR): os.remove(os.path.join(TEMP_DIR, f))
    except: pass
    
    # --- THỐNG KÊ URL MỚI THEO DOMAIN ---
    if domain_new_urls:
        print(f"\n📊 THỐNG KÊ URL MỚI LƯỢT NÀY:")
        total_new = 0
        for dom in sorted(domain_new_urls.keys()):
            count = len(domain_new_urls[dom])
            total_new += count
            print(f"  ▪️ {dom}: {count} URL mới")
        print(f"  ━━━ Tổng: {total_new} URL mới")
    
    print(f"\n🎉 HOÀN TẤT CHIẾN DỊCH TỐI THƯỢNG! Đã đóng gói: {pass_sys} URL | Lỗi: {err_sys}")
    
    # --- CẬP NHẬT URL-LISTS VÀ HISTORY ---
    if domain_new_urls:
        update_url_lists(domain_new_urls)
        update_global_image_urls(domain_new_urls)
    rebuild_history_file()
    
    # --- PUSH LÊN GITHUB ---
    if _session_new_urls:
        push_to_github()
    
    # --- GỬI BÁO CÁO TELEGRAM ---
    if pass_sys > 0 or err_sys > 0:
        report_msg = f"✅ BÁO CÁO ULTIMATE PIPELINE (Total: {pass_sys} Thành công / {err_sys} Lỗi)\nThống kê chi tiết:\n\n"
        for d, stats in domain_stats.items():
            report_msg += f"▪️ {d}: {stats['pass']} OK, {stats['error']} Lỗi\n"
        if domain_new_urls:
            report_msg += "\n📊 URL mới theo domain:\n"
            for dom in sorted(domain_new_urls.keys()):
                report_msg += f"  • {dom}: {len(domain_new_urls[dom])}\n"
        report_msg += "\nCheck error_report.txt log để lấy URL ảnh xấu."
        send_alert(report_msg)

if __name__ == "__main__":
    CRASH_LOG = os.path.join(BASE_DIR, "crash_log.txt")
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Đã dừng bằng Ctrl+C.")
    except Exception as fatal_err:
        timestamp = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh')).strftime('%Y-%m-%d %H:%M:%S')
        error_msg = f"[{timestamp}] FATAL CRASH: {type(fatal_err).__name__}: {fatal_err}\n"
        print(f"\n💀 SCRIPT CRASH: {fatal_err}")
        print(f"📋 Chi tiết đã ghi vào {CRASH_LOG}")
        try:
            import traceback
            with open(CRASH_LOG, 'a', encoding='utf-8') as f:
                f.write(error_msg)
                traceback.print_exc(file=f)
                f.write("\n")
        except: pass
        # Thoát với mã lỗi 1 để shell script biết
        sys.exit(1)
