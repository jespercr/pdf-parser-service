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
import time
from contextlib import timeout

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
    """
    Parse PDF text with optimized memory usage and timeout protection
    """
    text = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            # Process in chunks of 10 pages
            for i in range(0, total_pages, 10):
                chunk = pdf.pages[i:i+10]
                for page in chunk:
                    try:
                        # Add timeout protection for each page
                        with timeout(seconds=30):
                            page_text = page.extract_text() or ""
                            text.append(page_text)
                    except TimeoutError:
                        logger.warning(f"Timeout extracting text from page {i}")
                        text.append(f"[Error: Timeout processing page {i}]")
                    except Exception as e:
                        logger.error(f"Error processing page {i}: {str(e)}")
                        text.append(f"[Error processing page {i}]")
                
                # Clear memory after each chunk
                del chunk
                import gc
                gc.collect()
                
    except Exception as e:
        logger.error(f"Error parsing PDF: {str(e)}")
        raise
        
    return "\n\n".join(text).strip()

class timeout:
    """
    Timeout context manager to prevent hanging on PDF processing
    """
    def __init__(self, seconds):
        self.seconds = seconds

    def __enter__(self):
        def signal_handler(signum, frame):
            raise TimeoutError("Timed out")
        
        import signal
        signal.signal(signal.SIGALRM, signal_handler)
        signal.alarm(self.seconds)

    def __exit__(self, type, value, traceback):
        import signal
        signal.alarm(0)

def extract_images_from_pdf(pdf_path, output_dir="/tmp/pdf_images"):
    """
    Extract images with optimized memory usage and timeout protection
    """
    os.makedirs(output_dir, exist_ok=True)
    image_paths = []
    
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        
        # Process in chunks of 5 pages
        for start_page in range(0, total_pages, 5):
            try:
                with timeout(seconds=30):  # 30 second timeout per chunk
                    end_page = min(start_page + 5, total_pages)
                    for page_num in range(start_page, end_page):
                        page = doc[page_num]
                        images = page.get_images(full=True)
                        
                        for i, img in enumerate(images):
                            try:
                                xref = img[0]
                                base_image = doc.extract_image(xref)
                                image_bytes = base_image["image"]
                                ext = base_image["ext"]
                                
                                # Skip if image is too large (e.g., > 10MB)
                                if len(image_bytes) > 10 * 1024 * 1024:
                                    logger.warning(f"Skipping large image on page {page_num}")
                                    continue
                                    
                                img_filename = f"page{page_num+1}_img{i+1}.{ext}"
                                img_path = os.path.join(output_dir, img_filename)
                                
                                with open(img_path, "wb") as f:
                                    f.write(image_bytes)
                                
                                image_paths.append(img_path)
                                
                                # Clear memory
                                del image_bytes
                                del base_image
                                
                            except Exception as e:
                                logger.error(f"Error extracting image {i} from page {page_num}: {str(e)}")
                                continue
                        
                        # Clear page from memory
                        page = None
                        
                # Force garbage collection after each chunk
                import gc
                gc.collect()
                        
            except TimeoutError:
                logger.warning(f"Timeout processing pages {start_page}-{end_page}")
                continue
                
    except Exception as e:
        logger.error(f"Error in image extraction: {str(e)}")
        raise
    finally:
        if 'doc' in locals():
            doc.close()
            
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

async def scrape_with_playwright(url):
    """
    Scrape a website using Playwright.
    This version includes better cookie consent handling and content loading detection.
    """
    logger.info(f"🌐 Scraping URL: {url}")
    start_time = time.time()
    
    browser = None
    try:
        chromium_path = find_chromium_executable()
        logger.info(f"🔍 Using Chromium at {chromium_path}")
        
        # Launch browser with custom settings
        browser = await playwright.chromium.launch(
            executable_path=chromium_path if chromium_path else None,
            headless=True
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        
        # Add storage state for persistence if needed
        await context.add_cookies([{
            "name": "cookieConsent", 
            "value": "true", 
            "domain": "."+".".join(url.split('/')[2].split('.')[-2:]),
            "path": "/"
        }])
        
        page = await context.new_page()
        
        # Set long timeout for navigation
        await page.set_default_timeout(30000)
        
        logger.info(f"⌛ Navigating to URL...")
        response = await page.goto(url)
        
        # Check response status
        if not response:
            logger.error("❌ Failed to get response from page")
            return {"error": "No response from page"}
        
        status = response.status
        logger.info(f"🔢 Response status: {status}")
        
        if status >= 400:
            logger.error(f"❌ Error status code: {status}")
            return {"error": f"HTTP error: {status}"}
        
        # Wait for network to be idle
        await page.wait_for_load_state("networkidle")
        logger.info("🛑 Network is idle")
        
        # Handle cookie consent - try multiple common selectors
        consent_buttons = [
            # General accept buttons
            'button[id*="accept"]', 'button[class*="accept"]', 
            'button:has-text("Accept")', 'button:has-text("Accept all")',
            'button:has-text("Godkänn")', 'button:has-text("Acceptera")',
            'a[id*="accept"]', 'a[class*="accept"]',
            'a:has-text("Accept")', 'a:has-text("Accept all")',
            
            # Common cookie consent button IDs/classes
            '#onetrust-accept-btn-handler', '.cookie-accept-button',
            '#accept-all-cookies', '.accept-cookies-button',
            '#accept-cookies', '.cookie-accept',
            '#acceptCookies', '#CybotCookiebotDialogBodyButtonAccept',
            '#gdpr-cookie-accept', '#cookie-notice-accept-button',
            
            # Common consent interfaces
            '[aria-label="Accept cookies"]', '[data-testid="cookie-accept"]',
            '[data-action="accept-cookies"]', '[data-action="accept-all"]'
        ]
        
        # Try to accept cookies
        for selector in consent_buttons:
            try:
                logger.info(f"🍪 Looking for consent button: {selector}")
                if await page.locator(selector).count() > 0:
                    await page.locator(selector).click(timeout=2000)
                    logger.info(f"🍪 Clicked consent button: {selector}")
                    # Wait for potential overlay to disappear
                    await page.wait_for_timeout(1000)
                    break
            except Exception as e:
                # Just log and continue to next selector
                logger.debug(f"Couldn't click {selector}: {str(e)}")
                continue
        
        # Wait a bit for page to settle after cookie interactions
        await page.wait_for_timeout(1000)
        
        # Wait for content to stabilize by checking for DOM size changes
        previous_content_size = 0
        stable_count = 0
        max_stabilize_checks = 5
        
        for i in range(max_stabilize_checks):
            # Get the current content size
            content_size = await page.evaluate('''() => {
                return document.body.innerHTML.length;
            }''')
            
            logger.debug(f"Content size check {i+1}: {content_size} bytes")
            
            # If content size is stable, increment counter
            if abs(content_size - previous_content_size) < 100:
                stable_count += 1
                if stable_count >= 2:  # Content is considered stable after 2 consecutive stable checks
                    logger.info(f"✅ Content appears stable after {i+1} checks")
                    break
            else:
                stable_count = 0
                
            previous_content_size = content_size
            await page.wait_for_timeout(1000)  # Wait a second between checks
        
        # Try scrolling to load any lazy content
        await page.evaluate('''() => {
            window.scrollTo(0, document.body.scrollHeight / 2);
            setTimeout(() => { window.scrollTo(0, document.body.scrollHeight); }, 500);
        }''')
        await page.wait_for_timeout(1500)  # Wait for lazy loading to complete
        
        # Get HTML content after all interactions
        html_content = await page.content()
        logger.info(f"📄 Got HTML content: {len(html_content)} bytes")
        
        # Clean the HTML response
        cleaned_data = clean_html_response(html_content)
        
        # Check if cleaned content is mostly about cookies/consent
        content_text = cleaned_data.get("content", "")
        if content_text and len(content_text) < 200 or "cookie" in content_text.lower()[:100]:
            logger.warning("⚠️ Initial content appears to be cookie-related. Trying alternative extraction...")
            
            # Take a screenshot for debugging if content is cookie-related
            await page.screenshot(path="/tmp/cookie_page.png")
            logger.info("📸 Saved screenshot to /tmp/cookie_page.png")
            
            # Try to extract content directly from page
            extracted_content = await page.evaluate('''() => {
                // Remove cookie-related content
                const cookieElements = document.querySelectorAll('[id*="cookie"], [class*="cookie"], [id*="consent"], [class*="consent"], [id*="gdpr"], [class*="gdpr"]');
                cookieElements.forEach(el => el.remove());
                
                // Try to get main content
                const mainContent = document.querySelector('main') || document.querySelector('article');
                if (mainContent) return mainContent.innerText;
                
                // Fallback to paragraphs
                const paragraphs = Array.from(document.querySelectorAll('p')).map(p => p.innerText).filter(text => text.length > 50);
                return paragraphs.join('\n\n');
            }''')
            
            if extracted_content and len(extracted_content) > 200:
                logger.info(f"🔄 Alternative extraction successful: {len(extracted_content)} characters")
                cleaned_data["content"] = extracted_content
        
        elapsed_time = time.time() - start_time
        logger.info(f"⏱️ Scraping completed in {elapsed_time:.2f} seconds")
        
        # Add scraping metadata
        cleaned_data["metadata"] = {
            "scrape_time": elapsed_time,
            "url": url,
            "status_code": status,
            "timestamp": datetime.now().isoformat()
        }
        
        return cleaned_data
        
    except Exception as e:
        logger.error(f"❌ Error during scraping: {str(e)}")
        return {"error": str(e)}
        
    finally:
        if browser:
            logger.info(" Closing browser")
            await browser.close()

def clean_html_response(html_content):
    """
    Clean and structure HTML content using BeautifulSoup.
    More aggressively extracts only property-relevant information.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Remove script, style, and footer elements
    for element in soup(['script', 'style', 'footer', 'iframe', 'header', 'nav']):
        element.decompose()
        
    # Remove all elements containing policy-related content
    policy_terms = ['cookie', 'gdpr', 'privacy', 'policy', 'villkor', 'consent', 'personuppgift', 
                   'integritet', 'acceptera', 'godkänn', 'samtycke', 'rättigheter',
                   'accept', 'allow', 'datapolicy', 'dataskydd']
                   
    # Remove common non-content elements by ID, class, and role
    non_content_selectors = [
        '[id*="cookie"]', '[class*="cookie"]', '[id*="consent"]', '[class*="consent"]',
        '[id*="gdpr"]', '[class*="gdpr"]', '[id*="privacy"]', '[class*="privacy"]',
        '[id*="popup"]', '[class*="popup"]', '[id*="modal"]', '[class*="modal"]',
        '[id*="banner"]', '[class*="banner"]', '[id*="alert"]', '[class*="alert"]',
        '[id*="dialog"]', '[class*="dialog"]', '[id*="notice"]', '[class*="notice"]',
        '[id*="overlay"]', '[class*="overlay"]', '[id*="notification"]', '[class*="notification"]',
        '[id*="menu"]', '[class*="menu"]', '[id*="nav"]', '[class*="nav"]',
        '[id*="header"]', '[class*="header"]', '[id*="footer"]', '[class*="footer"]',
        '[id*="sidebar"]', '[class*="sidebar"]', '[id*="aside"]', '[class*="aside"]',
        '[role="banner"]', '[role="navigation"]', '[role="complementary"]', '[role="contentinfo"]',
        '[aria-label*="cookie"]', '[aria-labelledby*="cookie"]',
        '[data-testid*="cookie"]', '[data-id*="cookie"]'
    ]
    
    for selector in non_content_selectors:
        for element in soup.select(selector):
            element.decompose()
    
    # First pass: remove elements with policy terms in their attributes
    for element in soup.find_all(lambda tag: any(term in (str(tag.get('id', '')) + str(tag.get('class', '')) + str(tag.get('title', ''))).lower() for term in policy_terms)):
        element.decompose()
    
    # Second pass: remove elements with policy terms in their text
    for element in soup.find_all(text=lambda text: text and any(term in text.lower() for term in policy_terms)):
        parent = element.parent
        if parent:
            parent.decompose()
    
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
    
    # Extract main content using multiple strategies
    content_text = ""
    
    # Strategy 1: Look for property-specific containers
    property_classes = [
        'property-info', 'property-details', 'listing-details', 'object-info',
        'property-description', 'estate-info', 'listing-description', 'main-content',
        'content-main', 'article', 'main', 'content-area', 'page-content'
    ]
    
    # Try each class separately to find meaningful content
    for cls in property_classes:
        elements = soup.find_all(class_=lambda x: x and cls.lower() in x.lower())
        if elements:
            for element in elements:
                text = element.get_text(separator=' ', strip=True)
                if len(text) > 100 and not any(term in text.lower() for term in policy_terms):
                    content_text = text
                    break
            if content_text:
                break
    
    # Try finding by semantic HTML elements if no property-specific class found
    if not content_text:
        for tag in ['main', 'article']:
            elements = soup.find_all(tag)
            if elements:
                for element in elements:
                    text = element.get_text(separator=' ', strip=True)
                    if len(text) > 100 and not any(term in text.lower() for term in policy_terms):
                        content_text = text
                        break
                if content_text:
                    break
    
    # Strategy 2: If no property container found, look for specific sections
    if not content_text:
        # Find sections that likely contain property details
        sections = []
        # Check for transportation info
        transport = soup.find_all(string=lambda text: text and any(term in text.lower() for term in ['kollektivt', 'kommunikation', 'pendel', 'transport', 'buss', 'station']))
        for t in transport:
            if t.parent and len(t.parent.get_text(strip=True)) > 50:
                sections.append(t.parent.get_text(separator=' ', strip=True))
        
        # Check for property features
        features = soup.find_all(string=lambda text: text and any(term in text.lower() for term in ['parkering', 'garage', 'restaurang', 'service', 'hyresgäst']))
        for f in features:
            if f.parent and len(f.parent.get_text(strip=True)) > 50:
                sections.append(f.parent.get_text(separator=' ', strip=True))
        
        # Combine the sections if we found any
        if sections:
            content_text = ' '.join(sections)
    
    # Strategy 3: Last resort - collect all substantial paragraphs
    if not content_text or len(content_text) < 100:
        paragraphs = []
        for p in soup.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > 30 and not any(term in text.lower() for term in policy_terms):
                paragraphs.append(text)
        
        if paragraphs:
            content_text = ' '.join(paragraphs)
    
    # Filter out policy text sentences
    if content_text:
        clean_sentences = []
        for sentence in re.split(r'(?<=[.!?])\s+', content_text):
            if len(sentence) > 10 and not any(term in sentence.lower() for term in policy_terms):
                clean_sentences.append(sentence)
        
        content_text = ' '.join(clean_sentences)
    
    # Extract the actual property information from the start of the description
    if "Kontorsfastighet" in content_text:
        property_start = content_text.find("Kontorsfastighet")
        if property_start >= 0:
            # Only keep text from this point forward
            content_text = content_text[property_start:]
    
    # Clean up the text
    content_text = re.sub(r'\s+', ' ', content_text).strip()
    
    return {
        "title": title,
        "description": meta_desc,
        "content": content_text
    }

@app.route('/scrape', methods=['POST'])
def scrape():
    """
    Endpoint for scraping a website and extracting content
    """
    start_time = time.time()
    logger.info("📥 Received scrape request")

    # Check request format
    if not request.is_json:
        logger.error("❌ Request is not JSON")
        return jsonify({"error": "Request must be JSON"}), 400

    # Parse request
    req_data = request.get_json()
    
    # Check for URL
    if 'url' not in req_data:
        logger.error("❌ No URL provided")
        return jsonify({"error": "URL is required"}), 400
    
    url = req_data['url']
    
    # Check if scraping is allowed
    try:
        if not is_scraping_allowed(url):
            logger.error(f"🚫 Scraping not allowed for {url}")
            return jsonify({"error": "Scraping not allowed by robots.txt"}), 403
    except Exception as e:
        logger.warning(f"⚠️ Error checking robots.txt: {str(e)}")
    
    # Run the scraping in a synchronous wrapper around the async function
    try:
        import asyncio
        # Check if we're already in an event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Create a new loop for this request
                new_loop = asyncio.new_event_loop()
                result = new_loop.run_until_complete(scrape_with_playwright(url))
                new_loop.close()
            else:
                result = loop.run_until_complete(scrape_with_playwright(url))
        except RuntimeError:
            # No event loop exists yet
            result = asyncio.run(scrape_with_playwright(url))
            
        elapsed_time = time.time() - start_time
        logger.info(f"⏱️ Total request time: {elapsed_time:.2f} seconds")
        
        # Add request timing to response
        if isinstance(result, dict) and "metadata" in result:
            result["metadata"]["total_request_time"] = elapsed_time
        
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"🚨 Error in scrape endpoint: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/parse", methods=["POST"])
def parse():
    """
    Enhanced parse endpoint with better error handling and timeouts
    """
    file = request.files.get("file")
    space_id = request.form.get("space_id")

    if not file or not space_id:
        return jsonify({"error": "Missing file or space_id"}), 400

    # Create unique temporary directory for this request
    import uuid
    temp_dir = f"/tmp/pdf_processing_{uuid.uuid4()}"
    os.makedirs(temp_dir, exist_ok=True)
    file_path = os.path.join(temp_dir, file.filename)
    
    try:
        # Save file with timeout protection
        with timeout(seconds=30):
            file.save(file_path)
            
        result = {"status": "processing"}
        
        # 1. Extract text with timeout
        try:
            with timeout(seconds=120):  # 2 minutes timeout for text extraction
                parsed_text = parse_pdf_text(file_path)
                result["text"] = parsed_text
        except TimeoutError:
            result["text_error"] = "Text extraction timed out"
            logger.error("Text extraction timed out")
        except Exception as e:
            result["text_error"] = str(e)
            logger.error(f"Text extraction error: {str(e)}")

        # 2. Extract and upload images with timeout
        try:
            with timeout(seconds=180):  # 3 minutes timeout for image processing
                image_paths = extract_images_from_pdf(file_path, os.path.join(temp_dir, "images"))
                if image_paths:
                    image_upload_result = send_images_to_rails(image_paths, space_id)
                    result["image_upload_result"] = image_upload_result
        except TimeoutError:
            result["image_error"] = "Image processing timed out"
            logger.error("Image processing timed out")
        except Exception as e:
            result["image_error"] = str(e)
            logger.error(f"Image processing error: {str(e)}")

        return jsonify(result)

    except TimeoutError:
        return jsonify({"error": "Request timed out"}), 504
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        # Clean up temporary files
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            logger.error(f"Error cleaning up temporary files: {str(e)}")