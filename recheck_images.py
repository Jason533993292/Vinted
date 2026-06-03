import os
import re
import shutil
import time
import json
import base64
import io
import urllib.request
import urllib.error
from PIL import Image
from dotenv import load_dotenv

# Load env variables relative to script location
script_dir = os.path.abspath(os.path.dirname(__file__))
dotenv_path = os.path.join(script_dir, ".env")
load_dotenv(dotenv_path)

# ── Config ────────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.5-flash")

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY not found in environment. Please add it to .env file.")
    
SOURCE_DIR = os.path.join(script_dir, "filtered_yupoo_images")
ACCEPTED_DIR = os.path.join(script_dir, "Accepted")
REJECTED_DIR = os.path.join(script_dir, "Rejected")

print(f"✅ OpenRouter API configured (Model: {OPENROUTER_MODEL})")
# ──────────────────────────────────────────────────────────────────────────────

PROMPT = """Look at this clothing item image.
The seller's title for this item is: "{title}"

We want to verify if it is EXACTLY one of the following:
1. Nike Nocta jacket or windbreaker (usually black, yellow, sail, or tan, with NOCTA branding or sleek design)
2. Adidas Chinese New Year jacket/jumper (traditional Chinese knot/frog buttons down the front, usually 3 stripes on the sleeves)
3. Black Under Armour t-shirt with a shiny/iridescent logo

Be strict. If the item is shorts, pants, generic Nike hoodies (not Nocta), or generic Adidas/UA shirts, it should be REJECTED.

Respond in this exact format and nothing else:
VERDICT: ACCEPT or REJECT
REASONING: <brief explanation of what you see and why you chose the verdict>"""

def parse_matches(matches_file: str) -> dict[str, str]:
    """Parse matches.txt to map image filenames to their original seller titles."""
    img_to_title = {}
    if not os.path.exists(matches_file):
        print(f"⚠️ Log file {matches_file} not found. Running validation without title context.")
        return img_to_title

    with open(matches_file, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(r"Image:\s*(\S+)\s*\|\s*Title:\s*(.*)")
    for line in content.splitlines():
        m = pattern.search(line)
        if m:
            img_name = m.group(1).strip()
            # If the line contains other fields separated by |, extract only the title
            title = m.group(2).split("|")[0].strip()
            img_to_title[img_name] = title
            
    return img_to_title

def load_references() -> list[tuple[str, str]]:
    """Load reference image paths from a 'references' folder if it exists."""
    ref_dir = os.path.join(script_dir, "references")
    if not os.path.exists(ref_dir) and os.path.exists(os.path.join(script_dir, "reference images")):
        ref_dir = os.path.join(script_dir, "reference images")
        
    ref_data = []
    if os.path.exists(ref_dir):
        files = sorted(os.listdir(ref_dir))
        for f in files:
            if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                ref_data.append((f, os.path.join(ref_dir, f)))
        if ref_data:
            print(f"📚 Loaded {len(ref_data)} reference images from '{ref_dir}' folder.")
    return ref_data

def get_image_base64(path: str) -> str:
    """Read image file, downscale, and convert to base64."""
    try:
        img = Image.open(path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((400, 400))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"  ⚠️ Error base64 encoding {path}: {e}")
        return ""

def call_openrouter(payload: dict) -> str:
    """Make a synchronous POST request to OpenRouter API with retries."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/phil/Resell",
        "X-Title": "Resell Scraper Image Rechecker"
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
                print(f"  ⚠️ OpenRouter error {e.code} on attempt {attempt}/{max_retries}. Retrying in {backoff}s...")
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

def check_image_with_openrouter(image_path: str, title: str, references: list[tuple[str, str]] = None) -> tuple[str, str]:
    """Send image to OpenRouter Vision API for double-checking."""
    formatted_prompt = PROMPT.format(title=title)
    
    content_list = []
    if references:
        content_list.append({
            "type": "text",
            "text": "Here are reference images of the target items we are looking for:"
        })
        for ref_name, ref_path in references:
            b64_ref = get_image_base64(ref_path)
            if b64_ref:
                content_list.append({
                    "type": "text",
                    "text": f"Reference Image ({ref_name}):"
                })
                content_list.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64_ref}"
                    }
                })
        content_list.append({
            "type": "text",
            "text": "Compare the candidate image below with the reference images above. Does it match any of them?"
        })
        
    b64_candidate = get_image_base64(image_path)
    if not b64_candidate:
        return "ERROR", "Failed to load candidate image"
        
    content_list.append({
        "type": "text",
        "text": "Now look at this candidate image of the clothing item to verify:"
    })
    content_list.append({
        "type": "image_url",
        "image_url": {
            "url": f"data:image/jpeg;base64,{b64_candidate}"
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
    
    response_text = call_openrouter(payload)
    if not response_text:
        return "ERROR", "Empty response from API"
        
    verdict = "REJECT"
    reasoning = "Could not parse response."
    
    for line in response_text.splitlines():
        if line.startswith("VERDICT:"):
            verdict = line.split("VERDICT:", 1)[-1].strip().upper()
        elif line.startswith("REASONING:"):
            reasoning = line.split("REASONING:", 1)[-1].strip()
            
    return verdict, reasoning

def main():
    os.makedirs(ACCEPTED_DIR, exist_ok=True)
    os.makedirs(REJECTED_DIR, exist_ok=True)

    matches_file = os.path.join(SOURCE_DIR, "matches.txt")
    img_to_title = parse_matches(matches_file)
    references = load_references()

    # Get all jpg files in source directory
    images = [f for f in os.listdir(SOURCE_DIR) if f.lower().endswith(('.jpg', '.jpeg'))]
    if not images:
        print(f"No images found in '{SOURCE_DIR}' to check.")
        return

    print(f"🔍 Found {len(images)} images in '{SOURCE_DIR}' to re-check using OpenRouter...")

    accepted_count = 0
    rejected_count = 0

    for idx, img_name in enumerate(images, 1):
        image_path = os.path.join(SOURCE_DIR, img_name)
        title = img_to_title.get(img_name, "Unknown Title")

        print(f"\n[{idx}/{len(images)}] Checking {img_name}...")
        print(f"  Title: {title}")

        verdict, reasoning = check_image_with_openrouter(image_path, title, references)

        if verdict == "ACCEPT":
            dest_path = os.path.join(ACCEPTED_DIR, img_name)
            shutil.move(image_path, dest_path)
            accepted_count += 1
            print(f"  ✅ [ACCEPTED] -> Moved to {ACCEPTED_DIR}/")
            print(f"  Reason: {reasoning}")
        elif verdict == "REJECT":
            dest_path = os.path.join(REJECTED_DIR, img_name)
            shutil.move(image_path, dest_path)
            rejected_count += 1
            print(f"  ❌ [REJECTED] -> Moved to {REJECTED_DIR}/")
            print(f"  Reason: {reasoning}")
        else:
            print(f"  ⚠️ [ERROR] Skipping verification for this image: {reasoning}")
            
        time.sleep(1.2) # Polite request spacer

    print(f"\n{'='*50}")
    print("Verification complete!")
    print(f"  Accepted items: {accepted_count} (moved to {ACCEPTED_DIR}/)")
    print(f"  Rejected items: {rejected_count} (moved to {REJECTED_DIR}/)")
    print("="*50)

if __name__ == "__main__":
    main()
