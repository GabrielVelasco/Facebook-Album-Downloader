# Facebook Album Downloader

A Python script to download Facebook albums even if you're not the album's owner. Facebook's built-in download feature requires album ownership and sometimes fails on large albums. With this script, you can download any public album.

## Features

- **Command-line interface** - Pass the album URL directly as an argument
- **Cross-platform support** - Works on Windows, macOS, and Linux
- **Parallel downloads** - Downloads multiple images simultaneously for faster completion
- **Automatic retry** - Retries failed downloads up to 3 times
- **Progress tracking** - Shows real-time download progress
- **Headless mode** - Optional mode to run without visible browser window
- **Robust scraping** - Uses multiple strategies to find images, more resilient to Facebook UI changes
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

### With Custom Output Folder

```bash
python albumDownloader.py <album_url> --output <folder_name>
```

**Example:**
```bash
python albumDownloader.py "https://www.facebook.com/media/set/?set=a.123456789" --output my_downloads
```

### Headless Mode (No Visible Browser)

```bash
python albumDownloader.py <album_url> --headless
```

### Interactive Mode

Simply run without arguments to enter the URL interactively:
```bash
python albumDownloader.py
```

## Command-Line Options

| Option | Description |
|--------|-------------|
| `album_url` | URL of the Facebook album to download |
| `-o, --output` | Output folder for downloaded images (default: `downloadedImgs`) |
| `--headless` | Run browser in headless mode (no visible window) |

## Limitations

- **Public albums only** - The script can only download public albums. Private albums require authentication which is not supported.
- **Facebook UI changes** - Facebook frequently changes their website structure. The script uses multiple strategies to find images, but may need updates if Facebook makes major changes.
- **Rate limiting** - Downloading too many albums in quick succession may trigger Facebook's rate limiting.

## Troubleshooting

**"Error creating Firefox driver"**
- Make sure Firefox is installed
- Make sure geckodriver is installed and in your PATH

**"No photos found"**
- The album may be private or require login
- Facebook's page structure may have changed
- Try running without `--headless` to see if there are any login prompts

**Downloads are slow**
- Consider running with `--headless` mode for slightly faster operation
- Check your internet connection

## License

This project is for educational purposes (web scraping practice). Feel free to contribute or modify it to suit your needs.

Happy album downloading! 📸
