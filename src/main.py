import os
import requests
import shutil
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
        logger.info(f"Asset already downloaded: {local_path}")
        return
    
    logger.info(f"Downloading asset {url}")
    local_dir = os.path.dirname(local_path)
    filename = os.path.basename(urlparse(url).path) or 'index.html'
    local_path = os.path.join(local_dir, filename)
    
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
        
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()  # to raise HTTPError for bad responses
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:  # filter out keep-alive chunks
                    f.write(chunk)

    logger.info(f"Finished downloading asset {url} to {local_path}")

def is_asset_url(url, base_url):
    url_parts = urlsplit(url)
    if not url_parts.netloc and url.startswith('/'):  # Relative URLs
        return True
    
    if url_parts.netloc:  # Full URLs
        if any(substring in url_parts.netloc for substring in ["akamai", "jw-cdn"]):
            return True
        return url_parts.netloc == urlsplit(base_url).netloc and ('.' in url_parts.path.split('/')[-1])

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
    local_folder = fulldir + urlunparse(local_url) + "/"
    os.makedirs(local_folder, exist_ok=True)

    time.sleep(3)
    page_content = page.content()
    bs_page = BeautifulSoup(page_content, "html.parser")

    # Collect asset URLs
    assets = []
    for tag_name, attribute_name in [("link", "href"), ("meta", "content"), ("script", "src"), ("video", "src")]:
        for tag in bs_page.find_all(tag_name, **{attribute_name: True}):
            asset_url = tag[attribute_name]
            if is_asset_url(asset_url, url):
                full_url = urljoin("https://www.jw.org", asset_url) if not urlsplit(asset_url).netloc else asset_url
                if is_valid_url(full_url):  # Only add valid URLs
                    assets.append((tag, attribute_name, full_url))

    # Download assets and modify their hrefs
    for tag_info in assets:
        tag = tag_info[0]
        attribute_name = tag_info[1]
        asset_url = tag_info[2]
        
        # Skip non-assets like viewport or other non-downloadable meta tags
        if tag.name == "meta" and "viewport" in tag.get("name", "").lower():
            continue
        
        asset_basename = os.path.basename(urlparse(asset_url).path) or 'index.html'
        if '?' in asset_basename:
            asset_basename = asset_basename.split('?')[0]

        local_asset_path = os.path.join(fulldir, "assets", asset_basename)
        full_asset_url = f"https://jw.filmmonitor.co.uk/assets/{asset_basename}"

        download_asset(asset_url, local_asset_path)
        tag[attribute_name] = full_asset_url

    local_folder_name = local_folder + "index.html"

    # href modification
    for tag_name, attribute_name in [("a", "href"), ("link", "href"), ("base", "href")]:
        for tag in bs_page.find_all(tag_name, **{attribute_name: True}):
            if tag[attribute_name].startswith("https://www.jw.org/"):
                tag[attribute_name] = tag[attribute_name].replace("https://www.jw.org", "https://jw.filmmonitor.co.uk")

    # Collect new jw.org/es/ links
    new_links = []
    for tag in bs_page.find_all("a", href=True):
        link = tag['href']
        full_link = urljoin(url, link)
        if is_jw_language_url(full_link):
            new_links.append(full_link)

    add_urls_to_db(new_links)

    # Cookies deletion
    for lnc_popup in bs_page.find_all("div", class_="lnc-firstRunPopup"):
        lnc_popup.decompose()

    # Save file
    with open(local_folder_name, "w", encoding="utf-8") as file:
        file.write(str(bs_page))

if __name__ == '__main__':
    if not os.path.exists(fulldir):
        os.makedirs(fulldir)
        
    # Initialize database
    init_db()
    
    original_links = get_sitemap()
    #add_urls_to_db("https://www.jw.org/es/biblioteca/videos/ebtv/respuestas-grandes-preguntas-vida/") # Test video
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
