"""Start the local Zotero PDF2zh service using this project's Conda runtime."""
import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path
from tkinter import Tk, messagebox, simpledialog

import pywintypes
import win32cred


ROOT = Path(__file__).resolve().parent
TARGET = "Zotero-PDF2zh/OpenAI"


def api_key(window):
    try:
        entry = win32cred.CredRead(TARGET, win32cred.CRED_TYPE_GENERIC)
    except pywintypes.error as error:
        if error.winerror != 1168:
            raise
    else:
        return entry["CredentialBlob"].decode("utf-16-le")

    # 首次启动由用户本地输入，后续从本项目专用的 Windows 凭据读取。
    key = simpledialog.askstring(
        "Zotero PDF2zh · OpenAI",
        "请输入 OpenAI API Key，保存到此 Windows 用户的凭据管理器。\n"
        "服务地址 api.openai.com，模型使用 Zotero PDF2zh 中激活的 OpenAI 配置。\n"
        "点击确定仅启动本地服务。关闭服务控制台即可停止。",
        show="*", parent=window,
    )
    if not key or not key.strip():
        return None
    key = key.strip()
    try:
        # pywin32 312 在写入时接收 str，读回时返回 UTF-16LE 字节。
        win32cred.CredWrite({
            "Type": win32cred.CRED_TYPE_GENERIC, "TargetName": TARGET,
            "UserName": "OpenAI", "CredentialBlob": key,
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        }, 0)
    except (pywintypes.error, TypeError) as error:
        messagebox.showerror(
            "Zotero PDF2zh", "Windows 凭据保存失败，服务未启动。\n"
            f"错误类型：{type(error).__name__}", parent=window,
        )
        return None
    return key


def main():
    parser = argparse.ArgumentParser(description="启动当前源码目录的 Zotero PDF2zh 翻译服务")
    parser.add_argument("--data-dir", type=Path, help="包含 config 和 translated 的服务数据目录")
    args = parser.parse_args()
    server_dir = ROOT / "server"
    window = Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8890)) == 0:
            messagebox.showinfo("Zotero PDF2zh", "本机 8890 端口已在使用。", parent=window)
            return 1
    key = api_key(window)
    window.destroy()
    if not key:
        return 0

    # 模型由插件在每个翻译任务中指定；环境变量会覆盖任务配置。
    env = os.environ.copy()
    env.pop("PDF2ZH_OPENAI_MODEL", None)
    prefix = Path(sys.executable).resolve().parent
    if args.data_dir is not None:
        env["ZOTERO_PDF2ZH_DATA_DIR"] = str(args.data_dir.resolve())
    # 在启动时记录修订号；后续提交只有在服务重启后才会加载。
    env.pop("ZOTERO_PDF2ZH_SOURCE_REVISION", None)
    if (ROOT / ".git").exists():
        try:
            env["ZOTERO_PDF2ZH_SOURCE_REVISION"] = subprocess.check_output(
                ["git", "describe", "--always", "--dirty"], cwd=ROOT,
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    env.update({
        "PATH": os.pathsep.join([str(prefix), str(prefix / "Scripts"),
                                 str(prefix / "Library" / "bin"), env["PATH"]]),
        "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
        "PDF2ZH_OPENAI_API_KEY": key,
        "PDF2ZH_OPENAI_BASE_URL": "https://api.openai.com/v1",
        "PDF2ZH_OPENAI_SEND_TEMPRATURE": "false",
    })
    return subprocess.call(
        [sys.executable, str(server_dir / "server.py"), "--host=127.0.0.1", "--port=8890",
         "--enable_venv=False", "--check_update=False"],
        cwd=server_dir, env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
