"""Lee las credenciales de Meta desde .env (sin dependencias externas).

Uso:
    from meta_config import load_config
    cfg = load_config()          # dict con las variables de .env
    python3 scripts/meta_config.py   # revisa qué variables faltan (no imprime valores)
"""
import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
REQUIRED = ["META_APP_ID", "META_APP_SECRET", "META_GRAPH_VERSION",
            "IG_USER_ID", "IG_ACCESS_TOKEN", "FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"]


def load_config(path=ENV_PATH):
    cfg = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    # Las variables de entorno del sistema tienen prioridad sobre el archivo
    for key in list(cfg) + REQUIRED:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def missing(cfg):
    return [k for k in REQUIRED if not cfg.get(k)]


if __name__ == "__main__":
    cfg = load_config()
    faltan = missing(cfg)
    if not ENV_PATH.exists():
        print(f"No existe {ENV_PATH}. Copia .env.example como .env.")
    elif faltan:
        print("Faltan estas variables en .env:", ", ".join(faltan))
    else:
        print("OK: todas las variables de Meta están definidas.")
