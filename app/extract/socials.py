"""Redes sociales: reconoce perfiles (no publicaciones ni botones de compartir)."""

import re
from collections.abc import Iterable
from urllib.parse import parse_qs, urlsplit

# dominio → (red, host canónico)
_DOMAINS: dict[str, tuple[str, str]] = {
    "instagram.com": ("instagram", "www.instagram.com"),
    "facebook.com": ("facebook", "www.facebook.com"),
    "fb.com": ("facebook", "www.facebook.com"),
    "fb.me": ("facebook", "www.facebook.com"),
    "tiktok.com": ("tiktok", "www.tiktok.com"),
    "youtube.com": ("youtube", "www.youtube.com"),
    "linkedin.com": ("linkedin", "www.linkedin.com"),
    "x.com": ("x", "x.com"),
    "twitter.com": ("x", "x.com"),
    "pinterest.com": ("pinterest", "www.pinterest.com"),
    "pinterest.cl": ("pinterest", "www.pinterest.com"),
}

# Primeros segmentos de ruta que no son un perfil (compartir, publicaciones, píxeles…)
_NOT_PROFILE: dict[str, frozenset[str]] = {
    "instagram": frozenset({"p", "reel", "reels", "tv", "explore", "accounts", "stories", "direct", "about", "legal"}),
    "facebook": frozenset(
        {"sharer", "sharer.php", "share", "share.php", "plugins", "tr", "dialog", "login", "login.php",
         "policies", "help", "l.php", "hashtag", "groups", "watch", "events", "photo.php", "photo", "story.php"}
    ),
    "tiktok": frozenset({"share", "embed", "tag", "music", "discover", "search"}),
    "youtube": frozenset({"watch", "embed", "shorts", "playlist", "results", "feed", "redirect"}),
    "linkedin": frozenset({"shareArticle", "sharing", "feed", "login", "jobs"}),
    "x": frozenset({"intent", "share", "home", "search", "hashtag", "i", "login"}),
    "pinterest": frozenset({"pin", "search", "ideas"}),
}

# Redes cuyo perfil ocupa dos segmentos de ruta: youtube.com/channel/<id>, linkedin.com/company/<nombre>
_TWO_SEGMENT_PREFIXES = frozenset({"channel", "c", "user", "company", "in", "school", "showcase", "pages"})


def _network_for_host(host: str) -> tuple[str, str] | None:
    host = host.lower()
    for domain, network in _DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return network
    return None


def classify_social(url: str | None) -> tuple[str, str] | None:
    """(red, URL del perfil) si el enlace apunta a un perfil; None en otro caso."""
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlsplit(url)
    network = _network_for_host(parts.hostname or "")
    if not network:
        return None
    name, host = network
    segments = [segment for segment in parts.path.split("/") if segment]

    if name == "facebook" and segments[:1] == ["profile.php"]:
        profile_id = parse_qs(parts.query).get("id", [""])[0]
        return (name, f"https://{host}/profile.php?id={profile_id}") if profile_id.isdigit() else None
    if not segments or segments[0] in _NOT_PROFILE[name] or segments[0].lower() in _NOT_PROFILE[name]:
        return None
    if name == "tiktok" and not segments[0].startswith("@"):
        return None
    if segments[0] in _TWO_SEGMENT_PREFIXES and len(segments) >= 2:
        profile_path = "/".join(segments[:2])
    else:
        profile_path = segments[0]
    if name in ("instagram", "x"):
        profile_path = profile_path.lower()
    return name, f"https://{host}/{profile_path}"


def extract_socials(urls: Iterable[str]) -> dict[str, list[str]]:
    """Perfiles por red, sin repetir, en el orden en que aparecen."""
    found: dict[str, dict[str, None]] = {}
    for url in urls:
        social = classify_social(url)
        if social:
            found.setdefault(social[0], {}).setdefault(social[1], None)
    return {network: list(profiles) for network, profiles in found.items()}
