"""WordPress REST ingest: find new or changed vonk posts and resolve their tak and PDF link.

``ingest()`` queries ``/wp-json/wp/v2/posts`` for the categories of the configured takken
(``Vonk <tak>`` plus the weaker ``<tak> Leiding`` hint) and for ``search=vonk``, paginating
each query. Every candidate vonk post that cannot be used (no PDF link, unknown tak, no
period) is reported as a warning in the result, never dropped silently.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import httpx

from vonk.models import (
    CATEGORY_TO_TAK,
    LEIDING_CATEGORY_TO_TAK,
    TZ,
    Scope,
    Tak,
    VonkKind,
    VonkPost,
)

WP_API_URL = "https://scoutstervant.be/wp-json/wp/v2"
"""Default WordPress REST base URL."""

ALLOWED_HOSTS: frozenset[str] = frozenset({"scoutstervant.be", "www.scoutstervant.be"})
"""The only hosts poll may fetch from (WP API and PDFs), over https, on every redirect hop."""


class BlockedUrlError(httpx.RequestError):
    """A request to a URL outside ``ALLOWED_HOSTS`` (or not https) was refused."""


def check_url(url: str | httpx.URL, allowed_hosts: frozenset[str] = ALLOWED_HOSTS) -> None:
    """Raise ``BlockedUrlError`` unless ``url`` is https on an allowed host and port."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise BlockedUrlError("invalid URL blocked") from exc
    host = parsed.host.lower()
    if parsed.scheme != "https" or host not in allowed_hosts or parsed.port not in (None, 443):
        raise BlockedUrlError(f"{host or '?'}: blocked (not an allowed https host)")


def host_guard(
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
) -> Callable[[httpx.Request], None]:
    """httpx ``request`` event hook; it runs for the first request and every redirect hop."""

    def guard(request: httpx.Request) -> None:
        try:
            check_url(request.url, allowed_hosts)
        except BlockedUrlError as exc:
            raise BlockedUrlError(str(exc), request=request) from None

    return guard


def describe_http_error(exc: httpx.HTTPError) -> str:
    """Host plus status (or error class) only: no internal addresses, paths or messages."""
    if isinstance(exc, BlockedUrlError):
        return str(exc)
    try:
        host = exc.request.url.host or "?"
    except RuntimeError:
        host = "?"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{host}: HTTP {exc.response.status_code}"
    return f"{host}: {type(exc).__name__}"

PER_PAGE = 100
MAX_PAGES = 20
"""Safety cap per query; the site has a few hundred posts in total."""

_FIELDS = "id,date,modified,modified_gmt,title,link,categories,content"

_MONTHS: Mapping[str, int] = {
    "januari": 1, "jan": 1,
    "februari": 2, "feb": 2,
    "maart": 3, "maa": 3, "mrt": 3,
    "april": 4, "apr": 4,
    "mei": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "augustus": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "okt": 10, "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}  # fmt: skip
_MONTH_RE = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")(?![a-z])"
)
_YEAR_RE = re.compile(r"(?<!\d)(20\d\d)(?!\d)")
_SCOUT_YEAR_RE = re.compile(r"(?<!\d)(?:20)?(\d\d)\s*[-/_]\s*(?:20)?(\d\d)(?!\d)")

# Order matters: the "jong ..." takken must be found (and removed) before gidsen/verkenners.
_TAK_KEYWORDS: tuple[tuple[Tak, re.Pattern[str]], ...] = (
    (Tak.JOGIS, re.compile(r"(?<![a-z])(jogi\w*|jong[\s_-]*gids\w*|jonggid\w*)")),
    (
        Tak.JEEVEES,
        re.compile(
            r"(?<![a-z])(jee?vee?s?|jv(?:'s|’s|s)?(?![a-z])|jong[\s_-]*verkenner\w*|jongverk\w*)"
        ),
    ),
    (Tak.KAPOENEN, re.compile(r"(?<![a-z])kapoen\w*")),
    (Tak.KABOUTERS, re.compile(r"(?<![a-z])kabouter\w*")),
    (Tak.WELPEN, re.compile(r"(?<![a-z])welp\w*")),
    (Tak.GIDSEN, re.compile(r"(?<![a-z])gids\w*")),
    (Tak.VERKENNERS, re.compile(r"(?<![a-z])verkenner\w*")),
    (Tak.JINS, re.compile(r"(?<![a-z])jins?(?![a-z])")),
)

_ANCHOR_RE = re.compile(r"<a\s[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z_:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


@dataclass(frozen=True, slots=True)
class IngestedPost:
    """A usable vonk post together with the scope it covers."""

    post: VonkPost
    scope: Scope


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Usable posts plus warnings about candidate vonk posts that could not be used."""

    posts: tuple[IngestedPost, ...]
    warnings: tuple[str, ...]


# --- parsing helpers ---------------------------------------------------------------------


def extract_pdf_url(content_html: str) -> str | None:
    """Return the PDF link of a post body: the ``pdfemb`` anchor, else any ``.pdf`` href."""
    fallback: str | None = None
    for tag in _ANCHOR_RE.findall(content_html or ""):
        attrs = {m.group(1).lower(): html.unescape(m.group(2) or m.group(3) or "")
                 for m in _ATTR_RE.finditer(tag)}  # fmt: skip
        href = attrs.get("href", "").strip()
        if not urlsplit(href).path.lower().endswith(".pdf"):
            continue
        if "pdfemb" in attrs.get("class", ""):
            return href
        fallback = fallback or href
    return fallback


def pdf_filename(pdf_url: str | None) -> str:
    """Lower-cased, URL-decoded file name of ``pdf_url`` ("" when None)."""
    if not pdf_url:
        return ""
    return unquote(urlsplit(pdf_url).path.rsplit("/", 1)[-1]).lower()


def is_jaarvonk(title: str) -> bool:
    """True when the post title announces the jaarvonk (year overview)."""
    return "jaarvonk" in title.lower()


def tak_from_keywords(text: str) -> Tak | None:
    """Resolve a tak from free text (title or file name); None when absent or ambiguous."""
    text = text.lower()
    found: list[Tak] = []
    for tak, pattern in _TAK_KEYWORDS:
        if pattern.search(text):
            found.append(tak)
            text = pattern.sub(" ", text)
    return found[0] if len(set(found)) == 1 else None


def _single(categories: Iterable[int], mapping: Mapping[int, Tak]) -> Tak | None:
    takken = {mapping[c] for c in categories if c in mapping}
    return takken.pop() if len(takken) == 1 else None


def resolve_tak(categories: Sequence[int], title: str, filename: str) -> Tak | None:
    """Category ``Vonk <tak>`` first, then title, then file name, then ``<tak> Leiding``."""
    return (
        _single(categories, CATEGORY_TO_TAK)
        or tak_from_keywords(title)
        or tak_from_keywords(filename)
        or _single(categories, LEIDING_CATEGORY_TO_TAK)
    )


def _month_year(text: str) -> tuple[int | None, int | None, set[int]]:
    text = text.lower()
    months = [_MONTHS[m] for m in _MONTH_RE.findall(text)]
    years = _YEAR_RE.findall(text)
    return (months[0] if months else None, int(years[0]) if years else None, set(months))


def _infer_year(month: int, anchor: datetime) -> int:
    """Year that puts ``month`` within -1..+10 months of ``anchor`` (closest wins)."""
    best = anchor.year
    best_dist: int | None = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        diff = (year * 12 + month) - (anchor.year * 12 + anchor.month)
        if -1 <= diff <= 10 and (best_dist is None or abs(diff) < best_dist):
            best, best_dist = year, abs(diff)
    return best


def derive_period(
    kind: VonkKind, title: str, filename: str, post_date: datetime
) -> tuple[str, list[str]]:
    """Scope period for a post: filename first, then title, else the post date.

    Returns ``(period, warnings)``; period is ``YYYY-MM`` (monthly) or ``YYYY-YYYY``.
    """
    warnings: list[str] = []
    if kind == "jaarvonk":
        for text in (title, filename):
            m = _SCOUT_YEAR_RE.search(text)
            if m and int(m.group(2)) == (int(m.group(1)) + 1) % 100:
                start = 2000 + int(m.group(1))
                return f"{start:04d}-{start + 1:04d}", warnings
        start = post_date.year if post_date.month >= 8 else post_date.year - 1
        warnings.append("scout year taken from the post date")
        return f"{start:04d}-{start + 1:04d}", warnings

    for source in (filename, title):
        month, year, all_months = _month_year(source)
        if month is None:
            continue
        if len(all_months) > 1:
            warnings.append(f"several months named in {source!r}; using the first")
        if year is None:
            year = _infer_year(month, post_date)
        return f"{year:04d}-{month:02d}", warnings
    warnings.append("no month in file name or title; period taken from the post date")
    return f"{post_date.year:04d}-{post_date.month:02d}", warnings


def _parse_wp_datetime(raw: Any, gmt: Any = None) -> datetime:
    """WordPress dates are naive: ``*_gmt`` is UTC, the plain one is site-local time."""
    if gmt:
        return datetime.fromisoformat(str(gmt)).replace(tzinfo=UTC)
    value = datetime.fromisoformat(str(raw))
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(TZ))


def _rendered(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("rendered", "")
    return html.unescape(str(value or "")).strip()


def is_vonk_candidate(categories: Sequence[int], title: str, filename: str) -> bool:
    """A post counts as a vonk when it is in a ``Vonk <tak>`` category or says "vonk"."""
    return (
        any(c in CATEGORY_TO_TAK for c in categories)
        or "vonk" in title.lower()
        or "vonk" in filename
    )


def parse_post(
    raw: Mapping[str, Any], takken: Sequence[Tak]
) -> tuple[IngestedPost | None, list[str]]:
    """Turn one WP post into an ``IngestedPost``.

    Returns ``(None, [])`` for posts that are not vonks or belong to a tak that is not
    configured, and ``(None, [warning, ...])`` for vonk posts that cannot be used.
    """
    post_id = int(raw["id"])
    title = _rendered(raw.get("title"))
    link = str(raw.get("link", ""))
    categories = tuple(int(c) for c in raw.get("categories", ()))
    pdf_url = extract_pdf_url(_rendered_html(raw.get("content")))
    filename = pdf_filename(pdf_url)
    if not is_vonk_candidate(categories, title, filename):
        return None, []

    label = f"post {post_id} {title!r} ({link})"
    kind: VonkKind = "jaarvonk" if is_jaarvonk(title) else "monthly"
    tak = Tak.GROEP if kind == "jaarvonk" else resolve_tak(categories, title, filename)
    warnings: list[str] = []
    if tak is None:
        warnings.append(f"{label}: unknown tak (categories {list(categories)})")
    elif tak is not Tak.GROEP and tak not in takken:
        return None, []
    if pdf_url is None:
        warnings.append(f"{label}: no PDF link in the post body")
    if warnings or tak is None or pdf_url is None:
        return None, warnings

    post_date = _parse_wp_datetime(raw.get("date"), raw.get("date_gmt"))
    period, period_warnings = derive_period(kind, title, filename, post_date)
    try:
        scope = Scope(tak, period)
    except ValueError as exc:
        return None, [f"{label}: {exc}"]
    post = VonkPost(
        id=post_id,
        modified=_parse_wp_datetime(raw.get("modified"), raw.get("modified_gmt")),
        title=title,
        link=link,
        pdf_url=pdf_url,
        categories=categories,
        tak=tak,
        kind=kind,
        date=post_date,
    )
    return IngestedPost(post, scope), [f"{label}: {w}" for w in period_warnings]


def _rendered_html(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("rendered", "")
    return str(value or "")


# --- HTTP --------------------------------------------------------------------------------


def fetch_all(
    client: httpx.Client, base_url: str, params: Mapping[str, str | int]
) -> list[dict[str, Any]]:
    """GET ``/posts`` with ``params``, following ``X-WP-TotalPages``. Raises on HTTP errors."""
    posts: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_PAGES:
        resp = client.get(
            f"{base_url.rstrip('/')}/posts",
            params={**params, "per_page": PER_PAGE, "page": page, "_fields": _FIELDS},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list):
            raise ValueError(f"unexpected WP response for {dict(params)}: not a list")
        posts.extend(batch)
        total = resp.headers.get("X-WP-TotalPages")
        last = page >= int(total) if total and total.isdigit() else len(batch) < PER_PAGE
        if last:
            break
        page += 1
    return posts


def ingest(client: httpx.Client, takken: Sequence[Tak], base_url: str = WP_API_URL) -> IngestResult:
    """Find the vonk posts of ``takken`` (plus the jaarvonk); newest post per scope wins."""
    categories = sorted(
        {c for c, t in CATEGORY_TO_TAK.items() if t in takken}
        | {c for c, t in LEIDING_CATEGORY_TO_TAK.items() if t in takken}
    )
    queries: list[dict[str, str | int]] = [{"search": "vonk"}]
    if categories:
        queries.insert(0, {"categories": ",".join(map(str, categories))})

    raw_by_id: dict[int, dict[str, Any]] = {}
    for params in queries:
        for raw in fetch_all(client, base_url, params):
            raw_by_id.setdefault(int(raw["id"]), raw)

    warnings: list[str] = []
    by_scope: dict[str, IngestedPost] = {}
    for raw in sorted(raw_by_id.values(), key=lambda r: int(r["id"])):
        item, post_warnings = parse_post(raw, takken)
        warnings.extend(post_warnings)
        if item is None:
            continue
        current = by_scope.get(item.scope.key)
        if current is None or _newer(item.post, current.post):
            by_scope[item.scope.key] = item
    posts = tuple(sorted(by_scope.values(), key=lambda p: p.scope.key))
    return IngestResult(posts=posts, warnings=tuple(warnings))


def _newer(a: VonkPost, b: VonkPost) -> bool:
    return (a.date or a.modified, a.id) > (b.date or b.modified, b.id)
