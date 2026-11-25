"""Start the local Zotero PDF2zh service using this project's Conda runtime."""
import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path
from tkinter import Tk, messagebox


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="启动当前源码目录的 Zotero PDF2zh 翻译服务")
    parser.add_argument("--data-dir", type=Path, help="包含 config 和 translated 的服务数据目录")
    args = parser.parse_args()
    server_dir = ROOT / "server"
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8890)) == 0:
            window = Tk()
            window.withdraw()
            window.attributes("-topmost", True)
            try:
                messagebox.showinfo("Zotero PDF2zh", "本机 8890 端口已在使用。", parent=window)
            finally:
                window.destroy()
            return 1

    # 在接到任务后选择该任务的 API 档案，服务启动只准备 Python 环境。
    env = os.environ.copy()
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
        "ZOTERO_PDF2ZH_DESKTOP": "1",
    })
    # 日志窗口属于服务进程，不继承 Zotero 或开发终端的隐藏控制台。
    # 下次翻译通过 /health 复用同一服务，启动程序可以立即退出。
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 1  # SW_SHOWNORMAL
    subprocess.Popen(
        [str(prefix / "python.exe"), str(server_dir / "server.py"), "--host=127.0.0.1", "--port=8890",
         "--enable_venv=False", "--check_update=False"],
        cwd=server_dir, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=info,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
