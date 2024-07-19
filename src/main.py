import os
import requests
import logging
import time
import sqlite3
from urllib.parse import urljoin, urlsplit, urlparse, urlunparse, unquote
from bs4 import BeautifulSoup
import requests_cache
import sys
from playwright.sync_api import sync_playwright
import validators
import re

language = "es"
fulldir = "/jworg"
mirror_base_url = "https://jw.filmmonitor.co.uk"  # Set your mirror base URL here

logger = logging.getLogger('mylogger')
logger.setLevel(logging.DEBUG)
logFormatter = logging.Formatter("%(asctime)s - %(name=s)s - %(levelname)s - %(message)s")
consoleHandler = logging.StreamHandler(sys.stdout)
consoleHandler.setFormatter(logFormatter)
logger.addHandler(consoleHandler)

database_path = os.path.join(fulldir, 'urls.db')

def is_valid_url(url):
    return validators.url(url)

def is_jw_language_url(url):
    parsed_url = urlsplit(url)
    return parsed_url.netloc == "www.jw.org" and parsed_url.path.startswith(f"/{language}/")

def get_sitemap():
    requests_cache.install_cache(fulldir + "/" + language + '/sitemap')
    session = requests_cache.CachedSession('sitemap', expire_after=86400)  # 24h
    jw_org = "https://www.jw.org/"
    links = [jw_org + language]  # Add main page
    response = requests.get(jw_org + language + "/sitemap.xml")
    soup = BeautifulSoup(response.text, features="xml")
    url_locs = soup.find_all("loc")
    for url in url_locs:
        links.append(url.text)
    return links

def download_asset(url, local_path):
    if os.path.exists(local_path):
        logger.info(f"Asset already exists: {local_path}")
        return True  # Indicate that asset already exists and there's no need to download
    
    logger.info(f"Downloading asset {url}")
    local_dir = os.path.dirname(local_path)

    filename = os.path.basename(urlparse(url).path)
    if not filename:  # for URLs that do not end with a file
        filename = 'index.html'
    
    local_path = os.path.join(local_dir, filename)
    
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
    
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()  # Raise HTTPError for bad responses
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:  # Filter out keep-alive chunks
                        f.write(chunk)
        logger.info(f"Downloaded asset {url} to {local_path}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download {url}: {str(e)}")
        return False

def is_asset_url(url, base_url):
    url_parts = urlsplit(url)
    if not url_parts.netloc and url.startswith('/'):  # Relative URLs
        return True
    
    if url_parts.netloc:  # Full URLs
        if any(substring in url_parts.netloc for substring in ["akamaihd.net", "jw-cdn", "gstatic.com"]):  # Added gstatic.com
            return True
        return url_parts.netloc == urlsplit(base_url).netloc and ('.' in url_parts.path.split('/')[-1] or '?' in url)

def init_db():
    conn = sqlite3.connect(database_path)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            processed INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def add_url_to_db(url):
    conn = sqlite3.connect(database_path)
    cur = conn.cursor()
    try:
        cur.execute('INSERT INTO urls (url, processed) VALUES (?, 0)', (url,))
    except sqlite3.IntegrityError:
        # URL already exists
        pass
    conn.commit()
    conn.close()

def mark_url_processed(url):
    conn = sqlite3.connect(database_path)
    cur = conn.cursor()
    cur.execute('UPDATE urls SET processed = 1 WHERE url = ?', (url,))
    conn.commit()
    conn.close()

def get_next_unprocessed_url():
    conn = sqlite3.connect(database_path)
    cur = conn.cursor()
    cur.execute('SELECT url FROM urls WHERE processed = 0 ORDER BY id LIMIT 1')
    result = cur.fetchone()
    conn.close()
    if result:
        return result[0]
    return None

def add_urls_to_db(urls):
    conn = sqlite3.connect(database_path)
    cur = conn.cursor()
    for url in urls:
        try:
            cur.execute('INSERT INTO urls (url, processed) VALUES (?, 0)', (url,))
        except sqlite3.IntegrityError:
            # URL already exists
            continue
    conn.commit()
    conn.close()

def insert_url_first(url):
    conn = sqlite3.connect(database_path)
    cur = conn.cursor()
    try:
        cur.execute('INSERT OR REPLACE INTO urls (id, url, processed) VALUES ((SELECT min(id) FROM urls) - 1, ?, 0)', (url,))
    except sqlite3.IntegrityError:
        # URL already exists
        pass
    conn.commit()
    conn.close()

def download_webpage(url, context):
    page = context.new_page()
    page.goto(url, wait_until='load')
    
    local_url = urlparse(url)._replace(netloc="", scheme="")
    local_folder = fulldir + urlunparse(map(unquote, local_url))
    
    # Ensure directory exists
    os.makedirs(local_folder, exist_ok=True)
    
    # Allow time for page content to fully load
    time.sleep(3)
    page_content = page.content()
    bs_page = BeautifulSoup(page_content, "html.parser")

    # Collect asset URLs
    assets = []
    css_assets = []
    
    for tag_name, attribute_name in [("link", "href"), ("meta", "content"), ("script", "src"), ("video", "src"), ("img", "src")]:
        for tag in bs_page.find_all(tag_name, **{attribute_name: True}):
            asset_url = tag[attribute_name]
            if is_asset_url(asset_url, url):
                full_url = urljoin("https://www.jw.org", asset_url) if not urlsplit(asset_url).netloc else asset_url
                if is_valid_url(full_url):
                    if tag_name == "link" and tag.get("rel") == ["stylesheet"]:
                        css_assets.append((tag, attribute_name, full_url))
                    else:
                        assets.append((tag, attribute_name, full_url))

    # Process and download CSS assets
    for tag, attribute_name, css_url in css_assets:
        logger.info(f"Processing CSS asset {css_url}")
        css_filename = os.path.basename(urlparse(css_url).path)
        local_css_path = os.path.join(fulldir, "assets", css_filename)
        
        # Download CSS file
        if not download_asset(css_url, local_css_path):
            logger.error(f"Failed to download CSS file {css_url}. Skipping modifications.")
            continue

        # Read and modify CSS content
        with open(local_css_path, "r", encoding="utf-8") as css_file:
            css_content = css_file.read()

        # Find all URLs in CSS content, including relative ones
        css_urls = re.findall(r'url\(["\']?(.*?)["\']?\)', css_content)
        font_assets = []

        # Fallback base URLs for assets if the first attempt fails
        fallback_base_urls = [
            "https://assetsnffrgf-a.akamaihd.net",
            "https://b.jw-cdn.org/code/media-player/"
        ]

        # Download fonts and update URLs in CSS content
        for font_url in css_urls:
            # If URL is already base64, skip downloading and continue
            if font_url.startswith('data:'):
                continue
            
            # Determine full URL for download
            if not urlsplit(font_url).netloc:
                # Asset is relative, convert to full URL
                full_font_url = urljoin(css_url, font_url)
            else:
                # Asset is already a full URL
                full_font_url = font_url

            asset_basename = os.path.basename(urlparse(full_font_url).path.split('?')[0])
            local_font_path = os.path.join(fulldir, "assets", asset_basename)

            # Check if local asset already exists before downloading
            if os.path.exists(local_font_path):
                logger.info(f"Asset already exists locally: {local_font_path}")
                new_font_url = f"{mirror_base_url}/assets/{asset_basename}"
                css_content = css_content.replace(font_url, new_font_url)
                continue

            asset_downloaded = False
            for base_url in fallback_base_urls:
                if is_valid_url(full_font_url):
                    try:
                        if download_asset(full_font_url, local_font_path):
                            asset_downloaded = True
                            new_font_url = f"{mirror_base_url}/assets/{asset_basename}"
                            css_content = css_content.replace(font_url, new_font_url)
                            break  # Exit loop as soon as one download succeeds
                    except Exception as e:
                        logger.error(f"Failed to download {full_font_url}: {str(e)}")
            if not asset_downloaded:
                logger.error(f"Failed to download asset {font_url} from all base URLs.")

        # Save modified CSS content to file
        with open(local_css_path, "w", encoding="utf-8") as css_file:
            css_file.write(css_content)

        # Update the CSS link in HTML
        tag[attribute_name] = f"{mirror_base_url}/assets/{css_filename}"

    # Download other assets and update URLs in the HTML
    for tag, attribute_name, asset_url in assets:
        asset_basename = os.path.basename(urlparse(asset_url).path) or 'index.html'
        
        # Remove URL parameters for local asset names if any
        if '?' in asset_basename:
            asset_basename = asset_basename.split('?')[0]
        
        # Special handling for 'cast_sender.js'
        if 'cast_sender.js' in asset_basename:
            asset_basename = 'cast_sender.js'

        # Create local paths and download
        local_asset_path = os.path.join(fulldir, "assets", asset_basename)
        full_asset_url = asset_url  # Original URL for downloading
        
        # Check if local asset already exists before downloading
        if os.path.exists(local_asset_path):
            logger.info(f"Asset already exists locally: {local_asset_path}")
            new_asset_url = f"{mirror_base_url}/assets/{asset_basename}"
            tag[attribute_name] = new_asset_url
            continue

        # Download the asset
        if is_valid_url(full_asset_url):
            try:
                if download_asset(full_asset_url, local_asset_path):
                    new_asset_url = f"{mirror_base_url}/assets/{asset_basename}"
                    tag[attribute_name] = new_asset_url
            except Exception as e:
                logger.error(f"Failed to download {full_asset_url}: {str(e)}")

    # Modify hrefs in <a>, <link>, <base> tags
    for tag in bs_page.find_all(["a", "link", "base"], href=True):
        href = tag['href']
        if href.startswith("https://www.jw.org/"):
            tag['href'] = href.replace("https://www.jw.org", mirror_base_url)
        elif href.startswith("/"):
            tag['href'] = urljoin(f"{mirror_base_url}/{language}", href.lstrip("/"))

    # Collect new jw.org/es/ links
    new_links = set()
    for tag in bs_page.find_all("a", href=True):
        link = urljoin(url, tag['href'])
        if is_jw_language_url(link):
            new_links.add(link)

    add_urls_to_db(new_links)

    # Remove unwanted popups
    for lnc_popup in bs_page.find_all("div", class_="lnc-firstRunPopup"):
        lnc_popup.decompose()

    # Save modified HTML content to local file
    local_file_name = os.path.join(local_folder, "index.html")
    with open(local_file_name, "w", encoding="utf-8") as file:
        file.write(str(bs_page))
    
    # Close the page to free up memory
    page.close()

if __name__ == '__main__':
    if not os.path.exists(fulldir):
        os.makedirs(fulldir)
        
    # Initialize database
    init_db()
    
    test_url = "https://www.jw.org/es/biblioteca/videos/#es/mediaitems/pub-jwbvod24_27_VIDEO"
    
    # Insert the test URL manually at the top
    insert_url_first(test_url)

    # Now, add other URLs
    original_links = get_sitemap()
    add_urls_to_db(original_links)
    if "https://www.jw.org/" + language not in original_links:
        add_url_to_db("https://www.jw.org/" + language)  # Add the main page if not already in the sitemap
    
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-GB,en-NZ;q=0.9,en-AU;q=0.8,en;q=0.7,en-US;q=0.6",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win32; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    with sync_playwright() as p:
        browser = p.firefox.connect('ws://192.168.10.100:3030/firefox/playwright?token=YGxoYfrARhtkSVxyfbfLwHc9me4afP9VS3y89EVa')
        context = browser.new_context(extra_http_headers=headers)

        while True:
            link = get_next_unprocessed_url()
            if not link:
                logger.info("All URLs have been processed.")
                break

            logger.info(f"Processing {link}")
            try:
                download_webpage(link, context)
                mark_url_processed(link)
            except Exception as e:
                logger.error(f"Failed to process {link}: {e}")

        browser.close()
