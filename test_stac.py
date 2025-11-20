import requests
from main_functions import main_utils, main_publish_stac_fsdi
import configuration as config

main_publish_stac_fsdi.publish_to_stac("ch.swisstopo.swisseo_s2-sr_v200_mosaic_2025-06-10t103641_cloudprobability-10m.tif",
        "2025-06-10t103641",
        "ch.swisstopo.swisseo_s2-sr_v200",
        config.PRODUCT_S2_LEVEL_2A['geocat_id'],
        None
    )
breakpoint()
item_path='https://sys-data.int.bgdi.ch/api/stac/v0.9/collections/ch.swisstopo.swisseo_s2-sr_v200/items/2025-06-10t103641'


item_payload={'id': '2025-06-10t103641', 'geometry': {'type': 'Polygon', 'coordinates': [[(8.488505088522633, 46.816734222561195), (10.66589636434412, 46.7759572646395), (10.734607268558028, 47.89862782695354), (8.510872419458634, 47.94026461332457), (8.488505088522633, 46.816734222561195)]]}, 'properties': {'datetime': '2025-06-10T10:36:41Z', 'title': 'swisseo_s2-sr_v200_2025-06-10t103641'}, 'links': [{'href': 'https://map.geo.admin.ch/index.html?layers=WMS||swisseo_s2-sr_v200_2025-06-10t103641||https://wms.geo.admin.ch/?item=2025-06-10t103641||ch.swisstopo.swisseo_s2-sr_v200', 'rel': 'visual'}, {'href': 'https://sys-data.int.bgdi.ch/ch.swisstopo.swisseo_s2-sr_v200/2025-06-10t103641/thumbnail.jpg', 'rel': 'preview'}]}
print(item_payload)


response = requests.put(
            url=item_path,
            json=item_payload,
            auth=(conf, config.STAC_PASSWORD))

print(response.status_code)
breakpoint()
print(item_payload)