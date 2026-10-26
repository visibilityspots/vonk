"""Public ICS feed: one calendar per tak, published to a GitHub Pages repository.

Every ``<tak>.ics`` holds the events of that tak plus the group-wide ones (jaarvonk), so a
parent subscribes with a single URL; ``groep.ics`` holds the group-wide events alone. The
feed is rendered from the desired events in SQLite, i.e. what the latest vonk says, without
the approve step the family calendar has. Titles name the tak, never a child.

UIDs are derived from scope, date and start time, so a re-extracted vonk updates the event
on every subscribed phone instead of duplicating it. Their domain differs from the CalDAV
one, so ``models.is_own_uid`` never mistakes a feed event for an own event.

History: every monthly vonk is its own scope and no scope is ever dropped from the store,
so a new vonk only adds events. The feed shows the current scout year (September to
August) and anything later; from September the previous year leaves the feed (the store
keeps it).

Publishing goes through the GitHub contents API and only writes files whose content
changed: ``DTSTAMP`` is the time the scope was stored, so an unchanged vonk renders to the
same bytes and does not produce a commit.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from string import Template
from urllib.parse import quote

import httpx
from icalendar import Calendar

from vonk.models import Event, Tak
from vonk.normalize import event_bounds
from vonk.sinks.caldav_sink import PRODID, build_vevent

log = logging.getLogger("vonk.feed")

FEED_UID_DOMAIN = "@feed.vonk.visibilityspots"
GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 30.0
REFRESH = "PT6H"
"""Suggested refresh interval: the poll runs every 6 hours."""

TAK_NAMES: Mapping[Tak, str] = {
    Tak.KAPOENEN: "Kapoenen",
    Tak.KABOUTERS: "Kabouters",
    Tak.WELPEN: "Welpen",
    Tak.JOGIS: "Jogi's",
    Tak.JEEVEES: "Jeevees",
    Tak.GIDSEN: "Gidsen",
    Tak.VERKENNERS: "Verkenners",
    Tak.JINS: "Jins",
    Tak.GROEP: "Groep",
}

StoredScope = tuple[str, Sequence[Event], float]
"""``(scope_key, events, updated_at)`` as returned by ``Store.desired_all``."""


def scope_tak(scope_key: str) -> Tak:
    return Tak(scope_key.partition(":")[0])


SCOUT_YEAR_START_MONTH = 9


def scout_year_of(day: date) -> int:
    """First calendar year of the scout year (September to August) ``day`` falls in."""
    return day.year if day.month >= SCOUT_YEAR_START_MONTH else day.year - 1


def scope_scout_year(scope_key: str) -> int:
    """Scout year of a scope: ``2026-10`` -> 2026, ``2027-03`` -> 2026, ``2026-2027`` -> 2026."""
    period = scope_key.partition(":")[2]
    first, _, second = period.partition("-")
    if len(second) == 4:  # jaarvonk: YYYY-YYYY
        return int(first)
    return scout_year_of(date(int(first), int(second), 1))


def feed_title(ev: Event) -> str:
    """``Scouts <Tak> - <activiteit>``; group-wide events ``Scouts - <activiteit>``."""
    tak = scope_tak(ev.scope_key)
    prefix = "Scouts" if tak is Tak.GROEP else f"Scouts {TAK_NAMES[tak]}"
    return f"{prefix} - {ev.title_activity}"


def _sort_key(ev: Event) -> tuple:
    return (ev.date, ev.start or time.min, ev.scope_key, ev.title_activity)


def feed_uid(ev: Event, n: int = 0) -> str:
    """Stable UID from scope, date and start; ``n`` separates events sharing those."""
    start = ev.start.isoformat() if ev.start else "all-day"
    digest = hashlib.sha256(f"{ev.scope_key}|{ev.date}|{start}|{n}".encode()).hexdigest()
    return f"vonk-{digest[:24]}{FEED_UID_DOMAIN}"


def render_calendar(name: str, items: Iterable[tuple[Event, float]]) -> str:
    """One VCALENDAR with every ``(event, stored_at)`` in ``items``, sorted by date."""
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", "Europe/Brussels")
    cal.add("x-published-ttl", REFRESH)
    seen: dict[str, int] = {}
    timed = False
    for ev, stored_at in sorted(items, key=lambda item: _sort_key(item[0])):
        base = feed_uid(ev)
        n = seen.get(base, 0)
        seen[base] = n + 1
        stamp = datetime.fromtimestamp(int(stored_at), UTC)
        cal.add_component(build_vevent(feed_uid(ev, n), ev, feed_title(ev), dtstamp=stamp))
        timed = timed or event_bounds(ev) is not None
    if timed:
        cal.add_missing_timezones()
    return cal.to_ical().decode("utf-8")


def pages_url(repo: str) -> str:
    owner, _, name = repo.partition("/")
    return f"https://{owner.lower()}.github.io/{name}/"


def google_url(webcal: str) -> str:
    """Google Calendar's "add by URL" link; Android has no handler for webcal:// itself."""
    return "https://calendar.google.com/calendar/render?cid=" + quote(webcal, safe="")


_LILY_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<path d="M50 4C63 18 66 37 57 56H43C34 37 37 18 50 4Z"/>'
    '<path d="M41 56C27 60 12 53 9 40 7 29 17 22 26 27 19 32 22 43 32 45 37 46 41 50 41 56Z"/>'
    '<path d="M59 56C73 60 88 53 91 40 93 29 83 22 74 27 81 32 78 43 68 45 63 46 59 50 59 56Z"/>'
    '<rect x="28" y="56" width="44" height="7" rx="3"/>'
    '<path d="M45 63 50 96 55 63Z"/>'
    '<path d="M41 63C40 75 34 84 25 86 31 79 35 71 36 63Z"/>'
    '<path d="M59 63C60 75 66 84 75 86 69 79 65 71 64 63Z"/>'
    "</svg>"
)
LILY = "url('data:image/svg+xml," + quote(_LILY_SVG) + "')"
"""Fleur-de-lis watermark, drawn for this page (not the WOSM emblem)."""

INDEX_TEMPLATE = Template("""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scouts Tervant agenda</title>
<style>
:root { --accent: #2e4e4e; --line: #bdd732; color-scheme: light dark; }
@media (prefers-color-scheme: dark) { :root { --accent: #bdd732; } }
body { max-width: 40rem; margin: 0 auto; padding: 2rem 1rem; line-height: 1.6;
  font-family: Raleway, system-ui, sans-serif; }
h1 { color: var(--accent); border-bottom: 3px solid var(--line); padding-bottom: .3rem; }
a { color: var(--accent); }
ul.feeds { list-style: none; padding: 0; }
ul.feeds li { padding: .6rem 0; border-bottom: 1px solid #8884; }
code { font-size: .8rem; opacity: .7; overflow-wrap: anywhere; }
small { opacity: .7; }
body::before { content: ""; position: fixed; inset: 0; z-index: -1; opacity: .06;
  background: var(--accent); --pos: right max(1rem, 50vw - 24rem) bottom 4vh;
  -webkit-mask: $lily var(--pos) / min(60vw, 22rem) no-repeat;
  mask: $lily var(--pos) / min(60vw, 22rem) no-repeat; }
</style>
</head>
<body>
<h1>Scouts Tervant agenda</h1>
<p>Activiteiten uit de maandelijkse vonk, automatisch bijgewerkt. Elke tak-agenda bevat ook
de groepsactiviteiten.</p>
<ul>
<li><b>iPhone of Mac</b>: tik op "iPhone/Mac" en bevestig het abonnement.</li>
<li><b>Android / Google Agenda</b>: tik op "Google Agenda" en kies "Toevoegen". Lukt dat
niet op je gsm, open de link dan op een computer: de Android-app kan zelf geen agenda via
een link toevoegen, maar toont hem daarna wel.</li>
<li><b>Andere agenda</b>: voeg de link als agenda "via URL" toe.</li>
</ul>
<ul class="feeds">
$items
</ul>
<p><small>Geen officiële dienst van de scouts: bij twijfel geldt de vonk zelf.</small></p>
</body>
</html>
""")


def render_index(repo: str, takken: Sequence[Tak]) -> str:
    """A plain page listing the subscribe links: webcal:// for Apple devices, a Google
    Calendar link for Android (webcal:// does nothing there), and the plain URL. Accent
    colours follow scoutstervant.be; no external fonts or scripts."""
    base = pages_url(repo)
    rows = []
    for tak in [*takken, Tak.GROEP]:
        url = f"{base}{tak.value}.ics"
        webcal = "webcal://" + url.removeprefix("https://")
        label = "Hele groep (alleen groepsactiviteiten)" if tak is Tak.GROEP else TAK_NAMES[tak]
        rows.append(
            f"<li><b>{html.escape(label)}</b><br>"
            f'<a href="{html.escape(webcal)}">iPhone/Mac</a>'
            f' · <a href="{html.escape(google_url(webcal))}">Google Agenda</a><br>'
            f"<code>{html.escape(url)}</code></li>"
        )
    return INDEX_TEMPLATE.substitute(items="\n".join(rows), lily=LILY)


def build_feed(
    scopes: Sequence[StoredScope], takken: Sequence[Tak], repo: str, today: date
) -> dict[str, str]:
    """Every feed file by path: ``<tak>.ics`` per tak, ``groep.ics`` and ``index.html``.

    Only scopes of the scout year ``today`` is in, or a later one (a September vonk that
    appears in August), are published.
    """
    current = scout_year_of(today)
    by_tak: dict[Tak, list[tuple[Event, float]]] = {}
    for scope_key, events, stored_at in scopes:
        if scope_scout_year(scope_key) < current:
            continue
        by_tak.setdefault(scope_tak(scope_key), []).extend((ev, stored_at) for ev in events)
    group = by_tak.get(Tak.GROEP, [])
    files = {
        f"{tak.value}.ics": render_calendar(
            f"Scouts Tervant - {TAK_NAMES[tak]}", [*by_tak.get(tak, []), *group]
        )
        for tak in takken
    }
    files["groep.ics"] = render_calendar("Scouts Tervant - Groep", group)
    files["index.html"] = render_index(repo, takken)
    return files


class GitHubFeedPublisher:
    """Writes feed files to ``repo`` on ``branch`` via the contents API, changed ones only."""

    def __init__(self, client: httpx.Client, repo: str, token: str, branch: str) -> None:
        self.client = client
        self.repo = repo
        self.branch = branch
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str) -> str:
        return f"{GITHUB_API}/repos/{self.repo}/contents/{path}"

    def _current(self, path: str) -> tuple[str | None, bytes | None]:
        resp = self.client.get(
            self._url(path), params={"ref": self.branch}, headers=self.headers,
            timeout=HTTP_TIMEOUT,
        )  # fmt: skip
        if resp.status_code == 404:
            return None, None
        resp.raise_for_status()
        body = resp.json()
        return body["sha"], base64.b64decode(body.get("content") or "")

    def publish(self, files: Mapping[str, str]) -> list[str]:
        """Create or update every file whose content differs; returns the changed paths."""
        changed = []
        for path in sorted(files):
            data = files[path].encode("utf-8")
            sha, current = self._current(path)
            if current == data:
                continue
            body = {
                "message": f"feed: update {path}",
                "content": base64.b64encode(data).decode("ascii"),
                "branch": self.branch,
            }
            if sha:
                body["sha"] = sha
            resp = self.client.put(
                self._url(path), json=body, headers=self.headers, timeout=HTTP_TIMEOUT
            )
            resp.raise_for_status()
            changed.append(path)
        return changed
