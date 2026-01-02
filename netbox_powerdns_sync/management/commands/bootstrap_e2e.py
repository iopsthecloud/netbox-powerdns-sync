"""
Bootstrap command for PowerDNS Sync e2e tests.

Creates all necessary objects in NetBox to test PowerDNS synchronization.
Detects if running in "empty" mode (fresh database) or with existing data.

Usage:
    python manage.py bootstrap_e2e [--empty] [--sync] [--cleanup]

Options:
    --empty     Force empty mode (use example.com domain)
    --sync      Trigger a sync after creating objects
    --cleanup   Remove all test objects before creating new ones
"""

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.db import transaction

from dcim.models import Site, Manufacturer, DeviceRole, DeviceType, Device, Interface
from ipam.models import IPAddress
from virtualization.models import Cluster, ClusterType, VirtualMachine, VMInterface
from core.models import Job

from netbox_powerdns_sync.models import ApiServer, Zone
from netbox_powerdns_sync.jobs import PowerdnsTaskFullSync


class Command(BaseCommand):
    help = "Bootstrap NetBox with test data for PowerDNS Sync e2e tests"

    # Configuration for empty mode (example.com)
    EMPTY_CONFIG = {
        "domain": "example.com.",
        "prefix": "e2e",
        "network": "10.100.0",
        "api_server": {
            "name": "E2E PDNS Server",
            "api_url": "http://pdns-auth:8081/api/v1",
            "api_token": "secret",
        },
    }

    # Configuration for non-empty mode (different domain to avoid conflicts)
    DATA_CONFIG = {
        "domain": "test-e2e.local.",
        "prefix": "e2e-test",
        "network": "10.200.0",
        "api_server": {
            "name": "E2E Test PDNS",
            "api_url": "http://pdns-auth:8081/api/v1",
            "api_token": "secret",
        },
    }

    def add_arguments(self, parser):
        parser.add_argument(
            "--empty",
            action="store_true",
            help="Force empty mode (use example.com domain)",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Trigger a sync after creating objects",
        )
        parser.add_argument(
            "--cleanup",
            action="store_true",
            help="Remove all test objects before creating new ones",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be created without making changes",
        )

    def handle(self, *args, **options):
        self.verbosity = options.get("verbosity", 1)
        self.dry_run = options.get("dry_run", False)

        # Detect mode
        is_empty = options["empty"] or self._detect_empty_mode()
        config = self.EMPTY_CONFIG if is_empty else self.DATA_CONFIG

        mode_str = "EMPTY" if is_empty else "DATA"
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"PowerDNS Sync E2E Bootstrap - Mode: {mode_str}")
        self.stdout.write(f"Domain: {config['domain']}")
        self.stdout.write(f"{'='*60}\n")

        if self.dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - No changes will be made\n"))

        # Cleanup if requested
        if options["cleanup"]:
            self._cleanup(config)

        # Create objects
        with transaction.atomic():
            if self.dry_run:
                # Don't commit in dry-run mode
                transaction.set_rollback(True)

            api_server = self._create_api_server(config)
            zone = self._create_zone(config, api_server)
            site = self._create_site(config)
            manufacturer = self._create_manufacturer(config)
            device_role = self._create_device_role(config)
            device_type = self._create_device_type(config, manufacturer)

            # Create devices with IPs
            devices = self._create_devices(config, site, device_role, device_type)

            # Create VMs with IPs
            vms = self._create_vms(config, site)

            # Create standalone IPs with dns_name
            standalone_ips = self._create_standalone_ips(config)

        # Summary
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(self.style.SUCCESS("Bootstrap completed!"))
        self.stdout.write(f"{'='*60}")
        self.stdout.write(f"  Zone: {zone.name if not self.dry_run else config['domain']}")
        self.stdout.write(f"  Devices: {len(devices)}")
        self.stdout.write(f"  VMs: {len(vms)}")
        self.stdout.write(f"  Standalone IPs: {len(standalone_ips)}")

        # Trigger sync if requested
        if options["sync"] and not self.dry_run:
            self._trigger_sync(zone)

    def _detect_empty_mode(self):
        """Detect if database is mostly empty (fresh install)."""
        device_count = Device.objects.count()
        vm_count = VirtualMachine.objects.count()
        ip_count = IPAddress.objects.count()

        total = device_count + vm_count + ip_count
        is_empty = total < 10

        if self.verbosity >= 2:
            self.stdout.write(f"Detection: {device_count} devices, {vm_count} VMs, {ip_count} IPs")
            self.stdout.write(f"Mode detected: {'EMPTY' if is_empty else 'DATA'}")

        return is_empty

    def _cleanup(self, config):
        """Remove test objects created by previous runs."""
        prefix = config["prefix"]
        domain = config["domain"]

        self.stdout.write(self.style.WARNING(f"Cleaning up objects with prefix '{prefix}'..."))

        if self.dry_run:
            self.stdout.write("  Would delete test objects")
            return

        # Delete in reverse order of dependencies
        IPAddress.objects.filter(dns_name__endswith=domain.rstrip(".")).delete()
        VMInterface.objects.filter(virtual_machine__name__startswith=prefix).delete()
        VirtualMachine.objects.filter(name__startswith=prefix).delete()
        Interface.objects.filter(device__name__startswith=prefix).delete()
        Device.objects.filter(name__startswith=prefix).delete()
        DeviceType.objects.filter(model__startswith=prefix).delete()
        DeviceRole.objects.filter(name__startswith=prefix).delete()
        Manufacturer.objects.filter(name__startswith=prefix).delete()
        Cluster.objects.filter(name__startswith=prefix).delete()
        ClusterType.objects.filter(name__startswith=prefix).delete()
        Site.objects.filter(name__startswith=prefix).delete()
        Zone.objects.filter(name=domain).delete()
        ApiServer.objects.filter(name=config["api_server"]["name"]).delete()

        self.stdout.write(self.style.SUCCESS("  Cleanup completed"))

    def _create_api_server(self, config):
        """Create or get PowerDNS API Server."""
        api_config = config["api_server"]

        # First try to find by URL (unique constraint)
        try:
            api_server = ApiServer.objects.get(api_url=api_config["api_url"])
            self.stdout.write(f"  API Server: {api_server.name} (Found existing by URL)")
            return api_server
        except ApiServer.DoesNotExist:
            pass

        # Then try to find by name or create
        api_server, created = ApiServer.objects.get_or_create(
            name=api_config["name"],
            defaults={
                "api_url": api_config["api_url"],
                "api_token": api_config["api_token"],
                "enabled": True,
            },
        )

        status = "Created" if created else "Found existing"
        self.stdout.write(f"  API Server: {api_server.name} ({status})")
        return api_server

    def _create_zone(self, config, api_server):
        """Create or get DNS Zone with proper naming methods."""
        domain = config["domain"]

        zone, created = Zone.objects.get_or_create(
            name=domain,
            defaults={
                "description": f"E2E Test Zone for {domain}",
                "enabled": True,
                "default_ttl": 300,
                "naming_ip_method": "netbox_powerdns_sync.naming.NamingIpDnsName",
                "naming_device_method": "netbox_powerdns_sync.naming.NamingDeviceName",
                "naming_fgrpgroup_method": "netbox_powerdns_sync.naming.NamingFGRPGroupName",
            },
        )

        # Update naming methods if zone existed but was misconfigured
        if not created and not zone.naming_device_method:
            zone.naming_device_method = "netbox_powerdns_sync.naming.NamingDeviceName"
            zone.naming_ip_method = "netbox_powerdns_sync.naming.NamingIpDnsName"
            zone.save()
            self.stdout.write(f"  Zone: {zone.name} (Updated naming methods)")
        else:
            status = "Created" if created else "Found existing"
            self.stdout.write(f"  Zone: {zone.name} ({status})")

        # Link API server to zone
        if api_server not in zone.api_servers.all():
            zone.api_servers.add(api_server)
            self.stdout.write(f"    Linked to API Server: {api_server.name}")

        return zone

    def _create_site(self, config):
        """Create test site."""
        name = f"{config['prefix']}-site"
        site, created = Site.objects.get_or_create(
            name=name,
            defaults={"slug": name, "status": "active"},
        )
        if created and self.verbosity >= 2:
            self.stdout.write(f"  Site: {site.name}")
        return site

    def _create_manufacturer(self, config):
        """Create test manufacturer."""
        name = f"{config['prefix']}-manufacturer"
        manufacturer, created = Manufacturer.objects.get_or_create(
            name=name,
            defaults={"slug": name},
        )
        if created and self.verbosity >= 2:
            self.stdout.write(f"  Manufacturer: {manufacturer.name}")
        return manufacturer

    def _create_device_role(self, config):
        """Create test device role."""
        name = f"{config['prefix']}-role"
        role, created = DeviceRole.objects.get_or_create(
            name=name,
            defaults={"slug": name, "color": "4caf50"},
        )
        if created and self.verbosity >= 2:
            self.stdout.write(f"  DeviceRole: {role.name}")
        return role

    def _create_device_type(self, config, manufacturer):
        """Create test device type."""
        model = f"{config['prefix']}-server"
        device_type, created = DeviceType.objects.get_or_create(
            manufacturer=manufacturer,
            model=model,
            defaults={"slug": model},
        )
        if created and self.verbosity >= 2:
            self.stdout.write(f"  DeviceType: {device_type.model}")
        return device_type

    def _create_devices(self, config, site, role, device_type):
        """Create test devices with interfaces and IPs."""
        domain = config["domain"].rstrip(".")
        network = config["network"]
        prefix = config["prefix"]

        devices_config = [
            {"name": f"{prefix}-server01.{domain}", "ip": f"{network}.1/24", "iface": "eth0"},
            {"name": f"{prefix}-server02.{domain}", "ip": f"{network}.2/24", "iface": "eth0"},
            {"name": f"{prefix}-db01.{domain}", "ip": f"{network}.10/24", "iface": "ens192"},
        ]

        devices = []
        for dev_cfg in devices_config:
            device, created = Device.objects.get_or_create(
                name=dev_cfg["name"],
                defaults={
                    "site": site,
                    "role": role,
                    "device_type": device_type,
                    "status": "active",
                },
            )
            devices.append(device)

            # Create interface
            interface, _ = Interface.objects.get_or_create(
                device=device,
                name=dev_cfg["iface"],
                defaults={"type": "1000base-t"},
            )

            # Create IP and assign to interface
            ip, ip_created = IPAddress.objects.get_or_create(
                address=dev_cfg["ip"],
                defaults={
                    "status": "active",
                    "dns_name": dev_cfg["name"],
                },
            )
            if ip_created or ip.assigned_object != interface:
                ip.assigned_object = interface
                ip.save()

            # Refresh to get proper address object
            ip.refresh_from_db()

            # Set as primary IP (IPv4 only)
            if ip.family == 4 and device.primary_ip4_id != ip.pk:
                device.primary_ip4 = ip
                device.save()

            if self.verbosity >= 2:
                status = "Created" if created else "Found"
                self.stdout.write(f"  Device: {device.name} - {dev_cfg['ip']} ({status})")

        return devices

    def _create_vms(self, config, site):
        """Create test VMs with interfaces and IPs."""
        domain = config["domain"].rstrip(".")
        network = config["network"]
        prefix = config["prefix"]

        # Create cluster infrastructure
        cluster_type, _ = ClusterType.objects.get_or_create(
            name=f"{prefix}-cluster-type",
            defaults={"slug": f"{prefix}-cluster-type"},
        )
        cluster, _ = Cluster.objects.get_or_create(
            name=f"{prefix}-cluster",
            defaults={"type": cluster_type},
        )

        vms_config = [
            {"name": f"{prefix}-web01.{domain}", "ip": f"{network}.100/24", "iface": "eth0"},
            {"name": f"{prefix}-web02.{domain}", "ip": f"{network}.101/24", "iface": "eth0"},
            {"name": f"{prefix}-app01.{domain}", "ip": f"{network}.110/24", "iface": "ens18"},
        ]

        vms = []
        for vm_cfg in vms_config:
            vm, created = VirtualMachine.objects.get_or_create(
                name=vm_cfg["name"],
                defaults={
                    "cluster": cluster,
                    "site": site,
                    "status": "active",
                },
            )
            vms.append(vm)

            # Create VM interface
            vminterface, _ = VMInterface.objects.get_or_create(
                virtual_machine=vm,
                name=vm_cfg["iface"],
            )

            # Create IP and assign to VM interface
            ip, ip_created = IPAddress.objects.get_or_create(
                address=vm_cfg["ip"],
                defaults={
                    "status": "active",
                    "dns_name": vm_cfg["name"],
                },
            )
            if ip_created or ip.assigned_object != vminterface:
                ip.assigned_object = vminterface
                ip.save()

            # Refresh to get proper address object
            ip.refresh_from_db()

            # Set as primary IP (IPv4 only)
            if ip.family == 4 and vm.primary_ip4_id != ip.pk:
                vm.primary_ip4 = ip
                vm.save()

            if self.verbosity >= 2:
                status = "Created" if created else "Found"
                self.stdout.write(f"  VM: {vm.name} - {vm_cfg['ip']} ({status})")

        return vms

    def _create_standalone_ips(self, config):
        """Create standalone IPs with dns_name (not assigned to any device)."""
        domain = config["domain"].rstrip(".")
        network = config["network"]
        prefix = config["prefix"]

        ips_config = [
            {"dns_name": f"{prefix}-api.{domain}", "ip": f"{network}.200/24"},
            {"dns_name": f"{prefix}-cdn.{domain}", "ip": f"{network}.201/24"},
            {"dns_name": f"{prefix}-mail.{domain}", "ip": f"{network}.202/24"},
        ]

        ips = []
        for ip_cfg in ips_config:
            ip, created = IPAddress.objects.get_or_create(
                address=ip_cfg["ip"],
                defaults={
                    "status": "active",
                    "dns_name": ip_cfg["dns_name"],
                },
            )
            if not created and ip.dns_name != ip_cfg["dns_name"]:
                ip.dns_name = ip_cfg["dns_name"]
                ip.save()

            ips.append(ip)

            if self.verbosity >= 2:
                status = "Created" if created else "Found"
                self.stdout.write(f"  Standalone IP: {ip_cfg['dns_name']} - {ip_cfg['ip']} ({status})")

        return ips

    def _trigger_sync(self, zone):
        """Trigger a PowerDNS sync for the zone."""
        self.stdout.write(f"\nTriggering sync for zone: {zone.name}")

        User = get_user_model()
        user = User.objects.first()

        try:
            Job.enqueue(
                PowerdnsTaskFullSync.run_full_sync,
                name=f"E2E Bootstrap Sync - {zone.name}",
                user=user,
                zone_id=zone.pk,
            )
            self.stdout.write(self.style.SUCCESS("  Sync job enqueued! Check worker logs."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Failed to enqueue sync: {e}"))
