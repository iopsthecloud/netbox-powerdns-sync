import logging
import traceback
from datetime import timedelta

from django.conf import settings  # Importer la configuration de NetBox
from core.choices import JobStatusChoices
from core.models import Job
from dcim.models import Device, Interface
from django.db.models import Q
from extras.choices import LogLevelChoices
from ipam.models import FHRPGroup, IPAddress
from netaddr import IPNetwork
from virtualization.models import VirtualMachine, VMInterface

from netbox_powerdns_sync.constants import FAMILY_TYPES, PTR_TYPE

from .exceptions import PowerdnsSyncNoServers, PowerdnsSyncServerZoneMissing
from .models import ApiServer, Zone
from .naming import generate_fqdn
from .record import DnsRecord
from .utils import (
    get_custom_domain,
    get_ip_ttl,
    make_canonical,
    make_dns_label,
    set_dns_name,
    is_reverse,
    has_managed_comment,
)

logger = logging.getLogger("netbox.netbox_powerdns_sync.jobs")


class JobLoggingMixin:
    def log(self, level: str, msg: str) -> None:
        if not hasattr(self, "_log_buffer"):
            self._log_buffer = self.job.data.get("log", []) if self.job.data else []

        self._log_buffer.append(
            {
                "message": msg,
                "status": level,
            }
        )

    def flush_logs(self) -> None:
        if hasattr(self, "_log_buffer"):
            data = self.job.data or {}
            data["log"] = self._log_buffer
            self.job.data = data
            self.job.save()

    def log_debug(self, msg: str) -> None:
        if settings.DEBUG:
            logger.debug(msg)
            self.log(LogLevelChoices.LOG_DEBUG, msg)

    def log_success(self, msg: str) -> None:
        logger.info(msg)
        self.log(LogLevelChoices.LOG_SUCCESS, msg)
        self.flush_logs()

    def log_info(self, msg: str) -> None:
        if settings.DEBUG or logger.isEnabledFor(logging.INFO):
            logger.info(msg)
            self.log(LogLevelChoices.LOG_INFO, msg)

    def log_warning(self, msg: str) -> None:
        if settings.DEBUG or logger.isEnabledFor(logging.WARNING):
            logger.warning(msg)
            self.log(LogLevelChoices.LOG_WARNING, msg)
            self.flush_logs()

    def log_failure(self, msg: str) -> None:
        if settings.DEBUG or logger.isEnabledFor(logging.ERROR):
            logger.error(msg)
            self.log(LogLevelChoices.LOG_FAILURE, msg)
            self.flush_logs()


class PowerdnsTask(JobLoggingMixin):
    def __init__(self, job: Job) -> None:
        self.job = job
        self.init_attrs()

    def init_attrs(self):
        self.fqdn: str = ""
        self.forward_zone: Zone = None
        self.reverse_zone: Zone = None
        self.make_fqdn_ran: bool = False

    def get_pdns_servers_for_zone(self, zone_name: str) -> list[ApiServer]:
        if not zone_name:
            self.log_debug("get_pdns_servers_for_zone: zone_name is empty")
            return []
        # Support names with or without trailing dot, and case-insensitive
        zone_name_clean = zone_name.rstrip(".")
        zone_name_dot = zone_name_clean + "."
        zone = Zone.objects.filter(
            Q(name__iexact=zone_name_clean) | Q(name__iexact=zone_name_dot)
        ).first()
        if not zone:
            self.log_debug(
                f"get_pdns_servers_for_zone: Zone object not found for {zone_name} "
                f"(tried iexact {zone_name_clean} and {zone_name_dot})"
            )
            return []
        servers = list(zone.api_servers.filter(enabled=True))
        if not servers:
            self.log_debug(
                f"get_pdns_servers_for_zone: No enabled API servers found for zone {zone.name} (ID: {zone.pk})"
            )
        return servers

    def add_to_output(self, row):
        if not self.job.data:
            self.job.data = dict()
        if "output" not in self.job.data:
            self.job.data["output"] = []
        self.job.data["output"].append(row)

    def make_name_from_interface(
        self, interface: Interface | VMInterface, host: Device | VirtualMachine
    ) -> str:
        name = host.name
        name = ".".join(map(make_dns_label, name.split(".")))
        if self.ip != host.primary_ip4 and self.ip != host.primary_ip6:
            name = make_dns_label(interface.name) + "." + name
        return name

    def make_fqdn(self) -> str:
        """Determines FQDN and sets forward zone"""
        if self.make_fqdn_ran:
            return self.fqdn
        self.make_fqdn_ran = True
        if self.determine_forward_zone():
            self.fqdn = generate_fqdn(self.ip, self.forward_zone)
        return self.fqdn

    def determine_forward_zone(self):
        # determine zone from any FQDN names
        name = None
        if self.ip.dns_name:
            name = self.ip.dns_name
        elif isinstance(self.ip.assigned_object, Interface):
            name = self.ip.assigned_object.device.name
        elif isinstance(self.ip.assigned_object, VMInterface):
            name = self.ip.assigned_object.virtual_machine.name
        elif isinstance(self.ip.assigned_object, FHRPGroup):
            name = self.ip.assigned_object.name
        if name:
            self.forward_zone = Zone.get_best_zone(name)
        # determine zone by matching tags or roles
        if not self.forward_zone:
            self.forward_zone = Zone.match_ip(self.ip).first()

        if not self.forward_zone:
            self.forward_zone = get_custom_domain(self.ip)

        return self.forward_zone

    def make_reverse_domain(self) -> str | None:
        """Returns reverse domain name"""
        self.log_debug(f"Making reverse domain for {self.ip}")
        return make_canonical(self.ip.address.ip.reverse_dns)

    def create_record(self, dns_record: DnsRecord) -> None:
        servers = self.get_pdns_servers_for_zone(dns_record.zone_name)

        if not servers:
            self.log_debug(f"create_record: No servers found for zone {dns_record.zone_name}")
            raise PowerdnsSyncNoServers(
                f"No valid servers found for zone {dns_record.zone_name}"
            )

        for api_server in servers:
            zone = api_server.api.get_zone(make_canonical(dns_record.zone_name))
            if not zone:
                raise PowerdnsSyncServerZoneMissing(
                    f"Zone {dns_record.zone_name} not found on server {api_server}"
                )
            self.add_to_output(
                {
                    "action": "CREATE",
                    "rr": str(dns_record),
                    "zone": str(zone),
                    "server": str(api_server),
                }
            )
            zone.create_records([dns_record.as_rrset()])

    def delete_record(self, dns_record: DnsRecord) -> None:
        servers = self.get_pdns_servers_for_zone(dns_record.zone_name)
        if not servers:
            self.log_debug(f"delete_record: No servers found for zone {dns_record.zone_name}")
            raise PowerdnsSyncNoServers(
                f"No valid servers found for zone {dns_record.zone_name}"
            )
        for api_server in servers:
            zone = api_server.api.get_zone(make_canonical(dns_record.zone_name))
            if not zone:
                raise PowerdnsSyncServerZoneMissing(
                    f"Zone {dns_record.zone_name} not found on server {api_server}"
                )
            self.add_to_output(
                {
                    "action": "DELETE",
                    "rr": str(dns_record),
                    "zone": str(zone),
                    "server": str(api_server),
                }
            )
            zone.delete_records([dns_record.as_rrset()])


class PowerdnsTaskIP(PowerdnsTask):
    def __init__(self, job: Job) -> None:
        super().__init__(job)
        self.ip: IPAddress = job.object
        self.log_debug(f"IP: {self.ip}")

    @classmethod
    def run_update_ip(cls, job: Job, *args, **kwargs) -> None:
        task = cls(job)
        if job.object_id and not job.object:
            task.job.start()
            task.log_warning(
                "No IP Address object given. IP was probably removed or DB transaction aborted, nothing to do."
            )
            task.job.terminate(status=JobStatusChoices.STATUS_COMPLETED)
            return
        try:
            task.log_debug("Starting task")
            task.job.start()
            task.log_debug("Creating forward record")
            task.create_forward()
            task.log_debug("Creating reverse record")
            task.create_reverse()
            task.flush_logs()
            task.log_success("Finished")
            task.job.terminate()
        except Exception as e:
            task.log_failure(f"error {e}")
            task.job.data = task.job.data or dict()
            task.job.data["exception"] = str(e)
            task.job.terminate(status=JobStatusChoices.STATUS_ERRORED)
            raise e

    def create_forward(self) -> None:
        self.make_fqdn()

        if not self.forward_zone:
            self.log_info(f"No matching forward zone found for IP:{self.ip}. Skipping")
            return
        else:
            self.log_debug(f"Found matching forward zone to be {self.forward_zone}")

        if not self.fqdn:
            self.log_info(
                f"No FQDN could be determined for IP:{self.ip} (zone:{self.forward_zone}). Skipping"
            )
            return

        name = self.fqdn.replace(self.forward_zone.name, "").rstrip(".")

        dns_record = DnsRecord(
            name=name,
            dns_type=FAMILY_TYPES[self.ip.family],
            data=str(self.ip.address.ip),
            ttl=get_ip_ttl(self.ip) or self.forward_zone.default_ttl,
            zone_name=self.forward_zone.name,
        )
        self.log_info(f"Forward record: {dns_record}")
        self.create_record(dns_record)
        self.log_info(f"Forward record created")

    def create_reverse(self) -> None:
        self.make_fqdn()

        if not self.fqdn:
            self.log_info(
                f"No FQDN could be determined for IP:{self.ip}. Skipping"
            )
            return

        reverse_fqdn = self.make_reverse_domain()
        self.reverse_zone = Zone.get_best_zone(reverse_fqdn)

        if not self.reverse_zone:
            self.log_warning(
                f"No reverse zone for IP:{self.ip} fqdn:{self.fqdn} Skipping"
            )
            return

        name = reverse_fqdn.replace(self.reverse_zone.name, "").rstrip(".")
        fqdn = generate_fqdn(self.ip, self.reverse_zone)
        custom_domain = get_custom_domain(self.ip)
        dns_record = DnsRecord(
            name=name,
            dns_type=PTR_TYPE,
            data=f"{fqdn or ''}{custom_domain or ''}.",
            ttl=get_ip_ttl(self.ip) or self.reverse_zone.default_ttl,
            zone_name=self.reverse_zone.name,
        )

        self.log_info(f"Reverse record {dns_record}")
        self.create_record(dns_record)
        set_dns_name(self.ip, make_canonical(dns_record.data))
        self.log_success("Reverse record created")


class PowerdnsTaskFullSync(PowerdnsTask):
    def __init__(self, job: Job, zone_id: int = None) -> None:
        super().__init__(job)

        if zone_id:
            self.zone: Zone = Zone.objects.filter(pk=zone_id).first()
        else:
            self.zone: Zone = job.object

    @classmethod
    def run_full_sync(cls, job: Job, zone_id: int = None, *args, **kwargs) -> None:
        """Runs full synchronization, schedules next job if configured"""
        task = cls(job, zone_id=zone_id)

        try:
            if not task.zone:
                task.log_failure(f"Zone not found (zone_id={zone_id})")
                task.job.terminate(status=JobStatusChoices.STATUS_ERRORED)
                return

            task.log_debug(f"Starting sync for zone {task.zone}")
            task.job.start()
            if not task.zone.enabled:
                task.log_warning(
                    f"Zone {task.zone} is disabled for updates, not syncing"
                )
                task.job.terminate()
                return
            task.log_debug("Loading Netbox records")
            netbox_records, ignored_netbox_count = task.load_netbox_records()
            task.flush_logs()
            task.log_debug("Loading Powerdns records")
            pdns_records, pdns_excluded_records, pdns_unmanaged_records = task.load_pdns_records()
            task.flush_logs()
            task.log_info(
                f"Found record count: netbox:{len(netbox_records)} pdns:{len(pdns_records)} pdns unmanaged:{len(pdns_unmanaged_records)} pdns excluded:{len(pdns_excluded_records)}"
            )
            to_delete = pdns_records - netbox_records
            to_create = netbox_records - pdns_records
            task.log_info(
                f"Record change count: to_delete:{len(to_delete)} to_create:{len(to_create)}"
            )
            for record in to_delete:
                task.delete_record(record)
            for record in to_create:
                excluded_record_type = pdns_excluded_records.get(record.get_fqdn())
                task.log_debug(
                    f"Check if {record.get_fqdn()} is in pdns_excluded_records => {excluded_record_type}"
                )
                if excluded_record_type:
                    task.log_debug(
                        f"Record {record.name} of type {excluded_record_type} skipped because it was found in pdns_excluded_records."
                    )
                else:
                    # Check if it exists but is unmanaged
                    # We compare name, type and data. TTL might be different.
                    matching_unmanaged = [
                        r for r in pdns_unmanaged_records
                        if r.name == record.name and r.dns_type == record.dns_type and r.data == record.data
                    ]
                    if matching_unmanaged:
                        task.log_info(
                            f"Taking management of existing record {record.get_fqdn()} (was unmanaged in PowerDNS)"
                        )
                    else:
                        # Check if it was managed but changed (e.g. TTL)
                        matching_managed = [
                            r for r in pdns_records
                            if r.name == record.name and r.dns_type == record.dns_type and r.data == record.data
                        ]
                        if matching_managed:
                            task.log_info(
                                f"Updating existing managed record {record.get_fqdn()} (TTL changed from {matching_managed[0].ttl} to {record.ttl})"
                            )
                        else:
                            task.log_info(f"Creating new record {record.get_fqdn()}")

                    task.create_record(record)
            task.flush_logs()
            task.log_success(f"Finished. Summary of ignored records: {ignored_netbox_count} IP(s) from NetBox without valid matching zone/FQDN, {len(pdns_excluded_records)} record(s) in PowerDNS with unmanaged types.")
            task.job.terminate()
        except PowerdnsSyncNoServers as e:
            task.log_failure(str(e))
            task.job.data = task.job.data or dict()
            task.job.save()  # Sauvegarder les logs avant terminate
            task.job.terminate(status=JobStatusChoices.STATUS_ERRORED)
        except PowerdnsSyncServerZoneMissing as e:
            task.log_failure(str(e))
            task.job.data = task.job.data or dict()
            task.job.save()  # Sauvegarder les logs avant terminate
            task.job.terminate(status=JobStatusChoices.STATUS_ERRORED)
        except Exception as e:
            stacktrace = traceback.format_exc()
            task.log_failure(
                f"An exception occurred: `{type(e).__name__}: {e}`\n```\n{stacktrace}\n```"
            )
            task.job.data = task.job.data or dict()
            task.job.save()  # Sauvegarder les logs avant terminate
            task.job.terminate(status=JobStatusChoices.STATUS_ERRORED)

        # Schedule the next job if an interval has been set
        if job.interval:
            new_scheduled_time = job.scheduled + timedelta(minutes=job.interval)
            Job.enqueue(
                cls.run_full_sync,
                instance=task.zone,
                name=job.name,
                user=job.user,
                schedule_at=new_scheduled_time,
                interval=job.interval,
            )

    @property
    def get_addresses(self):
        """Get IPAddress objects that could have DNS records"""
        zone_canonical = self.zone.name
        zone_domain = self.zone.name.rstrip(".")
        self.log_debug(f"Zone canonical: {zone_canonical}")
        self.log_debug(f"Zone domain: {zone_domain}")

        # START WITH AN EMPTY Q to allow multiple matching strategies
        query_zone = Q()

        # Domain-based matching (Keep for cases where dns_name is already set)
        query_zone |= Q(dns_name__endswith=zone_canonical) | Q(
            dns_name__endswith=zone_domain
        )
        query_zone |= Q(interface__device__name__endswith=zone_canonical) | Q(
            interface__device__name__endswith=zone_domain
        )
        query_zone |= Q(
            vminterface__virtual_machine__name__endswith=zone_canonical
        ) | Q(vminterface__virtual_machine__name__endswith=zone_domain)
        query_zone |= Q(fhrpgroup__name__endswith=zone_canonical) | Q(
            fhrpgroup__name__endswith=zone_domain
        )

        self.log_debug("Checking if this is a rdns zone")
        parts = zone_domain.split(".")
        if is_reverse(zone_canonical):
            self.log_debug(f"Zone is reverse zone, looking for prefixes")
            network_cidr = None

            # More robust extraction of IPv4 parts for reverse zones (Fix for 4.4)
            ipv4_parts = [p for p in parts if p.isdigit()]
            if len(ipv4_parts) == 3: # /24
                network_cidr = IPNetwork(f"{ipv4_parts[2]}.{ipv4_parts[1]}.{ipv4_parts[0]}.0/24")
            elif len(ipv4_parts) == 2: # /16
                network_cidr = IPNetwork(f"{ipv4_parts[1]}.{ipv4_parts[0]}.0.0/16")
            elif len(ipv4_parts) == 1: # /8
                network_cidr = IPNetwork(f"{ipv4_parts[0]}.0.0.0/8")

            if network_cidr:
                self.log_debug(
                    f"Prefix found: {network_cidr}. Checking for hosts."
                )
                # Query any address within the CIDR range
                query_zone |= Q(address__net_host_contained=network_cidr)
        else:
            self.log_debug("No rDNS zone found.")

        # filter for matchers (tags & roles)
        query_zone |= Q(tags__in=self.zone.match_ipaddress_tags.all())
        query_zone |= Q(interface__tags__in=self.zone.match_interface_tags.all())
        query_zone |= Q(vminterface__tags__in=self.zone.match_interface_tags.all())
        query_zone |= Q(interface__device__tags__in=self.zone.match_device_tags.all())
        query_zone |= Q(
            vminterface__virtual_machine__tags__in=self.zone.match_device_tags.all()
        )
        query_zone |= Q(fhrpgroup__tags__in=self.zone.match_fhrpgroup_tags.all())
        query_zone |= Q(interface__device__role__in=self.zone.match_device_roles.all())
        query_zone |= Q(
            vminterface__virtual_machine__role__in=self.zone.match_device_roles.all()
        )
        results = IPAddress.objects.filter(query_zone).distinct()
        if self.zone.match_interface_mgmt_only:
            results = results.filter(interface__mgmt_only=True)
        return results

    def load_netbox_records(self) -> tuple[set[DnsRecord], int]:
        records = set()
        ignored_count = 0
        ip: IPAddress
        ip_addresses = self.get_addresses

        self.log_info(f"Found {ip_addresses.count()} matching addresses to check")
        for ip in ip_addresses:
            self.log_debug(f"Checking IP: {ip}")
            self.init_attrs()
            self.ip = ip
            
            # --- PROCESS FORWARD ---
            self.make_fqdn()
            
            if self.forward_zone and self.forward_zone == self.zone and self.fqdn:
                name = self.fqdn.replace(self.forward_zone.name, "").rstrip(".")
                self.log_debug(
                    f"Forward zone {self.forward_zone} matches current zone {self.zone}, targeting forward record for {self.fqdn}"
                )
                records.add(
                    DnsRecord(
                        name=name,
                        data=str(ip.address.ip),
                        dns_type=FAMILY_TYPES.get(ip.family),
                        zone_name=self.forward_zone.name,
                        ttl=get_ip_ttl(ip) or self.forward_zone.default_ttl,
                    )
                )

            # --- PROCESS REVERSE ---
            if self.zone.is_reverse:
                reverse_fqdn = self.make_reverse_domain()
                self.log_debug(f"Reverse FQDN: {reverse_fqdn}")
                
                # Check if this IP's reverse address belongs to the current zone
                if reverse_fqdn.endswith(make_canonical(self.zone.name)):
                    name = reverse_fqdn.replace(self.zone.name, "").rstrip(".")
                    
                    # PRIORITY: Use NetBox dns_name if it exists (allows external domains)
                    dns_data = None
                    if ip.dns_name:
                        dns_data = make_canonical(ip.dns_name)
                    else:
                        # Fallback to generated FQDN if a forward zone is matched
                        fqdn = generate_fqdn(self.ip, self.forward_zone) if self.forward_zone else None
                        custom_domain = get_custom_domain(self.ip)
                        if fqdn or custom_domain:
                            dns_data = make_canonical(f"{fqdn or ''}{custom_domain or ''}")

                    if dns_data:
                        self.log_debug(f"Targeting reverse record: {name} -> {dns_data}")
                        records.add(
                            DnsRecord(
                                name=name,
                                dns_type=PTR_TYPE,
                                data=dns_data,
                                ttl=get_ip_ttl(self.ip) or self.zone.default_ttl,
                                zone_name=self.zone.name,
                            )
                        )
                    else:
                        self.log_info(f"Skipping reverse for {ip}: no dns_name and no FQDN could be generated")
                        ignored_count += 1

        return records, ignored_count

    def load_pdns_records(self) -> tuple[set[DnsRecord], dict, set[DnsRecord]]:
        managed_records = set()
        unmanaged_records = set()
        exclude_records = {}
        checked_types = [PTR_TYPE] + list(FAMILY_TYPES.values())
        servers = self.get_pdns_servers_for_zone(self.zone.name)
        if not servers:
            raise PowerdnsSyncNoServers(f"No valid servers found for zone {self.zone}")
        for api_server in servers:
            pdns_zone = api_server.api.get_zone(make_canonical(self.zone.name))
            if not pdns_zone:
                raise PowerdnsSyncServerZoneMissing(
                    f"Zone {self.zone.name} not found on server {api_server}"
                )
            for record in pdns_zone.records:
                if record["type"] not in checked_types:
                    self.log_debug(
                        f"Skipping record {record['name']} because of type {record['type']}"
                    )
                    exclude_records[record['name']] = record['type']
                else:
                    self.log_debug(f"Processing record {record['name']}")
                    pdns_recs = DnsRecord.from_pdns_record(record, pdns_zone)
                    if has_managed_comment(record):
                        managed_records.update(pdns_recs)
                    else:
                        unmanaged_records.update(pdns_recs)

        return managed_records, exclude_records, unmanaged_records
