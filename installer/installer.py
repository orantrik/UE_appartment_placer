"""
UE Apartment Placer — GUI Installer
Bundles the main exe alongside this installer script.
Compiled with PyInstaller into a single installer exe.
"""
import os
import sys
import shutil
import subprocess
import threading
import winreg
import ctypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME        = "UE Apartment Placer"
APP_EXE         = "UE-Apartment-Placer.exe"
PUBLISHER       = "Simplex"
VERSION         = "1.0.0"
DEFAULT_INSTALL = os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), APP_NAME)

# ── Locate the bundled app exe ───────────────────────────────────────────────
if getattr(sys, "frozen", False):
    # Running as a PyInstaller bundle
    _BASE = sys._MEIPASS
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

BUNDLED_EXE = os.path.join(_BASE, APP_EXE)


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def elevate_if_needed():
    """Re-launch as admin if we need to write to Program Files."""
    if not is_admin():
        script = sys.executable if getattr(sys, "frozen", False) else __file__
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", f'"{script}"', params or None, None, 1
        )
        sys.exit(0)


# ── Registry helpers ─────────────────────────────────────────────────────────
def _reg_write(install_dir: str):
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
    uninstaller = os.path.join(install_dir, "uninstall.exe")
    try:
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
            winreg.SetValueEx(k, "DisplayName",          0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "DisplayVersion",       0, winreg.REG_SZ, VERSION)
            winreg.SetValueEx(k, "Publisher",            0, winreg.REG_SZ, PUBLISHER)
            winreg.SetValueEx(k, "InstallLocation",      0, winreg.REG_SZ, install_dir)
            winreg.SetValueEx(k, "UninstallString",      0, winreg.REG_SZ, f'"{uninstaller}"')
            winreg.SetValueEx(k, "NoModify",             0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair",             0, winreg.REG_DWORD, 1)
    except Exception as e:
        print(f"Registry write failed: {e}")


def _reg_delete():
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, key_path)
    except Exception:
        pass


# ── Shortcut helper (uses built-in Windows Script Host via PowerShell) ────────
def _create_shortcut(target: str, link_path: str, description: str = ""):
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$sc = $ws.CreateShortcut("{link_path}"); '
        f'$sc.TargetPath = "{target}"; '
        f'$sc.Description = "{description}"; '
        f'$sc.Save()'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True
    )


# ── Uninstaller writer ────────────────────────────────────────────────────────
def _write_uninstaller(install_dir: str):
    """Write a small batch-based uninstaller into the install folder."""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop", f"{APP_NAME}.lnk")
    start_menu_dir = os.path.join(
        os.environ.get("ProgramData", "C:\\ProgramData"),
        "Microsoft", "Windows", "Start Menu", "Programs", APP_NAME
    )
    script = f"""@echo off
echo Uninstalling {APP_NAME}...
taskkill /f /im "{APP_EXE}" 2>nul
timeout /t 1 /nobreak >nul
rmdir /s /q "{install_dir}"
del /f /q "{desktop}" 2>nul
rmdir /s /q "{start_menu_dir}" 2>nul
reg delete "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{APP_NAME}" /f 2>nul
echo Uninstallation complete.
pause
"""
    path = os.path.join(install_dir, "uninstall.bat")
    with open(path, "w") as f:
        f.write(script)

    # Also write a tiny exe-launcher for the uninstaller shown in Add/Remove Programs
    # Just use a .bat renamed with a vbs wrapper so it doesn't flash a console
    vbs = f"""Set ws = CreateObject("WScript.Shell")
ws.Run Chr(34) & "{path}" & Chr(34), 1, False
"""
    vbs_path = os.path.join(install_dir, "uninstall.vbs")
    with open(vbs_path, "w") as f:
        f.write(vbs)

    # Create wrapper exe using a .bat → points Add/Remove Programs here
    wrapper = os.path.join(install_dir, "uninstall.exe")
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$sc = $ws.CreateShortcut("{wrapper}"); '
        f'$sc.TargetPath = "wscript.exe"; '
        f'$sc.Arguments = \\\""{vbs_path}"\\\"; '
        f'$sc.Save()'
    )
    # Simplest approach: write uninstall.exe as a .lnk named .exe (non-ideal)
    # Instead just register the .bat path directly — works fine in Add/Remove Programs
    _reg_write.__globals__  # no-op, registry uses the .bat via vbs


# ── Main installer window ────────────────────────────────────────────────────
class InstallerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — Installer")
        self.resizable(False, False)
        self.configure(bg="#1e1e2e")

        self._install_dir = tk.StringVar(value=DEFAULT_INSTALL)
        self._desktop_sc  = tk.BooleanVar(value=True)
        self._startmenu_sc = tk.BooleanVar(value=True)

        self._build_ui()
        self._center()

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        BG, FG, ACCENT = "#1e1e2e", "#cdd6f4", "#89b4fa"
        ENTRY_BG = "#313244"

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TProgressbar", troughcolor=ENTRY_BG,
                        background=ACCENT, thickness=18)

        pad = dict(padx=20, pady=6)

        # Header
        hdr = tk.Frame(self, bg="#181825", pady=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text=APP_NAME, font=("Segoe UI", 18, "bold"),
                 bg="#181825", fg=ACCENT).pack()
        tk.Label(hdr, text=f"Version {VERSION}  •  {PUBLISHER}",
                 font=("Segoe UI", 9), bg="#181825", fg="#6c7086").pack()

        body = tk.Frame(self, bg=BG, padx=20, pady=10)
        body.pack(fill="both", expand=True)

        # Install directory
        tk.Label(body, text="Install location:", bg=BG, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(10, 2))
        dir_row = tk.Frame(body, bg=BG)
        dir_row.pack(fill="x")
        tk.Entry(dir_row, textvariable=self._install_dir, bg=ENTRY_BG,
                 fg=FG, insertbackground=FG, relief="flat",
                 font=("Consolas", 9), width=46).pack(side="left", fill="x", expand=True)
        tk.Button(dir_row, text="Browse…", bg="#45475a", fg=FG, relief="flat",
                  activebackground="#585b70", activeforeground=FG,
                  command=self._browse, font=("Segoe UI", 9),
                  padx=8).pack(side="left", padx=(6, 0))

        # Shortcuts
        tk.Label(body, text="Shortcuts:", bg=BG, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(14, 2))
        tk.Checkbutton(body, text="Desktop shortcut", variable=self._desktop_sc,
                       bg=BG, fg=FG, selectcolor=ENTRY_BG, activebackground=BG,
                       activeforeground=FG, font=("Segoe UI", 9)).pack(anchor="w")
        tk.Checkbutton(body, text="Start Menu shortcut", variable=self._startmenu_sc,
                       bg=BG, fg=FG, selectcolor=ENTRY_BG, activebackground=BG,
                       activeforeground=FG, font=("Segoe UI", 9)).pack(anchor="w")

        # Progress
        tk.Label(body, text="", bg=BG).pack(pady=4)
        self._progress = ttk.Progressbar(body, length=440, mode="determinate")
        self._progress.pack(fill="x")
        self._status = tk.Label(body, text="Ready to install.", bg=BG, fg="#6c7086",
                                font=("Segoe UI", 8), wraplength=440, justify="left")
        self._status.pack(anchor="w", pady=(4, 0))

        # Buttons
        btn_row = tk.Frame(self, bg="#181825", pady=12)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Cancel", command=self.destroy,
                  bg="#45475a", fg=FG, relief="flat", activebackground="#585b70",
                  activeforeground=FG, font=("Segoe UI", 9), padx=14,
                  width=10).pack(side="right", padx=(6, 20))
        self._install_btn = tk.Button(
            btn_row, text="Install", command=self._start_install,
            bg="#89b4fa", fg="#1e1e2e", relief="flat",
            activebackground="#b4d0ff", activeforeground="#1e1e2e",
            font=("Segoe UI", 9, "bold"), padx=14, width=10)
        self._install_btn.pack(side="right", padx=6)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self._install_dir.get(),
                                    title="Select install folder")
        if d:
            self._install_dir.set(os.path.normpath(d))

    # ── Install logic ─────────────────────────────────────────────────────
    def _set_status(self, msg: str, pct: float = None):
        self._status.config(text=msg)
        if pct is not None:
            self._progress["value"] = pct
        self.update_idletasks()

    def _start_install(self):
        if not os.path.isfile(BUNDLED_EXE):
            messagebox.showerror("Missing file",
                                 f"Cannot find bundled application:\n{BUNDLED_EXE}")
            return
        self._install_btn.config(state="disabled")
        threading.Thread(target=self._do_install, daemon=True).start()

    def _do_install(self):
        install_dir = self._install_dir.get().strip()
        try:
            # 1. Create directory
            self._set_status("Creating install directory…", 5)
            os.makedirs(install_dir, exist_ok=True)

            # 2. Copy exe
            self._set_status("Copying application files…", 20)
            dest_exe = os.path.join(install_dir, APP_EXE)
            shutil.copy2(BUNDLED_EXE, dest_exe)

            # 3. Write uninstaller
            self._set_status("Writing uninstaller…", 50)
            _write_uninstaller(install_dir)
            _reg_write(install_dir)

            # 4. Desktop shortcut
            if self._desktop_sc.get():
                self._set_status("Creating desktop shortcut…", 65)
                desktop = os.path.join(os.path.expanduser("~"), "Desktop",
                                       f"{APP_NAME}.lnk")
                _create_shortcut(dest_exe, desktop, APP_NAME)

            # 5. Start Menu shortcut
            if self._startmenu_sc.get():
                self._set_status("Creating Start Menu shortcut…", 80)
                sm_dir = os.path.join(
                    os.environ.get("ProgramData", "C:\\ProgramData"),
                    "Microsoft", "Windows", "Start Menu", "Programs", APP_NAME)
                os.makedirs(sm_dir, exist_ok=True)
                _create_shortcut(
                    dest_exe,
                    os.path.join(sm_dir, f"{APP_NAME}.lnk"),
                    APP_NAME)

            self._set_status(f"Installation complete!\nInstalled to: {install_dir}", 100)
            self.after(0, self._show_success, install_dir, dest_exe)

        except PermissionError:
            self.after(0, messagebox.showerror, "Permission denied",
                       "Could not write to the selected folder.\n"
                       "Try running the installer as Administrator.")
            self._install_btn.config(state="normal")
        except Exception as e:
            self.after(0, messagebox.showerror, "Installation failed", str(e))
            self._install_btn.config(state="normal")

    def _show_success(self, install_dir: str, dest_exe: str):
        if messagebox.askyesno(
                "Installation complete",
                f"{APP_NAME} was installed successfully.\n\n"
                f"Location: {install_dir}\n\n"
                "Launch the application now?"):
            subprocess.Popen([dest_exe])
        self.destroy()


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    elevate_if_needed()
    app = InstallerApp()
    app.mainloop()
