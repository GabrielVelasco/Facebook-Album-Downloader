# Facebook Album Downloader

A Python script to download Facebook albums even if you're not the album's owner. Facebook's built-in download feature requires album ownership and sometimes fails on large albums. With this script, you can download any public album.

## Features

- **Interactive authentication** - Log in via the browser to access private or restricted albums
- **Session persistence** - Saves cookies to disk for seamless reuse across downloads without re-logging in
- **Resumable downloads & URL caching** - Saves extracted image URLs (both Facebook photo URL and direct download URL) to disk and detects already saved files on disk
- **Progressive saving** - Automatically persists URLs to disk after each photo is extracted, preventing lost progress on interruption (Ctrl+C, network timeout)
- **Direct manifest resumption** - Resume downloads directly from a saved `album_urls.json` manifest file without reopening Facebook
- **Automatic token refresh** - Automatically detects expired Facebook CDN tokens (HTTP 403) and refreshes direct URLs via the browser
- **Command-line interface** - Pass the album URL or saved manifest directly as an argument
- **Cross-platform support** - Works on Windows, macOS, and Linux
- **Parallel downloads** - Downloads multiple images simultaneously for faster completion
- **Automatic retry** - Retries failed downloads up to 3 times
- **Progress tracking** - Shows real-time download progress
- **Headless mode** - Optional mode to run without visible browser window
- **Robust scraping** - Uses multiple strategies to find images, more resilient to Facebook UI changes
- **Self-healing session & auto-recovery** - Automatically recovers from browser desynchronization or crashes (`InactiveActor`), reloads cookies, and retries without failing the album
- **Proactive memory management** - Automatically recycles the browser session during bulk extractions to prevent memory leaks on large albums
- **Smart file naming** - Automatically detects image format (jpg, png, gif, webp)

## Prerequisites

- Python 3.7 or higher
- Firefox browser installed
- geckodriver (Firefox WebDriver) installed and in your PATH

## Installation

1. **Clone or download the repository:**
   ```bash
   git clone https://github.com/GabrielVelasco/Facebook-Album-Downloader.git
   cd Facebook-Album-Downloader
   ```

2. **Install Python dependencies:**
   ```bash
   pip install selenium beautifulsoup4 requests
   ```

3. **Install geckodriver:**
   - **macOS:** `brew install geckodriver`
   - **Linux:** Download from [GitHub releases](https://github.com/mozilla/geckodriver/releases) and add to PATH
   - **Windows:** Download from [GitHub releases](https://github.com/mozilla/geckodriver/releases) and add to PATH

## Usage

### Basic Usage

```bash
python albumDownloader.py <album_url>
```

**Example:**
```bash
python albumDownloader.py "https://www.facebook.com/media/set/?set=a.796525246431139&type=3"
```

### Resuming an Interrupted Download

The script is automatically **resumable**. Extracted photo URLs (both the Facebook URL and the direct CDN download URL) are progressively saved to `album_urls.json` in the album's folder.

If a download is interrupted or stopped:
1. **Simply rerun the same command:**
   ```bash
   python albumDownloader.py "https://www.facebook.com/media/set/?set=a.796525246431139&type=3"
   ```
   The script detects `album_urls.json` and already downloaded images on disk:
   - Skips re-navigating to photo pages for already extracted URLs
   - Skips re-downloading files that are already present on disk with valid size
   - Only processes remaining photos and missing files

2. **Or resume directly from the saved URLs file:**
   ```bash
   python albumDownloader.py downloadedImgs/My_Album/album_urls.json
   ```
   This resumes the download directly using the saved URLs without needing to re-open or re-scroll Facebook.

### Extracting URLs Only (No Download)

To extract and save all photo URLs without downloading the image files:
```bash
python albumDownloader.py <album_url> --urls-only
```

This generates `album_urls.json` containing:
```json
{
  "album_url": "https://www.facebook.com/media/set/?set=...",
  "album_title": "My Album",
  "total_photos": 45,
  "extracted_count": 45,
  "downloaded_count": 45,
  "photos": [
    {
      "index": 1,
      "facebook_url": "https://www.facebook.com/photo/?fbid=...",
      "direct_url": "https://scontent-iad3-1.xx.fbcdn.net/v/...",
      "filename": "1.jpg",
      "downloaded": true
    }
  ]
}
```

### With Custom Output Folder

```bash
python albumDownloader.py <album_url> --output <folder_name>
```

**Example:**
```bash
python albumDownloader.py "https://www.facebook.com/media/set/?set=a.123456789" --output my_downloads
```

### Authentication & Private Albums

To download private or restricted albums that require you to be logged into Facebook, use the `--login` (or `--auth`) flag:

```bash
python albumDownloader.py <album_url> --login
```

This will:
1. Open the Facebook login page in a visible Firefox window.
2. Allow you to enter your credentials, complete 2FA, and solve any security prompts.
3. Automatically detect when you are logged in (or you can press `Enter` in the console).
4. Save your session cookies to `facebook_cookies.json` for subsequent downloads.

You can also authenticate ahead of time without downloading an album:
```bash
python albumDownloader.py --login
```

Subsequent runs will automatically restore your saved session from `facebook_cookies.json`, allowing you to run even in `--headless` mode.

### Headless Mode (No Visible Browser)

```bash
python albumDownloader.py <album_url> --headless
```

> **Note:** If an album requires login and the session has expired, running in `--headless` mode will prompt you to re-authenticate with `--login`.

### Interactive Mode

Simply run without arguments to enter the URL interactively:
```bash
python albumDownloader.py
```

## Command-Line Options

| Option | Description |
|--------|-------------|
| `album_url` | URL of the Facebook album or path to a saved `album_urls.json` file |
| `-o, --output` | Output folder for downloaded images (default: `downloadedImgs`) |
| `--urls-file` | Custom file path to save/load extracted photo URLs (default: `<album_folder>/album_urls.json`) |
| `--no-resume` | Disable resumption; force re-extracting all URLs and re-downloading files |
| `--urls-only` | Extract and save image URLs to disk without downloading images |
| `--headless` | Run browser in headless mode (no visible window) |
| `--login`, `--auth` | Open Facebook login page in browser and wait for authentication |
| `--cookies` | Path to save/load Facebook session cookies (default: `facebook_cookies.json`) |
| `--no-cookies` | Disable loading or saving session cookies to disk |
| `--max-scrolls` | Maximum scroll attempts for loading photos (default: `300`) |
| `--scroll-delay` | Delay in seconds between scroll actions (default: `2.0`) |
| `--page-timeout`, `--timeout` | Page load timeout in seconds before proceeding (default: `30`) |

## Limitations

- **Account Access** - To download private albums, your authenticated account must have permission to view the album (e.g. friends-only or shared albums).
- **Facebook UI changes** - Facebook frequently changes their website structure. The script uses multiple strategies to find images, but may need updates if Facebook makes major changes.
- **Rate limiting** - Downloading too many albums in quick succession may trigger Facebook's rate limiting.

## Troubleshooting

**"Error creating Firefox driver"**
- Make sure Firefox is installed
- Make sure geckodriver is installed and in your PATH

**"This album requires authentication to view" / "No photos found"**
- The album may be private or friends-only. Run with `--login` to log into your Facebook account inside the browser.
- If running in `--headless` mode, run without `--headless` and with `--login` to re-authenticate.

**Downloads are slow**
- Consider running with `--headless` mode for slightly faster operation
- Check your internet connection

## License

This project is for educational purposes (web scraping practice). Feel free to contribute or modify it to suit your needs.

Happy album downloading! 📸
