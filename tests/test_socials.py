import pytest

from app.extract.socials import classify_social, extract_socials


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.instagram.com/CerrajeriaXYZ/", ("instagram", "https://www.instagram.com/cerrajeriaxyz")),
        ("instagram.com/cerrajeriaxyz?igshid=abc", ("instagram", "https://www.instagram.com/cerrajeriaxyz")),
        ("https://m.facebook.com/cerrajeriaxyz/", ("facebook", "https://www.facebook.com/cerrajeriaxyz")),
        ("https://es-la.facebook.com/cerrajeriaxyz", ("facebook", "https://www.facebook.com/cerrajeriaxyz")),
        (
            "https://www.facebook.com/profile.php?id=100063512345678",
            ("facebook", "https://www.facebook.com/profile.php?id=100063512345678"),
        ),
        ("https://www.tiktok.com/@cerrajeriaxyz", ("tiktok", "https://www.tiktok.com/@cerrajeriaxyz")),
        ("https://www.tiktok.com/@cerrajeriaxyz/video/123", ("tiktok", "https://www.tiktok.com/@cerrajeriaxyz")),
        ("https://www.youtube.com/@cerrajeriaxyz", ("youtube", "https://www.youtube.com/@cerrajeriaxyz")),
        ("https://www.youtube.com/channel/UC123abc", ("youtube", "https://www.youtube.com/channel/UC123abc")),
        ("https://cl.linkedin.com/company/cerrajeria-xyz", ("linkedin", "https://www.linkedin.com/company/cerrajeria-xyz")),
        ("https://twitter.com/CerrajeriaXYZ", ("x", "https://x.com/cerrajeriaxyz")),
    ],
)
def test_profiles_are_recognized(url, expected):
    assert classify_social(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/sharer/sharer.php?u=https://negocio.cl",
        "https://www.facebook.com/tr?id=123&ev=PageView",
        "https://www.facebook.com/plugins/page.php?href=x",
        "https://www.instagram.com/p/C1a2b3c4/",
        "https://www.instagram.com/explore/tags/cerrajeria/",
        "https://www.youtube.com/watch?v=abc123",
        "https://www.youtube.com/embed/abc123",
        "https://twitter.com/intent/tweet?text=hola",
        "https://www.linkedin.com/shareArticle?url=x",
        "https://www.tiktok.com/tag/cerrajeria",
        "https://www.instagram.com/",
        "https://negocio.cl/instagram",
    ],
)
def test_share_links_posts_and_other_sites_are_not_profiles(url):
    assert classify_social(url) is None


def test_extract_socials_groups_and_deduplicates():
    urls = [
        "https://www.instagram.com/cerrajeriaxyz/",
        "https://instagram.com/cerrajeriaxyz",
        "https://www.facebook.com/cerrajeriaxyz",
        "https://www.facebook.com/sharer/sharer.php?u=x",
        "https://negocio.cl/",
    ]
    assert extract_socials(urls) == {
        "instagram": ["https://www.instagram.com/cerrajeriaxyz"],
        "facebook": ["https://www.facebook.com/cerrajeriaxyz"],
    }
