import os
import requests
import logging
import time
import sqlite3
from urllib.parse import urljoin, urlsplit, urlparse, urlunparse, unquote, quote
from bs4 import BeautifulSoup
import requests_cache
import sys
from playwright.sync_api import sync_playwright
import validators
import re
import random

JW_BROWSERLESS_KEY = os.environ.get('JW_BROWSERLESS_KEY')
JW_LANG = os.environ.get('JW_LANG', "es")
JW_DIR = os.environ.get('JW_DIR', "/jworg")
JW_LOCAL_BASE_URL = os.environ.get('JW_LOCAL_BASE_URL')

logger = logging.getLogger('mylogger')
logger.setLevel(logging.DEBUG)
logFormatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
consoleHandler = logging.StreamHandler(sys.stdout)
consoleHandler.setFormatter(logFormatter)
logger.addHandler(consoleHandler)

database_path = os.path.join(JW_DIR, 'urls.db')

def is_valid_url(url):
    return validators.url(url)

def is_jw_language_url(url):
    parsed_url = urlsplit(url)
    return parsed_url.netloc == "www.jw.org" and parsed_url.path.startswith(f"/{JW_LANG}/")

def get_sitemap():
    requests_cache.install_cache(JW_DIR + "/" + JW_LANG + '/sitemap')
    session = requests_cache.CachedSession('sitemap', expire_after=86400)  # 24h
    jw_org = "https://www.jw.org/"
    links = [jw_org + JW_LANG]  # Add main page
    response = requests.get(jw_org + JW_LANG + "/sitemap.xml")
    soup = BeautifulSoup(response.text, features="xml")
    url_locs = soup.find_all("loc")
    for url in url_locs:
        links.append(url.text)
    return links

def download_asset(url, local_path):
    if os.path.exists(local_path):
        logger.info(f"Asset already exists: {local_path}")
        return True
    
    logger.info(f"Downloading asset {url}")
    local_dir = os.path.dirname(local_path)

    filename = os.path.basename(urlparse(url).path)
    if not filename:
        filename = 'index.html'
    
    local_path = os.path.join(local_dir, filename)
    
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
    
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()  # Raise HTTPError for bad responses
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        logger.info(f"Downloaded asset {url} to {local_path}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download {url}: {str(e)}")
        return False

def is_asset_url(url, base_url):
    url_parts = urlsplit(url)
    if not url_parts.netloc and url.startswith('/'):
        return True

    if url_parts.netloc:
        if any(substring in url_parts.netloc for substring in [
                "akamaihd.net", "jw-cdn", "gstatic.com", "cloudfront.net"]):
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
            continue
    conn.commit()
    conn.close()

def insert_url_first(url):
    conn = sqlite3.connect(database_path)
    cur = conn.cursor()
    try:
        cur.execute('INSERT OR REPLACE INTO urls (id, url, processed) VALUES ((SELECT MIN(id) FROM urls) - 1, ?, 0)', (url,))
    except sqlite3.IntegrityError:
        pass
    conn.commit()
    conn.close()

def download_webpage(url, context, retry_count=5, backoff_factor=0.5):
    encoded_url = quote(url, safe='/:#?&=@[]!$&\'()*+,;')
    for attempt in range(retry_count):
        try:
            page = context.new_page()
            page.goto(encoded_url, wait_until='load')
            break
        except Exception as e:
            wait_time = backoff_factor * (2 ** attempt) + random.uniform(0, 1)
            logger.error(f"Failed to process {url} on attempt {attempt+1}: {str(e)}. Retrying in {wait_time} seconds.")
            time.sleep(wait_time)
    else:
        logger.error(f"Failed to process {url} after {retry_count} attempts.")
        return

    local_url = urlparse(url)._replace(netloc="", scheme="")
    local_folder = JW_DIR + urlunparse(map(unquote, local_url))

    os.makedirs(local_folder, exist_ok=True)
    time.sleep(5)
    page_content = page.content()
    bs_page = BeautifulSoup(page_content, "html.parser")

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

    for tag, attribute_name, full_url in assets:
        asset_basename = os.path.basename(urlparse(full_url).path) or 'index.html'
        
        if '?' in asset_basename:
            asset_basename = asset_basename.split('?')[0]
        
        if 'cast_sender.js' in asset_basename:
            asset_basename = 'cast_sender.js'

        local_asset_path = os.path.join(JW_DIR, "assets", asset_basename)
        full_asset_url = full_url
        
        if os.path.exists(local_asset_path):
            logger.info(f"Asset already exists locally: {local_asset_path}")
            new_asset_url = f"{JW_LOCAL_BASE_URL}/assets/{asset_basename}"
            tag[attribute_name] = new_asset_url
            continue

        if is_valid_url(full_asset_url):
            try:
                if download_asset(full_asset_url, local_asset_path):
                    new_asset_url = f"{JW_LOCAL_BASE_URL}/assets/{asset_basename}"
                    tag[attribute_name] = new_asset_url
            except Exception as e:
                logger.error(f"Failed to download {full_asset_url}: {str(e)}")

    for tag in bs_page.find_all(["a", "link", "base"], href=True):
        href = tag['href']
        if href.startswith("https://www.jw.org/"):
            tag['href'] = href.replace("https://www.jw.org", JW_LOCAL_BASE_URL)
        elif href.startswith("/"):
            tag['href'] = urljoin(f"{JW_LOCAL_BASE_URL}/{JW_LANG}", href.lstrip("/"))

    new_links = set()
    for tag in bs_page.find_all("a", href=True):
        link = urljoin(url, tag['href'])
        if is_jw_language_url(link):
            new_links.add(link)

    add_urls_to_db(new_links)

    for lnc_popup in bs_page.find_all("div", class_="lnc-firstRunPopup"):
        lnc_popup.decompose()

    local_file_name = os.path.join(local_folder, "index.html")
    with open(local_file_name, "w", encoding="utf-8") as file:
        file.write(str(bs_page))
    
    page.close()

def download_and_update_scripts(script_tags, JW_DIR, JW_LOCAL_BASE_URL):
    for tag in script_tags:
        script_url = tag['src']
        asset_basename = os.path.basename(urlparse(script_url).path)
        
        local_asset_path = os.path.join(JW_DIR, "assets", asset_basename)
        if not download_asset(script_url, local_asset_path):
            continue

        new_script_url = f"{JW_LOCAL_BASE_URL}/assets/{asset_basename}"
        tag['src'] = new_script_url

        with open(local_asset_path, 'r', encoding='utf-8') as file:
            script_content = file.read()

        updated_content = replace_https_links(script_content, JW_DIR, JW_LOCAL_BASE_URL)

        with open(local_asset_path, 'w', encoding='utf-8') as file:
            file.write(updated_content)

def replace_https_links(content, JW_DIR, JW_LOCAL_BASE_URL):
    https_links = re.findall(r'https://[^\s\'"]+', content)
    
    for https_link in https_links:
        asset_basename = os.path.basename(urlparse(https_link).path)
        local_asset_path = os.path.join(JW_DIR, "assets", asset_basename)

        if download_asset(https_link, local_asset_path):
            new_asset_url = f"{JW_LOCAL_BASE_URL}/assets/{asset_basename}"
            content = content.replace(https_link, new_asset_url)
    
    return content

if __name__ == '__main__':
    if not os.path.exists(JW_DIR):
        os.makedirs(JW_DIR)
        
    init_db()

    original_links = get_sitemap()
    add_urls_to_db(original_links)
    if "https://www.jw.org/" + JW_LANG not in original_links:
        add_url_to_db("https://www.jw.org/" + JW_LANG)

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-GB,en-NZ;q=0.9,en-AU;q=0.8,en;q=0.7,en-US;q=0.6",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win32; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    with sync_playwright() as p:
        browser = p.firefox.connect('ws://192.168.10.100:3030/firefox/playwright?token={JW_BROWSERLESS_KEY}')
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
                time.sleep(5)  # Add a delay before attempting the next URL as well

        browser.close()
