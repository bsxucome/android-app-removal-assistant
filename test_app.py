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
    LookupResult,
    PACKAGE_RE,
    classify_network_error,
    is_valid_app_name,
    query_fdroid_name,
    query_google_play_name,
    query_online_name,
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


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return FakeResponse(response)


class ParserTests(unittest.TestCase):
    def test_application_version(self):
        self.assertEqual(APP_VERSION, "1.2.0")

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
        opener = FakeOpener([html])
        result = query_google_play_name("com.spotify.music", "zh_CN", "CN", opener=opener)
        self.assertEqual(result, LookupResult("success", "Spotify", "Google Play"))
        self.assertIn("hl=zh_CN", opener.requests[0][0].full_url)
        self.assertIn("gl=CN", opener.requests[0][0].full_url)
        self.assertEqual(opener.requests[0][1], 10)

    def test_google_play_handles_missing_404_and_network_error(self):
        result = query_google_play_name(
            "com.example.missing", "en_US", "US", opener=FakeOpener(["<html></html>"])
        )
        self.assertEqual(result.status, "parse_error")
        error_404 = HTTPError("https://example.invalid", 404, "Not found", None, None)
        result = query_google_play_name(
            "com.example.missing", "en_US", "US", opener=FakeOpener([error_404])
        )
        self.assertEqual(result.status, "not_found")
        result = query_google_play_name(
            "com.example.missing", "en_US", "US", opener=FakeOpener([URLError("offline")])
        )
        self.assertEqual(result.status, "connection_error")

    def test_fdroid_name_and_online_fallback(self):
        fdroid_html = (
            '<meta property="og:title" content="F-Droid | F-Droid">'
        )
        result = query_fdroid_name(
            "org.fdroid.fdroid", "en_US", opener=FakeOpener([fdroid_html])
        )
        self.assertEqual(result, LookupResult("success", "F-Droid", "F-Droid"))

        play_404 = HTTPError("https://example.invalid", 404, "Not found", None, None)
        opener = FakeOpener([play_404, fdroid_html])
        with patch("app.network_opener", return_value=opener):
            result, attempts = query_online_name("org.fdroid.fdroid", "en_US", "US")
        self.assertEqual(result.source, "F-Droid")
        self.assertEqual([item.status for item in attempts], ["not_found", "success"])

    def test_network_error_classification(self):
        self.assertEqual(classify_network_error(URLError(TimeoutError())), "timeout")
        self.assertEqual(classify_network_error(URLError("proxy tunnel failed")), "proxy_error")
        self.assertEqual(classify_network_error(URLError("offline")), "connection_error")

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
                (
                    "com.example.local",
                    LookupResult("success", "Store name", "Google Play"),
                    [],
                ),
                (
                    "com.example.unknown",
                    LookupResult("success", "Known name", "F-Droid"),
                    [],
                ),
                ("com.example.missing", LookupResult("not_found"), []),
                (
                    "com.example.error",
                    LookupResult("failed"),
                    [LookupResult("timeout", source="Google Play")],
                ),
            ],
        )
        self.assertEqual(fake_app.apps[0].name, "本机名称")
        self.assertEqual(fake_app.apps[1].name, "Known name")
        self.assertEqual(counts["success"], 1)
        self.assertEqual(counts["F-Droid"], 1)
        self.assertEqual(counts["not_found"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["timeout"], 1)


if __name__ == "__main__":
    unittest.main()
