#!/usr/bin/env python3
"""Daily 2414world crawler: deterministic HTML extraction, PDF versions, Gloo upload.

Example:
  python crawler_gloo.py --mode daily
  python crawler_gloo.py --mode full
  python crawler_gloo.py --mode single --url https://www.2414world.com/blog/example
  python crawler_gloo.py --mode daily --dry-run

Dependencies: requests, beautifulsoup4, lxml, python-dotenv, reportlab.

The Gloo upload endpoint and request fields below are retained from the supplied
implementation and MUST be checked against the target tenant/API documentation.
Crawl state and generated PDFs are stored under ./storage by default. In cloud
deployment, set CRAWLER_STORAGE_DIR to a mounted persistent volume and back it up.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape

log = logging.getLogger("crawler2414")
BASE_URL = "https://www.2414world.com/"
LISTING_PATHS = ("/blog", "/missions", "/podcast", "/church-support/bulletin-updates")
IGNORED_PATHS = ("/news", "/news/latest-videos", "/news/blessing", "/page/employment")
NON_HTML = re.compile(r"\.(?:pdf|jpg|jpeg|png|gif|svg|webp|zip|mp4|mp3|css|js|xml)$", re.I)
# Persistent application storage. Override CRAWLER_STORAGE_DIR in the cloud
# environment to point this at a mounted persistent volume.
DEFAULT_STORAGE_DIR = Path(
    os.getenv("CRAWLER_STORAGE_DIR", str(Path(__file__).resolve().parent / "storage"))
).expanduser()
DEFAULT_DB = DEFAULT_STORAGE_DIR / "crawler_history.sqlite3"
DEFAULT_OUTPUT = DEFAULT_STORAGE_DIR / "generated_pdfs"


@contextlib.contextmanager
def crawl_lock(path: Path):
    """Single-host process lock; cron invocations fail cleanly when overlapping."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        handle.write("0"); handle.flush(); handle.seek(0)
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1))
        except ImportError:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError) as exc:
        handle.close()
        raise RuntimeError(f"another crawler run already holds {path}") from exc
    try:
        yield
    finally:
        unlock(); handle.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str, base: str = BASE_URL) -> str:
    p = urlparse(urljoin(base, (url or "").strip()))
    path = re.sub(r"/{2,}", "/", p.path).rstrip("/") or "/"
    # Remove common analytics parameters, but retain functional query parameters.
    tracking = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in tracking and not k.lower().startswith("utm_")]
    query.sort()
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), path, "", urlencode(query, doseq=True), ""))


def is_eligible(url: str, base: str = BASE_URL, exclusions: Iterable[str] = ()) -> bool:
    p, root = urlparse(normalize_url(url, base)), urlparse(base)
    if p.scheme not in ("http", "https") or p.hostname != (root.hostname or "").lower():
        return False
    if NON_HTML.search(p.path):
        return False
    return not any(p.path.lower().startswith(x.lower()) for x in (*IGNORED_PATHS, *exclusions))


@dataclass
class Block:
    kind: str
    text: str
    level: int = 0
    ordered: bool = False


@dataclass
class Page:
    url: str
    title: str
    blocks: list[Block]
    etag: str | None = None
    last_modified: str | None = None
    status: int = 200
    content_hash: str = ""

    def __post_init__(self):
        # Include the title: title-only edits must create a new version too.
        normalize_text = lambda value: re.sub(r"\s+", " ", value).strip()
        canonical = "\n".join(
            ["title:" + normalize_text(self.title)] +
            [b.kind + ":" + normalize_text(b.text) for b in self.blocks]
        )
        self.content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_page(html: str, url: str) -> Page | None:
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("script,style,noscript,nav,header,footer,aside,form,[role='navigation'],.cookie,.cookies,.cookie-banner"):
        node.decompose()
    main = soup.select_one("main, article, .article, .post, .blog-detail, .content") or soup.body or soup
    title = (main.find("h1") or soup.find("title"))
    title_text = title.get_text(" ", strip=True) if title else urlparse(url).path.strip("/").replace("/", " ").title()
    blocks: list[Block] = []
    for node in main.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote"]):
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if node.name.startswith("h"):
            blocks.append(Block("heading", text, int(node.name[1:])))
        elif node.name == "li":
            parent = node.find_parent(["ol", "ul"])
            blocks.append(Block("list", text, ordered=bool(parent and parent.name == "ol")))
        else:
            blocks.append(Block("paragraph", text))
    if not blocks:
        return None
    return Page(normalize_url(url), title_text, blocks)


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
          canonical_url TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, last_checked_at TEXT,
          last_successful_fetch_at TEXT, last_http_status INTEGER, etag TEXT, last_modified TEXT,
          current_content_hash TEXT, current_version_number INTEGER NOT NULL DEFAULT 0,
          current_pdf_storage_id TEXT, current_pdf_filename TEXT, page_title TEXT,
          page_status TEXT NOT NULL DEFAULT 'active', updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS versions (
          id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL REFERENCES pages(canonical_url),
          version_number INTEGER NOT NULL, content_hash TEXT NOT NULL, title TEXT,
          source_url TEXT NOT NULL, discovered_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
          uploaded_at TEXT NOT NULL, http_status INTEGER, pdf_filename TEXT NOT NULL,
          gloo_storage_id TEXT, change_type TEXT NOT NULL, UNIQUE(canonical_url, version_number),
          UNIQUE(canonical_url, content_hash)
        );
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, mode TEXT NOT NULL,
          stats_json TEXT
        );
        CREATE TABLE IF NOT EXISTS upload_attempts (
          id INTEGER PRIMARY KEY,
          canonical_url TEXT NOT NULL REFERENCES pages(canonical_url),
          version_number INTEGER NOT NULL,
          attempted_at TEXT NOT NULL,
          status TEXT NOT NULL,
          gloo_storage_id TEXT,
          response_json TEXT,
          error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(page_status);
        CREATE INDEX IF NOT EXISTS idx_upload_attempts_url
          ON upload_attempts(canonical_url, attempted_at);
        """)
        self.db.commit()
        self._lock = threading.Lock()
        self._migrate_versions_hash_constraint()

    def _migrate_versions_hash_constraint(self):
        """Drop the old UNIQUE(canonical_url, content_hash) constraint safely.

        --force may intentionally store another archived copy of identical content.
        Older databases created by this crawler had that uniqueness constraint.
        """
        indexes = self.db.execute("PRAGMA index_list(versions)").fetchall()
        for index in indexes:
            if not index["unique"]:
                continue
            cols = [r["name"] for r in self.db.execute(
                f'PRAGMA index_info("{index["name"]}")').fetchall()]
            if cols == ["canonical_url", "content_hash"]:
                with self._lock:
                    self.db.execute("PRAGMA foreign_keys=OFF")
                    self.db.executescript("""
                    CREATE TABLE versions_new (
                      id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL REFERENCES pages(canonical_url),
                      version_number INTEGER NOT NULL, content_hash TEXT NOT NULL, title TEXT,
                      source_url TEXT NOT NULL, discovered_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
                      uploaded_at TEXT NOT NULL, http_status INTEGER, pdf_filename TEXT NOT NULL,
                      gloo_storage_id TEXT, change_type TEXT NOT NULL,
                      UNIQUE(canonical_url, version_number)
                    );
                    INSERT INTO versions_new SELECT * FROM versions;
                    DROP TABLE versions;
                    ALTER TABLE versions_new RENAME TO versions;
                    """)
                    self.db.execute("PRAGMA foreign_keys=ON")
                    self.db.commit()
                break

    def close(self):
        with self._lock:
            self.db.close()

    def record_upload_attempt(
        self,
        url: str,
        version_number: int,
        status: str,
        storage_id_value: str | None = None,
        response: dict | None = None,
        error_message: str | None = None,
    ):
        """Persist every Gloo upload attempt, including failures."""
        with self._lock:
            self.db.execute(
                """INSERT INTO upload_attempts
                   (canonical_url, version_number, attempted_at, status,
                    gloo_storage_id, response_json, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    url, version_number, now(), status, storage_id_value,
                    json.dumps(response, default=str) if response is not None else None,
                    error_message,
                ),
            )
            self.db.commit()

    def tracked(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM pages WHERE page_status != 'excluded'"))

    def get(self, url: str):
        return self.db.execute("SELECT * FROM pages WHERE canonical_url=?", (url,)).fetchone()

    def start_run(self, mode: str) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO runs(started_at,mode) VALUES(?,?)", (now(), mode)); self.db.commit(); return cur.lastrowid

    def finish_run(self, run_id: int, stats: dict):
        with self._lock:
            self.db.execute("UPDATE runs SET ended_at=?, stats_json=? WHERE id=?", (now(), json.dumps(stats), run_id)); self.db.commit()

    def check(self, url: str, status: int, etag: str | None, last_modified: str | None):
        with self._lock:
            timestamp = now()
            self.db.execute(
                "INSERT INTO pages(canonical_url,first_seen_at,last_checked_at,last_http_status,etag,last_modified,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(canonical_url) DO UPDATE SET "
                "last_checked_at=excluded.last_checked_at,last_http_status=excluded.last_http_status,"
                "etag=COALESCE(excluded.etag,etag),last_modified=COALESCE(excluded.last_modified,last_modified),"
                "updated_at=excluded.updated_at",
                (url, timestamp, timestamp, status, etag, last_modified, timestamp),
            )
            self.db.commit()

    def commit_version(self, page: Page, filename: str, storage_id: str | None, change: str, fetched: str):
        with self._lock:
            t = now(); old = self.get(page.url); version = (old["current_version_number"] if old else 0) + 1
            self.db.execute("INSERT INTO pages(canonical_url,first_seen_at,last_checked_at,last_successful_fetch_at,last_http_status,etag,last_modified,current_content_hash,current_version_number,current_pdf_storage_id,current_pdf_filename,page_title,page_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical_url) DO UPDATE SET last_checked_at=excluded.last_checked_at,last_successful_fetch_at=excluded.last_successful_fetch_at,last_http_status=excluded.last_http_status,etag=excluded.etag,last_modified=excluded.last_modified,current_content_hash=excluded.current_content_hash,current_version_number=excluded.current_version_number,current_pdf_storage_id=excluded.current_pdf_storage_id,current_pdf_filename=excluded.current_pdf_filename,page_title=excluded.page_title,page_status='active',updated_at=excluded.updated_at", (page.url, old["first_seen_at"] if old else t, t, fetched, page.status, page.etag, page.last_modified, page.content_hash, version, storage_id, filename, page.title, "active", t))
            self.db.execute("INSERT INTO versions(canonical_url,version_number,content_hash,title,source_url,discovered_at,fetched_at,uploaded_at,http_status,pdf_filename,gloo_storage_id,change_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (page.url, version, page.content_hash, page.title, page.url, t, fetched, t, page.status, filename, storage_id, change))
            self.db.commit()

    def mark_unavailable(self, url: str, status: int):
        with self._lock:
            self.db.execute("UPDATE pages SET last_checked_at=?,last_http_status=?,page_status='unavailable',updated_at=? WHERE canonical_url=?", (now(), status, now(), url)); self.db.commit()


class GlooStorageClient:
    upload_url = "https://platform.ai.gloo.com/ingestion/v2/files"
    patch_url = "https://platform.ai.gloo.com/engine/v2/item"
    def __init__(self, api_key: str | None = None, publisher_id: str | None = None, session=None):
        self.api_key = (api_key or os.getenv("GLOO_API_KEY") or os.getenv("API_KEY") or "").strip()
        self.publisher_id = (publisher_id or os.getenv("PUBLISHER_ID") or "").strip()
        self.session = session or requests.Session()
        if not self.api_key or not self.publisher_id:
            raise RuntimeError("GLOO_API_KEY/API_KEY and PUBLISHER_ID are required for uploads")
    def upload(self, path: Path, producer_id: str) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(4):
            with path.open("rb") as fh:
                response = self.session.post(self.upload_url, params={"producer_id": producer_id}, data={"publisher_id": self.publisher_id}, files={"files": fh}, headers=headers, timeout=120)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3: time.sleep(2 ** attempt); continue
            response.raise_for_status()
            try: return response.json()
            except ValueError: return {}
        raise RuntimeError("Gloo upload failed after retries")
    def metadata(self, producer_id: str, page: Page):
        r = self.session.patch(self.patch_url, headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json={"publisher_id": self.publisher_id, "producer_id": producer_id, "item_title": page.title, "item_url": page.url}, timeout=60)
        if not r.ok: log.warning("Gloo metadata update failed for %s: HTTP %s", page.url, r.status_code)


def storage_id(result: dict) -> str | None:
    """Extract a storage/item identifier from common response envelopes.

    The exact Gloo response schema must be confirmed against the deployed tenant.
    Returning None is explicit: it must not be presented as a confirmed Gloo ID.
    """
    wanted = ("id", "file_id", "item_id", "storage_id")
    def walk(value):
        if isinstance(value, dict):
            for key in wanted:
                candidate = value.get(key)
                if candidate is not None and not isinstance(candidate, (dict, list)):
                    return str(candidate)
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None
    return walk(result)


def safe_name(url: str, version: int) -> str:
    path = urlparse(url).path.strip("/") or "home"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", path.replace("/", "__")).strip("-").lower() or "page"
    return f"2414world_{slug}_v{version:03d}.pdf"


def make_pdf(page: Page, path: Path, version: int):
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Left", parent=styles["BodyText"], alignment=TA_LEFT, fontSize=10.5, leading=15, spaceAfter=8))
    styles.add(ParagraphStyle("PageTitleLeft", parent=styles["Title"], alignment=TA_LEFT, fontSize=22, leading=27, spaceAfter=10))
    for i, size in ((1, 20), (2, 16), (3, 14), (4, 12), (5, 11), (6, 10)):
        styles.add(ParagraphStyle(f"H{i}", parent=styles[f"Heading{i}"], alignment=TA_LEFT, fontSize=size, leading=size*1.25, spaceBefore=12, spaceAfter=6, keepWithNext=True))
    story = [Paragraph(escape(page.title), styles["PageTitleLeft"]), Paragraph(escape(f"Source: {page.url} | Version: {version} | Fetched: {now()}"), styles["Left"]), Spacer(1, 8)]
    i = 0
    while i < len(page.blocks):
        b = page.blocks[i]
        if b.kind == "heading": story.append(Paragraph(escape(b.text), styles[f"H{min(b.level,6)}"]))
        elif b.kind == "list":
            items=[]; ordered=b.ordered
            while i < len(page.blocks) and page.blocks[i].kind == "list" and page.blocks[i].ordered == ordered:
                items.append(ListItem(Paragraph(escape(page.blocks[i].text), styles["Left"]))); i += 1
            story.append(ListFlowable(items, bulletType="1" if ordered else "bullet", leftIndent=18)); continue
        else: story.append(Paragraph(escape(b.text), styles["Left"]))
        i += 1
    path.parent.mkdir(parents=True, exist_ok=True); SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=18*mm, bottomMargin=18*mm, title=page.title).build(story)


class Fetcher:
    def __init__(self, base: str, timeout: int, retries: int, delay: float, session=None):
        self.base, self.timeout, self.retries, self.delay, self.session = base, timeout, retries, delay, session or requests.Session()
    def fetch(self, url: str, previous=None):
        headers = {"User-Agent": "2414world-crawler/2.0"}
        if previous:
            if previous["etag"]: headers["If-None-Match"] = previous["etag"]
            if previous["last_modified"]: headers["If-Modified-Since"] = previous["last_modified"]
        for attempt in range(self.retries + 1):
            try:
                time.sleep(self.delay)
                r = self.session.get(url, headers=headers, timeout=self.timeout)
                return r
            except requests.RequestException:
                if attempt >= self.retries: raise
                time.sleep(2 ** attempt)


def discover(base: str, fetcher: Fetcher, use_sitemap: bool, exclusions: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for path in LISTING_PATHS:
        try:
            r = fetcher.fetch(urljoin(base, path))
            if r.ok:
                soup = BeautifulSoup(r.text, "lxml")
                found.update(normalize_url(a.get("href"), base) for a in soup.find_all("a", href=True))
        except requests.RequestException as exc: log.warning("Listing failed %s: %s", path, exc)
    if use_sitemap:
        queue=[urljoin(base, "/sitemap.xml")]; visited=set()
        while queue:
            sitemap=queue.pop()
            if sitemap in visited: continue
            visited.add(sitemap)
            try:
                r=fetcher.fetch(sitemap)
                if not r.ok: continue
                root=ET.fromstring(r.content)
                for loc in root.iter():
                    if loc.tag.lower().endswith("loc") and loc.text:
                        value=loc.text.strip()
                        if root.tag.lower().endswith("sitemapindex"): queue.append(value)
                        else: found.add(normalize_url(value, base))
            except (requests.RequestException, ET.ParseError) as exc: log.warning("Sitemap failed %s: %s", sitemap, exc)
    return {u for u in found if is_eligible(u, base, exclusions)}


class CrawlJob:
    def __init__(self, args, store: StateStore, storage=None):
        self.args, self.store, self.fetcher = args, store, Fetcher(args.base_url, args.timeout, args.retries, args.delay)
        self.storage = storage
    def run(self) -> dict:
        # Dry-run is read-only: it does not create run rows, update page state,
        # write PDFs, or call Gloo.
        run_id = None if self.args.dry_run else self.store.start_run(self.args.mode)
        started = time.monotonic()
        stats = {"discovered": 0, "new": 0, "rechecked": 0, "changed": 0,
                 "unchanged": 0, "generated": 0, "uploaded": 0,
                 "upload_failed": 0, "failed": 0, "unavailable": 0}
        tracked = {r["canonical_url"]: r for r in self.store.tracked()}
        if self.args.url:
            candidates = {normalize_url(self.args.url, self.args.base_url)}
        elif self.args.mode == "single":
            candidates = set()
        else:
            candidates = discover(self.args.base_url, self.fetcher,
                                  not self.args.no_sitemap, self.args.exclude)
        candidates = {u for u in candidates if is_eligible(u, self.args.base_url, self.args.exclude)}
        if self.args.mode == "daily":
            candidates |= set(tracked)
        stats["discovered"] = len(candidates)

        # Prioritize never-successfully-indexed pages, then the least recently
        # checked pages. This avoids repeatedly starving later alphabetical URLs.
        def priority(url):
            row = tracked.get(url)
            return ((row["last_checked_at"] or "") if row else "", url)
        ordered = sorted(candidates, key=priority)[:max(0, self.args.max_pages)]

        def one(url):
            previous = tracked.get(url)
            try:
                response = self.fetcher.fetch(url, previous)
                if not self.args.dry_run:
                    self.store.check(url, response.status_code,
                                     response.headers.get("ETag"),
                                     response.headers.get("Last-Modified"))
                if response.status_code == 304:
                    return "unchanged", bool(previous)
                if response.status_code in (404, 410):
                    if not self.args.dry_run:
                        self.store.mark_unavailable(url, response.status_code)
                    return "unavailable", bool(previous)
                response.raise_for_status()
                page = extract_page(response.text, url)
                if not page:
                    return "failed", bool(previous)
                page.etag = response.headers.get("ETag")
                page.last_modified = response.headers.get("Last-Modified")
                page.status = response.status_code
                has_version = bool(previous and previous["current_version_number"] > 0)
                unchanged = bool(has_version and previous["current_content_hash"] == page.content_hash)
                if unchanged and not self.args.force:
                    return "unchanged", bool(previous)
                version = (previous["current_version_number"] if previous else 0) + 1
                filename = safe_name(url, version)
                pdf = self.args.output_dir / filename
                if self.args.dry_run:
                    return ("changed" if has_version else "new"), bool(previous)

                make_pdf(page, pdf, version)
                producer = f"2414web-{hashlib.sha256(url.encode()).hexdigest()[:24]}-v{version:03d}"
                try:
                    result = self.storage.upload(pdf, producer)
                except Exception as exc:
                    if not self.args.dry_run:
                        self.store.record_upload_attempt(
                            url, version, "failed", error_message=str(exc)
                        )
                    log.exception("Gloo upload failed for %s", url)
                    return "upload_failed", bool(previous)
                sid = storage_id(result)
                self.store.record_upload_attempt(
                    url, version, "succeeded",
                    storage_id_value=sid,
                    response=result,
                )
                self.storage.metadata(producer, page)
                if sid is None:
                    log.warning("Gloo upload succeeded for %s but response had no recognized ID; producer_id=%s response=%s",
                                url, producer, result)
                self.store.commit_version(page, filename, sid,
                                          "updated" if has_version else "new", now())
                return ("changed" if has_version else "new"), bool(previous)
            except Exception as exc:
                log.exception("Failed page %s: %s", url, exc)
                return "failed", bool(previous)

        with ThreadPoolExecutor(max_workers=max(1, self.args.workers)) as pool:
            futures = [pool.submit(one, url) for url in ordered]
            for future in as_completed(futures):
                action, was_tracked = future.result()
                stats[action] = stats.get(action, 0) + 1
                if was_tracked:
                    stats["rechecked"] += 1
        stats["duration_seconds"] = round(time.monotonic() - started, 2)
        if run_id is not None:
            self.store.finish_run(run_id, stats)
        log.info("crawl summary: %s", stats)
        return stats


def parse_args(argv=None):
    p=argparse.ArgumentParser(description="2414world daily PDF crawler")
    p.add_argument("--mode",choices=("full","daily","single"),default="daily"); p.add_argument("--url"); p.add_argument("--base-url",default=BASE_URL); p.add_argument("--db",type=Path,default=DEFAULT_DB); p.add_argument("--output-dir",type=Path,default=DEFAULT_OUTPUT); p.add_argument("--max-pages",type=int,default=1000); p.add_argument("--workers",type=int,default=4); p.add_argument("--delay",type=float,default=0.6); p.add_argument("--timeout",type=int,default=30); p.add_argument("--retries",type=int,default=2); p.add_argument("--exclude",action="append",default=[]); p.add_argument("--no-sitemap",action="store_true"); p.add_argument("--force",action="store_true"); p.add_argument("--dry-run",action="store_true"); return p.parse_args(argv)


def main(argv=None):
    load_dotenv(Path(__file__).resolve().with_name(".env")); args=parse_args(argv); logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if args.mode == "single" and not args.url: raise SystemExit("--url is required with --mode single")
    if args.max_pages < 1: raise SystemExit("--max-pages must be at least 1")
    if args.workers < 1: raise SystemExit("--workers must be at least 1")
    if args.dry_run:
        storage = None
    else:
        storage = GlooStorageClient()
    # The database parent is created by StateStore. Keep the lock beside it
    # so scheduled runs cannot overlap on the same persistent volume.
    with crawl_lock(args.db.with_suffix(args.db.suffix + ".lock")):
        store = StateStore(args.db)
        try:
            return CrawlJob(args, store, storage).run()
        finally:
            store.close()


if __name__ == "__main__": sys.exit(0 if main() else 1)
