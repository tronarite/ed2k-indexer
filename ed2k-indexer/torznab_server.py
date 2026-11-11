"""
Servidor Torznab para Hispashare + eMule
Estrategia: Blackhole - guarda un archivo .ed2k en carpeta, watcher lo manda a eMule
"""

from flask import Flask, request, Response
from scraper import login, search, get_all_ed2k
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import os, hashlib, re, json

app = Flask(__name__)

BASE       = "https://www.hispashare.org"
API_KEY    = os.environ.get("TORZNAB_APIKEY", "hispashare123")
EMULE_HOST = os.environ.get("EMULE_HOST", "host-gateway")
EMULE_PORT = int(os.environ.get("EMULE_PORT", "4711"))
BLACKHOLE  = "/watch"   # G:\emuleDescargas\watch en el host
# Claves externas: definidas por entorno (TMDB_API_KEY / OMDB_API_KEY)
TMDB_KEY   = os.environ.get("TMDB_API_KEY", "")
OMDB_KEY   = os.environ.get("OMDB_API_KEY", "")

os.makedirs(BLACKHOLE, exist_ok=True)

_session = None

import time as _time, threading as _threading
_http_cache = {}
def http_json(url, timeout=5, ttl=21600):
    """GET JSON con cache en memoria (TMDB/OMDb). Los fallos no se cachean."""
    now = _time.time()
    hit = _http_cache.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    j = requests.get(url, timeout=timeout).json()
    if isinstance(j, dict) and not j.get("status_code") and j.get("Response") != "False":
        if len(_http_cache) > 2000:
            _http_cache.clear()
        _http_cache[url] = (now, j)
    return j

def is_logged_in(session):
    """Verifica si la sesion sigue activa comprobando la cookie HSLOGIN."""
    return "HSLOGIN" in session.cookies

def get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })
    if not is_logged_in(_session):
        print("[*] Sesion caducada o no iniciada, haciendo login...")
        ok = login(_session)
        if ok:
            print("[OK] Login correcto")
        else:
            print("[ERROR] Login fallido")
    return _session


def guid(ed2k):
    return hashlib.md5(ed2k.encode()).hexdigest()


def parse_size(ed2k):
    try:
        return ed2k.split("|")[3]
    except Exception:
        return "1500000000"


def parse_fname(ed2k):
    try:
        return ed2k.split("|")[2]
    except Exception:
        return "hispashare.mkv"


def make_torrent(fname, fsize, ed2k):
    """Genera un .torrent V1 válido mínimo con el ed2k en el comment."""
    import hashlib

    pieces = hashlib.sha1(fname.encode()).digest()  # 20 bytes exactos = 1 pieza

    def be(obj):
        if isinstance(obj, int):
            return b"i" + str(obj).encode() + b"e"
        if isinstance(obj, bytes):
            return str(len(obj)).encode() + b":" + obj
        if isinstance(obj, str):
            e = obj.encode()
            return str(len(e)).encode() + b":" + e
        if isinstance(obj, list):
            return b"l" + b"".join(be(x) for x in obj) + b"e"
        if isinstance(obj, dict):
            return b"d" + b"".join(be(k) + be(v) for k, v in sorted(obj.items())) + b"e"

    torrent = {
        "announce": "http://fake.tracker:6969/announce",
        "comment":  ed2k,          # <-- guardamos el ed2k aqui
        "info": {
            "length":       int(fsize),
            "name":         fname,
            "piece length": 524288,  # 512 KB fijo, evita overflow Int32
            "pieces":       pieces,
        }
    }
    return be(torrent)


def detect_quality(fname):
    """
    Detecta resolución y fuente basándose en patrones reales de Hispashare.
    Devuelve (resolution, source) para los atributos Torznab.
    """
    f = fname.upper()

    # Detectar 3D antes que nada
    is_3d = bool(re.search(r'\.3D\.|3D\.SBS|3D\.HOU|HSBS|HTAB|\bSBS\b|\bHOU\b|3D\.BLURAY|BLURAY\.3D', f))

    # Resolución — orden importante: más específico primero
    if re.search(r'2160P|MICRO4K|\b4K\b|UHDRI P', f):
        res = "2160p"
    elif re.search(r'1080P|M1080P|1036P|\b1080\b', f):
        res = "1080p"
    elif re.search(r'720P', f):
        res = "720p"
    elif re.search(r'480P|DVD.?RIP|XVID|DIVX|BD\+DVD', f):
        res = "480p"
    else:
        res = "1080p"  # Hispashare casi siempre es 1080p

    # Fuente → calidad que entiende Radarr
    if re.search(r'XVID|DIVX|BD\+DVD', f):
        src = "DVD"
    elif re.search(r'MICRO4K|UHDRIP|UHD.?RIP', f):
        src = "Bluray-2160p"
    elif re.search(r'2160P', f):
        src = "Bluray-2160p"
    elif re.search(r'BDRIP|BDREMUX|BLU.?RAY|MICROHD|\bMHD\b|BR\+HQTV|BLURAY.RIP', f):
        src = f"Bluray-{res}"
    elif re.search(r'WEB.?DL|WEBDL|\bNFLX\b|\bATVP\b|\bNF\b|\bAPTV\b', f):
        src = f"WEBDL-{res}"
    elif re.search(r'WEBRIP|WEB.RIP|\bWEBRIP\b', f):
        src = f"WEBRip-{res}"
    elif re.search(r'HDRIP|HD.?RIP', f):
        src = f"WEBDL-{res}"
    elif re.search(r'\bHD\b', f):
        src = f"Bluray-{res}"
    elif re.search(r'DVD.?RIP|DIVX', f):
        src = "DVD"
    else:
        src = f"WEBRip-{res}"

    # Añadir sufijo 3D si se detectó — ponerlo antes de la resolución
    # para que Radarr lo trate como calidad distinta y no descargue automáticamente
    if is_3d:
        # Formato: "Bluray-1080p" -> "Bluray-1080p-3D"  
        src = src + "-3D"

    return res, src


def make_radarr_title(fname, title_en, year):
    """Genera un titulo en formato que Radarr puede parsear."""
    res, src = detect_quality(fname)
    src_tag = src.replace("Bluray", "BDRip").replace("WEBDL", "WEB-DL").replace("WEBRip", "WEBRip")
    return f"{title_en} ({year}) {src_tag}"


def cat_attrs(src, base, tv=False):
    """Categorias Torznab: la base mas la especifica por resolucion."""
    if "2160" in src: sub = 5045 if tv else 2045
    elif "1080" in src or "720" in src: sub = 5040 if tv else 2040
    else: sub = 5030 if tv else 2030
    return [base, str(sub)]


def page_args():
    try:
        off = max(0, int(request.args.get("offset", 0) or 0))
    except Exception:
        off = 0
    try:
        lim = int(request.args.get("limit", 0) or 0)
    except Exception:
        lim = 0
    return off, (lim if lim > 0 else None)


def finish_feed(rss, ch, items, off, lim):
    """Aplica offset/limit (Torznab) y añade <torznab:response>."""
    total = len(items)
    keep = set(id(i) for i in items[off: (off + lim) if lim else None])
    for i in items:
        if id(i) not in keep:
            ch.remove(i)
    resp = ET.Element("torznab:response")
    resp.set("offset", str(off)); resp.set("total", str(total))
    ch.insert(3, resp)
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


def build_xml(results, title_en="", year=""):
    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:torznab", "http://torznab.com/schemas/2015/feed")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Hispashare ed2k"
    ET.SubElement(ch, "description").text = "Hispashare ed2k indexer"
    ET.SubElement(ch, "link").text = BASE

    # 2160p solo se descarta si hay alguna alternativa que no sea 2160p
    _4k = ("Bluray-2160p", "WEBRip-2160p", "WEBDL-2160p")
    only_4k = all(detect_quality(parse_fname(e))[1] in _4k
                  for r in results for e in r.get("ed2k_links", []))

    _off, _lim = page_args()
    _items = []

    for r in results:
        # En el RSS cada entrada trae su propio titulo/año y la regla del 4K se aplica por pelicula
        te_r = r.get("title_en", title_en)
        yr_r = r.get("year", year)
        r_only_4k = (all(detect_quality(parse_fname(e))[1] in _4k for e in r.get("ed2k_links", []))
                     if "title_en" in r else only_4k)
        for ed2k in r.get("ed2k_links", []):
            fname = parse_fname(ed2k)
            size  = parse_size(ed2k)
            res, src = detect_quality(fname)

            # Filtrar 2160p salvo que sea lo unico disponible
            if src in _4k and not r_only_4k:
                print(f"[SKIP] {fname[:60]} ({src})")
                continue
            fname_upper = fname.upper()

            # Titulo en formato que Radarr puede parsear (inglés + año + calidad)
            ext_lower = os.path.splitext(fname)[1].lower()
            is_video = ext_lower in {'.mkv', '.mp4', '.avi', '.mov', '.m4v'}
            if te_r and yr_r and is_video:
                title = make_radarr_title(fname, te_r, yr_r)
            elif te_r and yr_r:
                # Archivo no video (srt, rar, etc) — usar nombre crudo limpio
                title = fname.replace(".", " ").replace("_", " ").strip()
            else:
                title = fname.replace(".", " ").replace("_", " ")
                title = re.sub(r'\s*\(\d+\)\s*', ' ', title).strip()

            item = ET.SubElement(ch, "item")
            ET.SubElement(item, "title").text    = title
            ET.SubElement(item, "guid").text     = guid(ed2k)
            ET.SubElement(item, "link").text     = ed2k
            ET.SubElement(item, "pubDate").text  = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
            ET.SubElement(item, "comments").text = r["url"]

            enc = ET.SubElement(item, "enclosure")
            enc.set("url",    f"http://hispashare-indexer:8085/download?ed2k={requests.utils.quote(ed2k)}&apikey={API_KEY}&title={requests.utils.quote(te_r or '')}&year={yr_r or ''}")
            enc.set("length", size)
            enc.set("type",   "application/x-bittorrent")

            for name, val in ([("category", c) for c in cat_attrs(src, "2000")] + [
                ("seeders",  "50"),
                ("peers",    "100"),
                ("size",     size),
                ("quality",  src),
            ]):
                a = ET.SubElement(item, "torznab:attr")
                a.set("name", name); a.set("value", val)

            _items.append(item)
            print(f"[XML] {title[:50]} | {src} | {int(size)//1024//1024} MB")

    return finish_feed(rss, ch, _items, _off, _lim)


# -- RSS: novedades reales de Hispashare para el sync de Radarr ---------------
_rss_cache = {"ts": 0, "results": []}

def _refresh_rss(max_titles=25):
    from scraper import make_session, login as hs_login
    from bs4 import BeautifulSoup as BS
    s = make_session()
    if not hs_login(s):
        print("[RSS] login fallido")
        return
    home = s.get(BASE + "/", timeout=15).text
    soup = BS(home, "html.parser")
    seen, found = set(), []
    for a in soup.find_all("a", href=True):
        m = re.search(r"view=title&id=(\d+)&uid=(\d+)", a["href"])
        txt = a.get_text(" ", strip=True)
        if not m or (m.group(1), m.group(2)) in seen or not txt:
            continue
        seen.add((m.group(1), m.group(2)))
        if re.search(r"Temporada|Serie\s+completa", txt, re.I):
            continue      # series: las atiende tvsearch
        found.append(f"{BASE}/?view=title&id={m.group(1)}&uid={m.group(2)}")
    results = []
    for url in found[:max_titles]:
        try:
            imdb, links = get_all_ed2k(s, url)
            if not imdb or not links:
                continue
            j = http_json(f"https://api.themoviedb.org/3/find/tt{imdb}?api_key={TMDB_KEY}&external_source=imdb_id&language=es-ES")
            mr = j.get("movie_results", [])
            if not mr:
                continue
            en = mr[0].get("original_title", "") or mr[0].get("title", "")
            if not all(ord(c) < 256 for c in en):
                en = mr[0].get("title", "") or en
            yr = (mr[0].get("release_date", "") or "")[:4]
            if en and yr:
                results.append({"title": en, "url": url, "ed2k_links": links, "title_en": en, "year": yr})
        except Exception as e:
            print(f"[RSS] error en {url}: {e}")
    _rss_cache["results"] = results
    _rss_cache["ts"] = _time.time()
    print(f"[RSS] {len(results)} peliculas en cache")

def _rss_loop():
    _time.sleep(20)
    while True:
        try:
            _refresh_rss()
        except Exception as e:
            print(f"[RSS] error: {e}")
        _time.sleep(1800)

def rss_latest_movies():
    """XML con las novedades en cache, o None si aun no hay."""
    if not _rss_cache["results"]:
        return None
    return build_xml(_rss_cache["results"])


@app.route("/")
@app.route("/api")
def api():
    if request.args.get("apikey","") != API_KEY:
        return Response('<?xml version="1.0" encoding="UTF-8"?><error code="100" description="Incorrect user credentials"/>',
                        status=401, mimetype="application/xml")

    t      = request.args.get("t", "search")
    query  = request.args.get("q","") or request.args.get("title","")
    imdbid = request.args.get("imdbid","")

    # t=search con categorias de TV (Prowlarr/Seerr): tratarlo como busqueda de series
    cats = [c for c in request.args.get("cat", "").split(",") if c]
    if t == "search" and cats and all(c.startswith("5") for c in cats):
        t = "tvsearch"

    # Sonarr usa t=tvsearch
    if t == "tvsearch":
        tvdbid  = request.args.get("tvdbid","")
        season  = request.args.get("season","")
        episode = request.args.get("ep","")
        season  = int(season)  if season  else 1
        episode = int(episode) if episode else None
        return handle_tvsearch(query, tvdbid, season, episode,
                               imdbid=imdbid[2:] if imdbid.startswith("tt") else imdbid)

    if t == "caps":
        return Response("""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="Hispashare ed2k"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <movie-search available="yes" supportedParams="q,title,imdbid,tmdbid"/>
    <tv-search available="yes" supportedParams="q,title,tvdbid,imdbid,season,ep"/>
  </searching>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2010" name="Movies/Foreign"/>
      <subcat id="2020" name="Movies/Other"/>
      <subcat id="2030" name="Movies/SD"/>
      <subcat id="2040" name="Movies/HD"/>
      <subcat id="2045" name="Movies/UHD"/>
      <subcat id="2050" name="Movies/BluRay"/>
      <subcat id="2060" name="Movies/3D"/>
    </category>
    <category id="5000" name="TV">
      <subcat id="5020" name="TV/Foreign"/>
      <subcat id="5030" name="TV/SD"/>
      <subcat id="5040" name="TV/HD"/>
      <subcat id="5045" name="TV/UHD"/>
      <subcat id="5070" name="TV/Anime"/>
    </category>
  </categories>
</caps>""", mimetype="application/xml")

    # Radarr v5+ puede mandar tmdbid: resolverlo a imdbid
    tmdbid = request.args.get("tmdbid", "")
    if tmdbid.isdigit() and not imdbid:
        try:
            ext = http_json(f"https://api.themoviedb.org/3/movie/{tmdbid}/external_ids?api_key={TMDB_KEY}")
            raw = ext.get("imdb_id", "") or ""
            if raw.startswith("tt"):
                imdbid = raw[2:]
                print(f"[*] tmdbid {tmdbid} -> imdb tt{imdbid}")
        except Exception as e:
            print(f"[WARN] tmdbid error: {e}")

    # Variables para el titulo en inglés y año (para el XML de Radarr)
    title_en = ""
    year_str = ""

    # Resolver titulo en ESPAÑOL via TMDB, guardando también el inglés
    if imdbid:
        try:
            find_url = f"https://api.themoviedb.org/3/find/tt{imdbid}?api_key={TMDB_KEY}&external_source=imdb_id&language=es-ES"
            j = http_json(find_url)
            tmdb_results = j.get("movie_results", []) or j.get("tv_results", [])
            if tmdb_results:
                r0 = tmdb_results[0]
                titulo_es = r0.get("title") or r0.get("name","")
                title_en  = r0.get("original_title") or r0.get("original_name","")
                release   = r0.get("release_date","") or r0.get("first_air_date","")
                year_str  = release[:4] if release else ""
                if titulo_es:
                    query = titulo_es.strip()
                    # Si original_title no es latino (anime/japones/etc), usar titulo ES como title_en tambien
                    if title_en and not all(ord(c) < 256 for c in title_en):
                        print(f"[*] TMDB tt{imdbid} -> titulo original no latino, usando ES como fallback EN")
                        title_en = titulo_es.strip()
                    print(f"[*] TMDB tt{imdbid} -> ES:'{query}' EN:'{title_en}' ({year_str})")
        except Exception as e:
            print(f"[WARN] TMDB error: {e}")

    # Fallback a OMDb si TMDB no resolvio
    if not query and imdbid:
        try:
            j = http_json(f"https://www.omdbapi.com/?i=tt{imdbid}&apikey={OMDB_KEY}")
            query    = j.get("Title","").strip()
            title_en = query
            year_str = j.get("Year","")[:4]
            print(f"[*] OMDb tt{imdbid} -> '{query}' (EN fallback)")
        except Exception as e:
            print(f"[WARN] OMDb error: {e}")

    # Quitar año del final si viene (ej: "Iron Man 2 2010")
    query = re.sub(r"\s+\d{4}$", "", query).strip()
    # Quitar subtitulo tras punto o dos puntos (ej: "La guerra de las galaxias. Episodio I: La amenaza fantasma" -> "La amenaza fantasma")
    # Hispashare indexa por el subtitulo, no por el titulo completo
    if ":" in query:
        query = query.split(":")[-1].strip()
    elif ". " in query:
        parts = query.split(". ")
        # Quedarse con la parte mas corta (suele ser el subtitulo real)
        query = min(parts, key=len).strip() if len(parts) > 1 else query
    # Limpiar caracteres especiales que confunden a Hispashare
    query = re.sub(r"[+:||]", " ", query)
    query = re.sub(r"\s+", " ", query).strip()

    # Si el query viene en inglés (sin imdbid), traducir via TMDB y obtener imdbid
    if query and not imdbid:
        try:
            search_url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_KEY}&query={requests.utils.quote(query)}&language=es-ES"
            j = http_json(search_url)
            tmdb_results = j.get("results", [])
            if tmdb_results:
                r0 = tmdb_results[0]
                titulo_es = r0.get("title","").strip()
                title_en  = r0.get("original_title","").strip() or query
                release   = r0.get("release_date","")
                year_str  = release[:4] if release else ""
                tmdb_id   = r0.get("id")
                # Obtener imdbid desde TMDB para verificacion exacta
                if tmdb_id:
                    try:
                        ext = http_json(f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={TMDB_KEY}")
                        imdb_from_tmdb = ext.get("imdb_id","")
                        if imdb_from_tmdb and imdb_from_tmdb.startswith("tt"):
                            imdbid = imdb_from_tmdb[2:]
                            print(f"[*] TMDB resolvio imdbid: tt{imdbid}")
                    except Exception:
                        pass
                if titulo_es and titulo_es.lower() != query.lower():
                    print(f"[*] TMDB traduce '{query}' -> ES:'{titulo_es}' EN:'{title_en}' ({year_str})")
                    query = titulo_es
        except Exception as e:
            print(f"[WARN] TMDB search error: {e}")

    if not query and not imdbid:
        feed = rss_latest_movies()
        if feed:
            return Response(feed, mimetype="application/xml")
        dummy_xml = """<?xml version='1.0' encoding='utf-8'?>
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed" version="2.0">
<channel>
<title>Hispashare ed2k</title>
<description>Hispashare ed2k indexer</description>
<link>https://www.hispashare.org</link>
<item>
<title>Test Movie (2020) WEBDL-1080p</title>
<guid>000000000000000000000000000000ff</guid>
<link>ed2k://|file|Test.Movie.2020.mkv|4000000000|AABBCCDDEEFF00112233445566778899|/</link>
<pubDate>Wed, 11 Mar 2026 00:00:00 +0000</pubDate>
<enclosure url="http://hispashare-indexer:8085/download?ed2k=test&amp;apikey=hispashare123" length="4000000000" type="application/x-bittorrent"/>
<torznab:attr name="category" value="2000"/>
<torznab:attr name="seeders" value="50"/>
<torznab:attr name="peers" value="100"/>
<torznab:attr name="size" value="4000000000"/>
</item>
</channel>
</rss>"""
        return Response(dummy_xml, mimetype="application/xml")

    print(f"[*] Buscando: '{query}'")
    try:
        s = get_session()

        def do_search(q, target_imdb=None, _retry=False):
            nonlocal s
            raw = search(s, q)
            # Si no hay resultados y no hemos reintentado, forzar re-login
            if not raw and not _retry:
                print("[*] 0 resultados, forzando re-login...")
                from scraper import login as hs_login
                s.cookies.clear()
                ok = hs_login(s)
                print(f"[*] Re-login: {'OK' if ok else 'FALLO'}")
                raw = search(s, q)
            print(f"[OK] {len(raw)} paginas para '{q}'")
            res = []
            for r in raw:
                # Si tenemos imdbid objetivo, verificar primero sin extraer elinks
                if target_imdb:
                    from scraper import get_elink_ids
                    resp_page = s.get(r["url"], timeout=10)
                    from bs4 import BeautifulSoup as BS
                    import re as re2
                    soup_page = BS(resp_page.text, "html.parser")
                    page_imdb = ""
                    for a in soup_page.find_all("a", href=True):
                        m = re2.search(r"imdb\.com/title/tt(\d+)", a["href"])
                        if m:
                            page_imdb = m.group(1)
                            break
                    print(f"  → {r['title'][:40]} | imdb={page_imdb or '?'}")
                    if page_imdb != target_imdb:
                        print(f"  → SKIP (imdb no coincide)")
                        continue
                    # IMDB coincide, ahora extraer elinks
                    pattern = re2.compile(r"Download\((\d+),'(\d+)'\)")
                    elink_ids = []
                    for a in soup_page.find_all("a", href=True):
                        m = pattern.search(a["href"])
                        if m:
                            pair = (m.group(1), m.group(2))
                            if pair not in elink_ids:
                                elink_ids.append(pair)
                    for a in soup_page.find_all("a", onclick=True):
                        m = pattern.search(a["onclick"])
                        if m:
                            pair = (m.group(1), m.group(2))
                            if pair not in elink_ids:
                                elink_ids.append(pair)
                    from scraper import fetch_ed2k
                    links = []
                    for eid, code in elink_ids:
                        links.extend(fetch_ed2k(s, eid, code))
                    if links:
                        res.append({"title": r["title"] or q, "url": r["url"],
                                    "ed2k_links": links, "page_imdb": page_imdb})
                        print(f"  → MATCH! {len(links)} enlaces ed2k")
                        break  # Solo necesitamos una pagina correcta
                else:
                    page_imdb, links = get_all_ed2k(s, r["url"])
                    if links:
                        res.append({"title": r["title"] or q, "url": r["url"],
                                    "ed2k_links": links, "page_imdb": page_imdb})
            return res

        if imdbid:
            # Buscar con titulo ES, verificando IMDB en cada pagina antes de extraer elinks
            results = do_search(query, target_imdb=imdbid)
            # Si no encontro nada, reintentar con titulo EN
            if not results and title_en and title_en.lower() != query.lower():
                print(f"[*] Sin match en ES, reintentando EN: '{title_en}'")
                results = do_search(title_en, target_imdb=imdbid)
            # Si sigue sin resultados, busqueda progresiva por palabras clave
            if not results:
                # Extraer palabras clave significativas del titulo (>=4 chars, no stopwords)
                stops = {"the","a","an","de","del","la","el","los","las","you","not","can",
                         "are","and","or","in","of","to","is","it","be","at","on","for"}
                words = [w for w in re.sub(r'[^a-zA-Z0-9\s]', ' ', (title_en or query)).split()
                         if len(w) >= 4 and w.lower() not in stops]
                # Probar con menos palabras progresivamente
                for n in range(len(words)-1, 0, -1):
                    short_query = " ".join(words[:n])
                    if len(short_query) < 3:
                        continue
                    print(f"[*] Busqueda progresiva: '{short_query}'")
                    results = do_search(short_query, target_imdb=imdbid)
                    if results:
                        print(f"[*] Encontrado con query reducido: '{short_query}'")
                        break
        else:
            results = do_search(query)
            if year_str and title_en and title_en.lower() != query.lower():
                year_found = any(year_str in " ".join([r.get("title","")] + r.get("ed2k_links",[])) for r in results)
                if not year_found:
                    print(f"[*] Sin resultados con año {year_str} en ES, reintentando EN: '{title_en}'")
                    results_en = do_search(title_en)
                    if results_en:
                        results = results_en

        total = sum(len(r['ed2k_links']) for r in results)
        print(f"[OK] {total} enlaces ed2k antes de filtrar por titulo")

        # Filtrar resultados irrelevantes comparando titulo con el esperado
        # Si verificamos por IMDB, no hace falta filtrar por titulo/año
        if imdbid:
            pass  # IMDB ya garantiza que es la pelicula correcta
        elif title_en or query:
            def title_words(s):
                """Extrae palabras significativas de un titulo (>=3 chars, no stopwords)"""
                stops = {"the","a","an","de","del","la","el","los","las","y","e","en",
                         "un","una","por","con","sin","que","su","sus"}
                s = s.lower()
                s = re.sub(r"[^a-z0-9\s]", " ", s)
                return {w for w in s.split() if len(w) >= 3 and w not in stops}

            words_en = title_words(title_en)
            words_es = title_words(query)
            # Palabras clave del titulo (unión de ambos idiomas)
            key_words = words_en | words_es

            filtered = []
            for r in results:
                # Texto a comparar: título de la página + nombre de cada archivo
                texts = [r.get("title","")]
                for ed2k in r.get("ed2k_links", []):
                    texts.append(parse_fname(ed2k))
                combined_words = set()
                for t in texts:
                    combined_words |= title_words(t)
                # Cuántas palabras clave aparecen
                matches = len(key_words & combined_words)
                # Verificar año: buscar en título de página o en cualquier fname
                all_text = " ".join(texts)
                year_ok = (not year_str) or (year_str in all_text)
                # Necesita al menos 1 palabra clave Y el año (si disponible)
                if matches >= 1 and year_ok:
                    filtered.append(r)
                else:
                    title_preview = (texts[1] if len(texts) > 1 else texts[0])[:60]
                    print(f"[SKIP] {title_preview} (no coincide: {matches} palabras, año={'ok' if year_ok else 'mal'})")
            results = filtered
        print(f"[OK] {sum(len(r['ed2k_links']) for r in results)} enlaces tras filtrar por titulo")

        # Elegir UN SOLO release: mejor ratio calidad/tamano
        # Prioridad: WEBDL > WEBRip > MicroHD > Bluray
        # Dentro de misma prioridad: menor tamano en rango 1-8 GB
        MAX_SIZE = 8 * 1024**3
        MIN_SIZE = 1 * 1024**3
        SRC_PRIORITY = {"WEBDL": 0, "WEBRip": 1, "MicroHD": 2, "Bluray": 3}

        def src_prio(src):
            for k, v in SRC_PRIORITY.items():
                if k.lower() in src.lower():
                    return v
            return 99

        candidates = []
        for r in results:
            for ed2k in r.get("ed2k_links", []):
                fname = parse_fname(ed2k)
                fname_upper = fname.upper()
                size  = parse_size(ed2k)
                res, src = detect_quality(fname)
                try:
                    sz = int(size)
                except:
                    sz = 0

                in_range = MIN_SIZE <= sz <= MAX_SIZE
                res_prio = 0 if res == "1080p" else (1 if res == "720p" else 2)
                candidates.append((src_prio(src), res_prio, sz, ed2k, r, in_range))

        # Devolver todos los candidatos a Radarr — el indexer no decide, Radarr elige
        if candidates:
            candidates.sort(key=lambda c: (c[0], c[1], c[2]))
            results = []
            for sp, rp, sz, ed2k, r, _ in candidates:
                results.append({"title": r["title"], "url": r["url"], "ed2k_links": [ed2k]})
                _, src_c = detect_quality(parse_fname(ed2k))
                print(f"  [OK] {parse_fname(ed2k)[:55]} | {src_c} | {sz//1024//1024} MB")
            print(f"[OK] {len(results)} candidatos devueltos a Radarr")
        else:
            # Sin candidatos — devolver todo incluyendo DVD para seleccion manual
            all_links = []
            for r in results:
                for ed2k in r.get("ed2k_links", []):
                    fname = parse_fname(ed2k)
                    fname_upper = fname.upper()
                    size = parse_size(ed2k)
                    try:
                        sz = int(size)
                    except:
                        sz = 0


                    all_links.append((sz, ed2k, r))
            if all_links:
                all_links.sort(key=lambda x: x[0], reverse=True)
                print(f"[OK] Devolviendo {len(all_links)} candidatos (sin 1080p) para seleccion manual")
                results = []
                for sz, ed2k, r in all_links:
                    results.append({"title": r["title"], "url": r["url"], "ed2k_links": [ed2k]})
                    _, src_c = detect_quality(parse_fname(ed2k))
                    print(f"  [MANUAL] {parse_fname(ed2k)[:55]} | {src_c} | {sz//1024//1024} MB")
            else:
                results = []
                print("[OK] Sin candidatos validos")
    except Exception as e:
        import traceback; traceback.print_exc()
        results = []

    return Response(build_xml(results, title_en=title_en, year=year_str), mimetype="application/xml")


def build_xml_tv(episodes, show_title, year=""):
    """
    Construye XML Torznab para episodios de series.
    episodes = [(season, ep, ed2k, fname), ...]
    """
    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:torznab", "http://torznab.com/schemas/2015/feed")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Hispashare ed2k"
    ET.SubElement(ch, "description").text = "Hispashare ed2k indexer"
    ET.SubElement(ch, "link").text = BASE
    _off, _lim = page_args()
    _items = []

    for season, ep, ed2k, fname in episodes:
        size  = parse_size(ed2k)
        res, src = detect_quality(fname)

        # Titulo en formato que Sonarr entiende con calidad incluida
        if year:
            title = f"{show_title} ({year}) S{season:02d}E{ep:02d} {src}"
        else:
            title = f"{show_title} S{season:02d}E{ep:02d} {src}"

        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text   = title
        ET.SubElement(item, "guid").text    = guid(ed2k)
        ET.SubElement(item, "link").text    = ed2k
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

        enc = ET.SubElement(item, "enclosure")
        enc.set("url",    f"http://hispashare-indexer:8085/download?ed2k={requests.utils.quote(ed2k)}&apikey={API_KEY}&title={requests.utils.quote(show_title)}&year={year}&season={season}&episode={ep}")
        enc.set("length", str(size))
        enc.set("type",   "application/x-bittorrent")

        for name, val in ([("category", c) for c in cat_attrs(src, "5000", tv=True)] + [
            ("seeders",   "50"),
            ("peers",     "100"),
            ("size",      str(size)),
            ("quality",   src),
            ("season",    str(season)),
            ("episode",   str(ep)),
        ]):
            a = ET.SubElement(item, "torznab:attr")
            a.set("name", name); a.set("value", val)

        _items.append(item)
        print(f"[TV-XML] {title} | {src} | {int(size)//1024//1024} MB")

    return finish_feed(rss, ch, _items, _off, _lim)


@app.route("/tvsearch")
@app.route("/api/tvsearch")
def tvsearch_redirect():
    return api()


def handle_tvsearch(query, tvdbid, season, episode, imdbid=""):
    """Manejador principal para t=tvsearch de Sonarr."""
    from scraper_tv import get_season_episodes

    title_en = ""
    year_str = ""

    # Sin tvdbid pero con imdbid: resolver por IMDb
    if imdbid and not tvdbid:
        try:
            j = http_json(f"https://api.themoviedb.org/3/find/tt{imdbid}?api_key={TMDB_KEY}&external_source=imdb_id&language=es-ES")
            tv_results = j.get("tv_results", [])
            if tv_results:
                r0 = tv_results[0]
                query     = r0.get("name", "").strip() or query
                title_en  = r0.get("original_name", "").strip() or query
                year_str  = (r0.get("first_air_date", "") or "")[:4]
                print(f"[TV] TMDB imdb tt{imdbid} -> ES:'{query}' EN:'{title_en}' ({year_str})")
        except Exception as e:
            print(f"[WARN] TMDB TV imdb error: {e}")

    # Resolver serie via TMDB usando tvdbid
    if tvdbid:
        try:
            find_url = f"https://api.themoviedb.org/3/find/{tvdbid}?api_key={TMDB_KEY}&external_source=tvdb_id&language=es-ES"
            j = http_json(find_url)
            tv_results = j.get("tv_results", [])
            if tv_results:
                r0 = tv_results[0]
                titulo_es = r0.get("name", "").strip()
                title_en  = r0.get("original_name", "").strip() or titulo_es
                release   = r0.get("first_air_date", "")
                year_str  = release[:4] if release else ""
                tmdb_id   = r0.get("id")
                # Obtener IMDB ID
                if tmdb_id:
                    ext = http_json(f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={TMDB_KEY}")
                    imdb_raw = ext.get("imdb_id", "")
                    if imdb_raw and imdb_raw.startswith("tt"):
                        imdbid = imdb_raw[2:]
                if titulo_es:
                    query = titulo_es
                print(f"[TV] TMDB tvdb:{tvdbid} -> ES:'{query}' EN:'{title_en}' ({year_str}) imdb={imdbid or '?'}")
        except Exception as e:
            print(f"[WARN] TMDB TV error: {e}")

    # Si no hay tvdbid, intentar traducir el query via TMDB
    if not tvdbid and query:
        try:
            search_url = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_KEY}&query={requests.utils.quote(query)}&language=es-ES"
            j = http_json(search_url)
            tv_results = j.get("results", [])
            if tv_results:
                r0 = tv_results[0]
                titulo_es = r0.get("name", "").strip()
                title_en  = r0.get("original_name", "").strip() or query
                release   = r0.get("first_air_date", "")
                year_str  = release[:4] if release else ""
                tmdb_id   = r0.get("id")
                if tmdb_id:
                    ext = http_json(f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={TMDB_KEY}")
                    imdb_raw = ext.get("imdb_id", "")
                    if imdb_raw and imdb_raw.startswith("tt"):
                        imdbid = imdb_raw[2:]
                if titulo_es and titulo_es.lower() != query.lower():
                    print(f"[TV] TMDB traduce '{query}' -> ES:'{titulo_es}' EN:'{title_en}' ({year_str})")
                    query = titulo_es
        except Exception as e:
            print(f"[WARN] TMDB TV search error: {e}")

    if not query and not tvdbid and not imdbid:
        dummy = build_xml_tv(
            [(1, 1, "ed2k://|file|Test.S01E01.mkv|1000000000|AABBCCDDEEFF00112233445566778899|/", "Test.S01E01.mkv")],
            "Test Show", "2020"
        )
        return Response(dummy, mimetype="application/xml")

    # Limpiar query
    query = re.sub(r"[+:||]", " ", query)
    query = re.sub(r"\s+", " ", query).strip()

    print(f"[TV] Buscando serie: '{query}' temporada={season} episodio={episode}")

    s = get_session()

    # Buscar en Hispashare con re-login si falla
    from scraper import search as hs_search

    def tv_search(q):
        raw = hs_search(s, q)
        if not raw:
            print(f"[TV] 0 resultados, forzando re-login...")
            from scraper import login as hs_login
            s.cookies.clear()
            ok = hs_login(s)
            print(f"[TV] Re-login: {'OK' if ok else 'FALLO'}")
            raw = hs_search(s, q)
        return raw

    raw_results = tv_search(query)
    print(f"[TV] {len(raw_results)} paginas encontradas")

    # Si no encuentra con titulo ES, reintentar con EN
    if not raw_results and title_en and title_en.lower() != query.lower():
        print(f"[TV] Reintentando con titulo EN: '{title_en}'")
        raw_results = tv_search(title_en)

    if not raw_results:
        print("[TV] Sin resultados")
        return Response(build_xml_tv([], title_en or query, year_str), mimetype="application/xml")

    # Elegir la página correcta verificando IMDB si está disponible
    target_url = None
    for r in raw_results:
        if imdbid:
            # Verificar IMDB en la página
            resp_page = s.get(r["url"], timeout=10)
            from bs4 import BeautifulSoup as BS
            soup_page = BS(resp_page.text, "html.parser")
            page_imdb = ""
            for a in soup_page.find_all("a", href=True):
                m = re.search(r"imdb\.com/title/tt(\d+)", a["href"])
                if m:
                    page_imdb = m.group(1)
                    break
            print(f"[TV] Pagina '{r['title'][:40]}' imdb={page_imdb or '?'}")
            if page_imdb == imdbid:
                target_url = r["url"]
                print(f"[TV] MATCH! Usando: {target_url}")
                break
        else:
            # Sin IMDB, usar el primer resultado
            target_url = r["url"]
            break

    if not target_url:
        print("[TV] No se encontro la serie correcta")
        return Response(build_xml_tv([], title_en or query, year_str), mimetype="application/xml")

    # Extraer episodios de la temporada pedida
    _, episodes = get_season_episodes(s, target_url, season)

    # Si Sonarr pide un episodio específico, filtrar
    if episode is not None:
        episodes = [(s, e, ed2k, fname) for s, e, ed2k, fname in episodes if e == episode]

    show_title = title_en or query
    return Response(build_xml_tv(episodes, show_title, year_str), mimetype="application/xml")


# Registro de torrents pendientes: hash -> {fname, fsize, added}
import threading, json as _json
_torrents_lock = threading.Lock()
_TORRENTS_FILE = "/watch/torrents.json"

def _load_torrents():
    try:
        with open(_TORRENTS_FILE) as f:
            return _json.load(f)
    except Exception:
        return {}

def _save_torrents(t):
    try:
        with open(_TORRENTS_FILE, "w") as f:
            _json.dump(t, f)
    except Exception as e:
        print(f"[WARN] No se pudo guardar torrents.json: {e}")

_torrents = _load_torrents()
_torrent_id_counter = [max((v.get("id",0) for v in _torrents.values()), default=0) + 1]

DOWNLOADS_DIR = "/downloads"


def find_file_in_downloads(fname, radarr_fname=None):
    """Busca el archivo en /downloads. Si radarr_fname es distinto, lo renombra."""
    import os
    try:
        for f in os.listdir(DOWNLOADS_DIR):
            if f.lower() == fname.lower():
                fpath = os.path.join(DOWNLOADS_DIR, f)
                # Renombrar al nombre que Radarr espera si es diferente
                if radarr_fname and f != radarr_fname:
                    new_path = os.path.join(DOWNLOADS_DIR, radarr_fname)
                    if not os.path.exists(new_path):
                        os.rename(fpath, new_path)
                        print(f"[*] Renombrado: {f[:50]} -> {radarr_fname[:50]}")
                        return new_path
                return fpath
            # También buscar por radarr_fname por si ya fue renombrado
            if radarr_fname and f.lower() == radarr_fname.lower():
                return os.path.join(DOWNLOADS_DIR, f)
    except Exception as e:
        print(f"[WARN] find_file_in_downloads: {e}")
    return None


@app.route("/download")
def download():
    if request.args.get("apikey","") != API_KEY:
        return Response("Unauthorized", status=401)

    ed2k = request.args.get("ed2k","")
    if not ed2k:
        return Response("Missing ed2k", status=400)

    fname    = parse_fname(ed2k)
    fsize    = parse_size(ed2k)
    title_en = request.args.get("title", "").strip()
    year     = request.args.get("year", "").strip()
    season   = request.args.get("season", "").strip()
    episode  = request.args.get("episode", "").strip()

    print(f"[*] Descarga solicitada: {fname}")

    # Guardar .ed2k en carpeta watch, el ED2K watcher lo mandara a eMule
    safe = re.sub(r'[\\/*?:"<>|]', "_", fname)
    with open(f"{BLACKHOLE}/{safe}.ed2k", "w") as f:
        f.write(ed2k)

    # Generar nombre que Sonarr/Radarr puede reconocer automaticamente
    _, src = detect_quality(fname)
    ext = fname.rsplit(".", 1)[-1] if "." in fname else "mkv"

    if title_en and season and episode:
        # Serie: "Show Title (Year) S01E01 Quality.mkv"
        s = int(season)
        e = int(episode)
        sonarr_fname = f"{title_en} ({year}) S{s:02d}E{e:02d} {src}.{ext}" if year else f"{title_en} S{s:02d}E{e:02d} {src}.{ext}"
        sonarr_fname = re.sub(r'[\\/*?:"<>|]', "_", sonarr_fname)
        radarr_fname = sonarr_fname
        print(f"[*] Nombre para Sonarr: {radarr_fname}")
    elif title_en and year:
        # Pelicula: "Title (Year) Quality.mkv"
        radarr_fname = f"{title_en} ({year}) {src}.{ext}"
        radarr_fname = re.sub(r'[\\/*?:"<>|]', "_", radarr_fname)
        print(f"[*] Nombre para Radarr: {radarr_fname}")
    else:
        radarr_fname = fname

    # Renombrar inmediatamente si el archivo ya esta en /downloads
    fpath = find_file_in_downloads(fname, radarr_fname)
    if fpath:
        print(f"[*] Archivo ya en /downloads, renombrado listo")
    else:
        print(f"[*] Archivo no encontrado aun en /downloads, esperando eMule")

    # Registrar torrent pendiente
    torrent_hash = guid(ed2k)
    with _torrents_lock:
        _torrent_id_counter[0] += 1
        tid = _torrent_id_counter[0]
        _torrents[torrent_hash] = {
            "id": tid,
            "name": re.sub(r'[\\/*?:"<>|]', "_", radarr_fname.rsplit(".", 1)[0]),
            "fname": fname,
            "radarr_fname": radarr_fname,
            "fsize": fsize
        }
        _save_torrents(_torrents)
    print(f"[Transmission] Torrent registrado: {torrent_hash[:8]}... id={tid}")

    torrent_bytes = make_torrent(fname, fsize, ed2k)
    return Response(torrent_bytes, mimetype="application/x-bittorrent",
                    headers={"Content-Disposition": f"attachment; filename={safe}.torrent"})


@app.route("/transmission/rpc", methods=["GET","POST"])
def transmission():
    if not request.headers.get("X-Transmission-Session-Id",""):
        return Response("session-id required", status=409,
                        headers={"X-Transmission-Session-Id": "hispashare00000000000000000000000000"})

    data   = request.get_json(silent=True) or {}
    method = data.get("method","")

    if method == "session-get":
        return Response('{"result":"success","arguments":{"version":"3.00","rpc-version":17,"download-dir":"/downloads"}}',
                        mimetype="application/json")

    if method == "torrent-add":
        url = data.get("arguments",{}).get("filename","")
        if "/download?" in url:
            try: requests.get(url, timeout=10)
            except: pass
        return Response('{"result":"success","arguments":{"torrent-added":{"id":1,"name":"hispashare","hashString":"aabbccddeeff00112233445566778899aabbccdd"}}}',
                        mimetype="application/json")

    if method == "torrent-get":
        import json, os
        result_torrents = []
        with _torrents_lock:
            for h, t in list(_torrents.items()):
                fpath = find_file_in_downloads(t["fname"], t.get("radarr_fname"))
                if fpath:
                    fsize = os.path.getsize(fpath)
                    radarr_fname = t.get("radarr_fname", t["fname"])
                    print(f"[Transmission] Archivo encontrado: {t['fname'][:50]} -> reportando como '{radarr_fname[:50]}'")
                    result_torrents.append({
                        "id": t["id"],
                        "name": t["name"],
                        "hashString": h,
                        "status": 6,
                        "percentDone": 1.0,
                        "downloadDir": DOWNLOADS_DIR,
                        "files": [{"name": radarr_fname, "length": fsize, "bytesCompleted": fsize}],
                        "fileStats": [{"wanted": True, "priority": 0, "bytesCompleted": fsize}],
                        "leftUntilDone": 0,
                        "eta": -1,
                        "rateDownload": 0,
                        "rateUpload": 0,
                        "uploadRatio": 0,
                        "totalSize": fsize,
                        "error": 0,
                        "errorString": "",
                        "isFinished": True,
                        "isStalled": False,
                        "labels": [],
                        "doneDate": int(__import__('time').time()),
                    })
                else:
                    print(f"[Transmission] Esperando: {t['fname'][:50]}")
        resp = {"result": "success", "arguments": {"torrents": result_torrents}}
        return Response(json.dumps(resp), mimetype="application/json")

    if method == "torrent-remove":
        ids = data.get("arguments",{}).get("ids",[])
        with _torrents_lock:
            for h, t in list(_torrents.items()):
                if t["id"] in ids:
                    print(f"[Transmission] Torrent eliminado: {t['fname'][:50]}")
                    del _torrents[h]
            _save_torrents(_torrents)
        return Response('{"result":"success","arguments":{}}', mimetype="application/json")

    return Response('{"result":"success","arguments":{}}', mimetype="application/json")


_emule_session = {"ses": None}

def _emule_login():
    """Hace login en la WebInterface de eMule y devuelve el session ID."""
    emule_pass = os.environ.get("EMULE_PASS", "")
    base = f"http://{EMULE_HOST}:{EMULE_PORT}"
    try:
        # POST login directamente
        resp = requests.post(f"{base}/", data={"p": emule_pass, "w": "password"},
                             timeout=5, allow_redirects=True)
        # El ses aparece en la URL de respuesta o en el HTML
        m = re.search(r'[?&]ses=(-?\d+)', resp.url)
        if not m:
            m = re.search(r'[?&]ses=(-?\d+)', resp.text)
        if m and m.group(1) != "0":
            _emule_session["ses"] = m.group(1)
            print(f"[eMule] Login OK, ses={_emule_session['ses']}")
            return True
        # Intentar GET primero para obtener la URL base con ses
        resp0 = requests.get(f"{base}/", timeout=5)
        m0 = re.search(r'[?&]ses=(-?\d+)', resp0.url + resp0.text)
        ses_inicial = m0.group(1) if m0 else "0"
        # POST con el ses inicial
        resp2 = requests.post(f"{base}/", data={"p": emule_pass, "w": "password"},
                              params={"ses": ses_inicial},
                              timeout=5, allow_redirects=True)
        m2 = re.search(r'[?&]ses=(-?\d+)', resp2.url)
        if not m2:
            m2 = re.search(r'[?&]ses=(-?\d+)', resp2.text)
        if m2 and m2.group(1) != "0":
            _emule_session["ses"] = m2.group(1)
            print(f"[eMule] Login OK (v2), ses={_emule_session['ses']}")
            return True
        print(f"[eMule] Login fallido. URL respuesta: {resp2.url[:100]}")
        print(f"[eMule] HTML snippet: {resp2.text[200:400]}")
        return False
    except Exception as e:
        print(f"[eMule] Login error: {e}")
        return False

def _emule_send_ed2k(ed2k):
    """Manda un enlace ed2k a eMule via WebInterface."""
    base = f"http://{EMULE_HOST}:{EMULE_PORT}"
    if not _emule_session["ses"]:
        _emule_login()
    try:
        url = f"{base}/?ses={_emule_session['ses']}&w=transfer&ed2k={requests.utils.quote(ed2k, safe='')}"
        print(f"[eMule] Enviando: {url[:120]}")
        resp = requests.get(url, timeout=5)
        # Si nos redirige al login, la sesion caducó
        if "password" in resp.text.lower() and "iniciar" in resp.text.lower():
            print("[eMule] Sesion caducada, relogin...")
            if _emule_login():
                url = f"{base}/?ses={_emule_session['ses']}&w=transfer&ed2k={requests.utils.quote(ed2k, safe='')}"
                resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            return True
        print(f"[eMule] Error HTTP {resp.status_code}")
        return False
    except Exception as e:
        print(f"[eMule] Error enviando ed2k: {e}")
        return False


RADARR_URL     = "http://radarr:7878"
RADARR_API_KEY = "c513394977184bcda278a811a9268a46"


def _guess_via_radarr(fname):
    """Fallback: consulta Radarr por título aproximado cuando TMDB falla."""
    name = os.path.splitext(fname)[0]
    ext  = os.path.splitext(fname)[1].lower()
    _, src = detect_quality(fname)

    clean = re.sub(r'\.(?!\d)', ' ', name).replace('_', ' ')
    clean = re.sub(r'\s+', ' ', clean).strip()
    q_m = re.search(r'\b(1080p|720p|2160p|BDRip|WEBRip|WEBDL|WEB-DL|BluRay|MicroHD|XviD|x264|x265)\b', clean, re.I)
    raw = clean[:q_m.start()].strip().rstrip('.-') if q_m else clean
    raw = re.sub(r'\s*\((?:19|20)\d{2}\)\s*', ' ', raw).strip()
    raw = re.sub(r'\s*\([^)]+\)\s*', ' ', raw).strip()
    raw = re.sub(r'\s+', ' ', raw).strip()
    if len(raw) < 2:
        return None

    try:
        movies = requests.get(f"{RADARR_URL}/api/v3/movie",
                              headers={"X-Api-Key": RADARR_API_KEY},
                              timeout=5).json()
        raw_words = set(re.sub(r'[^a-z0-9\s]', ' ', raw.lower()).split())
        best, best_score = None, 0
        for movie in movies:
            title = movie.get('title', '')
            year  = str(movie.get('year', ''))
            t_words = set(re.sub(r'[^a-z0-9\s]', ' ', title.lower()).split())
            score = len(raw_words & t_words)
            if score > best_score:
                best_score = score
                best = (title, year)
        if best and best_score >= 2:
            safe_title = re.sub(r'[\\/*?:"<>|]', '_', best[0])
            new_name = f"{safe_title} ({best[1]}) {src}{ext}"
            print(f"[Watcher] Radarr identificó: '{best[0]}' (score={best_score})")
            return new_name
    except Exception as e:
        print(f"[Watcher] Radarr API error: {e}")
    return None


def _guess_radarr_fname(fname):
    """
    Intenta identificar un archivo via TMDB y devuelve el nombre
    que Radarr puede importar automáticamente, o None si no puede.
    Funciona tanto para películas (sin SxEE) como para series (con SxEE).
    """
    pass  # TMDB_KEY global (entorno)
    name = os.path.splitext(fname)[0]
    ext  = os.path.splitext(fname)[1].lower()

    # Detectar si es episodio de serie
    ep_m = re.search(r'(\d{1,2})[xX](\d{2})|[Ss](\d{1,2})[Ee](\d{2})', fname)

    # Limpiar nombre: puntos → espacios, quitar tags
    clean = re.sub(r'\.(?!\d)', ' ', name).replace('_', ' ')
    clean = re.sub(r'\s+', ' ', clean).strip()
    # Quitar desde el marcador de episodio o calidad en adelante
    cutoff = ep_m.start() if ep_m else None
    if not cutoff:
        q_m = re.search(r'\b(1080p|720p|2160p|BDRip|WEBRip|WEBDL|WEB-DL|BluRay|MicroHD|XviD|x264|x265)\b', clean, re.I)
        cutoff = q_m.start() if q_m else None
    raw_title = clean[:cutoff].strip().rstrip('.-') if cutoff else clean

    # Quitar año entre paréntesis si está pegado al título
    year_m = re.search(r'\(((?:19|20)\d{2})\)', raw_title)
    year = year_m.group(1) if year_m else ""
    if year_m:
        raw_title = raw_title[:year_m.start()].strip()
    if not year:
        year_m2 = re.search(r'\b((?:19|20)\d{2})\b', clean)
        year = year_m2.group(1) if year_m2 else ""

    # Quitar subtítulos tras punto/dos puntos para mejorar búsqueda
    if ':' in raw_title:
        raw_title = raw_title.split(':')[-1].strip()
    elif '. ' in raw_title:
        parts = raw_title.split('. ')
        raw_title = min(parts, key=len).strip()

    raw_title = re.sub(r'\s+', ' ', raw_title).strip()
    if len(raw_title) < 2:
        return None

    _, src = detect_quality(fname)

    if ep_m:
        # Es serie — buscar en TMDB TV
        season  = int(ep_m.group(1) or ep_m.group(3))
        episode = int(ep_m.group(2) or ep_m.group(4))
        try:
            for lang in ['en-US', 'es-ES']:
                r = requests.get('https://api.themoviedb.org/3/search/tv',
                                 params={'api_key': TMDB_KEY, 'query': raw_title, 'language': lang},
                                 timeout=5).json()
                results = r.get('results', [])
                if results:
                    res   = results[0]
                    title = res.get('name') or res.get('original_name', '')
                    d     = res.get('first_air_date', '')
                    y     = d[:4] if d else year
                    if lang == 'es-ES' and title:
                        # Obtener título en inglés
                        r2 = requests.get(f"https://api.themoviedb.org/3/tv/{res['id']}",
                                          params={'api_key': TMDB_KEY, 'language': 'en-US'},
                                          timeout=5).json()
                        title = r2.get('name') or r2.get('original_name', title)
                        d2 = r2.get('first_air_date', '')
                        y  = d2[:4] if d2 else y
                    safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)
                    new_name = f"{safe_title} ({y}) S{season:02d}E{episode:02d} {src}{ext}" if y else \
                               f"{safe_title} S{season:02d}E{episode:02d} {src}{ext}"
                    return new_name
        except Exception as e:
            print(f"[Watcher] TMDB TV error: {e}")
    else:
        # Es película — buscar en TMDB Movies
        try:
            for lang in ['en-US', 'es-ES']:
                params = {'api_key': TMDB_KEY, 'query': raw_title, 'language': 'en-US'}
                if year:
                    params['year'] = year
                r = requests.get('https://api.themoviedb.org/3/search/movie',
                                 params=params, timeout=5).json()
                results = r.get('results', [])
                if not results and year:
                    params.pop('year')
                    r = requests.get('https://api.themoviedb.org/3/search/movie',
                                     params=params, timeout=5).json()
                    results = r.get('results', [])
                if results:
                    res   = results[0]
                    title = res.get('title') or res.get('original_title', '')
                    d     = res.get('release_date', '')
                    y     = d[:4] if d else year
                    safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)
                    new_name = f"{safe_title} ({y}) {src}{ext}"
                    return new_name
                if lang == 'en-US':
                    # Reintentar buscando en español
                    params['language'] = 'es-ES'
                    r2 = requests.get('https://api.themoviedb.org/3/search/movie',
                                      params=params, timeout=5).json()
                    results2 = r2.get('results', [])
                    if results2:
                        tmdb_id = results2[0].get('id')
                        r3 = requests.get(f"https://api.themoviedb.org/3/movie/{tmdb_id}",
                                          params={'api_key': TMDB_KEY, 'language': 'en-US'},
                                          timeout=5).json()
                        title = r3.get('title') or r3.get('original_title', '')
                        d     = r3.get('release_date', '')
                        y     = d[:4] if d else year
                        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)
                        new_name = f"{safe_title} ({y}) {src}{ext}"
                        return new_name
        except Exception as e:
            print(f"[Watcher] TMDB movie error: {e}")

    return None


def _incoming_watcher():
    """
    Vigila /downloads cada 30s. Cuando aparece un archivo nuevo:
    1. Busca en torrents.json si tiene el nombre radarr ya calculado
    2. Si no, lo identifica via TMDB por el nombre del archivo
    3. Renombra al nombre correcto para Radarr/Sonarr
    Funciona aunque el container se haya reiniciado.
    """
    import time, os
    print("[Watcher] Iniciado, vigilando /downloads...")
    # Al arrancar escanear todo Incoming, no solo archivos nuevos
    seen = set()
    first_run = True
    while True:
        try:
            if os.path.exists(DOWNLOADS_DIR):
                current = set(os.listdir(DOWNLOADS_DIR))
                # En el primer arranque procesar todos los archivos existentes
                to_process = current if first_run else (current - seen)
                first_run = False

                for fname in to_process:
                    if not fname.endswith(('.mkv', '.mp4', '.avi')):
                        continue
                    fpath = os.path.join(DOWNLOADS_DIR, fname)

                    # Paso 1: buscar en torrents registrados
                    radarr_fname = None
                    with _torrents_lock:
                        for h, t in list(_torrents.items()):
                            if t['fname'].lower() == fname.lower():
                                radarr_fname = t.get('radarr_fname')
                                break

                    # Paso 2: si no hay entrada, identificar via TMDB
                    if not radarr_fname:
                        print(f"[Watcher] Sin registro para '{fname[:55]}', identificando via TMDB...")
                        radarr_fname = _guess_radarr_fname(fname)
                        if radarr_fname:
                            print(f"[Watcher] TMDB identificó: {radarr_fname[:60]}")
                        else:
                            # Fallback a Radarr API
                            radarr_fname = _guess_via_radarr(fname)
                            if not radarr_fname:
                                print(f"[Watcher] No se pudo identificar: {fname[:55]}")

                    # Paso 3: renombrar si el nombre es diferente
                    if radarr_fname and radarr_fname != fname:
                        dst = os.path.join(DOWNLOADS_DIR, radarr_fname)
                        if not os.path.exists(dst):
                            try:
                                os.rename(fpath, dst)
                                print(f"[Watcher] Renombrado: {fname[:45]} -> {radarr_fname[:45]}")
                            except Exception as e:
                                print(f"[Watcher] Error renombrando: {e}")

                seen = current
        except Exception as e:
            print(f"[Watcher] Error: {e}")
        time.sleep(30)


def _ed2k_watcher():
    """
    Reemplaza el watcher de PowerShell.
    Vigila /watch cada 10s. Cuando aparece un .ed2k nuevo,
    lo manda a eMule via WebInterface y lo elimina.
    """
    import time, os
    print("[ED2K Watcher] Iniciado, vigilando /watch...")
    processed = set()
    while True:
        try:
            if os.path.exists(BLACKHOLE):
                for fname in os.listdir(BLACKHOLE):
                    if not fname.endswith('.ed2k'):
                        continue
                    fpath = os.path.join(BLACKHOLE, fname)
                    if fpath in processed:
                        continue
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            ed2k = f.read().strip()
                        if not ed2k.startswith('ed2k://'):
                            continue
                        if _emule_send_ed2k(ed2k):
                            print(f"[ED2K Watcher] Enviado a eMule: {fname[:60]}")
                            os.remove(fpath)
                        else:
                            print(f"[ED2K Watcher] Fallo enviando a eMule: {fname[:60]}")
                        processed.add(fpath)
                    except Exception as e:
                        print(f"[ED2K Watcher] Error procesando {fname}: {e}")
        except Exception as e:
            print(f"[ED2K Watcher] Error: {e}")
        time.sleep(10)


if __name__ == "__main__":
    import threading
    threading.Thread(target=_rss_loop, daemon=True).start()
    threading.Thread(target=_incoming_watcher, daemon=True).start()
    threading.Thread(target=_ed2k_watcher, daemon=True).start()
    port = int(os.environ.get("PORT", 8085))
    print(f"[*] Servidor iniciado en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
