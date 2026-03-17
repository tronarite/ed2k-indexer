"""
Indexer scraper - Estructura real verificada
- Busqueda: /?view=advsearch&q=TITULO
- Pagina pelicula: /?view=title&id=XXXX&uid=YYYY
- Links: javascript:Download(ID, CODE)
- AJAX: /ajax/download.php?id=ID&code=CODE&nocache=RANDOM
- Respuesta: <textarea id="ELINKSLIST">ed2k://...</textarea>
"""

import requests
from bs4 import BeautifulSoup
import re
import os
import random
import sys

# ── Configuracion ─────────────────────────────────────────────────────────────
BASE     = "https://www.indexer.org"
USERNAME = os.environ.get("INDEXER_USER", "")
PASSWORD = os.environ.get("INDEXER_PASS", "")
# ──────────────────────────────────────────────────────────────────────────────


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
        "Accept-Language": "es-ES,es;q=0.9",
    })
    return s


def login(session: requests.Session) -> bool:
    """Login en Indexer. Devuelve True si tiene exito."""
    resp = session.get(BASE, timeout=10)
    soup = BeautifulSoup(resp.text, "html.parser")

    payload = {"username": USERNAME, "password": PASSWORD}

    # Recoger campos hidden del formulario de login
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name")
        if name:
            payload[name] = inp.get("value", "")

    # Buscar action del form
    action = BASE
    for form in soup.find_all("form"):
        a = form.get("action", "")
        if a:
            action = a if a.startswith("http") else BASE + a
            break

    resp2 = session.post(action, data=payload, timeout=10, allow_redirects=True)

    # Verificar login exitoso por la cookie HSLOGIN
    return "HSLOGIN" in session.cookies


def search(session: requests.Session, query: str) -> list:
    """
    Busca en Indexer y devuelve lista de resultados.
    Cada resultado: {title, url, id, uid}
    """
    url = f"{BASE}/?view=advsearch&q={requests.utils.quote(query)}"
    resp = session.get(url, timeout=10)
    # Detectar si nos redirigieron al login (sesion caducada)
    if "HSLOGIN" not in session.cookies or "advsearch" not in resp.url and resp.url != url:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    # Verificar que no estamos en la pagina de login
    if soup.find("input", {"name": "username"}) or soup.find("input", {"name": "password"}):
        return []

    results = []
    seen = set()

    # Buscar links a paginas de titulo: /?view=title&id=XXXX&uid=YYYY
    pattern = re.compile(r"view=title&id=(\d+)&uid=(\d+)")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = pattern.search(href)
        if m:
            tid, uid = m.group(1), m.group(2)
            key = (tid, uid)
            if key not in seen:
                seen.add(key)
                full_url = f"{BASE}/?view=title&id={tid}&uid={uid}"
                results.append({
                    "title": a.get_text(strip=True),
                    "url":   full_url,
                    "id":    tid,
                    "uid":   uid,
                })

    return results


def get_imdb_id(soup) -> str:
    """Extrae el IMDB ID de la pagina de una pelicula en Indexer."""
    for a in soup.find_all("a", href=True):
        m = re.search(r"imdb\.com/title/tt(\d+)", a["href"])
        if m:
            return m.group(1)
    return ""


def get_elink_ids(session: requests.Session, title_url: str) -> tuple:
    """
    Entra a la pagina de una pelicula y extrae todos los (id, code).
    Devuelve (imdb_id, [(id, code), ...])
    """
    resp = session.get(title_url, timeout=10)
    soup = BeautifulSoup(resp.text, "html.parser")

    imdb_id = get_imdb_id(soup)

    ids = []
    pattern = re.compile(r"Download\((\d+),'(\d+)'\)")

    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m:
            pair = (m.group(1), m.group(2))
            if pair not in ids:
                ids.append(pair)

    for a in soup.find_all("a", onclick=True):
        m = pattern.search(a["onclick"])
        if m:
            pair = (m.group(1), m.group(2))
            if pair not in ids:
                ids.append(pair)

    return imdb_id, ids


def fetch_ed2k(session: requests.Session, elink_id: str, code: str) -> list:
    """
    Llama al endpoint AJAX y extrae los enlaces ed2k del textarea.
    """
    nocache = random.random()
    ajax_url = f"{BASE}/ajax/download.php?id={elink_id}&code={code}&nocache={nocache}"

    resp = session.get(ajax_url, timeout=10, headers={"Referer": BASE})
    soup = BeautifulSoup(resp.text, "html.parser")

    textarea = soup.find("textarea", {"id": "ELINKSLIST"})
    if not textarea:
        return []

    links = [l.strip() for l in textarea.get_text().splitlines()
             if l.strip().startswith("ed2k://")]
    return links


def get_all_ed2k(session: requests.Session, title_url: str) -> tuple:
    """
    Dado el URL de una pelicula, devuelve (imdb_id, [ed2k, ...]).
    """
    imdb_id, elink_ids = get_elink_ids(session, title_url)
    print(f"  → {len(elink_ids)} elinks encontrados (imdb={imdb_id or '?'})")

    all_links = []
    for eid, code in elink_ids:
        links = fetch_ed2k(session, eid, code)
        all_links.extend(links)
        if links:
            print(f"  → elink {eid}: {len(links)} enlace(s) ed2k")

    return imdb_id, all_links


def send_to_emule(ed2k: str,
                  host: str = None,
                  port: int = None) -> bool:
    """Envia un enlace ed2k a eMule via su WebInterface."""
    host = host or os.environ.get("EMULE_HOST", "localhost")
    port = port or int(os.environ.get("EMULE_PORT", "4711"))
    url  = f"http://{host}:{port}//?ed2k={requests.utils.quote(ed2k)}"
    try:
        resp = requests.get(url, timeout=5)
        return resp.status_code == 200
    except Exception as e:
        print(f"  [ERROR eMule] {e}")
        return False


# ── Uso standalone ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    query = " ".join(args) if args else "iron man 2"
    send  = "--send" in sys.argv

    session = make_session()

    print(f"[*] Login en Indexer...")
    if not login(session):
        print("[ERROR] Login fallido. Verifica usuario/contrasena en config.env")
        sys.exit(1)
    print("[OK] Login correcto\n")

    print(f"[*] Buscando: '{query}'")
    results = search(session, query)
    print(f"[OK] {len(results)} resultado(s)\n")

    for r in results:
        print(f"  Pelicula: {r['title']}")
        print(f"  URL:      {r['url']}")
        links = get_all_ed2k(session, r["url"])
        for l in links:
            print(f"  ed2k: {l[:100]}")
            if send:
                ok = send_to_emule(l)
                print(f"  eMule: {'OK' if ok else 'ERROR'}")
        print()
