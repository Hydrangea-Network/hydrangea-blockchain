from typing import KeysView, Generator

SERVICES_FOR_GROUP = {
    "all": (
        "hydrangea_harvester hydrangea_timelord_launcher hydrangea_timelord hydrangea_farmer "
        "hydrangea_full_node hydrangea_wallet hydrangea_data_layer hydrangea_data_layer_http"
    ).split(),
    # TODO: should this be `data_layer`?
    "data": "hydrangea_wallet hydrangea_data_layer".split(),
    "data_layer_http": "hydrangea_data_layer_http".split(),
    "node": "hydrangea_full_node".split(),
    "harvester": "hydrangea_harvester".split(),
    "farmer": "hydrangea_harvester hydrangea_farmer hydrangea_full_node hydrangea_wallet".split(),
    "farmer-no-wallet": "hydrangea_harvester hydrangea_farmer hydrangea_full_node".split(),
    "farmer-only": "hydrangea_farmer".split(),
    "timelord": "hydrangea_timelord_launcher hydrangea_timelord hydrangea_full_node".split(),
    "timelord-only": "hydrangea_timelord".split(),
    "timelord-launcher-only": "hydrangea_timelord_launcher".split(),
    "wallet": "hydrangea_wallet".split(),
    "introducer": "hydrangea_introducer".split(),
    "simulator": "hydrangea_full_node_simulator".split(),
    "crawler": "hydrangea_crawler".split(),
    "seeder": "hydrangea_crawler hydrangea_seeder".split(),
    "seeder-only": "hydrangea_seeder".split(),
}


def all_groups() -> KeysView[str]:
    return SERVICES_FOR_GROUP.keys()


def services_for_groups(groups) -> Generator[str, None, None]:
    for group in groups:
        for service in SERVICES_FOR_GROUP[group]:
            yield service


def validate_service(service: str) -> bool:
    return any(service in _ for _ in SERVICES_FOR_GROUP.values())
