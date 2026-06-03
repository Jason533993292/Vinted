import asyncio
import sys
from playwright.async_api import async_playwright
import os
import aiohttp
import io
import base64
import re
import hashlib
import time
import json

from PIL import Image
from dotenv import load_dotenv
# Load API keys from .env file
# Load .env from the script directory to guarantee it’s found
script_dir = os.path.abspath(os.path.dirname(__file__))
dotenv_path = os.path.join(script_dir, ".env")
load_dotenv(dotenv_path)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.5-flash")

if not OPENROUTER_API_KEY:
    raise RuntimeError("No OPENROUTER_API_KEY found in environment or fallback. Please check .env file.")

print(f"✅ OpenRouter API configured (Model: {OPENROUTER_MODEL})")
print("✅ API keys loaded successfully.")

BUDGET = 5.0  # max budget in USD for API usage
def render_cost_bar(spent: float, budget: float = 5.0) -> str:
    """Return a simple text progress bar showing API cost usage.
    Args:
        spent: Amount of budget spent so far.
        budget: Total budget limit (default $5.0).
    Returns:
        A string like "[██████░░░░] $1.20 / $5.00".
    """
    spent = max(0.0, min(spent, budget))
    percent = spent / budget
    bar_len = 12
    filled = int(round(percent * bar_len))
    bar = "█" * filled + "░" * (bar_len - filled)
    return f"[${spent:.2f} / ${budget:.2f}] [{bar}]"

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_PASSWORD = "859859"
SEMAPHORE_LIMIT = 1          # Set to 1 to strictly respect the 50 RPM rate limit on free tier

# Resolve absolute directory paths relative to script location
OUTPUT_DIR = os.path.join(script_dir, "filtered_yupoo_images")
ACCEPTED_DIR = os.path.join(script_dir, "Accepted")
REJECTED_DIR = os.path.join(script_dir, "Rejected")

# Resolve references directory
REF_DIR = os.path.join(script_dir, "references")
if not os.path.exists(REF_DIR):
    alt_ref_dir = os.path.join(script_dir, "reference images")
    if os.path.exists(alt_ref_dir):
        REF_DIR = alt_ref_dir

# Category config mappings for target/blocklist keywords and prompt descriptions
CATEGORY_CONFIGS = {
    "shoes": {
        "keywords": [
            "nike", "dunk", "nb", "new balance", "asics", "鞋", "耐克",
            "air force", "af1", "pegasus", "vomero", "cortez", "air max", "force"
        ],
        "block_keywords": [
            "jacket", "coat", "hoodie", "sweatshirt", "pants", "shorts", "shirt", "tee", "t-shirt", 
            "socks", "sock", "hat", "cap", "bag", "backpack", "underwear", "boxer", "衣", "裤", "包", "帽", "袜",
            "lv", "louis vuitton", "dior", "prada", "gucci", "chanel", "hermes", "boss", "fendi", "dolce", "armani",
            "jordan", "aj", "air jordan"
        ],
        "target_desc": "Sneakers, athletic shoes (Nike Air Force 1, Dunks, Air Max, Pegasus, Vomero, Cortez), New Balance, or ASICS matching the reference images. Strictly reject Jordans/AJ, and strictly reject designer fashion sneakers (such as Louis Vuitton, LV, Dior, Gucci, Prada, Armani, Fendi, Boss, Dolce & Gabbana)."
    },
    "coats": {
        "keywords": [
            "moncler", "canada goose", "north face", "stone island", "nocta", "nike", "adidas", 
            "carhartt", "cp company", "lacoste", "polo", "ralph lauren",
            "down", "jacket", "coat", "puffer", "parka", "windbreaker",
            "羽绒", "大衣", "外套", "棉服", "夹克", "风衣", "石岛"
        ],
        "block_keywords": [
            "pants", "pant", "shorts", "short", "shoes", "shoe", "socks", "sock", 
            "t-shirt", "tee", "underwear", "boxer", "hat", "cap", "bag", "backpack", 
            "jogger", "sweatpant", "cargo pant", "sweater", "knit",
            "裤", "鞋", "袜", "包", "帽"
        ],
        "target_desc": "Heavy puffer jackets (Nike/NOCTA, Stone Island, CP Company, Lacoste, Polo Ralph Lauren, Carhartt), down jackets, or windbreakers matching the reference images."
    },
    "clothing": {
        "keywords": [
            "nocta", "nike", "adidas", "under armour", 
            "stone island", "carhartt",
            "chinese new year", "新年", "新春", "国潮", "唐装", "外套", "风衣", "卫衣", "夹克", "短袖", "防晒", "连帽", "衫子"
        ],
        "block_keywords": [
            "shoes", "shoe", "socks", "sock", "bag", "bags", "hat", "hats", "cap", "caps", 
            "backpack", "belt", "belts", "glasses", "sunglasses", "underwear", "boxer", 
            "裤", "鞋", "包", "帽", "袜", "镜", "带"
        ],
        "target_desc": "Nike Nocta jackets/hoodies, Adidas CNY jackets, black Under Armour t-shirts, Stone Island hoodies/overshirts/knitwear, or Carhartt jackets matching the reference images."
    }
}

# The collection of Yupoo sources with their associated categories and passwords
SOURCES = [
    # --- SHOES ---
    {"url": "https://karen9394.x.yupoo.com/categories/4411331", "category": "shoes", "password": "859859"},
    {"url": "https://makeawishlove.x.yupoo.com/albums?tab=gallery", "category": "shoes", "password": None},
    {"url": "https://ghxy.x.yupoo.com/albums?tab=gallery", "category": "shoes", "password": None},
    {"url": "https://xy0594xy.x.yupoo.com/albums?tab=gallery", "category": "shoes", "password": None},
    {"url": "https://x.yupoo.com/photos/mzrycm102618/albums?tab=gallery", "category": "shoes", "password": None},
    {"url": "https://gm1688.x.yupoo.com/albums?tab=gallery", "category": "shoes", "password": None},
    
    # --- COATS ---
    {"url": "https://karen9394.x.yupoo.com/collections/4799532", "category": "coats", "password": "859859"},
    {"url": "https://linger1988.x.yupoo.com/search/album?uid=1&sort=&q=%E7%BE%BD%E7%BB%92%E6%9C%BD", "category": "coats", "password": None},
    {"url": "https://2605196665.x.yupoo.com/albums?tab=gallery", "category": "coats", "password": "918918"},
    {"url": "https://linger1988.x.yupoo.com/search/album?uid=1&sort=&q=%E5%A4%A4%E5%A5%97", "category": "coats", "password": None},
    {"url": "https://18588679886.x.yupoo.com/search/album?uid=1&sort=&q=%E5%A4%A4%E5%A5%97", "category": "coats", "password": None},
    {"url": "https://18086831394.x.yupoo.com/albums?tab=gallery", "category": "coats", "password": None},
    
    # --- CLOTHING ---
    {"url": "https://x.yupoo.com/photos/huarache/albums?tab=gallery", "category": "clothing", "password": "147258369"},
    {"url": "https://18588679886.x.yupoo.com/albums?tab=gallery", "category": "clothing", "password": None},
    {"url": "https://miao2017.x.yupoo.com/albums?tab=gallery", "category": "clothing", "password": None},
    {"url": "https://new666.x.yupoo.com/albums?tab=gallery", "category": "clothing", "password": None},
    {"url": "https://linger1988.x.yupoo.com/albums?tab=gallery", "category": "clothing", "password": None},
    {"url": "https://x.yupoo.com/photos/adidas666888/albums?tab=gallery", "category": "clothing", "password": None},
    {"url": "https://fuzhuangpifa1234.x.yupoo.com/albums?tab=gallery", "category": "clothing", "password": None}
]
# ──────────────────────────────────────────────────────────────────────────────

CHECKPOINT_FILE = os.path.join(OUTPUT_DIR, "processed_urls.txt")

def title_might_match(title: str, category: str) -> bool:
    t = title.lower()
    config = CATEGORY_CONFIGS.get(category, CATEGORY_CONFIGS["clothing"])
    
    # 1. Filter out blocklisted categories
    if any(block_kw in t for block_kw in config["block_keywords"]):
        # Exception: if the title also mentions "jacket" or "outerwear" (only for clothing category)
        if category == "clothing" and any(keep_kw in t for keep_kw in ["jacket", "outerwear", "外套", "风衣", "卫衣", "夹克"]):
            pass
        else:
            return False # Skip irrelevant categories
            
    # 2. Check if it contains target brand / theme keywords
    if any(kw in t for kw in config["keywords"]):
        return True
        
    # 3. Check if it contains standard style codes (e.g. JZ9927, DV9816, FQ4951, H12345)
    if re.search(r'\b[a-zA-Z]{1,2}\d{4,5}(?:-\d{3})?\b', title):
        return True
        
    return False

def load_processed_urls() -> set[str]:
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
        return set(f.read().splitlines())

def mark_url_processed(url: str):
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")

async def call_openrouter(session: aiohttp.ClientSession, payload: dict) -> str:
    """Make an async call to OpenRouter API with retries and exponential backoff."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/phil/Resell",
        "X-Title": "Resell Scraper"
    }
    
    max_retries = 5
    backoff = 2.0
    
    for attempt in range(1, max_retries + 1):
        try:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "").strip()
                    else:
                        print(f"⚠️ OpenRouter response missing choices: {data}")
                        return ""
                elif resp.status == 429:
                    print(f"⚠️ OpenRouter rate limit (429) on attempt {attempt}/{max_retries}. Retrying in {backoff:.1f}s...")
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                elif resp.status in [500, 502, 503, 504]:
                    print(f"⚠️ OpenRouter server error ({resp.status}) on attempt {attempt}/{max_retries}. Retrying in {backoff:.1f}s...")
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                else:
                    body = await resp.text()
                    print(f"❌ OpenRouter error {resp.status}: {body}")
                    return f"ERROR: {resp.status} - {body}"
        except asyncio.TimeoutError:
            print(f"⚠️ OpenRouter timeout on attempt {attempt}/{max_retries}. Retrying in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
            backoff *= 2.0
        except Exception as e:
            print(f"⚠️ OpenRouter exception on attempt {attempt}/{max_retries}: {e}")
            await asyncio.sleep(backoff)
            backoff *= 2.0
            
    return "ERROR: Max retries exceeded"

def get_openrouter_prompt(title: str, category: str) -> str:
    config = CATEGORY_CONFIGS.get(category, CATEGORY_CONFIGS["clothing"])
    target_desc = config["target_desc"]
    block_kws_str = ", ".join(config["block_keywords"])
    return f"""Look at this candidate item image.
The seller's title for this item is: "{title}"

We want to verify if it matches our target:
{target_desc}

Be strict. If the item is in the forbidden list ({block_kws_str}) or does not match the target, it should be REJECTED.

Respond in this exact format and nothing else:
VERDICT: ACCEPT or REJECT
REASONING: <brief explanation of what you see and why you chose the verdict>"""


def resize_image(image_bytes: bytes, max_size: int = 400) -> bytes:
    """Resize image to max_size x max_size to reduce API cost and latency."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_size, max_size))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def load_references_b64() -> list[dict]:
    """Load and resize reference images from a 'references' folder. Terminates script if empty."""
    if not os.path.exists(REF_DIR):
        raise SystemExit(
            f"\n❌ [CRITICAL ERROR] Reference folder '{REF_DIR}' does not exist!\n"
            "Please create a 'references' folder and add target clothing images to run the scraper."
        )
        
    ref_data = []
    files = sorted(os.listdir(REF_DIR))
    for f in files:
        if f.lower().endswith(('.jpg', '.jpeg', '.png')):
            try:
                with open(os.path.join(REF_DIR, f), "rb") as image_file:
                    content = image_file.read()
                    # Downscale reference to 400x400 to save tokens/cost
                    resized = resize_image(content, max_size=400)
                    b64_data = base64.standard_b64encode(resized).decode("utf-8")
                    
                    media_type = "image/jpeg"
                    
                    # Load as PIL Image for Gemini
                    pil_img = Image.open(io.BytesIO(resized))
                    
                    ref_data.append({
                        "name": f,
                        "media_type": media_type,
                        "data": b64_data,
                        "pil": pil_img
                    })
            except Exception as e:
                print(f"⚠️ Error loading reference {f}: {e}")
                
    if not ref_data:
        raise SystemExit(
            f"\n❌ [CRITICAL ERROR] No valid images (.jpg, .jpeg, .png) found in '{REF_DIR}' folder!\n"
            "Please add at least one reference image to run the scraper."
        )
        
    print(f"📚 Loaded {len(ref_data)} reference images from '{REF_DIR}' folder.")
    return ref_data


def load_reference_descriptions_cache(ref_dir: str) -> dict[str, str]:
    """Load reference descriptions from cache json file if it exists."""
    cache_path = os.path.join(ref_dir, "descriptions.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_reference_descriptions_cache(ref_dir: str, cache: dict[str, str]):
    """Save reference descriptions to cache json file."""
    cache_path = os.path.join(ref_dir, "descriptions.json")
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Error saving reference descriptions cache: {e}")

async def check_reference_description_with_openrouter(session: aiohttp.ClientSession, base64_image: str, filename: str) -> str:
    """Ask OpenRouter to describe a reference image once at startup."""
    prompt = (
        "Analyze this reference image of a clothing/footwear/outerwear item we want to match. "
        "Provide a highly descriptive, concise one-sentence description focusing on: "
        "item type (shoe/sneaker/jacket/shirt/coat/etc.), style, colour, key branding/logos, "
        "and unique design features (e.g. sole style, zippers, stitching, material). "
        "Respond with only the descriptive sentence and nothing else."
    )
    
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    }
                ]
            }
        ]
    }
    
    response = await call_openrouter(session, payload)
    if response.startswith("ERROR:") or not response:
        print(f"⚠️ Error describing reference {filename}: {response}")
        return "Clothing item depicted in the reference folder."
    return response

async def check_image_with_openrouter(
    session: aiohttp.ClientSession,
    image_bytes: bytes,
    title: str,
    category: str,
    references: list[dict] = None
) -> tuple[str, str]:
    """Send image bytes to OpenRouter Vision API for double-checking."""
    # Resize candidate image to max 400x400
    resized_candidate = resize_image(image_bytes, max_size=400)
    candidate_b64 = base64.standard_b64encode(resized_candidate).decode("utf-8")
    
    formatted_prompt = get_openrouter_prompt(title, category)
    
    content_list = []
    if references:
        content_list.append({
            "type": "text",
            "text": f"Here are reference images of the target {category} items we are looking for:"
        })
        for ref in references:
            content_list.append({
                "type": "text",
                "text": f"Reference Image ({ref['name']}):"
            })
            content_list.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{ref['data']}"
                }
            })
        content_list.append({
            "type": "text",
            "text": "Compare the candidate image below with the reference images above. Does it match any of them?"
        })
        
    content_list.append({
        "type": "text",
        "text": "Now look at this candidate image of the item to verify:"
    })
    content_list.append({
        "type": "image_url",
        "image_url": {
            "url": f"data:image/jpeg;base64,{candidate_b64}"
        }
    })
    content_list.append({
        "type": "text",
        "text": formatted_prompt
    })
    
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": content_list
            }
        ]
    }
    
    response_text = await call_openrouter(session, payload)
    
    if response_text.startswith("ERROR:"):
        return "ERROR", response_text
        
    verdict = "REJECT"
    reasoning = "Could not parse response."
    for line in response_text.splitlines():
        if line.startswith("VERDICT:"):
            verdict = line.split("VERDICT:", 1)[-1].strip().upper()
        elif line.startswith("REASONING:"):
            reasoning = line.split("REASONING:", 1)[-1].strip()
            
    return verdict, reasoning


def get_paginated_url(url: str, page: int) -> str:
    """Format Yupoo URLs dynamically for pagination."""
    if "page=" in url:
        return re.sub(r'([?&])page=\d+', r'\g<1>page=' + str(page), url)
    if "?" in url:
        return f"{url}&page={page}"
    return f"{url}?page={page}"

def classify_reference_category(desc: str, filename: str) -> str:
    """Classify the reference image into 'shoes', 'coats', or 'clothing' based on its description."""
    desc_lower = desc.lower()

    # ── Shoes ────────────────────────────────────────────────────────────────
    if any(w in desc_lower for w in [
        "shoe", "shoes", "sneaker", "sneakers", "cleat", "cleats",
        "dunk", "jordan", "yeezy", "footwear", "sole", "heel",
        "air force", "new balance", "asics",
    ]):
        return "shoes"

    # ── Coats / heavy outerwear ──────────────────────────────────────────────
    # Nike puffer / NOCTA down jackets
    if any(w in desc_lower for w in [
        "down jacket", "puffer", "parka", "heavy jacket",
        "moncler", "canada goose", "north face",
        "cp company", "nocta cardinal", "nocta sunset",
        "carhartt", "detroit jacket", "active jacket",
    ]):
        return "coats"
    # Stone Island puffers specifically (not knitwear/hoodies)
    if "stone island" in desc_lower and any(w in desc_lower for w in [
        "puffer", "down", "jacket", "windbreaker", "overshirt",
    ]):
        return "coats"
    # Generic coat/jacket description
    if any(w in desc_lower for w in ["coat", "coats"]):
        return "coats"

    # ── Clothing (default) ───────────────────────────────────────────────────
    return "clothing"


SCRAPER_CHECKPOINT_JSON = os.path.join(OUTPUT_DIR, "scraper_checkpoint.json")

def load_checkpoint() -> tuple[int, int]:
    """Load the current source index and page number from checkpoint file."""
    if os.path.exists(SCRAPER_CHECKPOINT_JSON):
        try:
            with open(SCRAPER_CHECKPOINT_JSON, "r") as f:
                data = json.load(f)
                return data.get("current_source_index", 0), data.get("current_page", 1)
        except Exception:
            pass
    return 0, 1

def save_checkpoint(source_idx: int, page: int):
    """Save the current source index and page number."""
    try:
        with open(SCRAPER_CHECKPOINT_JSON, "w") as f:
            json.dump({"current_source_index": source_idx, "current_page": page}, f)
    except Exception as e:
        print(f"⚠️ Error saving scraper checkpoint: {e}")

def extract_style_code(title: str) -> str:
    """Extract standard style code (e.g. HV3364, DA3861) from clothing title."""
    m = re.search(r'\b[a-zA-Z]{1,2}\d{4,5}(?:-\d{3})?\b', title)
    if m:
        return m.group(0).upper()
    return None

def generate_filename(title: str, src: str) -> str:
    """Generate a clean filename using the style code and a short hash of the URL."""
    style_code = extract_style_code(title)
    url_hash = hashlib.md5(src.encode()).hexdigest()[:6]
    if style_code:
        # e.g. "IO0559-072_a1b2c3.jpg"
        return f"{style_code}_{url_hash}.jpg"
    else:
        return f"{url_hash}.jpg"

def load_accepted_style_codes(matches_file: str) -> set[str]:
    """Parse matches.txt to extract style codes of already accepted items."""
    style_codes = set()
    if not os.path.exists(matches_file):
        return style_codes
    try:
        with open(matches_file, "r", encoding="utf-8") as f:
            content = f.read()
        # Find entries that have VERDICT: ACCEPT and extract style codes from Title
        pattern = re.compile(r"Title:\s*(.*?)\s*\|\s*VERDICT:\s*ACCEPT")
        for match in pattern.finditer(content):
            title = match.group(1)
            code = extract_style_code(title)
            if code:
                style_codes.add(code)
    except Exception as e:
        print(f"⚠️ Error loading accepted style codes: {e}")
    return style_codes

def load_accepted_hashes() -> set[str]:
    """Calculate and return MD5 hashes of all images in the Accepted directory."""
    hashes = set()
    if os.path.exists(ACCEPTED_DIR):
        for f in os.listdir(ACCEPTED_DIR):
            if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                try:
                    with open(os.path.join(ACCEPTED_DIR, f), "rb") as img_file:
                        hashes.add(hashlib.md5(img_file.read()).hexdigest())
                except Exception:
                    pass
    return hashes


async def analyze_and_save_image(
    session: aiohttp.ClientSession,
    url: str,
    img_name: str,
    title: str,
    page_num: int,
    sem: asyncio.Semaphore,
    match_counter: list,
    category: str,
    references: list = None,
    accepted_hashes: set = None,
    accepted_style_codes: set = None,
    ref_desc_text: str = None,
) -> tuple[bool, float]:
    """Download image, run OpenRouter verification, and save matching items."""
    async with sem:
        headers = {
            "Referer": "https://fuzhuangpifa1234.x.yupoo.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        for attempt in range(3):
            try:
                async with session.get(url, headers=headers, timeout=15) as response:
                    if response.status != 200:
                        if attempt == 2:
                            print(f"  [SKIP] {img_name} — HTTP {response.status}")
                        await asyncio.sleep(1)
                        continue

                    content = await response.read()

                    # Deduplication / Double checking:
                    # 1. Image hash check (exact image duplicates)
                    img_hash = hashlib.md5(content).hexdigest()
                    if accepted_hashes is not None and img_hash in accepted_hashes:
                        print(f"  [DUP SKIP] {img_name} — Exact duplicate image already accepted.")
                        mark_url_processed(url)
                        return False, 0.0

                    # 2. Style code check (prevent duplicate listings of the same item)
                    style_code = extract_style_code(title)
                    if accepted_style_codes is not None and style_code and style_code in accepted_style_codes:
                        print(f"  [DUP SKIP] {img_name} — Style code {style_code} already accepted.")
                        mark_url_processed(url)
                        return False, 0.0

                    # 1. OpenRouter Check
                    print(f"  🔍 [CHECK] {img_name} — Verifying with OpenRouter...")
                    verdict, openrouter_reason = await check_image_with_openrouter(
                        session, content, title, category, references
                    )
                    
                    is_match = False
                    if verdict == "ACCEPT":
                        is_match = True
                        mark_url_processed(url)
                        match_counter[0] += 1
                        save_path = os.path.join(ACCEPTED_DIR, img_name)
                        with open(save_path, "wb") as f:
                            f.write(content)
                            
                        # Save in duplicate checking filters
                        if accepted_hashes is not None:
                            accepted_hashes.add(img_hash)
                        if accepted_style_codes is not None and style_code:
                            accepted_style_codes.add(style_code)
                            
                        print(f"  ✅ [ACCEPTED] {img_name} — OpenRouter: {openrouter_reason}")
                        log_entry = (
                            f"Page: {page_num} | Image: {img_name} | Title: {title} | VERDICT: ACCEPT\n"
                            f"OpenRouter: {openrouter_reason}\n\n"
                        )
                    elif verdict == "REJECT":
                        is_match = False
                        mark_url_processed(url)
                        save_path = os.path.join(REJECTED_DIR, img_name)
                        with open(save_path, "wb") as f:
                            f.write(content)
                            
                        print(f"  ❌ [REJECTED] {img_name} — OpenRouter: {openrouter_reason}")
                        log_entry = (
                            f"Page: {page_num} | Image: {img_name} | Title: {title} | VERDICT: REJECT\n"
                            f"OpenRouter: {openrouter_reason}\n\n"
                        )
                    else:
                        # Error fallback — save to standard filtered output
                        # Note: We do NOT mark URL processed so it gets retried on the next run!
                        save_path = os.path.join(OUTPUT_DIR, img_name)
                        with open(save_path, "wb") as f:
                            f.write(content)
                            
                        print(f"  ⚠️ [API ERROR] {img_name} — {openrouter_reason} (saved to {OUTPUT_DIR}/)")
                        log_entry = (
                            f"Page: {page_num} | Image: {img_name} | Title: {title} | VERDICT: ERROR\n"
                            f"OpenRouter Error: {openrouter_reason}\n\n"
                        )

                    # Append immediately to matches.txt
                    with open(
                        os.path.join(OUTPUT_DIR, "matches.txt"), "a", encoding="utf-8"
                    ) as log_file:
                        log_file.write(log_entry)

                    await asyncio.sleep(1.2)  # Be polite to stay under the limit
                    return is_match, 0.0

            except Exception as e:
                # Detect rate limit errors (HTTP 429)
                is_rate_limit = False
                if hasattr(e, "status_code") and e.status_code == 429:
                    is_rate_limit = True
                elif "rate_limit" in str(e).lower() or "429" in str(e):
                    is_rate_limit = True

                if is_rate_limit:
                    print(f"  [RATE LIMIT] Exceeded OpenRouter limit. Sleeping 15s before retrying {img_name}...")
                    await asyncio.sleep(15)
                else:
                    if attempt == 2:
                        print(f"  [ERROR] {img_name} after 3 attempts: {e}")
                    await asyncio.sleep(2)
        return None, 0.0 # Error fallback


async def run(playwright) -> None:
    print("Launching browser...")
    is_mac = sys.platform == "darwin"
    is_headless = os.getenv("HEADLESS", str(not is_mac)).lower() == "true"
    browser = await playwright.chromium.launch(headless=is_headless)
    context = await browser.new_context()
    page = await context.new_page()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(ACCEPTED_DIR, exist_ok=True)
    os.makedirs(REJECTED_DIR, exist_ok=True)

    # Load processed URLs for checkpoint / resume capability
    seen_urls = load_processed_urls()
    references = load_references_b64()

    # Initialize duplicate checking filters
    matches_file = os.path.join(OUTPUT_DIR, "matches.txt")
    accepted_hashes = load_accepted_hashes()
    accepted_style_codes = load_accepted_style_codes(matches_file)
    print(f"🛡️ Loaded {len(accepted_hashes)} accepted image hashes and {len(accepted_style_codes)} style codes to prevent duplicates.")

    # Initialise log file (append mode or create if not exists)
    if not os.path.exists(matches_file):
        with open(matches_file, "w", encoding="utf-8") as f:
            f.write("=== YUPOO MATCHES ===\n\n")

    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    match_counter = [0]  # Mutable container so threads can increment it
    total_spent = 0.0
    pages_scraped = 0

    start_source_idx, start_page = load_checkpoint()
    print(f"🔄 Resuming from Source Index {start_source_idx}, Page {start_page}")

    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Describe reference images with OpenRouter if not cached
        if references:
            cache = load_reference_descriptions_cache(REF_DIR)
            
            # Load and resolve all descriptions silently at startup
            for ref in references:
                name = ref["name"]
                if name in cache:
                    desc = cache[name]
                else:
                    print(f"  🔍 Describing reference image: {name} via OpenRouter...")
                    desc = await check_reference_description_with_openrouter(session, ref["data"], name)
                    if "Error generating description" not in desc and desc != "Clothing item depicted in the reference folder.":
                        cache[name] = desc
                        save_reference_descriptions_cache(REF_DIR, cache)
                    await asyncio.sleep(1.0)
                
                ref["description"] = desc
                ref["category"] = classify_reference_category(desc, name)

        for source_idx in range(start_source_idx, len(SOURCES)):
            source = SOURCES[source_idx]
            base_url = source["url"]
            category = source["category"]
            password = source["password"]

            # Filter references and build ref descriptions for this source's category
            category_references = [ref for ref in references if ref.get("category") == category]
            category_ref_descs = [f"- '{ref['name']}': {ref['description']}" for ref in category_references]
            category_ref_desc_text = "\n".join(category_ref_descs) if category_ref_descs else None

            print(f"\n🚀 Source {source_idx + 1}/{len(SOURCES)} ── [{category.upper()}] ── {base_url}")
            print(f"📚 Active target reference descriptions for category '{category}':")
            for ref in category_references:
                print(f"  -> '{ref['name']}': {ref['description']}")

            page_num = start_page if source_idx == start_source_idx else 1

            while True:
                url = get_paginated_url(base_url, page_num)
                print(f"\n── Source {source_idx} Page {page_num} ── {url}")

                try:
                    await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    print(f"Navigation failed for {url}: {e}")
                    # Try once more
                    await asyncio.sleep(2)
                    try:
                        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    except Exception as e2:
                        print(f"Navigation retry also failed, stopping pages for this source: {e2}")
                        break

                # Handle password prompt (only appears occasionally)
                try:
                    pwd_input = await page.wait_for_selector(
                        'input[type="password"]', timeout=2000
                    )
                    if pwd_input:
                        print("  Password prompt detected — entering...")
                        current_password = password if password else DEFAULT_PASSWORD
                        await pwd_input.fill(current_password)
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(1000)
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=10000)
                        except:
                            pass
                        await page.wait_for_timeout(3000)
                except Exception:
                    pass  # No password prompt on this page

                # Scroll to trigger lazy-loaded images
                try:
                    for step in range(1, 6):
                        await page.evaluate(
                            f"window.scrollTo(0, document.body.scrollHeight * ({step} / 5))"
                        )
                        await page.wait_for_timeout(400)
                except Exception as e:
                    print(f"  Scroll skipped: {e}")

                # Extract image src + title pairs
                images_data = await page.evaluate("""() => {
                    const results = [];
                    document.querySelectorAll('img').forEach(img => {
                        let src = img.getAttribute('data-src') || img.getAttribute('src');
                        if (!src) return;

                        let title = '';
                        
                        // 1. Try to find the closest anchor tag container (representing the album link)
                        const albumAnchor = img.closest('a');
                        if (albumAnchor) {
                            if (albumAnchor.getAttribute('title')) {
                                title = albumAnchor.getAttribute('title').trim();
                            }
                            if (!title) {
                                const titleEls = albumAnchor.querySelectorAll('.album__title, .showindex__title, .text_overflow');
                                for (const el of titleEls) {
                                    const text = el.innerText.trim();
                                    if (text && !/^\\d+$/.test(text)) { // skip if it's just the photo count badge (number)
                                        title = text;
                                        break;
                                    }
                                }
                            }
                        }

                        // 2. Fallback to general parent traversal if title not found
                        if (!title) {
                            let parent = img.parentElement;
                            while (parent && parent !== document.body) {
                                if (parent.getAttribute('title')) {
                                    const val = parent.getAttribute('title').trim();
                                    if (val && !/^\\d+$/.test(val)) {
                                        title = val;
                                        break;
                                    }
                                }
                                const titleEls = parent.querySelectorAll('.album__title, .showindex__title, .text_overflow');
                                let found = false;
                                for (const el of titleEls) {
                                    const text = el.innerText.trim();
                                    if (text && !/^\\d+$/.test(text)) {
                                        title = text;
                                        found = true;
                                        break;
                                    }
                                }
                                if (found) break;
                                parent = parent.parentElement;
                            }
                        }
                        results.push({ src, title });
                    });
                    return results;
                }""")

                # Filter to valid Yupoo photo URLs on the page
                yupoo_photos = []
                for item in images_data:
                    src = item["src"]
                    title = item["title"]
                    if not src:
                        continue
                    if src.startswith("//"):
                        src = "https:" + src
                    if "photo.yupoo.com" in src:
                        yupoo_photos.append((src, title))

                # Pagination End Check: If no Yupoo photo images are found, we've reached the end of the source.
                if not yupoo_photos:
                    print(f"ℹ️ No Yupoo photos found on Page {page_num}. Ending pages for Source {source_idx}.")
                    break

                tasks = []
                valid_count = 0
                filtered_by_title = 0
                filtered_by_checkpoint = 0

                for src, title in yupoo_photos:
                    # 1. Title pre-filtering (skip if it contains no relevant keywords or style codes)
                    if not title_might_match(title, category):
                        filtered_by_title += 1
                        continue

                    # 2. Checkpoint & Deduplication check
                    if src in seen_urls:
                        filtered_by_checkpoint += 1
                        continue
                    seen_urls.add(src)

                    valid_count += 1
                    img_name = generate_filename(title, src)
                    tasks.append(
                        analyze_and_save_image(
                            session, src, img_name, title, page_num, sem, match_counter, category,
                            category_references, accepted_hashes, accepted_style_codes, category_ref_desc_text
                        )
                    )

                print(f"  Pre-filter skipped {filtered_by_title} images, checkpoint skipped {filtered_by_checkpoint} images.")
                if tasks:
                    print(f"  Sending {valid_count} new images to AI verification...")
                    results = await asyncio.gather(*tasks)
                    page_matches = sum(1 for r in results if r and r[0] is True)
                    page_unmatched = sum(1 for r in results if r and r[0] is False)
                    page_errors = sum(1 for r in results if r is None or r[0] is None)
                    page_cost = sum(r[1] for r in results if r and r[1] is not None)

                    total_spent += page_cost

                    err_str = f" ({page_errors} errors)" if page_errors > 0 else ""
                    print(f"  Page {page_num} complete: {page_matches} out of {valid_count} images matched ({page_unmatched} unmatched{err_str}).")
                    print(f"  API Usage: {render_cost_bar(total_spent)}")

                    # Enforce credit limit budget
                    if total_spent >= BUDGET:
                        print(f"\n⛔ Budget limit of ${BUDGET} reached. Stopping scraper.")
                        save_checkpoint(source_idx, page_num)
                        break
                else:
                    print("  No new images on this page to analyze.")

                pages_scraped += 1
                page_num += 1
                save_checkpoint(source_idx, page_num)
                print(f"  Done page. Total matches so far: {match_counter[0]} | Total spent: ${total_spent:.4f}")

            if total_spent >= BUDGET:
                break

            # Reset page_num to 1 for the next source, and update the checkpoint to start the next source
            save_checkpoint(source_idx + 1, 1)

    await browser.close()
    print(
        f"\n{'='*50}"
        f"\nScraping complete!"
        f"\n  Pages scraped : {pages_scraped}"
        f"\n  Total matches : {match_counter[0]}"
        f"\n  Total Spent   : ${total_spent:.4f}"
        f"\n  Saved to      : {OUTPUT_DIR}/"
        f"\n  Log file      : {OUTPUT_DIR}/matches.txt"
        f"\n{'='*50}"
    )


async def main() -> None:
    async with async_playwright() as playwright:
        await run(playwright)


if __name__ == "__main__":
    asyncio.run(main())
