# NetBox PowerDNS Sync - Architecture & Documentation

> Technical documentation for developers working on this plugin.

## Overview

This plugin synchronizes DNS records between NetBox and PowerDNS. It creates forward (A/AAAA) and reverse (PTR) DNS records based on IP addresses configured in NetBox.

### Key Concepts

- **Zone**: A DNS zone (e.g., `example.com.`) that defines which IP addresses should have DNS records created
- **ApiServer**: A PowerDNS API endpoint that the plugin communicates with
- **Naming Method**: A strategy for generating FQDNs from NetBox objects (IP, Device, VM, FHRP Group)
- **Sync Job**: A background task that compares NetBox records with PowerDNS and creates/deletes records as needed

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         NetBox                                   │
│  ┌─────────┐  ┌─────────┐  ┌─────────────┐  ┌───────────────┐   │
│  │ Devices │  │   VMs   │  │ FHRP Groups │  │  IP Addresses │   │
│  └────┬────┘  └────┬────┘  └──────┬──────┘  └───────┬───────┘   │
│       │            │              │                  │           │
│       └────────────┴──────────────┴──────────────────┘           │
│                              │                                   │
│                    ┌─────────▼─────────┐                         │
│                    │   Sync Job        │                         │
│                    │  (jobs.py)        │                         │
│                    └─────────┬─────────┘                         │
│                              │                                   │
│              ┌───────────────┼───────────────┐                   │
│              │               │               │                   │
│     ┌────────▼────────┐ ┌────▼────┐ ┌───────▼───────┐           │
│     │  Naming Methods │ │  Zone   │ │   ApiServer   │           │
│     │   (naming.py)   │ │ Matching│ │   Connection  │           │
│     └─────────────────┘ └─────────┘ └───────┬───────┘           │
└─────────────────────────────────────────────┼───────────────────┘
                                              │
                                    ┌─────────▼─────────┐
                                    │    PowerDNS API   │
                                    │   (REST API)      │
                                    └───────────────────┘
```

## Models

### ApiServer (`models.py`)

Represents a PowerDNS API endpoint.

| Field | Type | Description |
|-------|------|-------------|
| `name` | CharField | Unique name for the server |
| `api_url` | URLField | Base URL (e.g., `http://pdns:8081/api/v1`) |
| `api_token` | CharField | API authentication token |
| `enabled` | BooleanField | Whether this server is active |

**Important**: The `api_url` field has a unique constraint.

### Zone (`models.py`)

Represents a DNS zone to sync.

| Field | Type | Description |
|-------|------|-------------|
| `name` | CharField | Zone name with trailing dot (e.g., `example.com.`) |
| `enabled` | BooleanField | Whether sync is enabled |
| `api_servers` | ManyToManyField | PowerDNS servers to sync to |
| `default_ttl` | IntegerField | Default TTL for records |
| `is_default` | BooleanField | Use as fallback zone (only one allowed) |
| `naming_ip_method` | CharField | Class path for IP naming strategy |
| `naming_device_method` | CharField | Class path for Device/VM naming strategy |
| `naming_fgrpgroup_method` | CharField | Class path for FHRP Group naming strategy |
| `match_*` | Various | Tag/role matching for zone selection |

**Constraints**:
- Zone name must end with a dot (`.`)
- Only one zone can be `is_default=True`
- At least one naming method must be configured
- Reverse zones cannot be default

### Zone Matching

The plugin uses these criteria to match IPs to zones (in order):

1. `match_ipaddress_tags` - Tags on the IPAddress
2. `match_interface_tags` - Tags on the Interface/VMInterface
3. `match_device_tags` - Tags on the Device/VirtualMachine
4. `match_fhrpgroup_tags` - Tags on the FHRP Group
5. `match_device_roles` - Device/VM role
6. `is_default=True` - Fallback zone

## Naming Methods (`naming.py`)

Naming methods determine how FQDNs are generated from NetBox objects.

### Available Methods

| Class | Description | Example Output |
|-------|-------------|----------------|
| `NamingIpDnsName` | Use IPAddress.dns_name field | `server01.example.com.` |
| `NamingIpReverse` | Use PTR format (without arpa) | `1.0.168.192.` |
| `NamingDeviceName` | Device/VM name only | `server01.` |
| `NamingDeviceByInterface` | `interface.device` format | `eth0.server01.` |
| `NamingDeviceByInterfacePrimary` | `interface-device` for non-primary IPs | `eth0-server01.` |
| `NamingFGRPGroupName` | FHRP Group name | `vrrp-group1.` |

### How Naming Works

```python
# In naming.py
def generate_fqdn(ip: IPAddress, zone: Zone) -> str | None:
    for method_attr in ['naming_ip_method', 'naming_device_method', 'naming_fgrpgroup_method']:
        method = getattr(zone, method_attr, None)
        if method:
            klass = _load_class(method)  # Dynamic class loading
            if klass is None:
                continue  # IMPORTANT: Skip if class not found
            naming_method = klass(ip, zone)
            fqdn = naming_method.make_fqdn()
            if fqdn:
                return fqdn
    return None
```

**Important**: Methods are tried in order. First successful FQDN wins.

## Sync Jobs (`jobs.py`)

### Job Classes

- `PowerdnsTask` - Base class with logging and utilities
- `PowerdnsTaskIP` - Single IP sync (triggered by signals)
- `PowerdnsTaskFullSync` - Full zone sync

### Full Sync Flow

```
run_full_sync(job, zone_id)
    │
    ├── Validate zone exists and is enabled
    │
    ├── load_netbox_records()
    │   ├── get_addresses (property) - Find matching IPs
    │   ├── For each IP:
    │   │   ├── make_fqdn() - Generate FQDN using naming methods
    │   │   ├── Create forward record if zone matches
    │   │   └── Create reverse record if reverse zone
    │   └── Return set of DnsRecord objects
    │
    ├── load_pdns_records()
    │   ├── get_pdns_servers_for_zone() - Get enabled API servers
    │   ├── Fetch records from PowerDNS API
    │   └── Return set of DnsRecord objects
    │
    ├── Calculate diff:
    │   ├── to_delete = pdns_records - netbox_records
    │   └── to_create = netbox_records - pdns_records
    │
    └── Apply changes to PowerDNS
```

### Critical Code Paths

#### Getting API Servers for a Zone

```python
# jobs.py - CORRECT implementation
def get_pdns_servers_for_zone(self, zone_name: str) -> list[ApiServer]:
    if not zone_name:
        return []
    zone = Zone.objects.filter(name=zone_name).first()
    if not zone:
        return []
    # Use .filter(enabled=True) NOT .enabled()
    # ManyToManyField RelatedManagers don't inherit QuerySet methods
    return zone.api_servers.filter(enabled=True)
```

**WARNING**: Do NOT use `zone.api_servers.enabled()` - the `.enabled()` method from `EnabledQuerySet` is not available on ManyToMany RelatedManagers.

## Common Pitfalls & Constraints

### 1. ManyToMany QuerySet Methods

```python
# WRONG - Will fail with AttributeError
zone.api_servers.enabled().all()

# CORRECT
zone.api_servers.filter(enabled=True)
```

### 2. Zone Names Must End with Dot

```python
# WRONG
zone.name = "example.com"

# CORRECT
zone.name = "example.com."
```

### 3. Naming Methods Must Be Valid Class Paths

```python
# WRONG - Will silently fail
zone.naming_device_method = "device_name"

# CORRECT
zone.naming_device_method = "netbox_powerdns_sync.naming.NamingDeviceName"
```

### 4. FQDN Can Be None

Always check if `self.fqdn` is not None before using it:

```python
# WRONG
if self.forward_zone and self.forward_zone == self.zone:
    name = self.fqdn.replace(...)  # Fails if fqdn is None

# CORRECT
if self.forward_zone and self.forward_zone == self.zone and self.fqdn:
    name = self.fqdn.replace(...)
```

### 5. IPAddress.address Type

In NetBox v4, `IPAddress.address` can be a string during object creation. Always refresh from DB before accessing `.family` or `.version`:

```python
ip.refresh_from_db()
if ip.family == 4:
    # Safe to use
```

### 6. Job Enqueue Without Instance

In NetBox v4, jobs cannot be attached to plugin model instances by default:

```python
# WRONG - ValidationError
Job.enqueue(func, instance=zone, ...)

# CORRECT
Job.enqueue(func, zone_id=zone.pk, ...)
```

## Management Commands

### bootstrap_e2e

Creates test data for e2e testing.

```bash
# Empty mode (fresh database) - uses example.com
python manage.py bootstrap_e2e --empty --sync

# Data mode (existing data) - uses test-e2e.local
python manage.py bootstrap_e2e --sync

# With cleanup
python manage.py bootstrap_e2e --empty --cleanup --sync

# Dry run
python manage.py bootstrap_e2e --empty --dry-run -v 2
```

**Objects Created**:
- 3 Devices with interfaces and IPs
- 3 VMs with interfaces and IPs
- 3 standalone IPs with dns_name
- Zone with proper naming methods
- Link to existing API Server

## File Structure

```
netbox_powerdns_sync/
├── __init__.py          # PluginConfig
├── models.py            # ApiServer, Zone models
├── jobs.py              # Sync job classes (PowerdnsTaskFullSync, etc.)
├── naming.py            # FQDN generation strategies
├── signals.py           # Django signals for auto-sync
├── choices.py           # Naming method choices for forms
├── constants.py         # FAMILY_TYPES, PTR_TYPE, etc.
├── querysets.py         # EnabledQuerySet, ZoneQuerySet
├── record.py            # DnsRecord dataclass
├── utils.py             # Helper functions
├── validators.py        # Zone name validators
├── filtersets.py        # Django filter sets
├── tables.py            # Django tables
├── urls.py              # URL routing
├── navigation.py        # NetBox menu integration
├── forms/               # Django forms
├── views/               # Django views
├── api/                 # REST API serializers and views
├── templates/           # HTML templates
├── migrations/          # Database migrations
└── management/
    └── commands/
        └── bootstrap_e2e.py  # E2E test bootstrap command
```

## Debugging Tips

### Check Zone Configuration

```python
from netbox_powerdns_sync.models import Zone

zone = Zone.objects.first()
print(f"Zone: {zone.name}")
print(f"Enabled: {zone.enabled}")
print(f"API Servers: {list(zone.api_servers.filter(enabled=True))}")
print(f"Naming IP: {zone.naming_ip_method}")
print(f"Naming Device: {zone.naming_device_method}")
```

### Test FQDN Generation

```python
from ipam.models import IPAddress
from netbox_powerdns_sync.models import Zone
from netbox_powerdns_sync.naming import generate_fqdn

ip = IPAddress.objects.first()
zone = Zone.objects.first()
fqdn = generate_fqdn(ip, zone)
print(f"IP: {ip} -> FQDN: {fqdn}")
```

### Test API Server Connection

```python
from netbox_powerdns_sync.models import ApiServer

server = ApiServer.objects.first()
api = server.api
if api:
    zones = api.get_zones()
    print(f"PowerDNS zones: {[z.name for z in zones]}")
```

### Check Worker Logs

```bash
docker compose -p prod-dev-v4 logs -f netbox-worker
```

## NetBox v4 Migration Notes

Key changes from v3 to v4:

1. **Imports**: `extras.plugins` → `netbox.plugins`
2. **ContentType**: Now `ObjectType` from `core.models`
3. **count_related**: Moved to `utilities.query`
4. **normalize_querydict**: Moved to `utilities.querydict`
5. **get_plugin_config**: Moved to `netbox.plugins.utils`
6. **Cluster.site**: Removed - site is now on VM directly
7. **Job.enqueue**: Cannot use `instance` for plugin models

## Dependencies

- `python-powerdns>=2.1.0` - PowerDNS API client
- `django>=5.0` - Django framework (NetBox v4 requirement)
