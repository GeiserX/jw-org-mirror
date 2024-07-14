import os
from bs4 import BeautifulSoup
import requests
import requests_cache
from urllib.parse import urlparse, urlunparse
import time
import shutil
import logging
import sys
from playwright.sync_api import sync_playwright

language = "es"
fulldir = "/jworg"

logger = logging.getLogger('mylogger')
logger.setLevel(logging.DEBUG)  # set logger level
logFormatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
consoleHandler = logging.StreamHandler(sys.stdout)  # set streamhandler to stdout
consoleHandler.setFormatter(logFormatter)
logger.addHandler(consoleHandler)


def get_sitemap():
    requests_cache.install_cache(fulldir + "/" + language + '/sitemap')
    session = requests_cache.CachedSession('sitemap', expire_after=86400)  # 24h
    jw_org = "https://www.jw.org/"
    links = []
    r = requests.get(jw_org + language + "/sitemap.xml")
    soup = BeautifulSoup(r.text, "lxml")
    url_locs = soup.find_all("loc")
    links.append(jw_org + language)
    for url in url_locs:
        links.append(url.text)
    return links
    # return ["https://www.jw.org/es/biblioteca/videos/#es/mediaitems/FeaturedLibraryVideos/docid-502018518_1_VIDEO"]


def download_video(url, filename):
    with requests.get(url, stream=True, timeout=120) as r:
        with open(filename, "wb") as f:
            shutil.copyfileobj(r.raw, f)


def download_webpage(url, context):
    page = context.new_page()
    page.goto(url, wait_until='load')
    local_url = urlparse(url)._replace(netloc="", scheme="")
    local_folder = fulldir + urlunparse(local_url)
    if not os.path.exists(local_folder):
        os.makedirs(local_folder)
    time.sleep(3)
    page_content = page.content()
    bs_page = BeautifulSoup(page_content, "html.parser")

    # video page
    video = bs_page.find("video")
    if video:
        video_url = video["src"]
        video_filename = os.path.basename(urlparse(video_url).path)
        local_video_path = local_folder + "/" + video_filename
        print(local_video_path)
        download_video(video_url, local_video_path)
        video["src"] = "/" + local_video_path
        local_folder_name = local_folder + ".html"  # If it's a video, do not add /index.html
    else:
        local_folder_name = local_folder + "/index.html"

    # href modification
    for tag_name, attribute_name in [("a", "href"), ("link", "href"), ("base", "href")]:
        for tag in bs_page.find_all(tag_name, **{attribute_name: True}):
            if tag[attribute_name].startswith("https://www.jw.org/"):
                tag[attribute_name] = tag[attribute_name].replace("https://www.jw.org", "https://jw.filmmonitor.co.uk")

    with open(local_folder_name, "w", encoding="utf-8") as file:
        file.write(str(bs_page))


if __name__ == '__main__':
    if not os.path.exists(fulldir):
        os.makedirs(fulldir)
    links = get_sitemap()
    headers = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-GB,en-NZ;q=0.9,en-AU;q=0.8,en;q=0.7,en-US;q=0.6",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    with sync_playwright() as p:
        # browser = p.chromium.launch()
        browser = p.firefox.connect('ws://192.168.10.100:3030/firefox/playwright?token=YGxoYfrARhtkSVxyfbfLwHc9me4afP9VS3y89EVa')
        context = browser.new_context(extra_http_headers=headers)
        for link in links:
            logger.info(link)
            download_webpage(link, context)
        browser.close()

