"""Dataset definitions for North Dakota. Since suite 1.18.8 the list is damsuite/updatesources.py,
the same in the ND Data Updater and DamSafety Data; this plugin adds its offline pack source."""

from .damsuite import updatesources as _US
from .damsuite.updatesources import (GEOFABRIK_URL, MICROSOFT_URL,  # noqa: F401
                                     NSI_GENERATOR_COMMIT, NSI_GENERATOR_ZIP_SHA256)
from .packsource import PACK_FOLDER, PackSource

PACK_INFO = ("Offline pack for the ND dam plugins (all layers)",
             "Builds and keeps up to date the statewide offline data pack the ND Dam Hazard "
             "Helper, Breach Runner and Breach Screening read: structures, roads, rail, "
             "lifelines, dams, USGS 3DHP flowlines and waterbodies, aquatic barrier dams and "
             "surveyed and unsurveyed crossings (SARP inventory, ABI_*), USGS road-stream "
             "crossings, ND DEQ landfills and source water protection areas, DWR and HIFLD "
             "layers (from files in the data folder's dwr/ and hifld/), NLCD land cover and "
             "counties. A layer that is missing or older than the refresh interval is fetched; "
             "the rest is left alone.", True)
# id -> (display name, description, enabled by default)
SOURCE_INFO = _US.source_info(PACK_INFO)


def _pack(config, info):
    return PackSource(
        "pack", info["pack"][0], PACK_FOLDER, config.get("data_dir") or "",
        refresh_days=config.get("pack_days", 30), mode_3dhp=config.get("pack_3dhp", "auto"),
        share=config.get("pack_share", True), full=config.get("pack_full", False),
        description=info["pack"][1], box=config.get("pack_box"),
        minutes_3dhp=config.get("pack_3dhp_minutes"),
        only_layers=config.get("pack_only_layers"),
        missing_only=config.get("pack_missing_only", False))


def build_sources(config, merger=None):
    """config keys: enabled (dict id->bool), usa_structures_url, nsi_interval_days,
    nsi_mode ("merged", "nsi2026", "nsi2022"), nsi_match_m, and for the offline pack: data_dir,
    pack_days, pack_3dhp ("auto" | "file" | "service"), pack_3dhp_minutes (the cap on deciding
    that in mode "auto"), pack_share, pack_full, pack_box, and the one-shot run choices
    pack_only_layers (fetch these layers again) and pack_missing_only (build only what is
    missing)."""
    return _US.build_sources(config, merger=merger, pack=_pack, info=SOURCE_INFO)
