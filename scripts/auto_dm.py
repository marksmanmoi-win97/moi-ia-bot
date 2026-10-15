"""Auto-DM propio ("Comenta X y te mando la guía"), sin ManyChat.

Revisa los comentarios recientes de Instagram y de la página de Facebook y, a quien comentó
la palabra del post (TOOLS, MACRO, CV...), le manda por DM el link de la guía (respuesta
privada de Meta) y le contesta en público "te lo mandé por DM".

Corre en GitHub Actions (.github/workflows/auto-dm.yml) en modo bucle: revisa cada 15 s durante
casi 6 h y encadena la siguiente corrida. Sin estado entre corridas: un comentario ya está atendido
si la cuenta ya le respondió en público, así que no duplica (tampoco con ManyChat, que responde
como la misma cuenta). Para gastar poco del límite de Meta, en cada vuelta solo pide la lista de
posts y baja los comentarios de los posts cuyo número de comentarios cambió. Solo stdlib.

Cómo decide la palabra: del pie del post ("Comenta TOOLS..." / "Comment TOOLS...").
Si el pie está en inglés y existe la guía <PALABRA>_EN en docs/guias.csv, manda esa.

Credenciales (GitHub Secrets o .env, ver meta_config.py):
    IG_USER_ID, IG_ACCESS_TOKEN, FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN, META_GRAPH_VERSION

Permisos de Meta: ver README.md. Sin Advanced Access de instagram_manage_messages /
pages_messaging, Meta solo deja mandar DMs a cuentas con rol en la app (testers).

Uso:
    python scripts/auto_dm.py --dry-run          # muestra qué haría, no manda nada
    python scripts/auto_dm.py                    # IG + FB
    python scripts/auto_dm.py --redes ig
    python scripts/auto_dm.py --diagnostico      # permisos del token y nada más
    python scripts/auto_dm.py --cada 15 --duracion 345   # bucle: revisa cada 15 s por 345 min
"""
from __future__ import annotations
import argparse, csv, json, random, re, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from meta_config import load_config  # noqa: E402

GUIAS = ROOT / "docs/guias.csv"
GRAPH = "https://graph.facebook.com"
VENTANA_DIAS = 7       # Meta solo permite la respuesta privada dentro de 7 días del comentario
MAX_ACCIONES = 25      # tope por corrida para no disparar los bloqueos de IG a cuentas nuevas
PAUSA = 3              # segundos entre envíos
MAX_POR_HORA = 40      # tope de comentarios atendidos por hora en modo bucle
FB_CADA = 300          # en modo bucle, Facebook (sin contador de comentarios barato) cada 5 min
USO_ALTO = 75          # % del límite de Meta (cabeceras de uso) a partir del cual frena
HANDLE = "@moi_ali.ia"

# Variantes que la gente escribe y que cuentan como la palabra
ALIAS = {"PROMPT": "PROMPTS", "TOOL": "TOOLS", "TULS": "TOOLS", "MACROS": "MACRO",
         "GUIAS": "GUIA", "INTERVIEW": "ENTREVISTA"}

PUBLICO = {
    "es": ["¡Listo! Revisa tus mensajes 📩", "Te lo mandé por DM 🙌",
           "¡Va! Ya te llegó por mensaje directo 📬", "Enviado, revisa tus DMs 🚀"],
    "en": ["Done! Check your DMs 📩", "Sent it to your inbox 🙌", "On its way to your DMs 🚀"],
}

# Errores de Meta que significan "no tienes permiso": no tiene caso seguir intentando en esta corrida
PERMISO = {3, 10, 190, 200, 230, 294, 299}
# Errores que significan "ese comentario ya tuvo su respuesta privada"
YA_RESPONDIDO = {10900}
# Errores de límite de llamadas de Meta: esperar y volver a intentar
LIMITE = {4, 17, 32, 613, 80001, 80002, 80006}
USO = {"pct": 0}       # mayor % de uso que reportó Meta en la última llamada


class MetaError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        try:
            err = json.loads(body).get("error", {})
        except ValueError:
            err = {}
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        self.msg = err.get("message", body[:300])
        super().__init__(f"{status} code={self.code}/{self.subcode} {self.msg}")


def _req(method, path, params=None, body=None):
    url = path if path.startswith("http") else f"{GRAPH}/{VER}/{path}"
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data, headers = None, {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            _registra_uso(r.headers)
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise MetaError(e.code, e.read().decode(errors="replace")) from None


def _registra_uso(headers):
    """Meta dice qué tan cerca vamos del límite en X-App-Usage / X-Business-Use-Case-Usage."""
    pct = 0
    for h in ("x-app-usage", "x-business-use-case-usage", "x-page-usage"):
        try:
            d = json.loads(headers.get(h) or "{}")
        except ValueError:
            continue
        for v in ([d] if "call_count" in d else [x for l in d.values() for x in l]):
            for k in ("call_count", "total_cputime", "total_time"):
                pct = max(pct, int(v.get(k) or 0))
    USO["pct"] = pct


def _paginado(path, params, max_paginas=5):
    out, url, p = [], path, dict(params)
    for _ in range(max_paginas):
        r = _req("GET", url, p)
        out += r.get("data", [])
        url = r.get("paging", {}).get("next")
        if not url:
            break
        p = None  # el "next" ya trae los parámetros
    return out


# ---------- palabras y guías ----------
def normaliza(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).upper()


def cargar_guias():
    guias = {}
    with GUIAS.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("link") and row.get("acceso", "ok") == "ok":
                guias[normaliza(row["palabra_cta"].strip())] = row
    return guias


RE_CTA = re.compile(r"\b(Comenta|Comment)\s+[*\"“']*([A-Za-zÁÉÍÓÚÑáéíóúñ]+)")


def cta_del_post(caption: str, guias: dict):
    """Devuelve (palabra, idioma, fila_guia) o None si el post no promete guía."""
    caption = caption or ""
    for m in RE_CTA.finditer(caption):
        palabra = ALIAS.get(normaliza(m.group(2)), normaliza(m.group(2)))
        idioma = "en" if m.group(1) == "Comment" else "es"
        if palabra in guias:
            fila = guias.get(f"{palabra}_EN") if idioma == "en" else None
            return palabra, idioma, fila or guias[palabra]
    return None


def comentario_pide(texto: str, palabra: str) -> bool:
    tokens = re.findall(r"[A-Z0-9]+", normaliza(texto or ""))
    return any(ALIAS.get(t, t) == palabra for t in tokens)


def texto_dm(fila, idioma):
    if idioma == "en":
        return (f"Hi! 👋 Here's the guide you asked for: {fila['guia']}\n{fila['link']}\n\n"
                f"Follow {HANDLE} for more AI applied to engineering. Questions? Just reply here.")
    return (f"¡Hola! 👋 Aquí está la guía que pediste: {fila['guia']}\n{fila['link']}\n\n"
            f"Sígueme en {HANDLE} para más IA aplicada a ingeniería. ¿Dudas? Respóndeme por aquí.")


# ---------- envío ----------
def dm_privado(page_id, token, comment_id, texto):
    """Respuesta privada a un comentario (IG o FB) vía la página, con token de página."""
    return _req("POST", f"{page_id}/messages", {"access_token": token},
                {"recipient": {"comment_id": comment_id}, "message": {"text": texto}})


def responder_publico_ig(token, comment_id, texto):
    return _req("POST", f"{comment_id}/replies", {"message": texto, "access_token": token})


def responder_publico_fb(token, comment_id, texto):
    return _req("POST", f"{comment_id}/comments", {"message": texto, "access_token": token})


# ---------- lectura ----------
def pendientes_ig(cfg, guias, desde, estado=None):
    """estado (modo bucle) guarda quién soy y cuántos comentarios tenía cada post: solo se
    bajan los comentarios de los posts que cambiaron desde la vuelta anterior."""
    estado = {} if estado is None else estado
    ig, token = cfg["IG_USER_ID"], cfg["IG_ACCESS_TOKEN"]
    if "yo" not in estado:
        estado["yo"] = _req("GET", ig, {"fields": "id,username", "access_token": token})
    yo, conteo = estado["yo"], estado.setdefault("conteo", {})
    medios = _paginado(f"{ig}/media", {"fields": "id,caption,timestamp,comments_count",
                                       "limit": 25, "access_token": token}, max_paginas=2)
    for m in medios:
        if _fecha(m["timestamp"]) < desde - timedelta(days=60):
            break
        cta = cta_del_post(m.get("caption"), guias)
        if not cta or not m.get("comments_count"):
            continue
        if conteo.get(m["id"]) == m["comments_count"]:
            continue  # sin comentarios nuevos desde la vuelta anterior
        comentarios = _paginado(f"{m['id']}/comments", {
            "fields": "id,text,timestamp,username,from,replies{username,from}",
            "limit": 50, "access_token": token})
        conteo[m["id"]] = m["comments_count"]
        for c in comentarios:
            if _fecha(c["timestamp"]) < desde:
                continue
            if c.get("username") == yo["username"] or c.get("from", {}).get("id") == yo["id"]:
                continue
            respuestas = c.get("replies", {}).get("data", [])
            if any(r.get("username") == yo["username"] or r.get("from", {}).get("id") == yo["id"]
                   for r in respuestas):
                continue  # ya atendido (por este bot, por ManyChat o a mano)
            if comentario_pide(c.get("text"), cta[0]):
                yield {"red": "ig", "comment_id": c["id"], "media_id": m["id"],
                       "palabra": cta[0], "idioma": cta[1], "fila": cta[2]}


def pendientes_fb(cfg, guias, desde, estado=None):
    page, token = cfg["FB_PAGE_ID"], cfg["FB_PAGE_ACCESS_TOKEN"]
    since = int((desde - timedelta(days=60)).timestamp())
    posts = _paginado(f"{page}/posts", {"fields": "id,message,created_time", "since": since,
                                        "limit": 25, "access_token": token}, max_paginas=2)
    try:
        reels = _paginado(f"{page}/video_reels", {"fields": "id,description,created_time",
                                                  "since": since, "limit": 25,
                                                  "access_token": token}, max_paginas=2)
    except MetaError as e:
        print(f"[fb] no pude leer reels: {e}", flush=True)
        reels = []
    objetos = [(p["id"], p.get("message")) for p in posts] + \
              [(r["id"], r.get("description")) for r in reels]
    for obj_id, texto in objetos:
        cta = cta_del_post(texto, guias)
        if not cta:
            continue
        comentarios = _paginado(f"{obj_id}/comments", {
            "fields": "id,message,created_time,from,comments{from}", "filter": "toplevel",
            "limit": 50, "access_token": token})
        for c in comentarios:
            if _fecha(c["created_time"]) < desde:
                continue
            if c.get("from", {}).get("id") == page:
                continue
            if any(r.get("from", {}).get("id") == page
                   for r in c.get("comments", {}).get("data", [])):
                continue
            if comentario_pide(c.get("message"), cta[0]):
                yield {"red": "fb", "comment_id": c["id"], "media_id": obj_id,
                       "palabra": cta[0], "idioma": cta[1], "fila": cta[2]}


def _fecha(s):
    # Meta manda "2026-10-11T20:51:17+0000"
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


# ---------- diagnóstico ----------
def token_pagina(cfg):
    """Si FB_PAGE_ACCESS_TOKEN no es token de página, lo saca solo con el token del usuario
    del sistema (/{FB_PAGE_ID}?fields=access_token). Así basta con pegar el mismo token en los dos."""
    page = cfg.get("FB_PAGE_ID")
    candidatos = [cfg.get("FB_PAGE_ACCESS_TOKEN"), cfg.get("IG_ACCESS_TOKEN")]
    actual = candidatos[0]
    if actual:
        try:
            d = _req("GET", "debug_token", {"input_token": actual, "access_token": actual}).get("data", {})
            if d.get("type") == "PAGE" and d.get("is_valid"):
                return
        except MetaError:
            pass
    for tok in candidatos:
        if not (tok and page):
            continue
        try:
            nuevo = _req("GET", page, {"fields": "access_token", "access_token": tok}).get("access_token")
        except MetaError:
            continue
        if nuevo:
            cfg["FB_PAGE_ACCESS_TOKEN"] = nuevo
            print("FB_PAGE_ACCESS_TOKEN: saqué el token de página a partir del token del usuario del sistema")
            return


def cuenta_ig(cfg):
    """Revisa que IG_USER_ID y el token sirvan para leer la cuenta de IG. Si no, saca el ID de la
    cuenta de IG ligada a la página (instagram_business_account) y prueba con el token de página."""
    ig, page = cfg.get("IG_USER_ID"), cfg.get("FB_PAGE_ID")
    tokens = [("IG_ACCESS_TOKEN", cfg.get("IG_ACCESS_TOKEN")),
              ("token de página", cfg.get("FB_PAGE_ACCESS_TOKEN"))]
    ultimo = None
    for _, tok in tokens:
        if ig and tok:
            try:
                _req("GET", ig, {"fields": "id,username", "access_token": tok})
                cfg["IG_ACCESS_TOKEN"] = tok
                return True
            except MetaError as e:
                ultimo = e
    ligado = None
    for _, tok in tokens:
        if page and tok:
            try:
                ligado = (_req("GET", page, {"fields": "instagram_business_account",
                                             "access_token": tok})
                          .get("instagram_business_account", {}).get("id"))
            except MetaError as e:
                ultimo = e
            if ligado:
                break
    if ligado:
        for nombre, tok in tokens:
            if not tok:
                continue
            try:
                _req("GET", ligado, {"fields": "id,username", "access_token": tok})
            except MetaError as e:
                ultimo = e
                continue
            if ligado != ig:
                print("IG_USER_ID: el del secreto no sirve; uso la cuenta de IG ligada a la página "
                      "(corrige el secreto IG_USER_ID cuando puedas)", flush=True)
            if nombre != "IG_ACCESS_TOKEN":
                print("IG_ACCESS_TOKEN no puede leer la cuenta de IG; uso el token de página", flush=True)
            cfg["IG_USER_ID"], cfg["IG_ACCESS_TOKEN"] = ligado, tok
            return True
    print(f"IG: no pude leer la cuenta de Instagram ({ultimo}). Revisa que la cuenta de IG esté "
          "ligada a la página y asignada al usuario del sistema en Business Suite.", flush=True)
    return False


def diagnostico(cfg):
    for nombre in ("IG_ACCESS_TOKEN", "FB_PAGE_ACCESS_TOKEN"):
        tok = cfg.get(nombre)
        if not tok:
            print(f"{nombre}: no está definido")
            continue
        try:
            d = _req("GET", "debug_token", {"input_token": tok, "access_token": tok}).get("data", {})
        except MetaError as e:
            print(f"{nombre}: error {e}")
            continue
        scopes = set(d.get("scopes", []))
        vence = d.get("expires_at")
        vence = "nunca" if not vence else time.strftime("%Y-%m-%d", time.gmtime(vence))
        print(f"{nombre}: tipo={d.get('type')} válido={d.get('is_valid')} app={d.get('application')} vence={vence}")
        if nombre == "FB_PAGE_ACCESS_TOKEN" and d.get("type") != "PAGE":
            print("   ⚠️ No es token de página y no pude sacarlo solo. Pega en FB_PAGE_ACCESS_TOKEN el mismo "
                  "token del usuario del sistema (el de IG_ACCESS_TOKEN) y revisa que la página esté asignada a ese usuario.")
        for s in ("instagram_basic", "instagram_manage_comments", "instagram_manage_messages",
                  "pages_messaging", "pages_read_engagement", "pages_manage_engagement",
                  "pages_read_user_content", "pages_manage_metadata"):
            print(f"   {'✅' if s in scopes else '❌'} {s}")


# ---------- principal ----------
def atender(cfg, args, guias, redes, estado, cupo):
    """Una vuelta: busca comentarios pendientes y los atiende. Devuelve (atendidos, fallos)."""
    desde = datetime.now(timezone.utc) - timedelta(days=VENTANA_DIAS) + timedelta(hours=1)
    lectores = {"ig": (pendientes_ig, ("IG_USER_ID", "IG_ACCESS_TOKEN")),
                "fb": (pendientes_fb, ("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"))}
    vistos = estado.setdefault("vistos", set())
    acciones, fallos = 0, 0

    for red in redes:
        lector, claves = lectores[red]
        if not all(cfg.get(k) for k in claves) or not cfg.get("FB_PAGE_ID"):
            if not estado.get(f"aviso_{red}"):
                print(f"[{red}] faltan credenciales ({', '.join(claves)}, FB_PAGE_ID): me la salto")
                estado[f"aviso_{red}"] = True
            continue
        if red in estado.get("pausadas", {}) and time.time() < estado["pausadas"][red]:
            continue
        if red == "fb" and args.cada and time.time() - estado.get("fb_ultima", 0) < FB_CADA:
            continue
        estado["fb_ultima" if red == "fb" else "ig_ultima"] = time.time()
        page_token = cfg.get("FB_PAGE_ACCESS_TOKEN") or cfg.get("IG_ACCESS_TOKEN")
        pub_token = cfg["IG_ACCESS_TOKEN"] if red == "ig" else cfg["FB_PAGE_ACCESS_TOKEN"]
        responder = responder_publico_ig if red == "ig" else responder_publico_fb
        try:
            pendientes = [p for p in lector(cfg, guias, desde, estado.setdefault(red, {}))
                          if p["comment_id"] not in vistos]
        except MetaError as e:
            if e.code in LIMITE:
                raise
            print(f"[{red}] no pude leer comentarios: {e}", flush=True)
            fallos += 1
            continue
        if pendientes or not args.cada:
            print(f"[{red}] {len(pendientes)} comentario(s) pendientes", flush=True)

        for p in pendientes:
            if acciones >= min(args.max, cupo):
                print(f"Tope alcanzado ({min(args.max, cupo)}); lo demás sale en la siguiente vuelta.")
                estado.pop(red, None)  # que la siguiente vuelta vuelva a bajar los comentarios
                break
            dm, pub = texto_dm(p["fila"], p["idioma"]), random.choice(PUBLICO[p["idioma"]])
            etiqueta = f"[{red}] comment={p['comment_id']} palabra={p['palabra']} guía={p['fila']['palabra_cta']}"
            vistos.add(p["comment_id"])
            if args.dry_run:
                print(f"{etiqueta} [DRY-RUN] DM + respuesta pública: {pub!r}")
                continue
            acciones += 1
            try:
                dm_privado(cfg["FB_PAGE_ID"], page_token, p["comment_id"], dm)
                print(f"{etiqueta} DM ok", flush=True)
            except MetaError as e:
                if e.code in YA_RESPONDIDO or "already" in e.msg.lower():
                    print(f"{etiqueta} DM ya se había mandado antes", flush=True)
                else:
                    fallos += 1
                    print(f"{etiqueta} DM falló: {e}", flush=True)
                    if e.code in PERMISO or e.code in LIMITE:
                        print(f"[{red}] Meta no deja mandar DMs ahorita ({e.code}). "
                              f"Pauso {red} 30 min. Ver README.md.", flush=True)
                        estado.setdefault("pausadas", {})[red] = time.time() + 1800
                        break
                    continue  # sin DM no decimos en público que se mandó
            try:
                responder(pub_token, p["comment_id"], pub)
            except MetaError as e:
                fallos += 1
                print(f"{etiqueta} respuesta pública falló: {e}", flush=True)
                if e.code in PERMISO or e.code in LIMITE or "2207051" in e.body:
                    print(f"[{red}] Meta bloqueó la acción; pauso {red} 30 min.", flush=True)
                    estado.setdefault("pausadas", {})[red] = time.time() + 1800
                    break
            time.sleep(PAUSA)
    return acciones, fallos


def main(argv=None):
    global VER
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redes", default="ig,fb", help="ig, fb o ig,fb")
    ap.add_argument("--dry-run", action="store_true", help="no manda nada, solo muestra")
    ap.add_argument("--diagnostico", action="store_true", help="revisa permisos del token")
    ap.add_argument("--max", type=int, default=MAX_ACCIONES, help="máximo de comentarios por vuelta")
    ap.add_argument("--cada", type=float, default=0,
                    help="modo bucle: segundos entre revisiones (0 = una sola pasada)")
    ap.add_argument("--duracion", type=float, default=345, help="modo bucle: minutos que dura")
    args = ap.parse_args(argv)

    cfg = load_config()
    VER = cfg.get("META_GRAPH_VERSION") or "v23.0"
    token_pagina(cfg)
    ok_ig = cuenta_ig(cfg)
    if args.diagnostico:
        print(f"IG_USER_ID: {'✅ lee la cuenta de Instagram' if ok_ig else '❌ no lee la cuenta de Instagram'}")
        diagnostico(cfg)
        return 0

    guias = cargar_guias()
    redes = [r.strip() for r in args.redes.split(",") if r.strip()]
    estado = {}
    if not args.cada:
        acciones, fallos = atender(cfg, args, guias, redes, estado, args.max)
        print(f"Listo: {acciones} atendido(s), {fallos} fallo(s).")
        return 1 if fallos and not acciones else 0

    fin = time.time() + args.duracion * 60
    print(f"Modo bucle: reviso cada {args.cada:g} s durante {args.duracion:g} min ({', '.join(redes)}).",
          flush=True)
    total, total_fallos, vueltas, espera_limite = 0, 0, 0, 0
    hechos = []  # momentos de cada comentario atendido, para el tope por hora
    while time.time() < fin:
        inicio = time.time()
        hechos = [t for t in hechos if inicio - t < 3600]
        cupo = MAX_POR_HORA - len(hechos)
        try:
            if cupo > 0:
                a, f = atender(cfg, args, guias, redes, estado, cupo)
                hechos += [time.time()] * a
                total, total_fallos = total + a, total_fallos + f
            espera_limite = 0
        except MetaError as e:  # límite de llamadas de Meta
            espera_limite = min(900, (espera_limite or 60) * 2)
            print(f"Meta pide bajarle ({e.code}); espero {espera_limite // 60} min.", flush=True)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"Error de red: {e}; sigo en la siguiente vuelta.", flush=True)
        vueltas += 1
        if vueltas % 240 == 0:
            print(f"… {vueltas} vueltas, {total} atendido(s), uso de Meta {USO['pct']}%", flush=True)
        espera = espera_limite or args.cada
        if USO["pct"] >= USO_ALTO:
            espera = max(espera, args.cada * (4 if USO["pct"] < 90 else 20))
        time.sleep(max(0, min(espera - (time.time() - inicio), fin - time.time())))

    print(f"Listo: {vueltas} vueltas, {total} atendido(s), {total_fallos} fallo(s).")
    return 0


VER = "v23.0"

if __name__ == "__main__":
    sys.exit(main())
