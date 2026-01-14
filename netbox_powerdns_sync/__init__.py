from netbox.plugins import PluginConfig
from .version import __version__


class NetBoxPowerdnsSyncConfig(PluginConfig):
    name = "netbox_powerdns_sync"
    verbose_name = "NetBox PowerDNS sync"
    version = __version__
    description = "Sync DNS records in PowerDNS with NetBox"
    author = "Renaud RAKOTOMALALA"
    author_email = "renaud.rakotomalala@alterway?.fr"
    base_url = "powerdns-sync"
    min_version = "4.4.0"
    default_settings = {
        "ttl_custom_field": None,
        "powerdns_managed_record_comment": "netbox-powerdns-sync",
        "post_save_enabled": False,
        "custom_domain_field": None
    }

    def ready(self):
        super().ready()

        from netbox.registry import registry
        from .models import Zone

        # Enregistrement explicite pour autoriser l'assignation de Jobs aux Zones
        # Compatible avec les versions où registry est un dict ou un callable
        reg = registry() if callable(registry) else registry
        if 'model_features' in reg and 'jobs' in reg['model_features']:
            reg['model_features']['jobs'].add(Zone)

        import netbox_powerdns_sync.signals

config = NetBoxPowerdnsSyncConfig
