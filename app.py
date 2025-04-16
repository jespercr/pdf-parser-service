from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright
from pathlib import Path
from urllib.parse import urlparse
import urllib.robotparser
from utils.robots import is_scraping_allowed
from flask_cors import CORS
import os
import fitz  # PyMuPDF
import requests
import pdfplumber
import logging
import sys
import traceback
from datetime import datetime
from bs4 import BeautifulSoup
import re

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


# === CONFIG ===
RAILS_BASE_URL = "https://workplacerback.onrender.com" 
ORIGIN_URL = "https://workplacer-micro.onrender.com"

# Check both potential Chromium locations
PLAYWRIGHT_PATHS = [
    "/ms-playwright/chromium-1161/chrome-linux/chrome",  # Render's possible location
    str(Path.home() / ".cache/ms-playwright/chromium-1161/chrome-linux/chrome"),  # Local dev location
]

# Scraping configs
PAGE_TIMEOUT = 15000  # 15 seconds
NAVIGATION_TIMEOUT = 10000  # 10 seconds
MAX_SCRAPE_TIME = 10  # 10 seconds total max time
# ==============

@app.before_request
def log_request_info():
    logger.info("=== New Request ===")
    logger.info(f"Method: {request.method}")
    logger.info(f"URL: {request.url}")
    logger.info(f"Headers: {dict(request.headers)}")
    if request.is_json:
        logger.info(f"JSON Body: {request.get_json()}")
    logger.info("==================")

def parse_pdf_text(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
            text += "\n\n"
    return text.strip()


def extract_images_from_pdf(pdf_path, output_dir="/tmp/pdf_images"):
    os.makedirs(output_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    image_paths = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        images = page.get_images(full=True)

        for i, img in enumerate(images):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            ext = base_image["ext"]
            img_filename = f"page{page_num+1}_img{i+1}.{ext}"
            img_path = os.path.join(output_dir, img_filename)

            with open(img_path, "wb") as f:
                f.write(image_bytes)

            image_paths.append(img_path)

    return image_paths


def send_images_to_rails(image_paths, space_id):
    url = f"{RAILS_BASE_URL}/api/v1/spaces/{space_id}/addimages"
    files = [("imgs[]", open(path, "rb")) for path in image_paths]
    headers = {'Origin': ORIGIN_URL}

    try:
        response = requests.post(url, files=files, headers=headers)
        for _, f in files:
            f.close()
        return response.json() if response.status_code == 200 else {"error": response.text}
    except Exception as e:
        return {"error": str(e)}

# === ROBOTS.TXT + SCRAPE ===
def is_scraping_allowed(url):
    parsed_url = urlparse(url)
    robots_url = f"{parsed_url.scheme}://{parsed_url.netloc}/robots.txt"

    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)

    try:
        rp.read()
        return rp.can_fetch("*", url)
    except Exception as e:
        print(f"⚠️ robots.txt check failed: {e}")
        return False
   
def find_chromium_executable():
    # First check our predefined paths
    for path in PLAYWRIGHT_PATHS:
        if os.path.exists(path):
            print(f"✅ Chromium executable found at predefined path: {path}")
            return path

    # Fallback to searching in the cache directory
    base = Path.home() / ".cache/ms-playwright"
    print("🗂 Checking Chromium install path:", base)

    if not base.exists():
        print("⚠️ Base playwright directory not found")
        # Try to install playwright browsers
        os.system("playwright install chromium")

    folders = list(base.glob("chromium-*"))
    print("📁 Chromium folders found:", folders)

    for item in folders:
        executable = item / "chrome-linux/chrome"
        if executable.exists():
            print("✅ Chromium executable found at:", executable)
            return str(executable)

    print("❌ Chromium executable not found in any location")
    raise FileNotFoundError("Chromium executable not found. Please ensure Playwright browsers are installed.")

def scrape_with_playwright(url):
    """
    Simple, direct scraping function that avoids timeouts by limiting steps and operations.
    """
    logger.info(f"🚀 Starting scrape for URL: {url}")
    executable_path = find_chromium_executable()
    logger.info(f"🎭 Using Chromium at: {executable_path}")
    
    start_time = datetime.now()
    browser = None
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                executable_path=executable_path
            )
            
            # Create minimal browser context
            context = browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            )
            
            # Create page with minimal timeouts
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT)

            # Navigate to page with shortest path to content
            logger.info(f"🌐 Navigating to URL: {url}")
            response = page.goto(url, wait_until="domcontentloaded")
            
            if not response or response.status >= 400:
                logger.error(f"❌ Page error: {response.status if response else 'No response'}")
                raise Exception(f"Failed to load page: {response.status if response else 'No response'}")
            
            # Simple cookie handling - only try to click most common buttons once
            try:
                selectors = [
                    'button:has-text("Accept")', 
                    'button:has-text("Acceptera")',
                    'button:has-text("Godkänn")',
                    'button:has-text("Tillåt")'
                ]
                
                for selector in selectors:
                    if page.locator(selector).count() > 0:
                        page.locator(selector).first.click(timeout=2000)
                        logger.info(f"✅ Clicked cookie button: {selector}")
                        break
            except Exception as e:
                logger.info(f"ℹ️ No cookie dialog or failed to handle: {str(e)}")
            
            # Wait 1 second for any content to load
            page.wait_for_timeout(1000)
            
            # Simple scroll to trigger lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            
            # Immediately capture HTML content
            logger.info("📥 Getting page content...")
            html_content = page.content()
            
            # Clean and extract
            cleaned_data = clean_html_response(html_content)
            
            # Close browser to free resources
            browser.close()
            browser = None
            
            # Report timing
            elapsed = (datetime.now() - start_time).total_seconds()
            logger.info(f"✅ Scraping completed in {elapsed:.2f} seconds")
            
            return {
                "success": True,
                "data": cleaned_data
            }
    
    except Exception as e:
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.error(f"🚨 Scraping error after {elapsed:.2f} seconds: {str(e)}")
        logger.error(f"Stack trace: {traceback.format_exc()}")
        raise Exception(f"Failed to scrape URL: {str(e)}")
        
    finally:
        # Ensure browser is closed
        if browser:
            try:
                browser.close()
                logger.info("🎭 Browser closed")
            except Exception:
                logger.error("Failed to close browser")

def clean_html_response(html_content):
    """
    Clean and structure HTML content using BeautifulSoup.
    Returns a dictionary containing cleaned title, description, and main content.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Remove script and style elements
    for element in soup(['script', 'style']):
        element.decompose()
    
    # Get title - prefer h1 over title tag
    title = ""
    h1 = soup.find('h1')
    if h1:
        title = h1.get_text(strip=True)
    elif soup.title:
        title = soup.title.string
    
    # Get meta description
    meta_desc = ""
    meta_tag = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', attrs={'property': 'og:description'})
    if meta_tag and meta_tag.get('content'):
        meta_desc = meta_tag['content']
    
    # Extract main content - use targeted approach for property listings
    content_text = ""
    
    # Try to find property-specific elements
    property_classes = ['property-info', 'property-details', 'listing-details', 'object-info']
    property_content = None
    
    for cls in property_classes:
        elements = soup.find_all(class_=lambda x: x and cls.lower() in x.lower())
        if elements:
            property_content = elements[0]
            break
    
    if property_content:
        # We found property content, use it
        content_text = property_content.get_text(separator=' ', strip=True)
    else:
        # Fallback: collect all paragraphs that are substantial
        paragraphs = []
        for p in soup.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > 30 and not any(term in text.lower() for term in ['cookie', 'consent', 'gdpr', 'villkor']):
                paragraphs.append(text)
        
        content_text = ' '.join(paragraphs)
    
    # If content is too small, try an alternative approach
    if len(content_text) < 100:
        # Get all divs with substantial text
        for div in soup.find_all('div'):
            text = div.get_text(strip=True)
            if len(text) > 200 and not any(term in text.lower() for term in ['cookie', 'consent', 'gdpr', 'villkor']):
                content_text = text
                break
    
    # Final cleaning
    content_text = re.sub(r'\s+', ' ', content_text).strip()
    
    return {
        "title": title,
        "description": meta_desc,
        "content": content_text
    }

@app.route("/scrape", methods=["POST"])
def scrape():
    request_start_time = datetime.now()
    logger.info(f"📨 Received scrape request at {request_start_time}")
    
    try:
        if not request.is_json:
            logger.error("❌ Request is not JSON")
            return jsonify({"error": "Content-Type must be application/json"}), 400
            
        data = request.get_json()
        logger.info(f"📝 Request data: {data}")
        
        url = data.get("url")
        if not url:
            logger.error("❌ No URL provided in request")
            return jsonify({"error": "No URL provided"}), 400

        logger.info(f"🔍 Checking if scraping is allowed for: {url}")
        if not is_scraping_allowed(url):
            logger.error("🚫 Scraping disallowed by robots.txt")
            return jsonify({"error": "Scraping disallowed by robots.txt"}), 403

        logger.info("🤖 Starting scraping process...")
        try:
            scraped_data = scrape_with_playwright(url)
            request_duration = (datetime.now() - request_start_time).total_seconds()
            logger.info(f"✅ Scraping completed successfully in {request_duration} seconds")
            
            return jsonify(scraped_data)
            
        except Exception as e:
            logger.error(f"🚨 Scraping error: {str(e)}")
            logger.error(f"Stack trace: {traceback.format_exc()}")
            return jsonify({"error": str(e)}), 500
            
    except Exception as e:
        error_msg = f"🚨 Request processing failed: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        return jsonify({"error": str(e)}), 500

@app.route("/parse", methods=["POST"])
def parse():
    file = request.files.get("file")
    space_id = request.form.get("space_id")

    if not file or not space_id:
        return jsonify({"error": "Missing file or space_id"}), 400

    file_path = f"/tmp/{file.filename}"
    file.save(file_path)

    try:
        # 1. Extract text
        parsed_text = parse_pdf_text(file_path)

        # 2. Extract and upload images
        image_paths = extract_images_from_pdf(file_path)
        image_upload_result = send_images_to_rails(image_paths, space_id)

        return jsonify({
            "text": parsed_text,
            "image_upload_result": image_upload_result
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        os.remove(file_path)