import os
import requests
import logging
import time
import sqlite3
from urllib.parse import urljoin, urlsplit, urlparse, urlunparse
from bs4 import BeautifulSoup
import requests_cache
import sys
from playwright.sync_api import sync_playwright
import validators

language = "es"
fulldir = "/jworg"

logger = logging.getLogger('mylogger')
logger.setLevel(logging.DEBUG)
logFormatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
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
        return
    
    logger.info(f"Downloading asset {url}")
    local_dir = os.path.dirname(local_path)

    filename = os.path.basename(urlparse(url).path)
    if not filename:  # for URLs that do not end with a file
        filename = 'index.html'

    # Adjust for cast_sender.js
    if 'cast_sender.js' in filename:
        filename = 'cast_sender.js'

    local_path = os.path.join(local_dir, filename)
    
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
    
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()  # Raise HTTPError for bad responses
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:  # Filter out keep-alive chunks
                    f.write(chunk)

    logger.info(f"Downloaded asset {url} to {local_path}")

def is_asset_url(url, base_url):
    url_parts = urlsplit(url)
    if not url_parts.netloc and url.startswith('/'):  # Relative URLs
        return True
    
    if url_parts.netloc:  # Full URLs
        if any(substring in url_parts.netloc for substring in ["akamai", "jw-cdn", "gstatic.com"]):  # Added gstatic.com
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
    cur.execute('SELECT url FROM urls WHERE processed = 0 LIMIT 1')
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

def download_webpage(url, context):
    page = context.new_page()
    page.goto(url, wait_until='load')
    
    local_url = urlparse(url)._replace(netloc="", scheme="")
    local_folder = fulldir + urlunparse(local_url)
    
    # Ensure directory exists
    os.makedirs(local_folder, exist_ok=True)
    
    # Allow time for page content to fully load
    time.sleep(3)
    page_content = page.content()
    bs_page = BeautifulSoup(page_content, "html.parser")

    # Collect asset URLs
    assets = []
    for tag_name, attribute_name in [("link", "href"), ("meta", "content"), ("script", "src"), ("video", "src"), ("img", "src")]:
        for tag in bs_page.find_all(tag_name, **{attribute_name: True}):
            asset_url = tag[attribute_name]
            if is_asset_url(asset_url, url):
                full_url = urljoin("https://www.jw.org", asset_url) if not urlsplit(asset_url).netloc else asset_url
                if is_valid_url(full_url):
                    assets.append((tag, attribute_name, full_url))

    # Download assets and update URLs in the HTML
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
        full_asset_url = f"https://jw.filmmonitor.co.uk/assets/{asset_basename}"
        download_asset(asset_url, local_asset_path)
        
        # Update HTML tag with new URL
        tag[attribute_name] = full_asset_url

    # Modify hrefs in <a>, <link>, <base> tags
    for tag in bs_page.find_all(["a", "link", "base"], href=True):
        href = tag['href']
        if href.startswith("https://www.jw.org/"):
            tag['href'] = href.replace("https://www.jw.org", "https://jw.filmmonitor.co.uk")
        elif href.startswith("/"):
            tag['href'] = urljoin(f"https://jw.filmmonitor.co.uk/{language}", href.lstrip("/"))

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
