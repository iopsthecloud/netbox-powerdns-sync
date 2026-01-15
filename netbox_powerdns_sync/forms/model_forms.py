from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from netbox.forms import NetBoxModelForm
import powerdns
import requests
from utilities.forms.rendering import FieldSet
from utilities.forms import add_blank_choice

from ..choices import NamingDeviceChoices, NamingFgrpGroupChoices, NamingIpChoices
from ..models import ApiServer, Zone
from ..utils import is_reverse

__all__ = (
    "ApiServerForm",
    "ZoneForm",
)


class ApiServerForm(NetBoxModelForm):
    """
    Form for creating or updating an API server.
    """

    fieldsets = (
        FieldSet(
            "name", "api_url", "api_token", "description", "enabled", "tags",
            name="API Server"
        ),
    )

    class Meta:
        model = ApiServer
        fields = [
            "name", "api_url", "api_token", "description", "enabled", "tags",
        ]

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        api_url = self.cleaned_data.get("api_url")
        api_token = self.cleaned_data.get("api_token")

        if api_url and api_token:
            try:
                api_client = powerdns.PDNSApiClient(api_endpoint=api_url, api_key=api_token)
                endpoint = powerdns.PDNSEndpoint(api_client)
                # Try to access servers to verify the endpoint and token
                # This is a safe read-only operation.
                # We use a short timeout to not block the UI for too long.
                servers = endpoint.servers
                if not servers and self.request:
                    messages.warning(self.request, "PowerDNS API connected but no servers found. Check if the URL is correct (e.g. should it end with /api/v1 ?)")
            except requests.exceptions.RequestException as e:
                if self.request:
                    messages.warning(self.request, f"Unable to connect to PowerDNS API: {e}. Check the API URL and connectivity.")
            except Exception as e:
                # powerdns library might raise other errors (like PDNSError)
                if self.request:
                    messages.warning(self.request, f"PowerDNS API error: {e}. Check if the URL is correct (e.g. should it end with /api/v1 ?)")


class ZoneForm(NetBoxModelForm):
    naming_ip_method = forms.ChoiceField(
        choices=add_blank_choice(NamingIpChoices),
        required=False,
        label='From IP',
        help_text="How to construct DNS name from IP address. Leave blank to ignore.",
    )
    naming_device_method = forms.ChoiceField(
        choices=add_blank_choice(NamingDeviceChoices),
        required=False,
        label='From device',
        help_text="How to construct DNS name from device IP is assigned to. Leave blank to ignore.",
    )
    naming_fgrpgroup_method = forms.ChoiceField(
        choices=add_blank_choice(NamingFgrpGroupChoices),
        required=False,
        label='From FHRP Group',
        help_text="How to construct DNS name from FHRP Group IP is assigned to. Leave blank to ignore.",
    )

    fieldsets = (
        FieldSet(
            "name", "description", "enabled", "api_servers", "is_default",
            "default_ttl",
            name="DNS Zone"
        ),
        FieldSet(
            "match_ipaddress_tags", "match_interface_tags",
            "match_device_tags", "match_fhrpgroup_tags", "match_device_roles",
            "match_interface_mgmt_only",
            name="Matchers"
        ),
        FieldSet(
            "naming_ip_method", "naming_device_method", "naming_fgrpgroup_method",
            name="Naming methods"
        ),
        FieldSet("tags", name="General"),
    )

    class Meta:
        model = Zone
        fields = [
            "name", "description", "enabled", "api_servers", "is_default",
            "default_ttl", "match_ipaddress_tags", "match_interface_tags",
            "match_device_tags", "match_fhrpgroup_tags", "match_device_roles",
            "match_interface_mgmt_only", "naming_ip_method", "naming_device_method",
            "naming_fgrpgroup_method", "tags",
        ]

    def clean(self):
        super().clean()
        self.clean_match_tags(self.cleaned_data)
        self.clean_match_roles(self.cleaned_data)
        self.clean_naming_methods(self.cleaned_data)

    def clean_match_tags(self, data):
        if not is_reverse(data["name"]):
            return
        fields = (
            "match_ipaddress_tags",
            "match_interface_tags",
            "match_device_tags",
            "match_fhrpgroup_tags",
        )
        for f in fields:
            if data.get(f):
                self.add_error(f, "Cannot set match tags for reverse zone")

    def clean_match_roles(self, data):
        if not is_reverse(data["name"]):
            return

        if data.get("match_device_roles"):
            self.add_error("match_device_roles", "Cannot set match roles for reverse zone")

    def clean_naming_methods(self, data):
        if not is_reverse(data["name"]):
            return
