"""Conservative title-based comparisons for editorial review."""
from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher
import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit

from src.storage import utc
from src.url_safety import normalize_http_url

EVENT_WINDOW = timedelta(hours=48)
MAX_CANDIDATES = 300
MAX_RESULTS = 8
_DASHES = str.maketrans("‐‑‒–—−", "------")
_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid"}

# Check specific actions before generic release/update words.
_ACTIONS = tuple((name, re.compile(pattern)) for name, pattern in (
    ("denial", r"否认|辟谣|澄清|\b(?:denies|denied|refutes?|debunks?)\b"),
    ("delay", r"推迟|延期|延后|\b(?:delays?|delayed|postpones?|postponed)\b"),
    ("recall", r"召回|\brecall(?:s|ed)?\b"),
    ("acquisition", r"收购|并购|\b(?:acqui(?:res?|red|sition)|buys)\b"),
    ("partnership", r"合作|携手|\b(?:partners?|partnership|teams up)\b"),
    ("earnings_preview", r"财报预告|将.{0,12}发布财报|\bto report.{0,30}(?:results|earnings)\b"),
    ("earnings", r"财报|财务业绩|\b(?:financial results|earnings report|reports earnings)\b"),
    ("sales", r"销量|交付量|\b(?:vehicle sales|deliveries)\b"),
    ("price", r"降价|涨价|\b(?:price cut|price increase)\b"),
    ("update", r"升级|更新|上调|下调|\b(?:upgrades?|updates?|boosts?|raises?|lowers?)\b"),
    ("launch_plan", r"计划.{0,12}(?:发布|推出)|将于.{0,15}(?:发布|推出)|\bwill (?:launch|release|unveil)\b"),
    ("launch", r"发布|推出|亮相|首发|\b(?:introduc(?:e[sd]?|ing)|launch(?:es|ed)?|unveil(?:s|ed)?|releases?)\b"),
))

_PRODUCTS = tuple((family, re.compile(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])")) for family, pattern in (
    ("gpt", r"gpt[ -]*([0-9]+(?:\.[0-9]+)*[a-z]?)"),
    ("gemini", r"gemini[ -]*([0-9]+(?:\.[0-9]+)*)"),
    ("deepseek", r"(?:deepseek|深度求索)[ -]*([rv][0-9]+(?:\.[0-9]+)*)"),
    ("qwen", r"(?:qwen|通义千问)[ -]*([0-9]+(?:\.[0-9]+)*)"),
    ("rtx", r"rtx[ -]*([0-9]{4}(?:[ -]*ti|[ -]*super)?)"),
    ("instinct", r"(?:instinct[ -]*)?mi[ -]*([0-9]{3}[a-z]{0,2})"),
    ("model", r"model[ -]*([3syx](?:[ -]+(?:long range|performance|l))?)"),
    ("xiaomi", r"(su7|yu7)"),
))

_ENTITIES = tuple((name, re.compile(pattern)) for name, pattern in (
    ("openai", r"openai"), ("nvidia", r"nvidia|英伟达"), ("amd", r"\bamd\b|超威半导体"),
    ("google", r"google|谷歌"), ("deepseek", r"deepseek|深度求索"),
    ("tesla", r"tesla|特斯拉"), ("byd", r"\bbyd\b|比亚迪"), ("tsmc", r"tsmc|台积电"),
    ("catl", r"catl|宁德时代"), ("xiaomi", r"xiaomi|小米"),
))
_YEAR = re.compile(r"(?<!\d)20\d{2}(?!\d)")
_QUARTER = re.compile(r"(?<![a-z0-9])q([1-4])(?![a-z0-9])|第?([1-4一二三四])季度?")
_QUARTER_NUMBERS = str.maketrans("一二三四", "1234")
_RECURRING = re.compile(r"早报|晚报|晨报|日报|周报|早知道|新闻汇总|\b(?:daily|weekly) (?:brief|digest|roundup|newsletter)\b")
_REVIEW = re.compile(r"评测|实测|差评|上手体验|\b(?:hands-on|reviews?|first impressions)\b")


def original_identity(url):
    try:
        parts = urlsplit(normalize_http_url(url))
    except ValueError:
        return None
    # Preserve parameters that can select a different article or edition.
    query = tuple((key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                  if key.casefold() not in _TRACKING and not key.casefold().startswith("utm_"))
    return parts.scheme, parts.netloc, parts.path, query


@dataclass(frozen=True)
class Headline:
    text: str
    action: str | None
    products: frozenset[tuple[str, str]]
    entities: frozenset[str]
    years: frozenset[str]
    quarters: frozenset[str]
    roundup: bool
    review: bool


def _headline(text):
    normalized = unicodedata.normalize("NFKC", (text or "")[:512]).translate(_DASHES).casefold().strip()
    sections = normalized.split(";")
    roundup = len(sections) >= 3 or bool(_RECURRING.search(normalized))
    if roundup:
        normalized = sections[0]
    action = next((name for name, pattern in _ACTIONS if pattern.search(normalized)), None)
    products = frozenset((family, re.sub(r"[ -]+", "", hit.group(1)))
                         for family, pattern in _PRODUCTS for hit in pattern.finditer(normalized))
    entities = frozenset(name for name, pattern in _ENTITIES if pattern.search(normalized))
    quarters = frozenset((hit.group(1) or hit.group(2)).translate(_QUARTER_NUMBERS)
                         for hit in _QUARTER.finditer(normalized))
    return Headline(" ".join(re.findall(r"\w+", normalized)), action, products, entities,
                    frozenset(_YEAR.findall(normalized)), quarters, roundup, bool(_REVIEW.search(normalized)))


def _match(left, right):
    if left.action and right.action and left.action != right.action:
        return None
    if any(a and b and a.isdisjoint(b) for a, b in ((left.years, right.years), (left.quarters, right.quarters))):
        return None
    shared_products = left.products & right.products
    if left.products and right.products and not shared_products:
        return None
    if left.action and left.action == right.action and shared_products:
        if left.action == "launch" and left.review != right.review:
            return 1, "launch_review"
        if left.roundup or right.roundup:
            return 1, "roundup"
        return 2, "model_action"
    if left.roundup or right.roundup:
        return None
    if min(len(left.text.replace(" ", "")), len(right.text.replace(" ", ""))) < 12:
        return None
    threshold = 0.85 if left.entities & right.entities else 0.93
    matcher = SequenceMatcher(None, left.text, right.text, autojunk=False)
    if matcher.quick_ratio() >= threshold and matcher.ratio() >= threshold:
        return 1, "similar_title"
    return None


def article_time(row):
    for value in (row["published_at"], row["created_at"]):
        if value:
            try:
                return utc(value)
            except (ValueError, TypeError, AttributeError):
                continue
    return None


def related_stories(anchor, candidates):
    anchor_time = article_time(anchor)
    if anchor_time is None:
        return []
    headline = _headline(anchor["title"])
    identity = original_identity(anchor["url"])
    ranked = []
    for row in candidates:
        stamp = article_time(row)
        if stamp is None:
            continue
        seconds = abs((stamp - anchor_time).total_seconds())
        if row["id"] == anchor["id"] or seconds > EVENT_WINDOW.total_seconds():
            continue
        original = original_identity(row["url"])
        if not original:
            continue
        match = (3, "same_link") if identity and identity == original else _match(headline, _headline(row["title"]))
        if match:
            priority, reason = match
            ranked.append(((priority, -seconds, row["id"]), original, row, reason))
    ranked.sort(reverse=True, key=lambda item: item[0])
    seen, results = set(), []
    for _, original, row, reason in ranked:
        if original not in seen:
            seen.add(original)
            results.append((row, reason))
        if len(results) == MAX_RESULTS:
            break
    return results
