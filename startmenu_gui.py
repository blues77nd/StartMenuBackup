# -*- coding: utf-8 -*-
"""
Windows 11 开始菜单布局 备份/恢复工具（GUI 版）
================================================
功能：
  1. 备份当前开始菜单布局（Start2.bin + 系统版本元信息）
  2. 从备份一键恢复（带版本兼容性警告 + 自动回退点 + 重启 explorer 生效）
  3. 查看/管理备份

技术：
  - customtkinter 现代扁平风界面
  - 布局文件：%LOCALAPPDATA%\\Packages\\Microsoft.Windows.StartMenuExperienceHost_*
              \\LocalState\\Start2.bin
"""

import os
import sys
import json
import shutil
import hashlib
import datetime
import subprocess
import ctypes
import tempfile

from PIL import Image

import customtkinter as ctk

# ---------------------------------------------------------------------------
# 基础常量
# ---------------------------------------------------------------------------
PACKAGE_PREFIX = "Microsoft.Windows.StartMenuExperienceHost_"
PREFERRED_SUFFIX = "cw5n1h2txyewy"
START_BIN_NAME = "Start2.bin"
INFO_FILE_NAME = "info.json"
PINNED_FILE_NAME = "pinned.json"


def resource_path(relative):
    """兼容 PyInstaller 打包后的资源路径。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def load_icon(name, size):
    """从 icons 目录加载 PNG 并转为 CTkImage（超采样，显示清晰）。"""
    path = resource_path(os.path.join("icons", name))
    img = Image.open(path)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


def app_dir():
    """程序所在目录（打包后即 exe 同目录）。备份也存这里，方便拷走。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BACKUP_ROOT = os.path.join(app_dir(), "backups")


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------
def get_os_info():
    import platform
    try:
        sub_key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_key) as k:
            def _get(name):
                try:
                    return str(winreg.QueryValueEx(k, name)[0])
                except OSError:
                    return ""
            display = _get("DisplayVersion")
            cur_build = _get("CurrentBuild")
            ubr = _get("UBR")
    except Exception:
        display, cur_build, ubr = "", "", ""

    os_build = platform.version().split(".")[-1] if platform.version() else ""
    return {
        "captain": f"Windows {platform.release()}",
        "os_build": os_build,
        "display": display or platform.release(),
        "current_build": cur_build or os_build,
        "ubr": ubr,
    }


def get_package_dirs():
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages")
    if not os.path.isdir(base):
        return []
    dirs = [os.path.join(base, d) for d in os.listdir(base)
            if d.startswith(PACKAGE_PREFIX) and os.path.isdir(os.path.join(base, d))]
    return sorted(dirs)


def get_current_startbin():
    """返回当前真正使用的 Start2.bin 完整路径，找不到返回 None。"""
    dirs = get_package_dirs()
    preferred = None
    others = []
    for d in dirs:
        name = os.path.basename(d)
        if name == PACKAGE_PREFIX + PREFERRED_SUFFIX:
            preferred = d
        else:
            others.append(d)
    ordered = ([preferred] if preferred else []) + others

    for d in ordered:
        p = os.path.join(d, "LocalState", START_BIN_NAME)
        if os.path.isfile(p):
            return p
    if ordered:
        return os.path.join(ordered[0], "LocalState", START_BIN_NAME)
    return None


def file_fingerprint(path):
    if not os.path.isfile(path):
        return {"size": 0, "head": ""}
    with open(path, "rb") as f:
        data = f.read()
    head = data[:16].hex(" ").upper()
    return {"size": len(data), "head": head}


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


# ---------------------------------------------------------------------------
# 固定应用清单：收集 / 核对
# ---------------------------------------------------------------------------
# Windows 11 系统内置项（重装后自动存在，标为系统应用，不参与"缺失"判定）
BUILTIN_LNK_NAMES = {
    "Microsoft Edge": "Microsoft Edge（系统浏览器）",
    "File Explorer": "文件资源管理器",
    "Notepad": "记事本",
    "Calculator": "计算器",
    "Snipping Tool": "截图工具",
    "此电脑": "此电脑",
    "Administrative Tools": "管理工具",
    "Windows PowerShell": "Windows PowerShell",
    "Command Prompt": "命令提示符",
    "Settings": "设置",
    "Control Panel": "控制面板",
}


def _run_export_start_layout(dest_json):
    """调用系统 Export-StartLayout 导出固定项。返回 (ok, errmsg)。"""
    ps_script = (
        "$p = '" + dest_json.replace("'", "''") + "'; "
        + "Export-StartLayout -Path $p -ErrorAction SilentlyContinue; "
        + "if (Test-Path $p) { exit 0 } else { exit 1 }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=40,
        )
    except Exception as e:
        return False, f"调用系统导出失败：{e}"
    if r.returncode != 0 or not os.path.isfile(dest_json):
        return False, f"Export-StartLayout 失败（rc={r.returncode}）：{r.stderr.strip()[:200]}"
    return True, ""


def collect_pinned_list():
    """收集当前系统开始菜单的固定应用清单。返回 (items, errmsg)。"""
    tmp = os.path.join(tempfile.gettempdir(), "startmenu_pinned_tmp.json")
    if os.path.isfile(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    ok, err = _run_export_start_layout(tmp)
    if not ok:
        return [], err
    try:
        raw = open(tmp, "rb").read()
        text = raw.decode("utf-8-sig", errors="ignore").strip()
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    if not text.startswith("{"):
        return [], f"导出结果不是预期的 JSON 格式（可能是旧版 XML），无法解析固定项。\n首字符：{text[:1]}"

    try:
        data = json.loads(text)
    except Exception as e:
        return [], f"解析导出的 JSON 失败：{e}"

    pinned = data.get("pinnedList", [])
    items = []
    for p in pinned:
        if "desktopAppLink" in p:
            link = p["desktopAppLink"]
            exp = os.path.expandvars(link)
            name = os.path.splitext(os.path.basename(link))[0]
            is_builtin = name in BUILTIN_LNK_NAMES
            items.append({
                "type": "lnk",
                "name": BUILTIN_LNK_NAMES.get(name, name),
                "raw_name": name,
                "link": link,
                "expanded": exp,
                "builtin": is_builtin,
            })
        elif "desktopAppID" in p:
            aumid = p["desktopAppID"]
            items.append({
                "type": "aumid",
                "name": aumid,
                "raw_name": aumid,
                "link": aumid,
                "expanded": "",
                "builtin": aumid.startswith("Microsoft.")
                or "Windows" in aumid or aumid.startswith("MicrosoftWindows"),
            })
    return items, ""


def check_pinned_status(items):
    """核对清单：返回 (missing, existing, unsure)。每项为 dict。"""
    missing, existing, unsure = [], [], []
    for it in items:
        if it["type"] == "aumid":
            unsure.append(it)
            continue
        if it.get("builtin"):
            # 系统内置项：存在则算 existing，但单独标记
            exists = os.path.isfile(it["expanded"])
            tag = dict(it)
            tag["builtin"] = True
            (existing if exists else missing).append(tag)
            continue
        # 第三方 .lnk
        if os.path.isfile(it["expanded"]):
            existing.append(it)
        else:
            missing.append(it)
    return missing, existing, unsure


# ---------------------------------------------------------------------------
# 备份操作
# ---------------------------------------------------------------------------
def do_backup():
    src = get_current_startbin()
    if not src:
        return False, "未找到开始菜单包目录，可能不是 Windows 11。"
    if not os.path.isfile(src):
        return False, f"未找到布局文件：\n{src}\n请先打开一次开始菜单再备份。"

    osi = get_os_info()
    fp = file_fingerprint(src)
    md5 = md5_of(src)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = f"{stamp}_build{osi['current_build']}"
    dest = os.path.join(BACKUP_ROOT, folder)
    os.makedirs(dest, exist_ok=True)

    shutil.copy2(src, os.path.join(dest, START_BIN_NAME))

    # 顺便收集固定应用清单（供重装后核对缺失）
    pinned_items, pinned_err = collect_pinned_list()
    pinned_count = 0
    third_party_count = 0
    if pinned_err:
        pinned_note = f"清单收集失败：{pinned_err}"
    else:
        pinned_count = len(pinned_items)
        third_party_count = sum(1 for i in pinned_items if not i.get("builtin"))
        pinned_note = ""
        with open(os.path.join(dest, PINNED_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump({
                "collected_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "items": pinned_items,
            }, f, ensure_ascii=False, indent=2)

    info = {
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "backed_from": src,
        "os_captain": osi["captain"],
        "os_build": osi["os_build"],
        "display_version": osi["display"],
        "current_build": osi["current_build"],
        "ubr": osi["ubr"],
        "file_size": fp["size"],
        "md5": md5,
        "head_hex": fp["head"],
        "package_full_name": os.path.basename(os.path.dirname(os.path.dirname(src))),
        "pinned_count": pinned_count,
        "third_party_count": third_party_count,
        "pinned_note": pinned_note,
    }
    with open(os.path.join(dest, INFO_FILE_NAME), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    msg = (f"备份成功！\n\n保存位置：\n{dest}\n"
           f"布局文件：{fp['size']} 字节\n"
           f"系统：{osi['captain']}  Build {osi['current_build']}.{osi['ubr']}（{osi['display']}）\n"
           f"固定应用：共 {pinned_count} 项（第三方 {third_party_count} 项）")
    if pinned_err:
        msg += f"\n\n（应用清单未生成：{pinned_err}）"
    return True, msg


def list_backups():
    """返回备份列表（新→旧），每项为 dict。"""
    if not os.path.isdir(BACKUP_ROOT):
        return []
    items = []
    for d in sorted(os.listdir(BACKUP_ROOT), reverse=True):
        full = os.path.join(BACKUP_ROOT, d)
        if not os.path.isdir(full):
            continue
        bin_path = os.path.join(full, START_BIN_NAME)
        info_path = os.path.join(full, INFO_FILE_NAME)
        info = {}
        if os.path.isfile(info_path):
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except Exception:
                info = {}
        size = os.path.getsize(bin_path) if os.path.isfile(bin_path) else 0
        items.append({
            "folder": full,
            "name": d,
            "size": size,
            "info": info,
        })
    return items


def do_restore(folder_path):
    """从指定备份目录恢复。返回 (ok, message)。"""
    backup_bin = os.path.join(folder_path, START_BIN_NAME)
    if not os.path.isfile(backup_bin):
        return False, f"备份目录缺少 {START_BIN_NAME}：\n{folder_path}"

    osi = get_os_info()
    target = get_current_startbin()
    if not target:
        return False, "本机未找到开始菜单包目录，无法恢复。"
    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)

    # 读取备份元信息
    info = {}
    info_path = os.path.join(folder_path, INFO_FILE_NAME)
    if os.path.isfile(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception:
            info = {}

    # —— 跨版本兼容性检查 ——
    warns = []
    if info:
        bk = str(info.get("current_build", ""))
        cd = str(osi["current_build"])
        if bk and cd and bk != cd:
            warns.append(f"备份来自 Build {bk}.{info.get('ubr','')}（{info.get('display_version','')}）\n"
                         f"当前系统为 Build {cd}.{osi['ubr']}（{osi['display']}）")
        elif info.get("display_version") and info["display_version"] != osi["display"]:
            warns.append(f"备份来自 {info['display_version']}，当前系统为 {osi['display']}")

    # —— 恢复前备份当前状态（回退点）——
    if os.path.isfile(target):
        rollback = os.path.join(BACKUP_ROOT, "rollback_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(rollback, exist_ok=True)
        shutil.copy2(target, os.path.join(rollback, START_BIN_NAME))
        rb_info = {
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "backed_from": target,
            "os_captain": osi["captain"],
            "display_version": osi["display"],
            "current_build": osi["current_build"],
            "ubr": osi["ubr"],
            "note": "恢复前自动保存的当前布局（用于回退）",
        }
        with open(os.path.join(rollback, INFO_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump(rb_info, f, ensure_ascii=False, indent=2)

    # —— 结束开始菜单进程 ——
    _stop_process(["StartMenuExperienceHost"])

    # —— 覆盖写入 ——
    try:
        shutil.copy2(backup_bin, target)
    except Exception as e:
        return False, f"写入失败：{e}"

    # —— 重启 explorer ——
    _restart_explorer()

    msg = "恢复完成！\n\n开始菜单布局已生效。"
    if warns:
        msg += "\n\n版本提醒：\n" + "\n".join(warns) + "\n（本次为强制恢复，若布局异常请从回退点恢复）"
    return True, msg


def _stop_process(names):
    try:
        cmd = ["taskkill", "/F", "/IM"]
        for n in names:
            subprocess.run(cmd + [n + ".exe"], capture_output=True)
    except Exception:
        pass


def do_check_missing(folder_path):
    """读取备份里的固定应用清单，核对当前系统缺了哪些。返回 (ok, message)。"""
    pinned_path = os.path.join(folder_path, PINNED_FILE_NAME)
    if not os.path.isfile(pinned_path):
        return False, "该备份没有应用清单（可能是较旧的备份，备份时未收集）。\n\n请重新做一次备份，新备份会自动附带应用清单。"

    try:
        with open(pinned_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", [])
    except Exception as e:
        return False, f"读取应用清单失败：{e}"

    if not items:
        return False, "该备份的应用清单为空。"

    missing, existing, unsure = check_pinned_status(items)

    lines = []
    lines.append(f"备份中共有 {len(items)} 个固定应用。")
    lines.append("")

    if missing:
        lines.append(f"❌ 缺失（{len(missing)} 个，重装后需重新安装）：")
        for it in missing:
            if it.get("builtin"):
                lines.append(f"   · {it['name']}（系统项，异常缺失）")
            else:
                lines.append(f"   · {it['name']}")
                lines.append(f"       原路径：{it['link']}")
        lines.append("")
    else:
        lines.append("✅ 没有缺失的第三方应用。")
        lines.append("")

    if unsure:
        lines.append(f"⚠️ 需手动确认（{len(unsure)} 个，商店 UWP 应用）：")
        for it in unsure:
            lines.append(f"   · {it['name']}")
        lines.append("")

    if existing:
        lines.append(f"✅ 已存在（{len(existing)} 个）。")

    return True, "\n".join(lines)


def _restart_explorer():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "explorer.exe"], capture_output=True)
        import time
        time.sleep(1)
        subprocess.Popen(["explorer.exe"])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("开始菜单布局 备份与恢复")
        self.geometry("720x560")
        self.minsize(680, 520)

        # 加载图标
        self.icons = {
            "app": load_icon("app.png", 24),
            "backup": load_icon("backup.png", 20),
            "restore": load_icon("restore.png", 20),
            "folder": load_icon("folder.png", 20),
            "missing": load_icon("missing.png", 20),
        }

        # 设置窗口 / 任务栏图标
        try:
            ico_path = resource_path(os.path.join("icons", "app_icon.ico"))
            self.iconbitmap(ico_path)
        except Exception:
            pass

        self._build_ui()
        self.refresh_list()

    def _build_ui(self):
        # 顶部标题栏（标题 + 应用图标）
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 4))
        head_row = ctk.CTkFrame(header, fg_color="transparent")
        head_row.pack(anchor="w")
        ctk.CTkLabel(
            head_row, text="", image=self.icons["app"],
        ).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(
            head_row, text="开始菜单布局 · 备份与恢复",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text="一键备份 / 恢复 Windows 11 开始菜单磁贴布局（Start2.bin）",
            font=ctk.CTkFont(size=13),
            text_color=("#666666", "#9aa0a6"),
        ).pack(anchor="w", pady=(4, 0))

        # 系统信息条
        osi = get_os_info()
        info_bar = ctk.CTkFrame(self, fg_color=("gray90", "#2b2b2b"), corner_radius=10)
        info_bar.pack(fill="x", padx=24, pady=(10, 4))
        ctk.CTkLabel(
            info_bar,
            text=f"  当前系统：{osi['captain']}   Build {osi['current_build']}.{osi['ubr']}（{osi['display']}）   "
                 f"用户：{os.environ.get('USERNAME','')}",
            font=ctk.CTkFont(size=12),
            text_color=("gray30", "#d0d0d0"),
        ).pack(anchor="w", padx=12, pady=8)

        # 主操作按钮区
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(12, 6))

        self.btn_backup = ctk.CTkButton(
            actions, text="备份当前布局", height=52,
            image=self.icons["backup"],
            font=ctk.CTkFont(size=16, weight="bold"),
            command=self.on_backup,
        )
        self.btn_backup.grid(row=0, column=0, padx=(0, 10), pady=(0, 10), sticky="ew")

        self.btn_restore = ctk.CTkButton(
            actions, text="恢复所选备份", height=52,
            image=self.icons["restore"],
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="#1f6feb", hover_color="#1a5fd0",
            command=self.on_restore,
        )
        self.btn_restore.grid(row=0, column=1, padx=0, pady=(0, 10), sticky="ew")

        self.btn_check = ctk.CTkButton(
            actions, text="检查缺失应用", height=48,
            image=self.icons["missing"],
            font=ctk.CTkFont(size=15),
            fg_color="transparent", border_width=2,
            text_color=("#1f6feb", "#7ab3ff"),
            border_color=("#1f6feb", "#3a6ea5"),
            hover_color=("#e6f1fb", "#243a52"),
            command=self.on_check_missing,
        )
        self.btn_check.grid(row=1, column=0, padx=(0, 10), sticky="ew")

        self.btn_open = ctk.CTkButton(
            actions, text="打开备份目录", height=48,
            image=self.icons["folder"],
            font=ctk.CTkFont(size=15),
            fg_color="transparent", border_width=2,
            text_color=("gray20", "#e0e0e0"),
            border_color=("gray40", "#5a5a5a"),
            hover_color=("gray85", "#3a3a3a"),
            command=self.on_open_dir,
        )
        self.btn_open.grid(row=1, column=1, sticky="ew")
        actions.grid_columnconfigure((0, 1), weight=1)

        # 备份列表
        list_head = ctk.CTkFrame(self, fg_color="transparent")
        list_head.pack(fill="x", padx=24, pady=(14, 2))
        ctk.CTkLabel(
            list_head, text="备份记录",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(side="left")
        self.lbl_count = ctk.CTkLabel(
            list_head, text="", font=ctk.CTkFont(size=12),
            text_color=("gray50", "#9aa0a6"),
        )
        self.lbl_count.pack(side="right")

        self.list_frame = ctk.CTkScrollableFrame(self, height=220)
        self.list_frame.pack(fill="both", expand=True, padx=24, pady=(4, 8))

        # 底部状态栏
        self.status = ctk.CTkLabel(
            self, text="就绪", font=ctk.CTkFont(size=12),
            text_color=("gray40", "#8a8a8a"), anchor="w",
        )
        self.status.pack(fill="x", padx=24, pady=(2, 16))

    # ---- 事件 ----
    def on_backup(self):
        self.set_busy(True)
        self.after(50, self._run_backup)

    def _run_backup(self):
        ok, msg = do_backup()
        self.set_busy(False)
        if ok:
            self.show_dialog("备份成功", msg)
        else:
            self.show_dialog("备份失败", msg)
        self.refresh_list()

    def on_restore(self):
        sel = self.get_selected()
        if not sel:
            self.set_status("请先在列表中选择一个备份", warn=True)
            return
        folder = sel["folder"]

        # 跨版本提示
        osi = get_os_info()
        info = sel["info"]
        extra = ""
        if info:
            bk = str(info.get("current_build", ""))
            cd = str(osi["current_build"])
            if bk and cd and bk != cd:
                extra = (f"\n\n注意：该备份来自 Build {bk}.{info.get('ubr','')}（{info.get('display_version','')}），"
                         f"当前是 Build {cd}.{osi['ubr']}（{osi['display']}），跨版本可能失效。")
        self.ask_yes_no(
            "确认恢复",
            f"将从以下备份恢复布局：\n\n{sel['name']}\n备份时间：{info.get('created_at','未知')}\n文件大小：{sel['size']} 字节"
            f"{extra}\n\n恢复会：\n· 自动备份当前布局作为回退点\n· 结束开始菜单进程并重启资源管理器",
            on_yes=lambda: self._do_restore(folder),
            confirm_text="确认恢复",
        )

    def _do_restore(self, folder):
        self.set_busy(True)
        self.after(50, lambda: self._run_restore(folder))

    def _run_restore(self, folder):
        ok, msg = do_restore(folder)
        self.set_busy(False)
        if ok:
            self.show_dialog("恢复完成", msg)
        else:
            self.show_dialog("恢复失败", msg)
        self.refresh_list()

    def on_open_dir(self):
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        try:
            os.startfile(BACKUP_ROOT)  # noqa
        except Exception:
            subprocess.Popen(["explorer", BACKUP_ROOT], shell=True)

    def on_check_missing(self):
        sel = self.get_selected()
        if not sel:
            # 未选则默认用最新一个备份
            items = list_backups()
            if not items:
                self.set_status("还没有任何备份，无法检查", warn=True)
                return
            sel = items[0]
        self.set_busy(True)
        self.after(50, lambda: self._run_check_missing(sel["folder"]))

    def _run_check_missing(self, folder):
        self.set_busy(False)
        ok, msg = do_check_missing(folder)
        if ok:
            self.show_result("应用检查结果", msg)
        else:
            self.show_dialog("无法检查", msg)

    # ---- 列表渲染 ----
    def refresh_list(self):
        for w in self.list_frame.winfo_children():
            w.destroy()

        items = list_backups()
        self.lbl_count.configure(text=f"共 {len(items)} 个备份")
        self._selected = None

        if not items:
            ctk.CTkLabel(
                self.list_frame,
                text="暂无备份，点击上方「备份当前布局」创建第一个备份",
                font=ctk.CTkFont(size=13),
                text_color=("gray50", "#9aa0a6"),
            ).pack(pady=40)
            return

        for it in items:
            info = it["info"]
            created = info.get("created_at", "未知时间")
            size_txt = f"{it['size']} 字节" if it["size"] else ""
            ver_txt = f"{info.get('display_version','')}  Build {info.get('current_build','')}.{info.get('ubr','')}"
            if info.get("current_build"):
                pass
            subtitle = f"{created}    {ver_txt}    {size_txt}"
            row = self._make_row(it, subtitle)
            row.pack(fill="x", padx=2, pady=3)

    # 选中色（深色主题下的高亮蓝）与未选中色
    SEL_FG = "#4493f8"   # 选中行背景
    SEL_BORDER = "#7ab3ff"
    NORMAL_FG = ("gray90", "#2b2b2b")  # 未选中行背景（light, dark）

    def _make_row(self, item, subtitle):
        is_rollback = item["name"].startswith("rollback_")
        label_prefix = "回退点" if is_rollback else "备 份"

        frame = ctk.CTkFrame(
            self.list_frame, corner_radius=8,
            fg_color=self.NORMAL_FG, border_width=0, border_color=self.SEL_BORDER,
        )

        inner = ctk.CTkFrame(frame, fg_color="transparent")

        badge = ctk.CTkLabel(
            inner, text=label_prefix, width=72,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#1f6feb", "#4493f8"),
            anchor="w",
        )
        badge.pack(side="left", padx=(12, 4), pady=10)

        txt = ctk.CTkLabel(
            inner, text=f"{item['name']}\n{subtitle}",
            font=ctk.CTkFont(size=12), justify="left", anchor="w",
            text_color=("gray15", "#d5d5d5"),
        )
        txt.pack(side="left", pady=10)

        # 删除按钮（行右侧）
        del_btn = ctk.CTkButton(
            inner, text="删除", width=56, height=30,
            font=ctk.CTkFont(size=12),
            fg_color="transparent", border_width=1,
            text_color=("#a32d2d", "#ff8a80"),
            border_color=("#c0392b", "#e24b4a"),
            hover_color=("#fcebeb", "#3a2b2b"),
            command=lambda i=item: self.on_delete_row(i),
        )
        del_btn.pack(side="right", padx=(4, 12), pady=10)

        inner.pack(fill="x", expand=True)

        # 缓存这行内部的元素引用，便于选中态整体变色
        frame._item = item
        frame._badge = badge
        frame._txt = txt
        frame._inner = inner

        # 点击事件（删除按钮不绑定，避免与 command 冲突）
        for w in (frame, inner, badge, txt):
            w.bind("<Button-1>", lambda e, f=frame, i=item: self.select_row(f, i))
        return frame

    def select_row(self, frame, item):
        self._selected = item
        # 遍历所有行，切换选中/未选中外观（整行填充 + 文字反色）
        for w in self.list_frame.winfo_children():
            try:
                is_this = getattr(w, "_item", None) is item
                if is_this:
                    w.configure(fg_color=self.SEL_FG, border_color=self.SEL_BORDER, border_width=2)
                    w._badge.configure(text_color="#ffffff")
                    w._txt.configure(text_color="#ffffff")
                else:
                    w.configure(fg_color=self.NORMAL_FG, border_color=self.SEL_BORDER, border_width=0)
                    w._badge.configure(text_color=("#1f6feb", "#4493f8"))
                    w._txt.configure(text_color=("gray15", "#d5d5d5"))
            except Exception:
                pass
        self.set_status(f"已选中：{item['name']}")

    def get_selected(self):
        return getattr(self, "_selected", None)

    # ---- 删除 ----
    def on_delete_row(self, item):
        folder = item["folder"]
        self.ask_yes_no(
            "确认删除",
            f"确定要删除这个备份吗？\n\n{item['name']}\n\n"
            f"删除后不可恢复（不影响当前开始菜单布局，只删除这份备份数据）。",
            on_yes=lambda: self._do_delete(item),
            confirm_text="删除",
            danger=True,
        )

    def _do_delete(self, item):
        folder = item["folder"]
        try:
            shutil.rmtree(folder)
            self.set_status(f"已删除：{item['name']}")
        except Exception as e:
            self.show_dialog("删除失败", f"无法删除备份目录：\n{folder}\n\n{e}")
        finally:
            self.refresh_list()

    # ---- 对话框 / 状态 ----
    def show_dialog(self, title, msg):
        dlg = ctk.CTkToplevel(self)
        dlg.title(title.split(" ", 1)[-1] if " " in title else title)
        dlg.geometry("480x300")
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)

        ctk.CTkLabel(
            dlg, text=title, font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(pady=(20, 4))
        ctk.CTkLabel(
            dlg, text=msg, font=ctk.CTkFont(size=13), justify="left", wraplength=420,
        ).pack(padx=24, pady=8, fill="both", expand=True)
        ctk.CTkButton(
            dlg, text="确定", width=120, height=38,
            command=dlg.destroy,
        ).pack(pady=(0, 20))

    def show_result(self, title, msg):
        """带滚动文本框的结果窗口（适合较长的应用清单）。"""
        dlg = ctk.CTkToplevel(self)
        dlg.title(title)
        dlg.geometry("600x480")
        dlg.minsize(500, 400)
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)

        ctk.CTkLabel(
            dlg, text=title, font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(pady=(20, 8))

        box = ctk.CTkTextbox(
            dlg, font=ctk.CTkFont(size=13), wrap="word",
            fg_color=("gray95", "#1e1e1e"),
        )
        box.pack(padx=20, pady=(0, 12), fill="both", expand=True)
        box.insert("1.0", msg)
        box.configure(state="disabled")

        ctk.CTkButton(
            dlg, text="关闭", width=120, height=38,
            command=dlg.destroy,
        ).pack(pady=(0, 18))

    def ask_yes_no(self, title, msg, on_yes, confirm_text="确认", danger=False):
        dlg = ctk.CTkToplevel(self)
        dlg.title(title)
        dlg.geometry("520x330")
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)

        ctk.CTkLabel(
            dlg, text=title,
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color=("#a32d2d", "#ff6b6b") if danger else ("gray15", "#f0f0f0"),
        ).pack(pady=(20, 4))
        ctk.CTkLabel(
            dlg, text=msg, font=ctk.CTkFont(size=13), justify="left", wraplength=440,
        ).pack(padx=24, pady=8, fill="both", expand=True)

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=(0, 20))
        ctk.CTkButton(
            btn_row, text="取消", width=120, height=38,
            fg_color="transparent", border_width=2,
            text_color=("gray20", "#e0e0e0"), border_color=("gray40", "#5a5a5a"),
            hover_color=("gray85", "#3a3a3a"),
            command=dlg.destroy,
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            btn_row, text=confirm_text, width=120, height=38,
            fg_color=("#c0392b", "#e24b4a") if danger else None,
            hover_color=("#a93226", "#c73a39") if danger else None,
            command=lambda: (dlg.destroy(), on_yes()),
        ).pack(side="left", padx=8)

    def set_status(self, text, warn=False):
        self.status.configure(
            text=text,
            text_color=("#c0392b", "#ff6b6b") if warn else ("gray40", "#8a8a8a"),
        )

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.btn_backup.configure(state=state)
        self.btn_restore.configure(state=state)
        self.btn_open.configure(state=state)
        self.btn_check.configure(state=state)
        if busy:
            self.set_status("处理中，请稍候…")
        else:
            self.set_status("就绪")


def main():
    # Windows 高分屏
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()