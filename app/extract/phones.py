"""Teléfonos: normalización y detección con `phonenumbers` (nunca con IA)."""

import re
from dataclasses import dataclass
from urllib.parse import unquote

import phonenumbers
from phonenumbers import Leniency, PhoneNumberFormat, PhoneNumberMatcher, PhoneNumberType


@dataclass(frozen=True)
class Phone:
    raw: str
    e164: str  # +56987654321

    @property
    def digits(self) -> str:
        """Código de país + número, solo dígitos (el formato que usa wa.me)."""
        return self.e164.lstrip("+")


# Celular mexicano en el formato antiguo (+52 1 + 10 dígitos), que WhatsApp todavía usa.
_MX_LEGACY_MOBILE = re.compile(r"^\+?521(\d{10})$")
# Celular argentino: +54 9 + área + número. El mismo número sin el 9 es el que muestra Maps a veces.
_AR_MOBILE = re.compile(r"^549(\d{10})$")


def canonical_number(digits: str) -> str:
    """Forma única de un número para comparar si dos números son el mismo.

    México: "5215512345678" (formato antiguo) → "525512345678".
    Argentina: "5491123456789" (WhatsApp) → "541123456789" (como aparece a veces en Maps).
    """
    digits = re.sub(r"\D", "", digits or "")
    match = _MX_LEGACY_MOBILE.match(digits)
    if match:
        return f"52{match.group(1)}"
    match = _AR_MOBILE.match(digits)
    return f"54{match.group(1)}" if match else digits


def pretty_number(digits: str | None) -> str | None:
    """"56993557317" → "+56 9 9355 7317" (para mostrar)."""
    if not digits:
        return None
    try:
        number = phonenumbers.parse("+" + digits.lstrip("+"), None)
    except phonenumbers.NumberParseException:
        return "+" + digits.lstrip("+")
    return phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL)


def normalize_phone(raw: str | None, region: str = "CL") -> Phone | None:
    """Devuelve el teléfono en formato E.164 si es válido; si no se puede confirmar, None.

    Argentina: `phonenumbers` ya convierte el "15" local al formato móvil 549 + área.
    México: el formato antiguo de celular (+52 1 …) se acepta como +52 …
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    legacy = _MX_LEGACY_MOBILE.match(re.sub(r"[^\d+]", "", raw))
    parse_from = f"+52{legacy.group(1)}" if legacy else raw
    try:
        number = phonenumbers.parse(parse_from, region.upper())
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return Phone(raw=raw, e164=phonenumbers.format_number(number, PhoneNumberFormat.E164))


def phone_from_tel_href(href: str, region: str = "CL") -> Phone | None:
    """Teléfono de un enlace `tel:` o `callto:`."""
    match = re.match(r"\s*(?:tel|callto):(.*)", href or "", re.IGNORECASE)
    if not match:
        return None
    return normalize_phone(unquote(match.group(1)).split("?")[0], region)


def region_for_phone(e164: str | None, default: str = "CL") -> str:
    """País del teléfono ("CL", "US", "AR"…). Sirve para leer bien la web de un negocio de otro país."""
    if not e164:
        return default
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse(e164, None))
    except phonenumbers.NumberParseException:
        return default
    return region if region and region != "ZZ" else default


def is_probable_mobile(e164: str | None) -> bool:
    """¿Es probablemente un celular (y por lo tanto podría tener WhatsApp)?

    Chile: `phonenumbers` no distingue fijos de celulares, así que se usa la regla local
    (9 dígitos que empiezan con 9). En otros países se usa el tipo de número; si el país no
    los distingue (como EE. UU.), se considera probable.
    """
    if not e164:
        return False
    try:
        number = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return False
    if phonenumbers.region_code_for_number(number) == "CL":
        national = str(number.national_number)
        return len(national) == 9 and national.startswith("9")
    return phonenumbers.number_type(number) in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE)


def find_phones(text: str, region: str = "CL") -> list[Phone]:
    """Teléfonos válidos escritos en un texto visible, sin repetir, en orden de aparición."""
    found: dict[str, Phone] = {}
    for match in PhoneNumberMatcher(text or "", region.upper(), leniency=Leniency.VALID):
        e164 = phonenumbers.format_number(match.number, PhoneNumberFormat.E164)
        found.setdefault(e164, Phone(raw=match.raw_string, e164=e164))
    return list(found.values())
