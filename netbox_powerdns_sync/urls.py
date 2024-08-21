from django.urls import include, path
from utilities.urls import get_model_urls

from .views.api_servers import ApiServerListView, ApiServerEditView, ApiServerBulkDeleteView
from .views.zones import ZoneListView, ZoneEditView, ZoneBulkDeleteView
from .views.syncs import SyncJobsView, SyncScheduleView, SyncResultView

urlpatterns = [
    # API Servers
    path("api-servers/", ApiServerListView.as_view(), name="apiserver_list"),
    path("api-servers/add/", ApiServerEditView.as_view(), name="apiserver_add"),
    path(
        "api-servers/delete/",
        ApiServerBulkDeleteView.as_view(),
        name="apiserver_bulk_delete",
    ),
    path(
        "api-servers/<int:pk>/",
        include(get_model_urls("netbox_powerdns_sync", "apiserver")),
    ),
    # Zones
    path("zones/", ZoneListView.as_view(), name="zone_list"),
    path("zones/add/", ZoneEditView.as_view(), name="zone_add"),
    path("zones/delete/", ZoneBulkDeleteView.as_view(), name="zone_bulk_delete"),
    path("zones/<int:pk>/", include(get_model_urls("netbox_powerdns_sync", "zone"))),
    path("sync/", SyncJobsView.as_view(), name="sync_jobs"),
    path("sync/schedule/", SyncScheduleView.as_view(), name="sync_schedule"),
    path("sync/<int:job_pk>/", SyncResultView.as_view(), name="sync_result"),
]
