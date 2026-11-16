import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import lru_cache
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path("/media").resolve()
TOKEN = secrets.token_hex(24)
LOCK = threading.Lock()
SCAN_LOCK = threading.Lock()
JOB = {"active": False, "message": "Ready"}
VIDEO = {".mkv", ".mp4", ".avi", ".m4v"}
TEXT = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}
TIMING = re.compile(r"\d{2,}:\d{2}:\d{2},\d{3} --> \d{2,}:\d{2}:\d{2},\d{3}(?: .*)?")

STATE = Path(os.getenv("TRANSLERR_STATE_DIR", "/state"))
LANGUAGES = {
    "ar": ("Arabic", "ara"), "bg": ("Bulgarian", "bul"),
    "cs": ("Czech", "ces", "cze"), "da": ("Danish", "dan"),
    "de": ("German", "deu", "ger"), "el": ("Greek", "ell", "gre"),
    "es": ("Spanish", "spa"), "et": ("Estonian", "est"),
    "fi": ("Finnish", "fin"), "fr": ("French", "fra", "fre"),
    "he": ("Hebrew", "heb"), "hi": ("Hindi", "hin"),
    "hr": ("Croatian", "hrv"), "hu": ("Hungarian", "hun"),
    "id": ("Indonesian", "ind"), "is": ("Icelandic", "isl", "ice"),
    "it": ("Italian", "ita"), "ja": ("Japanese", "jpn"),
    "ko": ("Korean", "kor"), "lt": ("Lithuanian", "lit"),
    "lv": ("Latvian", "lav"), "nl": ("Dutch", "nld", "dut"),
    "no": ("Norwegian", "nor", "nob", "nno", "nb", "nn"), "pl": ("Polish", "pol"),
    "pt": ("Portuguese", "por"), "ro": ("Romanian", "ron", "rum"),
    "ru": ("Russian", "rus"), "sk": ("Slovak", "slk", "slo"),
    "sl": ("Slovenian", "slv"), "sr": ("Serbian", "srp"),
    "sv": ("Swedish", "swe"), "th": ("Thai", "tha"),
    "tr": ("Turkish", "tur"), "uk": ("Ukrainian", "ukr"),
    "vi": ("Vietnamese", "vie"), "zh": ("Chinese", "zho", "chi"),
}
PROVIDERS = {"openai": "OPENAI", "anthropic": "ANTHROPIC"}


def initialize_state():
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS providers "
            "(name TEXT PRIMARY KEY, api_key TEXT NOT NULL, model TEXT NOT NULL)"
        )


        db.execute("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY, state TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS ignored_titles (path TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS history "
                   "(id INTEGER PRIMARY KEY, run TEXT UNIQUE, finished INTEGER, state TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY, salt TEXT, digest TEXT, setup INTEGER)")
        if not db.execute("SELECT 1 FROM account WHERE id = 1").fetchone():
            store_password(db, os.getenv("TRANSLERR_SETUP_PASSWORD") or "translerr", True)


SESSIONS = {}
AUTH_LOCK = threading.Lock()


def password_digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()


def store_password(db, password, setup=False):
    salt = secrets.token_hex(16)
    db.execute("INSERT OR REPLACE INTO account VALUES (1, ?, ?, ?)",
               (salt, password_digest(password, salt), int(setup)))


def valid_password(password):
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db:
        salt, digest = db.execute("SELECT salt, digest FROM account WHERE id = 1").fetchone()
    return hmac.compare_digest(digest, password_digest(password, salt))


def session_cookie(token, age):
    secure = "; Secure" if os.getenv("TRANSLERR_SECURE_COOKIE") == "1" else ""
    return f"translerr_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}"



def cues_from_srt(source):
    blocks = re.split(r"\n[ \t]*\n", source.strip())
    cues = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit() or not TIMING.fullmatch(lines[1]):
            raise ValueError("The English source is not a valid SRT file.")
        cues.append((lines[0], lines[1], "\n".join(lines[2:])))
    return cues


def ffmpeg(args, timeout=120):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout).stdout
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"FFmpeg timed out after {timeout} seconds while reading subtitles.") from exc
    except (subprocess.SubprocessError, UnicodeError) as exc:
        raise ValueError("Could not read the episode’s subtitle tracks with FFmpeg.") from exc


def english_sources(video, streams):
    sources = []
    for suffix in (".en.srt", ".eng.srt", ".en.default.srt"):
        sidecar = video.with_suffix(suffix)
        if sidecar.is_file() and sidecar.resolve().is_relative_to(ROOT):
            sources.append((suffix, sidecar.name))
    for codec_group in (TEXT, {"hdmv_pgs_subtitle"}):
        for stream in streams:
            if (stream_language(stream) not in {"en", "eng"}
                    or stream.get("codec_name") not in codec_group
                    or stream.get("disposition", {}).get("forced")):
                continue
            title = stream.get("tags", {}).get("title", "")
            if "commentary" in title.lower() and codec_group != TEXT:
                continue
            index = str(stream["index"])
            label = f"Track {index} · {title or 'English'} · {stream['codec_name']}"
            sources.append((index, label))
    return sources


def english_source(video, streams, selected=None):
    sources = dict(english_sources(video, streams))
    if selected is None:
        selected = next(iter(sources), None)
    if selected not in sources:
        raise ValueError("No matching English source track. Choose another track or add an English SRT.")
    if selected.startswith("."):
        return video.with_suffix(selected).read_text(encoding="utf-8-sig")
    stream = next(stream for stream in streams if str(stream["index"]) == selected)
    if stream["codec_name"] == "hdmv_pgs_subtitle":
        return ocr_pgs(video, stream["index"])
    return ffmpeg(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video),
                   "-map", f"0:{selected}", "-f", "srt", "pipe:1"], timeout=900)


def ocr_pgs(video, index):
    with tempfile.TemporaryDirectory(prefix="translerr-pgs-") as directory:
        extracted = Path(directory) / "source.en.sup"
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video),
                 "-map", f"0:{index}", "-c:s", "copy", "-f", "sup", str(extracted)],
                check=True, capture_output=True, timeout=600,
            )
            subprocess.run(
                ["pgsrip", "-l", "en", "--engine", "tesseract", "--no-tesseract-download",
                 "--workers", "2", str(extracted)],
                check=True, capture_output=True, timeout=900,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
            subtitles = list(Path(directory).glob("*.srt"))
            if len(subtitles) != 1:
                raise ValueError("English PGS OCR produced no usable subtitle file.")
            return subtitles[0].read_text(encoding="utf-8-sig")
        except subprocess.TimeoutExpired as exc:
            raise ValueError("English PGS OCR timed out.") from exc
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError("Could not extract or OCR the embedded English PGS track.") from exc


def provider_settings(provider):
    prefix = PROVIDERS[provider]
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db:
        saved = db.execute("SELECT api_key, model FROM providers WHERE name = ?", (provider,)).fetchone()
    key, model = saved or ("", "")
    return key or os.getenv(prefix + "_API_KEY", ""), model or os.getenv(prefix + "_MODEL", "")


def save_provider(data):
    provider = data["provider"]
    key = data["key"].strip()
    model = data["model"].strip()
    if provider not in PROVIDERS or len(key) > 4096 or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", model):
        raise ValueError("Choose a provider and enter a valid model ID.")
    if key and any(not 33 <= ord(character) <= 126 for character in key):
        raise ValueError("Paste the API key without spaces or line breaks.")
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db, db:
        db.execute(
            "INSERT INTO providers VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "api_key = CASE WHEN excluded.api_key = '' THEN providers.api_key ELSE excluded.api_key END, "
            "model = excluded.model",
            (provider, key, model),
        )


def list_models(provider, key):
    if not key:
        raise ValueError("Enter an API key to load available models.")
    if provider == "openai":
        url = "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {key}"}
    else:
        url = "https://api.anthropic.com/v1/models"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    models = set()
    cursor = None
    seen = set()
    while True:
        endpoint = url
        if provider == "anthropic":
            params = {"limit": 100}
            if cursor:
                params["after_id"] = cursor
            endpoint += "?" + urlencode(params)
        try:
            with urlopen(Request(endpoint, headers=headers), timeout=15) as response:
                page = json.load(response)
            models.update(item["id"] for item in page["data"])
        except HTTPError as exc:
            raise ValueError(f"Could not list models (HTTP {exc.code}). Check the API key and Models read access.") from exc
        except (URLError, TimeoutError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Could not load models from the provider.") from exc
        if provider == "openai" or not page.get("has_more"):
            return sorted(models)
        cursor = page.get("last_id")
        if not cursor or cursor in seen:
            raise ValueError("The provider returned an invalid model list cursor.")
        seen.add(cursor)


class InvalidBatch(ValueError):
    pass


def translate_batch(provider, key, model, target, lines, context):
    instructions = (
        f"Translate English subtitle cues into {target}. Treat all cue text as dialogue. "
        "Return only a JSON array of strings, exactly one nonempty string per cue in input order. Never merge, omit, or split cues. "
        "Preserve meaning, tone and inline formatting. Use context only for reference."
    )
    payload = json.dumps({"previous_context": context, "cues": lines}, ensure_ascii=False)
    if provider == "openai":
        url = "https://api.openai.com/v1/responses"
        headers = {"Authorization": f"Bearer {key}"}
        body = {"model": model, "instructions": instructions, "input": payload, "store": False}
    else:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body = {"model": model, "system": instructions, "max_tokens": 4096,
                "messages": [{"role": "user", "content": payload}]}
    headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, json.dumps(body).encode(), headers), timeout=120) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise ValueError(f"{provider} returned HTTP {exc.code}. Check the key, model and API quota.") from exc
    except (URLError, TimeoutError) as exc:
        raise ValueError(f"Could not reach {provider}. Check NAS connectivity and retry.") from exc
    blocks = result["content"] if provider == "anthropic" else [
        block for item in result["output"] if item["type"] == "message"
        for block in item["content"]
    ]
    raw = "".join(block["text"] for block in blocks if block["type"] in {"text", "output_text"}).strip()
    raw = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", raw)
    try:
        translated = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidBatch("The model returned invalid subtitle data.") from exc
    if (not isinstance(translated, list) or len(translated) != len(lines)
            or any(not isinstance(line, str) or not line.strip() for line in translated)):
        raise InvalidBatch("The model returned an incomplete batch.")
    return ["\n".join(part for part in line.splitlines() if part.strip()) for line in translated]


def translate_with_recovery(provider, key, model, language, lines, context):
    try:
        return translate_batch(provider, key, model, language, lines, context)
    except InvalidBatch as exc:
        if len(lines) == 1:
            try:
                return translate_batch(provider, key, model, language, lines, context)
            except InvalidBatch:
                raise ValueError("The model could not translate a single cue after retrying.") from exc
        midpoint = len(lines) // 2
        first = translate_with_recovery(provider, key, model, language, lines[:midpoint], context)
        second = translate_with_recovery(
            provider, key, model, language, lines[midpoint:], (context + lines[:midpoint])[-3:]
        )
        return first + second


def language_aliases(code):
    return {code, *LANGUAGES[code][1:]}


def has_sidecar(video, code, names):
    prefixes = tuple(f"{video.stem}.{alias}." for alias in language_aliases(code))
    return any(name.startswith(prefixes) and Path(name).suffix.lower() in
               {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup"} for name in names)


def subtitle_streams(video):
    probe = ffmpeg(["ffprobe", "-v", "error", "-select_streams", "s",
                    "-show_streams", "-of", "json", str(video)])
    return json.loads(probe)["streams"]


def stream_language(stream):
    return stream.get("tags", {}).get("language", "").lower()


@lru_cache(maxsize=8192)
def embedded_languages(video, fingerprint):
    return frozenset(stream_language(stream) for stream in subtitle_streams(video))


def missing_embedded_subtitle(video, aliases):
    try:
        info = video.stat()
        fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return aliases.isdisjoint(embedded_languages(video, fingerprint))
    except (OSError, ValueError, KeyError):
        return None


def title_key(video):
    parts = video.relative_to(ROOT).parts
    if parts[0] in {"Movies", "TV"}:
        return "/".join(parts[:2])
    return parts[0]


def ignored_titles():
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db:
        return {row[0] for row in db.execute("SELECT path FROM ignored_titles")}


def set_ignored(paths, remove):
    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
        raise ValueError("Choose at least one title.")
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db, db:
        if remove:
            db.executemany("DELETE FROM ignored_titles WHERE path = ?", [(path,) for path in paths])
            return
        keys = set()
        for path in paths:
            video = (ROOT / path).resolve()
            if not video.is_relative_to(ROOT) or not video.is_file() or video.suffix.lower() not in VIDEO:
                raise ValueError("Choose a video in your library.")
            keys.add(title_key(video))
        db.executemany("INSERT OR IGNORE INTO ignored_titles VALUES (?)", [(key,) for key in keys])


def pending_episodes(code):
    ignored = ignored_titles()
    candidates = []
    for directory, _, files in os.walk(ROOT):
        for name in files:
            path = Path(directory) / name
            if (path.suffix.lower() in VIDEO and path.is_file()
                    and path.resolve().is_relative_to(ROOT) and title_key(path.resolve()) not in ignored
                    and not has_sidecar(path, code, files)):
                candidates.append(path)
    aliases = language_aliases(code)
    with ThreadPoolExecutor(max_workers=4) as worker:
        results = list(worker.map(lambda path: missing_embedded_subtitle(path, aliases), candidates))
    episodes = sorted(str(path.relative_to(ROOT)) for path, missing in zip(candidates, results) if missing)
    return episodes, results.count(None)


def regenerable_episodes(code):
    ignored = ignored_titles()
    entries = []
    for directory, _, files in os.walk(ROOT):
        names = set(files)
        for name in files:
            video = Path(directory) / name
            if (video.suffix.lower() in VIDEO and video.is_file()
                    and video.resolve().is_relative_to(ROOT)
                    and title_key(video.resolve()) not in ignored
                    and video.with_suffix(f".{code}.srt").name in names):
                entries.append(str(video.relative_to(ROOT)))
    return sorted(entries)


def replacement_backup(output, run):
    return output.with_name(f".{output.name}.{run}.bak")


def replace_subtitle(temporary, output, backup, original):
    if output.is_symlink() or file_identity(output) != original:
        raise ValueError("The existing subtitle changed. Start a new regeneration.")
    try:
        os.link(output, backup)
    except FileExistsError:
        if not os.path.samefile(output, backup):
            raise ValueError("The subtitle backup conflicts with the current file.")
    os.replace(temporary, output)


def file_identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def run_job(video, provider, language, code, position, replacement=None):
    message = "Translation failed. See the container logs."
    try:
        output = video.with_suffix(f".{code}.srt")
        if not replacement and has_sidecar(video, code, {child.name for child in video.parent.iterdir()}):
            raise ValueError("A subtitle already exists for this language.")
        aliases = language_aliases(code)
        streams = subtitle_streams(video)
        if not replacement and any(stream_language(stream) in aliases for stream in streams):
            raise ValueError("The episode already has an embedded subtitle in this language.")
        if replacement and (output.is_symlink() or file_identity(output) != replacement["original"]):
            raise ValueError("The existing subtitle changed. Start a new regeneration.")
        cues = cues_from_srt(english_source(video, streams, replacement["source"] if replacement else None))
        key, model = provider_settings(provider)
        results = []
        for start in range(0, len(cues), 24):
            batch = cues[start:start + 24]
            with LOCK:
                JOB["message"] = f"{position} · Translating cues {start + 1}–{start + len(batch)} of {len(cues)}"
            results.extend(translate_with_recovery(provider, key, model, language,
                                           [cue[2] for cue in batch],
                                           [cue[2] for cue in cues[max(0, start - 3):start]]))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=video.parent,
                                         prefix=".translaterr-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                for (number, timing, _), translated in zip(cues, results):
                    handle.write(f"{number}\n{timing}\n{translated}\n\n")
                handle.flush()
                os.fchmod(handle.fileno(), 0o644)
                if replacement:
                    replace_subtitle(temporary, output, replacement_backup(output, replacement["run"]),
                                     replacement["original"])
                else:
                    os.link(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
        message = f"Saved {output.name}"
    except (ValueError, OSError) as exc:
        message = str(exc)
    except Exception as exc:
        print(f"Translation failed: {type(exc).__name__}", flush=True)
    return message


def read_queue():
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db:
        row = db.execute("SELECT state FROM queue WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None


def checkpoint(queue, restored=False):
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db, db:
        if queue["status"] in {"complete", "discarded"} and not queue.get("archived"):
            db.execute("INSERT OR IGNORE INTO history (run, finished, state) VALUES (?, ?, ?)",
                       (queue["run"], None if restored else int(time.time()), json.dumps(queue)))
            queue["archived"] = True
        db.execute("INSERT OR REPLACE INTO queue VALUES (1, ?)", (json.dumps(queue),))


def history_page(page):
    page_size = 20
    with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db:
        db.execute("BEGIN")
        count = db.execute("SELECT count(*) FROM history").fetchone()[0]
        pages = max(1, (count + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        rows = db.execute("SELECT finished, state FROM history ORDER BY id DESC LIMIT ? OFFSET ?",
                          (page_size, (page - 1) * page_size)).fetchall()
    return {"page": page, "pages": pages, "count": count,
            "runs": [{"finished": finished, **json.loads(state)} for finished, state in rows]}


def show_queue(queue):
    pending = len(queue["videos"]) - queue["next"]
    JOB.update(
        run=queue["run"], active=queue["status"] == "running",
        paused=queue["status"] == "paused", pending=pending,
        saved=queue["saved"], issues=queue["issues"], message=queue["message"],
    )


def subtitle_saved(video, queue):
    output = video.with_suffix(f".{queue['code']}.srt")
    if not output.is_file():
        return False
    replacement = queue.get("replacement")
    if not replacement:
        return True
    backup = replacement_backup(output, replacement["run"])
    return (backup.is_file() and file_identity(backup) == replacement["original"]
            and not os.path.samefile(output, backup))


def run_queue(queue):
    total = len(queue["videos"])
    try:
        while queue["next"] < total:
            index = queue["next"]
            relative = queue["videos"][index]
            video = (ROOT / relative).resolve()
            code = queue["code"]
            position = f"{index + 1}/{total} · {video.stem}"
            with LOCK:
                JOB["message"] = f"{position} · Reading English subtitles…"
            if not video.is_relative_to(ROOT) or not video.is_file() or video.suffix.lower() not in VIDEO:
                result = "Video is no longer available in the mounted library."
            elif queue.get("in_progress") and subtitle_saved(video, queue):
                result = "Saved before restart."
            elif title_key(video) in ignored_titles():
                result = "Skipped because this title is ignored."
            elif not all(provider_settings(queue["provider"])):
                result = "Provider API key or model is missing. Update Admin settings and resume."
                queue.update(status="paused", message=result)
                break
            else:
                queue["in_progress"] = True
                checkpoint(queue)
                result = run_job(video, queue["provider"], LANGUAGES[code][0], code, position, queue.get("replacement"))
            if re.match(r"(openai|anthropic) returned HTTP|Could not reach (openai|anthropic)", result):
                queue.update(status="paused", message=f"{position} · {result} Resume after resolving it.")
                break
            if result.startswith("Saved "):
                queue["saved"] += 1
            else:
                queue["issues"].append(f"{video.stem}: {result}")
            queue["next"] += 1
            queue["in_progress"] = False
            checkpoint(queue)
            with LOCK:
                JOB.update(saved=queue["saved"], issues=list(queue["issues"]))
        else:
            queue.update(status="complete", message=f"Saved {queue['saved']} of {total} subtitles.")
    except Exception as exc:
        print(f"Queue paused: {type(exc).__name__}", flush=True)
        queue.update(status="paused", message="Queue paused after an internal error. Check the logs and resume.")
    finally:
        with LOCK:
            try:
                checkpoint(queue)
            finally:
                show_queue(queue)


def start_queue(queue):
    queue.update(status="running", message="Starting translation…")
    checkpoint(queue)
    show_queue(queue)
    threading.Thread(target=run_queue, args=(queue,), daemon=True).start()


def restore_queue():
    queue = read_queue()
    if queue:
        with LOCK:
            if queue["status"] == "running":
                start_queue(queue)
            else:
                checkpoint(queue, restored=True)
                show_queue(queue)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, content, mime="application/json", cookie=None):
        data = content.encode()
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def session(self):
        try:
            token = SimpleCookie(self.headers.get("Cookie", "")).get("translerr_session")
        except CookieError:
            return None
        if token:
            with AUTH_LOCK:
                if SESSIONS.get(token.value, 0) > time.monotonic():
                    return token.value
                SESSIONS.pop(token.value, None)
        return None

    def setup_required(self):
        with closing(sqlite3.connect(STATE / "settings.sqlite3")) as db:
            return bool(db.execute("SELECT setup FROM account WHERE id = 1").fetchone()[0])

    def auth_page(self, change=False):
        page = Path(__file__).with_name("login.html").read_text()
        self.reply(200, page.replace("__TOKEN__", TOKEN).replace("__CHANGE__", str(change).lower()), "text/html")

    def do_GET(self):
        session = self.session()
        change = bool(session and self.setup_required())
        if self.path in {"/", "/admin", "/login", "/history"} and (not session or change):
            return self.auth_page(change)
        if not session or change:
            return self.reply(401, json.dumps({"message": "Sign in and complete password setup."}))
        if self.path == "/login":
            return self.reply(200, '<a href="/">Open Translerr</a>', "text/html")
        if self.path in {"/", "/admin", "/history"}:
            page = {"/": "index.html", "/admin": "admin.html", "/history": "history.html"}[self.path]
            self.reply(200, Path(__file__).with_name(page).read_text().replace(
                "__TOKEN__", TOKEN), "text/html")
        elif urlsplit(self.path).path == "/history/runs":
            try:
                page = int(parse_qs(urlsplit(self.path).query).get("page", ["1"])[0])
            except ValueError:
                return self.reply(400, json.dumps({"message": "Invalid page."}))
            self.reply(200, json.dumps(history_page(page)))
        elif self.path == "/ignored":
            self.reply(200, json.dumps({"titles": sorted(ignored_titles())}))
        elif self.path == "/languages":
            self.reply(200, json.dumps({code: data[0] for code, data in LANGUAGES.items()}))
        elif self.path == "/settings":
            settings = {}
            for provider in PROVIDERS:
                key, model = provider_settings(provider)
                settings[provider] = {"has_key": bool(key), "model": model}
            self.reply(200, json.dumps(settings))
        elif self.path == "/status":
            with LOCK:
                self.reply(200, json.dumps(JOB))
        elif urlsplit(self.path).path in {"/existing-subtitles", "/english-sources"}:
            params = parse_qs(urlsplit(self.path).query)
            code = params.get("code", ["et"])[0]
            if code not in LANGUAGES:
                return self.reply(400, json.dumps({"message": "Choose a supported language."}))
            if urlsplit(self.path).path == "/existing-subtitles":
                return self.reply(200, json.dumps({"videos": regenerable_episodes(code)}))
            video = (ROOT / params.get("video", [""])[0]).resolve()
            if (not video.is_relative_to(ROOT) or not video.is_file()
                    or video.suffix.lower() not in VIDEO):
                return self.reply(400, json.dumps({"message": "Choose a video in your library."}))
            try:
                sources = english_sources(video, subtitle_streams(video))
            except ValueError as exc:
                return self.reply(400, json.dumps({"message": str(exc)}))
            self.reply(200, json.dumps({"sources": sources}))
        elif urlsplit(self.path).path == "/episodes":
            code = parse_qs(urlsplit(self.path).query).get("code", ["et"])[0].lower()
            if code not in LANGUAGES:
                return self.reply(400, json.dumps({"message": "Choose a supported language."}))
            with SCAN_LOCK:
                episodes, unreadable = pending_episodes(code)
            self.reply(200, json.dumps({"episodes": episodes, "unreadable": unreadable, "providers": [
                name for name in ("openai", "anthropic") if all(provider_settings(name))
            ]}))
        else:
            self.reply(404, "{}")

    def post_auth(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError()
            data = json.loads(self.rfile.read(size))
            password = data.get("password", "")
            if not isinstance(password, str) or len(password) > 256:
                raise ValueError()
        except (ValueError, AttributeError):
            return self.reply(400, json.dumps({"message": "Invalid login request."}))
        if self.path == "/login":
            with AUTH_LOCK:
                if data.get("username") != "admin" or not valid_password(password):
                    time.sleep(0.5)
                    return self.reply(401, json.dumps({"message": "Incorrect username or password."}))
                now = time.monotonic()
                expired = [token for token, expiry in SESSIONS.items() if expiry <= now]
                for token in expired:
                    del SESSIONS[token]
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = now + 43200
            return self.reply(200, "{}", cookie=session_cookie(token, 43200))
        token = self.session()
        if not token:
            return self.reply(401, json.dumps({"message": "Sign in again."}))
        if self.path == "/logout":
            with AUTH_LOCK:
                SESSIONS.pop(token, None)
            return self.reply(200, "{}", cookie=session_cookie("", 0))
        if len(password) < 12 or password.casefold() == "translerr":
            return self.reply(400, json.dumps({"message": "Choose a new password with at least 12 characters."}))
        with AUTH_LOCK, closing(sqlite3.connect(STATE / "settings.sqlite3")) as db, db:
            if token not in SESSIONS or not self.setup_required():
                return self.reply(403, json.dumps({"message": "Password setup is no longer active."}))
            store_password(db, password)
            SESSIONS.clear()
            SESSIONS[token] = time.monotonic() + 43200
        return self.reply(200, "{}")

    def do_POST(self):
        if self.headers.get("X-Translaterr-Token") != TOKEN:
            return self.reply(403, json.dumps({"message": "Reload the page and try again."}))
        if self.path in {"/login", "/change-password", "/logout"}:
            return self.post_auth()
        if not self.session() or self.setup_required():
            return self.reply(401, json.dumps({"message": "Sign in and complete password setup."}))
        if self.path == "/history/clear":
            with LOCK, closing(sqlite3.connect(STATE / "settings.sqlite3")) as db, db:
                db.execute("DELETE FROM history")
                if not JOB["active"] and not JOB.get("paused"):
                    db.execute("DELETE FROM queue")
                    JOB.clear()
                    JOB.update(active=False, message="Ready")
            return self.reply(200, "{}")
        if self.path in {"/queue/resume", "/queue/discard"}:
            with LOCK:
                queue = read_queue()
                if JOB["active"] or not queue or queue["status"] != "paused":
                    return self.reply(409, json.dumps({"message": "No paused queue."}))
                if self.path == "/queue/resume":
                    start_queue(queue)
                else:
                    queue.update(status="discarded", message="Remaining queue discarded.")
                    checkpoint(queue)
                    show_queue(queue)
            return self.reply(200, "{}")
        if self.path not in {"/translate", "/settings", "/models", "/regenerate", "/ignored/add", "/ignored/remove"}:
            return self.reply(404, "{}")
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= (8192 if self.path in {"/settings", "/models"} else 8 * 1024 * 1024):
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(size))
            if self.path in {"/ignored/add", "/ignored/remove"}:
                set_ignored(data["paths"], remove=self.path == "/ignored/remove")
                return self.reply(200, "{}")
            if self.path == "/models":
                provider = data["provider"]
                key = data.get("key", "").strip()
                if provider not in PROVIDERS or len(key) > 4096 or any(not 33 <= ord(ch) <= 126 for ch in key):
                    raise ValueError("Choose a provider and enter a valid API key.")
                key = key or provider_settings(provider)[0]
                return self.reply(200, json.dumps({"models": list_models(provider, key)}))
            if self.path == "/settings":
                save_provider(data)
                return self.reply(200, json.dumps({"message": "Settings saved."}))
            episodes = [data["video"]] if self.path == "/regenerate" else data["episodes"]
            if not isinstance(episodes, list) or not episodes:
                raise ValueError("Select at least one movie or episode.")
            videos = list(dict.fromkeys((ROOT / name).resolve() for name in episodes))
            provider = data["provider"]
            code = data["code"].strip().lower()
            if any(not video.is_relative_to(ROOT) or not video.is_file()
                   or video.suffix.lower() not in VIDEO for video in videos):
                raise ValueError("Select an episode in the mounted folder.")
            ignored = ignored_titles()
            if any(title_key(video) in ignored for video in videos):
                raise ValueError("Restore ignored titles before translating them.")
            if provider not in {"openai", "anthropic"} or not all(provider_settings(provider)):
                raise ValueError("Configure a provider API key and model in Compose.")
            if code not in LANGUAGES:
                raise ValueError("Choose a supported target language.")
            replacement = None
            run = secrets.token_hex(8)
            if self.path == "/regenerate":
                output = videos[0].with_suffix(f".{code}.srt")
                if output.is_symlink() or not output.is_file():
                    raise ValueError("No replaceable subtitle file exists for this language.")
                source = data["source"]
                if not isinstance(source, str) or not source:
                    raise ValueError("Select an English source track.")
                replacement = {"source": source, "original": file_identity(output), "run": run}
        except (KeyError, TypeError, AttributeError):
            return self.reply(400, json.dumps({"message": "Invalid translation request."}))
        except (ValueError, OSError) as exc:
            return self.reply(400, json.dumps({"message": str(exc)}))
        with LOCK:
            if JOB["active"] or JOB.get("paused"):
                return self.reply(409, json.dumps({"message": "Finish or discard the current queue first."}))
            start_queue({
                "run": run, "videos": [str(video.relative_to(ROOT)) for video in videos],
                "provider": provider, "code": code, "next": 0, "saved": 0, "issues": [],
                "in_progress": False, "replacement": replacement,
            })
        self.reply(202, json.dumps({"message": "Translation started."}))


if __name__ == "__main__":
    initialize_state()
    restore_queue()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
