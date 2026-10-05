"""Interfaz `MapsSource`: de dónde salen los negocios (sección 2, nota de diseño).

La implementación por defecto navega Google Maps con el navegador. Una futura
implementación con la API oficial de Google Places solo tiene que devolver los mismos
`MapsListing` y `MapsPlace`; el resto del sistema no cambia.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from types import TracebackType

from pydantic import BaseModel, Field

from app.extract.links import LinkKind
from app.extract.whatsapp import WhatsAppExtraction, choose_whatsapp
from app.models import Business, WebsiteStatus


class MapsBlockedError(RuntimeError):
    """Google mostró un captcha, "tráfico inusual" o páginas vacías repetidas. Nunca se intenta resolver."""


class MapsPlaceError(RuntimeError):
    """Una ficha puntual no se pudo leer. La búsqueda sigue con las demás."""


class MapsListing(BaseModel):
    """Un resultado de la lista de Maps, con su posición."""

    position: int
    name: str | None = None
    url: str
    # Lo que muestra la tarjeta de la lista: sirve para saltar, sin abrir la ficha, a los
    # repetidos y a los negocios sin web. `card_seen` dice si la tarjeta se leyó (si es False,
    # que no haya web en la tarjeta no significa nada).
    card_seen: bool = False
    website: str | None = None
    phone_e164: str | None = None


class MapsPlace(BaseModel):
    """Datos de una ficha, normalizados con Python. Todo sale literalmente de Maps."""

    maps_url: str  # canónica
    place_key: str | None = None
    name: str
    category: str | None = None
    address: str | None = None
    city: str | None = None
    commune: str | None = None
    phone_raw: str | None = None
    phone_e164: str | None = None
    website_raw: str | None = None  # el enlace del botón "Sitio web", tal cual
    website: str | None = None  # sin redirección de Google
    website_kind: LinkKind | None = None
    socials: dict[str, list[str]] = Field(default_factory=dict)
    whatsapp: WhatsAppExtraction
    rating: float | None = None
    review_count: int | None = None
    opening_hours: dict[str, str] = Field(default_factory=dict)
    description: str | None = None
    services: list[str] = Field(default_factory=list)
    about: dict[str, list[str]] = Field(default_factory=dict)  # pestaña "Información", por sección
    links: list[str] = Field(default_factory=list)  # todos los enlaces del panel del negocio

    def to_business(self) -> Business:
        """Negocio listo para guardar. Cada dato queda marcado con su fuente ("maps")."""
        decision = choose_whatsapp(self.whatsapp.candidates, self.phone_e164, self.whatsapp.broken_evidence())
        instagram = (self.socials.get("instagram") or [None])[0]
        facebook = (self.socials.get("facebook") or [None])[0]

        # Sección 5, paso 5: qué es realmente el "sitio web" que entrega Maps.
        website: str | None = None
        if self.website_kind is None:
            website_status = WebsiteStatus.SIN_WEB
        elif self.website_kind == LinkKind.WEB_PROPIA:
            website, website_status = self.website, None  # se verifica en la Fase 3
        elif self.website_kind == LinkKind.AGREGADOR:
            # Linktree y similares: se guarda para abrirlo después y buscar ahí el WhatsApp.
            website, website_status = self.website, WebsiteStatus.SOLO_REDES
        elif self.website_kind == LinkKind.RED_SOCIAL:
            website_status = WebsiteStatus.SOLO_REDES
        else:  # WhatsApp, enlace de Google o inválido: no es una web propia
            website_status = WebsiteStatus.SIN_WEB

        fields = {
            "business_name": self.name,
            "category": self.category,
            "phone_raw": self.phone_raw,
            "phone_e164": self.phone_e164,
            "whatsapp_url": decision.url,
            "website": website,
            "instagram": instagram,
            "facebook": facebook,
            "address": self.address,
            "city": self.city,
            "commune": self.commune,
            "opening_hours": self.opening_hours or None,
            "rating": self.rating,
            "review_count": self.review_count,
            "services": self.services or None,
            "description": self.description,
        }
        sources = {name: "maps" for name, value in fields.items() if value is not None}
        if self.website_raw and self.website_kind != LinkKind.WEB_PROPIA:
            sources["website_maps"] = self.website_raw  # lo que Maps mostraba como "Sitio web"

        fields.update({"place_id": self.place_key, "website_status": website_status})
        fields.update(decision.business_fields())
        # Solo se pasan los campos con valor: al actualizar un negocio existente,
        # un dato que Maps no mostró esta vez no borra el que ya estaba guardado.
        return Business(
            google_maps_url=self.maps_url,
            field_sources=sources,
            **{name: value for name, value in fields.items() if value is not None},
        )


class MapsSource(ABC):
    """Fuente de negocios. Se usa como `async with Source(...) as source:`."""

    async def __aenter__(self) -> "MapsSource":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    @abstractmethod
    async def search(self, query: str, limit: int) -> list[MapsListing]:
        """Resultados de la búsqueda, en el orden de Maps, hasta `limit`."""

    async def iter_search(self, query: str, max_results: int = 120) -> AsyncIterator[MapsListing]:
        """Resultados uno a uno, a medida que se necesitan (para juntar N negocios nuevos).

        Por defecto pide la lista completa de una vez; la fuente del navegador la recorre
        de a poco para no visitar Google más de lo necesario.
        """
        for listing in await self.search(query, max_results):
            yield listing

    @abstractmethod
    async def get_details(self, listing: MapsListing) -> MapsPlace:
        """Datos de una ficha. Lanza `MapsBlockedError` si Google bloquea."""
