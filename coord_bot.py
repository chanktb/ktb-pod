#!/home/phim/ktb-pod/.venv/bin/python3
"""
coord_bot.py — Telegram bot nhận ảnh crop + URL gốc → tính tọa độ → patch config.json

Cách dùng:
    python coord_bot.py

Luồng trong Telegram:
    1. Gửi ảnh crop đã cắt, caption = URL ảnh gốc (có thể nhiều URL, mỗi dòng 1 URL)
    2. Bot trả tọa độ + confidence
    3. Nếu caption có URL từ domain chưa có trong config → bot hỏi thêm params để tạo domain mới
    4. Bấm nút chọn domain → chọn pattern → chọn coords key
    5. Bot patch config.json + git push
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
CONFIG_PATH = Path(__file__).parent / "config.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_image_from_url(url: str) -> np.ndarray:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = np.frombuffer(resp.read(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Không decode được ảnh: {url}")
    return img


def load_image_from_bytes(raw: bytes) -> np.ndarray:
    data = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Không decode được ảnh từ bytes")
    return img


def template_match(original: np.ndarray, template: np.ndarray) -> dict | None:
    """Tìm vị trí crop trong ảnh gốc, thử nhiều scale để chịu resize."""
    orig_h, orig_w = original.shape[:2]
    tmpl_h, tmpl_w = template.shape[:2]
    best = {"val": -1, "x": 0, "y": 0, "w": tmpl_w, "h": tmpl_h, "scale": 1.0}

    for s in range(9):  # scale 0.80 → 1.20, bước 0.05
        scale = round(0.80 + s * 0.05, 2)
        rw, rh = int(tmpl_w * scale), int(tmpl_h * scale)
        if rw < 10 or rh < 10 or rw > orig_w or rh > orig_h:
            continue
        resized = cv2.resize(template, (rw, rh))
        result = cv2.matchTemplate(original, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val > best["val"]:
            best = {"val": max_val, "x": max_loc[0], "y": max_loc[1],
                    "w": rw, "h": rh, "scale": scale}

    return best if best["val"] >= 0.5 else None


def read_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def add_rule_at_top(domain: str, pattern: str, coords_key: str, coords: dict) -> str:
    """Thêm rule mới lên đầu rules list, kế thừa action + mockup_sets từ rule cũ cùng pattern."""
    config = read_config()
    domain_cfg = config.get("domains", {}).get(domain, {})
    rules = domain_cfg.get("rules", [])

    # Lấy rule cũ cùng pattern để kế thừa action + mockup_sets
    existing = next((r for r in rules if r.get("pattern") == pattern), None)
    new_rule = {
        "pattern": pattern,
        "action": existing.get("action", "generate") if existing else "generate",
        "mockup_sets_to_use": existing.get("mockup_sets_to_use", []) if existing else [],
        coords_key: coords,
    }
    rules.insert(0, new_rule)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
    return f"✅ Đã thêm rule mới lên đầu\n{coords_key}: {coords}"


def git_push() -> str:
    try:
        subprocess.run(
            ["git", "add", "config.json"],
            cwd=CONFIG_PATH.parent, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=CONFIG_PATH.parent, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "push", "--force"],
            cwd=CONFIG_PATH.parent, check=True, capture_output=True
        )
        return "✅ Git push force thành công"
    except subprocess.CalledProcessError as e:
        return f"⚠️ Git push lỗi: {e.stderr.decode()[:200]}"


def detect_domain_from_url(url: str) -> str | None:
    """Lấy domain trong config khớp với hostname của URL ảnh.
    Hỗ trợ image_domains để map CDN domain về store domain.
    """
    try:
        hostname = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return None
    config = read_config()
    for domain, cfg in config.get("domains", {}).items():
        # Match trực tiếp theo store domain
        if hostname == domain or hostname.endswith("." + domain):
            return domain
        # Match qua image_domains (CDN mapping)
        for img_domain in cfg.get("image_domains", []):
            if hostname == img_domain or hostname.endswith("." + img_domain):
                return domain
    return None


def detect_pattern_from_url(url: str, domain: str) -> str | None:
    """Tìm pattern khớp với URL bằng endswith, giống logic main.py."""
    config = read_config()
    rules = config.get("domains", {}).get(domain, {}).get("rules", [])
    for rule in rules:
        pat = rule.get("pattern", "")
        if pat and url.endswith(pat):
            return pat
    return None


def get_patterns_for_domain(domain: str) -> list[str]:
    config = read_config()
    rules = config.get("domains", {}).get(domain, {}).get("rules", [])
    return [r["pattern"] for r in rules if r.get("action") == "generate"]


def extract_urls_from_caption(caption: str) -> list[str]:
    """Trích xuất tất cả HTTP/HTTPS URL từ text caption."""
    return re.findall(r'https?://\S+', caption)


def infer_pattern_from_url(url: str) -> str:
    """Suy ra pattern suffix (-producttype.ext) từ filename trong URL."""
    path = urllib.parse.urlparse(url).path
    filename = path.rsplit('/', 1)[-1] if '/' in path else path
    m = re.search(r'(-[A-Za-z][A-Za-z0-9-]*\.[a-z]{2,5})$', filename)
    if m:
        return m.group(1)
    return ('.' + filename.rsplit('.', 1)[-1]) if '.' in filename else filename


def add_new_domain(domain: str, base_url: str, pattern: str, source_type: str,
                   coords: dict, mockup_sets: list) -> str:
    """Thêm domain mới vào config.json với rule cơ bản."""
    config = read_config()
    if domain in config.get("domains", {}):
        return f"⚠️ Domain '{domain}' đã tồn tại"
    config["domains"][domain] = {
        "base_url": base_url,
        "source_type": source_type,
        "max_days_old": 1,
        "rules": [{
            "pattern": pattern,
            "action": "generate",
            "mockup_sets_to_use": mockup_sets,
            "coords": coords,
        }],
    }
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
    return f"✅ Đã thêm domain '{domain}'"


def probe_listing_url(listing_url: str) -> dict:
    """Thử auto-detect source_type từ listing URL.
    Trả về dict: domain, base_url, source_type (None nếu không detect được), detected (bool).
    """
    parsed = urllib.parse.urlparse(listing_url)
    hostname = parsed.netloc.lstrip("www.")
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Thử Shopify API: /products.json?limit=1
    try:
        api_url = f"{origin}/products.json?limit=1"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if isinstance(data, dict) and "products" in data:
                return {
                    "domain": hostname,
                    "base_url": origin + "/",
                    "source_type": "api",
                    "detected": True,
                    "note": "Shopify API (/products.json)",
                }
    except Exception:
        pass

    # Thử fetch listing URL → xác nhận truy cập được
    try:
        req = urllib.request.Request(listing_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "html" in ct:
                base = listing_url if listing_url.endswith("/") else listing_url + "/"
                return {
                    "domain": hostname,
                    "base_url": base,
                    "source_type": "product-list",
                    "detected": True,
                    "note": "HTML listing page",
                }
    except Exception:
        pass

    # Không detect được — trả về default, user chọn source_type
    return {
        "domain": hostname,
        "base_url": origin + "/",
        "source_type": None,
        "detected": False,
        "note": "Không truy cập được URL",
    }


def get_default_mockup_sets(primary_domain: str | None) -> list:
    """Lấy mockup_sets từ rule generate đầu tiên của primary domain, fallback printiment."""
    if not primary_domain:
        return ["printiment"]
    config = read_config()
    rules = config.get("domains", {}).get(primary_domain, {}).get("rules", [])
    for r in rules:
        if r.get("action") == "generate" and r.get("mockup_sets_to_use"):
            return r["mockup_sets_to_use"]
    return ["printiment"]


# ─── State store (in-memory, đủ dùng cho single-user bot) ─────────────────────

# pending[chat_id] = {
#   "coords": {...}, "confidence": 0.9, "orig_url": "...",
#   "domain": "...", "pattern": "...",
#   "new_domain_entries": [{"domain":..., "url":..., "pattern":..., "source_type": None}, ...],
#   "new_domain_idx": 0,
# }
pending: dict[int, dict] = {}


# ─── Internal UI helpers ───────────────────────────────────────────────────────

async def _ask_source_type(query, state: dict):
    """Hỏi source_type cho new_domain_entries[new_domain_idx]."""
    idx = state.get("new_domain_idx", 0)
    entries = state.get("new_domain_entries", [])
    entry = entries[idx]
    total = len(entries)
    step = f"({idx + 1}/{total}) " if total > 1 else ""
    buttons = [
        [InlineKeyboardButton("product-list", callback_data="source_type:product-list")],
        [InlineKeyboardButton("api",          callback_data="source_type:api")],
        [InlineKeyboardButton("prevnext",     callback_data="source_type:prevnext")],
        [InlineKeyboardButton("❌ Bỏ qua tất cả", callback_data="skip_new_domains")],
    ]
    await query.edit_message_text(
        f"🔧 {step}source_type cho <b>{entry['domain']}</b>?\n"
        f"Pattern: <code>{entry['pattern']}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _finish_add_new_domains(query, state: dict):
    """Thêm tất cả domain mới vào config và push, rồi hiển thị key selection."""
    entries = state.get("new_domain_entries", [])
    primary_domain = state.get("domain")
    mockup_sets = get_default_mockup_sets(primary_domain)
    coords = state["coords"]

    results = []
    for e in entries:
        parsed = urllib.parse.urlparse(e["url"])
        base_url = f"{parsed.scheme}://{parsed.netloc}/"
        msg = add_new_domain(
            e["domain"], base_url, e["pattern"],
            e["source_type"], coords, mockup_sets,
        )
        results.append(msg)

    push_msg = git_push()
    summary = "\n".join(results) + f"\n{push_msg}"

    state["new_domain_entries"] = []

    # Tiếp tục với key selection cho primary domain
    domain = state.get("domain")
    pattern = state.get("pattern")
    c = state["coords"]
    conf = state.get("confidence", 0)
    warn = " ⚠️ confidence thấp!" if conf < 0.75 else ""

    if domain and pattern:
        buttons = [
            [InlineKeyboardButton("coords (chung)", callback_data="key:coords")],
            [InlineKeyboardButton("coords_white", callback_data="key:coords_white")],
            [InlineKeyboardButton("coords_black", callback_data="key:coords_black")],
            [InlineKeyboardButton("✏️ Pattern khác", callback_data="custom_pattern")],
            [InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")],
        ]
        await query.edit_message_text(
            f"{summary}\n\n"
            f"📐 <b>Tiếp tục cập nhật tọa độ</b>{warn}\n"
            f"x={c['x']}, y={c['y']}, w={c['w']}, h={c['h']}\n"
            f"Domain: <b>{domain}</b>\nPattern: <b>{pattern}</b>\nCập nhật key nào?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await query.edit_message_text(
            f"{summary}\n\n✅ Hoàn tất thêm domain mới.",
            parse_mode="HTML",
        )
        pending.pop(query.message.chat_id, None)


async def _show_key_selection(query, state: dict, header: str = ""):
    """Hiển thị lựa chọn coords key sau khi đã có domain + pattern."""
    domain = state.get("domain")
    pattern = state.get("pattern")
    coords = state["coords"]
    conf = state.get("confidence", 0)
    warn = " ⚠️ confidence thấp!" if conf < 0.75 else ""

    buttons = [
        [InlineKeyboardButton("coords (chung)", callback_data="key:coords")],
        [InlineKeyboardButton("coords_white", callback_data="key:coords_white")],
        [InlineKeyboardButton("coords_black", callback_data="key:coords_black")],
        [InlineKeyboardButton("✏️ Pattern khác", callback_data="custom_pattern")],
        [InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")],
    ]
    text = (
        f"{header}"
        f"📐 <b>Tọa độ tìm được</b>{warn}\n"
        f"x={coords['x']}, y={coords['y']}, w={coords['w']}, h={coords['h']}\n\n"
        f"Domain: <b>{domain}</b>\nPattern: <b>{pattern}</b>\nCập nhật key nào?"
    )
    await query.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(buttons))


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nhận ảnh crop + caption chứa URL(s) gốc → chạy template matching."""
    msg = update.message
    if not msg or not msg.photo:
        return

    caption = (msg.caption or "").strip()
    urls = extract_urls_from_caption(caption)

    if not urls:
        await msg.reply_text(
            "⚠️ Hãy gửi ảnh crop với <b>caption là URL ảnh gốc</b>.\n"
            "Có thể thêm nhiều URL (mỗi dòng 1 URL) để tạo domain mới.\n"
            "Ví dụ caption:\n<code>https://images.techteesusa.com/.../image.webp\n"
            "https://newsite.com/.../image.webp</code>",
            parse_mode="HTML"
        )
        return

    await msg.reply_text("⏳ Đang xử lý…")

    orig_url = urls[0]

    try:
        original = load_image_from_url(orig_url)
        photo_file = await msg.photo[-1].get_file()
        raw = await photo_file.download_as_bytearray()
        template = load_image_from_bytes(bytes(raw))
        result = template_match(original, template)
    except Exception as e:
        await msg.reply_text(f"❌ Lỗi xử lý ảnh:\n<code>{e}</code>", parse_mode="HTML")
        return

    if result is None:
        await msg.reply_text(
            "❌ Không tìm được vị trí crop trong ảnh gốc (confidence < 0.5).\n"
            "Hãy đảm bảo ảnh crop lấy trực tiếp từ ảnh gốc, không qua xử lý."
        )
        return

    coords = {"x": result["x"], "y": result["y"], "w": result["w"], "h": result["h"]}
    conf = result["val"]
    warn = " ⚠️ confidence thấp!" if conf < 0.75 else ""
    conf_bar = "🟩" * int(conf * 10) + "⬜" * (10 - int(conf * 10))

    # Tự detect domain + pattern từ URL đầu tiên
    domain = detect_domain_from_url(orig_url)
    pattern = detect_pattern_from_url(orig_url, domain) if domain else None

    # Tìm domain mới từ các URL còn lại
    config_domains = set(read_config().get("domains", {}).keys())
    new_domain_entries = []
    for u in urls[1:]:
        h = urllib.parse.urlparse(u).hostname or ""
        known = any(h == d or h.endswith("." + d) for d in config_domains)
        if not known and h:
            new_domain_entries.append({
                "domain": h,
                "url": u,
                "pattern": infer_pattern_from_url(u),
                "source_type": None,
            })

    # Lưu vào pending
    pending[msg.chat_id] = {
        "coords": coords,
        "orig_url": orig_url,
        "confidence": conf,
        "domain": domain,
        "pattern": pattern,
        "new_domain_entries": new_domain_entries,
        "new_domain_idx": 0,
    }

    header_base = (
        f"📐 <b>Tọa độ tìm được</b>{warn}\n"
        f"x={coords['x']}, y={coords['y']}, w={coords['w']}, h={coords['h']}\n"
        f"Confidence: {conf:.3f} {conf_bar}\n\n"
    )

    # Phần hiển thị domain mới
    new_domain_section = ""
    if new_domain_entries:
        lines = "\n".join(f"• {e['domain']}  (pattern: <code>{e['pattern']}</code>)"
                          for e in new_domain_entries)
        new_domain_section = f"🆕 <b>Domain mới chưa có trong config:</b>\n{lines}\n\n"

    if new_domain_entries:
        # Có domain mới → hỏi thêm trước
        new_domain_buttons = [
            [InlineKeyboardButton("✅ Thêm domain mới", callback_data="start_add_new_domains")],
            [InlineKeyboardButton("⏭ Bỏ qua domain mới", callback_data="skip_new_domains")],
        ]

        if domain and pattern:
            extra = f"Domain: <b>{domain}</b>\nPattern: <b>{pattern}</b>\n\n"
        elif domain:
            extra = f"Domain: <b>{domain}</b> (chưa match pattern)\n\n"
        else:
            extra = ""

        await msg.reply_text(
            header_base + new_domain_section + extra + "Xử lý domain mới trước?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(new_domain_buttons),
        )
        return

    # Không có domain mới → luồng bình thường
    if domain and pattern:
        buttons = [
            [InlineKeyboardButton("coords (chung)", callback_data="key:coords")],
            [InlineKeyboardButton("coords_white", callback_data="key:coords_white")],
            [InlineKeyboardButton("coords_black", callback_data="key:coords_black")],
            [InlineKeyboardButton("✏️ Pattern khác", callback_data="custom_pattern")],
            [InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")],
        ]
        await msg.reply_text(
            header_base + f"Domain: <b>{domain}</b>\nPattern: <b>{pattern}</b>\nCập nhật key nào?",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )
    elif domain:
        patterns = get_patterns_for_domain(domain)
        buttons = [[InlineKeyboardButton(p, callback_data=f"pattern:{p}")] for p in patterns]
        buttons.append([InlineKeyboardButton("✏️ Nhập thủ công", callback_data="custom_pattern")])
        buttons.append([InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")])
        await msg.reply_text(
            header_base + f"Domain: <b>{domain}</b>\nChọn pattern:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        all_domains = list(read_config().get("domains", {}).keys())
        header = (
            f"📐 <b>Tọa độ tìm được</b>{warn}\n"
            f"x={coords['x']}, y={coords['y']}, w={coords['w']}, h={coords['h']}\n"
            f"Confidence: {conf:.3f} {conf_bar}\n\n"
            f"⚠️ Không nhận ra domain từ URL. Chọn domain thủ công:"
        )
        buttons, row = [], []
        for d in all_domains:
            row.append(InlineKeyboardButton(d, callback_data=f"domain:{d}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("✏️ Nhập URL listing mới", callback_data="input_listing_url")])
        buttons.append([InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")])
        await msg.reply_text(header, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # callback cũ đã hết hạn, bỏ qua
    chat_id = query.message.chat_id
    data = query.data

    if data == "cancel":
        await query.edit_message_text("❌ Đã hủy.")
        pending.pop(chat_id, None)
        return

    state = pending.get(chat_id, {})
    if not state:
        await query.edit_message_text("⏱️ Session hết hạn. Hãy gửi lại ảnh crop.")
        return

    # ── Thêm domain mới: bắt đầu hỏi source_type ──────────────────────────────
    if data == "start_add_new_domains":
        state["new_domain_idx"] = 0
        await _ask_source_type(query, state)
        return

    # ── Bỏ qua domain mới, tiếp tục luồng bình thường ─────────────────────────
    if data == "skip_new_domains":
        state["new_domain_entries"] = []
        domain = state.get("domain")
        pattern = state.get("pattern")
        coords = state["coords"]
        conf = state.get("confidence", 0)
        warn = " ⚠️ confidence thấp!" if conf < 0.75 else ""
        conf_bar = "🟩" * int(conf * 10) + "⬜" * (10 - int(conf * 10))
        header_base = (
            f"📐 <b>Tọa độ tìm được</b>{warn}\n"
            f"x={coords['x']}, y={coords['y']}, w={coords['w']}, h={coords['h']}\n"
            f"Confidence: {conf:.3f} {conf_bar}\n\n"
        )
        if domain and pattern:
            buttons = [
                [InlineKeyboardButton("coords (chung)", callback_data="key:coords")],
                [InlineKeyboardButton("coords_white", callback_data="key:coords_white")],
                [InlineKeyboardButton("coords_black", callback_data="key:coords_black")],
                [InlineKeyboardButton("✏️ Pattern khác", callback_data="custom_pattern")],
                [InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")],
            ]
            await query.edit_message_text(
                header_base + f"Domain: <b>{domain}</b>\nPattern: <b>{pattern}</b>\nCập nhật key nào?",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
            )
        elif domain:
            patterns = get_patterns_for_domain(domain)
            buttons = [[InlineKeyboardButton(p, callback_data=f"pattern:{p}")] for p in patterns]
            buttons.append([InlineKeyboardButton("✏️ Nhập thủ công", callback_data="custom_pattern")])
            buttons.append([InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")])
            await query.edit_message_text(
                header_base + f"Domain: <b>{domain}</b>\nChọn pattern:",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await query.edit_message_text("⏭ Đã bỏ qua domain mới. Hãy gửi lại ảnh nếu cần.")
            pending.pop(chat_id, None)
        return

    # ── Nhận source_type cho domain mới ───────────────────────────────────────
    if data.startswith("source_type:"):
        source_type = data[len("source_type:"):]
        entries = state.get("new_domain_entries", [])
        idx = state.get("new_domain_idx", 0)
        if idx < len(entries):
            entries[idx]["source_type"] = source_type
        idx += 1
        state["new_domain_idx"] = idx
        if idx < len(entries):
            await _ask_source_type(query, state)
        else:
            await _finish_add_new_domains(query, state)
        return

    # Bước 1: chọn domain → hiển thị patterns
    if data.startswith("domain:"):
        domain = data[len("domain:"):]
        state["domain"] = domain
        patterns = get_patterns_for_domain(domain)
        if not patterns:
            await query.edit_message_text(f"⚠️ Domain '{domain}' không có pattern generate nào.")
            return
        buttons = [
            [InlineKeyboardButton(p, callback_data=f"pattern:{p}")]
            for p in patterns
        ]
        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back_domain")])
        await query.edit_message_text(
            f"Domain: <b>{domain}</b>\nChọn pattern:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # Bước 1b: quay lại chọn domain
    if data == "back_domain":
        config = read_config()
        domains = list(config.get("domains", {}).keys())
        buttons = []
        row = []
        for i, d in enumerate(domains):
            row.append(InlineKeyboardButton(d, callback_data=f"domain:{d}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("✏️ Nhập URL listing mới", callback_data="input_listing_url")])
        buttons.append([InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")])
        await query.edit_message_text(
            "Chọn domain để cập nhật config:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # ── Nhập URL listing để tạo domain mới ────────────────────────────────────
    if data == "input_listing_url":
        state["waiting_for"] = "listing_url"
        await query.edit_message_text(
            "✏️ Nhập URL trang danh sách sản phẩm:\n"
            "Ví dụ: <code>https://newsite.com/collections/all</code>\n\n"
            "Bot sẽ tự detect source_type nếu có thể.",
            parse_mode="HTML",
        )
        return

    # ── Xác nhận source_type sau khi probe listing URL ────────────────────────
    if data.startswith("confirm_new_domain:"):
        source_type = data[len("confirm_new_domain:"):]
        probe = state.get("pending_listing_probe", {})
        domain = probe.get("domain", "")
        base_url = probe.get("base_url", "")
        coords = state["coords"]
        pattern = infer_pattern_from_url(state.get("orig_url", ""))
        mockup_sets = get_default_mockup_sets(state.get("domain"))

        result_msg = add_new_domain(domain, base_url, pattern, source_type, coords, mockup_sets)
        push_msg = git_push()
        state.pop("pending_listing_probe", None)

        await query.edit_message_text(
            f"{result_msg}\n{push_msg}\n\n"
            f"Domain: <b>{domain}</b>\nbase_url: <code>{base_url}</code>\n"
            f"source_type: <b>{source_type}</b>\nPattern: <code>{pattern}</code>",
            parse_mode="HTML",
        )
        pending.pop(chat_id, None)
        return

    # Bước 2: chọn pattern → hiển thị coords key
    if data.startswith("pattern:"):
        pattern = data[len("pattern:"):]
        state["pattern"] = pattern
        buttons = [
            [InlineKeyboardButton("coords (chung)", callback_data="key:coords")],
            [InlineKeyboardButton("coords_white", callback_data="key:coords_white")],
            [InlineKeyboardButton("coords_black", callback_data="key:coords_black")],
            [InlineKeyboardButton("✏️ Pattern khác", callback_data="custom_pattern")],
            [InlineKeyboardButton("⬅️ Quay lại", callback_data=f"domain:{state['domain']}")],
        ]
        coords = state["coords"]
        await query.edit_message_text(
            f"Pattern: <b>{pattern}</b>\n"
            f"Tọa độ: x={coords['x']}, y={coords['y']}, w={coords['w']}, h={coords['h']}\n\n"
            f"Cập nhật key nào?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # Bước 3: xác nhận patch + push
    if data.startswith("key:"):
        coords_key = data[len("key:"):]
        domain = state.get("domain")
        pattern = state.get("pattern")
        coords = state.get("coords")

        if not all([domain, pattern, coords]):
            await query.edit_message_text("⚠️ Thiếu thông tin. Hãy gửi lại ảnh.")
            return

        patch_msg = add_rule_at_top(domain, pattern, coords_key, coords)

        await query.edit_message_text(
            f"🔧 <b>Kết quả cập nhật</b>\n\n"
            f"Domain: {domain}\nPattern: {pattern}\nKey: {coords_key}\n\n"
            f"{patch_msg}",
            parse_mode="HTML"
        )
        pending.pop(chat_id, None)
        return

    if data == "custom_pattern":
        state["waiting_for"] = "pattern"
        coords = state["coords"]
        await query.edit_message_text(
            f"✏️ Nhập pattern tùy chỉnh (ví dụ: <code>-T-shirt.webp</code>)\n\n"
            f"Tọa độ sẽ dùng: x={coords['x']}, y={coords['y']}, w={coords['w']}, h={coords['h']}",
            parse_mode="HTML"
        )
        return

    await query.edit_message_text("❓ Lệnh không hợp lệ.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nhận text input thủ công: pattern hoặc listing URL."""
    msg = update.message
    if not msg or not msg.text:
        return
    state = pending.get(msg.chat_id, {})
    waiting = state.get("waiting_for")
    if not waiting:
        return

    # ── Nhận listing URL → probe và hỏi source_type ───────────────────────────
    if waiting == "listing_url":
        listing_url = msg.text.strip()
        state["waiting_for"] = None

        await msg.reply_text("⏳ Đang kiểm tra URL…")

        probe = probe_listing_url(listing_url)
        state["pending_listing_probe"] = probe

        source_type_buttons = [
            [InlineKeyboardButton("product-list", callback_data="confirm_new_domain:product-list")],
            [InlineKeyboardButton("api",          callback_data="confirm_new_domain:api")],
            [InlineKeyboardButton("prevnext",     callback_data="confirm_new_domain:prevnext")],
            [InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")],
        ]

        if probe["detected"]:
            st = probe["source_type"]
            note = probe.get("note", "")
            # Đặt nút detected lên đầu
            detected_btn = InlineKeyboardButton(
                f"✅ {st} (tự detect)", callback_data=f"confirm_new_domain:{st}"
            )
            other = [b for b in source_type_buttons if f"confirm_new_domain:{st}" not in b[0].callback_data]
            buttons = [[detected_btn]] + other
            await msg.reply_text(
                f"🔍 <b>Kết quả phân tích:</b>\n"
                f"Domain: <b>{probe['domain']}</b>\n"
                f"base_url: <code>{probe['base_url']}</code>\n"
                f"source_type: <b>{st}</b> ({note})\n\n"
                f"Xác nhận hoặc chọn source_type khác:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await msg.reply_text(
                f"⚠️ <b>Không tự detect được</b> ({probe.get('note', '')})\n"
                f"Domain: <b>{probe['domain']}</b>\n"
                f"base_url: <code>{probe['base_url']}</code>\n\n"
                f"Chọn source_type thủ công:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(source_type_buttons),
            )
        return

    # ── Nhận pattern nhập thủ công ────────────────────────────────────────────
    if waiting != "pattern":
        return

    pattern = msg.text.strip()
    state["pattern"] = pattern
    state["waiting_for"] = None

    coords = state["coords"]
    domain = state.get("domain", "?")
    buttons = [
        [InlineKeyboardButton("coords (chung)", callback_data="key:coords")],
        [InlineKeyboardButton("coords_white", callback_data="key:coords_white")],
        [InlineKeyboardButton("coords_black", callback_data="key:coords_black")],
        [InlineKeyboardButton("❌ Bỏ qua", callback_data="cancel")],
    ]
    await msg.reply_text(
        f"Domain: <b>{domain}</b>\nPattern: <b>{pattern}</b>\n"
        f"Tọa độ: x={coords['x']}, y={coords['y']}, w={coords['w']}, h={coords['h']}\n\n"
        f"Cập nhật key nào?",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("[ERROR] Thiếu TELEGRAM_BOT_TOKEN trong .env")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("Coord bot đang chạy… Gửi ảnh crop với caption = URL(s) gốc")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
