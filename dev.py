import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

API_HEALTH_URL = "http://127.0.0.1:8000/api/runtime/health"
API_PROTOCOL_VERSION = 1
API_STARTUP_TIMEOUT_SECONDS = 30
API_POLL_INTERVAL_SECONDS = 0.2

def _wait_for_api(api_proc: subprocess.Popen, nonce: str) -> None:
    # Körlemesine sabit süre beklemek yerine backend'in health cevabını doğrula.
    deadline = time.monotonic() + API_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        # Backend daha hazır olmadan kapandıysa Tauri'yi başlatma.
        return_code = api_proc.poll()
        if return_code is not None:
            raise RuntimeError(f"LMZ backend exited before becoming ready (code {return_code})")

        try:
            with urlopen(API_HEALTH_URL, timeout=1) as response:
                payload = json.load(response)
            if (
                isinstance(payload, dict)
                and payload.get("service") == "lmz-api"
                and payload.get("ready") is True
                and payload.get("protocol_version") == API_PROTOCOL_VERSION
                and payload.get("nonce") == nonce
            ):
                return
        except (OSError, ValueError):
            pass

        time.sleep(API_POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"LMZ backend did not become ready within {API_STARTUP_TIMEOUT_SECONDS} seconds"
    )

def _stop_process_tree(process: subprocess.Popen) -> None:
    # Process zaten kapanmışsa tekrar kapatmaya çalışma.
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Daha sonra ayrıca incele: Windows'ta ana süreç ve alt süreçlerin kapanışı.
        # Uvicorn reload alt process oluşturduğu için Windows'ta bütün ağacı kapat.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    # Daha sonra ayrıca incele: Linux/macOS'ta ana süreç ve alt süreçlerin kapanışı.
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()

def run() -> int:
    root = Path(__file__).resolve().parent  # dev.py'nin bulunduğu klasör
    backend_dir = root / "backend" # Backend'in bulunduğu klasör
    api_script = backend_dir / "web_api.py"  # FastAPI kodlarının giriş dosyası
    frontend_dir = root / "frontend"  # Frontend'in bulunduğu klasör
    
    # Nonce (tek kullanımlık rastgele değer), bu başlatmaya ait backend'i ayırt eder.
    nonce = secrets.token_hex(16)
    api_env = os.environ.copy()
    # Backend bu değeri health cevabında geri verir; eşleşme eski/başka bir servisi elememizi sağlar.
    api_env["LMZ_STARTUP_NONCE"] = nonce

    print("Starting LMZ Development Stack...")
    print("--- Starting Python FastAPI (Backend) ---")
    api_proc = subprocess.Popen(  # Yeni bir işletim sistemi süreci başlat.
        [sys.executable, str(api_script)],  # Aktif Python ile web_api.py'yi çalıştır.
        cwd=backend_dir, # Başlatılan backend sürecinin çalışma klasörünü belirtir.
        env=api_env,  # Mevcut ortam değişkenlerini ve bu başlangıcın nonce değerini backend'e aktar.
    )

    try:
        # FastAPI gerçekten hazır olmadan Tauri'yi başlatma.
        _wait_for_api(api_proc, nonce)
        print("[OK] FastAPI backend is ready.")
        print("--- Starting Tauri (Frontend) ---")
        npm_command = "npm.cmd" if os.name == "nt" else "npm"
        tauri_proc = subprocess.Popen(
            [npm_command, "run", "tauri", "dev"],
            cwd=frontend_dir,
        )
        # Tauri kapanana kadar dev.py burada bekler.
        tauri_proc.wait()
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down...")
    except (RuntimeError, TimeoutError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    finally:
        # Normal kapanışta ve hata durumunda backend process ağacını temizle.
        print("--- Cleaning up processes ---")
        _stop_process_tree(api_proc)
        print("[OK] Done.")
    return 0

if __name__ == "__main__":
    raise SystemExit(run())
