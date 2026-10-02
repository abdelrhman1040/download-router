"""Download Router (Python side).

The Chrome extension asks this app, the moment a download starts, where the
file should be saved. This app shows a popup, then moves the file when the
download finishes.

Run:
    download_router.exe / pythonw download_router.py
        normal start: server + popups, and the settings window opens.
        If another copy is already running it is replaced by this one.
    ... --background   same, but silent (use this in the Windows Startup shortcut)
    ... --settings     settings window only (no server)
"""
import argparse
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import urllib.request
import webbrowser
import tkinter as tk
import tkinter.font as tkfont
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

APP_NAME = "Download Router"
APP_VERSION = "1.0"
APP_AUTHOR = "Abdelrahman Alaa"

# PyInstaller --noconsole leaves sys.stdout/sys.stderr as None; make them safe.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# ===========================================================================
# 1. Config
# ===========================================================================

PORT = 5959
MAX_RECENT = 5
CONFIG_PATH = Path.home() / ".download_router.json"
LOG_PATH = Path.home() / ".download_router.log"

FIELDS = ["site", "download_url", "page_url", "file_name", "file_type"]
FIELD_LABELS = {
    "site": "Site",
    "download_url": "Download URL",
    "page_url": "Page URL",
    "file_name": "File name",
    "file_type": "File type",
}
FIELD_TOOLTIPS = {
    "Site": "Domain of the download link or webpage.",
    "Download URL": "Direct link of the file.",
    "Page URL": "Address of the webpage where the download started.",
    "File name": "Name of the downloaded file.",
    "File type": "File extension (e.g., .pdf, .zip)."
}
LABEL_TO_FIELD = {label: key for key, label in FIELD_LABELS.items()}
OPS = ["contains", "equals", "starts with", "ends with"]

_config_lock = threading.Lock()


def log_error():
    try:
        with open(str(LOG_PATH), "a", encoding="utf-8") as f:
            f.write("[%s] ERROR:\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), traceback.format_exc()))
    except Exception:
        pass


def log_info(msg):
    try:
        with open(str(LOG_PATH), "a", encoding="utf-8") as f:
            f.write("[%s] INFO: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _default_config():
    return {"version": 2, "theme": "dark", "recent": [], "shortcuts": []}


def _normalize_condition(c):
    if not isinstance(c, dict) or c.get("field") not in FIELDS:
        return None
    field = c["field"]
    if field == "file_type":
        op = "is"
    else:
        op = c.get("op") if c.get("op") in OPS else "contains"
    return {"field": field, "op": op, "value": str(c.get("value", "")).strip()}


def _normalize_shortcut(s):
    if not isinstance(s, dict):
        return None
    name = str(s.get("name", "")).strip()
    folder = str(s.get("folder", "")).strip()
    if not name or not folder:
        return None
    try:
        priority = int(s.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    conds = [_normalize_condition(c) for c in (s.get("conditions") or [])]
    routes = []
    for r in (s.get("routes") or []):
        if isinstance(r, dict):
            value = str(r.get("value", "")).strip()
            rfolder = str(r.get("folder", "")).strip()
            if value and rfolder:
                routes.append({"value": value, "folder": rfolder})
    return {
        "id": str(s.get("id") or uuid.uuid4().hex),
        "name": name,
        "folder": folder,
        "priority": priority,
        "exclusive": bool(s.get("exclusive", False)),
        "auto": bool(s.get("auto", False)),
        "pinned": bool(s.get("pinned", False)),
        "open_after": bool(s.get("open_after", False)),
        "routes": routes,
        "conditions": [c for c in conds if c],
        "formula": str(s.get("formula") or "").strip(),
    }


def _normalize_config(raw):
    cfg = _default_config()
    if isinstance(raw, list):  
        cfg["recent"] = [p for p in raw if isinstance(p, str)][:MAX_RECENT]
        return cfg
    if not isinstance(raw, dict):
        return cfg
    if raw.get("theme") in ("dark", "light"):
        cfg["theme"] = raw["theme"]
    if isinstance(raw.get("recent"), list):
        cfg["recent"] = [p for p in raw["recent"] if isinstance(p, str)][:MAX_RECENT]
    if isinstance(raw.get("shortcuts"), list):
        for s in raw["shortcuts"]:
            sc = _normalize_shortcut(s)
            if sc:
                cfg["shortcuts"].append(sc)
    return cfg


def load_config():
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_config()
    except Exception:
        try:
            shutil.copy2(str(CONFIG_PATH), str(CONFIG_PATH) + ".bak")
        except Exception:
            pass
        return _default_config()
    return _normalize_config(raw)


def save_config(cfg):
    try:
        tmp = CONFIG_PATH.with_name("%s.%d.tmp" % (CONFIG_PATH.name, os.getpid()))
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(CONFIG_PATH))
    except Exception:
        log_error()


def update_config(mutator):
    with _config_lock:
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
    return cfg


def remember_recent(folder):
    def mutate(cfg):
        cfg["recent"] = ([folder] + [p for p in cfg["recent"] if p != folder])[:MAX_RECENT]
    update_config(mutate)


# ===========================================================================
# 2. Conditions engine
# ===========================================================================

class FormulaError(Exception):
    pass

def host_of(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host

def build_context(data):
    filename = str(data.get("filename") or "")
    url = str(data.get("url") or "")
    page = str(data.get("page") or data.get("referrer") or "")
    return {
        "file_name": filename,
        "download_url": url,
        "page_url": page,
        "ext": os.path.splitext(filename)[1].lower(),
        "hosts": [host_of(url), host_of(page)],
    }

def parse_extensions(value):
    exts = []
    for part in value.split(","):
        part = part.strip().lower()
        if part:
            exts.append(part if part.startswith(".") else "." + part)
    return exts

def _compare(op, actual, needle):
    actual = actual.lower()
    needle = needle.strip().lower()
    if op == "contains":
        return needle in actual
    if op == "equals":
        return actual == needle
    if op == "starts with":
        return actual.startswith(needle)
    if op == "ends with":
        return actual.endswith(needle)
    return False

def eval_condition(cond, ctx):
    value = cond["value"].strip()
    if not value:
        return False
    field = cond["field"]
    if field == "file_type":
        return ctx["ext"] in parse_extensions(value)
    if field == "site":  
        return any(h and _compare(cond["op"], h, value) for h in ctx["hosts"])
    actual = ctx.get(field, "")  
    return bool(actual) and _compare(cond["op"], actual, value)

def _tokenize(text):
    tokens = []
    pos = 0
    while pos < len(text):
        ch = text[pos]
        if ch.isspace():
            pos += 1
        elif ch.isdecimal():
            end = pos
            while end < len(text) and text[end].isdecimal():
                end += 1
            tokens.append(("num", int(text[pos:end])))
            pos = end
        elif ch in "()+*":
            tokens.append((ch, ch))
            pos += 1
        else:
            raise FormulaError("Unexpected character '%s'" % ch)
    return tokens

def parse_formula(text, n_conditions):
    tokens = _tokenize(text)
    if not tokens:
        raise FormulaError("Formula is empty")
    pos = [0]
    used = set()

    def peek():
        return tokens[pos[0]][0] if pos[0] < len(tokens) else None

    def take():
        tok = tokens[pos[0]]
        pos[0] += 1
        return tok

    def expr():
        terms = [term()]
        while peek() == "+":
            take()
            terms.append(term())
        return terms[0] if len(terms) == 1 else ("or", terms)

    def term():
        factors = [factor()]
        while peek() == "*":
            take()
            factors.append(factor())
        return factors[0] if len(factors) == 1 else ("and", factors)

    def factor():
        kind = peek()
        if kind == "num":
            number = take()[1]
            if number < 1 or number > n_conditions:
                raise FormulaError("Condition %d does not exist" % number)
            used.add(number)
            return ("c", number - 1)
        if kind == "(":
            take()
            if peek() == ")":
                raise FormulaError("Empty parentheses")
            node = expr()
            if peek() != ")":
                raise FormulaError("Missing closing parenthesis")
            take()
            return node
        if kind is None:
            raise FormulaError("Formula ends unexpectedly")
        if kind == ")":
            raise FormulaError("Unmatched ')'")
        raise FormulaError("Unexpected '%s'" % kind)

    tree = expr()
    if pos[0] != len(tokens):
        if peek() == ")":
            raise FormulaError("Unmatched ')'")
        raise FormulaError("Missing '+' or '*' between conditions")
    return tree, used

def eval_tree(node, results):
    kind = node[0]
    if kind == "c":
        return results[node[1]]
    if kind == "and":
        return all(eval_tree(n, results) for n in node[1])
    return any(eval_tree(n, results) for n in node[1])

def shortcut_matches(sc, ctx):
    conds = sc["conditions"]
    if not conds:  
        return False
    results = [eval_condition(c, ctx) for c in conds]
    formula = sc.get("formula", "").strip()
    if not formula:
        return all(results)
    try:
        tree, _ = parse_formula(formula, len(conds))
    except FormulaError:
        return False
    return eval_tree(tree, results)

def _rank_key(sc):
    return (-sc["priority"], -len(sc["conditions"]), sc["name"].lower())

def compute_suggestions(shortcuts, ctx):
    matched = sorted((s for s in shortcuts if shortcut_matches(s, ctx)), key=_rank_key)
    suggested = []
    for sc in matched:
        suggested.append(sc)
        if sc["exclusive"]:
            break
    return suggested


def resolve_folder(sc, ctx):
    """Folder for this download: first matching sub-folder rule, else the main folder."""
    name = ctx.get("file_name", "").lower()
    for r in sc.get("routes", []):
        words = [w.strip().lower() for w in r["value"].split(",") if w.strip()]
        if any(w in name for w in words):
            return os.path.normpath(os.path.join(sc["folder"], r["folder"]))
    return sc["folder"]


def folder_for(sc, ctx):
    """Always apply sub-folder rules based on filename, even on manual click."""
    return resolve_folder(sc, ctx)


def wants_open(shortcuts, ctx, dest):
    """True if a shortcut with 'open folder after download' points at dest."""
    norm = lambda path: os.path.normcase(os.path.normpath(path))
    return any(sc.get("open_after") and norm(folder_for(sc, ctx)) == norm(dest) for sc in shortcuts)


# ===========================================================================
# 3. Local server (talks to the Chrome extension)
# ===========================================================================

requests_q = queue.Queue()  
pending = {}                

def move_file(src, dest_dir):
    try:
        if not src.is_file():
            return
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest_dir.resolve() == src.parent.resolve():
            return src
        dest = dest_dir / src.name
        if dest.exists():
            dest = dest.with_name("%s_%d%s" % (src.stem, int(time.time()), src.suffix))
        shutil.move(str(src), str(dest))
        return dest
    except Exception:
        log_error()
        return None


def open_folder(file_path):
    """Open the folder that holds file_path (selecting the file on Windows)."""
    try:
        file_path = os.path.normpath(str(file_path))
        if sys.platform == "win32":
            subprocess.Popen('explorer /select,"%s"' % file_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", file_path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(file_path)])
    except Exception:
        log_error()

class Server(ThreadingHTTPServer):
    """HTTP server that really fails when the port is taken.

    On Windows, SO_REUSEADDR (the default for HTTPServer) lets a second copy
    bind the same port without any error, so "is it already running?" never
    triggered. SO_EXCLUSIVEADDRUSE fixes that.
    """
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _allowed(self):
        return self.headers.get("Origin", "").startswith("chrome-extension://")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        if not self._allowed():
            return self.send_error(403)
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if not self._allowed():
            return self.send_error(403)
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self.send_error(400)
        try:
            if self.path == "/ask":
                result = {"dest": self._ask(data)}
            elif self.path == "/done":
                self._done(data)
                result = {"ok": True}
            elif self.path == "/quit":
                # Special endpoint to kill the existing background process
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                import os
                os._exit(0)  # Instantly terminates the old instance
                return
            else:
                return self.send_error(404)
        except Exception:
            log_error()
            result = {"dest": None} if self.path == "/ask" else {"ok": False}
            
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ask(self, data):
        reply = queue.Queue()
        requests_q.put((data, reply))
        try:
            dest, open_after = reply.get(timeout=3600)  
        except queue.Empty:
            dest, open_after = None, False
        if dest:
            pending[data.get("id")] = (dest, open_after)
        return dest

    def _done(self, data):
        entry = pending.pop(data.get("id"), None)
        if entry and data.get("path"):
            dest, open_after = entry
            final = move_file(Path(data["path"]), dest)
            if final and open_after:
                open_folder(final)


# ===========================================================================
# 4. Theme (dark / light) for plain tkinter + ttk
# ===========================================================================

PALETTES = {
    "dark": {
        "bg": "#1e1f22", "surface": "#2b2d31", "text": "#e8eaed", "muted": "#9aa0a6",
        "accent": "#4f8ef7", "accent_hover": "#6aa0f8", "on_accent": "#ffffff",
        "border": "#3c4043", "hover": "#383a40", "pressed": "#43464d", "error": "#f28b82",
    },
    "light": {
        "bg": "#f5f6f8", "surface": "#ffffff", "text": "#1f2328", "muted": "#656d76",
        "accent": "#2563eb", "accent_hover": "#1d4fd8", "on_accent": "#ffffff",
        "border": "#d0d7de", "hover": "#eaeef2", "pressed": "#dde3ea", "error": "#cf222e",
    },
}

def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

def set_titlebar_dark(win, dark):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass

class Theme:
    def __init__(self, root, mode):
        self.root = root
        family = "Segoe UI" if sys.platform == "win32" else tkfont.nametofont("TkDefaultFont").actual("family")
        self.font = (family, 10)
        self.font_bold = (family, 10, "bold")
        self.font_title = (family, 12, "bold")
        self.font_small = (family, 9)
        self.mode = mode if mode in PALETTES else "dark"
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.windows = []  
        self.combos = []
        self.apply_style()

    @property
    def p(self):
        return PALETTES[self.mode]

    def icon(self):
        return "\u2600" if self.mode == "dark" else "\u263D"

    def register(self, win, callback=None):
        self.windows.append((win, callback))
        self._paint_window(win)

    def unregister(self, win):
        self.windows = [(w, cb) for (w, cb) in self.windows if w is not win]

    def _paint_window(self, win):
        try:
            win.configure(bg=self.p["bg"])
            set_titlebar_dark(win, self.mode == "dark")
        except tk.TclError:
            pass

    def toggle(self):
        self.mode = "light" if self.mode == "dark" else "dark"
        self.apply_style()
        mode = self.mode
        update_config(lambda cfg: cfg.update({"theme": mode}))
        for win, callback in list(self.windows):
            try:
                if not win.winfo_exists():
                    continue
                self._paint_window(win)
                if callback:
                    callback()
            except tk.TclError:
                pass
        self._restyle_combos()

    def style_combo(self, combo):
        self.combos.append(combo)
        self._style_popdown(combo)

    def _style_popdown(self, combo):
        p = self.p
        try:
            popdown = combo.tk.eval("ttk::combobox::PopdownWindow %s" % combo)
            combo.tk.call(
                "%s.f.l" % popdown, "configure",
                "-background", p["surface"], "-foreground", p["text"],
                "-selectbackground", p["accent"], "-selectforeground", p["on_accent"],
                "-borderwidth", 0, "-highlightthickness", 0)
        except tk.TclError:
            pass

    def _restyle_combos(self):
        alive = []
        for combo in self.combos:
            try:
                if combo.winfo_exists():
                    alive.append(combo)
                    self._style_popdown(combo)
            except tk.TclError:
                pass
        self.combos = alive

    def apply_style(self):
        p, s = self.p, self.style
        self.root.configure(bg=p["bg"])
        s.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["surface"],
                    bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                    troughcolor=p["bg"], focuscolor=p["surface"], font=self.font)
        s.configure("TFrame", background=p["bg"])
        s.configure("TLabel", background=p["bg"], foreground=p["text"])
        s.configure("Muted.TLabel", foreground=p["muted"], font=self.font_small)
        s.configure("Title.TLabel", font=self.font_title)
        s.configure("Section.TLabel", foreground=p["muted"], font=self.font_bold)
        s.configure("Error.TLabel", foreground=p["error"], font=self.font_small)
        s.configure("TSeparator", background=p["border"])

        s.configure("TButton", background=p["surface"], foreground=p["text"],
                    padding=(12, 6), relief="flat", borderwidth=1, focuscolor=p["surface"])
        s.map("TButton",
              background=[("disabled", p["bg"]), ("pressed", p["pressed"]), ("active", p["hover"])],
              foreground=[("disabled", p["muted"])])
        s.configure("Accent.TButton", background=p["accent"], foreground=p["on_accent"],
                    bordercolor=p["accent"], lightcolor=p["accent"], darkcolor=p["accent"],
                    focuscolor=p["accent"])
        s.map("Accent.TButton",
              background=[("disabled", p["border"]), ("pressed", p["accent_hover"]),
                          ("active", p["accent_hover"])],
              foreground=[("disabled", p["muted"])])
        
        icon_font = ("Segoe UI Symbol", 16) if sys.platform == "win32" else (self.font_title[0], 16)
        s.configure("Icon.TButton", padding=(6, 4), font=icon_font)

        s.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"],
                    insertcolor=p["text"], padding=5)
        s.map("TEntry",
              fieldbackground=[("disabled", p["bg"])],
              foreground=[("disabled", p["muted"])],
              bordercolor=[("focus", p["accent"])],
              lightcolor=[("focus", p["accent"])],
              darkcolor=[("focus", p["accent"])])

        s.configure("TCombobox", fieldbackground=p["surface"], background=p["surface"],
                    foreground=p["text"], arrowcolor=p["text"], insertcolor=p["text"], padding=4)
        s.map("TCombobox",
              fieldbackground=[("disabled", p["bg"]), ("readonly", p["surface"])],
              foreground=[("disabled", p["muted"]), ("readonly", p["text"])],
              selectbackground=[("readonly", p["surface"])],
              selectforeground=[("readonly", p["text"])],
              background=[("pressed", p["pressed"]), ("active", p["hover"])],
              bordercolor=[("focus", p["accent"])])

        s.configure("TSpinbox", fieldbackground=p["surface"], background=p["surface"],
                    foreground=p["text"], arrowcolor=p["text"], insertcolor=p["text"], padding=4)
        s.map("TSpinbox", background=[("pressed", p["pressed"]), ("active", p["hover"])],
              bordercolor=[("focus", p["accent"])])

        s.configure("TCheckbutton", background=p["bg"], foreground=p["text"],
                    indicatorbackground=p["surface"], indicatorforeground=p["accent"],
                    upperbordercolor=p["border"], lowerbordercolor=p["border"])
        s.map("TCheckbutton",
              background=[("active", p["bg"])],
              indicatorbackground=[("pressed", p["pressed"]), ("disabled", p["bg"])])

        s.configure("Vertical.TScrollbar", background=p["border"], troughcolor=p["bg"],
                    bordercolor=p["bg"], arrowcolor=p["text"],
                    lightcolor=p["border"], darkcolor=p["border"])
        s.map("Vertical.TScrollbar", background=[("active", p["muted"])])


# ===========================================================================
# 5. Shared UI pieces
# ===========================================================================

class ScrollFrame(ttk.Frame):
    def __init__(self, parent, theme, max_height=None, width=None):
        ttk.Frame.__init__(self, parent)
        self.max_height = max_height
        self._vsb_visible = False
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=theme.p["bg"], yscrollincrement=20)
        if width:
            self.canvas.configure(width=width)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._update)
        self.canvas.bind("<Configure>", self._on_canvas)
        top = self.winfo_toplevel()
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            top.bind(sequence, self._on_wheel, add="+")

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        self._update()

    def _update(self, _event=None):
        inner_h = self.inner.winfo_reqheight()
        self.canvas.configure(scrollregion=(0, 0, max(self.canvas.winfo_width(), 1), inner_h))
        if self.max_height:
            view_h = min(inner_h, self.max_height)
            self.canvas.configure(height=view_h)
        else:
            view_h = self.canvas.winfo_height()
        need = inner_h > view_h + 1
        if need and not self._vsb_visible:
            self.vsb.pack(side="right", fill="y")
            self._vsb_visible = True
        elif not need and self._vsb_visible:
            self.vsb.pack_forget()
            self._vsb_visible = False

    def _on_wheel(self, event):
        try:
            if not self.winfo_exists():
                return
            target = self.winfo_containing(event.x_root, event.y_root)
            path = str(target)
            if target is None or not (path == str(self) or path.startswith(str(self) + ".")):
                return
            if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
                return
            num = getattr(event, "num", None)
            if num == 4:
                step = -1
            elif num == 5:
                step = 1
            else:
                step = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(step, "units")
        except (tk.TclError, KeyError):
            pass


class FlowFrame(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.bind("<Configure>", self._on_resize)
        
    def _on_resize(self, event=None):
        width = self.winfo_width()
        if width <= 1: 
            return
        x = y = max_h = 0
        for child in self.winfo_children():
            cw = child.winfo_reqwidth()
            ch = child.winfo_reqheight()
            if x + cw > width and x > 0:
                x = 0
                y += max_h + 6
                max_h = 0
            child.place(x=x, y=y)
            x += cw + 6
            max_h = max(max_h, ch)
        self.configure(height=y + max_h)


class Tooltip:
    def __init__(self, widget, text_or_func, theme):
        self.widget = widget
        self.text_or_func = text_or_func
        self.theme = theme
        self.id = None
        self.tw = None
        self.x = 0
        self.y = 0
        self.widget.bind("<Enter>", self.schedule, add="+")
        self.widget.bind("<Leave>", self.hide, add="+")
        self.widget.bind("<Motion>", self.motion, add="+")

    def motion(self, event):
        self.x = event.x_root
        self.y = event.y_root

    def schedule(self, event=None):
        self.unschedule()
        self.id = self.widget.after(500, self.show)

    def unschedule(self):
        if self.id:
            self.widget.after_cancel(self.id)
            self.id = None

    def show(self):
        self.unschedule()
        if self.tw:
            return
            
        text = self.text_or_func() if callable(self.text_or_func) else self.text_or_func
        if not text:
            return
            
        x = self.x + 10
        y = self.y + 10
        self.tw = tk.Toplevel(self.widget)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{x}+{y}")
        self.tw.attributes("-topmost", True)
        
        label = tk.Label(self.tw, text=text, justify=tk.LEFT,
                         background=self.theme.p["surface"],
                         foreground=self.theme.p["text"],
                         relief=tk.SOLID, borderwidth=1,
                         font=self.theme.font_small)
        label.pack(ipadx=4, ipady=2)

    def hide(self, event=None):
        self.unschedule()
        if self.tw:
            self.tw.destroy()
            self.tw = None


# ===========================================================================
# 6. Download popup
# ===========================================================================

class DownloadPopup:
    WIDTH = 452

    def __init__(self, root, theme, data):
        self.root = root
        self.theme = theme
        self.data = data
        self.ctx = build_context(data)
        self.result = None
        self.show_all = False
        self.win = tk.Toplevel(root)
        self.win.title("Save download to\u2026")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        self.win.bind("<Escape>", lambda e: self.cancel())
        self.reload()
        theme.register(self.win, self.render)
        self._center()
        self.win.lift()
        self.win.focus_force()

    def reload(self):
        if not self.win.winfo_exists():
            return
        self.cfg = load_config()
        self.suggested = compute_suggestions(self.cfg["shortcuts"], self.ctx)
        self.render()

    def _center(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        x = max((self.win.winfo_screenwidth() - w) // 2, 0)
        y = max((self.win.winfo_screenheight() - h) // 3, 0)
        self.win.geometry("+%d+%d" % (x, y))

    def choose(self, folder):
        self.result = folder
        self._close()

    def cancel(self):
        self.result = None
        self._close()

    def _close(self):
        self.theme.unregister(self.win)
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def browse(self):
        recent = self.cfg["recent"]
        initial = recent[0] if recent and os.path.isdir(recent[0]) else str(Path.home())
        folder = filedialog.askdirectory(parent=self.win, initialdir=initial, title="Choose a folder")
        if folder:
            self.choose(os.path.normpath(folder))

    def toggle_all(self):
        self.show_all = not self.show_all
        self.render()

    def open_settings(self):
        SettingsWindow(self.root, self.theme, on_close=self.reload)

    def render(self):
        t = self.theme
        for child in self.win.winfo_children():
            child.destroy()
        self.win.configure(bg=t.p["bg"])
        outer = ttk.Frame(self.win, padding=14)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        buttons = ttk.Frame(head)
        buttons.pack(side="right", anchor="n")
        
        settings_btn = ttk.Button(buttons, text="\u26ED", style="Icon.TButton", width=3,
                                  command=self.open_settings)
        settings_btn.pack(side="left", padx=(0, 4))
        Tooltip(settings_btn, "Open settings", t)

        theme_btn = ttk.Button(buttons, text=t.icon(), style="Icon.TButton", width=3,
                               command=t.toggle)
        theme_btn.pack(side="left")
        Tooltip(theme_btn, "Toggle dark/light theme", t)
        
        info = ttk.Frame(head)
        info.pack(side="left", fill="x", expand=True)
        ttk.Label(info, text=self.ctx["file_name"] or "(unnamed file)", style="Title.TLabel",
                  wraplength=330).pack(anchor="w")
        host = self.ctx["hosts"][0] or self.ctx["hosts"][1] or "unknown source"
        meta = "%s  \u2022  %s" % (host, self.ctx["ext"] or "no extension")
        ttk.Label(info, text=meta, style="Muted.TLabel").pack(anchor="w")

        scroll = ScrollFrame(outer, t, max_height=320, width=self.WIDTH)
        scroll.pack(fill="x", pady=(8, 0))
        body = scroll.inner
        shown = False
        
        pinned_all = sorted([s for s in self.cfg["shortcuts"] if s.get("pinned")], key=lambda x: x["name"].lower())
        best_match_id = self.suggested[0]["id"] if self.suggested else None
        
        if pinned_all:
            shown = True
            self._section(body, "Pinned")
            flow = FlowFrame(body, bg=t.p["bg"])
            flow.pack(fill="x", pady=(0, 4))
            
            for p_sc in pinned_all:
                is_best = (p_sc["id"] == best_match_id)
                border = t.p["accent"] if is_best else t.p["border"]
                
                card = tk.Frame(flow, bg=t.p["surface"], highlightthickness=1,
                                highlightbackground=border, highlightcolor=border, cursor="hand2")
                
                name_str = p_sc["name"]
                if len(name_str) > 20: 
                    name_str = name_str[:20] + "..."
                
                lbl = tk.Label(card, text=name_str, bg=t.p["surface"], fg=t.p["text"],
                               font=t.font_bold, cursor="hand2")
                lbl.pack(padx=8, pady=6)
                
                def make_paint(c_w, l_w, f_path):
                    def paint_func(color):
                        c_w.configure(bg=color)
                        l_w.configure(bg=color)
                    def on_enter(e):
                        paint_func(t.p["hover"])
                    def on_leave(e):
                        paint_func(t.p["surface"])
                    def on_click(e):
                        self.choose(f_path)
                    
                    c_w.bind("<Button-1>", on_click)
                    l_w.bind("<Button-1>", on_click)
                    c_w.bind("<Enter>", on_enter)
                    c_w.bind("<Leave>", on_leave)
                    l_w.bind("<Enter>", on_enter)
                    l_w.bind("<Leave>", on_leave)
                    
                    tt = Tooltip(c_w, f_path, t)
                    l_w.bind("<Enter>", tt.schedule, add="+")
                    l_w.bind("<Leave>", tt.hide, add="+")
                    l_w.bind("<Motion>", tt.motion, add="+")

                make_paint(card, lbl, folder_for(p_sc, self.ctx))
            
            flow.update_idletasks()
            flow._on_resize()

        suggested_unpinned = [s for s in self.suggested if not s.get("pinned")]
        if suggested_unpinned:
            shown = True
            self._section(body, "Suggested")
            for sc in suggested_unpinned:
                is_best = (sc["id"] == best_match_id)
                dest = resolve_folder(sc, self.ctx)
                sub = dest if not is_best else "Best match  \u2022  " + dest
                self._item(body, sc["name"], sub, dest, best=is_best)
                
        if self.cfg["recent"]:
            shown = True
            self._section(body, "Recent folders")
            for folder in self.cfg["recent"]:
                self._item(body, Path(folder).name or folder, folder, folder)
                
        if self.show_all:
            shown = True
            self._section(body, "All shortcuts")
            everything = sorted(self.cfg["shortcuts"], key=lambda s: s["name"].lower())
            if not everything:
                ttk.Label(body, text="No shortcuts yet. Open settings (\u2699) to add one.",
                          style="Muted.TLabel").pack(anchor="w", pady=4)
            for sc in everything:
                dest = resolve_folder(sc, self.ctx)
                self._item(body, sc["name"], dest, dest)
                
        if not shown:
            ttk.Label(body, text="No suggestions. Use Browse\u2026 to pick a folder.",
                      style="Muted.TLabel").pack(anchor="w", pady=8)

        foot = ttk.Frame(outer)
        foot.pack(fill="x", pady=(10, 0))
        for col in range(3):
            foot.columnconfigure(col, weight=1, uniform="footer")
            
        browse_btn = ttk.Button(foot, text="Browse\u2026", command=self.browse)
        browse_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        Tooltip(browse_btn, "Choose a folder manually", t)
        
        all_btn = ttk.Button(foot, text="Hide shortcuts" if self.show_all else "All shortcuts",
                             command=self.toggle_all)
        all_btn.grid(row=0, column=1, sticky="ew", padx=3)
        Tooltip(all_btn, "Show or hide all shortcuts", t)
        
        keep_btn = ttk.Button(foot, text="Keep in Downloads", command=self.cancel)
        keep_btn.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        Tooltip(keep_btn, "Keep file in the default Downloads folder", t)
        
        set_titlebar_dark(self.win, t.mode == "dark")

    def _section(self, parent, text):
        ttk.Label(parent, text=text, style="Section.TLabel").pack(anchor="w", pady=(8, 2))

    def _item(self, parent, title, subtitle, folder, best=False):
        p = self.theme.p
        border = p["accent"] if best else p["border"]
        frame = tk.Frame(parent, bg=p["surface"], highlightthickness=1,
                         highlightbackground=border, highlightcolor=border, cursor="hand2")
        frame.pack(fill="x", pady=3)
        name = tk.Label(frame, text=title, bg=p["surface"], fg=p["text"],
                        font=self.theme.font_bold, anchor="w", cursor="hand2")
        name.pack(fill="x", padx=10, pady=(7, 0))
        path = tk.Label(frame, text=subtitle, bg=p["surface"], fg=p["muted"],
                        font=self.theme.font_small, anchor="w", justify="left",
                        wraplength=self.WIDTH - 40, cursor="hand2")
        path.pack(fill="x", padx=10, pady=(0, 7))
        widgets = (frame, name, path)

        def paint(color):
            for w in widgets:
                w.configure(bg=color)

        for w in widgets:
            w.bind("<Button-1>", lambda e, f=folder: self.choose(f))
            w.bind("<Enter>", lambda e: paint(p["hover"]))
            w.bind("<Leave>", lambda e: paint(p["surface"]))


def ask_folder(root, theme, data):
    popup = DownloadPopup(root, theme, data)
    root.wait_window(popup.win)
    return popup.result


# ===========================================================================
# 7. Settings & Export/Import Windows
# ===========================================================================

class ExportWindow:
    def __init__(self, owner):
        self.owner = owner
        self.theme = owner.theme
        self.win = tk.Toplevel(owner.win)
        self.win.title("Export Shortcuts")
        self.win.geometry("450x550")
        self.win.transient(owner.win)
        self.win.grab_set()
        self.theme.register(self.win)

        self.shortcuts = load_config().get("shortcuts", [])
        self.vars = {}

        body = ttk.Frame(self.win, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Select shortcuts to export:", style="Title.TLabel").pack(anchor="w", pady=(0, 10))

        scroll = ScrollFrame(body, self.theme)
        scroll.pack(fill="both", expand=True)

        if not self.shortcuts:
            ttk.Label(scroll.inner, text="No shortcuts available to export.", style="Muted.TLabel").pack(anchor="w", pady=4)
        else:
            for sc in self.shortcuts:
                var = tk.BooleanVar(value=True)
                self.vars[sc["id"]] = var
                cb = ttk.Checkbutton(scroll.inner, text=sc["name"], variable=var)
                cb.pack(anchor="w", pady=4, padx=4)

        foot = ttk.Frame(body)
        foot.pack(fill="x", side="bottom", pady=(10, 0))

        ttk.Button(foot, text="Cancel", command=self.close).pack(side="right", padx=(8, 0))
        ttk.Button(foot, text="Export Selected", style="Accent.TButton", command=self.do_export).pack(side="right")

        self.win.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        self.theme.unregister(self.win)
        self.win.destroy()

    def do_export(self):
        selected = [sc for sc in self.shortcuts if self.vars.get(sc["id"]) and self.vars[sc["id"]].get()]
        if not selected:
            messagebox.showwarning("Export", "No shortcuts selected.", parent=self.win)
            return

        path = filedialog.asksaveasfilename(
            parent=self.win,
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")],
            title="Save Shortcuts"
        )
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(selected, f, ensure_ascii=False, indent=2)
                messagebox.showinfo("Success", f"Exported {len(selected)} shortcuts successfully.", parent=self.win)
                self.close()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save file:\n{e}", parent=self.win)


class ImportWindow:
    def __init__(self, owner, file_path):
        self.owner = owner
        self.theme = owner.theme
        self.win = tk.Toplevel(owner.win)
        self.win.title("Import Shortcuts")
        self.win.geometry("450x550")
        self.win.transient(owner.win)
        self.win.grab_set()
        self.theme.register(self.win)

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("File does not contain a list of shortcuts.")
            self.imported = [_normalize_shortcut(s) for s in data if _normalize_shortcut(s)]
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read file:\n{e}", parent=owner.win)
            self.close()
            return

        if not self.imported:
            messagebox.showinfo("Import", "No valid shortcuts found in the file.", parent=owner.win)
            self.close()
            return

        self.existing_config = load_config()
        self.existing_names = {s["name"].lower(): s for s in self.existing_config.get("shortcuts", [])}
        self.vars = {}

        body = ttk.Frame(self.win, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Select shortcuts to import:", style="Title.TLabel").pack(anchor="w", pady=(0, 10))

        scroll = ScrollFrame(body, self.theme)
        scroll.pack(fill="both", expand=True)

        for i, sc in enumerate(self.imported):
            var = tk.BooleanVar(value=True)
            self.vars[i] = var
            conflict = sc["name"].lower() in self.existing_names
            
            frame = ttk.Frame(scroll.inner)
            frame.pack(fill="x", pady=2, padx=4)
            
            cb = ttk.Checkbutton(frame, text=sc["name"], variable=var)
            cb.pack(side="left")
            
            if conflict:
                ttk.Label(frame, text=" (Will overwrite existing)", style="Error.TLabel").pack(side="left")

        foot = ttk.Frame(body)
        foot.pack(fill="x", side="bottom", pady=(10, 0))

        ttk.Button(foot, text="Cancel", command=self.close).pack(side="right", padx=(8, 0))
        ttk.Button(foot, text="Import Selected", style="Accent.TButton", command=self.do_import).pack(side="right")

        self.win.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        self.theme.unregister(self.win)
        self.win.destroy()

    def do_import(self):
        selected = [sc for i, sc in enumerate(self.imported) if self.vars[i].get()]
        if not selected:
            messagebox.showwarning("Import", "No shortcuts selected.", parent=self.win)
            return

        def mutate(cfg):
            for sc in selected:
                # Remove existing shortcut with the same name
                cfg["shortcuts"] = [s for s in cfg["shortcuts"] if s["name"].lower() != sc["name"].lower()]
                # Generate a new ID to avoid duplicates if exporting/importing same file
                sc["id"] = uuid.uuid4().hex 
                cfg["shortcuts"].append(sc)

        update_config(mutate)
        messagebox.showinfo("Success", f"Imported {len(selected)} shortcuts successfully.", parent=self.win)
        self.owner.show_list()
        self.close()


class SettingsWindow:
    def __init__(self, root, theme, on_close=None):
        self.root = root
        self.theme = theme
        self.on_close = on_close
        self.mode = "list"
        self.editor = None
        self.body = None
        self.win = tk.Toplevel(root)
        self.win.title("Download Router \u2014 Settings")
        self.win.geometry("640x720")
        self.win.minsize(580, 440)
        if on_close:  
            self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.show_list()
        theme.register(self.win, self.on_theme)
        self.win.lift()
        self.win.focus_force()

    def close(self):
        self.theme.unregister(self.win)
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close()

    def on_theme(self):
        if self.mode == "list":
            self.show_list()
        elif self.editor:
            self.editor.on_theme()

    def open_export(self):
        ExportWindow(self)

    def open_import(self):
        path = filedialog.askopenfilename(
            parent=self.win,
            title="Select Shortcuts File",
            filetypes=[("JSON Files", "*.json")]
        )
        if path:
            ImportWindow(self, path)

    def _fresh_body(self):
        if self.body is not None:
            self.body.destroy()
        self.body = ttk.Frame(self.win, padding=14)
        self.body.pack(fill="both", expand=True)
        return self.body

    def show_list(self):
        self.mode = "list"
        self.editor = None
        t = self.theme
        body = self._fresh_body()
        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="Shortcuts", style="Title.TLabel").pack(side="left")
        
        theme_btn = ttk.Button(head, text=t.icon(), style="Icon.TButton", width=3, command=t.toggle)
        theme_btn.pack(side="right")
        Tooltip(theme_btn, "Toggle dark/light theme", t)
        
        ttk.Label(body, text="Shortcuts appear in the download window when their conditions match.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 8))

        info_lbl = tk.Label(body, text="%s v%s  \u2022  by %s" % (APP_NAME, APP_VERSION, APP_AUTHOR),
                            fg=t.p["accent"], bg=t.p["bg"], cursor="hand2", font=t.font_small)
        info_lbl.pack(side="bottom", fill="x", pady=(5, 5))
        info_lbl.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/abdelrhman1040/download-router"))
        Tooltip(info_lbl, "Open project repository on GitHub", t)

        foot = ttk.Frame(body)
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        
        add_btn = ttk.Button(foot, text="Add shortcut", style="Accent.TButton",
                             command=lambda: self.show_editor(None))
        add_btn.pack(side="left")
        Tooltip(add_btn, "Create a new shortcut", t)

        export_btn = ttk.Button(foot, text="Export", command=self.open_export)
        export_btn.pack(side="left", padx=(8, 0))
        Tooltip(export_btn, "Export specific shortcuts to a file", t)

        import_btn = ttk.Button(foot, text="Import", command=self.open_import)
        import_btn.pack(side="left", padx=(4, 0))
        Tooltip(import_btn, "Import shortcuts from a file", t)

        ttk.Button(foot, text="Close", command=self.close).pack(side="right")

        scroll = ScrollFrame(body, t)
        scroll.pack(fill="both", expand=True)
        shortcuts = sorted(load_config()["shortcuts"], key=lambda s: s["name"].lower())
        if not shortcuts:
            ttk.Label(scroll.inner, text="No shortcuts yet. Click \u201cAdd shortcut\u201d to create one.",
                      style="Muted.TLabel").pack(anchor="w", pady=8)
        for sc in shortcuts:
            self._row(scroll.inner, sc)

    def _row(self, parent, sc):
        p = self.theme.p
        f = self.theme
        row = tk.Frame(parent, bg=p["surface"], highlightthickness=1, highlightbackground=p["border"])
        row.pack(fill="x", pady=3)
        buttons = tk.Frame(row, bg=p["surface"])
        buttons.pack(side="right", padx=8)
        
        edit_btn = ttk.Button(buttons, text="Edit", command=lambda s=sc: self.show_editor(s))
        edit_btn.pack(side="left", padx=2)
        Tooltip(edit_btn, "Edit this shortcut", f)
        
        dup_btn = ttk.Button(buttons, text="Duplicate", command=lambda s=sc: self.duplicate(s))
        dup_btn.pack(side="left", padx=2)
        Tooltip(dup_btn, "Create an editable copy of this shortcut", f)

        del_btn = ttk.Button(buttons, text="Delete", command=lambda s=sc: self.delete(s))
        del_btn.pack(side="left", padx=2)
        Tooltip(del_btn, "Delete this shortcut", f)
        
        info = tk.Frame(row, bg=p["surface"])
        info.pack(side="left", fill="x", expand=True, padx=10, pady=8)
        name_lbl = tk.Label(info, text=sc["name"], bg=p["surface"], fg=p["text"], font=f.font_bold,
                            anchor="w", justify="left")
        name_lbl.pack(fill="x")
        folder_lbl = tk.Label(info, text=sc["folder"], bg=p["surface"], fg=p["muted"], font=f.font_small,
                              anchor="w", justify="left", wraplength=360)
        folder_lbl.pack(fill="x")
        n = len(sc["conditions"])
        bits = ["Priority %d" % sc["priority"], "%d condition%s" % (n, "" if n == 1 else "s")]
        if sc["exclusive"]:
            bits.append("Exclusive")
        if sc.get("auto"):
            bits.append("Auto-save")
        if sc.get("pinned"):
            bits.append("Pinned")
        if sc.get("open_after"):
            bits.append("Opens folder")
        if sc.get("routes"):
            bits.append("%d sub-folder%s" % (len(sc["routes"]), "" if len(sc["routes"]) == 1 else "s"))
            
        bits_lbl = tk.Label(info, text="  \u2022  ".join(bits), bg=p["surface"], fg=p["muted"], font=f.font_small,
                            anchor="w", justify="left", wraplength=360)
        bits_lbl.pack(fill="x")
        info.bind("<Configure>", lambda e: [w.configure(wraplength=max(e.width - 6, 80)) for w in (name_lbl, folder_lbl, bits_lbl)])

    def delete(self, sc):
        if not messagebox.askyesno("Delete shortcut", 'Delete "%s"?' % sc["name"], parent=self.win):
            return
        def mutate(cfg):
            cfg["shortcuts"] = [s for s in cfg["shortcuts"] if s["id"] != sc["id"]]
        update_config(mutate)
        self.show_list()

    def duplicate(self, sc):
        self.show_editor(sc, as_copy=True)

    def show_editor(self, shortcut, as_copy=False):
        self.mode = "editor"
        self.editor = ShortcutEditor(self, shortcut, as_copy)


class ShortcutEditor:
    def __init__(self, owner, shortcut, as_copy=False):
        self.owner = owner
        self.theme = owner.theme
        self.win = owner.win
        self.sc = None if as_copy else shortcut  # a copy is saved as a NEW shortcut
        self.rows = []
        self.route_rows = []
        self.name = tk.StringVar(value=(shortcut["name"] + " (copy)" if as_copy else shortcut["name"]) if shortcut else "")
        self.folder = tk.StringVar(value=shortcut["folder"] if shortcut else "")
        self.priority = tk.StringVar(value=str(shortcut["priority"]) if shortcut else "0")
        self.exclusive = tk.BooleanVar(value=shortcut["exclusive"] if shortcut else False)
        # Auto-save and Pinned are switched off in a copy, so an unedited copy can't move files by mistake.
        self.auto = tk.BooleanVar(value=shortcut.get("auto", False) if shortcut and not as_copy else False)
        self.pinned = tk.BooleanVar(value=shortcut.get("pinned", False) if shortcut and not as_copy else False)
        self.open_after = tk.BooleanVar(value=shortcut.get("open_after", False) if shortcut else False)
        self.formula = tk.StringVar(value=shortcut["formula"] if shortcut else "")
        self._build(owner._fresh_body())
        for cond in (shortcut["conditions"] if shortcut else []):
            self.add_row(cond)
        for route in (shortcut.get("routes", []) if shortcut else []):
            self.add_route(route)
        self.formula.trace_add("write", lambda *args: self.check_formula())
        self.check_formula()

    def on_theme(self):
        self.scroll.canvas.configure(bg=self.theme.p["bg"])
        self.theme_btn.configure(text=self.theme.icon())

    def _build(self, body):
        t = self.theme
        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Button(head, text="\u2190 Back", command=self.owner.show_list).pack(side="left")
        ttk.Label(head, text="Edit shortcut" if self.sc else "New shortcut",
                  style="Title.TLabel").pack(side="left", padx=12)
        
        self.theme_btn = ttk.Button(head, text=t.icon(), style="Icon.TButton", width=3, command=t.toggle)
        self.theme_btn.pack(side="right")
        Tooltip(self.theme_btn, "Toggle dark/light theme", t)

        foot = ttk.Frame(body)
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        self.error = ttk.Label(foot, text="", style="Error.TLabel")
        self.error.pack(side="top", anchor="w", pady=(0, 6))
        self.save_btn = ttk.Button(foot, text="Save", style="Accent.TButton", command=self.save)
        self.save_btn.pack(side="left")
        ttk.Button(foot, text="Cancel", command=self.owner.show_list).pack(side="left", padx=8)

        self.scroll = ScrollFrame(body, t)
        self.scroll.pack(fill="both", expand=True, pady=(10, 0))
        form = self.scroll.inner
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Name").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 10))
        ttk.Entry(form, textvariable=self.name).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(form, text="Folder").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        ttk.Entry(form, textvariable=self.folder).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse\u2026", command=self.browse).grid(row=1, column=2, padx=(6, 0), pady=4)

        ttk.Label(form, text="Priority").grid(row=2, column=0, sticky="w", pady=4, padx=(0, 10))
        opts = ttk.Frame(form)
        opts.grid(row=2, column=1, columnspan=2, sticky="w", pady=4)
        
        spin_pri = ttk.Spinbox(opts, from_=-999, to=999, width=6, textvariable=self.priority)
        spin_pri.pack(side="left")
        Tooltip(spin_pri, "A higher number acts as a tiebreaker for equal conditions.", t)
        
        chk_exc = ttk.Checkbutton(opts, text="Exclusive", variable=self.exclusive)
        chk_exc.pack(side="left", padx=(16, 0))
        Tooltip(chk_exc, "If this matches, it hides any other matching shortcuts that have fewer conditions or lower priority.", t)
        
        chk_auto = ttk.Checkbutton(opts, text="Auto-save (don't ask)", variable=self.auto)
        chk_auto.pack(side="left", padx=(16, 0))
        Tooltip(chk_auto, "Moves file automatically ONLY if it's the top match.", t)
        
        chk_pin = ttk.Checkbutton(opts, text="Pinned", variable=self.pinned)
        chk_pin.pack(side="left", padx=(16, 0))
        Tooltip(chk_pin, "Always show at the top of the popup.", t)

        opts2 = ttk.Frame(form)
        opts2.grid(row=3, column=1, columnspan=2, sticky="w", pady=(0, 4))
        chk_open = ttk.Checkbutton(opts2, text="Open folder after download", variable=self.open_after)
        chk_open.pack(side="left")
        Tooltip(chk_open, "When the download finishes, opens the destination folder with the file selected.", t)
        
        ttk.Label(form, text="Hover over options for info. Higher priority is listed first (ties: more conditions first).",
                  style="Muted.TLabel", wraplength=480, justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w")

        ttk.Separator(form).grid(row=5, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(form, text="Conditions", style="Section.TLabel").grid(row=6, column=0, columnspan=3, sticky="w")
        self.cond_frame = ttk.Frame(form)
        self.cond_frame.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Button(form, text="Add condition", command=lambda: self.add_row(None, focus=True)).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(form, text="File type: one or more extensions, comma-separated (e.g. .pdf, .docx). "
                             "Site matches the download's host or the page's host.",
                  style="Muted.TLabel", wraplength=480, justify="left").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Separator(form).grid(row=10, column=0, columnspan=3, sticky="ew", pady=12)
        
        lbl_formula = ttk.Label(form, text="Formula")
        lbl_formula.grid(row=11, column=0, sticky="w", padx=(0, 10))
        Tooltip(lbl_formula, "Link conditions using * (AND) and + (OR).", t)
        
        ttk.Entry(form, textvariable=self.formula).grid(row=11, column=1, columnspan=2, sticky="ew")
        self.note = ttk.Label(form, text="", style="Muted.TLabel", wraplength=480, justify="left")
        self.note.grid(row=12, column=0, columnspan=3, sticky="w", pady=(4, 0))
        
        ttk.Label(form, text="Example: (1 + 2) * 3. Leave empty to require all conditions.",
                  style="Muted.TLabel", wraplength=480, justify="left").grid(
            row=13, column=0, columnspan=3, sticky="w", pady=(2, 8))

        ttk.Separator(form).grid(row=14, column=0, columnspan=3, sticky="ew", pady=12)
        lbl_routes = ttk.Label(form, text="Sub-folders (optional)", style="Section.TLabel")
        lbl_routes.grid(row=15, column=0, columnspan=3, sticky="w")
        Tooltip(lbl_routes, "Send files to a sub-folder of this shortcut's folder, based on words in the file name.", t)
        self.route_frame = ttk.Frame(form)
        self.route_frame.grid(row=16, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Button(form, text="Add sub-folder", command=lambda: self.add_route(None, focus=True)).grid(
            row=17, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(form, text="If the file name contains any of the words (comma-separated, e.g. sheet, sh), "
                             "the file goes to that folder. A relative folder is created inside this shortcut's "
                             "folder. The first matching row wins; if none match, the main folder is used.",
                  style="Muted.TLabel", wraplength=480, justify="left").grid(
            row=18, column=0, columnspan=3, sticky="w", pady=(4, 8))

    def add_row(self, cond=None, focus=False):
        t = self.theme
        frame = ttk.Frame(self.cond_frame)
        frame.pack(fill="x", pady=2)
        frame.columnconfigure(3, weight=1)
        row = {
            "frame": frame,
            "field": tk.StringVar(value=FIELD_LABELS[cond["field"]] if cond else FIELD_LABELS["site"]),
            "op": tk.StringVar(value=cond["op"] if cond else "contains"),
            "value": tk.StringVar(value=cond["value"] if cond else ""),
        }
        row["num"] = ttk.Label(frame, text="", width=3)
        row["num"].grid(row=0, column=0, padx=(0, 4))
        
        field_cb = ttk.Combobox(frame, textvariable=row["field"], state="readonly", width=13,
                                values=[FIELD_LABELS[f] for f in FIELDS])
        field_cb.grid(row=0, column=1, padx=(0, 6))
        Tooltip(field_cb, lambda r=row: FIELD_TOOLTIPS.get(r["field"].get(), ""), t)
        
        row["op_cb"] = ttk.Combobox(frame, textvariable=row["op"], state="readonly", width=11, values=OPS)
        row["op_cb"].grid(row=0, column=2, padx=(0, 6))
        row["entry"] = ttk.Entry(frame, textvariable=row["value"])
        row["entry"].grid(row=0, column=3, sticky="ew", padx=(0, 6))
        ttk.Button(frame, text="\u2715", style="Icon.TButton", width=3,
                   command=lambda r=row: self.remove_row(r)).grid(row=0, column=4)
        t.style_combo(field_cb)
        t.style_combo(row["op_cb"])
        field_cb.bind("<<ComboboxSelected>>", lambda e, r=row: self.on_field_change(r))
        self.rows.append(row)
        self.on_field_change(row)
        self.renumber()
        self.check_formula()
        if focus:
            row["entry"].focus_set()

    def on_field_change(self, row):
        if LABEL_TO_FIELD.get(row["field"].get()) == "file_type":
            row["op"].set("is")
            row["op_cb"].configure(values=["is"], state="disabled")
        else:
            row["op_cb"].configure(values=OPS, state="readonly")
            if row["op"].get() not in OPS:
                row["op"].set("contains")

    def remove_row(self, row):
        row["frame"].destroy()
        self.rows.remove(row)
        self.renumber()
        self.check_formula()

    def renumber(self):
        for i, row in enumerate(self.rows, 1):
            row["num"].configure(text="%d" % i)

    def add_route(self, route=None, focus=False):
        frame = ttk.Frame(self.route_frame)
        frame.pack(fill="x", pady=2)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        row = {"frame": frame,
               "value": tk.StringVar(value=route["value"] if route else ""),
               "folder": tk.StringVar(value=route["folder"] if route else "")}
        ttk.Label(frame, text="File name contains").grid(row=0, column=0, padx=(0, 6))
        row["entry"] = ttk.Entry(frame, textvariable=row["value"], width=16)
        row["entry"].grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Label(frame, text="\u2192").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(frame, textvariable=row["folder"], width=16).grid(row=0, column=3, sticky="ew", padx=(0, 6))
        browse_btn = ttk.Button(frame, text="\u2026", style="Icon.TButton", width=3,
                                command=lambda r=row: self.browse_route(r))
        browse_btn.grid(row=0, column=4, padx=(0, 6))
        Tooltip(browse_btn, "Pick the sub-folder", self.theme)
        ttk.Button(frame, text="\u2715", style="Icon.TButton", width=3,
                   command=lambda r=row: self.remove_route(r)).grid(row=0, column=5)
        self.route_rows.append(row)
        if focus:
            row["entry"].focus_set()

    def remove_route(self, row):
        row["frame"].destroy()
        self.route_rows.remove(row)

    def browse_route(self, row):
        base = self.folder.get().strip()
        initial = base if os.path.isdir(base) else str(Path.home())
        chosen = filedialog.askdirectory(parent=self.win, initialdir=initial, title="Choose a sub-folder")
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        if base:
            try:
                rel = os.path.relpath(chosen, base)
                if not rel.startswith(".."):
                    chosen = rel  # inside the main folder -> store it relative
            except ValueError:
                pass  # different drive
        row["folder"].set(chosen)

    def check_formula(self):
        text = self.formula.get().strip()
        n = len(self.rows)
        ok = True
        style = "Muted.TLabel"
        if not text:
            if n == 0:
                msg = "No conditions yet: this shortcut will only appear under All shortcuts."
            else:
                msg = "Formula empty: all conditions must match (AND)."
        else:
            try:
                _, used = parse_formula(text, n)
                unused = [str(i) for i in range(1, n + 1) if i not in used]
                msg = ("Not used in the formula: condition %s." % ", ".join(unused)) if unused else "Formula OK."
            except FormulaError as exc:
                msg, style, ok = str(exc), "Error.TLabel", False
        self.note.configure(text=msg, style=style)
        self.save_btn.configure(state="normal" if ok else "disabled")
        return ok

    def browse(self):
        current = self.folder.get().strip()
        initial = current if os.path.isdir(current) else str(Path.home())
        folder = filedialog.askdirectory(parent=self.win, initialdir=initial, title="Choose a folder")
        if folder:
            self.folder.set(os.path.normpath(folder))

    def fail(self, message):
        self.error.configure(text=message)

    def save(self):
        self.error.configure(text="")
        name = self.name.get().strip()
        folder = self.folder.get().strip()
        if not name:
            return self.fail("Name is required.")
        if not folder:
            return self.fail("Folder is required.")
        try:
            priority = int(self.priority.get().strip() or 0)
        except ValueError:
            return self.fail("Priority must be a whole number.")
            
        if self.auto.get() and len(self.rows) == 0:
            return self.fail("Auto-save requires at least one condition.")
            
        conditions = []
        for i, row in enumerate(self.rows, 1):
            value = row["value"].get().strip()
            if not value:
                return self.fail("Condition %d needs a value." % i)
            field = LABEL_TO_FIELD[row["field"].get()]
            conditions.append({"field": field, "op": "is" if field == "file_type" else row["op"].get(),
                               "value": value})
        if not self.check_formula():
            return
        routes = []
        for i, r in enumerate(self.route_rows, 1):
            words = r["value"].get().strip()
            rfolder = r["folder"].get().strip()
            if not words and not rfolder:
                continue
            if not words or not rfolder:
                return self.fail("Sub-folder %d needs both words and a folder." % i)
            routes.append({"value": words, "folder": rfolder})
        shortcut = {
            "id": self.sc["id"] if self.sc else uuid.uuid4().hex,
            "name": name,
            "folder": folder,
            "priority": priority,
            "exclusive": bool(self.exclusive.get()),
            "auto": bool(self.auto.get()),
            "pinned": bool(self.pinned.get()),
            "open_after": bool(self.open_after.get()),
            "routes": routes,
            "conditions": conditions,
            "formula": self.formula.get().strip(),
        }

        def mutate(cfg):
            for i, old in enumerate(cfg["shortcuts"]):
                if old["id"] == shortcut["id"]:
                    cfg["shortcuts"][i] = shortcut
                    return
            cfg["shortcuts"].append(shortcut)
        update_config(mutate)
        self.owner.show_list()


# ===========================================================================
# 8. Entry points (Smart Restart Logic)
# ===========================================================================

def run_server_mode(root, theme, silent=False):
    server = None

    # 1. Try to start the server. If the port is busy, an older copy is running:
    #    ask it to quit, then take over its port.
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/quit", method="POST",
                                         headers={"Origin": "chrome-extension://local"})
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass
        # Wait (up to ~4 s) for the old copy to free the port.
        for _ in range(20):
            time.sleep(0.2)
            try:
                server = Server(("127.0.0.1", PORT), Handler)
                break
            except OSError:
                pass

    if not server:
        messagebox.showerror("Download Router",
                             "Port %d is used by another program, so Download Router cannot start." % PORT)
        return

    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 2. Normal start (double-click) shows the settings window.
    #    --background (Windows Startup) stays silent.
    if not silent:
        SettingsWindow(root, theme)

    # 3. Main processing loop for Chrome requests
    def poll():
        try:
            data, reply = requests_q.get_nowait()
        except queue.Empty:
            root.after(200, poll)
            return
            
        dest = None
        open_after = False
        try:
            ctx = build_context(data)
            cfg = load_config()
            suggested = compute_suggestions(cfg["shortcuts"], ctx)
            
            if suggested and suggested[0].get("auto"):
                dest = resolve_folder(suggested[0], ctx)
                open_after = bool(suggested[0].get("open_after"))
                log_info("Auto-saved '%s' to '%s' (Rule: %s)" % (ctx.get("filename") or "(unnamed)", dest, suggested[0]["name"]))
            else:
                dest = ask_folder(root, theme, data)
                if dest:
                    remember_recent(dest)
                    open_after = wants_open(load_config()["shortcuts"], ctx, dest)
        except Exception:
            log_error()
            dest = None
            open_after = False
            
        reply.put((dest, open_after))
        root.after(200, poll)

    poll()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Download Router")
    parser.add_argument("--settings", action="store_true", help="open the settings window only (no server)")
    parser.add_argument("--background", action="store_true", help="start silently, without opening settings")
    args = parser.parse_args()

    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    theme = Theme(root, load_config()["theme"])
    if args.settings:
        SettingsWindow(root, theme, on_close=root.destroy)
        root.mainloop()
    else:
        run_server_mode(root, theme, silent=args.background)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error()
        raise