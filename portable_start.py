"""Start the shared copy with its bundled Python and OCR runtime."""
import os
from pathlib import Path
import runpy
import sys
import traceback


def main():
    base = Path(__file__).resolve().parent
    os.chdir(base)
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["TESSERACT_CMD"] = str(base / "runtime/ocr/tesseract.exe")
    # Tesseract's data-file loader cannot reliably open non-ASCII absolute paths.
    os.environ["TESSDATA_PREFIX"] = "runtime/ocr/tessdata"
    sys.path.insert(0, str(base))
    try:
        runpy.run_path(str(base / "ui.py"), run_name="__main__")
    except Exception:
        error_path = base / "startup-error.log"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("启动失败", f"请将压缩包完整解压后重试。\n错误记录：{error_path}")
        root.destroy()
        raise


if __name__ == "__main__":
    main()
