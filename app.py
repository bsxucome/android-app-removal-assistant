from __future__ import annotations

import json
import locale
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import ctypes
import webbrowser
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from tkinter import messagebox, ttk

from apkutils2 import APK


APP_TITLE = "Android应用清除助手"
APP_VERSION = "1.2.0"
PROJECT_URL = "https://github.com/bsxucome/android-app-removal-assistant"
CONFIG_NAME = "Android应用清除助手.json"
LEGACY_CONFIG_NAMES = ("安卓应用清理助手.json", "安卓三方应用清理工具.json")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
FORMAT_PLACEHOLDER_RE = re.compile(r"%(?:\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[a-zA-Z]")

UI_BG = "#f7f9fc"
UI_SURFACE = "#ffffff"
UI_SURFACE_ALT = "#f1f3f4"
UI_PRIMARY = "#0b57d0"
UI_PRIMARY_HOVER = "#0842a0"
UI_PRIMARY_CONTAINER = "#d3e3fd"
UI_TEXT = "#1f1f1f"
UI_TEXT_MUTED = "#5f6368"
UI_OUTLINE = "#c4c7c5"
UI_DANGER = "#b3261e"
UI_DANGER_HOVER = "#8c1d18"
UI_DANGER_CONTAINER = "#f9dedc"
UI_SUCCESS = "#146c2e"


def app_dir() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", app_dir()))


def adb_path() -> Path:
    bundled = resource_dir() / "adb" / "adb.exe"
    return bundled if bundled.exists() else Path("adb.exe")


def aapt2_path() -> Path:
    bundled = resource_dir() / "aapt2" / "aapt2.exe"
    if bundled.exists():
        return bundled
    local = app_dir() / "assets" / "aapt2" / "aapt2.exe"
    return local if local.exists() else Path("aapt2.exe")


def icon_path() -> Path:
    bundled = resource_dir() / "icon" / "app-icon.ico"
    if bundled.exists():
        return bundled
    return app_dir() / "assets" / "icon" / "app-icon.ico"


def system_locale() -> tuple[str, str]:
    language = ""
    if sys.platform == "win32":
        try:
            buffer = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
                language = buffer.value
        except (AttributeError, OSError):
            pass
    language = (language or locale.getlocale()[0] or "en_US").replace("-", "_")
    windows_fallbacks = {
        "Chinese (Simplified)_China": "zh_CN",
        "Chinese (Traditional)_Taiwan": "zh_TW",
        "English_United States": "en_US",
    }
    language = windows_fallbacks.get(language, language)
    parts = language.split("_", 1)
    return language, parts[1].upper() if len(parts) == 2 else "US"


@dataclass
class Device:
    serial: str
    state: str
    description: str

    @property
    def display(self) -> str:
        return f"{self.serial}  [{self.state}]  {self.description}".strip()


@dataclass
class AppInfo:
    name: str
    package: str
    apk_path: str
    whitelisted: bool = False


@dataclass(frozen=True)
class LookupResult:
    status: str
    name: str = ""
    source: str = ""
    reason: str = ""


class AdbError(RuntimeError):
    pass


class AdbClient:
    def __init__(self, serial: str | None = None):
        self.serial = serial

    def run(self, *args: str, timeout: int = 45) -> str:
        command = [str(adb_path())]
        if self.serial:
            command += ["-s", self.serial]
        command += list(args)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                startupinfo=startup,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise AdbError("未找到内置 ADB。请重新下载完整程序。") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError("ADB 操作超时，请检查手机连接后重试。") from exc
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode != 0:
            raise AdbError(output or f"ADB 返回错误代码 {result.returncode}")
        return output

    @staticmethod
    def devices() -> list[Device]:
        output = AdbClient().run("devices", "-l")
        devices: list[Device] = []
        for line in output.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial = parts[0]
            state = parts[1] if len(parts) > 1 else "unknown"
            details = []
            for key in ("model:", "product:", "device:"):
                value = next((p[len(key):] for p in parts[2:] if p.startswith(key)), "")
                if value:
                    details.append(value)
            devices.append(Device(serial, state, " / ".join(details)))
        return devices

    def third_party_packages(self) -> list[tuple[str, str]]:
        output = self.run("shell", "pm", "list", "packages", "-3", "-f")
        apps = []
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("package:") or "=" not in line:
                continue
            apk, package = line[len("package:"):].rsplit("=", 1)
            if PACKAGE_RE.fullmatch(package):
                apps.append((package, apk))
        return sorted(apps)

    def pull(self, remote: str, local: Path) -> None:
        self.run("pull", remote, str(local), timeout=180)

    def uninstall(self, package: str) -> tuple[bool, str]:
        try:
            output = self.run("uninstall", package, timeout=120)
        except AdbError as exc:
            return False, str(exc)
        return "success" in output.lower(), output


def is_valid_app_name(name: object, package: str = "") -> bool:
    if not isinstance(name, str):
        return False
    name = name.strip()
    if not name or name.startswith("@") or len(name) > 80:
        return False
    if FORMAT_PLACEHOLDER_RE.search(name) or "\n" in name or "\r" in name:
        return False
    return name != package if package else True


def resolve_app_name(apk_file: Path, package: str) -> str:
    try:
        apk = APK(str(apk_file))
        manifest = apk.get_manifest()
        label = manifest.get("application", {}).get("@android:label", "")
        if not label:
            return package
        if isinstance(label, str) and label.startswith("@"):
            resource_id = int(label[1:], 16)
            values = apk.resources.get_resolved_res_configs(resource_id)
            chinese = [
                value
                for config, value in values
                if config.get_language() == "zh" and is_valid_app_name(value)
            ]
            default = [
                value
                for config, value in values
                if not config.get_language().strip("\x00") and is_valid_app_name(value)
            ]
            valid = [value for _, value in values if is_valid_app_name(value)]
            label = (chinese or default or valid)[0] if (chinese or default or valid) else ""
        label = str(label).strip()
        return label if is_valid_app_name(label, package) else package
    except Exception:
        return package


def resolve_app_name_aapt2(apk_file: Path, package: str, language: str | None = None) -> str:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        result = subprocess.run(
            [str(aapt2_path()), "dump", "badging", str(apk_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            startupinfo=startup,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return package
    if result.returncode != 0:
        return package

    localized: dict[str, str] = {}
    default = ""
    application = ""
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"application-label(?:-([^:]+))?:'(.*)'", line.strip())
        if match:
            key = (match.group(1) or "").replace("-", "_").casefold()
            value = match.group(2).strip()
            if is_valid_app_name(value, package):
                if key:
                    localized[key] = value
                else:
                    default = value
        elif line.startswith("application: label='"):
            match = re.match(r"application: label='(.*?)' icon=", line)
            if match and is_valid_app_name(match.group(1), package):
                application = match.group(1).strip()

    requested = (language or system_locale()[0]).replace("-", "_").casefold()
    base_language = requested.split("_", 1)[0]
    return (
        localized.get(requested)
        or localized.get(base_language)
        or default
        or application
        or package
    )


def resolve_app_name_offline(apk_file: Path, package: str, language: str | None = None) -> str:
    name = resolve_app_name(apk_file, package)
    return name if name != package else resolve_app_name_aapt2(apk_file, package, language)


class PlayMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_json_ld = False
        self.current: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self.in_json_ld = True
            self.current = []

    def handle_data(self, data):
        if self.in_json_ld:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json_ld:
            self.parts.append("".join(self.current))
            self.current = []
            self.in_json_ld = False


class FdroidMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.name = ""
        self.in_heading = False
        self.heading_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:title":
            self.name = attributes.get("content", "").strip()
        elif tag in {"h1", "h2"} and not self.name:
            self.in_heading = True
            self.heading_parts = []

    def handle_data(self, data):
        if self.in_heading:
            self.heading_parts.append(data)

    def handle_endtag(self, tag):
        if tag in {"h1", "h2"} and self.in_heading:
            candidate = " ".join("".join(self.heading_parts).split())
            if candidate:
                self.name = candidate
            self.in_heading = False


def network_opener(proxy_url: str = "") -> urllib.request.OpenerDirector:
    proxies = (
        {"http": proxy_url, "https": proxy_url}
        if proxy_url
        else urllib.request.getproxies()
    )
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))


def classify_network_error(exc: BaseException) -> str:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, socket.gaierror):
        return "dns_error"
    if isinstance(reason, ssl.SSLError):
        return "tls_error"
    text = str(reason).casefold()
    if "proxy" in text or "tunnel connection failed" in text:
        return "proxy_error"
    return "connection_error"


def query_google_play_name(
    package: str,
    language: str | None = None,
    region: str | None = None,
    timeout: int = 10,
    opener: urllib.request.OpenerDirector | None = None,
) -> LookupResult:
    language, detected_region = system_locale() if language is None else (language, region or "US")
    region = region or detected_region
    query = urllib.parse.urlencode({"id": package, "hl": language.replace("-", "_"), "gl": region})
    request = urllib.request.Request(
        f"https://play.google.com/store/apps/details?{query}",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AndroidAppCleaner/1.0",
            "Accept-Language": language.replace("_", "-"),
        },
    )
    try:
        with (opener or network_opener()).open(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return LookupResult("not_found", source="Google Play") if exc.code == 404 else LookupResult(
            "http_error", source="Google Play", reason=f"HTTP {exc.code}"
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return LookupResult(classify_network_error(exc), source="Google Play", reason=str(exc))

    parser = PlayMetadataParser()
    try:
        parser.feed(html)
    except Exception as exc:
        return LookupResult("parse_error", source="Google Play", reason=str(exc))
    for raw in parser.parts:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if (
                isinstance(item, dict)
                and item.get("@type") == "SoftwareApplication"
                and is_valid_app_name(item.get("name"), package)
            ):
                return LookupResult("success", item["name"].strip(), "Google Play")
    return LookupResult("parse_error", source="Google Play", reason="页面中未找到应用名称元数据")


def query_fdroid_name(
    package: str,
    language: str | None = None,
    timeout: int = 10,
    opener: urllib.request.OpenerDirector | None = None,
) -> LookupResult:
    language = language or system_locale()[0]
    request = urllib.request.Request(
        f"https://f-droid.org/packages/{urllib.parse.quote(package, safe='.')}/",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AndroidAppCleaner/1.1",
            "Accept-Language": language.replace("_", "-"),
        },
    )
    try:
        with (opener or network_opener()).open(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return LookupResult("not_found", source="F-Droid") if exc.code == 404 else LookupResult(
            "http_error", source="F-Droid", reason=f"HTTP {exc.code}"
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return LookupResult(classify_network_error(exc), source="F-Droid", reason=str(exc))

    parser = FdroidMetadataParser()
    try:
        parser.feed(html)
    except Exception as exc:
        return LookupResult("parse_error", source="F-Droid", reason=str(exc))
    name = re.sub(r"\s*\|\s*F-Droid.*$", "", parser.name, flags=re.IGNORECASE).strip()
    return (
        LookupResult("success", name, "F-Droid")
        if is_valid_app_name(name, package)
        else LookupResult("parse_error", source="F-Droid", reason="页面中未找到应用名称")
    )


def query_online_name(
    package: str,
    language: str | None = None,
    region: str | None = None,
    timeout: int = 10,
    proxy_url: str = "",
) -> tuple[LookupResult, list[LookupResult]]:
    opener = network_opener(proxy_url)
    attempts = [
        query_google_play_name(package, language, region, timeout, opener),
    ]
    if attempts[-1].status == "success":
        return attempts[-1], attempts
    attempts.append(query_fdroid_name(package, language, timeout, opener))
    if attempts[-1].status == "success":
        return attempts[-1], attempts
    if all(item.status == "not_found" for item in attempts):
        return LookupResult("not_found", reason="两个来源均未收录"), attempts
    return LookupResult("failed", reason="所有来源均查询失败"), attempts


class ConfirmDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, text: str, phrase: str):
        super().__init__(parent)
        self.result = False
        self.phrase = phrase
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=text, justify="left", wraplength=520).pack(anchor="w")
        ttk.Label(frame, text=f"\n请输入：{phrase}", foreground="#b42318").pack(anchor="w")
        self.entry = ttk.Entry(frame, width=55)
        self.entry.pack(fill="x", pady=(8, 16))
        self.entry.bind("<KeyRelease>", self._update)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        self.ok = ttk.Button(buttons, text="确认卸载", command=self._accept, state="disabled")
        self.ok.pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.entry.focus_set()
        self.wait_window()

    def _update(self, _event=None):
        self.ok.configure(state="normal" if self.entry.get().strip() == self.phrase else "disabled")

    def _accept(self):
        self.result = True
        self.destroy()


class DeviceDropdown(tk.Frame):
    def __init__(self, parent, variable: tk.StringVar, command):
        super().__init__(
            parent,
            background=UI_SURFACE_ALT,
            highlightbackground=UI_OUTLINE,
            highlightcolor=UI_PRIMARY,
            highlightthickness=1,
            height=36,
        )
        self.variable = variable
        self.command = command
        self.values: list[str] = []
        self.selected_index = -1
        self.popup: tk.Frame | None = None
        self.listbox: tk.Listbox | None = None
        self.outside_bind_id: str | None = None
        self.pack_propagate(False)

        self.label = tk.Label(
            self,
            textvariable=variable,
            background=UI_SURFACE_ALT,
            foreground=UI_TEXT,
            anchor="w",
            padx=11,
            font=("Microsoft YaHei UI", 9),
        )
        self.label.pack(side="left", fill="both", expand=True)
        self.arrow = tk.Label(
            self,
            text="▼",
            background=UI_SURFACE_ALT,
            foreground=UI_TEXT_MUTED,
            width=2,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.arrow.pack(side="right", fill="y")
        for widget in (self, self.label, self.arrow):
            widget.bind("<Button-1>", self._toggle_popup)
        self.bind("<Destroy>", lambda _event: self._close_popup())

    def __setitem__(self, key, value):
        if key != "values":
            return super().__setitem__(key, value)
        self.values = list(value)
        if self.selected_index >= len(self.values):
            self.selected_index = -1

    def current(self, index: int | None = None) -> int:
        if index is None:
            return self.selected_index
        if not 0 <= index < len(self.values):
            self.selected_index = -1
            self.variable.set("")
            return -1
        self.selected_index = index
        self.variable.set(self.values[index])
        return index

    def _toggle_popup(self, _event=None):
        if self.popup and self.popup.winfo_exists():
            self._close_popup()
        elif self.values:
            self._open_popup()
        return "break"

    def _open_popup(self):
        self.update_idletasks()
        host = self.winfo_toplevel()
        popup = tk.Frame(
            host,
            background=UI_SURFACE,
            highlightbackground=UI_OUTLINE,
            highlightthickness=1,
        )
        self.popup = popup

        visible_rows = min(max(len(self.values), 1), 8)
        listbox = tk.Listbox(
            popup,
            activestyle="none",
            background=UI_SURFACE,
            foreground=UI_TEXT,
            selectbackground=UI_PRIMARY_CONTAINER,
            selectforeground=UI_TEXT,
            borderwidth=0,
            relief="flat",
            highlightthickness=0,
            exportselection=False,
            font=("Microsoft YaHei UI", 9),
            height=visible_rows,
        )
        self.listbox = listbox
        for value in self.values:
            listbox.insert("end", value)
        if self.selected_index >= 0:
            listbox.selection_set(self.selected_index)
            listbox.activate(self.selected_index)
            listbox.see(self.selected_index)
        listbox.pack(fill="both", expand=True)
        listbox.bind("<ButtonRelease-1>", self._choose_from_popup)
        listbox.bind("<Return>", self._choose_from_popup)
        listbox.bind("<Escape>", lambda _event: self._close_popup())
        self.outside_bind_id = self.winfo_toplevel().bind(
            "<Button-1>", self._close_popup_from_outside, add="+"
        )

        width = max(self.winfo_width(), 1)
        row_height = max(listbox.winfo_reqheight(), 22)
        x = self.winfo_rootx() - host.winfo_rootx()
        y = self.winfo_rooty() - host.winfo_rooty() + self.winfo_height()
        screen_height = self.winfo_screenheight()
        if self.winfo_rooty() + self.winfo_height() + row_height > screen_height:
            y -= self.winfo_height() + row_height
        popup.place(x=x, y=y, width=width, height=row_height)
        popup.lift()
        listbox.focus_set()

    def _choose_from_popup(self, _event=None):
        if not self.listbox:
            return
        selected = self.listbox.curselection()
        if selected:
            self.current(selected[0])
            self.command()
        self._close_popup()

    def _close_popup_from_outside(self, event):
        widget = event.widget
        while widget is not None:
            if widget is self or widget is self.popup:
                return
            widget = getattr(widget, "master", None)
        self._close_popup()

    def _close_popup(self):
        popup = self.popup
        self.popup = None
        self.listbox = None
        if self.outside_bind_id:
            try:
                self.winfo_toplevel().unbind("<Button-1>", self.outside_bind_id)
            except tk.TclError:
                pass
            self.outside_bind_id = None
        if popup and popup.winfo_exists():
            popup.destroy()


class CleanerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        try:
            self.iconbitmap(default=str(icon_path()))
        except tk.TclError:
            pass
        self.geometry("1000x740")
        self.minsize(900, 640)
        self.devices: list[Device] = []
        self.apps: list[AppInfo] = []
        self.uninstall_checked: set[str] = set()
        self.keep_checked: set[str] = set()
        self.name_cache: dict[str, dict[str, str]] = {}
        self.online_name_cache: dict[str, dict[str, str]] = {}
        self.online_lookup_consent = False
        self.proxy_url = ""
        self.events: queue.Queue = queue.Queue()
        self.logs: list[str] = []
        self.busy = False
        self.progress_hide_job = None
        self.cancel_event = threading.Event()
        self._build_ui()
        self._load_keep_list()
        self.after_idle(self.focus_set)
        self.after(100, self._drain_events)
        self.after(250, self.refresh_devices)

    def _build_ui(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.configure(background=UI_BG)
        style.configure(".", font=("Microsoft YaHei UI", 9))
        style.configure("TFrame", background=UI_BG)
        style.configure("Card.TFrame", background=UI_SURFACE)
        style.configure("TLabel", background=UI_BG, foreground=UI_TEXT)
        style.configure("Card.TLabel", background=UI_SURFACE, foreground=UI_TEXT)
        style.configure(
            "Title.TLabel",
            background=UI_BG,
            foreground=UI_TEXT,
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=UI_BG,
            foreground=UI_TEXT_MUTED,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Section.TLabel",
            background=UI_SURFACE,
            foreground=UI_TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure(
            "Device.TCombobox",
            padding=(8, 5),
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Device.TButton",
            background=UI_PRIMARY_CONTAINER,
            foreground=UI_PRIMARY,
            borderwidth=0,
            padding=(14, 7),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Device.TButton",
            background=[("active", "#b8d2fa"), ("pressed", "#a8c7fa")],
            foreground=[("disabled", "#9aa0a6")],
        )
        style.configure(
            "Meta.TLabel",
            background=UI_BG,
            foreground=UI_TEXT_MUTED,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "About.TButton",
            background=UI_BG,
            foreground=UI_TEXT_MUTED,
            borderwidth=0,
            padding=(8, 4),
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "About.TButton",
            background=[("active", UI_PRIMARY_CONTAINER)],
            foreground=[("active", UI_PRIMARY)],
        )
        style.configure("TNotebook", background=UI_BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            padding=(16, 7),
            background=UI_SURFACE_ALT,
            foreground=UI_TEXT_MUTED,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", UI_PRIMARY), ("active", UI_PRIMARY_CONTAINER)],
            foreground=[("selected", "#ffffff"), ("active", UI_PRIMARY)],
            font=[("selected", ("Microsoft YaHei UI", 10, "bold"))],
            padding=[("selected", (22, 11))],
        )
        style.configure(
            "Treeview",
            rowheight=34,
            background=UI_SURFACE,
            fieldbackground=UI_SURFACE,
            foreground=UI_TEXT,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Treeview.Heading",
            background=UI_SURFACE_ALT,
            foreground="#3c4043",
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(8, 9),
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", UI_PRIMARY_CONTAINER)],
            foreground=[("selected", UI_TEXT)],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#e8eaed")],
        )
        style.configure(
            "Primary.TButton",
            background=UI_PRIMARY,
            foreground="#ffffff",
            borderwidth=0,
            padding=(16, 9),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", UI_PRIMARY_HOVER), ("disabled", "#a8c7fa")],
        )
        style.configure(
            "Danger.TButton",
            background=UI_DANGER,
            foreground="#ffffff",
            borderwidth=0,
            padding=(16, 9),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Danger.TButton",
            background=[("active", UI_DANGER_HOVER), ("disabled", "#e6b8b5")],
        )
        style.configure(
            "CompactPrimary.TButton",
            background=UI_PRIMARY,
            foreground="#ffffff",
            borderwidth=0,
            padding=(12, 7),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "CompactPrimary.TButton",
            background=[("active", UI_PRIMARY_HOVER), ("disabled", "#a8c7fa")],
        )
        style.configure(
            "CompactDanger.TButton",
            background=UI_DANGER,
            foreground="#ffffff",
            borderwidth=0,
            padding=(12, 7),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "CompactDanger.TButton",
            background=[("active", UI_DANGER_HOVER), ("disabled", "#e6b8b5")],
        )
        style.configure(
            "CompactOnline.TButton",
            background=UI_PRIMARY_CONTAINER,
            foreground=UI_PRIMARY,
            borderwidth=0,
            padding=(12, 7),
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "CompactOnline.TButton",
            background=[("active", "#b8d2fa"), ("disabled", "#edf0f4")],
            foreground=[("disabled", "#9aa0a6")],
        )
        style.configure(
            "Success.Horizontal.TProgressbar",
            troughcolor="#e8eaed",
            background=UI_SUCCESS,
            borderwidth=0,
            thickness=4,
        )
        self.checkbox_off = self._checkbox_image(False)
        self.checkbox_on = self._checkbox_image(True)
        root = ttk.Frame(self, padding=(22, 18, 22, 16))
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 14))
        title_block = ttk.Frame(header)
        title_block.pack(side="left")
        ttk.Label(title_block, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_block,
            text="安全管理已连接 Android 设备中的第三方应用",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Button(
            header,
            text="关于软件",
            command=self._show_about,
            style="About.TButton",
            cursor="hand2",
        ).pack(side="right", anchor="n")

        device_row = ttk.Frame(root, style="Card.TFrame", padding=(14, 12))
        device_row.pack(fill="x", pady=(0, 12))
        device_icon = tk.Label(
            device_row,
            text="●",
            background=UI_SURFACE,
            foreground=UI_PRIMARY,
            font=("Segoe UI Symbol", 12),
        )
        device_icon.pack(side="left", padx=(0, 8))
        ttk.Label(
            device_row,
            text="安卓设备",
            style="Card.TLabel",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left", padx=(0, 12))
        self.device_var = tk.StringVar()
        self.device_box = DeviceDropdown(
            device_row,
            variable=self.device_var,
            command=self._device_selection_changed,
        )
        self.device_box.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ttk.Button(
            device_row,
            text="刷新设备",
            command=self.refresh_devices,
            style="Device.TButton",
        ).pack(side="left")
        self.connection_notice = tk.Frame(
            root,
            background="#fef7e0",
            highlightbackground="#f9ab00",
            highlightthickness=1,
            padx=14,
            pady=11,
        )
        self.connection_notice_text = tk.StringVar(
            value="未检测到可用设备。请开启“开发者选项 > USB 调试”，连接手机后允许 USB 调试。"
        )
        tk.Label(
            self.connection_notice,
            text="!",
            background="#fef7e0",
            foreground="#b06000",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(side="left", padx=(0, 10))
        tk.Label(
            self.connection_notice,
            textvariable=self.connection_notice_text,
            background="#fef7e0",
            foreground="#5f4300",
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
            justify="left",
        ).pack(side="left", fill="x", expand=True)
        self.connection_notice.pack(fill="x", pady=(0, 12))

        style.layout("Clean.TNotebook.Tab", [])
        style.configure("Clean.TNotebook", borderwidth=0, background=UI_BG)
        nav = ttk.Frame(root)
        nav.pack(fill="x", pady=(0, 10))
        self.nav_buttons = []
        self.nav_buttons.append(
            self._make_nav_button(nav, "勾选应用进行卸载", 0)
        )
        self.nav_buttons.append(
            self._make_nav_button(nav, "勾选保留，清理其余", 1)
        )
        self.notebook = ttk.Notebook(root, style="Clean.TNotebook", height=340)
        self.notebook.pack(fill="both", expand=True)
        scan_tab = ttk.Frame(self.notebook, style="Card.TFrame", padding=(16, 14))
        whitelist_tab = ttk.Frame(self.notebook, style="Card.TFrame", padding=(16, 14))
        self.notebook.add(scan_tab, text="勾选应用进行卸载")
        self.notebook.add(whitelist_tab, text="勾选保留，清理其余")
        self.notebook.bind("<<NotebookTabChanged>>", self._sync_nav_buttons)
        self.after_idle(self._sync_nav_buttons)

        scan_heading = ttk.Frame(scan_tab, style="Card.TFrame")
        scan_heading.pack(fill="x", pady=(0, 12))
        scan_heading_text = ttk.Frame(scan_heading, style="Card.TFrame")
        scan_heading_text.pack(side="left")
        ttk.Label(
            scan_heading_text,
            text="选择要卸载的应用",
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            scan_heading_text,
            text="扫描后勾选应用，可一次卸载多个项目",
            style="Card.TLabel",
            foreground=UI_TEXT_MUTED,
        ).pack(anchor="w", pady=(2, 0))

        toolbar = ttk.Frame(scan_tab, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        self.uninstall_search_var = tk.StringVar()
        _, self.uninstall_clear_label = self._make_search_box(
            toolbar, self.uninstall_search_var, UI_SURFACE
        )
        self.uninstall_search_var.trace_add("write", self._search_changed)
        self.selected_count_var = tk.StringVar(value="已选择 0 个应用")
        ttk.Label(
            toolbar,
            textvariable=self.selected_count_var,
            style="Card.TLabel",
            foreground=UI_TEXT_MUTED,
        ).pack(side="left", padx=(12, 0))
        self.uninstall_button = ttk.Button(
            scan_heading,
            text="卸载已选应用",
            command=self.uninstall_selected,
            state="disabled",
            style="CompactDanger.TButton",
        )
        self.uninstall_button.pack(side="right")
        self.online_lookup_button = ttk.Button(
            scan_heading,
            text="联网补全名称（0）",
            command=self.lookup_names_online,
            state="disabled",
            style="CompactOnline.TButton",
        )
        self.online_lookup_button.pack(side="right", padx=(0, 8))
        self.scan_button = ttk.Button(
            scan_heading,
            text="扫描手机应用",
            command=self.scan_apps,
            style="CompactPrimary.TButton",
        )
        self.scan_button.pack(side="right", padx=(0, 8))

        scan_list_frame = tk.Frame(
            scan_tab,
            background=UI_SURFACE,
            highlightbackground="#dadce0",
            highlightthickness=1,
        )
        scan_list_frame.pack(fill="both", expand=True)
        columns = ("name", "package")
        self.tree = ttk.Treeview(
            scan_list_frame,
            columns=columns,
            show="tree headings",
            selectmode="none",
            height=7,
        )
        self.tree.heading("#0", text="全选", command=self._toggle_select_all)
        self.tree.column("#0", width=92, minwidth=92, stretch=False, anchor="center")
        self.tree.heading("name", text="应用名（系统标签）")
        self.tree.heading("package", text="包名")
        self.tree.column("name", width=290)
        self.tree.column("package", width=500)
        self.tree.bind("<Button-1>", self._toggle_uninstall_check)
        tree_scroll = ttk.Scrollbar(scan_list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.scan_empty_state = self._make_empty_state(
            scan_list_frame,
            "第 1 步：扫描手机应用",
            "读取手机中用户安装的应用后，才能进行搜索、选择和卸载。",
            self.scan_apps,
        )

        keep_tip = tk.Frame(
            whitelist_tab,
            background="#e8f0fe",
            highlightbackground="#aecbfa",
            highlightthickness=1,
            padx=13,
            pady=10,
        )
        keep_tip.pack(fill="x", pady=(0, 10))
        tk.Label(
            keep_tip,
            text="保留规则",
            background="#e8f0fe",
            foreground=UI_PRIMARY,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(0, 10))
        tk.Label(
            keep_tip,
            text="勾选需要保留的应用；清理时仅卸载未勾选项。选择会自动保存。",
            background="#e8f0fe",
            foreground="#3c4043",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")
        keep_heading = ttk.Frame(whitelist_tab, style="Card.TFrame")
        keep_heading.pack(fill="x", pady=(0, 12), before=keep_tip)
        keep_heading_text = ttk.Frame(keep_heading, style="Card.TFrame")
        keep_heading_text.pack(side="left")
        ttk.Label(
            keep_heading_text,
            text="保留重要应用，清理其余应用",
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            keep_heading_text,
            text="已勾选的应用会保留，未勾选的应用将被卸载",
            style="Card.TLabel",
            foreground=UI_TEXT_MUTED,
        ).pack(anchor="w", pady=(2, 0))

        keep_toolbar = ttk.Frame(whitelist_tab, style="Card.TFrame")
        keep_toolbar.pack(fill="x", pady=(0, 10))
        self.keep_search_var = tk.StringVar()
        _, self.keep_clear_label = self._make_search_box(
            keep_toolbar, self.keep_search_var, UI_SURFACE
        )
        self.keep_search_var.trace_add("write", self._search_changed)
        self.keep_count_var = tk.StringVar(value="已保留 0 个应用")
        ttk.Label(
            keep_toolbar,
            textvariable=self.keep_count_var,
            style="Card.TLabel",
            foreground=UI_TEXT_MUTED,
        ).pack(side="right")
        self.keep_online_lookup_button = ttk.Button(
            keep_toolbar,
            text="联网补全名称（0）",
            command=self.lookup_names_online,
            state="disabled",
            style="CompactOnline.TButton",
        )
        self.keep_online_lookup_button.pack(side="right", padx=(0, 12))

        keep_list_frame = tk.Frame(
            whitelist_tab,
            background=UI_SURFACE,
            highlightbackground="#dadce0",
            highlightthickness=1,
        )
        keep_list_frame.pack(fill="both", expand=True)
        keep_columns = ("name", "package")
        self.keep_tree = ttk.Treeview(
            keep_list_frame,
            columns=keep_columns,
            show="tree headings",
            selectmode="none",
            height=4,
        )
        self.keep_tree.heading("#0", text="全选", command=self._toggle_select_all_keep)
        self.keep_tree.column("#0", width=92, minwidth=92, stretch=False, anchor="center")
        self.keep_tree.heading("name", text="应用名")
        self.keep_tree.heading("package", text="包名")
        self.keep_tree.column("name", width=280)
        self.keep_tree.column("package", width=520)
        self.keep_tree.bind("<Button-1>", self._toggle_keep_check)
        keep_scroll = ttk.Scrollbar(keep_list_frame, orient="vertical", command=self.keep_tree.yview)
        self.keep_tree.configure(yscrollcommand=keep_scroll.set)
        keep_scroll.pack(side="right", fill="y")
        self.keep_tree.pack(fill="both", expand=True)
        self.keep_empty_state = self._make_empty_state(
            keep_list_frame,
            "请先扫描手机应用",
            "扫描完成后，可在这里勾选需要保留的应用。",
            self.scan_apps,
        )
        white_buttons = ttk.Frame(whitelist_tab, style="Card.TFrame")
        white_buttons.pack(fill="x", pady=(10, 0))
        tk.Label(
            white_buttons,
            text="卸载会同时删除应用数据，请确认保留项无误。",
            background=UI_SURFACE,
            foreground=UI_DANGER,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", pady=5)
        self.bulk_button = ttk.Button(
            white_buttons,
            text="卸载所有未保留应用",
            command=self.bulk_uninstall,
            style="Danger.TButton",
            state="disabled",
        )
        self.bulk_button.pack(side="right")

        self.status_frame = ttk.Frame(root, style="Card.TFrame", padding=(14, 10))
        self.status_frame.pack(fill="x", pady=(12, 0))
        self.progress = ttk.Progressbar(
            self.status_frame, mode="determinate", style="Success.Horizontal.TProgressbar"
        )
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(
            self.status_frame, textvariable=self.status_var, style="Card.TLabel"
        )
        self.status_label.pack(side="left", anchor="w")
        ttk.Label(
            self.status_frame,
            text=f"v{APP_VERSION}",
            style="Card.TLabel",
            foreground="#9aa0a6",
            font=("Microsoft YaHei UI", 8),
        ).pack(side="right")

    def _config_path(self) -> Path:
        return app_dir() / CONFIG_NAME

    def _show_about(self):
        dialog = tk.Toplevel(self)
        dialog.title("关于软件")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        try:
            dialog.iconbitmap(default=str(icon_path()))
        except tk.TclError:
            pass

        card = ttk.Frame(dialog, padding=(24, 20))
        card.pack(fill="both", expand=True)
        ttk.Label(
            card,
            text=APP_TITLE,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            card,
            text=f"版本 {APP_VERSION}",
            foreground="#687b8d",
        ).pack(anchor="w", pady=(5, 12))
        ttk.Label(
            card,
            text="用于扫描和清除 Android 设备中的第三方应用。",
            foreground="#40566b",
        ).pack(anchor="w")
        project_link = tk.Label(
            card,
            text="查看 GitHub 项目",
            foreground="#1677d2",
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "underline"),
        )
        project_link.pack(anchor="w", pady=(14, 18))
        project_link.bind("<Button-1>", lambda _event: webbrowser.open(PROJECT_URL))
        buttons = ttk.Frame(card)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="网络设置", command=lambda: self._show_network_settings(dialog)).pack(
            side="left"
        )
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side="right")

        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _show_network_settings(self, parent):
        dialog = tk.Toplevel(parent)
        dialog.title("网络设置")
        dialog.resizable(False, False)
        dialog.transient(parent)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="代理地址（可选）", font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            frame,
            text="留空时使用 Windows 系统代理或环境变量代理。",
            foreground="#687b8d",
        ).pack(anchor="w", pady=(4, 8))
        proxy_var = tk.StringVar(value=self.proxy_url)
        entry = ttk.Entry(frame, textvariable=proxy_var, width=52)
        entry.pack(fill="x")
        ttk.Label(
            frame,
            text="示例：http://127.0.0.1:7890",
            foreground="#7a8b9a",
        ).pack(anchor="w", pady=(5, 14))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        def close():
            dialog.destroy()
            if parent.winfo_exists():
                parent.grab_set()

        def save():
            value = proxy_var.get().strip()
            if value and not re.match(r"^https?://", value, re.IGNORECASE):
                messagebox.showwarning("代理地址无效", "请输入以 http:// 或 https:// 开头的代理地址。")
                return
            self.proxy_url = value
            self.save_keep_list(quiet=True)
            close()

        ttk.Button(
            buttons,
            text="测试连接",
            command=lambda: self._test_online_sources(proxy_var.get().strip()),
        ).pack(side="left")
        ttk.Button(buttons, text="取消", command=close).pack(side="right")
        ttk.Button(buttons, text="保存", command=save).pack(side="right", padx=(0, 8))
        entry.focus_set()
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.bind("<Escape>", lambda _event: close())

    def _test_online_sources(self, proxy_url: str):
        if self.busy:
            return
        if proxy_url and not re.match(r"^https?://", proxy_url, re.IGNORECASE):
            messagebox.showwarning("代理地址无效", "请输入正确的代理地址后再测试。")
            return
        self._set_busy(True, "正在测试应用商店连接…")

        def worker():
            opener = network_opener(proxy_url)
            play = query_google_play_name("com.spotify.music", "zh_CN", "CN", 8, opener)
            fdroid = query_fdroid_name("org.fdroid.fdroid", "en_US", 8, opener)
            self.events.put(("network_test_done", play, fdroid))
            self.events.put(("idle",))

        threading.Thread(target=worker, daemon=True).start()

    def _make_empty_state(self, parent, title, description, command):
        card = tk.Frame(
            parent,
            background=UI_SURFACE,
            padx=36,
            pady=28,
        )
        tk.Label(
            card,
            text=title,
            background=UI_SURFACE,
            foreground=UI_TEXT,
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack()
        tk.Label(
            card,
            text=description,
            background=UI_SURFACE,
            foreground=UI_TEXT_MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(pady=(8, 16))
        ttk.Button(
            card,
            text="扫描手机应用",
            command=command,
            style="Primary.TButton",
        ).pack()
        card.place(relx=0.5, rely=0.45, anchor="center")
        return card

    def _update_empty_states(self):
        states = (
            (self.scan_empty_state, self.tree),
            (self.keep_empty_state, self.keep_tree),
        )
        for card, tree in states:
            if self.apps:
                card.place_forget()
            elif not card.winfo_ismapped():
                card.place(relx=0.5, rely=0.45, anchor="center")
            tree.configure(takefocus=bool(self.apps))

    def _make_nav_button(self, parent: tk.Misc, text: str, index: int):
        canvas = tk.Canvas(
            parent,
            width=210,
            height=46,
            background=UI_BG,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        canvas.pack(side="left", padx=(0, 8))
        canvas.nav_text = text
        canvas.nav_index = index
        canvas.bind("<Button-1>", lambda _event: self.notebook.select(index))
        canvas.bind("<Enter>", lambda _event: self._draw_nav_button(canvas, hover=True))
        canvas.bind("<Leave>", lambda _event: self._draw_nav_button(canvas))
        self._draw_nav_button(canvas)
        return canvas

    def _draw_nav_button(self, canvas: tk.Canvas, hover: bool = False):
        if not hasattr(self, "notebook"):
            selected = canvas.nav_index == 0
        else:
            selected = self.notebook.index(self.notebook.select()) == canvas.nav_index
        canvas.delete("all")
        fill = UI_PRIMARY_CONTAINER if selected else ("#eef3fc" if hover else UI_BG)
        outline = UI_PRIMARY_CONTAINER if selected else UI_BG
        foreground = UI_PRIMARY if selected else UI_TEXT_MUTED
        self._rounded_rectangle(
            canvas, 1, 1, 209, 45, radius=22, fill=fill, outline=outline, width=1
        )
        canvas.create_text(
            105,
            23,
            text=canvas.nav_text,
            fill=foreground,
            font=("Microsoft YaHei UI", 10, "bold" if selected else "normal"),
        )

    def _sync_nav_buttons(self, _event=None):
        for button in getattr(self, "nav_buttons", []):
            self._draw_nav_button(button)

    def _make_search_box(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        parent_background: str = UI_BG,
    ):
        canvas = tk.Canvas(
            parent,
            width=310,
            height=40,
            background=parent_background,
            highlightthickness=0,
            borderwidth=0,
        )
        canvas.pack(side="left")

        def draw_border(color=UI_OUTLINE, width=1):
            canvas.delete("search_bg")
            self._rounded_rectangle(
                canvas,
                1,
                1,
                309,
                39,
                radius=19,
                fill=UI_SURFACE_ALT,
                outline=color,
                width=width,
                tags="search_bg",
            )
            canvas.tag_lower("search_bg")

        draw_border()
        search_icon = tk.Label(
            canvas,
            text="⌕",
            background=UI_SURFACE_ALT,
            foreground=UI_TEXT_MUTED,
            font=("Segoe UI Symbol", 13),
        )
        canvas.create_window(21, 20, window=search_icon, width=26, height=28)
        entry = tk.Entry(
            canvas,
            textvariable=variable,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            background=UI_SURFACE_ALT,
            foreground=UI_TEXT,
            insertbackground=UI_PRIMARY,
            font=("Microsoft YaHei UI", 9),
        )
        canvas.create_window(150, 20, window=entry, width=225, height=26)
        placeholder = tk.Label(
            canvas,
            text="搜索应用名或包名",
            background=UI_SURFACE_ALT,
            foreground="#80868b",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            cursor="xterm",
        )
        placeholder_window = canvas.create_window(
            150, 20, window=placeholder, width=225, height=26
        )
        clear = tk.Label(
            canvas,
            text="×",
            background=UI_SURFACE_ALT,
            foreground="#9aa0a6",
            activebackground=UI_PRIMARY_CONTAINER,
            activeforeground=UI_DANGER,
            cursor="arrow",
            font=("Microsoft YaHei UI", 12),
            padx=8,
        )
        canvas.create_window(289, 20, window=clear, width=36, height=30)
        clear.bind("<Button-1>", lambda _event: variable.set(""))
        entry.bind("<Escape>", lambda _event: variable.set(""))

        def update_placeholder(*_args):
            if variable.get() or entry.focus_get() == entry:
                canvas.itemconfigure(placeholder_window, state="hidden")
            else:
                canvas.itemconfigure(placeholder_window, state="normal")

        placeholder.bind("<Button-1>", lambda _event: entry.focus_set())
        search_icon.bind("<Button-1>", lambda _event: entry.focus_set())
        entry.bind(
            "<FocusIn>",
            lambda _event: (draw_border(UI_PRIMARY, 2), update_placeholder()),
        )
        entry.bind(
            "<FocusOut>",
            lambda _event: (draw_border(UI_OUTLINE, 1), update_placeholder()),
        )
        variable.trace_add("write", update_placeholder)
        return canvas, clear

    @staticmethod
    def _rounded_rectangle(canvas, x1, y1, x2, y2, radius=10, **kwargs):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def _checkbox_image(self, checked: bool) -> tk.PhotoImage:
        image = tk.PhotoImage(width=18, height=18)
        image.put(UI_SURFACE, to=(0, 0, 18, 18))
        image.put("#747775", to=(2, 2, 16, 16))
        image.put(UI_SURFACE, to=(3, 3, 15, 15))
        if checked:
            image.put(UI_PRIMARY, to=(3, 3, 15, 15))
            for x, y in ((5, 8), (6, 9), (7, 10), (8, 9), (9, 8), (10, 7), (11, 6), (12, 5)):
                image.put("#ffffff", to=(x, y, x + 2, y + 2))
        return image

    def _load_keep_list(self):
        data = {}
        paths = [self._config_path(), *(app_dir() / name for name in LEGACY_CONFIG_NAMES)]
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError, AttributeError):
                continue
        values = data.get("whitelist", [])
        self.keep_checked = {value for value in values if PACKAGE_RE.fullmatch(value)}
        cache = data.get("name_cache", {})
        self.name_cache = {
            package: value
            for package, value in cache.items()
            if PACKAGE_RE.fullmatch(package)
            and isinstance(value, dict)
            and isinstance(value.get("path"), str)
            and isinstance(value.get("name"), str)
            and is_valid_app_name(value.get("name"), package)
            and value.get("name") != package
        }
        online_cache = data.get("online_name_cache", {})
        self.online_name_cache = {
            package: value
            for package, value in online_cache.items()
            if PACKAGE_RE.fullmatch(package)
            and isinstance(value, dict)
            and is_valid_app_name(value.get("name"), package)
            and isinstance(value.get("locale"), str)
            and isinstance(value.get("fetched_at"), str)
        }
        self.online_lookup_consent = data.get("online_lookup_consent") is True
        proxy_url = data.get("proxy_url", "")
        self.proxy_url = proxy_url if isinstance(proxy_url, str) else ""
        self._update_counts()

    def get_whitelist(self) -> set[str]:
        return set(self.keep_checked)

    def save_keep_list(self, quiet: bool = False) -> bool:
        values = sorted(self.keep_checked)
        try:
            self._config_path().write_text(
                json.dumps(
                    {
                        "whitelist": values,
                        "name_cache": self.name_cache,
                        "online_lookup_consent": self.online_lookup_consent,
                        "online_name_cache": self.online_name_cache,
                        "proxy_url": self.proxy_url,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法在程序目录保存保留选择：\n{exc}")
            return False
        if not quiet:
            messagebox.showinfo("已保存", f"已保存 {len(values)} 个需要保留的应用。")
        self._refresh_trees()
        return True

    def _selected_device(self) -> Device | None:
        index = self.device_box.current()
        return self.devices[index] if 0 <= index < len(self.devices) else None

    def _device_selection_changed(self, _event=None):
        self._update_connection_notice()

    def _update_connection_notice(self):
        device = self._selected_device()
        if device and device.state == "device":
            self.connection_notice.pack_forget()
            return
        if device and device.state == "unauthorized":
            text = "手机尚未授权。请解锁手机，并在“允许 USB 调试”提示中点击“允许”。"
        elif device and device.state == "offline":
            text = "设备当前离线。请重新插拔 USB 线，或关闭后重新开启 USB 调试。"
        elif device:
            text = f"设备暂不可用（状态：{device.state}）。请检查 USB 调试和连接方式。"
        else:
            text = "未检测到可用设备。请开启“开发者选项 > USB 调试”，连接手机后允许 USB 调试。"
        self.connection_notice_text.set(text)
        if not self.connection_notice.winfo_ismapped():
            self.connection_notice.pack(fill="x", pady=(8, 10), before=self.notebook)

    def _ready_device(self) -> Device | None:
        device = self._selected_device()
        if not device:
            messagebox.showwarning("未选择设备", "请连接手机并点击“刷新设备”。")
            return None
        if device.state != "device":
            hints = {
                "unauthorized": "手机尚未授权 USB 调试，请在手机上点击“允许”。",
                "offline": "设备处于离线状态，请重新插拔 USB 线。",
            }
            messagebox.showwarning("设备不可用", hints.get(device.state, f"设备状态：{device.state}"))
            return None
        return device

    def refresh_devices(self):
        if self.busy:
            return
        self._set_busy(True, "正在查找设备…")

        def worker():
            try:
                devices = AdbClient.devices()
                self.events.put(("devices", devices))
            except Exception as exc:
                self.events.put(("error", "刷新设备失败", str(exc)))
            finally:
                self.events.put(("idle",))

        threading.Thread(target=worker, daemon=True).start()

    def scan_apps(self):
        device = self._ready_device()
        if not device or self.busy:
            return
        self.cancel_event.clear()
        self._set_busy(True, "正在读取用户安装的应用…")
        self.apps = []
        self.uninstall_checked.clear()
        self._refresh_trees()
        whitelist = self.get_whitelist()
        language, region = system_locale()
        locale_key = f"{language}|{region}"

        def worker():
            client = AdbClient(device.serial)
            try:
                packages = client.third_party_packages()
                total = len(packages)
                cached = []
                unresolved = []
                for package, remote_apk in packages:
                    entry = self.name_cache.get(package, {})
                    if entry.get("path") == remote_apk and entry.get("name"):
                        name = entry["name"]
                        cached.append((package, remote_apk))
                    else:
                        online_entry = self.online_name_cache.get(package, {})
                        name = (
                            online_entry.get("name", package)
                            if online_entry.get("locale") == locale_key
                            else package
                        )
                        unresolved.append((package, remote_apk))
                    self.events.put(
                        ("app", AppInfo(name, package, remote_apk, package in whitelist), 0, total)
                    )
                self.events.put(("scan_listed", total, len(cached), len(unresolved)))
                self.events.put(("progress_max", len(unresolved)))

                def resolve_one(item, temp_path):
                    package, remote_apk = item
                    local_apk = temp_path / f"{abs(hash(package))}.apk"
                    AdbClient(device.serial).pull(remote_apk, local_apk)
                    offline_name = resolve_app_name_offline(local_apk, package, language)
                    if offline_name != package:
                        return package, remote_apk, offline_name, "offline"
                    online_entry = self.online_name_cache.get(package, {})
                    online_name = (
                        online_entry.get("name", package)
                        if online_entry.get("locale") == locale_key
                        else package
                    )
                    return package, remote_apk, online_name, "online" if online_name != package else "package"

                with tempfile.TemporaryDirectory(prefix="android_app_cleaner_") as temp:
                    with ThreadPoolExecutor(max_workers=min(4, max(1, len(unresolved)))) as pool:
                        futures = {
                            pool.submit(resolve_one, item, Path(temp)): item for item in unresolved
                        }
                        completed = 0
                        for future in as_completed(futures):
                            if self.cancel_event.is_set():
                                break
                            package, remote_apk = futures[future]
                            try:
                                package, remote_apk, name, source = future.result()
                                if source == "offline":
                                    self.name_cache[package] = {"path": remote_apk, "name": name}
                                else:
                                    self.name_cache.pop(package, None)
                                self.events.put(("name_updated", package, name))
                            except Exception as exc:
                                self.events.put(("log", f"[名称读取失败] {package}: {exc}"))
                            completed += 1
                            self.events.put(("scan_progress", completed, len(unresolved)))
                self.events.put(("scan_done", total, len(cached), len(unresolved)))
            except Exception as exc:
                self.events.put(("error", "扫描失败", str(exc)))
            finally:
                self.events.put(("idle",))

        threading.Thread(target=worker, daemon=True).start()

    def lookup_names_online(self):
        if self.busy:
            return
        targets = [app for app in self.apps if app.name == app.package]
        if not targets:
            messagebox.showinfo("无需补全", "当前应用名称均已识别。")
            return
        if not self.online_lookup_consent:
            allowed = messagebox.askyesno(
                "联网补全名称",
                "为了查询应用商店名称，程序将把以下信息发送给 Google Play 和 F-Droid：\n\n"
                "• 无法识别名称的应用包名\n"
                "• Windows 当前语言和地区\n\n"
                "不会发送设备序列号、应用数据或个人信息。\n"
                "是否同意并记住此选择？",
                icon="info",
            )
            if not allowed:
                return
            self.online_lookup_consent = True
            self.save_keep_list(quiet=True)

        language, region = system_locale()
        locale_key = f"{language}|{region}"
        self._set_busy(True, f"正在联网补全 {len(targets)} 个应用名称…")
        self._show_progress(len(targets))

        def worker():
            results = []
            with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
                futures = {
                    pool.submit(
                        query_online_name,
                        app.package,
                        language,
                        region,
                        10,
                        self.proxy_url,
                    ): app.package
                    for app in targets
                }
                for index, future in enumerate(as_completed(futures), 1):
                    package = futures[future]
                    try:
                        result, attempts = future.result()
                    except Exception as exc:
                        result = LookupResult("failed", reason=str(exc))
                        attempts = []
                    if result.status == "success":
                        self.online_name_cache[package] = {
                            "name": result.name,
                            "locale": locale_key,
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                            "source": result.source,
                        }
                    results.append((package, result, attempts))
                    self.events.put(("online_progress", index, len(targets)))
            self.events.put(("online_done", results))
            self.events.put(("idle",))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_online_results(
        self,
        results: list[tuple[str, LookupResult, list[LookupResult]]],
    ) -> dict[str, int]:
        stats = {
            "success": 0,
            "Google Play": 0,
            "F-Droid": 0,
            "not_found": 0,
            "timeout": 0,
            "dns_error": 0,
            "proxy_error": 0,
            "connection_error": 0,
            "tls_error": 0,
            "http_error": 0,
            "parse_error": 0,
            "failed": 0,
        }
        apps_by_package = {app.package: app for app in self.apps}
        for package, result, attempts in results:
            app = apps_by_package.get(package)
            if result.status == "success" and is_valid_app_name(result.name, package):
                if app and app.name == package:
                    app.name = result.name
                    stats["success"] += 1
                    if result.source in {"Google Play", "F-Droid"}:
                        stats[result.source] += 1
            elif result.status == "not_found":
                stats["not_found"] += 1
            else:
                stats["failed"] += 1
                attempt_statuses = {attempt.status for attempt in attempts}
                for status in attempt_statuses:
                    if status in stats and status not in {"success", "not_found", "failed"}:
                        stats[status] += 1
        return stats

    @staticmethod
    def _online_summary(stats: dict[str, int]) -> tuple[str, str]:
        summary = (
            f"成功补全 {stats['success']} 个"
            f"（Google Play {stats['Google Play']}，F-Droid {stats['F-Droid']}），"
            f"未收录 {stats['not_found']} 个，请求失败 {stats['failed']} 个"
        )
        details = []
        labels = {
            "timeout": "请求超时",
            "dns_error": "DNS 解析失败",
            "proxy_error": "代理连接失败",
            "connection_error": "网络连接失败",
            "tls_error": "安全连接失败",
            "http_error": "商店返回错误",
            "parse_error": "页面格式异常",
        }
        for key, label in labels.items():
            if stats[key]:
                details.append(f"{label} {stats[key]}")
        detail_text = "；".join(details)
        if stats["failed"]:
            proxy_note = (
                "\n\n请确认电脑能访问 Google Play 或 F-Droid。"
                "程序会使用 Windows 系统代理和环境变量代理；浏览器扩展中的代理不会自动生效。"
            )
            detail_text = (f"\n失败原因：{detail_text}" if detail_text else "") + proxy_note
        return summary, detail_text

    def _refresh_trees(self):
        self.tree.delete(*self.tree.get_children())
        self.keep_tree.delete(*self.keep_tree.get_children())
        for app in self.apps:
            app.whitelisted = app.package in self.keep_checked
            if self._app_matches(app, self.uninstall_search_var.get()):
                self.tree.insert(
                    "",
                    "end",
                    iid=f"uninstall:{app.package}",
                    image=self.checkbox_on if app.package in self.uninstall_checked else self.checkbox_off,
                    values=(app.name, app.package),
                )
            if self._app_matches(app, self.keep_search_var.get()):
                self.keep_tree.insert(
                    "",
                    "end",
                    iid=f"keep:{app.package}",
                    image=self.checkbox_on if app.whitelisted else self.checkbox_off,
                    values=(app.name, app.package),
                )
        self._update_counts()
        self._update_empty_states()
        self._update_online_buttons()
        self.uninstall_button.configure(state="normal" if self.apps and not self.busy else "disabled")
        self.bulk_button.configure(state="normal" if self.apps and not self.busy else "disabled")

    def _update_online_buttons(self):
        if not hasattr(self, "online_lookup_button"):
            return
        unknown = sum(app.name == app.package for app in self.apps)
        text = f"联网补全名称（{unknown}）"
        state = "normal" if unknown and not self.busy else "disabled"
        self.online_lookup_button.configure(text=text, state=state)
        self.keep_online_lookup_button.configure(text=text, state=state)

    @staticmethod
    def _app_matches(app: AppInfo, query: str) -> bool:
        terms = query.casefold().split()
        if not terms:
            return True
        searchable = f"{app.name} {app.package}".casefold()
        return all(term in searchable for term in terms)

    def _filtered_packages(self, query: str) -> set[str]:
        return {app.package for app in self.apps if self._app_matches(app, query)}

    def _search_changed(self, *_args):
        if hasattr(self, "tree") and hasattr(self, "keep_tree"):
            self._update_search_clear_icons()
            self._refresh_trees()

    def _update_search_clear_icons(self):
        pairs = (
            (getattr(self, "uninstall_clear_label", None), self.uninstall_search_var),
            (getattr(self, "keep_clear_label", None), self.keep_search_var),
        )
        for label, variable in pairs:
            if label is not None:
                active = bool(variable.get())
                label.configure(
                    foreground=UI_TEXT_MUTED if active else "#bdc1c6",
                    cursor="hand2" if active else "arrow",
                )

    def _update_counts(self):
        if hasattr(self, "selected_count_var"):
            selected = len(self.uninstall_checked & {app.package for app in self.apps})
            visible_packages = self._filtered_packages(self.uninstall_search_var.get())
            self.selected_count_var.set(f"已选择 {selected} 个 · 当前显示 {len(visible_packages)} 个")
            if visible_packages and visible_packages.issubset(self.uninstall_checked):
                self.tree.heading("#0", text="取消全选", command=self._toggle_select_all)
            else:
                self.tree.heading("#0", text="全选", command=self._toggle_select_all)
        if hasattr(self, "keep_count_var"):
            visible = len(self.keep_checked & {app.package for app in self.apps})
            visible_packages = self._filtered_packages(self.keep_search_var.get())
            self.keep_count_var.set(f"已保留 {visible} 个 · 当前显示 {len(visible_packages)} 个")
            if visible_packages and visible_packages.issubset(self.keep_checked):
                self.keep_tree.heading("#0", text="取消全选", command=self._toggle_select_all_keep)
            else:
                self.keep_tree.heading("#0", text="全选", command=self._toggle_select_all_keep)

    def _toggle_uninstall_check(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        package = str(item).removeprefix("uninstall:")
        if package in self.uninstall_checked:
            self.uninstall_checked.remove(package)
        else:
            self.uninstall_checked.add(package)
        self.tree.item(item, image=self.checkbox_on if package in self.uninstall_checked else self.checkbox_off)
        self._update_counts()
        return "break"

    def _toggle_keep_check(self, event):
        item = self.keep_tree.identify_row(event.y)
        if not item:
            return
        package = str(item).removeprefix("keep:")
        if package in self.keep_checked:
            self.keep_checked.remove(package)
        else:
            self.keep_checked.add(package)
        self.save_keep_list(quiet=True)
        return "break"

    def _select_all(self, select: bool):
        visible = self._filtered_packages(self.uninstall_search_var.get())
        if select:
            self.uninstall_checked.update(visible)
        else:
            self.uninstall_checked.difference_update(visible)
        self._refresh_trees()

    def _toggle_select_all(self):
        visible = self._filtered_packages(self.uninstall_search_var.get())
        self._select_all(not visible or not visible.issubset(self.uninstall_checked))

    def _select_all_keep(self, select: bool):
        visible = self._filtered_packages(self.keep_search_var.get())
        if select:
            self.keep_checked.update(visible)
        else:
            self.keep_checked.difference_update(visible)
        self.save_keep_list(quiet=True)

    def _toggle_select_all_keep(self):
        visible = self._filtered_packages(self.keep_search_var.get())
        self._select_all_keep(not visible or not visible.issubset(self.keep_checked))

    def uninstall_selected(self):
        device = self._ready_device()
        if not device or self.busy:
            return
        targets = [app for app in self.apps if app.package in self.uninstall_checked]
        if not targets:
            messagebox.showinfo("尚未选择", "请先勾选需要卸载的应用。")
            return
        preview = "\n".join(f"• {app.name}  ({app.package})" for app in targets[:12])
        if len(targets) > 12:
            preview += f"\n……另有 {len(targets) - 12} 个应用"
        if not messagebox.askyesno(
            "确认卸载所选应用",
            f"即将卸载以下 {len(targets)} 个应用，并删除其应用数据：\n\n{preview}\n\n是否继续？",
            icon="warning",
        ):
            return
        self._start_uninstall_batch(device, targets, "selected")

    def bulk_uninstall(self):
        device = self._ready_device()
        if not device or self.busy or not self.save_keep_list(quiet=True):
            return
        if not self.apps:
            messagebox.showinfo("请先扫描", "请先在“勾选应用进行卸载”页面扫描手机应用。")
            return
        whitelist = self.get_whitelist()
        targets = [app for app in self.apps if app.package not in whitelist]
        if not targets:
            messagebox.showinfo("无需清理", "当前所有用户安装的应用都已设为保留。")
            return
        preview = "\n".join(f"• {app.name}  ({app.package})" for app in targets[:12])
        if len(targets) > 12:
            preview += f"\n……另有 {len(targets) - 12} 个应用"
        phrase = f"确认卸载{len(targets)}个"
        dialog = ConfirmDialog(
            self,
            "确认清理未保留应用",
            f"即将卸载以下 {len(targets)} 个未保留应用，并删除其应用数据：\n\n{preview}",
            phrase,
        )
        if not dialog.result:
            return
        self._start_uninstall_batch(device, targets, "cleanup")

    def _start_uninstall_batch(self, device: Device, targets: list[AppInfo], mode: str):
        self._set_busy(True, f"准备卸载 {len(targets)} 个应用…")
        self._show_progress(len(targets))

        def worker():
            client = AdbClient(device.serial)
            results = []
            for index, app in enumerate(targets, 1):
                ok, output = client.uninstall(app.package)
                results.append((app, ok, output))
                self.events.put(("batch_item", app, ok, output, index, len(targets)))
            self.events.put(("batch_done", results, mode))
            self.events.put(("idle",))

        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy: bool, status: str | None = None):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.bulk_button.configure(state="disabled" if busy or not self.apps else "normal")
        self.uninstall_button.configure(state="disabled" if busy or not self.apps else "normal")
        self._update_online_buttons()
        if status:
            self.status_var.set(status)

    def _show_progress(self, maximum: int):
        if self.progress_hide_job is not None:
            self.after_cancel(self.progress_hide_job)
            self.progress_hide_job = None
        self.progress.configure(maximum=max(maximum, 1), value=0)
        if not self.progress.winfo_ismapped():
            self.progress.pack(fill="x", pady=(0, 7), before=self.status_label)

    def _hide_progress(self):
        self.progress_hide_job = None
        if self.progress.winfo_ismapped():
            self.progress.pack_forget()

    def _schedule_progress_hide(self):
        if self.progress_hide_job is not None:
            self.after_cancel(self.progress_hide_job)
        self.progress_hide_job = self.after(1400, self._hide_progress)

    def _write_log(self, text: str):
        self.logs.append(text.rstrip())
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]

    def _drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "devices":
                    self.devices = event[1]
                    self.device_box["values"] = [d.display for d in self.devices]
                    if self.devices:
                        ready = next((i for i, d in enumerate(self.devices) if d.state == "device"), 0)
                        self.device_box.current(ready)
                        self.status_var.set(f"发现 {len(self.devices)} 台设备")
                    else:
                        self.device_var.set("")
                        self.status_var.set("未发现设备")
                    self._update_connection_notice()
                elif kind == "progress_max":
                    maximum = event[1]
                    if maximum:
                        self._show_progress(maximum)
                    else:
                        self._hide_progress()
                elif kind == "app":
                    app, index, total = event[1:]
                    self.apps.append(app)
                    if self._app_matches(app, self.uninstall_search_var.get()):
                        self.tree.insert(
                            "",
                            "end",
                            iid=f"uninstall:{app.package}",
                            image=self.checkbox_off,
                            values=(app.name, app.package),
                        )
                    if self._app_matches(app, self.keep_search_var.get()):
                        self.keep_tree.insert(
                            "",
                            "end",
                            iid=f"keep:{app.package}",
                            image=self.checkbox_on if app.whitelisted else self.checkbox_off,
                            values=(app.name, app.package),
                        )
                    self._update_counts()
                    self._update_empty_states()
                elif kind == "scan_listed":
                    total, cached, unresolved = event[1:]
                    if unresolved:
                        self.status_var.set(
                            f"已列出 {total} 个应用，正在补全 {unresolved} 个应用名称…"
                        )
                    else:
                        self.status_var.set(f"已从缓存快速载入 {total} 个应用")
                elif kind == "name_updated":
                    package, name = event[1:]
                    for app in self.apps:
                        if app.package == package:
                            app.name = name
                            break
                    self._refresh_trees()
                elif kind == "scan_progress":
                    completed, total = event[1:]
                    self.progress.configure(value=completed)
                    self.status_var.set(f"正在补全应用名称：{completed}/{total}")
                elif kind == "scan_done":
                    total, cached, resolved = event[1:]
                    self.save_keep_list(quiet=True)
                    self.status_var.set(
                        f"扫描完成：共 {total} 个应用，缓存命中 {cached} 个，本次更新 {resolved} 个"
                    )
                    self._write_log(f"[扫描完成] 共 {len(self.apps)} 个用户安装的应用")
                    self._schedule_progress_hide()
                elif kind == "online_progress":
                    completed, total = event[1:]
                    self.progress.configure(value=completed)
                    self.status_var.set(f"正在联网补全应用名称：{completed}/{total}")
                elif kind == "online_done":
                    stats = self._apply_online_results(event[1])
                    self.save_keep_list(quiet=True)
                    self._refresh_trees()
                    summary, details = self._online_summary(stats)
                    self.status_var.set(f"联网补全完成：{summary}")
                    self._write_log(f"[联网补全完成] {summary}{details}")
                    self._schedule_progress_hide()
                    if stats["failed"]:
                        messagebox.showwarning("联网补全完成", summary + details)
                    else:
                        messagebox.showinfo("联网补全完成", summary)
                elif kind == "network_test_done":
                    play, fdroid = event[1:]
                    labels = {
                        "success": "连接正常",
                        "not_found": "可访问",
                        "timeout": "请求超时",
                        "dns_error": "DNS 解析失败",
                        "proxy_error": "代理连接失败",
                        "connection_error": "网络连接失败",
                        "tls_error": "安全连接失败",
                        "http_error": "服务器返回错误",
                        "parse_error": "页面格式异常",
                    }
                    play_text = labels.get(play.status, "连接失败")
                    fdroid_text = labels.get(fdroid.status, "连接失败")
                    self.status_var.set(
                        f"连接测试：Google Play {play_text}，F-Droid {fdroid_text}"
                    )
                    messagebox.showinfo(
                        "连接测试完成",
                        f"Google Play：{play_text}\nF-Droid：{fdroid_text}\n\n"
                        "浏览器扩展代理不会自动应用到本程序；如有需要，请填写代理地址。",
                    )
                elif kind == "batch_item":
                    app, ok, output, index, total = event[1:]
                    self.progress.configure(value=index)
                    self.status_var.set(f"正在卸载：{index}/{total}  {app.name}")
                    self._write_log(f"[{'成功' if ok else '失败'}] {app.name} ({app.package}): {output}")
                elif kind == "batch_done":
                    results, mode = event[1:]
                    succeeded = {app.package for app, ok, _ in results if ok}
                    failed = [(app, output) for app, ok, output in results if not ok]
                    self.apps = [app for app in self.apps if app.package not in succeeded]
                    self.uninstall_checked.difference_update(succeeded)
                    self.keep_checked.difference_update(succeeded)
                    self.save_keep_list(quiet=True)
                    action = "清理" if mode == "cleanup" else "卸载"
                    self.status_var.set(f"{action}完成：成功 {len(succeeded)}，失败 {len(failed)}")
                    self._schedule_progress_hide()
                    if failed:
                        messagebox.showwarning(
                            f"{action}完成",
                            f"成功 {len(succeeded)} 个，失败 {len(failed)} 个。\n失败详情请查看底部日志。",
                        )
                    else:
                        messagebox.showinfo(f"{action}完成", f"已成功卸载 {len(succeeded)} 个应用。")
                elif kind == "log":
                    self._write_log(event[1])
                elif kind == "error":
                    self._hide_progress()
                    self.status_var.set(event[1])
                    self._write_log(f"[错误] {event[1]}: {event[2]}")
                    messagebox.showerror(event[1], event[2])
                elif kind == "idle":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)


if __name__ == "__main__":
    CleanerApp().mainloop()
