"""
STAC Item Filter by Metadata Asset Properties (Direct Collection Search)

This script filters STAC items directly in a predefined collection
based on specific conditions found inside the 'metadata' asset JSON
(Orbit NR and Cloud Percentage).
Uses pure requests with full pagination for items. ( Since v200 was not yet published in STAC officially)

Author: Dave Oesch
Date: 2026-04-07
"""

import logging
import requests
from urllib.parse import urljoin

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

STAC_BASE_URL = "https://data.geo.admin.ch/api/stac/v0.9/"
TARGET_COLLECTION = "ch.swisstopo.swisseo_s2-sr_v200"

# ---------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------

def get_items_iterator(base_url: str, collection_id: str):
    """
    Ein Generator, der alle Items einer Collection abruft
    und die Paginierung (next-Links) automatisch abarbeitet.
    """
    url = urljoin(base_url, f"collections/{collection_id}/items")

    while url:
        r = requests.get(url)
        r.raise_for_status()
        data = r.json()

        # Items der aktuellen Seite zurückgeben
        features = data.get("features", [])
        for feature in features:
            yield feature

        # Nach dem Link zur nächsten Seite suchen
        next_url = None
        for link in data.get("links", []):
            if link.get("rel") == "next":
                next_url = link.get("href")
                if not next_url.startswith("http"):
                    next_url = urljoin(url, next_url)
                break

        url = next_url

def check_item_metadata(asset_href: str, item_id: str) -> bool:
    """
    Lädt das JSON vom Metadata-Asset herunter und prüft die Bedingungen.
    """
    try:
        r = requests.get(asset_href)
        r.raise_for_status()
        data = r.json()

        properties = data.get("PROPERTIES", {})
        orbit_nr = properties.get("ORBIT_NR")
        cloud_pct_str = properties.get("CLOUDPERCENTAGE")

        if orbit_nr and cloud_pct_str:
            cloud_pct = float(cloud_pct_str)

            logging.info(f"Prüfe {item_id}: Orbit={orbit_nr}, Cloud={cloud_pct}%")

            if orbit_nr in ["8", "22"] and cloud_pct > 85.0:
                return True

        return False

    except Exception as e:
        logging.error(f"Fehler beim Abrufen/Parsen der Metadaten: {asset_href} | {str(e)}")
        return False

# ---------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(message)s"
    )

    matching_item_ids = []

    print(f"\nStarte direkte Suche in Collection: {TARGET_COLLECTION}...")
    print("Rufe Items ab (inkl. Pagination, bitte warten)...\n")

    # Direkte Iteration über alle Seiten der Ziel-Collection
    for item in get_items_iterator(STAC_BASE_URL, TARGET_COLLECTION):
        item_id = item.get("id")
        assets = item.get("assets", {})

        metadata_href = None
        for asset_key, asset in assets.items():
            href = asset.get("href", "")
            if "metadata" in asset_key.lower() or href.endswith("metadata.json"):
                metadata_href = href
                break

        if metadata_href:
            is_match = check_item_metadata(metadata_href, item_id)

            if is_match:
                matching_item_ids.append(item_id)
                logging.info(f"--> MATCH GEFUNDEN: {item_id} erfüllt alle Kriterien!\n")
        else:
            logging.debug(f"Keine Metadaten-Datei im Item {item_id} gefunden.")

    # Ergebnisse ausgeben
    print("\n" + "=" * 60)
    print("FILTER SUMMARY")
    print("=" * 60)
    print(f"Collection: {TARGET_COLLECTION}")
    print(f"Gefundene Items (Orbit 8 oder 22 UND Cloud Percentage > 85): {len(matching_item_ids)}\n")

    for item_id in matching_item_ids:
        print(item_id)

if __name__ == "__main__":
    main()