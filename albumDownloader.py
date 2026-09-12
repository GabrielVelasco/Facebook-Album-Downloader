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
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# Configuration
DEFAULT_OUTPUT_FOLDER = "downloadedImgs"
DEFAULT_COOKIE_FILE = "facebook_cookies.json"
DEFAULT_URLS_FILENAME = "album_urls.json"
ALT_URLS_FILENAME = "photo_urls.json"
MAX_WORKERS = 5  # Number of concurrent downloads
SCROLL_PAUSE_TIME = 2.0
DEFAULT_MAX_SCROLLS = 300
DEFAULT_PAGE_TIMEOUT = 30
PAGE_LOAD_TIMEOUT = 10
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
LOGIN_TIMEOUT = 300  # Max wait time for interactive login (5 minutes)
MIN_ALBUM_TITLE_LENGTH = 10
MAX_ALBUM_TITLE_LENGTH = 100
RECYCLE_BATCH_SIZE = 40  # Proactively recycle browser session every 40 photos to prevent memory leak and actor desync


def is_fatal_driver_error(exc):
    """
    Check whether an exception indicates the WebDriver session has crashed,
    desynchronized, or has an invalid/inactive Marionette actor.
    """
    if not exc:
        return False
    msg = str(exc).lower()
    fatal_patterns = [
        "inactiveactor",
        "actor is no longer active",
        "nosuchwindow",
        "no such window",
        "invalidsessionid",
        "invalid session id",
        "failed to decode response from marionette",
        "connection refused",
        "broken pipe",
        "connection reset",
        "max retries exceeded with url",
        "target window already closed",
        "not connected to devtools",
        "disconnected",
    ]
    if isinstance(exc, (NoSuchWindowException, InvalidSessionIdException)):
        return True
    return any(pattern in msg for pattern in fatal_patterns)


def heal_driver(driver):
    """
    Attempt lightweight in-place recovery of a desynchronized driver session:
    - Discard any subframe / iframe context and return to top-level document
    - Re-select the primary window handle
    - Check responsiveness with a trivial script execution
    Returns True if healed, False if recovery failed.
    """
    if not driver:
        return False
    try:
        driver.switch_to.default_content()
        handles = driver.window_handles
        if not handles:
            return False
        current = None
        try:
            current = driver.current_window_handle
        except Exception:
            pass
        if not current or current not in handles:
            driver.switch_to.window(handles[0])
        driver.execute_script("return 1;")
        return True
    except Exception:
        return False


class BrowserSession:
    """
    Manages the lifecycle of a Selenium browser instance:
    - Creation and configuration with memory optimizations
    - Cookie restoration
    - In-place session healing
    - Automatic restart/recovery upon fatal errors (e.g. InactiveActor)
    - Proactive recycling during bulk extractions to prevent memory exhaustion
    """
    def __init__(self, headless=False, page_timeout=DEFAULT_PAGE_TIMEOUT,
                 cookie_file=DEFAULT_COOKIE_FILE, save_cookies_flag=True,
                 recycle_interval=RECYCLE_BATCH_SIZE):
        self.headless = headless
        self.page_timeout = page_timeout
        self.cookie_file = cookie_file
        self.save_cookies_flag = save_cookies_flag
        self.recycle_interval = recycle_interval
        self.driver = None
        self.photos_since_restart = 0

    def start(self):
        """Launch a new browser and restore cookies if available."""
        if self.driver is not None:
            self.quit()
        self.driver = create_driver(headless=self.headless, page_load_timeout=self.page_timeout)
        if self.save_cookies_flag and self.cookie_file:
            resolved = resolve_cookie_path(self.cookie_file)
            if resolved:
                load_cookies(self.driver, resolved)
        self.photos_since_restart = 0
        return self.driver

    def get_driver(self):
        """Get the active driver, starting a new one if not running."""
        if self.driver is None:
            return self.start()
        return self.driver

    def restart(self, reason=None):
        """Cleanly close existing driver and start a fresh session."""
        if reason:
            print(f"\n[!] {reason}. Restarting browser session...")
        else:
            print("\n[!] Restarting browser session...")
        self.quit()
        return self.start()

    def check_and_recycle(self):
        """
        Increment extraction counter and proactively recycle if interval reached.
        Returns the active driver.
        """
        self.photos_since_restart += 1
        if self.recycle_interval and self.photos_since_restart >= self.recycle_interval:
            print(f"\n[+] Proactively recycling browser after {self.photos_since_restart} photos to free memory...")
            return self.restart("Scheduled session refresh")
        return self.get_driver()

    def recover(self, exc=None):
        """
        Recover from a driver error. First tries in-place healing;
        if that fails, restarts the browser cleanly and restores cookies.
        Returns the active, healthy driver.
        """
        if self.driver is not None:
            if heal_driver(self.driver):
                return self.driver

        err_str = str(exc).splitlines()[0] if exc else "Session error"
        return self.restart(f"Browser recovered from: {err_str[:70]}")

    def quit(self):
        """Safely shut down the browser."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.quit()


def safe_get(driver, url):
    """
    Navigate to a URL using the driver, handling page load timeouts gracefully.
    If navigation times out (common on Facebook due to long-polling / telemetry / media prefetch),
    stops page loading via window.stop() so subsequent DOM inspection can continue.
    Propagates fatal driver errors (like InactiveActor or disconnected session) for recovery.
    """
    try:
        driver.get(url)
    except TimeoutException:
        try:
            driver.execute_script("window.stop();")
        except Exception as stop_exc:
            if is_fatal_driver_error(stop_exc):
                raise stop_exc
    except Exception as e:
        if is_fatal_driver_error(e):
            raise
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
    # Brief settling delay to allow Facebook router / DOM to stabilize
    time.sleep(0.5)


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
        safe_get(driver, "https://www.facebook.com")
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
        safe_get(driver, "https://www.facebook.com")
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
        safe_get(driver, login_url)
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



def normalize_photo_id(url):
    """
    Extract a canonical identifier for a Facebook photo URL.
    Handles various Facebook photo URL formats:
      - /photo/?fbid=123456789...
      - /photo.php?fbid=123456789...
      - /username/photos/a.123/456789/
      - /photos/pcb.123/456789/
    Falls back to cleaned URL without session/ephemeral query strings.
    """
    if not url:
        return ""
    # Check for fbid query parameter
    match = re.search(r'[?&]fbid=(\d+)', url)
    if match:
        return f"fbid_{match.group(1)}"

    # Check for /photos/[...]/(\d+)
    match = re.search(r'/photos/(?:[^/]+/)?(\d+)', url)
    if match:
        return f"photo_{match.group(1)}"

    # Check for pcb.(\d+)
    match = re.search(r'pcb\.(\d+)', url)
    if match:
        return f"pcb_{match.group(1)}"

    # Fallback to normalized URL without query strings
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip('/')
    return clean_url.lower() if clean_url else url.strip().lower()


def get_manifest_path(output_path, custom_path=None):
    """
    Determine the path to the saved URLs manifest file.
    If custom_path is given, returns that path.
    Otherwise checks if album_urls.json or photo_urls.json exists in output_path,
    defaulting to DEFAULT_URLS_FILENAME (album_urls.json).
    """
    if custom_path:
        return custom_path
    if not output_path:
        return DEFAULT_URLS_FILENAME

    candidate_album = os.path.join(output_path, DEFAULT_URLS_FILENAME)
    candidate_photo = os.path.join(output_path, ALT_URLS_FILENAME)

    if os.path.exists(candidate_album):
        return candidate_album
    if os.path.exists(candidate_photo):
        return candidate_photo

    return candidate_album


def load_saved_urls(file_path):
    """
    Load saved photo URLs from a JSON file on disk.
    Supports:
      1. Standard manifest schema:
         {"album_url": "...", "album_title": "...", "photos": [...]}
      2. List schema:
         [{"facebook_url": "...", "direct_url": "..."}, ...]
      3. Dictionary mapping schema:
         {"https://facebook.com/...": "https://scontent..."}
    Returns a standardized dictionary with keys:
      'album_url', 'album_title', 'photos'
    or None if the file does not exist or is invalid.
    """
    if not file_path or not os.path.exists(file_path):
        return None

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if isinstance(data, dict):
            if "photos" in data and isinstance(data["photos"], list):
                # Standard manifest schema
                photos = []
                for i, item in enumerate(data["photos"]):
                    if isinstance(item, dict):
                        p = {
                            "index": item.get("index", i + 1),
                            "facebook_url": item.get("facebook_url", item.get("fb_url", "")),
                            "direct_url": item.get("direct_url", item.get("url", item.get("img_url", ""))),
                            "filename": item.get("filename", ""),
                            "downloaded": bool(item.get("downloaded", False)),
                        }
                        if "extracted_at" in item:
                            p["extracted_at"] = item["extracted_at"]
                        photos.append(p)
                return {
                    "album_url": data.get("album_url", ""),
                    "album_title": data.get("album_title", ""),
                    "photos": photos,
                }
            else:
                # Key-value mapping: facebook_url -> direct_url
                photos = []
                for i, (fb_url, direct_url) in enumerate(data.items()):
                    if fb_url in ["album_url", "album_title", "total_photos", "extracted_count", "downloaded_count", "updated_at"]:
                        continue
                    photos.append({
                        "index": i + 1,
                        "facebook_url": fb_url,
                        "direct_url": direct_url if isinstance(direct_url, str) else "",
                        "filename": f"{i + 1}.jpg",
                        "downloaded": False,
                    })
                return {
                    "album_url": data.get("album_url", ""),
                    "album_title": data.get("album_title", ""),
                    "photos": photos,
                }

        elif isinstance(data, list):
            # List of photo objects or URL strings
            photos = []
            for i, item in enumerate(data):
                if isinstance(item, dict):
                    photos.append({
                        "index": item.get("index", i + 1),
                        "facebook_url": item.get("facebook_url", item.get("fb_url", "")),
                        "direct_url": item.get("direct_url", item.get("url", item.get("img_url", ""))),
                        "filename": item.get("filename", ""),
                        "downloaded": bool(item.get("downloaded", False)),
                    })
                elif isinstance(item, str):
                    photos.append({
                        "index": i + 1,
                        "facebook_url": "",
                        "direct_url": item,
                        "filename": f"{i + 1}.jpg",
                        "downloaded": False,
                    })
            return {
                "album_url": "",
                "album_title": "",
                "photos": photos,
            }

        return None

    except Exception as e:
        print(f"Warning: Could not read saved URLs file from {file_path}: {e}")
        return None


def save_urls_manifest(file_path, album_url, album_title, photos):
    """
    Save the photos manifest to disk atomically in JSON format.
    Uses a temporary file before renaming to prevent corrupted data on unexpected termination.
    """
    if not file_path:
        return False

    folder = os.path.dirname(os.path.abspath(file_path))
    if folder and not os.path.exists(folder):
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass

    manifest_data = {
        "album_url": album_url or "",
        "album_title": album_title or "",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_photos": len(photos),
        "extracted_count": sum(1 for p in photos if p.get("direct_url")),
        "downloaded_count": sum(1 for p in photos if p.get("downloaded")),
        "photos": photos,
    }

    temp_path = f"{file_path}.tmp_{os.getpid()}"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(manifest_data, f, indent=2, ensure_ascii=False)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        os.replace(temp_path, file_path)
        return True
    except Exception as e:
        print(f"Warning: Failed to save URLs manifest to {file_path}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return False


def detect_downloaded_images(photos, output_path):
    """
    Inspect the output directory on disk to check which photos have already been saved.
    Updates each photo's 'downloaded' and 'filename' fields in-place.
    Returns (downloaded_count, pending_count).
    """
    if not photos:
        return 0, 0

    if not os.path.exists(output_path):
        for p in photos:
            p["downloaded"] = False
        return 0, len(photos)

    valid_extensions = ['jpg', 'jpeg', 'png', 'webp', 'gif']
    downloaded_count = 0

    for p in photos:
        idx = p.get("index", 1)
        found = False
        found_filename = None

        # 1. Check existing recorded filename
        if p.get("filename"):
            candidate_path = os.path.join(output_path, p["filename"])
            if os.path.exists(candidate_path) and os.path.getsize(candidate_path) > 0:
                found = True
                found_filename = p["filename"]

        # 2. Check candidate filenames: {idx}.{ext}
        if not found:
            for ext in valid_extensions:
                candidate_name = f"{idx}.{ext}"
                candidate_path = os.path.join(output_path, candidate_name)
                if os.path.exists(candidate_path) and os.path.getsize(candidate_path) > 0:
                    found = True
                    found_filename = candidate_name
                    break

        if found:
            p["downloaded"] = True
            p["filename"] = found_filename
            downloaded_count += 1
        else:
            p["downloaded"] = False

    pending_count = len(photos) - downloaded_count
    return downloaded_count, pending_count


def find_existing_manifest(output_folder, album_url=None, album_title=None):
    """
    Check if a saved URLs manifest already exists on disk in the output folder.
    Matches by album_title folder name or by matching album_url / set ID in saved manifests.
    Returns (album_dir_path, manifest_path, manifest_data) or (None, None, None).
    """
    if not output_folder or not os.path.exists(output_folder):
        return None, None, None

    candidate_filenames = [DEFAULT_URLS_FILENAME, ALT_URLS_FILENAME]

    # 1. Check direct album_title folder variations if provided
    if album_title:
        clean_title = sanitize_filename(album_title)
        dir_candidates = [
            clean_title,
            clean_title.replace(' ', '_'),
            clean_title.replace(' ', '-'),
            clean_title.replace('_', ' ')
        ]
        for dir_name in dir_candidates:
            target_dir = os.path.join(output_folder, dir_name)
            if os.path.exists(target_dir):
                for fname in candidate_filenames:
                    mpath = os.path.join(target_dir, fname)
                    if os.path.exists(mpath):
                        data = load_saved_urls(mpath)
                        if data:
                            return target_dir, mpath, data

    # 2. Search all subdirectories in output_folder for matching album_url, set ID, or title in manifest
    set_id = None
    if album_url:
        set_match = re.search(r'set=([a-zA-Z0-9._]+)', album_url)
        set_id = set_match.group(1) if set_match else None

    norm_title = album_title.strip().lower() if album_title else None

    try:
        with os.scandir(output_folder) as entries:
            for entry in entries:
                if entry.is_dir():
                    for fname in candidate_filenames:
                        mpath = os.path.join(entry.path, fname)
                        if os.path.exists(mpath):
                            data = load_saved_urls(mpath)
                            if not data:
                                continue
                            saved_url = data.get("album_url", "")
                            saved_title = data.get("album_title", "").strip().lower()

                            # Match by URL or set ID
                            if album_url and saved_url and (saved_url == album_url or (set_id and set_id in saved_url)):
                                return entry.path, mpath, data

                            # Match by title stored in manifest
                            if norm_title and saved_title and (saved_title == norm_title or saved_title.replace(' ', '_') == norm_title.replace(' ', '_')):
                                return entry.path, mpath, data
    except Exception:
        pass

    return None, None, None


def sync_photos_with_manifest(photo_links, saved_photos):
    """
    Merge newly scraped Facebook photo links with previously saved photos from disk.
    Preserves existing direct URLs, downloaded status, and filenames for matching photos.
    Assigns sequential indices to new photos.
    Returns a unified list of photo dictionaries.
    """
    photos = []
    seen_ids = set()

    # Index saved photos by normalized ID
    saved_by_id = {}
    if saved_photos:
        for p in saved_photos:
            fb_url = p.get("facebook_url", "")
            if fb_url:
                nid = normalize_photo_id(fb_url)
                if nid:
                    saved_by_id[nid] = p

    current_index = 1

    # First, process scraped links in order
    for link in photo_links:
        nid = normalize_photo_id(link)
        if nid in seen_ids:
            continue
        seen_ids.add(nid)

        if nid in saved_by_id:
            # Re-use existing saved record
            existing = saved_by_id[nid]
            photos.append({
                "index": current_index,
                "facebook_url": link,
                "direct_url": existing.get("direct_url", ""),
                "filename": existing.get("filename", f"{current_index}.jpg"),
                "downloaded": bool(existing.get("downloaded", False)),
                "extracted_at": existing.get("extracted_at", "")
            })
        else:
            # New photo link
            photos.append({
                "index": current_index,
                "facebook_url": link,
                "direct_url": "",
                "filename": f"{current_index}.jpg",
                "downloaded": False,
                "extracted_at": ""
            })
        current_index += 1

    # Keep any previously saved photos that might not have appeared in current scroll
    if saved_photos:
        for p in saved_photos:
            fb_url = p.get("facebook_url", "")
            nid = normalize_photo_id(fb_url) if fb_url else None
            if nid and nid not in seen_ids:
                seen_ids.add(nid)
                photos.append({
                    "index": current_index,
                    "facebook_url": fb_url,
                    "direct_url": p.get("direct_url", ""),
                    "filename": p.get("filename", f"{current_index}.jpg"),
                    "downloaded": bool(p.get("downloaded", False)),
                    "extracted_at": p.get("extracted_at", "")
                })
                current_index += 1

    return photos


def download_single_image(args):
    """
    Download a single image with retry logic and disk detection.
    Args can be:
        (index, img_url, output_path, session)
        or (index, img_url, output_path, session, known_filename, resume)
    Returns:
        (index, success, message, filename, is_skipped)
    """
    if len(args) >= 6:
        index, img_url, output_path, session, known_filename, resume = args[:6]
    elif len(args) == 5:
        index, img_url, output_path, session, known_filename = args
        resume = True
    else:
        index, img_url, output_path, session = args
        known_filename = None
        resume = True

    # 1. Disk detection: check if image is already saved on disk
    if resume:
        if known_filename:
            target_path = os.path.join(output_path, known_filename)
            if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                return (index, True, f"Already downloaded: {known_filename} (skipped)", known_filename, True)

        for candidate_ext in ['jpg', 'jpeg', 'png', 'webp', 'gif']:
            candidate_name = f"{index + 1}.{candidate_ext}"
            candidate_path = os.path.join(output_path, candidate_name)
            if os.path.exists(candidate_path) and os.path.getsize(candidate_path) > 0:
                return (index, True, f"Already downloaded: {candidate_name} (skipped)", candidate_name, True)

    if not img_url:
        return (index, False, f"Failed to download image {index + 1}: No direct download URL available", None, False)

    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(img_url, stream=True, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                # Determine file extension from content-type or URL
                content_type = response.headers.get('content-type', '').lower()
                if 'jpeg' in content_type or 'jpg' in content_type:
                    ext = 'jpg'
                elif 'png' in content_type:
                    ext = 'png'
                elif 'gif' in content_type:
                    ext = 'gif'
                elif 'webp' in content_type:
                    ext = 'webp'
                elif known_filename and '.' in known_filename:
                    ext = known_filename.rsplit('.', 1)[-1]
                else:
                    ext = 'jpg'  # Default to jpg

                filename = known_filename if known_filename else f"{index + 1}.{ext}"
                file_path = os.path.join(output_path, filename)
                temp_file_path = f"{file_path}.tmp_{index}_{os.getpid()}"

                with open(temp_file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)

                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                os.replace(temp_file_path, file_path)

                return (index, True, f"Downloaded: {filename}", filename, False)
            elif response.status_code in [403, 404, 410]:
                return (index, False, f"Failed to download image {index + 1}: HTTP {response.status_code} (URL expired or inaccessible)", None, False)
            else:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1)
                    continue
                return (index, False, f"Failed to download image {index + 1}: HTTP {response.status_code}", None, False)

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(1)
                continue
            return (index, False, f"Failed to download image {index + 1}: {str(e)}", None, False)

    return (index, False, f"Failed to download image {index + 1} after {MAX_RETRIES} attempts", None, False)


def download_images_parallel(photo_items, output_path, cookies=None, driver=None,
                             manifest_path=None, album_url=None, album_title=None,
                             resume=True, browser_session=None):
    """
    Download all images in parallel using ThreadPoolExecutor.
    Supports resuming by detecting already downloaded files on disk.
    Accepts either a list of URL strings or a list of photo dictionaries.
    Refreshes expired direct download URLs via driver if available.
    """
    if not photo_items:
        print("No images to download.")
        return 0, 0

    # Normalize photo_items into structured records
    photos = []
    for i, item in enumerate(photo_items):
        if isinstance(item, dict):
            photos.append(dict(item))
        else:
            photos.append({
                "index": i + 1,
                "facebook_url": "",
                "direct_url": str(item),
                "filename": f"{i + 1}.jpg",
                "downloaded": False
            })

    total = len(photos)

    # Disk detection before downloading
    if resume:
        downloaded_on_disk, pending_count = detect_downloaded_images(photos, output_path)
        if downloaded_on_disk > 0:
            print(f"[+] Detected {downloaded_on_disk} already downloaded image(s) on disk. {pending_count} to download.")

    print(f"\nDownloading images to: {output_path}")
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
    download_args = []
    for i, p in enumerate(photos):
        idx = p.get("index", i + 1) - 1
        download_args.append((
            idx,
            p.get("direct_url", ""),
            output_path,
            session,
            p.get("filename", ""),
            resume
        ))

    successful = 0
    failed = 0
    skipped = 0
    expired_items = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_single_image, args): args[0]
            for args in download_args
        }

        for future in as_completed(futures):
            res = future.result()
            idx, success, message = res[0], res[1], res[2]
            filename = res[3] if len(res) > 3 else None
            is_skipped = res[4] if len(res) > 4 else False

            if success:
                successful += 1
                if is_skipped:
                    skipped += 1
                photos[idx]["downloaded"] = True
                if filename:
                    photos[idx]["filename"] = filename
                print(f"[{successful + failed}/{total}] {message}")
            else:
                # Check if failure was due to expired/inaccessible CDN URL
                active_browser = browser_session or driver
                if ("HTTP 403" in message or "HTTP 410" in message or "expired" in message.lower()) and photos[idx].get("facebook_url") and active_browser:
                    expired_items.append((idx, photos[idx]))
                else:
                    failed += 1
                    print(f"[{successful + failed}/{total}] {message}")

    # Handle expired URLs if driver or session is available
    active_browser = browser_session or driver
    if expired_items and active_browser:
        print(f"\n[!] Refreshing {len(expired_items)} expired direct URL(s) via browser...")
        for idx, p in expired_items:
            fb_url = p.get("facebook_url")
            print(f"  Refreshing URL for image {idx + 1} ({fb_url})...")
            fresh_url = extract_high_res_image(active_browser, fb_url, timeout=PAGE_LOAD_TIMEOUT)
            if fresh_url:
                p["direct_url"] = fresh_url
                print(f"  [+] Refreshed image {idx + 1}. Retrying download...")
                retry_res = download_single_image((
                    idx,
                    fresh_url,
                    output_path,
                    session,
                    p.get("filename", ""),
                    False
                ))
                if retry_res[1]:
                    successful += 1
                    p["downloaded"] = True
                    if retry_res[3]:
                        p["filename"] = retry_res[3]
                    print(f"  [+] Successfully downloaded refreshed image {idx + 1}")
                else:
                    failed += 1
                    print(f"  [-] Retry failed for image {idx + 1}: {retry_res[2]}")
            else:
                failed += 1
                print(f"  [-] Could not refresh direct URL for image {idx + 1}")

    session.close()

    # Persist updated manifest with download states
    if manifest_path:
        save_urls_manifest(manifest_path, album_url, album_title, photos)

    print("-" * 50)
    print(f"Download summary: {successful} completed ({skipped} already on disk, {successful - skipped} downloaded), {failed} failed")
    return successful, failed



def scroll_and_collect_photos(driver, max_scroll_attempts=DEFAULT_MAX_SCROLLS, scroll_pause_time=SCROLL_PAUSE_TIME):
    """
    Scroll down the album container and page, progressively accumulating photo links.
    Handles modern Facebook's internal scrollable containers, DOM recycling/virtualization,
    and provides real-time progress updates.
    """
    print("Scanning and loading all photos in album...")

    photo_links = []
    seen_links = set()

    def collect_from_dom():
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        new_count = 0
        for href in find_photo_links(soup):
            if href not in seen_links:
                seen_links.add(href)
                photo_links.append(href)
                new_count += 1
        return new_count

    # Collect initial links before scrolling
    initial_count = collect_from_dom()
    print(f"  Initially detected: {initial_count} photo(s)")

    no_change_count = 0
    scroll_attempts = 0

    while scroll_attempts < max_scroll_attempts:
        scroll_attempts += 1
        prev_total = len(photo_links)

        # 1. Scroll any internal scrollable containers (Facebook Comshell / React container divs)
        # 2. Also scroll window / documentElement
        driver.execute_script("""
            const all = document.querySelectorAll('*');
            for (let el of all) {
                const style = window.getComputedStyle(el);
                if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
                    el.scrollTop = el.scrollHeight;
                    el.dispatchEvent(new Event('scroll', { bubbles: true }));
                }
            }
            window.scrollTo(0, document.documentElement.scrollHeight || document.body.scrollHeight);
            window.dispatchEvent(new Event('scroll'));
        """)

        # 3. Send END key to body to trigger Facebook's keyboard-based infinite scrolling
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.END)
        except Exception:
            pass

        # 4. Wait for potential loading spinner / network activity
        time.sleep(scroll_pause_time)

        # Check if Facebook is showing a loading spinner
        try:
            loading_indicators = driver.find_elements(By.CSS_SELECTOR, "[role='progressbar'], [aria-busy='true']")
            if loading_indicators:
                time.sleep(1.0)
        except Exception:
            pass

        # 5. Collect newly loaded photo links
        new_items = collect_from_dom()

        if new_items > 0:
            print(f"  Loading photos... Found {len(photo_links)} photos so far...", end='\r')
            no_change_count = 0
        else:
            no_change_count += 1
            # Nudge scroll up slightly then down again to re-trigger intersection observers if stuck
            try:
                driver.execute_script("""
                    const all = document.querySelectorAll('*');
                    for (let el of all) {
                        const style = window.getComputedStyle(el);
                        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
                            el.scrollTop = Math.max(0, el.scrollTop - 200);
                        }
                    }
                """)
                time.sleep(0.5)
                driver.execute_script("""
                    const all = document.querySelectorAll('*');
                    for (let el of all) {
                        const style = window.getComputedStyle(el);
                        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
                            el.scrollTop = el.scrollHeight;
                            el.dispatchEvent(new Event('scroll', { bubbles: true }));
                        }
                    }
                """)
                time.sleep(scroll_pause_time)
                collect_from_dom()
            except Exception:
                pass

            if len(photo_links) > prev_total:
                print(f"  Loading photos... Found {len(photo_links)} photos so far...", end='\r')
                no_change_count = 0
            elif no_change_count >= 4:
                # No new photos after 4 consecutive attempts
                break

    print(f"\nScanning complete: Found {len(photo_links)} photo(s) across {scroll_attempts} scroll(s)")
    return photo_links


def scroll_to_load_all(driver, max_scroll_attempts=DEFAULT_MAX_SCROLLS):
    """
    Scroll down to load all photos in the album (legacy compatibility wrapper).
    """
    return scroll_and_collect_photos(driver, max_scroll_attempts=max_scroll_attempts)


def find_photo_links(soup):
    """
    Find photo links in the page using multiple strategies.
    This is more robust than relying on specific class names that change frequently.
    """
    photo_links = []

    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        # Strategy 1: Find links that point to photo pages (contain /photo/ or /photos/)
        # Strategy 2: Find links with photo thumbnail inside containing fbid=
        if '/photo/' in href or '/photos/' in href or (link.find('img') and 'fbid=' in href):
            if href.startswith('/'):
                href = f"https://www.facebook.com{href}"
            elif not href.startswith('http'):
                continue
            if href not in photo_links:
                photo_links.append(href)

    return photo_links


def is_avatar_url(url):
    """
    Check if a Facebook image URL is a profile picture or avatar thumbnail.
    """
    if not url:
        return True
    lower_url = url.lower()
    # Facebook CDN uses -1 for profile pictures (e.g., t39.30808-1, t1.6435-1, t1.15752-1)
    if '/t39.30808-1/' in lower_url or '/t1.6435-1/' in lower_url or '/t1.15752-1/' in lower_url or '-1/' in lower_url:
        return True
    # Common avatar thumbnail size parameters
    avatar_patterns = [
        's100x100', 'p100x100', 's50x50', 'p50x50', 's60x60', 'p60x60',
        's160x160', 'p160x160', 's200x200', 'p200x200', 'dst-jpg_s', 'dst-jpg_p'
    ]
    if any(pattern in lower_url for pattern in avatar_patterns):
        return True
    return False


def extract_high_res_image(driver_or_session, photo_url, timeout=PAGE_LOAD_TIMEOUT, retries=1):
    """
    Navigate to a photo page and extract the high-resolution image URL.
    Uses multiple strategies to find the real album image and avoid profile avatars.
    Handles navigation timeouts gracefully by stopping page load and inspecting
    the DOM that was already loaded.
    Detects fatal driver errors (like InactiveActor: Actor is no longer active) and
    automatically heals or restarts the browser session before retrying.
    """
    session = driver_or_session if isinstance(driver_or_session, BrowserSession) else None
    driver = session.get_driver() if session else driver_or_session

    for attempt in range(retries + 1):
        try:
            safe_get(driver, photo_url)

            # Wait for the photo viewer element to appear
            try:
                WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        "img[data-visualcompletion='media-vc-image'], div[role='dialog'] img, div[data-pagelet='MediaViewerRoot'] img, div[role='main'] img"
                    ))
                )
            except Exception:
                time.sleep(1.5)

            # Strategy 1: Check specifically for Facebook's media viewer image
            # Facebook tags the main full-screen photo with data-visualcompletion="media-vc-image"
            main_img = driver.find_elements(By.CSS_SELECTOR, "img[data-visualcompletion='media-vc-image']")
            if main_img:
                src = main_img[0].get_attribute("src")
                if src and "scontent" in src:
                    return src

            # Strategy 2: Use JavaScript to find the largest image by natural dimensions
            # Real album photos will have naturalWidth / naturalHeight > 300 (usually 1000-2048+),
            # whereas avatars and icons are 100x100, 50x50, 40x40, etc.
            js_find_largest = """
                const imgs = Array.from(document.querySelectorAll('img'))
                    .filter(img => {
                        if (!img.src || !img.src.includes('scontent')) return false;
                        const s = img.src.toLowerCase();
                        if (s.includes('-1/') || s.includes('_s100x100') || s.includes('_p100x100') ||
                            s.includes('dst-jpg_s') || s.includes('dst-jpg_p')) {
                            return false;
                        }
                        const w = img.naturalWidth || img.width || 0;
                        const h = img.naturalHeight || img.height || 0;
                        return w > 300 && h > 300;
                    });
                if (imgs.length > 0) {
                    imgs.sort((a, b) => {
                        const areaA = (a.naturalWidth || a.width || 0) * (a.naturalHeight || a.height || 0);
                        const areaB = (b.naturalWidth || b.width || 0) * (b.naturalHeight || b.height || 0);
                        return areaB - areaA;
                    });
                    return imgs[0].src;
                }
                return null;
            """
            largest_src = driver.execute_script(js_find_largest)
            if largest_src:
                return largest_src

            # Strategy 3: Check dialog / media viewer container
            dialog_imgs = driver.find_elements(By.CSS_SELECTOR, "div[role='dialog'] img, div[data-pagelet*='Media'] img, div[role='main'] img")
            for img in dialog_imgs:
                src = img.get_attribute("src")
                if src and "scontent" in src and not is_avatar_url(src):
                    return src

            # Strategy 4: Fallback BeautifulSoup parsing with strict avatar filtering
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            candidate_images = []
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if not src or 'scontent' not in src or 'emoji' in src.lower():
                    continue
                if is_avatar_url(src):
                    continue
                candidate_images.append(src)

            if candidate_images:
                non_avatar = [u for u in candidate_images if '-1/' not in u]
                if non_avatar:
                    return non_avatar[0]
                return candidate_images[0]

            if attempt < retries:
                time.sleep(1)
                continue

            return None

        except Exception as e:
            if is_fatal_driver_error(e):
                err_line = str(e).splitlines()[0] if str(e) else "Fatal driver error"
                if session:
                    print(f"\n[!] Browser session error ({err_line[:70]}). Initiating automatic recovery...")
                    driver = session.recover(e)
                    if attempt < retries:
                        time.sleep(1)
                        continue
                else:
                    if heal_driver(driver):
                        if attempt < retries:
                            time.sleep(1)
                            continue

            if attempt < retries:
                time.sleep(1)
                continue
            print(f"\n  Error extracting image from {photo_url}: {str(e)}")
            return None


def extract_album_title(soup, album_url=None):
    """
    Extract the album title from the page using multiple strategies.
    Avoids picking up navigation banners, notifications, or unrelated text.
    """
    ignored_keywords = [
        'log in', 'sign up', 'facebook', 'menu', 'notification', 'notifications',
        'unread', 'approved a login', 'create', 'search', 'messages', 'messenger',
        'home', 'friends', 'watch', 'marketplace', 'gaming', 'today', 'earlier', 'new'
    ]

    def is_valid_title(text):
        if not text or len(text) < 3 or len(text) > MAX_ALBUM_TITLE_LENGTH:
            return False
        if 'http' in text.lower():
            return False
        lower = text.lower()
        if any(k in lower for k in ignored_keywords):
            return False
        return True

    # Strategy 1: Check link matching the album set ID or album URL
    if album_url:
        set_match = re.search(r'set=([a-zA-Z0-9._]+)', album_url)
        if set_match:
            set_id = set_match.group(1)
            for a in soup.find_all('a', href=True):
                if set_id in a.get('href', ''):
                    text = a.get_text(strip=True)
                    if is_valid_title(text):
                        return text

    # Strategy 2: Look for heading elements (h1, h2, h3, [role="heading"])
    for elem in soup.find_all(['h1', 'h2', 'h3']):
        text = elem.get_text(strip=True)
        if is_valid_title(text):
            return text

    for elem in soup.find_all(attrs={'role': 'heading'}):
        text = elem.get_text(strip=True)
        if is_valid_title(text):
            return text

    # Strategy 3: Use page title if it is clean
    title_tag = soup.find('title')
    if title_tag:
        title = title_tag.get_text(strip=True)
        title = re.sub(r'^\(\d+\)\s*', '', title)
        title = re.sub(r'\s*[-|]\s*Facebook.*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'^Facebook.*$', '', title, flags=re.IGNORECASE).strip()
        if is_valid_title(title):
            return title

    # Strategy 4: Fallback to album ID if available
    if album_url:
        set_match = re.search(r'set=([a-zA-Z0-9._]+)', album_url)
        if set_match:
            return f"Album_{set_match.group(1)}"

    return "Facebook_Album"


def create_driver(headless=False, page_load_timeout=DEFAULT_PAGE_TIMEOUT):
    """
    Create and configure a Firefox WebDriver.
    Uses 'eager' page load strategy so navigation returns as soon as the DOM
    is ready (DOMContentLoaded) rather than blocking indefinitely for background
    media, tracking beacons, and long-polling connections to finish.
    Applies memory and performance optimizations to prevent InactiveActor and memory leaks.
    """
    options = Options()

    if headless:
        options.add_argument("--headless")

    # Additional options for stability
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.page_load_strategy = "eager"

    # Disable BFcache (fastback cache) so old photo pages aren't retained in RAM with dead actors
    options.set_preference("browser.sessionhistory.max_total_viewers", 0)
    options.set_preference("browser.sessionhistory.max_entries", 5)

    # Disable media / video autoplay to prevent background decoding during photo browsing
    options.set_preference("media.autoplay.default", 5)
    options.set_preference("media.autoplay.blocking_policy", 2)

    # Multi-process limit to control memory usage
    options.set_preference("dom.ipc.processCount", 4)

    try:
        driver = webdriver.Firefox(options=options)
        driver.set_page_load_timeout(page_load_timeout)
        return driver
    except Exception as e:
        print(f"Error creating Firefox driver: {e}")
        print("Make sure Firefox and geckodriver are installed.")
        sys.exit(1)


def validate_url(url):
    """
    Validate that the URL is a valid Facebook album URL or saved URLs JSON file.
    """
    if not url:
        return False, "No URL or file provided"

    # Check if input is an existing local manifest JSON file
    if os.path.isfile(url) or url.endswith('.json'):
        if os.path.exists(url):
            return True, None
        return False, f"File not found: {url}"

    parsed = urlparse(url)

    if 'facebook.com' not in parsed.netloc:
        return False, "URL must be a Facebook URL or saved URLs JSON file"

    # Check for album indicators
    if 'media/set' in url or 'set=' in url or '/album' in url or '/photos' in url:
        return True, None

    return True, "Warning: URL may not be an album page, but will try anyway"


def do_scraping(album_url=None, output_folder=DEFAULT_OUTPUT_FOLDER, headless=False,
                login=False, cookie_file=DEFAULT_COOKIE_FILE, save_cookies_flag=True,
                max_scrolls=DEFAULT_MAX_SCROLLS, scroll_delay=SCROLL_PAUSE_TIME,
                page_timeout=DEFAULT_PAGE_TIMEOUT, urls_file=None,
                resume=True, urls_only=False):
    """
    Main scraping function with authentication, resumable URL extraction, and disk detection.
    """
    resolved_cookie_file = resolve_cookie_path(cookie_file)
    has_cookie_file = resolved_cookie_file is not None and os.path.exists(resolved_cookie_file)

    # If urls_file was specified without album_url, use it directly
    if not album_url and urls_file:
        album_url = urls_file

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
                driver = create_driver(headless=False, page_load_timeout=page_timeout)
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
            print("Error: No album URL or URLs file provided.")
            return False

    # Validate URL or input file
    is_valid, message = validate_url(album_url)
    if not is_valid:
        print(f"Error: {message}")
        return False
    if message:
        print(message)

    # Direct resume from saved JSON manifest file without requiring browser
    if album_url and (os.path.isfile(album_url) or album_url.endswith('.json')):
        print(f"\nResuming album from saved URLs manifest: {album_url}")
        print("-" * 50)
        saved_manifest = load_saved_urls(album_url)
        if not saved_manifest or not saved_manifest.get("photos"):
            print(f"Error: Could not load photo URLs from {album_url}")
            return False

        album_title = saved_manifest.get("album_title") or "Facebook_Album"
        manifest_album_url = saved_manifest.get("album_url", "")
        manifest_dir = os.path.dirname(os.path.abspath(album_url))
        output_path = manifest_dir if (manifest_dir and os.path.basename(manifest_dir) != "") else create_folder(output_folder, album_title)

        photos = saved_manifest["photos"]
        print(f"Album: {album_title}")
        print(f"Output folder: {output_path}")
        print(f"Loaded {len(photos)} photo(s) from manifest.")

        if urls_only:
            print(f"URLs already saved in manifest ({len(photos)} photos). Exiting (--urls-only).")
            return True

        # Check if all images are already downloaded on disk
        if resume:
            downloaded_on_disk, pending = detect_downloaded_images(photos, output_path)
            if pending == 0 and downloaded_on_disk == len(photos):
                print(f"\n[+] All {len(photos)} images are already downloaded on disk in {output_path}!")
                return True

        # Check if any photos need direct URLs
        missing_direct = [p for p in photos if not p.get("direct_url")]
        browser_session = None
        driver = None
        if missing_direct:
            print(f"\n[!] {len(missing_direct)} photo(s) are missing direct URLs. Launching browser to extract...")
            browser_session = BrowserSession(
                headless=headless,
                page_timeout=page_timeout,
                cookie_file=resolved_cookie_file if (save_cookies_flag and has_cookie_file) else None,
                save_cookies_flag=save_cookies_flag
            )
            driver = browser_session.start()
            for p in missing_direct:
                if p.get("facebook_url"):
                    d_url = extract_high_res_image(browser_session, p["facebook_url"], timeout=PAGE_LOAD_TIMEOUT)
                    if d_url:
                        p["direct_url"] = d_url
                        save_urls_manifest(album_url, manifest_album_url, album_title, photos)
                browser_session.check_and_recycle()
            driver = browser_session.get_driver()

        try:
            cookies = driver.get_cookies() if driver else None
            successful, failed = download_images_parallel(
                photos,
                output_path,
                cookies=cookies,
                driver=driver,
                browser_session=browser_session,
                manifest_path=album_url,
                album_url=manifest_album_url,
                album_title=album_title,
                resume=resume
            )
            return successful > 0
        finally:
            if browser_session:
                print("Closing browser...")
                browser_session.quit()
            elif driver:
                print("Closing browser...")
                driver.quit()

    # Pre-scrape disk detection: check if existing manifest exists in output folder
    existing_dir, existing_manifest_path, existing_data = (None, None, None)
    if resume and not urls_file:
        existing_dir, existing_manifest_path, existing_data = find_existing_manifest(output_folder, album_url=album_url)
        if existing_dir and existing_data:
            print(f"[+] Detected existing album download on disk: {existing_dir}")
            print(f"    Loaded {len(existing_data.get('photos', []))} URLs from existing manifest.")

    print(f"\nStarting Facebook Album Downloader")
    print(f"Album URL: {album_url}")
    print(f"Output folder: {output_folder}")
    if has_cookie_file and save_cookies_flag:
        print(f"Authentication: Using saved cookies from {os.path.basename(resolved_cookie_file)}")
    elif login:
        print("Authentication: Interactive login enabled")
    print("-" * 50)

    browser_session = None
    driver = None
    try:
        # Create browser driver session
        print("Launching browser...")
        browser_session = BrowserSession(
            headless=headless,
            page_timeout=page_timeout,
            cookie_file=resolved_cookie_file if (save_cookies_flag and has_cookie_file) else None,
            save_cookies_flag=save_cookies_flag
        )
        driver = browser_session.start()

        # Check authentication status
        session_active = is_authenticated(driver)

        # If interactive login was explicitly requested
        if login:
            if session_active:
                print("[+] Already authenticated using saved cookies.")
            else:
                if headless:
                    print("Notice: Disabling headless mode for interactive authentication.")
                    browser_session.headless = False
                    driver = browser_session.restart("Switching to headed mode for authentication")

                if not wait_for_login(driver):
                    print("Authentication failed or was cancelled.")
                    return False
                if save_cookies_flag:
                    save_cookies(driver, cookie_file)
                session_active = True

        # Navigate to album
        print("Navigating to album page...")
        safe_get(driver, album_url)
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
            safe_get(driver, album_url)
            time.sleep(3)

        # Parse initial page and extract album title
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        album_title = extract_album_title(soup, album_url=album_url)
        album_title = sanitize_filename(album_title)
        print(f"Album title: {album_title}")

        # Resolve output path and manifest path early
        output_path = create_folder(output_folder, album_title)
        manifest_path = get_manifest_path(output_path, urls_file)

        # Check for saved URLs on disk
        saved_manifest = None
        if resume:
            saved_manifest = load_saved_urls(manifest_path)
            if not saved_manifest and existing_manifest_path:
                saved_manifest = existing_data
                manifest_path = existing_manifest_path
            if saved_manifest:
                print(f"[+] Using saved URLs manifest: {manifest_path} ({len(saved_manifest.get('photos', []))} photos loaded)")

        # Scroll to load all photos and collect links progressively
        photo_links = scroll_and_collect_photos(
            driver,
            max_scroll_attempts=max_scrolls,
            scroll_pause_time=scroll_delay
        )
        print(f"Total photo links detected: {len(photo_links)}")

        if not photo_links and not (saved_manifest and saved_manifest.get("photos")):
            print("No photos found. The page structure may have changed or the album may be private/inaccessible.")
            return False

        # Merge newly collected links with saved manifest records
        saved_photos = saved_manifest.get("photos", []) if (saved_manifest and resume) else []
        photos = sync_photos_with_manifest(photo_links, saved_photos)

        # Check existing direct URLs
        cached_count = sum(1 for p in photos if p.get("direct_url"))
        to_extract = [p for p in photos if not p.get("direct_url")]

        if cached_count > 0:
            print(f"\n[+] Detected {cached_count} cached image URL(s) on disk. {len(to_extract)} to extract.")

        # Extract high-resolution image URLs for any photos missing direct URLs
        if to_extract:
            print(f"\nExtracting high-resolution image URLs ({len(to_extract)} photos)...")
            extracted_new = 0
            for i, p in enumerate(to_extract):
                print(f"  Processing photo {i + 1}/{len(to_extract)} (total photo #{p['index']})...", end='\r')
                img_url = extract_high_res_image(browser_session, p["facebook_url"], timeout=PAGE_LOAD_TIMEOUT)
                if img_url:
                    p["direct_url"] = img_url
                    p["extracted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    extracted_new += 1
                    # Progressively save manifest to disk so progress is never lost
                    save_urls_manifest(manifest_path, album_url, album_title, photos)
                browser_session.check_and_recycle()
            print(f"\nExtracted {extracted_new} new image URLs ({cached_count + extracted_new} total available)")
        else:
            print("\n[+] All photo URLs already extracted and saved on disk. Skipping extraction!")

        # Save manifest after extraction phase
        save_urls_manifest(manifest_path, album_url, album_title, photos)

        available_urls = [p for p in photos if p.get("direct_url")]
        if not available_urls:
            print("No image URLs could be extracted or loaded from disk.")
            return False

        if urls_only:
            print(f"\n[+] URLs successfully saved to: {manifest_path} (--urls-only specified, skipping download)")
            return True

        driver = browser_session.get_driver()
        # Download images in parallel with authenticated session cookies and resume detection
        successful, failed = download_images_parallel(
            photos,
            output_path,
            cookies=driver.get_cookies() if driver else None,
            driver=driver,
            browser_session=browser_session,
            manifest_path=manifest_path,
            album_url=album_url,
            album_title=album_title,
            resume=resume
        )

        return successful > 0

    except KeyboardInterrupt:
        print("\nDownload cancelled by user.")
        if 'manifest_path' in locals() and 'photos' in locals() and photos:
            save_urls_manifest(manifest_path, album_url, album_title if 'album_title' in locals() else "", photos)
            print(f"Current progress saved to {manifest_path}")
        return False

    except Exception as e:
        print(f"\nError during scraping: {str(e)}")
        return False

    finally:
        if browser_session:
            print("\nClosing browser...")
            browser_session.quit()
        elif driver:
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
        help='URL of the Facebook album or path to a saved URLs JSON file'
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

    parser.add_argument(
        '--urls-file',
        default=None,
        help=f'File path to save/load extracted photo URLs (defaults to {DEFAULT_URLS_FILENAME} inside album folder)'
    )

    parser.add_argument(
        '--no-resume',
        action='store_true',
        help='Do not resume; re-extract all image URLs and re-download existing files'
    )

    parser.add_argument(
        '--urls-only',
        action='store_true',
        help='Only extract and save image URLs to disk without downloading image files'
    )

    parser.add_argument(
        '--max-scrolls',
        type=int,
        default=DEFAULT_MAX_SCROLLS,
        help=f'Maximum scroll attempts for loading photos (default: {DEFAULT_MAX_SCROLLS})'
    )

    parser.add_argument(
        '--scroll-delay',
        type=float,
        default=SCROLL_PAUSE_TIME,
        help=f'Delay in seconds between scroll actions (default: {SCROLL_PAUSE_TIME})'
    )

    parser.add_argument(
        '--page-timeout', '--timeout',
        type=int,
        default=DEFAULT_PAGE_TIMEOUT,
        dest='page_timeout',
        help=f'Page load timeout in seconds before aborting slow background requests (default: {DEFAULT_PAGE_TIMEOUT})'
    )

    args = parser.parse_args()

    # If no URL provided, prompt for it or handle login-only or urls-file
    if not args.album_url:
        if args.urls_file:
            args.album_url = args.urls_file
        elif args.login:
            print("Facebook Album Downloader - Authentication Mode")
            print("-" * 50)
            url_choice = input("Enter the Facebook album URL or saved URLs file (or press Enter to only log in and save session): ").strip()
            if url_choice:
                args.album_url = url_choice
        else:
            print("Facebook Album Downloader")
            print("-" * 30)
            args.album_url = input("Enter the Facebook album URL or saved URLs file: ").strip()

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
        save_cookies_flag=not args.no_cookies,
        max_scrolls=args.max_scrolls,
        scroll_delay=args.scroll_delay,
        page_timeout=args.page_timeout,
        urls_file=args.urls_file,
        resume=not args.no_resume,
        urls_only=args.urls_only
    )

    if success:
        if not args.album_url and args.login:
            print("\n[+] Authentication completed and session saved successfully!")
        elif args.urls_only:
            print("\n[+] URLs extraction completed successfully!")
        else:
            print("\n[+] Album download completed successfully!")
        sys.exit(0)
    else:
        if not args.album_url and args.login:
            print("\n[-] Authentication failed or incomplete")
        elif args.urls_only:
            print("\n[-] URLs extraction failed or incomplete")
        else:
            print("\n[-] Album download failed or incomplete")
        sys.exit(1)


if __name__ == "__main__":
    main()


