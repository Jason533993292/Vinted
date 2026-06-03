"""
One-shot script: generates OpenRouter descriptions for any reference images
not yet present in references/descriptions.json, then saves the cache.
"""
import os
import json
import io
import time
import base64
import urllib.request
import urllib.error
from PIL import Image
from dotenv import load_dotenv

script_dir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(script_dir, ".env"))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")

if not OPENROUTER_API_KEY:
    raise SystemExit("❌ No OPENROUTER_API_KEY found in .env")

print(f"✅ OpenRouter API configured (Model: {OPENROUTER_MODEL})\n")

REF_DIR = os.path.join(script_dir, "references")
CACHE_PATH = os.path.join(REF_DIR, "descriptions.json")

# Load existing cache
cache: dict = {}
if os.path.exists(CACHE_PATH):
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        pass

image_files = sorted(
    f for f in os.listdir(REF_DIR)
    if f.lower().endswith((".jpg", ".jpeg", ".png"))
)

missing = [f for f in image_files if f not in cache]
print(f"📚 {len(image_files)} reference images found, {len(missing)} need descriptions.\n")

PROMPT = (
    "Analyze this reference image of a clothing/footwear/outerwear item we want to match. "
    "Provide a highly descriptive, concise one-sentence description focusing on: "
    "item type (shoe/sneaker/jacket/shirt/coat/etc.), style, colour, key branding/logos, and unique design features "
    "(e.g. sole style, zippers, buttons, contrast stitching, material). "
    "Respond with only the descriptive sentence and nothing else."
)

def call_openrouter(payload: dict) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/phil/Resell",
        "X-Title": "Resell Reference Describer"
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    
    max_retries = 5
    backoff = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                choices = res_data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
                return ""
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                print(f"  ⚠️ OpenRouter HTTP error {e.code} on attempt {attempt}/{max_retries}. Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2.0
            else:
                body = e.read().decode("utf-8")
                print(f"  ❌ OpenRouter HTTP error {e.code}: {body}")
                return ""
        except Exception as e:
            print(f"  ⚠️ OpenRouter error: {e}. Retrying in {backoff}s...")
            time.sleep(backoff)
            backoff *= 2.0
    return ""

for i, filename in enumerate(missing, 1):
    path = os.path.join(REF_DIR, filename)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()

        # Skip tiny/corrupt images (< 5 KB — likely blank screenshots)
        if len(raw) < 5000:
            print(f"  [{i}/{len(missing)}] ⚠️  SKIPPED '{filename}' — file too small ({len(raw)} bytes).")
            cache[filename] = "Blank or corrupt reference image."
            continue

        # Downscale image to 400x400 to save bandwidth
        img = Image.open(io.BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((400, 400))
        
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        b64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")

        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}}
                    ]
                }
            ]
        }

        print(f"  [{i}/{len(missing)}] 🔍 Generating description for '{filename}'...")
        desc = call_openrouter(payload)
        
        if desc:
            cache[filename] = desc
            print(f"  [{i}/{len(missing)}] ✅ '{filename}':\n      {desc}\n")
        else:
            print(f"  [{i}/{len(missing)}] ❌ Failed to get description for '{filename}'\n")

        # Save after every image so we don't lose progress
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=4, ensure_ascii=False)

        time.sleep(1.2)   # respect polite rate limits

    except Exception as e:
        print(f"  [{i}/{len(missing)}] ❌ OUTER ERROR '{filename}': {e}\n")
        time.sleep(2)

print("\n✅ All descriptions resolved. Cache saved to references/descriptions.json")
