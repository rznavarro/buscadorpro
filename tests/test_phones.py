import pytest

from app.extract.phones import find_phones, normalize_phone, phone_from_tel_href


@pytest.mark.parametrize(
    ("raw", "region", "expected"),
    [
        ("+56 9 8765 4321", "CL", "+56987654321"),
        ("9 8765 4321", "CL", "+56987654321"),
        ("987654321", "CL", "+56987654321"),
        ("56987654321", "CL", "+56987654321"),
        ("(72) 223 4567", "CL", "+56722234567"),  # fijo de Rancagua
        ("+56 2 2345 6789", "CL", "+56223456789"),  # fijo de Santiago
        # Argentina: el "15" local se convierte al formato móvil 549 + área
        ("011 15-2345-6789", "AR", "+5491123456789"),
        ("11 15 2345 6789", "AR", "+5491123456789"),
        ("+54 11 15 2345 6789", "AR", "+5491123456789"),
        ("+54 9 11 2345-6789", "AR", "+5491123456789"),
        ("0351 15-234-5678", "AR", "+5493512345678"),
        ("+54 11 4123-4567", "AR", "+541141234567"),  # fijo: sin 9
    ],
)
def test_normalize_phone(raw, region, expected):
    assert normalize_phone(raw, region).e164 == expected


@pytest.mark.parametrize("raw", ["", None, "123", "abc", "+56 9 1234", "15-2345-6789"])
def test_invalid_phones_are_not_confirmed(raw):
    assert normalize_phone(raw, "AR" if raw == "15-2345-6789" else "CL") is None


def test_phone_digits_for_whatsapp_format():
    assert normalize_phone("+56 9 8765 4321").digits == "56987654321"


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("tel:+56722234567", "+56722234567"),
        ("tel:%2B56%209%208765%204321", "+56987654321"),
        ("TEL:+56 9 8765 4321", "+56987654321"),
        ("callto:+56987654321", "+56987654321"),
    ],
)
def test_phone_from_tel_href(href, expected):
    assert phone_from_tel_href(href).e164 == expected


def test_non_tel_href_is_ignored():
    assert phone_from_tel_href("mailto:hola@negocio.cl") is None


def test_find_phones_in_visible_text():
    text = "Llámanos al +56 9 8765 4321 o al (72) 223 4567. Otra vez: 9 8765 4321. RUT 76.123.456-7. Desde 1998."
    assert [p.e164 for p in find_phones(text, "CL")] == ["+56987654321", "+56722234567"]


def test_find_phones_argentina():
    text = "Llamanos al 011 15-2345-6789 o al (011) 4123-4567"
    assert [p.e164 for p in find_phones(text, "AR")] == ["+5491123456789", "+541141234567"]
