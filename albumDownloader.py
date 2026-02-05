"""
Facebook Album Downloader

A Python script to download Facebook albums. Facebook's built-in download
feature requires album ownership and sometimes fails on large albums.
With this script, you can download any public album.

Usage:
    python albumDownloader.py <album_url>
    python albumDownloader.py <album_url> --output <output_folder>
    python albumDownloader.py <album_url> --headless
"""

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# Configuration
DEFAULT_OUTPUT_FOLDER = "downloadedImgs"
MAX_WORKERS = 5  # Number of concurrent downloads
SCROLL_PAUSE_TIME = 1.5
PAGE_LOAD_TIMEOUT = 10
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
MIN_ALBUM_TITLE_LENGTH = 10
MAX_ALBUM_TITLE_LENGTH = 100


def sanitize_filename(name):
    """
    Remove or replace invalid characters from a filename.
    Works on Windows, macOS, and Linux.
    """
    # Replace common problematic characters
    invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    for char in invalid_chars:
        name = name.replace(char, '-')
    # Remove leading/trailing whitespace and dots
    name = name.strip().strip('.')
    # If empty after sanitization, use a default name
    return name if name else "album"


def create_folder(main_folder, album_title):
    """
    Create the output folder structure.
    Uses os.path.join for cross-platform compatibility.
    """
    if not os.path.exists(main_folder):
        os.makedirs(main_folder)

    full_path = os.path.join(main_folder, album_title)
    if not os.path.exists(full_path):
        os.makedirs(full_path)

    return full_path


def download_single_image(args):
    """
    Download a single image with retry logic.
    Args is a tuple of (index, img_url, output_path, session).
    Returns a tuple of (index, success, message).
    """
    index, img_url, output_path, session = args

    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(img_url, stream=True, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                # Determine file extension from content-type or URL
                content_type = response.headers.get('content-type', '')
                if 'jpeg' in content_type or 'jpg' in content_type:
                    ext = 'jpg'
                elif 'png' in content_type:
                    ext = 'png'
                elif 'gif' in content_type:
                    ext = 'gif'
                elif 'webp' in content_type:
                    ext = 'webp'
                else:
                    ext = 'jpg'  # Default to jpg

                filename = f"{index + 1}.{ext}"
                file_path = os.path.join(output_path, filename)

                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)

                return (index, True, f"Downloaded: {filename}")
            else:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1)
                    continue
                return (index, False, f"Failed to download image {index + 1}: HTTP {response.status_code}")

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(1)
                continue
            return (index, False, f"Failed to download image {index + 1}: {str(e)}")

    return (index, False, f"Failed to download image {index + 1} after {MAX_RETRIES} attempts")


def download_images_parallel(img_urls, output_path):
    """
    Download all images in parallel using ThreadPoolExecutor.
    Shows progress as images are downloaded.
    """
    total = len(img_urls)
    successful = 0
    failed = 0

    print(f"\nDownloading {total} images to: {output_path}")
    print("-" * 50)

    # Create a session for connection pooling
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })

    # Prepare arguments for parallel download
    download_args = [
        (i, url, output_path, session)
        for i, url in enumerate(img_urls)
    ]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_single_image, args): args[0]
            for args in download_args
        }

        for future in as_completed(futures):
            index, success, message = future.result()
            if success:
                successful += 1
                print(f"[{successful + failed}/{total}] {message}")
            else:
                failed += 1
                print(f"[{successful + failed}/{total}] {message}")

    session.close()
    print("-" * 50)
    print(f"Download complete: {successful} successful, {failed} failed")
    return successful, failed


def scroll_to_load_all(driver, max_scroll_attempts=50):
    """
    Scroll down to load all photos in the album.
    Uses a more reliable scrolling approach with maximum attempts.
    """
    print("Loading all photos in album...")

    last_height = driver.execute_script("return document.body.scrollHeight")
    scroll_attempts = 0
    no_change_count = 0

    while scroll_attempts < max_scroll_attempts:
        # Scroll down
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE_TIME)

        # Check for new content
        new_height = driver.execute_script("return document.body.scrollHeight")

        if new_height == last_height:
            no_change_count += 1
            # If no change for 3 consecutive checks, assume we've loaded everything
            if no_change_count >= 3:
                break
        else:
            no_change_count = 0

        last_height = new_height
        scroll_attempts += 1

    print(f"Scrolling complete after {scroll_attempts} scroll(s)")


def find_photo_links(soup):
    """
    Find photo links in the page using multiple strategies.
    This is more robust than relying on specific class names that change frequently.
    """
    photo_links = []

    # Strategy 1: Find links that point to photo pages (contain /photo/)
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        if '/photo/' in href or '/photos/' in href:
            if href.startswith('/'):
                href = f"https://www.facebook.com{href}"
            elif not href.startswith('http'):
                continue
            if href not in photo_links:
                photo_links.append(href)

    # Strategy 2: Find links with photo-related data attributes
    if not photo_links:
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            # Look for links with image thumbnails inside
            if link.find('img') and 'fbid=' in href:
                if href.startswith('/'):
                    href = f"https://www.facebook.com{href}"
                if href not in photo_links:
                    photo_links.append(href)

    return photo_links


def extract_high_res_image(driver, photo_url):
    """
    Navigate to a photo page and extract the high-resolution image URL.
    Uses multiple strategies to find the image.
    """
    try:
        driver.get(photo_url)
        time.sleep(1)

        # Wait for page to load
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "img"))
        )

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # Strategy 1: Find the largest image by checking data-visualcompletion
        # or specific class patterns used by Facebook for photo viewers
        all_imgs = soup.find_all('img')

        candidate_images = []
        for img in all_imgs:
            src = img.get('src', '')
            # Skip small images, profile pics, and icons
            if not src or 'emoji' in src.lower():
                continue
            if 'scontent' in src:  # Facebook CDN images
                candidate_images.append(src)

        # Return the image URL that's likely the main photo
        # Usually it's one of the larger images with scontent CDN
        if candidate_images:
            # Prefer images with specific size indicators or the longest URL
            # (high-res images typically have more parameters)
            return max(candidate_images, key=len)

        return None

    except Exception as e:
        print(f"  Error extracting image from {photo_url}: {str(e)}")
        return None


def extract_album_title(soup):
    """
    Extract the album title from the page using multiple strategies.
    """
    # Strategy 1: Look for common heading patterns
    for tag in ['h1', 'h2', 'span', 'div']:
        elements = soup.find_all(tag)
        for elem in elements:
            text = elem.get_text(strip=True)
            # Album titles are usually short and don't contain URLs
            if text and MIN_ALBUM_TITLE_LENGTH < len(text) < MAX_ALBUM_TITLE_LENGTH and 'http' not in text.lower():
                # Check if it looks like an album title
                if not any(x in text.lower() for x in ['log in', 'sign up', 'facebook', 'menu']):
                    return text

    # Strategy 2: Use page title
    title_tag = soup.find('title')
    if title_tag:
        title = title_tag.get_text(strip=True)
        # Remove "Facebook" from title
        title = re.sub(r'\s*[-|]\s*Facebook.*$', '', title, flags=re.IGNORECASE)
        if title:
            return title

    return "Facebook_Album"


def create_driver(headless=False):
    """
    Create and configure a Firefox WebDriver.
    """
    options = Options()

    if headless:
        options.add_argument("--headless")

    # Additional options for stability
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    try:
        driver = webdriver.Firefox(options=options)
        driver.set_page_load_timeout(30)
        return driver
    except Exception as e:
        print(f"Error creating Firefox driver: {e}")
        print("Make sure Firefox and geckodriver are installed.")
        sys.exit(1)


def validate_url(url):
    """
    Validate that the URL is a valid Facebook album URL.
    """
    parsed = urlparse(url)

    if 'facebook.com' not in parsed.netloc:
        return False, "URL must be a Facebook URL"

    # Check for album indicators
    if 'media/set' in url or 'set=' in url or '/album' in url or '/photos' in url:
        return True, None

    return True, "Warning: URL may not be an album page, but will try anyway"


def do_scraping(album_url, output_folder=DEFAULT_OUTPUT_FOLDER, headless=False):
    """
    Main scraping function with improved error handling and efficiency.
    """
    # Validate URL
    is_valid, message = validate_url(album_url)
    if not is_valid:
        print(f"Error: {message}")
        return False
    if message:
        print(message)

    print(f"\nStarting Facebook Album Downloader")
    print(f"Album URL: {album_url}")
    print(f"Output folder: {output_folder}")
    print("-" * 50)

    driver = None
    try:
        # Create browser driver
        print("Launching browser...")
        driver = create_driver(headless=headless)

        # Navigate to album
        print("Navigating to album page...")
        driver.get(album_url)
        time.sleep(3)  # Allow page to load

        # Scroll to load all photos
        scroll_to_load_all(driver)

        # Parse the page
        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # Extract album title
        album_title = extract_album_title(soup)
        album_title = sanitize_filename(album_title)
        print(f"Album title: {album_title}")

        # Find photo links
        print("Finding photo links...")
        photo_links = find_photo_links(soup)
        print(f"Found {len(photo_links)} photo links")

        if not photo_links:
            print("No photos found. The page structure may have changed or the album may be private.")
            return False

        # Extract high-resolution image URLs
        print("\nExtracting high-resolution image URLs...")
        img_urls = []
        for i, photo_link in enumerate(photo_links):
            print(f"  Processing photo {i + 1}/{len(photo_links)}...", end='\r')
            img_url = extract_high_res_image(driver, photo_link)
            if img_url:
                img_urls.append(img_url)

        print(f"\nExtracted {len(img_urls)} image URLs")

        if not img_urls:
            print("No image URLs could be extracted.")
            return False

        # Create output folder
        output_path = create_folder(output_folder, album_title)

        # Download images in parallel
        successful, failed = download_images_parallel(img_urls, output_path)

        return successful > 0

    except KeyboardInterrupt:
        print("\nDownload cancelled by user")
        return False

    except Exception as e:
        print(f"\nError during scraping: {str(e)}")
        return False

    finally:
        if driver:
            print("\nClosing browser...")
            driver.quit()


def main():
    """
    Main entry point with argument parsing.
    """
    parser = argparse.ArgumentParser(
        description="Download Facebook albums easily",
        epilog="Example: python albumDownloader.py https://facebook.com/media/set/?set=a.123456789"
    )

    parser.add_argument(
        'album_url',
        nargs='?',
        help='URL of the Facebook album to download'
    )

    parser.add_argument(
        '-o', '--output',
        default=DEFAULT_OUTPUT_FOLDER,
        help=f'Output folder for downloaded images (default: {DEFAULT_OUTPUT_FOLDER})'
    )

    parser.add_argument(
        '--headless',
        action='store_true',
        help='Run browser in headless mode (no visible window)'
    )

    args = parser.parse_args()

    # If no URL provided, prompt for it
    if not args.album_url:
        print("Facebook Album Downloader")
        print("-" * 30)
        args.album_url = input("Enter the Facebook album URL: ").strip()

        if not args.album_url:
            print("Error: No URL provided")
            sys.exit(1)

    # Run the scraper
    success = do_scraping(
        album_url=args.album_url,
        output_folder=args.output,
        headless=args.headless
    )

    if success:
        print("\n✓ Album download completed successfully!")
        sys.exit(0)
    else:
        print("\n✗ Album download failed or incomplete")
        sys.exit(1)


if __name__ == "__main__":
    main()
