#!/usr/bin/env python3
"""Point-and-click front end for fetch_usage.py.

Double-click the launcher for your OS (Frontier Usage.command / .bat / .sh) or run:
    python3 gui.py        (Windows: pyw gui.py)

Paste a usage key for each provider once; it is checked and saved in the OS credential store.
Then press "Generate report": the report (usage, on-prem sizing, Grafana export) opens in your
browser, and is optionally published to Grafana.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
import urllib.error
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    sys.exit("This Python has no Tkinter (GUI) support.\n"
             "  macOS (Homebrew): brew install python-tk\n"
             "  Debian/Ubuntu:    sudo apt install python3-tk\n"
             "  Fedora:           sudo dnf install python3-tkinter\n"
             "  Windows/macOS:    or install Python from python.org, which includes it.")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import fetch_usage  # noqa: E402
import grafana  # noqa: E402
from auth import ApiError  # noqa: E402

APP_TITLE = "Frontier Usage & On-prem Sizing"
DAY_CHOICES = {"Last 7 days": 7, "Last 30 days": 30, "Last 60 days": 60, "Last 90 days": 90}


def default_out_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "Frontier Usage Reports"


def open_path(path: Path) -> None:
    """Open a folder in Explorer / Finder / the Linux file manager."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def friendly(e: BaseException) -> str:
    if isinstance(e, grafana.GrafanaError):
        return str(e)
    if isinstance(e, ApiError):
        said = f"\n\nProvider response: HTTP {e.status}, \"{e.api_message}\""
        scope_msg = (e.api_message or "").lower()
        if e.status == 403 and "read:analytics" in scope_msg:
            return ("The key works but can't read usage analytics. In claude.ai > Organization settings > API, create "
                    "a key with the read:analytics scope (only the primary owner can), then connect the Claude "
                    "Enterprise row with it. The organization must be on a Claude Enterprise plan." + said)
        if e.status == 403 and "scope" in scope_msg:
            return ("The key works but isn't allowed to read usage. For Anthropic this usually means a Claude "
                    "Enterprise key (sk-ant-api01-) went into the Anthropic API row. Put it in the Claude Enterprise "
                    "row instead. The Anthropic API row needs a Console Admin key (sk-ant-admin01-) from "
                    "platform.claude.com > Settings > Admin keys." + said)
        if e.status in (401, 403):
            return ("The key was rejected. Check that it's the right kind of key for this row, that it was copied "
                    "completely, and that it hasn't been revoked. If you just created it, wait a minute and "
                    "click Connect again; new keys can take a moment to start working." + said)
        return "The provider returned an error." + said
    if isinstance(e, urllib.error.URLError):
        return f"Couldn't reach the server. Check the address, your internet connection or VPN.\n({e.reason})"
    return str(e) or e.__class__.__name__


class ProviderRow:
    """One line in the Accounts box: status, key field, Connect/Disconnect."""

    def __init__(self, app: "App", parent: ttk.Frame, row: int, provider: str):
        self.app, self.provider = app, provider
        p = auth.PROVIDERS[provider]
        self.include = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Checking...")
        self.key = tk.StringVar()

        ttk.Checkbutton(parent, text=p["label"], variable=self.include).grid(row=row, column=0, sticky="w", padx=(0, 8))
        self.status_lbl = ttk.Label(parent, textvariable=self.status, width=24)
        self.status_lbl.grid(row=row, column=1, sticky="w")
        self.entry = ttk.Entry(parent, textvariable=self.key, show="•", width=34)
        self.entry.grid(row=row, column=2, sticky="ew", padx=6)
        self.entry.bind("<Return>", lambda _e: self.connect())
        self.connect_btn = ttk.Button(parent, text="Connect", command=self.connect)
        self.connect_btn.grid(row=row, column=3, padx=2)
        self.forget_btn = ttk.Button(parent, text="Disconnect", command=self.disconnect)
        self.forget_btn.grid(row=row, column=4, padx=2)
        link = ttk.Label(parent, text="Get a key", foreground="#2a6bd1", cursor="hand2")
        link.grid(row=row, column=5, padx=(6, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(p["where"]))

    def set_status(self, text: str, ok: bool | None) -> None:
        self.status.set(text)
        self.status_lbl.configure(foreground={True: "#1a7f37", False: "#c62828", None: ""}[ok])

    def refresh(self) -> None:
        """Check the env var / saved key in the background."""
        self.set_status("Checking...", None)
        p = auth.PROVIDERS[self.provider]

        def work():
            for source, secret in (("env var", os.environ.get(p["env"])), ("saved", auth.load_saved(self.provider))):
                if not secret:
                    continue
                try:
                    auth.verify(self.provider, secret)
                    return (f"✔ Connected ({source})" if source == "env var" else "✔ Connected", True)
                except ApiError:
                    return ("✖ Saved key rejected", False)
                except urllib.error.URLError:
                    return ("? Saved key (offline)", None)
            return ("Not connected", None)

        self.app.run_bg(work, lambda res: self.set_status(*res))

    def connect(self) -> None:
        secret = self.key.get().strip()
        label = auth.PROVIDERS[self.provider]["label"]
        if not secret:
            messagebox.showinfo(APP_TITLE, f"Paste your {label} key into the box first.\n\n"
                                           "Click \"Get a key\" if you don't have one.")
            self.entry.focus_set()
            return
        self.set_status("Checking key...", None)
        self.connect_btn.state(["disabled"])

        def work():
            auth.verify(self.provider, secret)
            return auth.save(self.provider, secret)

        def done(store):
            self.connect_btn.state(["!disabled"])
            self.key.set("")
            self.set_status("✔ Connected", True)
            self.app.log(f"{label}: key verified and saved to {store}.")

        def failed(e):
            self.connect_btn.state(["!disabled"])
            self.set_status("✖ Key not accepted", False)
            hint = auth.diagnose_key(self.provider, secret) if isinstance(e, ApiError) else None
            if hint:
                msg = f"{hint}\n\nProvider response: HTTP {e.status}, \"{e.api_message}\""
            else:
                msg = friendly(e)
            self.app.log(f"{label}: key rejected ({auth.mask(secret)}, {len(secret)} chars). {msg}")
            messagebox.showerror(APP_TITLE, f"{label}: {msg}")

        self.app.run_bg(work, done, failed)

    def disconnect(self) -> None:
        label = auth.PROVIDERS[self.provider]["label"]
        if not messagebox.askyesno(APP_TITLE, f"Remove the saved {label} key from this computer?"):
            return
        auth.forget(self.provider)
        self.app.log(f"{label}: saved key removed.")
        self.refresh()

    def headers(self) -> dict[str, str]:
        """Headers for a report run (called on the worker thread). Raises LookupError to skip."""
        p = auth.PROVIDERS[self.provider]
        secret = os.environ.get(p["env"]) or auth.load_saved(self.provider)
        if not secret:
            raise LookupError(f"{p['label']}: not connected")
        return p["headers"](secret)


class GrafanaBox:
    """Optional Grafana target: URL + service account token, publish after each report."""

    def __init__(self, app: "App", parent: ttk.Frame):
        self.app = app
        settings = auth.load_settings()
        self.url = tk.StringVar(value=settings.get("grafana_url", ""))
        self.token = tk.StringVar()
        self.auto = tk.BooleanVar(value=bool(settings.get("grafana_auto", False)))
        self.status = tk.StringVar(value="Not connected")
        self.connected = False

        box = ttk.LabelFrame(parent, text=" 3. Grafana (optional) ", padding=10)
        box.pack(fill="x", pady=(0, 10))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Grafana address").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.url).grid(row=0, column=1, sticky="ew", padx=6)
        self.status_lbl = ttk.Label(box, textvariable=self.status, width=28)
        self.status_lbl.grid(row=0, column=2, columnspan=3, sticky="w")
        ttk.Label(box, text="Service account token").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.entry = ttk.Entry(box, textvariable=self.token, show="•")
        self.entry.grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        self.entry.bind("<Return>", lambda _e: self.connect())
        self.connect_btn = ttk.Button(box, text="Connect", command=self.connect)
        self.connect_btn.grid(row=1, column=2, padx=2, pady=(4, 0))
        ttk.Button(box, text="Disconnect", command=self.disconnect).grid(row=1, column=3, padx=2, pady=(4, 0))
        link = ttk.Label(box, text="How?", foreground="#2a6bd1", cursor="hand2")
        link.grid(row=1, column=4, padx=(6, 0), pady=(4, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(
            "https://grafana.com/docs/grafana/latest/administration/service-accounts/"))
        ttk.Checkbutton(box, text="Publish to Grafana after each report", variable=self.auto,
                        command=lambda: auth.save_settings(grafana_auto=self.auto.get())).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.publish_btn = ttk.Button(box, text="Publish last report now", command=lambda: self.publish(open_after=True))
        self.publish_btn.grid(row=2, column=2, columnspan=3, sticky="e", pady=(6, 0))
        ttk.Label(box, text="Needs the free Infinity data source plugin in Grafana. The report's Grafana tab "
                            "also lets you download the dashboard and import it by hand.",
                  foreground="#777", wraplength=640).grid(row=3, column=0, columnspan=5, sticky="w", pady=(6, 0))

    def set_status(self, text: str, ok: bool | None) -> None:
        self.connected = bool(ok)
        self.status.set(text)
        self.status_lbl.configure(foreground={True: "#1a7f37", False: "#c62828", None: ""}[ok])

    def base_url(self) -> str:
        url = self.url.get().strip().rstrip("/")
        if url and "://" not in url:
            url = "https://" + url
        return url

    def saved_token(self) -> str | None:
        return os.environ.get("GRAFANA_TOKEN") or auth.load_saved(grafana.TOKEN_ACCOUNT)

    def refresh(self) -> None:
        url, token = self.base_url(), self.saved_token()
        if not (url and token):
            self.set_status("Not connected", None)
            return
        self.set_status("Checking...", None)

        def work():
            try:
                return (f"✔ Connected: {grafana.verify(url, token)}", True)
            except grafana.GrafanaError:
                return ("✖ Saved token rejected", False)
            except (ApiError, urllib.error.URLError, ValueError):
                return ("? Can't reach Grafana", None)

        self.app.run_bg(work, lambda res: self.set_status(*res))

    def connect(self) -> None:
        url, typed = self.base_url(), self.token.get().strip()
        token = typed or self.saved_token()
        if not url or not token:
            messagebox.showinfo(APP_TITLE, "Enter your Grafana address (for example https://grafana.example.com) "
                                           "and a service account token with the Editor role.\n\n"
                                           "Click \"How?\" for how to create a token.")
            return
        self.url.set(url)
        self.set_status("Checking...", None)
        self.connect_btn.state(["disabled"])

        def work():
            org = grafana.verify(url, token)
            store = auth.save(grafana.TOKEN_ACCOUNT, token) if typed else None
            auth.save_settings(grafana_url=url)
            return org, store

        def done(res):
            org, store = res
            self.connect_btn.state(["!disabled"])
            self.token.set("")
            self.set_status(f"✔ Connected: {org}", True)
            self.app.log(f"Grafana: connected to {org} at {url}" + (f"; token saved to {store}." if store else "."))

        def failed(e):
            self.connect_btn.state(["!disabled"])
            self.set_status("✖ Not connected", False)
            masked = f" ({auth.mask(token)})" if typed else ""
            self.app.log(f"Grafana: connection failed{masked}. {friendly(e)}")
            messagebox.showerror(APP_TITLE, f"Grafana: {friendly(e)}")

        self.app.run_bg(work, done, failed)

    def disconnect(self) -> None:
        if not messagebox.askyesno(APP_TITLE, "Remove the saved Grafana token from this computer?"):
            return
        auth.forget(grafana.TOKEN_ACCOUNT)
        self.app.log("Grafana: saved token removed.")
        self.refresh()

    def publish(self, open_after: bool = False) -> None:
        url, token, out = self.base_url(), self.saved_token(), Path(self.app.out_dir.get()).expanduser()
        if not url or not token:
            messagebox.showinfo(APP_TITLE, "Connect to Grafana first: enter the address and a token, then click Connect.")
            return
        self.publish_btn.state(["disabled"])
        self.app.log("Publishing to Grafana...")

        def done(link: str):
            self.publish_btn.state(["!disabled"])
            self.app.log(f"Grafana dashboard updated: {link}")
            if open_after:
                webbrowser.open(link)

        def failed(e):
            self.publish_btn.state(["!disabled"])
            self.app.log("Grafana: " + friendly(e))
            messagebox.showerror(APP_TITLE, f"Couldn't publish to Grafana.\n\n{friendly(e)}")

        self.app.run_bg(lambda: fetch_usage.publish_grafana(out, url, token), done, failed)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.last_dashboard: Path | None = None
        root.title(APP_TITLE)
        root.minsize(780, 640)
        try:
            ttk.Style().theme_use({"win32": "vista", "darwin": "aqua"}.get(sys.platform, "clam"))
        except tk.TclError:
            pass

        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="Token usage, Grafana & on-prem sizing", font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
        ttk.Label(main, text="Connect each provider once, then generate a report. It opens in your web browser with "
                       "usage charts, an on-prem cluster sizing tab and a Grafana export.",
                  foreground="#555").pack(anchor="w", pady=(0, 10))

        # Accounts
        acct = ttk.LabelFrame(main, text=" 1. Accounts ", padding=10)
        acct.pack(fill="x")
        acct.columnconfigure(2, weight=1)
        self.rows = [ProviderRow(self, acct, i, prov) for i, prov in enumerate(auth.PROVIDERS)]
        ttk.Label(acct, text=f"Keys are stored in: {auth.store_name()}", foreground="#777").grid(
            row=len(self.rows), column=0, columnspan=6, sticky="w", pady=(6, 0))

        # Options
        opts = ttk.LabelFrame(main, text=" 2. Report options ", padding=10)
        opts.pack(fill="x", pady=10)
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="Time period").grid(row=0, column=0, sticky="w")
        self.period = tk.StringVar(value="Last 30 days")
        ttk.Combobox(opts, textvariable=self.period, values=list(DAY_CHOICES), state="readonly", width=16).grid(
            row=0, column=1, sticky="w", padx=6)
        self.claude_code = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Include Claude Code usage (Anthropic API Admin key)", variable=self.claude_code).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Label(opts, text="Extra CSV files").grid(row=2, column=0, sticky="w")
        self.csvs: list[str] = []
        self.csv_label = tk.StringVar(value="none (optional, e.g. Gemini exports)")
        ttk.Label(opts, textvariable=self.csv_label, foreground="#555").grid(row=2, column=1, sticky="w", padx=6)
        csv_btns = ttk.Frame(opts)
        csv_btns.grid(row=2, column=2, sticky="e")
        ttk.Button(csv_btns, text="Add...", command=self.add_csv).pack(side="left")
        ttk.Button(csv_btns, text="Clear", command=self.clear_csv).pack(side="left", padx=(4, 0))

        ttk.Label(opts, text="Save reports to").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.out_dir = tk.StringVar(value=str(default_out_dir()))
        ttk.Entry(opts, textvariable=self.out_dir).grid(row=3, column=1, sticky="ew", padx=6, pady=(4, 0))
        ttk.Button(opts, text="Browse...", command=self.pick_out).grid(row=3, column=2, sticky="e", pady=(4, 0))

        self.grafana = GrafanaBox(self, main)

        # Actions
        actions = ttk.Frame(main)
        actions.pack(fill="x")
        self.go_btn = ttk.Button(actions, text="Generate report", command=self.generate)
        self.go_btn.pack(side="left")
        self.demo_btn = ttk.Button(actions, text="Try with sample data", command=lambda: self.generate(demo=True))
        self.demo_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(actions, text="Open last report", command=self.open_last, state="disabled")
        self.open_btn.pack(side="left")
        ttk.Button(actions, text="Open folder", command=self.open_folder).pack(side="left", padx=6)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=140)
        self.progress.pack(side="right")

        # Log
        logf = ttk.Frame(main)
        logf.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(logf, height=8, wrap="word", state="disabled", relief="flat",
                                background="#f6f6f6", foreground="#333", font=("TkFixedFont", 10))
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        root.after(50, self.pump)
        for r in self.rows:
            r.refresh()
        self.grafana.refresh()
        self.log("Ready.")

    # -- threading: workers put callbacks on a queue; the Tk thread runs them.
    def run_bg(self, work, on_done=None, on_error=None) -> None:
        def runner():
            try:
                res = work()
            except BaseException as e:  # noqa: BLE001 - report everything to the UI
                self.events.put(lambda err=e: (on_error or self.show_error)(err))
            else:
                if on_done:
                    self.events.put(lambda res=res: on_done(res))
        threading.Thread(target=runner, daemon=True).start()

    def pump(self) -> None:
        try:
            while True:
                self.events.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def log_from_thread(self, msg: str) -> None:
        self.events.put(lambda: self.log(msg))

    def show_error(self, e: BaseException) -> None:
        self.log("ERROR: " + friendly(e))
        if not isinstance(e, (ApiError, urllib.error.URLError, fetch_usage.ReportError, OSError, ValueError)):
            self.log("".join(traceback.format_exception(type(e), e, e.__traceback__)))
        messagebox.showerror(APP_TITLE, friendly(e))

    # -- options
    def add_csv(self) -> None:
        paths = filedialog.askopenfilenames(title="Choose usage CSV files",
                                            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        self.csvs += [p for p in paths if p not in self.csvs]
        self.csv_label.set(", ".join(Path(p).name for p in self.csvs) or "none (optional, e.g. Gemini exports)")

    def clear_csv(self) -> None:
        self.csvs = []
        self.csv_label.set("none (optional, e.g. Gemini exports)")

    def pick_out(self) -> None:
        d = filedialog.askdirectory(title="Save reports to", initialdir=self.out_dir.get())
        if d:
            self.out_dir.set(d)

    def open_last(self) -> None:
        if self.last_dashboard:
            webbrowser.open(self.last_dashboard.resolve().as_uri())

    def open_folder(self) -> None:
        out = Path(self.out_dir.get()).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        open_path(out)

    # -- the main action
    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for b in (self.go_btn, self.demo_btn):
            b.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def generate(self, demo: bool = False) -> None:
        if self.busy:
            return
        rows = [r for r in self.rows if r.include.get()]
        if not demo and not rows and not self.csvs:
            messagebox.showinfo(APP_TITLE, "Tick at least one provider, or add a CSV file.")
            return
        days = DAY_CHOICES[self.period.get()]
        out = Path(self.out_dir.get()).expanduser()
        by_name = {r.provider: r for r in rows}
        claude_code, csvs = self.claude_code.get(), list(self.csvs)  # Tk vars must be read on this thread
        self.set_busy(True)
        self.log(f"--- {'Sample data' if demo else 'Generating report'}: {self.period.get().lower()} ---")

        def work():
            if not demo and not csvs:
                connected = []
                for r in rows:
                    try:
                        r.headers()
                        connected.append(r)
                    except LookupError:
                        pass
                if not connected:
                    raise fetch_usage.ReportError("No provider is connected yet. Paste a key next to a "
                                                  "provider and click Connect, or try the sample data.")
            return fetch_usage.generate(days, list(by_name), claude_code, csvs, demo, out,
                                        lambda prov: by_name[prov].headers(), self.log_from_thread, friendly)

        def done(dashboard: Path):
            self.set_busy(False)
            self.last_dashboard = dashboard
            self.open_btn.state(["!disabled"])
            self.log("Done. Opening the dashboard in your browser.")
            webbrowser.open(dashboard.resolve().as_uri())
            if self.grafana.auto.get() and self.grafana.connected:
                self.grafana.publish()

        def failed(e):
            self.set_busy(False)
            self.show_error(e)

        self.run_bg(work, done, failed)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
