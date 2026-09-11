"""
Facebook Album Downloader

A Python script to download Facebook albums. Facebook's built-in download
feature requires album ownership and sometimes fails on large albums.
With this script, you can download public albums as well as albums
accessible through your Facebook account using authentication.

Usage:
    python albumDownloader.py <album_url>
    python albumDownloader.py <album_url> --output <output_folder>
    python albumDownloader.py <album_url> --headless
    python albumDownloader.py <album_url> --login
    python albumDownloader.py --login
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# Configuration
DEFAULT_OUTPUT_FOLDER = "downloadedImgs"
DEFAULT_COOKIE_FILE = "facebook_cookies.json"
MAX_WORKERS = 5  # Number of concurrent downloads
SCROLL_PAUSE_TIME = 1.5
PAGE_LOAD_TIMEOUT = 10
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
LOGIN_TIMEOUT = 300  # Max wait time for interactive login (5 minutes)
MIN_ALBUM_TITLE_LENGTH = 10
MAX_ALBUM_TITLE_LENGTH = 100


def resolve_cookie_path(cookie_path=DEFAULT_COOKIE_FILE):
    """
    Resolve cookie file path, checking cwd and the script's directory.
    """
    if not cookie_path:
        return None
    if os.path.isabs(cookie_path):
        return cookie_path if os.path.exists(cookie_path) else None
    if os.path.exists(cookie_path):
        return os.path.abspath(cookie_path)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, cookie_path)
    if os.path.exists(script_path):
        return script_path
    return None


def save_cookies(driver, cookie_path=DEFAULT_COOKIE_FILE):
    """
    Save Facebook session cookies to a JSON file.
    """
    try:
        cookies = driver.get_cookies()
        if not cookies:
            print("Warning: No cookies found to save.")
            return False

        with open(cookie_path, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, indent=2)
        print(f"Session cookies saved to: {cookie_path}")
        return True
    except Exception as e:
        print(f"Warning: Failed to save cookies to {cookie_path}: {e}")
        return False


def get_authenticated_user_id(driver):
    """
    Check whether the browser session is authenticated to Facebook and return (is_auth, user_id).
    Facebook sets the 'c_user' cookie containing the logged-in user ID upon authentication.
    """
    try:
        cookies = {c.get('name'): c.get('value') for c in driver.get_cookies()}
        user_id = cookies.get('c_user')
        if user_id:
            return True, user_id
        return False, None
    except Exception:
        return False, None


def is_authenticated(driver):
    """
    Check whether the browser session is authenticated to Facebook.
    """
    auth, _ = get_authenticated_user_id(driver)
    return auth


def load_cookies(driver, cookie_path=DEFAULT_COOKIE_FILE):
    """
    Load saved session cookies into the browser.
    Opens facebook.com first (as required by Selenium), clears initial
    guest cookies to avoid collisions, injects the cookies, and reloads
    facebook.com to activate the authenticated session.
    """
    resolved_path = resolve_cookie_path(cookie_path)
    if not resolved_path:
        return False

    try:
        with open(resolved_path, 'r', encoding='utf-8') as f:
            cookies = json.load(f)

        if not cookies:
            print(f"Warning: Cookie file {resolved_path} is empty.")
            return False

        # Step 1: Open facebook.com first before trying to insert cookies
        print(f"Opening facebook.com before inserting cookies from {os.path.basename(resolved_path)}...")
        driver.get("https://www.facebook.com")
        time.sleep(2)

        # Step 2: Delete guest cookies assigned on initial load to avoid collision
        try:
            driver.delete_all_cookies()
        except Exception:
            pass

        # Step 3: Insert saved cookies
        loaded_count = 0
        for cookie in cookies:
            cookie_dict = {
                'name': cookie['name'],
                'value': cookie['value'],
            }
            if 'domain' in cookie:
                cookie_dict['domain'] = cookie['domain']
            if 'path' in cookie:
                cookie_dict['path'] = cookie['path']
            if 'expiry' in cookie:
                cookie_dict['expiry'] = int(cookie['expiry'])
            if 'secure' in cookie:
                cookie_dict['secure'] = bool(cookie['secure'])
            if 'httpOnly' in cookie:
                cookie_dict['httpOnly'] = bool(cookie['httpOnly'])
            if 'sameSite' in cookie and cookie['sameSite'] in ['Strict', 'Lax', 'None']:
                cookie_dict['sameSite'] = cookie['sameSite']

            try:
                driver.add_cookie(cookie_dict)
                loaded_count += 1
            except Exception:
                # If domain constraint failed, retry without explicit domain
                try:
                    alt_dict = dict(cookie_dict)
                    alt_dict.pop('domain', None)
                    driver.add_cookie(alt_dict)
                    loaded_count += 1
                except Exception:
                    pass

        print(f"Inserted {loaded_count} session cookies into browser.")

        # Step 4: Reload facebook.com to activate authenticated session
        print("Reloading facebook.com to activate session...")
        driver.get("https://www.facebook.com")
        time.sleep(2)

        # Step 5: Check if authenticated
        authenticated, user_id = get_authenticated_user_id(driver)
        if authenticated:
            if user_id:
                print(f"[+] Authenticated session active (Facebook User ID: {user_id}).")
            else:
                print("[+] Authenticated session active.")
            return True
        else:
            print("Warning: Cookies loaded, but session does not appear to be authenticated (c_user not found).")
            return False

    except Exception as e:
        print(f"Warning: Failed to load cookies from {resolved_path}: {e}")
        return False


def wait_for_login(driver, timeout=LOGIN_TIMEOUT):
    """
    Open the Facebook login page and wait for the user to log in inside the browser.
    Detects successful login automatically (via cookies/URL redirect) or allows the user
    to press Enter in the console to proceed.
    """
    login_url = "https://www.facebook.com/login.php"
    print("\n" + "=" * 60)
    print("FACEBOOK AUTHENTICATION")
    print("=" * 60)
    print("Opening Facebook login page in your browser...")
    print("1. Enter your credentials in the browser window.")
    print("2. Complete any 2FA or security prompts if required.")
    print("3. Once logged in, the script will detect your session automatically,")
    print("   or you can press ENTER here in the terminal to continue.")
    print("=" * 60 + "\n")

    try:
        driver.get(login_url)
    except Exception as e:
        print(f"Error navigating to login page: {e}")
        return False

    user_confirmed = threading.Event()

    def wait_for_enter_key():
        try:
            sys.stdin.readline()
            user_confirmed.set()
        except Exception:
            pass

    enter_listener = threading.Thread(target=wait_for_enter_key, daemon=True)
    enter_listener.start()

    start_time = time.time()
    last_prompt_time = start_time

    while time.time() - start_time < timeout:
        # Check if user pressed Enter in console
        if user_confirmed.is_set():
            if is_authenticated(driver):
                print("Login confirmed via terminal input and authenticated successfully!")
                return True
            else:
                print("Enter pressed. Checking authentication status...")
                try:
                    current_url = driver.current_url.lower()
                    if 'login' not in current_url:
                        print("Proceeding with active session...")
                        return True
                    else:
                        print("Warning: Still on login page. Continuing to wait for login...")
                        user_confirmed.clear()
                        enter_listener = threading.Thread(target=wait_for_enter_key, daemon=True)
                        enter_listener.start()
                except Exception:
                    return False

        # Automated detection
        try:
            current_url = driver.current_url.lower()
            if is_authenticated(driver):
                transitional_keywords = ['login', 'checkpoint', 'two_step_verification', 'recover', 'confirm']
                if not any(k in current_url for k in transitional_keywords):
                    print("Login detected automatically!")
                    time.sleep(2)
                    return True
        except WebDriverException:
            print("Browser window was closed or disconnected.")
            return False

        # Status update every 15 seconds
        if time.time() - last_prompt_time >= 15:
            remaining = int(timeout - (time.time() - start_time))
            print(f"Waiting for login inside the browser... ({remaining}s remaining - or press Enter here)")
            last_prompt_time = time.time()

        time.sleep(1.5)

    print(f"\nAuthentication timed out after {timeout} seconds.")
    return False


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


def download_images_parallel(img_urls, output_path, cookies=None):
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

    # Apply authenticated cookies if available
    if cookies:
        for cookie in cookies:
            session.cookies.set(
                cookie['name'],
                cookie['value'],
                domain=cookie.get('domain', '.facebook.com'),
                path=cookie.get('path', '/')
            )

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
                lower_text = text.lower()
                ignored_keywords = ['log in', 'sign up', 'facebook', 'menu', 'notification', 'create', 'search']
                if not any(x in lower_text for x in ignored_keywords):
                    return text

    # Strategy 2: Use page title
    title_tag = soup.find('title')
    if title_tag:
        title = title_tag.get_text(strip=True)
        # Remove notification count like "(3) " and "Facebook" suffix
        title = re.sub(r'^\(\d+\)\s*', '', title)
        title = re.sub(r'\s*[-|]\s*Facebook.*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'^Facebook.*$', '', title, flags=re.IGNORECASE).strip()
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


def do_scraping(album_url=None, output_folder=DEFAULT_OUTPUT_FOLDER, headless=False,
                login=False, cookie_file=DEFAULT_COOKIE_FILE, save_cookies_flag=True):
    """
    Main scraping function with authentication and improved error handling.
    """
    resolved_cookie_file = resolve_cookie_path(cookie_file)
    has_cookie_file = resolved_cookie_file is not None and os.path.exists(resolved_cookie_file)

    # Interactive login requires a visible browser window only if no valid cookies exist
    if login and not has_cookie_file and headless:
        print("Notice: Interactive login requested. Disabling headless mode so you can log in.")
        headless = False

    # Standalone login mode (no album URL provided)
    if not album_url:
        if login:
            print("\nStarting Facebook Album Downloader (Login Only)")
            print("-" * 50)
            driver = None
            try:
                driver = create_driver(headless=False)
                if wait_for_login(driver):
                    if save_cookies_flag:
                        save_cookies(driver, cookie_file)
                    return True
                return False
            except Exception as e:
                print(f"\nError during login: {str(e)}")
                return False
            finally:
                if driver:
                    print("Closing browser...")
                    driver.quit()
        else:
            print("Error: No album URL provided.")
            return False

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
    if has_cookie_file and save_cookies_flag:
        print(f"Authentication: Using saved cookies from {os.path.basename(resolved_cookie_file)}")
    elif login:
        print("Authentication: Interactive login enabled")
    print("-" * 50)

    driver = None
    try:
        # Create browser driver
        print("Launching browser...")
        driver = create_driver(headless=headless)

        # Attempt to restore session from cookie file first
        session_active = False
        if save_cookies_flag and has_cookie_file:
            print(f"Restoring session from {os.path.basename(resolved_cookie_file)}...")
            session_active = load_cookies(driver, resolved_cookie_file)

        # If interactive login was explicitly requested
        if login:
            if session_active:
                print("[+] Already authenticated using saved cookies.")
            else:
                if headless:
                    print("Notice: Disabling headless mode for interactive authentication.")
                    driver.quit()
                    driver = create_driver(headless=False)

                if not wait_for_login(driver):
                    print("Authentication failed or was cancelled.")
                    return False
                if save_cookies_flag:
                    save_cookies(driver, cookie_file)
                session_active = True

        # Navigate to album
        print("Navigating to album page...")
        driver.get(album_url)
        time.sleep(3)  # Allow page to load

        # Check if Facebook redirected to login
        current_url = driver.current_url.lower()
        if 'login' in current_url or 'checkpoint' in current_url:
            print("\nThis album requires authentication to view.")
            if headless:
                print("Error: Album requires login, but running in --headless mode.")
                print("Please rerun with --login without --headless to authenticate first.")
                return False

            print("Opening login page for authentication...")
            if not wait_for_login(driver):
                print("Authentication failed or was cancelled.")
                return False

            if save_cookies_flag:
                save_cookies(driver, cookie_file)

            print("\nRe-navigating to album page...")
            driver.get(album_url)
            time.sleep(3)

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
            print("No photos found. The page structure may have changed or the album may be private/inaccessible.")
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

        # Download images in parallel with authenticated session cookies
        successful, failed = download_images_parallel(
            img_urls,
            output_path,
            cookies=driver.get_cookies()
        )

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
        epilog="Example: python albumDownloader.py https://facebook.com/media/set/?set=a.123456789 --login"
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

    parser.add_argument(
        '--login', '--auth',
        action='store_true',
        dest='login',
        help='Open Facebook login page in browser and wait for user to authenticate'
    )

    parser.add_argument(
        '--cookies',
        default=DEFAULT_COOKIE_FILE,
        help=f'File path to save/load Facebook session cookies (default: {DEFAULT_COOKIE_FILE})'
    )

    parser.add_argument(
        '--no-cookies',
        action='store_true',
        help='Do not save or load session cookies from disk'
    )

    args = parser.parse_args()

    # If no URL provided, prompt for it or handle login-only
    if not args.album_url:
        if args.login:
            print("Facebook Album Downloader - Authentication Mode")
            print("-" * 50)
            url_choice = input("Enter the Facebook album URL (or press Enter to only log in and save session): ").strip()
            if url_choice:
                args.album_url = url_choice
        else:
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
        headless=args.headless,
        login=args.login,
        cookie_file=args.cookies,
        save_cookies_flag=not args.no_cookies
    )

    if success:
        if not args.album_url and args.login:
            print("\n[+] Authentication completed and session saved successfully!")
        else:
            print("\n[+] Album download completed successfully!")
        sys.exit(0)
    else:
        if not args.album_url and args.login:
            print("\n[-] Authentication failed or incomplete")
        else:
            print("\n[-] Album download failed or incomplete")
        sys.exit(1)


if __name__ == "__main__":
    main()

