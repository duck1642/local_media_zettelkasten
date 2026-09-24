import argparse
import platform
import subprocess
import sys
from pathlib import Path


def default_triple() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    if system == "windows":
        return f"{arch}-pc-windows-msvc"
    if system == "darwin":
        return f"{arch}-apple-darwin"
    return f"{arch}-unknown-linux-gnu"


def pyinstaller_python() -> str:
    probe = subprocess.run(
        [sys.executable, "-c", "import PyInstaller, fastapi"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return sys.executable

    platform_extra = "windows" if platform.system().lower() == "windows" else "unix"
    python_command = (
        f'& "{sys.executable}"' if platform_extra == "windows" else f'"{sys.executable}"'
    )
    install_command = f'{python_command} -m pip install -e ".[{platform_extra},tauri]"'
    detail = probe.stderr.strip().splitlines()
    cause = f" ({detail[-1]})" if detail else ""
    raise RuntimeError(
        f"The active Python interpreter cannot import the sidecar build dependencies{cause}.\n"
        f"Interpreter: {sys.executable}\n"
        f"Install them with: {install_command}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-triple", default=default_triple())
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    entry = root / "backend" / "web_api.py"
    bin_dir = root / "frontend" / "src-tauri" / "bin"
    work_dir = root / "build" / "tauri-sidecar"
    target_name = f"lmz-api-{args.target_triple}"

    if not entry.exists():
        print(f"missing backend entry: {entry}", file=sys.stderr)
        return 1

    bin_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        builder_python = pyinstaller_python()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    cmd = [
        builder_python,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        target_name,
        "--distpath",
        str(bin_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
    ]
    if args.clean:
        cmd.append("--clean")
    cmd.append(str(entry))

    result = subprocess.run(cmd, cwd=str(root), check=False)
    if result.returncode != 0:
        return result.returncode

    suffix = ".exe" if platform.system().lower() == "windows" else ""
    output = bin_dir / f"{target_name}{suffix}"
    if not output.exists():
        print(f"sidecar build finished but output is missing: {output}", file=sys.stderr)
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
