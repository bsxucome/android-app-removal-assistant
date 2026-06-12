import unittest
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app import (
    AdbClient,
    AppInfo,
    APP_VERSION,
    CleanerApp,
    DeviceDropdown,
    Device,
    PACKAGE_RE,
    is_valid_app_name,
    query_google_play_name,
    resolve_app_name_aapt2,
)


class FakeResponse:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class ParserTests(unittest.TestCase):
    def test_application_version(self):
        self.assertEqual(APP_VERSION, "1.0.4")

    def test_package_validation(self):
        self.assertTrue(PACKAGE_RE.fullmatch("com.example.app"))
        self.assertTrue(PACKAGE_RE.fullmatch("cn.test_app.mobile2"))
        self.assertFalse(PACKAGE_RE.fullmatch("not-a-package"))
        self.assertFalse(PACKAGE_RE.fullmatch("rm -rf"))

    def test_device_display(self):
        device = Device("ABC", "device", "Pixel")
        self.assertIn("ABC", device.display)
        self.assertIn("Pixel", device.display)

    def test_device_dropdown_matches_control_width(self):
        root = tk.Tk()
        root.geometry("800x240+10+10")
        variable = tk.StringVar()
        dropdown = DeviceDropdown(root, variable, lambda: None)
        dropdown.pack(fill="x", padx=20, pady=20)
        dropdown["values"] = ["DEVICE  [device]  MODEL"]
        dropdown.current(0)
        root.update()
        dropdown._open_popup()
        root.update()
        self.assertEqual(dropdown.winfo_rootx(), dropdown.popup.winfo_rootx())
        self.assertEqual(dropdown.winfo_width(), dropdown.popup.winfo_width())
        root.destroy()

    def test_rejects_invalid_app_names(self):
        self.assertFalse(is_valid_app_name("超出字元限制 %1$d 個字元 (上限 %2$d 個字元)"))
        self.assertFalse(is_valid_app_name("Entered %1$d of %2$d characters"))
        self.assertFalse(is_valid_app_name("com.example.app", "com.example.app"))
        self.assertTrue(is_valid_app_name("Facebook"))

    def test_package_list_parser(self):
        output = (
            "package:/data/app/one/base.apk=com.example.one\n"
            "package:/data/app/two/base.apk=cn.example.two\n"
            "noise\n"
        )
        client = AdbClient("ABC")
        with patch.object(client, "run", return_value=output):
            self.assertEqual(
                client.third_party_packages(),
                [
                    ("cn.example.two", "/data/app/two/base.apk"),
                    ("com.example.one", "/data/app/one/base.apk"),
                ],
            )

    def test_aapt2_prefers_windows_language_then_default(self):
        output = "\n".join(
            [
                "application-label:'Default name'",
                "application-label-en:'English name'",
                "application-label-zh-CN:'中文名称'",
                "application: label='Manifest name' icon=''",
            ]
        )
        result = SimpleNamespace(returncode=0, stdout=output, stderr="")
        with patch("app.subprocess.run", return_value=result):
            self.assertEqual(
                resolve_app_name_aapt2(Path("sample.apk"), "com.example.app", "zh_CN"),
                "中文名称",
            )
            self.assertEqual(
                resolve_app_name_aapt2(Path("sample.apk"), "com.example.app", "fr_FR"),
                "Default name",
            )

    def test_google_play_name_uses_json_ld_and_locale(self):
        html = (
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"SoftwareApplication","name":"Spotify"}'
            "</script>"
        )
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return FakeResponse(html)

        with patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(
                query_google_play_name("com.spotify.music", "zh_CN", "CN"),
                ("success", "Spotify"),
            )
        self.assertIn("hl=zh_CN", seen["url"])
        self.assertIn("gl=CN", seen["url"])
        self.assertEqual(seen["timeout"], 10)

    def test_google_play_handles_missing_404_and_network_error(self):
        with patch("app.urllib.request.urlopen", return_value=FakeResponse("<html></html>")):
            self.assertEqual(
                query_google_play_name("com.example.missing", "en_US", "US"),
                ("not_found", ""),
            )
        error_404 = HTTPError("https://example.invalid", 404, "Not found", None, None)
        with patch("app.urllib.request.urlopen", side_effect=error_404):
            self.assertEqual(
                query_google_play_name("com.example.missing", "en_US", "US"),
                ("not_found", ""),
            )
        with patch("app.urllib.request.urlopen", side_effect=URLError("offline")):
            self.assertEqual(
                query_google_play_name("com.example.missing", "en_US", "US"),
                ("error", ""),
            )

    def test_online_results_do_not_overwrite_local_name(self):
        fake_app = SimpleNamespace(
            apps=[
                AppInfo("本机名称", "com.example.local", ""),
                AppInfo("com.example.unknown", "com.example.unknown", ""),
            ]
        )
        counts = CleanerApp._apply_online_results(
            fake_app,
            [
                ("com.example.local", "success", "Store name"),
                ("com.example.unknown", "success", "Known name"),
                ("com.example.missing", "not_found", ""),
                ("com.example.error", "error", ""),
            ],
        )
        self.assertEqual(fake_app.apps[0].name, "本机名称")
        self.assertEqual(fake_app.apps[1].name, "Known name")
        self.assertEqual(counts, (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
