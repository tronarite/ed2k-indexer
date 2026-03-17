"""
Indexer TV scraper
Extrae episodios de series organizados por temporada y calidad.

Estructura de Indexer para series:
- Una página por serie con todos los episodios
- Cada bloque h2 = una calidad (WEBRip 1080p, BDRip 1080p, Micro4K...)
- Dentro de cada bloque, episodios de una o varias temporadas
- Nombres: "Serie SxEE Titulo.mkv" o "Serie S01E01.mkv"
- IMDB ID siempre presente en la página
"""

import re
import random
from bs4 import BeautifulSoup
from scraper import fetch_ed2k, get_imdb_id, BASE

# Patrones de episodio: SxEE o S01E01
EP_PATTERN = re.compile(r'(\d+)[xX](\d+)|[Ss](\d+)[Ee](\d+)')

# Calidades a filtrar (no queremos estas)
SKIP_QUALITY = re.compile(r'2160|4K|Micro4K|UHD|BSO|\.rar|\.srt|\.nfo|\.txt', re.I)
SKIP_FNAME   = re.compile(r'BSO|\.rar|\.srt|\.nfo|\.txt|SUBTITULO|MEDIAINFO', re.I)

# Prioridad de fuente (menor = mejor), igual que películas
SRC_PRIORITY = {
    'WEB-DL': 0, 'WEBDL': 0,
    'WEBRip': 1, 'WebRip': 1,
    'BDRip':  2, 'BDRemux': 2, 'BluRay': 2, 'Bluray': 2,
    'MicroHD': 3, 'MHD': 3,
    'HD':     4,
}

def src_prio(h2_text):
    h = h2_text.upper()
    if 'WEB-DL' in h or 'WEBDL' in h:
        return 0
    if 'WEBRIP' in h or 'WEBDRIP' in h:
        return 1
    if 'BDRIP' in h or 'BDREMUX' in h or 'BLURAY' in h or 'BLU-RAY' in h:
        return 2
    if 'MICROHD' in h or 'MHD' in h:
        return 3
    return 4

def parse_episode(fname):
    """Extrae (season, episode) del nombre de archivo. Devuelve (None, None) si no encuentra."""
    m = EP_PATTERN.search(fname)
    if not m:
        return None, None
    if m.group(1) is not None:
        # Formato SxEE
        return int(m.group(1)), int(m.group(2))
    else:
        # Formato S01E01
        return int(m.group(3)), int(m.group(4))


def get_series_page(session, url):
    """
    Descarga la página de una serie y extrae toda la estructura.
    Devuelve (imdb_id, bloques)
    donde bloques = [{"quality": str, "src_prio": int, "episodes": {(s,e): [(eid,code,fname), ...]}}]
    """
    resp = session.get(url, timeout=15)
    soup = BeautifulSoup(resp.text, 'html.parser')

    imdb_id = get_imdb_id(soup)

    dl_pattern = re.compile(r"Download\((\d+),'([^']+)'\)")
    bloques = []

    for h2 in soup.find_all('h2'):
        h2_text = h2.get_text(strip=True)

        # Saltar bloques de BSO, 2160p, etc.
        if SKIP_QUALITY.search(h2_text):
            continue

        # Recoger episodios de este bloque
        parent = h2.find_parent()
        episodes = {}  # (season, ep) -> [(eid, code, fname)]

        for a in (parent or h2).find_all('a', href=True):
            m = dl_pattern.search(a['href'])
            if not m:
                continue
            eid, code = m.group(1), m.group(2)
            fname = a.get_text(strip=True)

            if SKIP_FNAME.search(fname):
                continue

            season, ep = parse_episode(fname)
            if season is None:
                # Si no tiene número de episodio, ignorar
                continue

            key = (season, ep)
            if key not in episodes:
                episodes[key] = []
            episodes[key].append((eid, code, fname))

        if episodes:
            bloques.append({
                'quality': h2_text,
                'src_prio': src_prio(h2_text),
                'episodes': episodes,
            })

    return imdb_id, bloques


def get_season_episodes(session, url, season):
    """
    Dado el URL de una serie y el número de temporada,
    devuelve lista de (season, ep, ed2k) para esa temporada,
    eligiendo el mejor bloque de calidad disponible.
    """
    imdb_id, bloques = get_series_page(session, url)

    if not bloques:
        print(f'[TV] Sin bloques encontrados en {url}')
        return imdb_id, []

    # Filtrar bloques que tengan episodios de la temporada pedida
    bloques_validos = [b for b in bloques if any(s == season for s, e in b['episodes'])]

    if not bloques_validos:
        # Si no hay bloque específico, buscar bloque con toda la serie (muchas temporadas)
        bloques_validos = [b for b in bloques if len(set(s for s, e in b['episodes'])) > 1]

    if not bloques_validos:
        print(f'[TV] No hay episodios para temporada {season}')
        return imdb_id, []

    # Elegir el mejor bloque: menor src_prio (WEBDL > WEBRip > BDRip)
    bloques_validos.sort(key=lambda b: b['src_prio'])
    mejor = bloques_validos[0]
    print(f'[TV] Bloque elegido: {mejor["quality"]} (prio={mejor["src_prio"]})')

    # Extraer episodios de la temporada pedida
    eps_temporada = {(s, e): datos for (s, e), datos in mejor['episodes'].items() if s == season}
    print(f'[TV] {len(eps_temporada)} episodios en temporada {season}')

    results = []
    for (s, e), datos in sorted(eps_temporada.items()):
        # Elegir el primer link de cada episodio (suele haber solo uno por bloque)
        eid, code, fname = datos[0]
        links = fetch_ed2k(session, eid, code)
        if links:
            results.append((s, e, links[0], fname))
            print(f'  [TV] S{s:02d}E{e:02d} OK: {fname[:60]}')
        else:
            print(f'  [TV] S{s:02d}E{e:02d} sin ed2k: {fname[:60]}')

    return imdb_id, results
