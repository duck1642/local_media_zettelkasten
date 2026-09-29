import os
import sys

from api.app import app


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()

    import uvicorn

    if getattr(sys, "frozen", False):
        uvicorn.run(app, host="127.0.0.1", port=8000)
    else:
        # "modül:özellik" biçimi api.app modülünü yükleyip içindeki app nesnesini seçer.
        # Metin biçimi, Uvicorn'un uygulamayı reload sürecinde yeniden içe aktarmasını sağlar.
        uvicorn.run(
            "api.app:app",
            host="127.0.0.1",
            port=8000,
            reload=os.getenv("LMZ_DISABLE_RELOAD") != "1",
        )
