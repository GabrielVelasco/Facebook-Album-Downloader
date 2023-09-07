# Facebook Album Downloader

A Python script to download Facebook albums even if you're not the album's owner. Facebook's built-in download feature requires album ownership and sometimes fails on large albums. With this script, you can download any album.

## Usage

1. **Download the Repository:**
   - Click on the 'Code' button at the top right of this page, then select 'Download ZIP'.
   - Extract the downloaded ZIP file.

2. **Edit the Configuration:**
   - Open the 'albumDownloader.py' file with a text editor.

3. **Set the Album URL:**
   - Find the 'ALBUM_URL' variable at the beginning of the 'doScraping()' function.
   - Replace 'ALBUM_URL' with the URL of the Facebook album you want to download. (Example: `ALBUM_URL = https://www.facebook.com/media/set/?set=a.796525246431139&type=3`)

4. **Install Dependencies:**
   - You may need to install some dependencies, such as `selenium` and `bs4`. Just use:
     ```bash
     pip install selenium beautifulsoup4
     ```

5. **Run the Script:**
   - Execute the script in your terminal or command prompt using Python 3:
     ```bash
     python3 albumDownloader.py
     ```

**Note:** I've done this for educational purposes. Feel free to contribute or modify it to suit your needs.

**Note:** If you encounter issues or have questions, you can check the [Issues](https://github.com/your-repo/issues) section of this repository for help or open a new issue.

Happy album downloading :)!
