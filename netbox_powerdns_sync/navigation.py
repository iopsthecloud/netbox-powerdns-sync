from netbox.plugins import PluginMenuItem, PluginMenuButton


apiserver_buttons = [
    PluginMenuButton(
        link='plugins:netbox_powerdns_sync:apiserver_add',
        title='Add',
        icon_class='mdi mdi-plus-thick',
    ),
]

zone_buttons = [
    PluginMenuButton(
        link='plugins:netbox_powerdns_sync:zone_add',
        title='Add',
        icon_class='mdi mdi-plus-thick',
    ),
]

menu_items = (
    PluginMenuItem(
        link='plugins:netbox_powerdns_sync:apiserver_list',
        link_text='API Servers',
        permissions=["netbox_powerdns_sync.view_apiserver"],
        buttons=apiserver_buttons,
    ),
    PluginMenuItem(
        link='plugins:netbox_powerdns_sync:zone_list',
        link_text='DNS Zones',
        permissions=["netbox_powerdns_sync.view_zone"],
        buttons=zone_buttons,
    ),
    PluginMenuItem(
        link='plugins:netbox_powerdns_sync:sync_jobs',
        link_text='Sync Jobs',
        permissions=["core.view_job"],
    ),
)
