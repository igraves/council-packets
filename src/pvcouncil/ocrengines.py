"""Switchable OCR engines for the deed images, all run on this machine.

Tesseract reads clean typed pages well but fails on the 1950s photostat
negatives. These engines can stand in for it, on a boxed region in the deed
viewer or on a whole document:

  tesseract  Tesseract 5, the engine the pipeline was built on
  surya      Surya 2 (Datalab), a small model trained for document OCR, served
             through llama.cpp; installed as an isolated tool with
             `uv tool install surya-ocr` and `brew install llama.cpp`
  olmocr     olmOCR-2 (Allen AI, a Qwen2.5-VL fine-tune for scanned pages), via Ollama
  qwen-vl    Qwen2.5-VL 7B, a general vision-language model, via Ollama
  gemma4     Gemma 4 26B, via Ollama

All but Tesseract are vision-language models. They read degraded type far better
than Tesseract, but they can also write what a page ought to say rather than
what it does: these declarations follow stock wording, so a model can produce a
plausible clause from a smudge. Their readings are kept apart from the OCR the
report is built on, and a document's report text switches to one only when a
reader adopts it in the viewer after checking it against the image.

Whole-document readings live in deeds/ocr/engines/<engine>/<key>.json
(gitignored: the text names signers and owners), with the fingerprint of the
image they were read from. Adoptions are in deeds/ocr/engines/adopted.json.
"""

import base64
import html
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import fitz

from pvcouncil import plats, transcripts

READINGS_DIR = plats.DEEDS_DIR / "ocr" / "engines"
ADOPTED_PATH = READINGS_DIR / "adopted.json"
OLLAMA = "http://127.0.0.1:11434"
_lock = threading.Lock()

VLM_PROMPT = (
    "Transcribe all of the text in this image exactly as it is written, line by line, in reading order. "
    "Do not correct spelling, complete words, summarize, or add anything that is not visible. "
    "If a word cannot be read, write [illegible]. Output only the transcription."
)
# olmOCR-2 was trained on this prompt; its reply starts with a YAML front matter block
OLMOCR_PROMPT = (
    "Attached is one page of a document that you must process. Just return the plain text representation "
    "of this document as if you were reading it naturally. Convert equations to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax "
    "![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying values for the "
    "primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram parameters."
)


@dataclass(frozen=True)
class Engine:
    name: str
    label: str
    kind: str  # "classic" (Tesseract) or "vlm" (a vision-language model, which can invent text)
    model: str = ""  # Ollama model name; engines without one run their own program
    max_side: int = 0  # model engines: longest side, in pixels, of the image sent
    page_dpi: int = 300  # whole-document renders for engines that take any size
    think: bool | None = None  # Ollama models that think unless told not to


ENGINES = {e.name: e for e in [
    Engine("tesseract", "Tesseract", "classic"),
    Engine("surya", "Surya 2", "vlm", max_side=2048),
    Engine("olmocr", "olmOCR-2 7B", "vlm", "hf.co/lmstudio-community/olmOCR-2-7B-1025-GGUF:Q4_K_M", 1288),
    Engine("qwen-vl", "Qwen2.5-VL 7B", "vlm", "qwen2.5vl:7b", 1600),
    Engine("gemma4", "Gemma 4 26B", "vlm", "gemma4:26b", 1600, think=False),
]}


# ---- availability ----

_avail: tuple[float, dict] = (0.0, {})


def _ollama_models() -> set[str] | None:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            return {m["name"] for m in json.load(r)["models"]}
    except (OSError, ValueError):
        return None


def available(refresh: bool = False) -> dict[str, dict]:
    """Each engine with whether it can run now, and if not, how to set it up."""
    global _avail
    if not refresh and time.time() - _avail[0] < 30:
        return _avail[1]
    models = _ollama_models()
    out = {}
    for e in ENGINES.values():
        if e.name == "tesseract":
            ok, why = bool(shutil.which("tesseract")), "brew install tesseract"
        elif e.name == "surya":  # Surya 2 serves its model through llama.cpp
            ok = bool(shutil.which("surya_ocr") and shutil.which("llama-server"))
            why = "uv tool install surya-ocr && brew install llama.cpp"
        elif models is None:
            ok, why = False, "start Ollama (ollama serve)"
        else:
            ok = e.model in models or f"{e.model}:latest" in models
            why = f"ollama pull {e.model}"
        out[e.name] = {"label": e.label, "kind": e.kind, "model": e.model, "ok": ok, "setup": None if ok else why}
    _avail = (time.time(), out)
    return out


def get(name: str) -> Engine:
    if name not in ENGINES:
        raise ValueError(f"unknown OCR engine {name!r}; choose from {', '.join(ENGINES)}")
    return ENGINES[name]


# ---- image clean-up ----

def _otsu(hist: list[int]) -> int:
    total = sum(hist)
    sum_all = sum(i * c for i, c in enumerate(hist))
    best, best_t, w0, sum0 = -1.0, 128, 0, 0
    for t in range(256):
        w0 += hist[t]
        if w0 == 0 or w0 == total:
            continue
        sum0 += t * hist[t]
        m0, m1 = sum0 / w0, (sum_all - sum0) / (total - w0)
        between = w0 * (total - w0) * (m0 - m1) ** 2
        if between > best:
            best, best_t = between, t
    return best_t


def is_negative(pix: fitz.Pixmap) -> bool:
    """White type on black: most of a negative's pixels are dark."""
    data = bytes(pix.samples)[:: max(1, pix.width * pix.height // 200_000)]
    return sum(data) / max(1, len(data)) < 110


def prepare(pix: fitz.Pixmap, invert: bool, threshold: bool) -> fitz.Pixmap:
    """A grayscale render, optionally inverted, with its levels stretched between the 1st and
    99th percentile (faint photostats use a narrow band), optionally forced to black and white."""
    w, h = pix.width, pix.height
    data = bytes(pix.samples)
    if pix.stride != w:  # drop any row padding
        data = b"".join(data[i * pix.stride: i * pix.stride + w] for i in range(h))
    if invert:
        data = data.translate(bytes(255 - v for v in range(256)))
    hist = [0] * 256
    for v, n in Counter(data[:: max(1, len(data) // 400_000)]).items():
        hist[v] = n
    total, acc, lo, hi = sum(hist), 0, 0, 255
    for v in range(256):
        acc += hist[v]
        if acc <= total * 0.01:
            lo = v
        if acc <= total * 0.99:
            hi = v
    if hi - lo > 8:
        lut = bytes(min(255, max(0, (v - lo) * 255 // (hi - lo))) for v in range(256))
        data = data.translate(lut)
        hist = [sum(hist[u] for u in range(256) if lut[u] == v) for v in range(256)]
    if threshold:
        t = _otsu(hist)
        data = data.translate(bytes(255 if v > t else 0 for v in range(256)))
    return fitz.Pixmap(fitz.csGRAY, w, h, data, False)


def fit(pix: fitz.Pixmap, max_side: int) -> fitz.Pixmap:
    """Scale an image so its longest side is max_side: what a VLM's vision encoder expects."""
    side = max(pix.width, pix.height)
    if not max_side or side == max_side:
        return pix
    s = max_side / side
    if s < 1:
        return fitz.Pixmap(pix, max(1, round(pix.width * s)), max(1, round(pix.height * s)))
    # upscale small crops through a PDF render; Pixmap only shrinks
    doc = fitz.open()
    page = doc.new_page(width=pix.width, height=pix.height)
    page.insert_image(page.rect, pixmap=pix)
    return page.get_pixmap(matrix=fitz.Matrix(s, s), colorspace=fitz.csGRAY)


# ---- reading ----

def _clean_vlm(text: str) -> str:
    text = re.sub(r"^\s*---\n.*?\n---\n?", "", text, flags=re.S)  # olmOCR front matter
    text = re.sub(r"^```\w*\n|\n```\s*$", "", text.strip())
    # a model that loses its place repeats a word, a phrase or a line; keep one
    text = re.sub(r"(\[illegible\][\s.,]*){2,}", "[illegible] ", text)
    text = re.sub(r"(\b\S+(?:\s+\S+){0,4}?)(?:\s+\1\b){3,}", r"\1", text)
    out, run = [], 0
    for line in text.splitlines():
        run = run + 1 if out and line.strip() and line == out[-1] else 0
        if run < 3:
            out.append(line)
    return "\n".join(out).strip()


def _ollama(e: Engine, png: bytes, region: bool) -> str:
    body = {
        "model": e.model, "stream": False, "keep_alive": "15m",
        "messages": [{"role": "user", "content": OLMOCR_PROMPT if e.name == "olmocr" else VLM_PROMPT,
                      "images": [base64.b64encode(png).decode()]}],
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1536 if region else 4096},
    }
    if e.think is not None:
        body["think"] = e.think
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return _clean_vlm(json.load(r)["message"]["content"])
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"{e.label}: {err.read().decode(errors='replace')[:200]}") from err


def _surya(pngs: list[bytes]) -> list[str]:
    """Surya loads its models on each run, so a whole document goes through in one call."""
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "in", Path(d) / "out"
        src.mkdir()
        for i, png in enumerate(pngs):
            (src / f"{i:04d}.png").write_bytes(png)
        p = subprocess.run(["surya_ocr", str(src), "--output_dir", str(out)], capture_output=True, text=True)
        found = list(out.rglob("results.json"))
        if not found:
            raise RuntimeError(f"Surya: {(p.stderr or p.stdout).strip()[-300:]}")
        res = json.loads(found[0].read_text())
    def page_text(pg: dict) -> str:
        if "text_lines" in pg:  # Surya 1
            return "\n".join(ln["text"] for ln in pg["text_lines"])
        # Surya 2: blocks of HTML in reading order
        blocks = sorted(pg.get("blocks", []), key=lambda b: b.get("reading_order", 0))
        return "\n\n".join(t for b in blocks if not b.get("skipped")
                           if (t := html.unescape(re.sub(r"<br\s*/?>", "\n", re.sub(r"</?(?!br)\w[^>]*>", "", b.get("html", "")))).strip()))

    return ["\n\n".join(page_text(pg) for pg in res.get(f"{i:04d}", [])) for i in range(len(pngs))]


def _tesseract(png: bytes, psm: int) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(png)
        f.flush()
        return subprocess.run(["tesseract", f.name, "-", "--psm", str(psm), "-c", "preserve_interword_spaces=1"],
                              capture_output=True, text=True).stdout.strip()


def read(name: str, images: list[fitz.Pixmap], psm: int = 6, region: bool = False) -> list[str]:
    """Read prepared grayscale images with one engine; returns one text per image."""
    e = get(name)
    if not available()[name]["ok"]:
        raise RuntimeError(f"{e.label} isn't set up: {available(refresh=True)[name]['setup'] or 'retry'}")
    if e.model:
        return [_ollama(e, fit(p, e.max_side).tobytes("png"), region) for p in images]
    if name == "surya":
        return _surya([fit(p, e.max_side).tobytes("png") for p in images])
    return [_tesseract(p.tobytes("png"), psm) for p in images]


def words(text: str) -> int:
    return len(re.findall(r"\b[A-Za-z]{3,}\b", text.replace("[illegible]", "")))


# ---- whole documents ----

def _slug(key: str) -> str:
    return key.replace(":", "-")


def reading_path(name: str, key: str) -> Path:
    return READINGS_DIR / name / f"{_slug(key)}.json"


def load_reading(name: str, key: str) -> dict | None:
    p = reading_path(name, key)
    return json.loads(p.read_text()) if p.exists() else None


def readings_for(key: str) -> dict[str, dict]:
    return {n: r for n in ENGINES if (r := load_reading(n, key))}


def reading_text(r: dict) -> str:
    return "\n\n".join(f"[page {p['page']}]\n{p['text']}" for p in r["pages"])


def read_document(name: str, key: str, pdf_path: str | Path, rot: int = 0, invert: str = "auto",
                  progress=None) -> dict:
    """Read every page of a document with one engine and save the reading. invert is "auto"
    (invert a page that's mostly dark), "on", or "off". progress(done, total) is called per page."""
    e = get(name)
    pages, started = [], time.time()
    with fitz.open(pdf_path) as pdf:
        n = pdf.page_count
        imgs, inverted = [], []
        for pg in pdf:
            # VLMs get the page at about their input size; the classic engines at their page dpi
            dpi = e.page_dpi if not e.max_side else max(150, e.max_side * 72 / max(pg.rect.width, pg.rect.height))
            pix = pg.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72).prerotate(rot), colorspace=fitz.csGRAY)
            inv = invert == "on" or (invert == "auto" and is_negative(pix))
            imgs.append(prepare(pix, inv, False))
            inverted.append(inv)
    if name == "surya":
        texts = read(name, imgs)
        pages = [{"page": i + 1, "text": t, "inverted": inverted[i]} for i, t in enumerate(texts)]
        if progress:
            progress(n, n)
    else:
        for i, img in enumerate(imgs):
            t0 = time.time()
            text = read(name, [img], psm=3)[0]
            pages.append({"page": i + 1, "text": text, "inverted": inverted[i], "seconds": round(time.time() - t0, 1)})
            if progress:
                progress(i + 1, n)
    out = {
        "engine": name, "label": e.label, "model": e.model, "key": key,
        "ran": time.strftime("%Y-%m-%d %H:%M"), "seconds": round(time.time() - started, 1),
        "settings": {"rot": rot, "invert": invert},
        "fingerprint": transcripts.fingerprint(pdf_path),
        "words": sum(words(p["text"]) for p in pages), "pages": pages,
    }
    p = reading_path(name, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1) + "\n")
    tmp.replace(p)
    return out


# ---- adoption: which reading the report uses for a document ----

def adopted() -> dict[str, str]:
    return json.loads(ADOPTED_PATH.read_text()) if ADOPTED_PATH.exists() else {}


def adopt(key: str, name: str | None) -> None:
    """Use an engine's reading for a document's report text in place of Tesseract's (None: back to Tesseract)."""
    if name is not None and not load_reading(name, key):
        raise ValueError(f"no {name} reading of {key}")
    with _lock:
        data = adopted()
        if name:
            data[key] = name
        else:
            data.pop(key, None)
        READINGS_DIR.mkdir(parents=True, exist_ok=True)
        ADOPTED_PATH.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def base_text(key: str, ocr_text: str, pdf_path: str | Path, chosen: dict | None = None) -> tuple[str, dict | None]:
    """The text a document's report is built on: its adopted engine reading if it has one, else
    the pipeline's Tesseract OCR. Returns (text, {engine, label, stale}) or (ocr_text, None)."""
    name = (chosen if chosen is not None else adopted()).get(key)
    r = load_reading(name, key) if name else None
    if not r:
        return ocr_text, None
    stale = Path(pdf_path).exists() and r.get("fingerprint") != transcripts.fingerprint(pdf_path)
    return reading_text(r), {"engine": name, "label": r["label"], "stale": stale}
