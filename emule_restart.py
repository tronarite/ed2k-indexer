"""
emule_restart.py
- Reinicia eMule cada 90 minutos
- También reinicia si detecta G:\emuleDescargas\watch\restart.flag
Ejecutar en el host Windows (no en Docker).
"""

import subprocess
import time
import logging
import os

EMULE_EXE    = r"C:\Program Files (x86)\eMule\emule.exe"
FLAG_FILE    = r"G:\emuleDescargas\watch\restart.flag"
INTERVALO    = 90 * 60   # 90 minutos
CHECK_FLAG   = 10        # revisar el flag cada 10 segundos

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("emule_restart.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger()


def matar_emule():
    result = subprocess.run(
        ["taskkill", "/F", "/IM", "emule.exe"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log.info("eMule no estaba corriendo")
    else:
        log.info("eMule terminado")
    time.sleep(3)


def abrir_emule():
    subprocess.Popen([EMULE_EXE])
    log.info(f"eMule iniciado")


def reiniciar(motivo):
    log.info(f"── Reinicio eMule ({motivo}) ──────────────────")
    matar_emule()
    abrir_emule()


if __name__ == "__main__":
    log.info("=== emule_restart.py arrancado ===")
    log.info(f"Intervalo automático: {INTERVALO // 60} min | Flag: {FLAG_FILE}")

    ultimo_reinicio = time.time()

    # Reinicio inicial al arrancar
    reiniciar("arranque")

    while True:
        time.sleep(CHECK_FLAG)

        # Comprobar flag de reinicio pedido por el indexer
        if os.path.exists(FLAG_FILE):
            try:
                os.remove(FLAG_FILE)
            except Exception:
                pass
            log.info("Flag de reinicio detectado desde el indexer")
            reiniciar("flag del indexer")
            ultimo_reinicio = time.time()

        # Reinicio periódico cada 90 minutos
        elif time.time() - ultimo_reinicio >= INTERVALO:
            reiniciar("intervalo periódico")
            ultimo_reinicio = time.time()
