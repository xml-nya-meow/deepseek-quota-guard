"""Windows desktop workflow. All Tk and ledger access stays on the UI thread.

The browser adapter owns its own worker thread. The UI persists a deletion
intent before granting a single-use permit; unknown outcomes are never retried.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import os
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Any, Callable

from .core import DataStore, Person

TZ = timezone(timedelta(hours=8))
POLL_MS = 300_000
STATUS_LABELS = {
    "skipped": "跳过管理", "unknown": "等待有效费用", "ok": "尚未达到额度",
    "triggered": "达到额度 · 待处理", "prepared": "待你在平台确认",
    "pending": "待处理", "absent_verified": "平台列表已不存在 · 原因需核实",
    "failed": "处理失败", "unknown_result": "处理结果待核实",
    "retired": "已从平台列表移除 · 保留账目",
}


def amount_text(value: Any) -> str:
    """Display full supplied precision, rather than hide a threshold crossing."""
    if value is None:
        return "未知"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "未知"
        return format(number, "f")
    except (ValueError, InvalidOperation):
        return "未知"


def safe_error(exc: Exception | str) -> str:
    # External errors can contain credentials. Never put raw error URLs in logs.
    text = str(exc)
    text = re.sub(r"sk-[A-Za-z0-9_*.-]+", "[密钥已隐藏]", text)
    text = re.sub(r"(?i)(authorization|cookie|access_token|refresh_token)\s*[:=]\s*[^\s,;]+", r"\1=[已隐藏]", text)
    return text[:1200]


def has_saved_data(folder: Path) -> bool:
    """A leftover ledger/log is history, not permission to initialize again."""
    if not folder.exists():
        return False
    names = {"管理设置.json", "人员额度.xlsx", "状态.db", "状态.db-wal", "状态.db-shm"}
    for item in folder.iterdir():
        if item.name in names or item.name.startswith(("运行日志-", "运行摘要-", ".initial", ".初始化", "初始化")):
            return True
    return False


def parse_editor_person(name: str, amount: str) -> Person:
    name, amount = name.strip(), amount.strip()
    if not amount or amount.startswith("="):
        raise ValueError("个人金额必须填写数字；暂不管理填 -1。")
    try:
        number = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError("个人金额不是有效数字。") from exc
    if not number.is_finite() or (number < 0 and number != -1):
        raise ValueError("金额只允许 -1、0 或正数。")
    if number != -1 and not name:
        raise ValueError("金额为 0 或正数时，必须填写 API 名/人员名。")
    return Person(name, number)


class App:
    def __init__(self, root: tk.Tk, folder: Path):
        self.root = root
        self.folder = Path(folder).resolve()
        from .instance import FolderLock
        self.folder_lock = FolderLock(self.folder)
        self.store: DataStore | None = None
        self.config = None
        self.browser = None
        self.busy = False
        self.observing = False
        self.automatic = False
        self.delete_permit = None
        self.retry_collection = True
        self.closing = False
        self.timer = None
        self.rows: list[dict] = []
        self.last_keys: list[dict] = []
        self.validated_hash: str | None = None
        self.root.title("DeepSeek 额度管理 · 候选预发布")
        self.root.geometry("1120x740")
        self.root.minsize(930, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.root.after(60, self.load_folder)

    def _build(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=31, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Muted.TLabel", foreground="#526174")
        style.configure("Warn.TLabel", foreground="#915212")
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="DeepSeek 额度管理", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="核对平台费用 · 按人员核算 · 保留处理记录", style="Muted.TLabel").pack(anchor="w", pady=(3, 12))
        self.path_var = tk.StringVar(value=f"数据文件夹：{self.folder}")
        ttk.Label(outer, textvariable=self.path_var, wraplength=1040).pack(anchor="w")
        self.summary_var = tk.StringVar(value="正在检查已有数据……")
        ttk.Label(outer, textvariable=self.summary_var, wraplength=1040).pack(anchor="w", pady=(8, 8))
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 7))
        self.buttons = {}
        specs = [
            ("folder", "数据文件夹", self.choose_folder),
            ("setup", "首次设置", self.setup),
            ("edit", "填写两列表格", self.edit_people),
            ("excel", "用 Excel/WPS 打开", self.open_excel),
            ("check", "检查表格", self.check_people),
            ("import", "检查官方导出", self.import_billing),
        ]
        for key, text, command in specs:
            b = ttk.Button(bar, text=text, command=command)
            b.pack(side="left", padx=(0, 7))
            self.buttons[key] = b
        bar2 = ttk.Frame(outer)
        bar2.pack(fill="x", pady=(0, 10))
        for key, text, command in [
            ("login", "登录 DeepSeek", self.login),
            ("collect", "立即读取费用", self.collect),
            ("start", "启动自动管理", self.start),
            ("stop", "停止管理", self.stop),
            ("revoke", "手动处理选中人员", self.prepare_revoke),
            ("verify", "核实平台状态", self.verify_revoke),
        ]:
            b = ttk.Button(bar2, text=text, command=command)
            b.pack(side="left", padx=(0, 7))
            self.buttons[key] = b
        ttk.Label(outer, text="“立即读取”只核对数据；启动自动管理后，达到任一额度将永久撤销对应 Key。账单延迟与在途请求仍可能超支。真实自动撤销尚待验证。", style="Warn.TLabel", wraplength=1040).pack(anchor="w", pady=(0, 10))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        overview = ttk.Frame(tabs, padding=8)
        reports = ttk.Frame(tabs, padding=8)
        tabs.add(overview, text="人员与额度")
        tabs.add(reports, text="处理汇报")
        columns = ("name", "amount", "share", "spent", "status")
        self.tree = ttk.Treeview(overview, columns=columns, show="headings", selectmode="browse")
        for col, title, width in zip(columns, ["API名 / 人员名", "个人额度（元）", "公共额度（元）", "累计费用（元）", "当前状态"], [190, 150, 150, 150, 270]):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=90, anchor="w")
        scroll = ttk.Scrollbar(overview, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("triggered", foreground="#a52323")
        self.tree.tag_configure("unknown", foreground="#94600a")
        self.tree.tag_configure("skipped", foreground="#758192")
        self.tree.bind("<<TreeviewSelect>>", self._selection)
        self.detail_var = tk.StringVar(value="-1：跳过管理　　0：零额度，进入停用流程　　正数：达到个人或公共额度即触发")
        ttk.Label(overview, textvariable=self.detail_var, wraplength=1000).pack(anchor="w", pady=(8, 0))
        report_bar = ttk.Frame(reports)
        report_bar.pack(fill="x", pady=(0, 6))
        ttk.Button(report_bar, text="刷新汇报", command=self.refresh_events).pack(side="left")
        ttk.Button(report_bar, text="打开日志文件夹", command=self.open_folder).pack(side="left", padx=8)
        self.report_text = tk.Text(reports, wrap="word", state="disabled", relief="flat", bg="#f6f8fa", padx=12, pady=10)
        report_scroll = ttk.Scrollbar(reports, command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=report_scroll.set)
        report_scroll.pack(side="right", fill="y")
        self.report_text.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="尚未启动观察")
        ttk.Label(outer, textvariable=self.status_var, wraplength=1040).pack(anchor="w", pady=(10, 0))
        self._update_buttons()

    def _update_buttons(self):
        ready = self.config is not None and not self.busy and not self.closing
        for key, button in self.buttons.items():
            enabled = ready
            if key in {"folder", "setup"}:
                enabled = not self.busy and not self.closing and not self.observing
            if key == "setup":
                enabled = enabled and self.config is None
            if key == "stop":
                enabled = self.observing and not self.closing
            if key == "start":
                enabled = ready and not self.observing
            if key in {"edit", "excel", "check"}:
                enabled = ready and not self.observing
            if key in {"revoke", "verify"}:
                enabled = ready and not self.observing
            button.configure(state="normal" if enabled else "disabled")

    def error(self, title: str, exc: Exception | str):
        self.status_var.set(f"{title}：{safe_error(exc)}")
        if not self.closing:
            messagebox.showerror(title, safe_error(exc), parent=self.root)

    def load_folder(self):
        self.config = None
        self.validated_hash = None
        self.path_var.set(f"数据文件夹：{self.folder}")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            existing = has_saved_data(self.folder)
            self.store = DataStore(self.folder)
            if not existing:
                self.summary_var.set("首次运行：先填写人数和总预算，然后生成两列表格。")
                self.status_var.set("没有既有记录。请选择“首次设置”，或打开已有数据文件夹。")
                self._update_buttons()
                return
            self.config = self.store.load()
            self._show_config()
            self.refresh_rows()
            self.refresh_events()
            self.status_var.set("已恢复原有统计。请检查表格并读取平台费用；不会自动执行任何删除。")
        except Exception as exc:
            self.summary_var.set("已有数据需要核查；未覆盖文件，未清零统计。")
            self.error("无法恢复数据", exc)
        self._update_buttons()

    def _show_config(self):
        c = self.config
        self.summary_var.set(f"分配人数 {c.people_count}　｜　本期总预算 ¥{amount_text(c.total_budget)}　｜　公共人均额度 ¥{amount_text(c.share)}　｜　起点 {c.start_date}（UTC+8）")

    def choose_folder(self):
        path = filedialog.askdirectory(title="选择已有数据文件夹，或一个新的空文件夹", initialdir=str(self.folder), parent=self.root)
        if not path or Path(path).resolve() == self.folder:
            return
        if self.browser is not None:
            messagebox.showinfo("请重新打开程序", "浏览器会话已绑定当前数据文件夹。请关闭本程序后，从目标文件夹重新启动或在登录前切换目录。", parent=self.root)
            return
        from .instance import FolderLock
        try:
            new_lock = FolderLock(Path(path).resolve())
        except Exception as exc:
            self.error('无法打开目录', exc)
            return
        self.folder_lock.close()
        self.folder_lock = new_lock
        self.folder = Path(path).resolve()
        self.tree.delete(*self.tree.get_children())
        self.rows = []
        self.load_folder()

    def setup(self):
        if has_saved_data(self.folder):
            self.error("不能重新初始化", "此文件夹已有设置、统计或日志。请恢复原记录，或选择新的空数据文件夹。")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("首次设置")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="设置本期预算", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 15))
        values = [tk.StringVar(value="14"), tk.StringVar(value="1500"), tk.StringVar(value=datetime.now(TZ).strftime("%Y-%m-%d"))]
        for row, (label, value) in enumerate(zip(["分配人数", "本期总预算（元）", "统计起点（YYYY-MM-DD）"], values), 1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=7, padx=(0, 18))
            ttk.Entry(body, textvariable=value, width=24).grid(row=row, column=1)
        ttk.Label(body, text="公共额度按总预算 ÷ 人数向下取整到元。\n起点应覆盖本期已有消费，不会默认清零。\n新表金额均为 -1，填写并检查后才纳入管理。", wraplength=420).grid(row=4, column=0, columnspan=2, pady=15, sticky="w")

        def create():
            try:
                n = int(values[0].get())
                budget = Decimal(values[1].get().strip())
                start = values[2].get().strip()
                if n <= 0 or not budget.is_finite() or budget < 0:
                    raise ValueError("人数必须为正整数，总预算必须为非负金额。")
                if datetime.strptime(start, "%Y-%m-%d").date() > datetime.now(TZ).date():
                    raise ValueError("统计起点不能在未来。")
                if budget == 0 and not messagebox.askyesno("零总预算", "公共额度将为 0。所有纳入管理的人员都会进入停用流程。仍按此预算生成表格？", parent=dialog):
                    return
                self.config = self.store.initialize(n, budget, start)
            except Exception as exc:
                messagebox.showerror("创建失败", safe_error(exc), parent=dialog)
                return
            dialog.destroy()
            self._show_config()
            self.refresh_rows()
            self._update_buttons()
            self.status_var.set("已生成表格。请填写并保存，再检查数据。")
            self.edit_people()

        ttk.Button(body, text="生成表格", command=create).grid(row=5, column=0, sticky="w")
        ttk.Button(body, text="取消", command=dialog.destroy).grid(row=5, column=1, sticky="e")

    def open_excel(self):
        try:
            os.startfile(str(self.folder / "人员额度.xlsx"))
        except OSError:
            messagebox.showinfo("可使用内置表格", "没有可用的 Excel/WPS 打开方式也没关系，请点击“填写两列表格”。", parent=self.root)

    def import_billing(self):
        if not self.config:
            return
        path = filedialog.askopenfilename(title='选择官方 ZIP 或其中一个 CSV', filetypes=[('官方导出', '*.zip *.csv')], parent=self.root)
        if path:
            try:
                from .billing import inspect_export
                result = inspect_export(path, self.config.start_date)
                messagebox.showinfo('导出检查', result['message'] + f"\n用量记录 {result['amount_rows']} 行，费用记录 {result['cost_rows']} 行。", parent=self.root)
            except Exception as exc:
                self.error('导出检查未通过', exc)

    def open_folder(self):
        try:
            os.startfile(str(self.folder))
        except OSError as exc:
            self.error("无法打开文件夹", exc)

    def edit_people(self):
        try:
            expected_hash = self.store.workbook_hash()
            people = self.store.read_people()
        except Exception as exc:
            self.error("无法读取表格", exc)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("填写两列表格")
        dialog.geometry("720x580")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="-1 跳过　｜　0 零额度，需停用　｜　正数为个人累计额度", wraplength=670).pack(anchor="w", pady=(0, 10))
        table = ttk.Treeview(body, columns=("name", "amount"), show="headings", selectmode="browse")
        table.heading("name", text="API名/人员名")
        table.heading("amount", text="个人金额（元）")
        table.column("name", width=370)
        table.column("amount", width=180)
        sb = ttk.Scrollbar(body, command=table.yview)
        table.configure(yscrollcommand=sb.set)
        table.pack(fill="both", expand=True, side="top")
        for p in people:
            table.insert("", "end", values=(p.name, amount_text(p.amount)))
        if not people:
            table.insert("", "end", values=("", "-1"))
        form = ttk.Frame(body)
        form.pack(fill="x", pady=12)
        name_var, amount_var = tk.StringVar(), tk.StringVar(value="-1")
        ttk.Label(form, text="名字").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=name_var, width=30).grid(row=1, column=0, padx=(0, 15))
        ttk.Label(form, text="个人金额（元）").grid(row=0, column=1, sticky="w")
        ttk.Entry(form, textvariable=amount_var, width=18).grid(row=1, column=1)

        def select(_event=None):
            chosen = table.selection()
            if chosen:
                name, amount = table.item(chosen[0], "values")
                name_var.set(name)
                amount_var.set(amount)

        def apply_row(new=False):
            try:
                p = parse_editor_person(name_var.get(), amount_var.get())
            except ValueError as exc:
                messagebox.showerror("请检查这一行", str(exc), parent=dialog)
                return
            chosen = table.selection()
            if chosen and not new:
                table.item(chosen[0], values=(p.name, str(p.amount)))
            else:
                table.selection_set(table.insert("", "end", values=(p.name, str(p.amount))))

        def remove():
            for item in table.selection():
                table.delete(item)
            name_var.set("")
            amount_var.set("-1")

        def save():
            try:
                result = [parse_editor_person(*table.item(item, "values")) for item in table.get_children()]
                zeros = [p.name for p in result if p.amount == 0]
                zero_note = f"其中 {len(zeros)} 人为 0 元：后续核对 Key 后会进入停用流程。\n" if zeros else ""
                if not messagebox.askyesno("保存并检查", zero_note + "保存这份表格？运行中的 Key 不会因为保存表格就被删除。", parent=dialog):
                    return
                self.store.save_people(result, expected_hash=expected_hash)
                self.validated_hash = None
            except Exception as exc:
                messagebox.showerror("未保存", safe_error(exc), parent=dialog)
                return
            dialog.destroy()
            self.check_people()

        table.bind("<<TreeviewSelect>>", select)
        operations = ttk.Frame(body)
        operations.pack(fill="x", pady=(0, 10))
        ttk.Button(operations, text="更新选中行", command=apply_row).pack(side="left")
        ttk.Button(operations, text="添加新行", command=lambda: apply_row(True)).pack(side="left", padx=8)
        ttk.Button(operations, text="移除选中行", command=remove).pack(side="left")
        ttk.Label(body, text="修改输入框后先点击“更新选中行”，最后保存整张表。外部 Excel 同时改动时将拒绝覆盖。", wraplength=670, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Button(body, text="保存表格并检查", command=save).pack(side="left")
        ttk.Button(body, text="取消", command=dialog.destroy).pack(side="right")

    def check_people(self, show_dialog=True) -> bool:
        try:
            people = self.store.read_people()
            self.refresh_rows()
            count = sum(p.amount != -1 for p in people)
            zeros = sum(p.amount == 0 for p in people)
            self.validated_hash = self.store.workbook_hash()
            message = f"表格可读：待管理 {count} 人，其中零额度 {zeros} 人。\n文件检查通过不等于平台费用已核实，请继续读取平台。"
            if count == 0:
                message = "当前没有待管理 API。填写姓名和金额后再启动观察。"
            self.status_var.set(message.replace("\n", " "))
            if show_dialog:
                messagebox.showinfo("表格检查", message, parent=self.root)
            return count > 0
        except Exception as exc:
            self.validated_hash = None
            self.stop(quiet=True)
            self.error("表格检查未通过", exc)
            return False

    def refresh_rows(self):
        if not self.store or not self.config:
            return
        self.rows = self.store.evaluate()
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            status = row.get("status", "unknown")
            amount = "-1（跳过）" if str(row.get("amount")) == "-1" else amount_text(row.get("amount"))
            spent = amount_text(row.get("spent"))
            if row.get('spent_is_lower_bound') and row.get('spent') is not None:
                spent = '≥' + spent + '（待补全）'
            self.tree.insert("", "end", iid=str(i), values=(row.get("name") or "（待填写）", amount, amount_text(row.get("share", self.config.share)), spent, STATUS_LABELS.get(status, status)), tags=(status,))

    def refresh_events(self):
        if not self.store or not self.config:
            return
        try:
            events = self.store.events(limit=200)
            lines = []
            for event in events:
                # Field names remain visible to avoid suppressing new audit fields.
                lines.append(json.dumps(event, ensure_ascii=False, default=str, indent=2))
            self.report_text.configure(state="normal")
            self.report_text.delete("1.0", "end")
            self.report_text.insert("end", "\n\n".join(lines) if lines else "暂时没有处理记录。日志保存在当前数据文件夹。")
            self.report_text.configure(state="disabled")
        except Exception as exc:
            self.status_var.set(f"汇报读取失败：{safe_error(exc)}")

    def _selection(self, _event=None):
        row = self.selected()
        if not row:
            return
        reason = row.get("reasons") or []
        if isinstance(reason, list):
            reason = "；".join(map(str, reason))
        self.detail_var.set(f"{row.get('name', '')}：{reason or STATUS_LABELS.get(row.get('status'), '等待检查')}。平台标识：{row.get('tracking_id') or '尚未绑定'}")

    def selected(self) -> dict | None:
        chosen = self.tree.selection()
        return self.rows[int(chosen[0])] if chosen else None

    def _browser_action(self, action: str, on_success: Callable, on_error: Callable | None = None, on_progress: Callable | None = None, **kwargs):
        if self.busy or self.closing:
            return
        try:
            if self.browser is None:
                from .browser_service import BrowserService
                self.browser = BrowserService()
            future = self.browser.submit(action, **kwargs)
        except Exception as exc:
            self.stop(quiet=True)
            if on_error:
                on_error(exc)
            self.error("浏览器不可用", exc)
            return
        self.busy = True
        self.status_var.set("正在操作平台，请勿在同一浏览器窗口同时切换账户或修改筛选……")
        self._update_buttons()

        def poll():
            if not future.done():
                if on_progress:
                    try:
                        on_progress()
                    except Exception as exc:
                        self.stop(quiet=True)
                        self.status_var.set('撤销授权未完成：' + safe_error(exc))
                self.root.after(100, poll)
                return
            self.busy = False
            try:
                on_success(future.result())
            except Exception as exc:
                self.stop(quiet=True)
                if on_error:
                    try:
                        on_error(exc)
                    except Exception as log_exc:
                        self.error("处理记录写入失败", log_exc)
                if not self.closing:
                    self.error("本次操作未完成", exc)
            finally:
                self._update_buttons()
        self.root.after(100, poll)

    def login(self):
        def done(_result):
            self.status_var.set("已打开独立浏览器。请自行登录 DeepSeek，完成后点击“立即读取费用”。")
        self._browser_action("login", done)

    def collect(self):
        if self.busy or not self.config:
            return
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if not self.check_people(show_dialog=False):
            self.stop(quiet=True)
            return
        checked_hash = self.validated_hash

        def done(result):
            if self.store.workbook_hash() != checked_hash:
                raise ValueError("读取平台期间表格发生变化。本次结果未应用，请重新检查。")
            self.store.bind_keys(result["keys"], account_id=result["account_id"])
            if result.get('totals') is None:
                self.retry_collection = bool(result.get('retryable'))
                self.last_keys = result['keys']
                self.refresh_rows()
                self.refresh_events()
                self.status_var.set('Key 已核对，费用尚未就绪：' + result.get('message', '未取得费用'))
                if self.observing:
                    self._schedule_processing()
                return
            self.retry_collection = True
            self.store.apply_snapshot(result["totals"], result["source_id"], result["start_date"], result["end_date"])
            self.last_keys = result["keys"]
            self.refresh_rows()
            self.refresh_events()
            now = datetime.now(TZ).strftime("%H:%M:%S")
            reached = sum(row.get("status") == "triggered" for row in self.rows)
            unknown = sum(row.get('status') == 'unknown' for row in self.rows)
            self.status_var.set(f"最近采集 {now}（UTC+8）｜{reached} 人达到额度，{unknown} 人费用未完整。" + ("下次检查约 5 分钟后。" if self.observing else "定时观察未启动。"))
            if reached:
                self.root.bell()
            if self.observing:
                self._schedule_processing()
        self._browser_action("collect", done, start_date=self.config.start_date,
                             account_id=self.store.load().account_id,
                             known_keys=self.store.active_tracking_ids())

    def start(self):
        if self.busy or self.observing or not self.check_people(show_dialog=False):
            return
        managed = [p.name for p in self.store.read_people() if p.amount >= 0]
        if not messagebox.askyesno('启动自动管理',
            '管理人员：' + '、'.join(managed) + '\n\n达到个人额度或公共额度（含 0 元）时，将永久撤销对应 Key。-1 和表格外的 Key 不参与。\n'
            '撤销不可恢复；账单延迟与在途请求可能导致超支。遇到身份、表格或结果不明时会暂停。\n\n启动本次自动管理？', parent=self.root):
            return
        self.observing = True
        self.automatic = True
        self._update_buttons()
        self.collect()

    def stop(self, quiet=False):
        self.observing = False
        self.automatic = False
        if self.delete_permit is not None:
            self.delete_permit.decide(False)
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if not quiet:
            self.status_var.set("管理已停止；已提交的撤销不能取消，结果仍会核实并记入日志。其余 Key 保持原状态。")
        self._update_buttons()

    def _process_next(self):
        if not self.automatic or not self.observing or self.closing:
            return
        if self.store.workbook_hash() != self.validated_hash:
            self.stop(quiet=True)
            raise ValueError('表格已变化，自动管理暂停，请检查后重新启动。')
        self.refresh_rows()
        targets = [row for row in self.rows if row['status'] == 'triggered' and row.get('tracking_id')]
        if not targets:
            if self.retry_collection:
                self.timer = self.root.after(POLL_MS, self.collect)
            else:
                self.stop(quiet=True)
                self.status_var.set('已处理可确定达到额度的人员；费用来源未通过核验，管理暂停，请检查平台日期范围或结构。')
            return
        row = targets[0]
        previous = self.store.latest_action(row['tracking_id'])
        if previous in {'pending', 'requested', 'unknown_result', 'unknown', 'failed', 'prepared', 'confirmed'}:
            self.stop(quiet=True)
            self.status_var.set(f"{row['name']} 上次操作尚未核实。请选中该人员，点击“核实平台状态”，再决定是否重新启动。")
            return
        from .browser_service import DeletePermit
        permit = DeletePermit()
        self.delete_permit = permit
        checked_hash = self.validated_hash
        self.store.record_action(row['name'], row['tracking_id'], 'pending', '达到额度，准备自动撤销；尚未点击最终确认')
        verified_absence = False

        def authorize():
            if not permit.ready.is_set() or permit.decided.is_set():
                return
            if self.closing or not self.automatic or self.store.workbook_hash() != checked_hash:
                permit.decide(False)
                return
            current = next((r for r in self.store.evaluate() if r['name'] == row['name']), None)
            if not current or current['status'] != 'triggered' or current['tracking_id'] != row['tracking_id']:
                permit.decide(False)
                return
            # Commit before the worker is allowed to click, including after a crash.
            self.store.record_action(row['name'], row['tracking_id'], 'requested', '目标与额度已复核，批准本次撤销；最终结果仍待平台核实')
            permit.decide(True)

        def done(result):
            nonlocal verified_absence
            self.delete_permit = None
            if result.get('cancelled'):
                self.store.record_action(row['name'], row['tracking_id'], 'cancelled', '最终确认前已取消，未执行删除')
                self.stop(quiet=True)
            elif result.get('absent') is True:
                verified_absence = True
                self.store.record_action(row['name'], row['tracking_id'], 'absent_verified', '自动撤销后，已核实平台列表中不再存在此 Tracking ID；在途费用仍可能入账')
            else:
                raise ValueError('未取得明确撤销结果，停止且不自动重试。')
            self.refresh_rows()
            self.refresh_events()
            self._schedule_processing()

        def failed(exc):
            permit.decide(False)
            self.delete_permit = None
            if verified_absence:
                # A local write/display error cannot change an observed outcome.
                self.status_var.set('平台已核实 Key 不存在，但记录保存或显示失败，请保留数据并重新核实。')
                return
            self.store.record_action(row['name'], row['tracking_id'], 'unknown_result', '自动处理未完成或结果不明，禁止自动重试：' + safe_error(exc))
            self.refresh_events()

        self._browser_action('revoke', done, on_error=failed, on_progress=authorize,
                             name=row['name'], tracking_id=row['tracking_id'], permit=permit)

    def _schedule_processing(self):
        def advance():
            try:
                self._process_next()
            except Exception as exc:
                self.stop(quiet=True)
                if not self.closing:
                    self.error('自动管理已暂停', exc)
        self.root.after(0, advance)

    def prepare_revoke(self):
        self.stop(quiet=True)
        if not self.config or self.store.workbook_hash() != self.validated_hash:
            messagebox.showinfo('请重新检查', '表格已变更或尚未校验，请检查并重新读取平台。', parent=self.root)
            return
        row = self.selected()
        if not row or row.get("status") != "triggered" or not row.get("tracking_id"):
            messagebox.showinfo("先选择待处理人员", "请选择已绑定平台 Key 且达到额度的人员。若状态未知，请先读取费用。", parent=self.root)
            return
        if not messagebox.askyesno("打开平台处理页面", f"准备处理 {row['name']} 的 API Key。\n删除 Key 后不能恢复原 Key，后续需要发新 Key。\n\n本程序定位对应列表行，由你核对后自行点击删除及确认。继续？", parent=self.root):
            return
        try:
            self.store.record_action(row["name"], row["tracking_id"], "pending", "用户请求定位平台 Key 列表行；未执行删除")
        except Exception as exc:
            self.error("未执行操作：日志无法保存", exc)
            return
        self.refresh_events()

        def done(_result):
            self.store.record_action(row["name"], row["tracking_id"], "prepared", "已定位平台 Key 列表，等待用户自行点击删除及确认；未确认删除结果")
            self.refresh_events()
            self.status_var.set("请在 DeepSeek 页面核对并自行确认或取消。随后点击“核实平台状态”。")
        def failed(exc):
            self.store.record_action(row["name"], row["tracking_id"], "unknown_result", "定位列表行未完成，未执行删除：" + safe_error(exc))
            self.refresh_events()
        self._browser_action("prepare_revoke", done, on_error=failed, name=row["name"], tracking_id=row["tracking_id"])

    def verify_revoke(self):
        row = self.selected()
        if not row or not row.get("tracking_id"):
            messagebox.showinfo("先选择人员", "请选择已有平台绑定的人员。", parent=self.root)
            return

        verified_absence = False
        def done(result):
            nonlocal verified_absence
            absent = isinstance(result, dict) and result.get("absent") is True
            verified_absence = absent
            present = result.get('present') is True
            state = "absent_verified" if absent else ("cancelled" if present else "unknown_result")
            detail = "平台列表中已不存在该 Tracking ID；不据此认定由本程序删除，也不代表在途请求费用取消" if absent else ("已核实原 Key 仍存在；尚未停用。可检查后重新启动管理。" if present else "未确认 Key 已从平台消失，请检查平台状态；不能认为已经停用")
            self.store.record_action(row["name"], row["tracking_id"], state, detail)
            self.refresh_rows()
            self.refresh_events()
            self.status_var.set(detail)
            if not self.closing:
                messagebox.showinfo("平台状态核实", detail, parent=self.root)
        def failed(exc):
            if verified_absence:
                return
            self.store.record_action(row["name"], row["tracking_id"], "unknown_result", "无法核实平台结果：" + safe_error(exc))
            self.refresh_events()
        self._browser_action("verify_revoke", done, on_error=failed, name=row["name"], tracking_id=row["tracking_id"], known_keys=self.store.active_tracking_ids())

    def close(self):
        if self.closing:
            return
        self.stop(quiet=True)
        self.closing = True
        self._update_buttons()
        if self.browser is None:
            self.folder_lock.close()
            self.root.destroy()
            return
        self.status_var.set("正在保存当前操作结果并关闭独立浏览器，请稍候……")
        finished = threading.Event()

        def shutdown():
            try:
                self.browser.close()
            finally:
                finished.set()
        def begin_shutdown():
            if self.busy:
                self.root.after(100, begin_shutdown)
                return
            threading.Thread(target=shutdown, name="browser-close", daemon=True).start()
        begin_shutdown()

        def wait_close():
            if finished.is_set():
                self.folder_lock.close()
                self.root.destroy()
            else:
                self.root.after(150, wait_close)
        self.root.after(150, wait_close)


def run(folder: Path):
    root = tk.Tk()
    App(root, folder)
    root.mainloop()
