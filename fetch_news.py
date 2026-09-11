#!/usr/bin/env python3
"""
newsdash (GitHub Pages edition) - data fetcher.

Runs inside GitHub Actions, never on your PC. Standard library only, so the
workflow needs no pip install. It reads feeds.json, polls RSS/Atom feeds,
optional Benzinga news and Yahoo quotes, merges the result with the previous
run's data (so history survives between runs) and writes one news.json that
the web page polls.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib

UTC = dt.timezone.utc
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 newsdash-pages/3")
TIMEOUT = 20
RUN_STARTED = dt.datetime.now(UTC)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url, headers=None, timeout=TIMEOUT):
    """GET with gzip support. Returns (status, body_bytes, headers). 304 is not an error."""
    h = {
        "User-Agent": UA,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                body = gzip.decompress(body)
            elif enc == "deflate":
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            return r.status, body, r.headers
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", e.headers
        raise


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s, limit=None):
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(html.unescape(s))
    s = TAG_RE.sub(" ", s)
    s = WS_RE.sub(" ", s).strip()
    if limit and len(s) > limit:
        cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:.-")
        s = cut + "\u2026"
    return s


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    d = None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        d = parse_iso(s)
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


TRACKING_PARAMS = re.compile(r"^(utm_|mc_|ns_|at_|cmpid$|ocid$|traffic_source$|src$)", re.I)


def normalize_link(link):
    try:
        p = urllib.parse.urlsplit(link.strip())
    except ValueError:
        return link
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not TRACKING_PARAMS.match(k)]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def make_id(*parts):
    key = next((p for p in parts if p), "")
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# RSS / Atom / RDF parsing
# --------------------------------------------------------------------------- #
BAD_AMP = re.compile(rb"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9A-Fa-f]+);)")
CTRL = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def parse_xml(body):
    body = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        # common real-world breakage: HTML entities (&nbsp;) or bare '&', control chars
        return ET.fromstring(CTRL.sub(b"", BAD_AMP.sub(b"&amp;", body)))


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_entries(root):
    entries = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = link = desc = content = guid = None
        published = updated = None
        cats = []
        for c in el:
            n = local(c.tag)
            text = (c.text or "").strip()
            if n == "title":
                title = "".join(c.itertext()).strip()
            elif n == "link":
                href = c.get("href")
                if href:
                    if c.get("rel", "alternate") == "alternate" or not link:
                        link = href
                elif text and not link:
                    link = text
            elif n in ("description", "summary"):
                desc = desc or "".join(c.itertext())
            elif n in ("encoded", "content"):
                content = content or "".join(c.itertext())
            elif n in ("pubDate", "published", "date", "issued"):
                published = published or text
            elif n in ("updated", "modified"):
                updated = updated or text
            elif n in ("guid", "id"):
                guid = guid or text
            elif n in ("category", "subject"):
                v = c.get("term") or text
                if v:
                    cats.append(v)
        if not title and not link:
            continue
        entries.append({
            "title": title or "",
            "link": link or (guid if guid and guid.startswith("http") else ""),
            "guid": guid or "",
            "summary": desc or content or "",
            "date": parse_date(published) or parse_date(updated),
            "categories": cats,
        })
    return entries


# --------------------------------------------------------------------------- #
# source fetchers
# --------------------------------------------------------------------------- #
def fetch_rss(src, prev_meta, prev_count, settings):
    headers = {}
    # Only ask "changed since last time?" if we still hold that source's items.
    if prev_count > 0:
        if prev_meta.get("etag"):
            headers["If-None-Match"] = prev_meta["etag"]
        if prev_meta.get("last_modified"):
            headers["If-Modified-Since"] = prev_meta["last_modified"]

    status, body, hdrs = http_get(src["url"], headers)
    meta = {
        "etag": (hdrs.get("ETag") if hdrs else None) or prev_meta.get("etag"),
        "last_modified": (hdrs.get("Last-Modified") if hdrs else None) or prev_meta.get("last_modified"),
    }
    if status == 304:
        return {"status": "not_modified", "items": [], **meta}

    entries = parse_entries(parse_xml(body))
    if not entries:
        raise ValueError("feed loaded but contained no headlines")

    limit = int(src.get("max_items", settings["max_items_per_source"]))
    items = []
    for e in entries:
        link = normalize_link(e["link"]) if e["link"] else ""
        title = clean_text(e["title"])
        if not title:
            continue
        items.append({
            "id": make_id(link, e["guid"], src["id"] + title),
            "title": title,
            "link": link,
            "summary": clean_text(e["summary"], settings["summary_chars"]),
            "published": iso(e["date"]) if e["date"] else None,
            "source": src["id"],
            "source_name": src["name"],
            "category": src["category"],
            "tickers": [],
            "tags": [],
        })
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    return {"status": "ok", "items": items[:limit], **meta}


def fetch_benzinga(cfg, key, settings):
    params = {
        "token": key,
        "pageSize": int(cfg.get("page_size", 60)),
        "displayOutput": "abstract",
        "updatedSince": int(time.time()) - int(cfg.get("lookback_hours", 6)) * 3600,
    }
    if cfg.get("channels"):
        params["channels"] = ",".join(cfg["channels"])
    if cfg.get("tickers"):
        params["tickers"] = ",".join(cfg["tickers"])
    url = "https://api.benzinga.com/api/v2/news?" + urllib.parse.urlencode(params)

    try:
        _, body, _ = http_get(url, {"Accept": "application/json"})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(f"Benzinga rejected the API key (HTTP {e.code})") from None
        raise RuntimeError(f"Benzinga returned HTTP {e.code}") from None

    data = json.loads(body.decode("utf-8", "replace"))
    if isinstance(data, dict):
        data = data.get("data") or data.get("news") or []
    include_summary = bool(cfg.get("include_summaries", False))
    items = []
    for a in data:
        title = clean_text(a.get("title"))
        if not title:
            continue
        created = parse_date(a.get("created") or a.get("updated") or "")
        items.append({
            "id": "bz-" + str(a.get("id") or make_id(a.get("url"), title)),
            "title": title,
            "link": a.get("url") or "",
            "summary": clean_text(a.get("teaser") or a.get("body"), settings["summary_chars"]) if include_summary else "",
            "published": iso(created) if created else None,
            "source": "benzinga",
            "source_name": cfg.get("name", "Benzinga"),
            "category": cfg.get("category", "wire"),
            "tickers": [s.get("name") for s in (a.get("stocks") or []) if isinstance(s, dict) and s.get("name")][:6],
            "tags": [c.get("name") for c in (a.get("channels") or []) if isinstance(c, dict) and c.get("name")][:4],
        })
    return {"status": "ok", "items": items}


def fetch_quote(q):
    sym = urllib.parse.quote(q["symbol"], safe="")
    last_err = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=1d"
        try:
            _, body, _ = http_get(url, {"Accept": "application/json"})
            meta = json.loads(body)["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None:
                raise ValueError("no price in response")
            change = (price - prev) if prev else None
            ts = meta.get("regularMarketTime")
            return {
                "symbol": q["symbol"],
                "label": q.get("label", q["symbol"]),
                "decimals": q.get("decimals", 2),
                "price": price,
                "change": change,
                "change_pct": (change / prev * 100) if prev else None,
                "currency": meta.get("currency"),
                "market_time": iso(dt.datetime.fromtimestamp(ts, UTC)) if ts else None,
                "stale": False,
            }
        except Exception as e:  # noqa: BLE001 - try the other host
            last_err = e
    raise RuntimeError(describe_error(last_err))


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"network error: {e.reason}"
    if isinstance(e, ET.ParseError):
        return "response was not valid RSS/XML"
    if isinstance(e, TimeoutError):
        return "timed out"
    return str(e) or e.__class__.__name__


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def load_json(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        log(f"warning: could not read {path}, starting fresh")
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="feeds.json")
    ap.add_argument("--prev", default="prev.json")
    ap.add_argument("--out", default="out/news.json")
    args = ap.parse_args()

    cfg = load_json(args.config)
    if not cfg.get("sources"):
        log("error: config has no sources")
        return 1
    settings = {"max_age_hours": 48, "max_items_total": 800, "max_items_per_source": 40, "summary_chars": 280}
    settings.update(cfg.get("settings") or {})

    prev = load_json(args.prev)
    prev_items = {i["id"]: i for i in prev.get("items", []) if isinstance(i, dict) and i.get("id")}
    prev_sources = {s["id"]: s for s in prev.get("sources", []) if isinstance(s, dict) and s.get("id")}
    prev_quotes = {q["symbol"]: q for q in prev.get("quotes", []) if isinstance(q, dict) and q.get("symbol")}
    prev_counts = {}
    for i in prev_items.values():
        prev_counts[i.get("source")] = prev_counts.get(i.get("source"), 0) + 1

    sources = [s for s in cfg["sources"] if s.get("enabled", True)]
    bz_cfg = cfg.get("benzinga") or {}
    bz_key = os.environ.get("BENZINGA_API_KEY", "").strip()
    bz_active = bool(bz_cfg.get("enabled", True) and bz_key)

    source_status = []
    fresh_items = []

    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {}
        for s in sources:
            futures[pool.submit(fetch_rss, s, prev_sources.get(s["id"], {}), prev_counts.get(s["id"], 0), settings)] = ("rss", s)
        if bz_active:
            futures[pool.submit(fetch_benzinga, bz_cfg, bz_key, settings)] = ("benzinga", None)
        quote_futures = {pool.submit(fetch_quote, q): q for q in cfg.get("quotes", [])}

        for fut in cf.as_completed(futures):
            kind, s = futures[fut]
            sid = s["id"] if s else "benzinga"
            base = {
                "id": sid,
                "name": s["name"] if s else bz_cfg.get("name", "Benzinga"),
                "category": s["category"] if s else bz_cfg.get("category", "wire"),
                "kind": kind,
            }
            prev_meta = prev_sources.get(sid, {})
            try:
                res = fut.result()
                fresh_items.extend(res["items"])
                source_status.append({
                    **base, "ok": True, "status": res["status"], "error": None,
                    "fetched": len(res["items"]), "last_ok": iso(RUN_STARTED),
                    "etag": res.get("etag"), "last_modified": res.get("last_modified"),
                })
            except Exception as e:  # noqa: BLE001 - one broken feed must not stop the run
                msg = describe_error(e)
                if bz_key:
                    msg = msg.replace(bz_key, "***")
                log(f"{sid}: {msg}")
                source_status.append({
                    **base, "ok": False, "status": "error", "error": msg, "fetched": 0,
                    "last_ok": prev_meta.get("last_ok"),
                    "etag": prev_meta.get("etag"), "last_modified": prev_meta.get("last_modified"),
                })

        if not bz_active:
            source_status.append({
                "id": "benzinga", "name": bz_cfg.get("name", "Benzinga"), "category": bz_cfg.get("category", "wire"),
                "kind": "benzinga", "ok": True, "status": "disabled",
                "error": None, "fetched": 0, "last_ok": None,
            })

        quotes = []
        for fut, q in quote_futures.items():
            try:
                quotes.append(fut.result())
            except Exception as e:  # noqa: BLE001
                log(f"quote {q['symbol']}: {e}")
                old = prev_quotes.get(q["symbol"])
                if old:
                    quotes.append({**old, "label": q.get("label", old.get("label")), "stale": True})

    # ---- merge with previous run --------------------------------------------
    now_iso = iso(RUN_STARTED)
    merged = dict(prev_items)
    for it in fresh_items:
        old = merged.get(it["id"])
        it["first_seen"] = old.get("first_seen", now_iso) if old else now_iso
        if not it["published"]:
            it["published"] = old.get("published") if old else None
        merged[it["id"]] = it

    active_ids = {s["id"] for s in sources} | ({"benzinga"} if bz_active else set())
    cutoff = RUN_STARTED - dt.timedelta(hours=float(settings["max_age_hours"]))
    future_limit = RUN_STARTED + dt.timedelta(minutes=10)

    items = []
    for it in merged.values():
        if it.get("source") not in active_ids:
            continue
        pub = parse_iso(it.get("published") or "") or parse_iso(it.get("first_seen") or "") or RUN_STARTED
        if pub > future_limit:  # feeds with broken time zones
            pub = parse_iso(it.get("first_seen") or "") or RUN_STARTED
        it["published"] = iso(pub)
        if pub < cutoff:
            continue
        items.append(it)
    items.sort(key=lambda i: (i["published"], i.get("first_seen", "")), reverse=True)
    items = items[: int(settings["max_items_total"])]

    order = {s["id"]: n for n, s in enumerate(sources)}
    order["benzinga"] = len(order)
    source_status.sort(key=lambda s: order.get(s["id"], 999))
    quote_order = {q["symbol"]: n for n, q in enumerate(cfg.get("quotes", []))}
    quotes.sort(key=lambda q: quote_order.get(q["symbol"], 999))

    out = {
        "version": 3,
        "generated_at": now_iso,
        "categories": cfg.get("categories", []),
        "sources": source_status,
        "quotes": quotes,
        "items": items,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    ok = sum(1 for s in source_status if s["ok"] and s["status"] != "disabled")
    total = sum(1 for s in source_status if s["status"] != "disabled")
    log(f"done: {len(items)} items, {ok}/{total} sources ok, {len(quotes)} quotes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
