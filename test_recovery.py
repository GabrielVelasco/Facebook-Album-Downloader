import unittest
from unittest.mock import MagicMock, patch
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from albumDownloader import (
    BrowserSession,
    extract_high_res_image,
    heal_driver,
    is_fatal_driver_error,
    RECYCLE_BATCH_SIZE,
)


class TestRecoveryAndSession(unittest.TestCase):

    def test_is_fatal_driver_error_inactive_actor(self):
        e1 = WebDriverException(
            "Message: InactiveActor: Actor is no longer active\n"
            "Stacktrace:\n"
            "receiveMessage@chrome://remote/content/marionette/actors/MarionetteCommandsChild.sys.mjs:165:13"
        )
        self.assertTrue(is_fatal_driver_error(e1))

        e2 = WebDriverException("InactiveActor: Content actor is not active")
        self.assertTrue(is_fatal_driver_error(e2))

        e3 = WebDriverException("Actor is no longer active")
        self.assertTrue(is_fatal_driver_error(e3))

    def test_is_fatal_driver_error_window_and_session(self):
        self.assertTrue(is_fatal_driver_error(NoSuchWindowException("Window closed")))
        self.assertTrue(is_fatal_driver_error(InvalidSessionIdException("Session expired")))
        self.assertTrue(is_fatal_driver_error(WebDriverException("Failed to decode response from marionette")))
        self.assertTrue(is_fatal_driver_error(WebDriverException("Connection refused by geckodriver")))

    def test_is_non_fatal_driver_error(self):
        self.assertFalse(is_fatal_driver_error(TimeoutException("Timed out waiting for element")))
        self.assertFalse(is_fatal_driver_error(ValueError("Invalid argument")))
        self.assertTrue(is_fatal_driver_error(None) == False)
        self.assertFalse(is_fatal_driver_error(Exception("Some other error")))

    def test_heal_driver_success(self):
        mock_driver = MagicMock()
        mock_driver.window_handles = ["handle1"]
        mock_driver.current_window_handle = "handle1"
        mock_driver.execute_script.return_value = 1

        self.assertTrue(heal_driver(mock_driver))
        mock_driver.switch_to.default_content.assert_called_once()
        mock_driver.execute_script.assert_called_once_with("return 1;")

    def test_heal_driver_failure(self):
        mock_driver = MagicMock()
        mock_driver.switch_to.default_content.side_effect = WebDriverException("InactiveActor")

        self.assertFalse(heal_driver(mock_driver))

    def test_heal_driver_none(self):
        self.assertFalse(heal_driver(None))

    @patch("albumDownloader.create_driver")
    @patch("albumDownloader.load_cookies")
    def test_browser_session_lifecycle(self, mock_load_cookies, mock_create_driver):
        mock_driver1 = MagicMock()
        mock_driver2 = MagicMock()
        mock_create_driver.side_effect = [mock_driver1, mock_driver2]

        session = BrowserSession(headless=True, recycle_interval=3, cookie_file=None)
        self.assertIsNone(session.driver)

        # Start session
        driver = session.start()
        self.assertEqual(driver, mock_driver1)
        self.assertEqual(session.photos_since_restart, 0)

        # Recycling counter check before interval
        session.check_and_recycle()
        self.assertEqual(session.photos_since_restart, 1)
        self.assertEqual(session.driver, mock_driver1)

        session.check_and_recycle()
        self.assertEqual(session.photos_since_restart, 2)
        self.assertEqual(session.driver, mock_driver1)

        # Hit recycle interval (3 photos)
        driver_after_recycle = session.check_and_recycle()
        self.assertEqual(driver_after_recycle, mock_driver2)
        self.assertEqual(session.photos_since_restart, 0)
        mock_driver1.quit.assert_called_once()

        # Quit
        session.quit()
        mock_driver2.quit.assert_called_once()
        self.assertIsNone(session.driver)

    @patch("albumDownloader.create_driver")
    def test_browser_session_recover(self, mock_create_driver):
        broken_driver = MagicMock()
        broken_driver.switch_to.default_content.side_effect = WebDriverException("InactiveActor")

        fresh_driver = MagicMock()
        mock_create_driver.side_effect = [broken_driver, fresh_driver]

        session = BrowserSession(headless=True, cookie_file=None)
        session.start()

        recovered_driver = session.recover(WebDriverException("InactiveActor: Actor is no longer active"))
        self.assertEqual(recovered_driver, fresh_driver)
        broken_driver.quit.assert_called_once()

    @patch("albumDownloader.safe_get")
    def test_extract_high_res_image_recovery_on_inactive_actor(self, mock_safe_get):
        broken_driver = MagicMock()
        fresh_driver = MagicMock()

        img_element = MagicMock()
        img_element.get_attribute.return_value = "https://scontent.xx.fbcdn.net/v/photo_highres.jpg"
        fresh_driver.find_elements.return_value = [img_element]

        session = MagicMock(spec=BrowserSession)
        session.get_driver.return_value = broken_driver
        session.recover.return_value = fresh_driver

        def fake_safe_get(drv, url):
            if drv == broken_driver:
                raise WebDriverException("Message: InactiveActor: Actor is no longer active")

        mock_safe_get.side_effect = fake_safe_get

        result_url = extract_high_res_image(session, "https://facebook.com/photo/?fbid=123", timeout=1, retries=1)

        self.assertEqual(result_url, "https://scontent.xx.fbcdn.net/v/photo_highres.jpg")
        session.recover.assert_called_once()


if __name__ == '__main__':
    unittest.main()
