"""Collector registry (source key -> class)."""
from __future__ import annotations

from .cdse import CDSECatalogueCollector
from .ecb import ECBCollector
from .eia import EIAProductsCollector, EIASpotCollector, EIAWeeklyCollector
from .firms import FIRMSCollector
from .jodi import JODICollector
from .news import GDELTCollector, RSSCollector
from .oil_bulletin import OilBulletinCollector
from .optional import ComextCollector, SECCollector
from .portwatch import PortWatchCollector, PortWatchPortsCollector
from .portwatch_geo import PortWatchGeoCollector
from .tankerkoenig import TankerkoenigCollector
from .eurostat import EurostatCollector

COLLECTORS = {
    c.key: c
    for c in (ECBCollector, EIASpotCollector, EIAWeeklyCollector, OilBulletinCollector, PortWatchCollector,
              GDELTCollector, RSSCollector, JODICollector, FIRMSCollector, CDSECatalogueCollector,
              SECCollector, ComextCollector, TankerkoenigCollector, EurostatCollector, EIAProductsCollector, PortWatchPortsCollector,
              PortWatchGeoCollector)
}

ECONOMIC = ["ecb_fx", "eia_spot", "eia_weekly", "eia_products", "oil_bulletin", "portwatch", "portwatch_ports", "portwatch_geo", "jodi", "eurostat", "tankerkoenig"]
NEWS = ["gdelt", "rss"]
SATELLITE = ["firms", "cdse"]
OPTIONAL = ["sec_edgar", "comext"]


def get(ctx, key: str):
    if key not in COLLECTORS:
        raise KeyError(f"unknown source {key!r}; known: {sorted(COLLECTORS)}")
    return COLLECTORS[key](ctx)
