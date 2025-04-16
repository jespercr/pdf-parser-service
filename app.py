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
PAGE_TIMEOUT = 60000  # 60 seconds
NAVIGATION_TIMEOUT = 30000  # 30 seconds
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

def clean_html_response(html_content):
    """
    Clean and structure HTML content using BeautifulSoup.
    Returns a dictionary containing cleaned title, description, and main content.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Remove script, style elements and cookie-related content
    for element in soup(['script', 'style']):
        element.decompose()
    
    # Remove cookie-related elements
    for element in soup.find_all(class_=lambda x: x and any(term in str(x).lower() for term in ['cookie', 'consent', 'gdpr'])):
        element.decompose()
    
    # Get title
    title = soup.title.string if soup.title else ""
    
    # Get meta description
    meta_desc = ""
    meta_tag = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', attrs={'property': 'og:description'})
    if meta_tag and meta_tag.get('content'):
        meta_desc = meta_tag['content']
    
    # Try to find main content with more specific selectors
    main_content = ""
    
    # Priority list of content selectors
    content_selectors = [
        # Property specific selectors
        {'tag': 'div', 'class_': lambda x: x and any(term in str(x).lower() for term in ['property-info', 'property-details', 'property-description'])},
        {'tag': 'div', 'class_': lambda x: x and any(term in str(x).lower() for term in ['listing-details', 'listing-description'])},
        {'tag': 'div', 'class_': lambda x: x and any(term in str(x).lower() for term in ['object-info', 'object-details'])},
        # Generic content selectors
        {'tag': ['article', 'main'], 'class_': None},
        {'tag': 'div', 'class_': lambda x: x and any(term in str(x).lower() for term in ['content', 'article', 'main'])}
    ]
    
    # Try each selector in order
    for selector in content_selectors:
        elements = soup.find_all(selector['tag'], class_=selector['class_'])
        if elements:
            # Get the largest content block
            content_block = max(elements, key=lambda x: len(x.get_text(strip=True)))
            main_content = content_block.get_text(separator=' ', strip=True)
            break
    
    # If no content found, fall back to paragraphs
    if not main_content:
        paragraphs = soup.find_all('p')
        main_content = ' '.join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 50)  # Only include substantial paragraphs
    
    # Clean up the text
    main_content = re.sub(r'\s+', ' ', main_content).strip()
    
    # Remove any remaining cookie-related content
    main_content = re.sub(r'(?i)(cookie|consent|gdpr).*?(\.|$)', '', main_content)
    
    return {
        "title": title,
        "description": meta_desc,
        "content": main_content
    }

def scrape_with_playwright(url):
    logger.info(f"🚀 Starting scrape for URL: {url}")
    executable_path = find_chromium_executable()
    logger.info(f"🎭 Using Chromium at: {executable_path}")
    
    with sync_playwright() as p:
        try:
            logger.info("📱 Launching browser...")
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
                executable_path=executable_path
            )
            
            logger.info("🌍 Creating browser context...")
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            )
            
            logger.info("📄 Creating new page...")
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT)

            logger.info(f"🌐 Navigating to URL: {url}")
            response = page.goto(url)
            
            if not response:
                logger.error("❌ Failed to get response from page")
                raise Exception("Failed to get response from page")
            
            logger.info(f"📡 Response status: {response.status}")
            if response.status >= 400:
                logger.error(f"❌ Page returned error status code: {response.status}")
                raise Exception(f"Page returned status code: {response.status}")

            logger.info("⏳ Waiting for page load...")
            page.wait_for_load_state("domcontentloaded")
            logger.info("✅ DOM content loaded")
            
            # Handle cookie consent
            logger.info("🍪 Handling cookie consent...")
            try:
                # Try different common cookie consent button selectors
                consent_selectors = [
                    'button[id*="accept"]',
                    'button[class*="accept"]',
                    'button[id*="consent"]',
                    'button[class*="consent"]',
                    'button:has-text("Accept")',
                    'button:has-text("Accept all")',
                    'button:has-text("Acceptera")',
                    'button:has-text("Godkänn")',
                    'button:has-text("Tillåt")',
                    'button:has-text("Tillåt alla")'
                ]
                
                for selector in consent_selectors:
                    try:
                        if page.locator(selector).count() > 0:
                            page.locator(selector).first.click()
                            logger.info(f"✅ Clicked cookie consent button with selector: {selector}")
                            # Wait for any animations/transitions
                            page.wait_for_timeout(1000)
                            break
                    except Exception as e:
                        continue
            except Exception as e:
                logger.warning(f"⚠️ Cookie consent handling failed, but continuing: {str(e)}")
            
            try:
                logger.info("⏳ Waiting for network idle...")
                page.wait_for_load_state("networkidle", timeout=5000)
                logger.info("✅ Network is idle")
            except Exception as e:
                logger.warning(f"⚠️ Network didn't become idle, but continuing: {str(e)}")

            # Wait for potential dynamic content
            page.wait_for_timeout(2000)

            logger.info("📥 Getting page content...")
            html_content = page.content()
            
            # Clean and structure the HTML content
            cleaned_data = clean_html_response(html_content)
            
            # If content is still mostly about cookies, try to extract content differently
            if "cookie" in cleaned_data["content"].lower()[:100]:
                logger.info("⚠️ Cookie content detected, trying alternative extraction...")
                try:
                    # Try to find the main content by waiting for specific elements
                    page.wait_for_selector('div[class*="property"], div[class*="listing"], div[class*="object"]', timeout=5000)
                    
                    # Get updated content
                    html_content = page.content()
                    cleaned_data = clean_html_response(html_content)
                except Exception as e:
                    logger.warning(f"⚠️ Alternative extraction failed: {str(e)}")
            
            logger.info("✅ Successfully scraped and cleaned content")
            return {
                "success": True,
                "data": cleaned_data
            }

        except Exception as e:
            logger.error(f"🚨 Scraping error: {str(e)}")
            logger.error(f"Stack trace: {traceback.format_exc()}")
            if 'browser' in locals():
                try:
                    browser.close()
                    logger.info("🎭 Browser closed after error")
                except:
                    logger.error("Failed to close browser after error")
            raise Exception(f"Failed to scrape URL: {str(e)}")

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