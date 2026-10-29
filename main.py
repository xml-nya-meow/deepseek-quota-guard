"""Portable entry point. No system configuration changes."""
import argparse
import json
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--smoke', action='store_true', help='Open then close UI without network or data mutation')
    parser.add_argument('--browser-smoke', action='store_true', help='Check bundled Chromium using about:blank only')
    args = parser.parse_args()
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent.parent
    folder = args.data_dir or base
    if args.browser_smoke:
        from quota_guard.browser_service import BrowserService
        folder.mkdir(parents=True, exist_ok=True)
        browser = BrowserService()
        try:
            result = browser.submit('selftest').result(timeout=45)
            (folder / 'browser-smoke.json').write_text(json.dumps(result), encoding='utf-8')
        finally:
            browser.close()
        return
    root = tk.Tk()
    try:
        from quota_guard.ui import App
        app = App(root, folder)
        if args.smoke:
            root.after(1500, app.close)
        root.mainloop()
    except Exception:
        messagebox.showerror('启动未完成', '程序启动失败。请确认文件夹完整、目录可写且没有其他实例；保留数据文件，请勿清空统计。')
        root.destroy()
        raise

if __name__ == '__main__':
    main()
