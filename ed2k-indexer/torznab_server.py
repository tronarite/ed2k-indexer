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

os.makedirs(BLACKHOLE, exist_ok=True)

_session = None

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
    # IMPORTANTE: XviD/DivX van primero porque BDrip.XviD es un DVD encode
    if re.search(r'XVID|DIVX|BD\+DVD', f):
        src = "DVD"                                  # filtrar
    elif re.search(r'MICRO4K|UHDRIP|UHD.?RIP', f):
        src = "Bluray-2160p"                         # filtrar
    elif re.search(r'2160P', f):
        src = "Bluray-2160p"                         # filtrar
    elif re.search(r'BDRIP|BDREMUX|BLU.?RAY|MICROHD|\bMHD\b|BR\+HQTV|BLURAY.RIP', f):
        src = f"Bluray-{res}"
    elif re.search(r'WEB.?DL|WEBDL|\bNFLX\b|\bATVP\b|\bNF\b|\bAPTV\b', f):
        src = f"WEBDL-{res}"
    elif re.search(r'WEBRIP|WEB.RIP|\bWEBRIP\b', f):
        src = f"WEBRip-{res}"
    elif re.search(r'HDRIP|HD.?RIP', f):
        src = f"WEBDL-{res}"                         # HDRip → WEBDL
    elif re.search(r'\bHD\b', f):
        src = f"Bluray-{res}"                        # HD genérico → BDRip
    elif re.search(r'DVD.?RIP|DIVX', f):
        src = "DVD"                                  # filtrar
    else:
        src = f"WEBRip-{res}"                        # fallback seguro

    return res, src


def make_radarr_title(fname, title_en, year):
    """Genera un titulo en formato que Radarr puede parsear."""
    res, src = detect_quality(fname)
    # Formato: "Movie Title Year Quality" - Radarr lo parsea bien
    src_tag = src.replace("Bluray", "BDRip").replace("WEBDL", "WEB-DL").replace("WEBRip", "WEBRip")
    return f"{title_en} ({year}) {src_tag}"


def build_xml(results, title_en="", year=""):
    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:torznab", "http://torznab.com/schemas/2015/feed")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Hispashare ed2k"
    ET.SubElement(ch, "description").text = "Hispashare ed2k indexer"
    ET.SubElement(ch, "link").text = BASE

    for r in results:
        for ed2k in r.get("ed2k_links", []):
            fname = parse_fname(ed2k)
            size  = parse_size(ed2k)
            res, src = detect_quality(fname)

            # Solo filtrar basura real: subtítulos, NFOs, RAR, 3D, BSO
            fname_upper = fname.upper()
            if any(x in fname_upper for x in ["BSO", ".SRT", ".RAR", ".NFO", ".TXT", "SUBTITULO", "MEDIAINFO", ".3D.", "3D.SBS", "3D.HOU", "HSBS", "HTAB"]):
                print(f"[SKIP] {fname[:60]} (BSO/sub/extra)")
                continue
            # Filtrar archivos sin tamaño real (< 10 MB)
            try:
                if int(size) < 10 * 1024 * 1024:
                    print(f"[SKIP] {fname[:60]} (tamaño {int(size)//1024//1024} MB)")
                    continue
            except Exception:
                pass

            # Titulo en formato que Radarr puede parsear (inglés + año + calidad)
            if title_en and year:
                title = make_radarr_title(fname, title_en, year)
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
            enc.set("url",    f"http://hispashare-indexer:8085/download?ed2k={requests.utils.quote(ed2k)}&apikey={API_KEY}&title={requests.utils.quote(title_en or '')}&year={year or ''}")
            enc.set("length", size)
            enc.set("type",   "application/x-bittorrent")

            for name, val in [
                ("category", "2000"),
                ("seeders",  "50"),
                ("peers",    "100"),
                ("size",     size),
                ("quality",  src),
            ]:
                a = ET.SubElement(item, "torznab:attr")
                a.set("name", name); a.set("value", val)

            print(f"[XML] {title[:50]} | {src} | {int(size)//1024//1024} MB")

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


def build_search_queries(title_es, title_en):
    """
    Construye la lista de queries a probar en orden, de más específico a más genérico.
    La idea: si el título exacto no aparece en Hispashare, buscar con la primera
    palabra clave y verificar IMDB resultado a resultado (como con Star Wars, Evangelion...).
    """
    queries = []
    seen = set()

    def add(q):
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)

    # 1. Título ES completo (sin año al final, sin caracteres raros)
    add(title_es)

    # 2. Título EN completo (si es diferente y está en ASCII)
    if title_en and all(ord(c) < 256 for c in title_en):
        add(title_en)

    # 3. Primera palabra del título ES (sin stopwords ni números sueltos)
    stopwords = {"the","a","an","de","del","la","el","los","las","una","un"}
    for title in [title_es, title_en]:
        if not title:
            continue
        words = [w for w in re.sub(r"[^a-zA-Z0-9\s]", " ", title).split()
                 if len(w) >= 3 and w.lower() not in stopwords]
        if words:
            add(words[0])           # solo la primera palabra clave
            if len(words) >= 2:
                add(f"{words[0]} {words[1]}")  # dos palabras clave

    return queries

@app.route("/")
@app.route("/api")
def api():
    if request.args.get("apikey","") != API_KEY:
        return Response("Unauthorized", status=401)

    t      = request.args.get("t", "search")
    query  = request.args.get("q","") or request.args.get("title","")
    imdbid = request.args.get("imdbid","")

    # Sonarr usa t=tvsearch
    if t == "tvsearch":
        tvdbid  = request.args.get("tvdbid","")
        season  = request.args.get("season","")
        episode = request.args.get("ep","")
        season  = int(season)  if season  else 1
        episode = int(episode) if episode else None
        return handle_tvsearch(query, tvdbid, season, episode)

    if t == "caps":
        return Response("""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="Hispashare ed2k"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <movie-search available="yes" supportedParams="q,title,imdbid"/>
    <tv-search available="yes" supportedParams="q,title,tvdbid,season,ep"/>
  </searching>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2010" name="Movies/Foreign"/>
    </category>
    <category id="5000" name="TV"/>
  </categories>
</caps>""", mimetype="application/xml")

    # Variables para el titulo en inglés y año (para el XML de Radarr)
    title_en = ""
    year_str = ""

    # Resolver titulo en ESPAÑOL via TMDB, guardando también el inglés
    if imdbid:
        try:
            find_url = f"https://api.themoviedb.org/3/find/tt{imdbid}?api_key=TU_TMDB_API_KEY&external_source=imdb_id&language=es-ES"
            j = requests.get(find_url, timeout=5).json()
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
            j = requests.get(f"https://www.omdbapi.com/?i=tt{imdbid}&apikey=TU_OMDB_API_KEY", timeout=5).json()
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
            search_url = f"https://api.themoviedb.org/3/search/movie?api_key=TU_TMDB_API_KEY&query={requests.utils.quote(query)}&language=es-ES"
            j = requests.get(search_url, timeout=5).json()
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
                        ext = requests.get(f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key=TU_TMDB_API_KEY", timeout=5).json()
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

        # Construir lista de queries: título completo primero, luego keyword corta
        search_queries = build_search_queries(query, title_en)
        print(f"[*] Queries a probar: {search_queries}")

        results = []
        for sq in search_queries:
            print(f"[*] Probando: '{sq}'")
            results = do_search(sq, target_imdb=imdbid if imdbid else None)
            if results:
                print(f"[*] Match con query: '{sq}'")
                break

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

        # Radarr se encarga de filtrar calidad según su Quality Profile.
        # Aquí solo eliminamos basura real (subs, NFO, 3D, BSO, archivos <10MB).
        SKIP_TERMS = ["3D.SBS", "3D.HOU", "HSBS", "HTAB", ".3D.", "BSO",
                      ".SRT", ".RAR", ".NFO", ".TXT", "SUBTITULO", "MEDIAINFO"]

        final_results = []
        total_links = 0
        for r in results:
            valid_links = []
            for ed2k in r.get("ed2k_links", []):
                fname = parse_fname(ed2k)
                fname_upper = fname.upper()
                size = parse_size(ed2k)
                try:
                    sz = int(size)
                except:
                    sz = 0
                if any(t in fname_upper for t in SKIP_TERMS):
                    print(f"  [SKIP] {fname[:55]} (3D/BSO/sub)")
                    continue
                if sz < 10 * 1024 * 1024:
                    print(f"  [SKIP] {fname[:55]} (muy pequeno)")
                    continue
                _, src = detect_quality(fname)
                print(f"  [OK] {fname[:55]} | {src} | {sz//1024//1024} MB")
                valid_links.append(ed2k)
                total_links += 1
            if valid_links:
                final_results.append({**r, "ed2k_links": valid_links})

        results = final_results
        print(f"[OK] {total_links} enlaces enviados a Radarr")
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

        for name, val in [
            ("category",  "5000"),
            ("seeders",   "50"),
            ("peers",     "100"),
            ("size",      str(size)),
            ("quality",   src),
            ("season",    str(season)),
            ("episode",   str(ep)),
        ]:
            a = ET.SubElement(item, "torznab:attr")
            a.set("name", name); a.set("value", val)

        print(f"[TV-XML] {title} | {src} | {int(size)//1024//1024} MB")

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


@app.route("/tvsearch")
@app.route("/api/tvsearch")
def tvsearch_redirect():
    return api()


def handle_tvsearch(query, tvdbid, season, episode):
    """Manejador principal para t=tvsearch de Sonarr."""
    from scraper_tv import get_season_episodes

    title_en = ""
    year_str = ""
    imdbid   = ""

    # Resolver serie via TMDB usando tvdbid
    if tvdbid:
        try:
            find_url = f"https://api.themoviedb.org/3/find/{tvdbid}?api_key=TU_TMDB_API_KEY&external_source=tvdb_id&language=es-ES"
            j = requests.get(find_url, timeout=5).json()
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
                    ext = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key=TU_TMDB_API_KEY", timeout=5).json()
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
            search_url = f"https://api.themoviedb.org/3/search/tv?api_key=TU_TMDB_API_KEY&query={requests.utils.quote(query)}&language=es-ES"
            j = requests.get(search_url, timeout=5).json()
            tv_results = j.get("results", [])
            if tv_results:
                r0 = tv_results[0]
                titulo_es = r0.get("name", "").strip()
                title_en  = r0.get("original_name", "").strip() or query
                release   = r0.get("first_air_date", "")
                year_str  = release[:4] if release else ""
                tmdb_id   = r0.get("id")
                if tmdb_id:
                    ext = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key=TU_TMDB_API_KEY", timeout=5).json()
                    imdb_raw = ext.get("imdb_id", "")
                    if imdb_raw and imdb_raw.startswith("tt"):
                        imdbid = imdb_raw[2:]
                if titulo_es and titulo_es.lower() != query.lower():
                    print(f"[TV] TMDB traduce '{query}' -> ES:'{titulo_es}' EN:'{title_en}' ({year_str})")
                    query = titulo_es
        except Exception as e:
            print(f"[WARN] TMDB TV search error: {e}")

    if not query and not tvdbid:
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

# Ruta al ejecutable de eMule en el HOST (se pasa como variable de entorno).
# Si no está definida, el reinicio automático se desactiva y solo se loguea el error.
EMULE_EXE = os.environ.get("EMULE_EXE", r"C:\Program Files (x86)\eMule\emule.exe")


def _emule_is_alive() -> bool:
    """Comprueba si la WebInterface de eMule responde. Timeout muy corto."""
    try:
        resp = requests.get(f"http://{EMULE_HOST}:{EMULE_PORT}/", timeout=4)
        return resp.status_code == 200
    except Exception:
        return False


def _emule_restart_host() -> bool:
    """
    Reinicia eMule en el host Windows vía subprocess.
    Solo funciona si el indexer corre directamente en el host (no en Docker).
    Si corre en Docker, este llamada fallará silenciosamente y se loguea.
    """
    import subprocess, time
    try:
        subprocess.run(["taskkill", "/F", "/IM", "emule.exe"],
                       capture_output=True, timeout=10)
        time.sleep(4)
        subprocess.Popen([EMULE_EXE])
        print(f"[eMule] Reiniciado: {EMULE_EXE}")
        time.sleep(15)  # Esperar a que la WebInterface arranque
        return True
    except FileNotFoundError:
        print("[eMule] No se puede reiniciar desde Docker (taskkill no disponible). "
              "Configura emule_restart.py en el host para reinicio automático.")
        return False
    except Exception as e:
        print(f"[eMule] Error al reiniciar: {e}")
        return False


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
    """
    Manda un enlace ed2k a eMule via WebInterface.
    Si la WebInterface no responde, intenta reiniciar eMule y reintenta una vez.
    """
    base = f"http://{EMULE_HOST}:{EMULE_PORT}"

    # 1. Comprobar si eMule está vivo antes de intentar enviar
    if not _emule_is_alive():
        print("[eMule] WebInterface no responde, intentando reiniciar...")
        _emule_restart_host()
        # Tras reiniciar, reset de sesión para forzar re-login
        _emule_session["ses"] = None
        if not _emule_is_alive():
            print("[eMule] Sigue sin responder tras reinicio. Se reintentará en el próximo ciclo.")
            return False

    # 2. Login si no hay sesión activa
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
    """
    Vigila /downloads cada 30s. Cuando aparece un archivo nuevo,
    busca si coincide con algún torrent registrado y lo renombra
    al nombre que Radarr puede importar automáticamente.
    """
    import time, os
    print("[Watcher] Iniciado, vigilando /downloads...")
    seen = set()
    while True:
        try:
            if os.path.exists(DOWNLOADS_DIR):
                current = set(os.listdir(DOWNLOADS_DIR))
                new_files = current - seen
                for fname in new_files:
                    if fname.endswith(('.mkv', '.mp4', '.avi')):
                        print(f"[Watcher] Nuevo archivo: {fname[:60]}")
                        # Buscar en torrents registrados
                        with _torrents_lock:
                            for h, t in list(_torrents.items()):
                                if t['fname'].lower() == fname.lower():
                                    radarr_fname = t.get('radarr_fname')
                                    if radarr_fname and radarr_fname != fname:
                                        src = os.path.join(DOWNLOADS_DIR, fname)
                                        dst = os.path.join(DOWNLOADS_DIR, radarr_fname)
                                        if not os.path.exists(dst):
                                            os.rename(src, dst)
                                            print(f"[Watcher] Renombrado: {fname[:50]} -> {radarr_fname[:50]}")
                                    break
                seen = current
        except Exception as e:
            print(f"[Watcher] Error: {e}")
        time.sleep(30)


def _incoming_watcher():
    """
    Vigila /downloads cada 30s. Cuando aparece un archivo nuevo,
    busca si coincide con algún torrent registrado y lo renombra
    al nombre que Radarr puede importar automáticamente.
    """
    import time, os
    print("[Watcher] Iniciado, vigilando /downloads...")
    seen = set()
    while True:
        try:
            if os.path.exists(DOWNLOADS_DIR):
                current = set(os.listdir(DOWNLOADS_DIR))
                new_files = current - seen
                for fname in new_files:
                    if fname.endswith(('.mkv', '.mp4', '.avi')):
                        print(f"[Watcher] Nuevo archivo: {fname[:60]}")
                        with _torrents_lock:
                            for h, t in list(_torrents.items()):
                                if t['fname'].lower() == fname.lower():
                                    radarr_fname = t.get('radarr_fname')
                                    if radarr_fname and radarr_fname != fname:
                                        src = os.path.join(DOWNLOADS_DIR, fname)
                                        dst = os.path.join(DOWNLOADS_DIR, radarr_fname)
                                        if not os.path.exists(dst):
                                            os.rename(src, dst)
                                            print(f"[Watcher] Renombrado: {fname[:50]} -> {radarr_fname[:50]}")
                                    break
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
    threading.Thread(target=_incoming_watcher, daemon=True).start()
    threading.Thread(target=_ed2k_watcher, daemon=True).start()
    port = int(os.environ.get("PORT", 8085))
    print(f"[*] Servidor iniciado en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
