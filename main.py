"""Выгрузка Копии (Snapshot) карты MapGenie для локального вьювера.

Пример:
    python3 main.py https://mapgenie.io/forza-horizon-6/maps/japan
    python3 main.py https://mapgenie.io/death-stranding-2/maps/australia --max-zoom 15

Термины — см. CONTEXT.md.
"""

import argparse
import json
import math
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
MAP_URL_RE = re.compile(r"^https://mapgenie\.io/([\w-]+)/maps/([\w-]+)/?$")

# Python с python.org на macOS идёт без корневых сертификатов; системный бандл есть всегда.
SSL_CONTEXT = ssl.create_default_context()
if Path("/etc/ssl/cert.pem").exists():
    SSL_CONTEXT.load_verify_locations("/etc/ssl/cert.pem")


class Blocked(Exception):
    """Сервер отказал так, будто нас приняли за бота (403/429/Cloudflare)."""


class Fetcher:
    """HTTP-клиент с общим ограничением частоты запросов для всех потоков."""

    def __init__(self, delay: float):
        self.delay = delay
        self._lock = threading.Lock()
        self._next_at = 0.0

    def _wait_turn(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self.delay
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, retries: int = 3) -> bytes | None:
        """Тело ответа или None, если файла нет. Сетевые сбои повторяет, остальное — исключения."""
        for attempt in range(retries + 1):
            try:
                return self._get_once(url)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if isinstance(e, urllib.error.HTTPError) or attempt == retries:
                    raise
                print(f"  повтор {attempt + 1}/{retries}: {url} ({e})")
                time.sleep(2 * (attempt + 1))

    def _get_once(self, url: str) -> bytes | None:
        self._wait_turn()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            body = e.read(2000).decode("utf-8", "replace").lower()
            # CDN тайлов — S3: на несуществующий ключ он отвечает 403 AccessDenied в XML.
            if e.code == 403 and "<code>accessdenied</code>" in body:
                return None
            if e.code in (403, 429, 503) or "cloudflare" in body:
                raise Blocked(f"{e.code} on {url}") from e
            raise


def extract_js_object(html: str, var: str) -> dict:
    m = re.search(rf"window\.{var}\s*=\s*", html)
    if not m:
        raise ValueError(f"window.{var} не найден на странице")
    obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    return obj


def extract_marker_sprite_url(html: str) -> str:
    m = re.search(r"MARKER_IMAGES_URL\s*=\s*'([^']+)'", html)
    if not m:
        raise ValueError("MARKER_IMAGES_URL не найден на странице")
    return m.group(1)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "default"


def lnglat_to_tile(lng: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lng + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def collect_bounds(locations: list[dict], regions: list[dict]) -> tuple[float, float, float, float]:
    """(west, south, east, north) по точкам и полигонам регионов."""
    lngs = [loc["lng"] for loc in locations]
    lats = [loc["lat"] for loc in locations]

    def walk(coords):
        if isinstance(coords[0], (int, float)):
            lngs.append(coords[0])
            lats.append(coords[1])
        else:
            for c in coords:
                walk(c)

    for region in regions:
        for feature in region.get("features") or []:
            walk(feature["geometry"]["coordinates"])
    return min(lngs), min(lats), max(lngs), max(lats)


def tiles_in_bounds(bounds, min_zoom: int, max_zoom: int, pad: int = 1):
    west, south, east, north = bounds
    for z in range(min_zoom, max_zoom + 1):
        x0, y0 = lnglat_to_tile(west, north, z)
        x1, y1 = lnglat_to_tile(east, south, z)
        last = 2**z - 1
        for y in range(max(y0 - pad, 0), min(y1 + pad, last) + 1):
            for x in range(max(x0 - pad, 0), min(x1 + pad, last) + 1):
                yield z, x, y


def save(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def build_snapshot(map_data: dict, game: dict, api_data: dict, page_url: str, tile_sets: list[dict]) -> dict:
    locations = [
        {
            "id": loc["id"],
            "category_id": loc["category_id"],
            "title": loc["title"],
            "description": loc.get("description"),
            "lat": float(loc["latitude"]),
            "lng": float(loc["longitude"]),
            "media": [
                {"file": m["file_name"], "title": m.get("title"), "url": m["url"]}
                for m in loc.get("media") or []
                if m.get("type") == "image"
            ],
        }
        for loc in api_data["locations"]
    ]
    groups = [
        {
            "id": g["id"],
            "title": g["title"],
            "order": g["order"],
            "color": g.get("color"),
            "categories": [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "icon": c["icon"],
                    "order": c["order"],
                    "premium": c.get("premium", False),
                }
                for c in sorted(g["categories"], key=lambda c: c["order"])
            ],
        }
        for g in sorted(map_data["groups"], key=lambda g: g["order"])
    ]
    regions = api_data.get("regions") or []
    config = map_data["mapConfig"]
    return {
        "source": page_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "game": {"id": game["id"], "slug": game["slug"], "title": game["title"]},
        "map": map_data["map"],
        "view": {
            "center": [config["start_lng"], config["start_lat"]],
            "zoom": config["initial_zoom"],
        },
        "bounds": collect_bounds(locations, regions),
        "tile_sets": tile_sets,
        "groups": groups,
        "locations": locations,
        "regions": [{"id": r["id"], "title": r["title"], "features": r.get("features")} for r in regions],
    }


def run(args) -> int:
    m = MAP_URL_RE.match(args.url)
    if not m:
        print(f"Ожидался URL вида https://mapgenie.io/<игра>/maps/<карта>, получено: {args.url}")
        return 2
    game_slug, map_slug = m.groups()
    out = Path(args.out) / game_slug / map_slug
    fetcher = Fetcher(args.delay)

    print(f"Страница {args.url}")
    html = fetcher.get(args.url).decode("utf-8")
    map_data = extract_js_object(html, "mapData")
    game = extract_js_object(html, "game")
    tiles_cdn = json.loads(re.search(r'window\.tilesCdnUrl\s*=\s*("[^"]+")', html).group(1))
    sprite_png_url = extract_marker_sprite_url(html)

    map_id = map_data["map"]["id"]
    print(f"Карта {map_data['map']['title']} (id {map_id}), API точек")
    api_data = json.loads(fetcher.get(f"https://mapgenie.io/api/v1/maps/{map_id}/data"))

    wanted = {s.strip().lower() for s in args.tile_sets.split(",")} if args.tile_sets else None
    tile_sets = []
    for ts in sorted(map_data["mapConfig"]["tile_sets"], key=lambda t: t["order"]):
        slug = slugify(ts["name"])
        if wanted and slug not in wanted and ts["name"].lower() not in wanted:
            continue
        tiles_max = ts.get("tiles_max_zoom") or ts["max_zoom"]
        if args.max_zoom is not None:
            tiles_max = min(tiles_max, args.max_zoom)
        tile_sets.append(
            {
                "name": ts["name"],
                "slug": slug,
                "extension": ts["extension"],
                "remote_pattern": tiles_cdn + ts["pattern"],
                "min_zoom": ts["min_zoom"],
                "max_zoom": ts["max_zoom"],
                "tiles_max_zoom": tiles_max,
            }
        )
    if not tile_sets:
        names = [t["name"] for t in map_data["mapConfig"]["tile_sets"]]
        print(f"Ни один набор тайлов не подошёл под --tile-sets. Есть: {', '.join(names)}")
        return 2

    snapshot = build_snapshot(map_data, game, api_data, args.url, tile_sets)
    save(out / "snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=1).encode("utf-8"))
    print(f"Точек {len(snapshot['locations'])}, групп {len(snapshot['groups'])} -> {out / 'snapshot.json'}")

    # Спрайт: MapLibre сам дописывает .json/.png и @2x к базовому URL.
    sprite_base = re.sub(r"(@2x)?\.png(\?.*)?$", "", sprite_png_url)
    for suffix in ("", "@2x"):
        for ext in ("json", "png"):
            target = out / "sprite" / f"markers{suffix}.{ext}"
            if not target.exists():
                body = fetcher.get(f"{sprite_base}{suffix}.{ext}")
                if body is None:
                    print(f"Нет файла спрайта markers{suffix}.{ext}")
                    continue
                save(target, body)

    jobs = []
    for ts in tile_sets:
        ts_dir = out / "tiles" / ts["slug"]
        missing_file = ts_dir / "missing.txt"
        missing = set(missing_file.read_text().split()) if missing_file.exists() else set()
        for z, x, y in tiles_in_bounds(snapshot["bounds"], ts["min_zoom"], ts["tiles_max_zoom"]):
            key = f"{z}/{y}/{x}"
            target = ts_dir / f"{key}.{ts['extension']}"
            if key in missing or target.exists():
                continue
            url = ts["remote_pattern"].replace("{z}", str(z)).replace("{y}", str(y)).replace("{x}", str(x))
            jobs.append((ts_dir, key, url, target))

    if args.with_media:
        for loc in snapshot["locations"]:
            for media in loc["media"]:
                target = out / "media" / media["file"]
                if not target.exists():
                    jobs.append((None, None, media["url"], target))

    print(f"К загрузке: {len(jobs)} файлов (пауза {args.delay} с, потоков {args.workers})")
    done = 0
    new_missing: dict[Path, list[str]] = {}
    lock = threading.Lock()
    stop = threading.Event()

    def fetch_one(job):
        nonlocal done
        ts_dir, key, url, target = job
        if stop.is_set():
            return
        body = fetcher.get(url)
        with lock:
            if body is None:
                if ts_dir is not None:
                    new_missing.setdefault(ts_dir, []).append(key)
            else:
                save(target, body)
            done += 1
            if done % 100 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}")

    blocked = None
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for future in [pool.submit(fetch_one, job) for job in jobs]:
                try:
                    future.result()
                except Blocked as e:
                    blocked = e
                    stop.set()
    finally:
        for ts_dir, keys in new_missing.items():
            ts_dir.mkdir(parents=True, exist_ok=True)
            with open(ts_dir / "missing.txt", "a") as f:
                f.write("".join(k + "\n" for k in keys))

    if blocked:
        print(f"Остановлено: сервер блокирует запросы ({blocked}). Скачанное сохранено, перезапуск продолжит.")
        return 1
    print(f"Готово: {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Выгрузить Копию карты MapGenie.")
    parser.add_argument("url", help="https://mapgenie.io/<игра>/maps/<карта>")
    parser.add_argument("--out", default="snapshots", help="корневая папка Копий (по умолчанию snapshots)")
    parser.add_argument("--max-zoom", type=int, help="не качать тайлы глубже этого зума")
    parser.add_argument("--tile-sets", help="наборы тайлов через запятую, например spring,winter (по умолчанию все)")
    parser.add_argument("--with-media", action="store_true", help="скачать фото точек в media/")
    parser.add_argument("--delay", type=float, default=0.5, help="минимальная пауза между запросами, с")
    parser.add_argument("--workers", type=int, default=2)
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
