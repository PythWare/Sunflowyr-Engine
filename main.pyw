import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

def report(text):
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Sunflowyr Engine", text, parent=root)
        root.destroy()
    except Exception:
        (HERE / "sunflowyr_error.txt").write_text(text, encoding="utf-8")

def console_python():
    executable = Path(sys.executable)
    console = executable.with_name("python.exe")
    return str(console if executable.name.lower() == "pythonw.exe" and console.exists() else executable)

def start():
    if sys.version_info < (3, 10):
        report("Sunflowyr Engine needs Python 3.10 or newer, this is Python " + sys.version.split()[0] + ".")
        return 1
    missing = []
    for module, package in (("PIL", "Pillow"), ("numpy", "numpy")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        report("Sunflowyr Engine needs " + " and ".join(missing) + ".\n\nInstall with:\n\"" + console_python() + "\" -m pip install " + " ".join(missing))
        return 1
    try:
        from Solar_Logic.gui import main
    except Exception as exc:
        report("Sunflowyr Engine couldnt start:\n\n" + type(exc).__name__ + ": " + str(exc))
        return 1
    return main()

if __name__ == "__main__":
    raise SystemExit(start())
