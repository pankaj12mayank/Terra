"""
Terra Cinema & Bulk Image Downloader - Master Launcher
One-command startup for serving the Web UI, running image downloads,
and managing project assets.
"""

import os
import sys
import shutil
import socket
import argparse
import webbrowser
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, List

REQUIRED_MODULES = [
    "fastapi",
    "uvicorn",
    "pydantic",
    "aiohttp",
    "aiofiles",
    "pandas",
    "tqdm",
    "PIL",
    "dotenv"
]


def get_project_dir() -> str:
    """Return absolute path to the Image-Scripts directory."""
    return os.path.dirname(os.path.abspath(__file__))


def ensure_environment() -> None:
    """Verify required packages and .env file existence."""
    project_dir = get_project_dir()
    env_file = os.path.join(project_dir, ".env")
    env_example = os.path.join(project_dir, ".env.example")

    if not os.path.isfile(env_file) and os.path.isfile(env_example):
        try:
            shutil.copyfile(env_example, env_file)
            print("[INFO] Created .env configuration from .env.example")
        except Exception:
            pass

    # Check dependencies
    missing = []
    for mod in REQUIRED_MODULES:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)

    if missing:
        print(f"[SETUP] Missing dependencies detected: {', '.join(missing)}")
        req_file = os.path.join(project_dir, "requirements.txt")
        if os.path.isfile(req_file):
            print("[SETUP] Installing requirements...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file], check=False)


def find_free_port(starting_port: int = 8000, max_attempts: int = 20) -> int:
    """Find an available port starting from starting_port."""
    for port in range(starting_port, starting_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            result = sock.connect_ex(("127.0.0.1", port))
            if result != 0:
                return port
    return starting_port


def count_cached_images() -> int:
    """Count existing valid WebP images."""
    img_dir = os.path.join(get_project_dir(), "country_images")
    if not os.path.isdir(img_dir):
        return 0
    return sum(1 for f in os.listdir(img_dir) if f.endswith(".webp") and os.path.getsize(os.path.join(img_dir, f)) > 0)


def serve_ui(port: Optional[int] = None, open_browser: bool = True) -> None:
    """Start local API server and open the cinematic web studio."""
    project_dir = get_project_dir()
    os.chdir(project_dir)

    assigned_port = port or find_free_port(8000)
    url = f"http://localhost:{assigned_port}"

    print("\n" + "=" * 56)
    print("     TERRA CINEMA • UNIVERSAL IMAGE & ADMIN STUDIO      ")
    print("=" * 56)
    print(f"  Local Server:  http://localhost:{assigned_port}")
    print(f"  API Docs:      http://localhost:{assigned_port}/docs")
    print(f"  Web Studio:    {url}")
    print(f"  Cached Images: {count_cached_images()} / 194 Destinations")
    print("=" * 56)
    print("  Serving API & Web Studio. Press Ctrl+C to stop.\n")

    if open_browser:
        import threading
        import time

        def _launch() -> None:
            time.sleep(0.8)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_launch, daemon=True).start()

    try:
        import uvicorn
        uvicorn.run("server:app", host="127.0.0.1", port=assigned_port, log_level="warning")
    except ImportError:
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", assigned_port), QuietHandler)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[INFO] Server stopped gracefully.")
        finally:
            httpd.server_close()


def run_downloader_cli(args_list: List[str]) -> None:
    """Invoke the bulk image downloader."""
    downloader_script = os.path.join(get_project_dir(), "image_downloader.py")
    subprocess.run([sys.executable, downloader_script] + args_list, check=False)


def open_direct_html() -> None:
    """Open index.html directly in browser via file protocol."""
    html_file = os.path.join(get_project_dir(), "index.html")
    print(f"[INFO] Opening {html_file} in default browser...")
    webbrowser.open(f"file:///{os.path.abspath(html_file)}")


def show_interactive_menu() -> None:
    """Display interactive CLI options."""
    cached = count_cached_images()
    print("\n" + "=" * 50)
    print("    TERRA CINEMA • ONE-COMMAND WORKSPACE    ")
    print("=" * 50)
    print(f"  Status: {cached} / 194 destinations cached in WebP")
    print("-" * 50)
    print("  [1] Launch Cinematic Web UI (Local HTTP Server)")
    print("  [2] Open HTML UI directly in Browser")
    print("  [3] Run Bulk Image Downloader (image_downloader.py)")
    print("  [4] Run Downloader Dry-Run (Audit missing)")
    print("  [5] Exit")
    print("=" * 50)

    try:
        choice = input("Select an option [Default: 1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nExiting.")
        return

    if choice in ("", "1"):
        serve_ui()
    elif choice == "2":
        open_direct_html()
    elif choice == "3":
        run_downloader_cli([])
    elif choice == "4":
        run_downloader_cli(["--dry-run"])
    elif choice == "5":
        print("Goodbye!")
    else:
        print("Invalid choice. Starting Web UI by default...")
        serve_ui()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-command launcher for Terra Cinema UI and bulk downloader."
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch the Cinematic Web Gallery on local server (default behavior)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for local HTTP server (default: first available from 8000)"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start HTTP server without automatically opening browser"
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Open index.html directly without starting an HTTP server"
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Run image downloader immediately"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Audit destinations without downloading"
    )
    parser.add_argument(
        "--menu",
        action="store_true",
        help="Show interactive launcher menu"
    )
    return parser.parse_args()


def main() -> None:
    # Ensure current working directory is always the project directory
    os.chdir(get_project_dir())
    args = parse_arguments()
    ensure_environment()

    # Determine command flow
    if args.menu:
        show_interactive_menu()
    elif args.download:
        run_downloader_cli([])
    elif args.dry_run:
        run_downloader_cli(["--dry-run"])
    elif args.direct:
        open_direct_html()
    else:
        # Default flow: Start web server and open UI
        serve_ui(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
