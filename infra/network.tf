// VPC, private connectivity, and the DNS that makes both halves resolvable.
//
// The single most common failure after a clean deploy is DNS, in one of two
// directions, and both are configured here:
//
//   1. The container must resolve *.intra.chrysler.com through the corporate
//      resolvers reached over the interconnect - that is the forwarding zone.
//   2. It must also resolve googleapis.com to restricted VIPs without leaving
//      the VPC - that is Private Google Access plus the private-googleapis zone.
//
// Getting one right and not the other produces a service that "works" from the
// console and times out from the application.

resource "google_compute_network" "vpc" {
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "GLOBAL"
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "run" {
  name          = "${local.prefix}-run"
  ip_cidr_range = var.subnet_cidr
  region        = var.region
  network       = google_compute_network.vpc.id

  // Lets the service reach Vertex AI, Secret Manager and Pub/Sub over Google's
  // network rather than the public internet, so no Cloud NAT and no egress to
  // 0.0.0.0/0 is required.
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

// ---------------------------------------------------------------- Cloud SQL
// Private Service Access: Cloud SQL is allocated a private IP from this range
// inside Google's producer network, peered to our VPC.
resource "google_compute_global_address" "psa" {
  name          = "${local.prefix}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = split("/", var.psa_cidr)[0]
  prefix_length = tonumber(split("/", var.psa_cidr)[1])
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa.name]

  // Without this a `terraform destroy` hangs on the peering.
  deletion_policy = "ABANDON"
}

// Cloud SQL lives behind a peering, so its routes must be exported for the
// on-premises side to reach it and for return traffic to work.
resource "google_compute_network_peering_routes_config" "psa" {
  peering              = google_service_networking_connection.psa.peering
  network              = google_compute_network.vpc.name
  import_custom_routes = true
  export_custom_routes = true
}

// ------------------------------------------------------------------ hybrid
// The Cloud Router carrying the Interconnect or HA VPN is created by the
// network team; this only advertises our ranges over it so the corporate side
// knows how to route back.
data "google_compute_router" "hybrid" {
  count   = var.interconnect_router_name == "" ? 0 : 1
  name    = var.interconnect_router_name
  region  = var.region
  network = google_compute_network.vpc.name
}

// ------------------------------------------------------------------- DNS
// Forward the corporate domain to the on-premises resolvers over the circuit.
resource "google_dns_managed_zone" "corporate" {
  count       = length(var.corporate_dns_servers) == 0 ? 0 : 1
  name        = "${local.prefix}-corporate"
  dns_name    = var.corporate_dns_suffix
  description = "Forwards ALM hostnames to the corporate resolvers over the interconnect."
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc.id
    }
  }

  forwarding_config {
    dynamic "target_name_servers" {
      for_each = var.corporate_dns_servers
      content {
        ipv4_address    = target_name_servers.value
        forwarding_path = "PRIVATE" // route over the interconnect, not the internet
      }
    }
  }

  depends_on = [google_project_service.required]
}

// Resolve Google APIs to the restricted VIP so traffic never leaves the VPC.
resource "google_dns_managed_zone" "google_apis" {
  name       = "${local.prefix}-private-googleapis"
  dns_name   = "googleapis.com."
  visibility = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc.id
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_dns_record_set" "restricted_a" {
  name         = "restricted.googleapis.com."
  managed_zone = google_dns_managed_zone.google_apis.name
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "google_apis_cname" {
  name         = "*.googleapis.com."
  managed_zone = google_dns_managed_zone.google_apis.name
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

resource "google_compute_route" "restricted_vip" {
  name             = "${local.prefix}-restricted-googleapis"
  network          = google_compute_network.vpc.name
  dest_range       = "199.36.153.4/30"
  next_hop_gateway = "default-internet-gateway"
  priority         = 100
}

// ---------------------------------------------------------------- firewall
// Default-deny egress, with the two destinations this workload legitimately
// needs. An autonomous agent with unrestricted egress is a data-exfiltration
// path; this makes that a policy decision rather than an oversight.
resource "google_compute_firewall" "deny_egress" {
  name               = "${local.prefix}-deny-egress"
  network            = google_compute_network.vpc.name
  direction          = "EGRESS"
  priority           = 65000
  destination_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }
}

resource "google_compute_firewall" "allow_google_apis" {
  name               = "${local.prefix}-allow-googleapis"
  network            = google_compute_network.vpc.name
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["199.36.153.4/30"]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "allow_corporate" {
  name               = "${local.prefix}-allow-corporate"
  network            = google_compute_network.vpc.name
  direction          = "EGRESS"
  priority           = 1000
  // RFC1918 covers the corporate estate reached over the interconnect and the
  // Cloud SQL private IP. Narrow this to the actual EWM/JTS/LDAP prefixes once
  // the network team confirms them.
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

  allow {
    protocol = "tcp"
    ports    = ["443", "5432", "389", "636"]
  }
}
