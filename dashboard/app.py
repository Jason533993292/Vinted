import os
import json
import subprocess
import sys
import shutil
import asyncio
import base64
import io
import aiohttp
from PIL import Image
from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# Load environment
script_dir = os.path.abspath(os.path.dirname(__file__))
root_dir = os.path.abspath(os.path.join(script_dir, ".."))
dotenv_path = os.path.join(root_dir, ".env")
load_dotenv(dotenv_path)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")

app = FastAPI(title="Yupoo Resell Scraper Dashboard")

# Enable CORS for convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure essential directories exist
os.makedirs(os.path.join(root_dir, "Accepted"), exist_ok=True)
os.makedirs(os.path.join(root_dir, "Rejected"), exist_ok=True)
os.makedirs(os.path.join(root_dir, "references"), exist_ok=True)
os.makedirs(os.path.join(root_dir, "filtered_yupoo_images"), exist_ok=True)

# Mount directories as static endpoints
app.mount("/static/Accepted", StaticFiles(directory=os.path.join(root_dir, "Accepted")), name="accepted")
app.mount("/static/Rejected", StaticFiles(directory=os.path.join(root_dir, "Rejected")), name="rejected")
app.mount("/static/references", StaticFiles(directory=os.path.join(root_dir, "references")), name="references")
app.mount("/static/filtered_yupoo_images", StaticFiles(directory=os.path.join(root_dir, "filtered_yupoo_images")), name="filtered_yupoo_images")

# Global tracking for active scraper subprocess
scraper_process = None

def get_scraper_status():
    global scraper_process
    if scraper_process is None:
        return "stopped"
    poll = scraper_process.poll()
    if poll is None:
        return "running"
    else:
        scraper_process = None
        return "stopped"

def classify_reference_category_local(desc: str) -> str:
    desc_lower = desc.lower()
    if any(w in desc_lower for w in [
        "shoe", "shoes", "sneaker", "sneakers", "cleat", "cleats",
        "dunk", "jordan", "yeezy", "footwear", "sole", "heel",
        "air force", "new balance", "asics",
    ]):
        return "shoes"
    if any(w in desc_lower for w in [
        "down jacket", "puffer", "parka", "heavy jacket",
        "moncler", "canada goose", "north face",
        "cp company", "nocta cardinal", "nocta sunset",
        "carhartt", "detroit jacket", "active jacket",
    ]) or "stone island" in desc_lower and any(w in desc_lower for w in [
        "puffer", "down", "jacket", "windbreaker", "overshirt",
    ]) or any(w in desc_lower for w in ["coat", "coats"]):
        return "coats"
    return "clothing"

async def generate_reference_description(filename: str) -> str:
    ref_path = os.path.join(root_dir, "references", filename)
    if not os.path.exists(ref_path):
        return "Reference image"
    try:
        # Resize to max 400x400 to stay light
        img = Image.open(ref_path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((400, 400))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        resized_bytes = buffer.getvalue()
        b64_data = base64.standard_b64encode(resized_bytes).decode("utf-8")
        
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
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}}
                    ]
                }
            ]
        }
        
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/phil/Resell",
            "X-Title": "Resell Scraper Dashboard"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"Error auto-describing {filename}: {e}")
    return "Newly uploaded clothing item."

# --- API Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(script_dir, "templates", "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="index.html template not found")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/status")
async def get_status():
    status = get_scraper_status()
    
    # Counts
    accepted_dir = os.path.join(root_dir, "Accepted")
    rejected_dir = os.path.join(root_dir, "Rejected")
    references_dir = os.path.join(root_dir, "references")
    
    accepted_count = len([f for f in os.listdir(accepted_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
    rejected_count = len([f for f in os.listdir(rejected_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
    references_count = len([f for f in os.listdir(references_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
    
    return {
        "status": status,
        "pid": scraper_process.pid if scraper_process else None,
        "metrics": {
            "accepted": accepted_count,
            "rejected": rejected_count,
            "references": references_count
        }
    }

@app.post("/api/scraper/start")
async def start_scraper_api():
    global scraper_process
    status = get_scraper_status()
    if status == "running":
        return {"status": "already running", "pid": scraper_process.pid}
        
    log_path = os.path.join(root_dir, "filtered_yupoo_images", "scraper_run.log")
    log_file = open(log_path, "w", encoding="utf-8")
    
    cmd = [sys.executable, "-u", os.path.join(root_dir, "yupoo_scraper.py")]
    
    scraper_process = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=root_dir,
        text=True
    )
    return {"status": "started", "pid": scraper_process.pid}

@app.post("/api/scraper/stop")
async def stop_scraper_api():
    global scraper_process
    status = get_scraper_status()
    if status == "stopped":
        return {"status": "not running"}
        
    scraper_process.terminate()
    try:
        scraper_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        scraper_process.kill()
        scraper_process.wait()
        
    scraper_process = None
    return {"status": "stopped"}

@app.get("/api/scraper/logs")
async def get_logs():
    log_path = os.path.join(root_dir, "filtered_yupoo_images", "scraper_run.log")
    if not os.path.exists(log_path):
        return {"logs": "No logs found yet. Start the scraper to generate logs."}
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            # Return last 150 lines
            return {"logs": "".join(lines[-150:])}
    except Exception as e:
        return {"logs": f"Error reading logs: {e}"}

@app.get("/api/gallery")
async def get_gallery():
    matches_path = os.path.join(root_dir, "filtered_yupoo_images", "matches.txt")
    if not os.path.exists(matches_path):
        return []
        
    items = []
    try:
        with open(matches_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        entries = content.split("\n\n")
        for entry in entries:
            entry = entry.strip()
            if not entry or entry.startswith("=== YUPOO MATCHES"):
                continue
                
            lines = entry.split("\n")
            header_line = lines[0]
            reason_line = lines[1] if len(lines) > 1 else ""
            
            page_num = ""
            img_name = ""
            title = ""
            verdict = ""
            
            parts = header_line.split("|")
            for part in parts:
                part = part.strip()
                if part.startswith("Page:"):
                    page_num = part.replace("Page:", "").strip()
                elif part.startswith("Image:"):
                    img_name = part.replace("Image:", "").strip()
                elif part.startswith("Title:"):
                    title = part.replace("Title:", "").strip()
                elif part.startswith("VERDICT:"):
                    verdict = part.replace("VERDICT:", "").strip()
            
            reasoning = reason_line.strip()
            for prefix in ["OpenRouter:", "Gemini:", "Claude:", "Gemini Error:", "OpenRouter Error:"]:
                if reasoning.startswith(prefix):
                    reasoning = reasoning[len(prefix):].strip()
                    
            if img_name:
                # Resolve local server URL for image
                if verdict == "ACCEPT":
                    img_url = f"/static/Accepted/{img_name}"
                elif verdict == "REJECT":
                    img_url = f"/static/Rejected/{img_name}"
                else:
                    img_url = f"/static/filtered_yupoo_images/{img_name}"
                    
                # Categorize item using title keywords as fallback
                item_cat = "clothing"
                title_lower = title.lower()
                if any(k in title_lower for k in ["shoe", "dunk", "nb", "new balance", "asics", "force", "af1", "pegasus", "vomero", "cortez", "air max"]):
                    item_cat = "shoes"
                elif any(k in title_lower for k in ["jacket", "coat", "down", "puffer", "parka", "windbreaker"]):
                    item_cat = "coats"
                
                items.append({
                    "page": page_num,
                    "image": img_name,
                    "title": title,
                    "verdict": verdict,
                    "reasoning": reasoning,
                    "url": img_url,
                    "category": item_cat
                })
    except Exception as e:
        print(f"Error parsing matches.txt: {e}")
        
    return list(reversed(items))

@app.get("/api/references")
async def get_references():
    ref_dir = os.path.join(root_dir, "references")
    cache_path = os.path.join(ref_dir, "descriptions.json")
    
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass
            
    ref_list = []
    if os.path.exists(ref_dir):
        files = sorted(os.listdir(ref_dir))
        for f in files:
            if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                desc = cache.get(f, "No description available.")
                category = classify_reference_category_local(desc)
                ref_list.append({
                    "filename": f,
                    "description": desc,
                    "category": category,
                    "url": f"/static/references/{f}"
                })
    return ref_list

@app.post("/api/references/upload")
async def upload_ref(file: UploadFile = File(...)):
    ref_dir = os.path.join(root_dir, "references")
    os.makedirs(ref_dir, exist_ok=True)
    
    file_path = os.path.join(ref_dir, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Auto-generate description
    desc = await generate_reference_description(file.filename)
    
    cache_path = os.path.join(ref_dir, "descriptions.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass
            
    cache[file.filename] = desc
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving description: {e}")
        
    return {
        "status": "success",
        "filename": file.filename,
        "description": desc,
        "category": classify_reference_category_local(desc),
        "url": f"/static/references/{file.filename}"
    }

class DeleteRefRequest(BaseModel):
    filename: str

@app.post("/api/references/delete")
async def delete_ref(req: DeleteRefRequest):
    ref_dir = os.path.join(root_dir, "references")
    file_path = os.path.join(ref_dir, req.filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        
    cache_path = os.path.join(ref_dir, "descriptions.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if req.filename in cache:
                del cache[req.filename]
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error removing description: {e}")
            
    return {"status": "success"}

class UpdateDescRequest(BaseModel):
    filename: str
    description: str

@app.post("/api/references/update-description")
async def update_desc(req: UpdateDescRequest):
    ref_dir = os.path.join(root_dir, "references")
    cache_path = os.path.join(ref_dir, "descriptions.json")
    
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass
            
    cache[req.filename] = req.description
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error updating description: {e}")
        
    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
