"""A bounded, on-demand diagnostic viewer with no continuous memory buffer."""
import os
import tkinter as tk
from tkinter import ttk

from .diagnostics import read_recent_diagnostics
from .appearance import apply_theme


def open_diagnostics_viewer(app):
    existing = getattr(app, '_diagnostics_window', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    dialog = tk.Toplevel(app.root)
    app._diagnostics_window = dialog
    dialog.title('运行诊断日志')
    dialog.resizable(True, True)
    dialog.geometry('900x580')
    dialog.minsize(600, 360)
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(1, weight=1)
    heading = ttk.Label(dialog, text='诊断日志保留 14 天，单文件约 2 MB，通常最多 12 份；活跃日志暂不清理。\n只显示最近 300 条（最多 256 KiB），点击刷新读取。账本与删除处理记录不会自动清理。',
                        wraplength=820, padding=12)
    heading.grid(row=0, column=0, sticky='ew')
    frame = ttk.Frame(dialog)
    frame.grid(row=1, column=0, sticky='nsew', padx=12)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    content = tk.Text(frame, wrap='word', state='disabled', width=60, height=10)
    content.grid(row=0, column=0, sticky='nsew')
    scrollbar = ttk.Scrollbar(frame, command=content.yview)
    scrollbar.grid(row=0, column=1, sticky='ns')
    content.configure(yscrollcommand=scrollbar.set)
    status = tk.StringVar()
    status_label = ttk.Label(dialog, textvariable=status, padding=(12, 6), wraplength=820)
    status_label.grid(row=2, column=0, sticky='ew')
    footer = ttk.Frame(dialog, padding=(12, 0, 12, 12))
    footer.grid(row=3, column=0, sticky='ew')

    def refresh():
        preview = read_recent_diagnostics(app.diagnostics_dir)
        content.configure(state='normal')
        content.delete('1.0', 'end')
        content.insert('end', preview.text or '暂无诊断记录。')
        content.configure(state='disabled')
        content.see('end')
        notice = f'显示 {preview.record_count} 条；跳过 {preview.skipped_lines} 条不完整记录。'
        if preview.truncated:
            notice += ' 较早内容已省略。'
        if preview.unavailable:
            notice += ' 部分日志暂时无法读取，可稍后刷新。'
        if app.diagnostics.disabled:
            notice += ' 当前诊断写入失败；业务处理记录单独保存。'
        status.set(notice)

    def folder():
        try:
            app.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(app.diagnostics_dir))
        except OSError as exc:
            app.error('无法打开诊断日志文件夹', exc)

    ttk.Button(footer, text='刷新', command=refresh).pack(side='left')
    ttk.Button(footer, text='打开日志文件夹', command=folder).pack(side='left', padx=8)
    ttk.Button(footer, text='关闭', command=dialog.destroy).pack(side='right')
    def rewrap(event):
        if event.widget is dialog:
            heading.configure(wraplength=max(250, event.width - 48))
            status_label.configure(wraplength=max(250, event.width - 48))
    dialog.bind('<Configure>', rewrap)
    apply_theme(app.root, app.current_theme)
    refresh()
    app._action_state('diagnostics', '已打开')
