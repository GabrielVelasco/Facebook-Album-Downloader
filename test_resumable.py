import json
import os
import shutil
import tempfile
import unittest

from albumDownloader import (
    DEFAULT_URLS_FILENAME,
    detect_downloaded_images,
    download_single_image,
    find_existing_manifest,
    get_manifest_path,
    load_saved_urls,
    normalize_photo_id,
    save_urls_manifest,
    sync_photos_with_manifest,
    validate_url,
)


class TestResumableDownloader(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="fb_test_resumable_")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_normalize_photo_id(self):
        # Test fbid query parameter in various formats
        url1 = "https://www.facebook.com/photo/?fbid=796525246431139&set=a.123456789"
        url2 = "https://www.facebook.com/photo.php?fbid=796525246431139"
        url3 = "https://www.facebook.com/photo?fbid=796525246431139&type=3"
        self.assertEqual(normalize_photo_id(url1), "fbid_796525246431139")
        self.assertEqual(normalize_photo_id(url2), "fbid_796525246431139")
        self.assertEqual(normalize_photo_id(url3), "fbid_796525246431139")

        # Test photo path formats
        url4 = "https://www.facebook.com/username/photos/a.12345/9876543210/"
        self.assertEqual(normalize_photo_id(url4), "photo_9876543210")

        # Test pcb ID format
        url5 = "https://www.facebook.com/media/set/?set=pcb.11223344"
        self.assertEqual(normalize_photo_id(url5), "pcb_11223344")

        # Empty/None
        self.assertEqual(normalize_photo_id(""), "")
        self.assertEqual(normalize_photo_id(None), "")

    def test_save_and_load_manifest(self):
        manifest_file = os.path.join(self.test_dir, DEFAULT_URLS_FILENAME)
        album_url = "https://www.facebook.com/media/set/?set=a.123456789&type=3"
        album_title = "Vacation 2026"
        photos = [
            {
                "index": 1,
                "facebook_url": "https://www.facebook.com/photo/?fbid=111",
                "direct_url": "https://scontent.xx.fbcdn.net/v/photo1.jpg",
                "filename": "1.jpg",
                "downloaded": True
            },
            {
                "index": 2,
                "facebook_url": "https://www.facebook.com/photo/?fbid=222",
                "direct_url": "https://scontent.xx.fbcdn.net/v/photo2.jpg",
                "filename": "2.jpg",
                "downloaded": False
            }
        ]

        # Save manifest
        saved = save_urls_manifest(manifest_file, album_url, album_title, photos)
        self.assertTrue(saved)
        self.assertTrue(os.path.exists(manifest_file))

        # Load manifest
        loaded = load_saved_urls(manifest_file)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["album_url"], album_url)
        self.assertEqual(loaded["album_title"], album_title)
        self.assertEqual(len(loaded["photos"]), 2)
        self.assertEqual(loaded["photos"][0]["facebook_url"], "https://www.facebook.com/photo/?fbid=111")
        self.assertEqual(loaded["photos"][0]["direct_url"], "https://scontent.xx.fbcdn.net/v/photo1.jpg")
        self.assertTrue(loaded["photos"][0]["downloaded"])
        self.assertFalse(loaded["photos"][1]["downloaded"])

    def test_load_saved_urls_formats(self):
        # 1. Test key-value mapping format: {fb_url: direct_url}
        kv_file = os.path.join(self.test_dir, "kv.json")
        with open(kv_file, "w", encoding="utf-8") as f:
            json.dump({
                "https://facebook.com/photo/?fbid=101": "https://scontent.fbcdn.net/img1.jpg",
                "https://facebook.com/photo/?fbid=102": "https://scontent.fbcdn.net/img2.jpg"
            }, f)

        loaded_kv = load_saved_urls(kv_file)
        self.assertIsNotNone(loaded_kv)
        self.assertEqual(len(loaded_kv["photos"]), 2)
        self.assertEqual(loaded_kv["photos"][0]["facebook_url"], "https://facebook.com/photo/?fbid=101")
        self.assertEqual(loaded_kv["photos"][0]["direct_url"], "https://scontent.fbcdn.net/img1.jpg")

        # 2. Test list of dicts format
        list_file = os.path.join(self.test_dir, "list.json")
        with open(list_file, "w", encoding="utf-8") as f:
            json.dump([
                {"facebook_url": "https://facebook.com/photo/?fbid=201", "direct_url": "https://scontent.fbcdn.net/img201.jpg"},
                {"facebook_url": "https://facebook.com/photo/?fbid=202", "direct_url": "https://scontent.fbcdn.net/img202.jpg"}
            ], f)

        loaded_list = load_saved_urls(list_file)
        self.assertIsNotNone(loaded_list)
        self.assertEqual(len(loaded_list["photos"]), 2)
        self.assertEqual(loaded_list["photos"][0]["direct_url"], "https://scontent.fbcdn.net/img201.jpg")

        # 3. Non-existent file
        self.assertIsNone(load_saved_urls(os.path.join(self.test_dir, "non_existent.json")))

    def test_detect_downloaded_images(self):
        output_folder = os.path.join(self.test_dir, "MyAlbum")
        os.makedirs(output_folder)

        # Create 1.jpg with content
        with open(os.path.join(output_folder, "1.jpg"), "wb") as f:
            f.write(b"fake image data 1")

        # Create 2.png with content
        with open(os.path.join(output_folder, "2.png"), "wb") as f:
            f.write(b"fake image data 2")

        # Create 3.jpg as empty 0-byte file (interrupted download)
        with open(os.path.join(output_folder, "3.jpg"), "wb") as f:
            pass

        photos = [
            {"index": 1, "filename": "1.jpg", "downloaded": False},
            {"index": 2, "filename": "", "downloaded": False},       # Will detect 2.png
            {"index": 3, "filename": "3.jpg", "downloaded": True},   # 0 bytes -> must become False!
            {"index": 4, "filename": "4.jpg", "downloaded": False},  # Does not exist -> False
        ]

        downloaded_count, pending_count = detect_downloaded_images(photos, output_folder)
        self.assertEqual(downloaded_count, 2)
        self.assertEqual(pending_count, 2)
        self.assertTrue(photos[0]["downloaded"])
        self.assertTrue(photos[1]["downloaded"])
        self.assertEqual(photos[1]["filename"], "2.png")
        self.assertFalse(photos[2]["downloaded"])  # 0-byte file detected as incomplete
        self.assertFalse(photos[3]["downloaded"])

    def test_sync_photos_with_manifest(self):
        saved_photos = [
            {
                "index": 1,
                "facebook_url": "https://www.facebook.com/photo/?fbid=100&set=a.1",
                "direct_url": "https://scontent.xx/img100.jpg",
                "filename": "1.jpg",
                "downloaded": True,
                "extracted_at": "2026-01-01 12:00:00"
            },
            {
                "index": 2,
                "facebook_url": "https://www.facebook.com/photo/?fbid=200&set=a.1",
                "direct_url": "https://scontent.xx/img200.jpg",
                "filename": "2.jpg",
                "downloaded": False,
                "extracted_at": "2026-01-01 12:01:00"
            }
        ]

        # Newly scraped links: contains photo 200 (existing) and photo 300 (new)
        scraped_links = [
            "https://www.facebook.com/photo.php?fbid=200",  # Same fbid, different query params
            "https://www.facebook.com/photo/?fbid=300&set=a.1"
        ]

        synced = sync_photos_with_manifest(scraped_links, saved_photos)

        # Should match photo 200 with its saved direct URL, add photo 300 as pending,
        # and preserve photo 100 since it was previously saved
        self.assertEqual(len(synced), 3)

        # First item should be photo 200 with restored direct_url
        self.assertEqual(synced[0]["direct_url"], "https://scontent.xx/img200.jpg")
        self.assertEqual(normalize_photo_id(synced[0]["facebook_url"]), "fbid_200")

        # Second item should be photo 300 with empty direct_url
        self.assertEqual(synced[1]["direct_url"], "")
        self.assertFalse(synced[1]["downloaded"])

        # Third item should be preserved photo 100
        self.assertEqual(synced[2]["direct_url"], "https://scontent.xx/img100.jpg")
        self.assertTrue(synced[2]["downloaded"])

    def test_find_existing_manifest(self):
        output_folder = os.path.join(self.test_dir, "downloads")
        album_dir = os.path.join(output_folder, "Trip_Photos")
        os.makedirs(album_dir)

        manifest_path = os.path.join(album_dir, DEFAULT_URLS_FILENAME)
        save_urls_manifest(
            manifest_path,
            "https://www.facebook.com/media/set/?set=a.998877&type=3",
            "Trip Photos",
            [{"index": 1, "facebook_url": "https://fb.com/1", "direct_url": "https://cdn.com/1.jpg"}]
        )

        # 1. Find by matching album_url
        found_dir, found_manifest, found_data = find_existing_manifest(
            output_folder,
            album_url="https://www.facebook.com/media/set/?set=a.998877&type=3"
        )
        self.assertEqual(found_dir, album_dir)
        self.assertEqual(found_manifest, manifest_path)
        self.assertIsNotNone(found_data)

        # 2. Find by set ID even if URL parameters differ
        found_dir2, _, _ = find_existing_manifest(
            output_folder,
            album_url="https://www.facebook.com/media/set/?set=a.998877"
        )
        self.assertEqual(found_dir2, album_dir)

        # 3. Find by album title
        found_dir3, _, _ = find_existing_manifest(
            output_folder,
            album_title="Trip Photos"
        )
        self.assertEqual(found_dir3, album_dir)

    def test_download_single_image_resume_skip(self):
        output_folder = os.path.join(self.test_dir, "album_imgs")
        os.makedirs(output_folder)

        # Pre-create 1.jpg
        existing_img = os.path.join(output_folder, "1.jpg")
        with open(existing_img, "wb") as f:
            f.write(b"pre-downloaded content")

        # When resume=True, download_single_image should skip without making HTTP requests
        res = download_single_image((0, "https://invalid-non-existent-url.local/image.jpg", output_folder, None, "1.jpg", True))
        idx, success, message, filename, is_skipped = res

        self.assertEqual(idx, 0)
        self.assertTrue(success)
        self.assertTrue(is_skipped)
        self.assertEqual(filename, "1.jpg")
        self.assertIn("skipped", message)

    def test_validate_url_with_manifest_file(self):
        # Create a valid JSON file
        valid_json = os.path.join(self.test_dir, "saved_manifest.json")
        with open(valid_json, "w", encoding="utf-8") as f:
            json.dump({"photos": []}, f)

        # Test valid JSON file
        is_valid, err = validate_url(valid_json)
        self.assertTrue(is_valid)
        self.assertIsNone(err)

        # Test non-existent JSON file
        non_existent_json = os.path.join(self.test_dir, "missing.json")
        is_valid, err = validate_url(non_existent_json)
        self.assertFalse(is_valid)
        self.assertIn("File not found", err)

        # Test Facebook URL
        is_valid, _ = validate_url("https://www.facebook.com/media/set/?set=a.123456")
        self.assertTrue(is_valid)

    def test_download_images_parallel_resume(self):
        from albumDownloader import download_images_parallel

        album_dir = os.path.join(self.test_dir, "ParallelAlbum")
        os.makedirs(album_dir)

        # Pre-create 1.jpg
        with open(os.path.join(album_dir, "1.jpg"), "wb") as f:
            f.write(b"already downloaded image 1")

        manifest_file = os.path.join(album_dir, DEFAULT_URLS_FILENAME)
        photos = [
            {"index": 1, "facebook_url": "https://fb.com/1", "direct_url": "https://invalid.test/1.jpg", "filename": "1.jpg", "downloaded": False},
        ]

        save_urls_manifest(manifest_file, "https://fb.com/album", "ParallelAlbum", photos)

        # Download with resume=True should skip 1.jpg and report success
        successful, failed = download_images_parallel(
            photos,
            album_dir,
            manifest_path=manifest_file,
            album_url="https://fb.com/album",
            album_title="ParallelAlbum",
            resume=True
        )

        self.assertEqual(successful, 1)
        self.assertEqual(failed, 0)

        # Manifest should be updated with downloaded=True
        updated_manifest = load_saved_urls(manifest_file)
        self.assertTrue(updated_manifest["photos"][0]["downloaded"])

    def test_do_scraping_from_saved_manifest(self):
        from albumDownloader import do_scraping

        album_dir = os.path.join(self.test_dir, "ScrapeTestAlbum")
        os.makedirs(album_dir)

        manifest_file = os.path.join(album_dir, DEFAULT_URLS_FILENAME)
        photos = [
            {"index": 1, "facebook_url": "https://fb.com/1", "direct_url": "https://invalid.test/1.jpg", "filename": "1.jpg", "downloaded": True}
        ]

        # Pre-create 1.jpg so it detects as fully downloaded
        with open(os.path.join(album_dir, "1.jpg"), "wb") as f:
            f.write(b"test image")

        save_urls_manifest(manifest_file, "https://fb.com/album", "ScrapeTestAlbum", photos)

        # Calling do_scraping directly on manifest file should detect all are downloaded and succeed without launching browser
        success = do_scraping(album_url=manifest_file, resume=True)
        self.assertTrue(success)

        # Calling with urls_only should also succeed immediately
        success_urls_only = do_scraping(album_url=manifest_file, urls_only=True)
        self.assertTrue(success_urls_only)


if __name__ == "__main__":
    unittest.main()

