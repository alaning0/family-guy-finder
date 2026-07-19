#!/usr/bin/env python3
"""Build docs/episodes.json for the Family Guy Episode Finder.

Merges two public sources, keyed on (season, episode):
  - JustWatch GraphQL  -> per-episode Disney+ playback UUIDs for the chosen country
  - Wikipedia          -> per-episode plot text (full Plot section when the episode
                          has its own article, else the season page's short summary)

Stdlib only. Responses are cached under build/cache/ so re-runs are cheap.

Usage:
  python3 build/build.py [--country nz] [--refresh] [--max-season N]
"""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "cache"
OUT = ROOT / "docs" / "episodes.json"

JW_ENDPOINT = "https://apis.justwatch.com/graphql"
WIKI_API = "https://en.wikipedia.org/w/api.php"

UA_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
UA_WIKI = "FamilyGuyFinder/1.0 (personal fan project; github.com/alaning0/family-guy-finder)"

SHOW_PATH = "/{country}/tv-show/family-guy"
SERIES_URL = "https://www.disneyplus.com/browse/entity-3c3c0f8b-7366-4d15-88ab-18050285978e"

SEASONS_QUERY = """
query GetShow($fullPath: String!, $country: Country!, $language: Language!) {
  urlV2(fullPath: $fullPath) {
    node {
      id
      ... on Show {
        content(country: $country, language: $language) { title }
        totalSeasonCount
        seasons { content(country: $country, language: $language) { seasonNumber fullPath } }
      }
    }
  }
}
"""

EPISODES_QUERY = """
query GetSeason($fullPath: String!, $country: Country!, $language: Language!) {
  urlV2(fullPath: $fullPath) {
    node {
      id
      ... on Season {
        totalEpisodeCount
        episodes {
          content(country: $country, language: $language) {
            title episodeNumber seasonNumber shortDescription
          }
          offers(country: $country, platform: WEB) {
            package { technicalName }
            deeplinkURL(platform: WEB)
          }
        }
      }
    }
  }
}
"""


def http_json(url, payload=None, ua=UA_BROWSER, retries=3):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": ua}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if attempt == retries - 1:
                raise
            if e.code == 429:  # honor Retry-After, generously
                wait = max(int(e.headers.get("Retry-After") or 0), 30)
            else:
                wait = 2 * (attempt + 1)
            print(f"    retry in {wait}s after {e}", file=sys.stderr)
            time.sleep(min(wait, 120))
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            wait = 2 * (attempt + 1)
            print(f"    retry in {wait}s after {e}", file=sys.stderr)
            time.sleep(wait)


def cached(key, fetch, refresh=False, delay=0.4):
    """Disk-cache a fetch under build/cache/<key>.json; sleep only on real fetches."""
    CACHE.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)
    path = CACHE / f"{safe}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    result = fetch()
    path.write_text(json.dumps(result, ensure_ascii=False))
    time.sleep(delay)
    return result


# ---------------------------------------------------------------- JustWatch

def jw_query(query, variables, cache_key, refresh):
    def fetch():
        return http_json(JW_ENDPOINT, {"query": query, "variables": variables})
    data = cached(cache_key, fetch, refresh)
    if data.get("errors"):
        raise RuntimeError(f"JustWatch error for {cache_key}: {data['errors']}")
    return data["data"]


def fetch_justwatch(country, refresh):
    """Return {(season, episode): {title, short, uuid}} plus season list info."""
    cc = country.upper()
    show_path = SHOW_PATH.format(country=country.lower())
    variables = {"fullPath": show_path, "country": cc, "language": "en"}
    show = jw_query(SEASONS_QUERY, variables, f"jw_show_{country}", refresh)
    node = show["urlV2"]["node"]
    seasons = node.get("seasons") or []
    print(f"JustWatch: {node['content']['title']} — {node.get('totalSeasonCount')} seasons ({cc})")

    episodes = {}
    for s in seasons:
        num = s["content"]["seasonNumber"]
        path = s["content"]["fullPath"] or f"{show_path}/season-{num}"
        try:
            season = jw_query(
                EPISODES_QUERY,
                {"fullPath": path, "country": cc, "language": "en"},
                f"jw_season_{country}_{num:02d}",
                refresh,
            )
            snode = season["urlV2"]["node"]
        except (RuntimeError, KeyError, TypeError) as e:
            print(f"  ! season {num}: no data ({e})")
            continue
        eps = snode.get("episodes") or []
        expected = snode.get("totalEpisodeCount")
        if expected is not None and expected != len(eps):
            print(f"  ! season {num}: got {len(eps)} episodes, API says {expected}")
        for ep in eps:
            c = ep["content"]
            uuid = None
            for offer in ep.get("offers") or []:
                if offer["package"]["technicalName"] == "disneyplus" and offer.get("deeplinkURL"):
                    m = re.search(r"/play/([0-9a-f-]{36})", offer["deeplinkURL"])
                    if m:
                        uuid = m.group(1)
                        break
            episodes[(c["seasonNumber"], c["episodeNumber"])] = {
                "title": c["title"],
                "short": (c.get("shortDescription") or "").strip(),
                "uuid": uuid,
            }
        print(f"  season {num}: {len(eps)} episodes, {sum(1 for e in eps if any(o['package']['technicalName']=='disneyplus' and o.get('deeplinkURL') for o in (e.get('offers') or [])))} with Disney+ links")
    return episodes


# ---------------------------------------------------------------- Wikipedia

def wiki_api(params, cache_key, refresh):
    def fetch():
        qs = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
        return http_json(f"{WIKI_API}?{qs}", ua=UA_WIKI)
    return cached(cache_key, fetch, refresh, delay=3.0)


def wiki_batch_pages(titles, cache_prefix, refresh):
    """Fetch wikitext for up to 50 titles per request (anon rate limits are strict).

    Returns {requested_title: (resolved_title, wikitext)}, following redirects.
    """
    result = {}
    titles = sorted(titles)
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        # key on the chunk's actual titles so a changed article list can't hit stale cache
        digest = hashlib.md5("|".join(chunk).encode()).hexdigest()[:10]
        data = wiki_api(
            {"action": "query", "prop": "revisions", "rvprop": "content",
             "rvslots": "main", "redirects": "1", "titles": "|".join(chunk)},
            f"{cache_prefix}_{digest}",
            refresh,
        )
        q = data.get("query", {})
        mapping = {t: t for t in chunk}
        for step in ("normalized", "redirects"):
            for entry in q.get(step, []):
                for req, cur in mapping.items():
                    if cur == entry["from"]:
                        mapping[req] = entry["to"]
        pages = {p["title"]: p for p in q.get("pages", [])}
        for req, final in mapping.items():
            p = pages.get(final)
            if p and not p.get("missing") and p.get("revisions"):
                result[req] = (final, p["revisions"][0]["slots"]["main"]["content"])
    return result


def split_top_level(text, sep="|"):
    """Split template body on `sep` at zero {{ }} / [[ ]] nesting depth."""
    parts, buf, i, brace, bracket = [], [], 0, 0, 0
    while i < len(text):
        two = text[i:i + 2]
        if two == "{{":
            brace += 1; buf.append(two); i += 2; continue
        if two == "}}":
            brace -= 1; buf.append(two); i += 2; continue
        if two == "[[":
            bracket += 1; buf.append(two); i += 2; continue
        if two == "]]":
            bracket -= 1; buf.append(two); i += 2; continue
        ch = text[i]
        if ch == sep and brace == 0 and bracket == 0:
            parts.append("".join(buf)); buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def extract_templates(wikitext, name_re):
    """Yield the inner body of every {{<name>...}} template, brace-balanced."""
    for m in re.finditer(name_re, wikitext, re.IGNORECASE):
        start = m.end()
        depth, i = 1, start
        while i < len(wikitext) and depth:
            two = wikitext[i:i + 2]
            if two == "{{":
                depth += 1; i += 2
            elif two == "}}":
                depth -= 1; i += 2
            else:
                i += 1
        yield wikitext[start:i - 2]


TAG_RE = re.compile(r"<!--.*?-->|<ref[^>/]*/>|<ref[^>]*>.*?</ref>|<[^>]+>", re.S)


def clean_wikitext(text):
    """Best-effort wikitext -> plain text for summaries."""
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"\[\[(?:File|Image):.*?\]\]", " ", text, flags=re.S)
    text = re.sub(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]*)\]\]", r"\1", text)  # [[a|b]] -> b
    for _ in range(4):  # drop innermost templates repeatedly
        text, n = re.subn(r"\{\{[^{}]*\}\}", " ", text)
        if not n:
            break
    text = text.replace("'''", "").replace("''", "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&ndash;", "–")
    return re.sub(r"\s+", " ", text).strip()


def parse_season_page(wikitext):
    """Return {episode_in_season: {title, article, summary, year}}."""
    rows = {}
    # matches {{Episode list, {{Episode list/sublist, and {{#invoke:Episode list|sublist
    for body in extract_templates(wikitext, r"\{\{\s*(?:#invoke:\s*)?Episode list\b"):
        fields = {}
        for part in split_top_level(body):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()
        num_raw = fields.get("EpisodeNumber2") or fields.get("EpisodeNumber") or ""
        m = re.search(r"\d+", num_raw)
        if not m:
            continue
        num = int(m.group())
        title_raw = fields.get("Title", "")
        link = re.search(r"\[\[([^\[\]|]+)(?:\|([^\[\]]+))?\]\]", title_raw)
        article = link.group(1).strip() if link else None
        title = clean_wikitext(title_raw).strip('"“” ')
        year = None
        ym = re.search(r"\{\{Start date\|(\d{4})", fields.get("OriginalAirDate", ""))
        if ym:
            year = int(ym.group(1))
        rows[num] = {
            "title": title,
            "article": article,
            "summary": clean_wikitext(fields.get("ShortSummary", "")),
            "year": year,
        }
    return rows


def plot_from_wikitext(wikitext):
    """The cleaned Plot section of an episode article's wikitext, or None."""
    m = re.search(r"\n==\s*Plot[^=\n]*==\s*\n(.*?)(?=\n==[^=]|\Z)", wikitext, re.S)
    if not m:
        return None
    body = re.sub(r"\n===+[^=\n]+===+\n", "\n", m.group(1))  # drop sub-headings
    plot = clean_wikitext(body)
    if len(plot) > 4500:  # keep the dataset lean; cut at a sentence boundary
        cut = plot.rfind(". ", 0, 4500)
        plot = plot[: cut + 1 if cut > 0 else 4500]
    return plot or None


def fetch_wikipedia(max_season, refresh):
    """Return {(season, episode): {title, summary, plot, year}}."""
    season_names = [f"Family Guy season {s}" for s in range(1, max_season + 1)]
    season_pages = wiki_batch_pages(season_names, "wiki_seasons", refresh)
    resolved_season_pages = {final for final, _ in season_pages.values()}

    out = {}
    for s in range(1, max_season + 1):
        got = season_pages.get(f"Family Guy season {s}")
        if not got:
            print(f"  ! Wikipedia: no season page for season {s}")
            continue
        rows = parse_season_page(got[1])
        print(f"  Wikipedia season {s}: {len(rows)} episodes in table")
        for e, row in rows.items():
            out[(s, e)] = {**row, "plot": None}

    articles = sorted({row["article"] for row in out.values() if row["article"]})
    print(f"Wikipedia: fetching {len(articles)} episode articles "
          f"in {(len(articles) + 49) // 50} batched requests...")
    article_pages = wiki_batch_pages(articles, "wiki_articles", refresh)
    for row in out.values():
        got = article_pages.get(row["article"]) if row["article"] else None
        # a redirect back to a season page means the episode has no real article
        if got and got[0] not in resolved_season_pages:
            row["plot"] = plot_from_wikitext(got[1])
    return out


# ------------------------------------------------------------------- merge

def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="nz")
    ap.add_argument("--refresh", action="store_true", help="ignore the disk cache")
    ap.add_argument("--max-season", type=int, default=None)
    args = ap.parse_args()

    jw = fetch_justwatch(args.country, args.refresh)
    max_season = args.max_season or max(s for s, _ in jw)
    wiki = fetch_wikipedia(max_season, args.refresh)

    keys = sorted(set(jw) | set(wiki))
    episodes, mismatches, no_link, no_plot = [], [], [], []
    for s, e in keys:
        j, w = jw.get((s, e)), wiki.get((s, e))
        title = (w or {}).get("title") or (j or {}).get("title") or f"Episode {e}"
        if j and w and norm_title(j["title"]) != norm_title(w["title"]):
            mismatches.append(f"S{s:02d}E{e:02d}: JW '{j['title']}' vs Wiki '{w['title']}'")
        short = (w or {}).get("summary") or (j or {}).get("short") or ""
        plot = (w or {}).get("plot") or ""
        uuid = (j or {}).get("uuid")
        if not uuid:
            no_link.append(f"S{s:02d}E{e:02d} {title}")
        if not plot:
            no_plot.append(f"S{s:02d}E{e:02d} {title}")
        episodes.append({
            "s": s, "e": e, "t": title,
            "y": (w or {}).get("year"),
            "short": short, "plot": plot, "u": uuid,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "built": time.strftime("%Y-%m-%d"),
        "country": args.country.upper(),
        "series": SERIES_URL,
        "count": len(episodes),
        "episodes": episodes,
    }, ensure_ascii=False, separators=(",", ":")))

    n = len(episodes)
    linked = n - len(no_link)
    plotted = n - len(no_plot)
    print("\n================ BUILD REPORT ================")
    print(f"episodes: {n}   deep links: {linked} ({linked/n:.0%})   plots: {plotted} ({plotted/n:.0%})")
    print(f"output: {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
    if mismatches:
        print(f"\ntitle mismatches ({len(mismatches)}):")
        print("\n".join(f"  {m}" for m in mismatches))
    if no_link:
        print(f"\nno Disney+ link ({len(no_link)}):")
        print("\n".join(f"  {m}" for m in no_link))
    if no_plot:
        print(f"\nno full plot, using summary ({len(no_plot)}):")
        print("\n".join(f"  {m}" for m in no_plot))


if __name__ == "__main__":
    main()
