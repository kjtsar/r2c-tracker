# Database Host Hardening Status

Observed August 7, 2026 in Google Cloud project `shaped-splicer-482602-v1`.

## Confirmed current state

- Database VM: `instance-20260104-171736`, zone `us-west1-b`, private address `10.138.0.2`, public address `35.212.177.190`.
- PostgreSQL TCP 5433 is restricted by firewall to the Cloud Run connector subnet `172.20.0.0/26`; public probes to 5432 and 5433 timed out.
- Public SSH TCP 22 is reachable. Public HTTP/HTTPS/RDP probes did not establish a service, although broad tag-based HTTP/HTTPS firewall rules still apply to the VM.
- The VM originally had tags `http-server`, `https-server`, and `r2c-pilot-db`; the unused public-web tags have now been removed.
- Secure Boot is disabled. Deletion protection is now enabled. No snapshot schedule was found during the review.
- The subnet does not currently provide Cloud NAT and Private Google Access is disabled. Removing the public IP without replacing outbound connectivity would impair package/agent access.
- No monitoring notification channel, alert policy, or custom log metric is configured in the project.

## Additive controls completed

- Identity-Aware Proxy API enabled.
- Interim administrator granted IAP tunnel access and OS Admin Login.
- Firewall rule `allow-iap-ssh-r2c-db` permits TCP 22 only from Google's IAP range `35.235.240.0/20` to the `r2c-pilot-db` tag, with logging enabled.
- Firewall rule `deny-public-rdp-r2c-db` explicitly denies and logs TCP 3389 from `0.0.0.0/0` to the database host.
- Unused `http-server` and `https-server` tags were removed; only `r2c-pilot-db` remains.
- VM deletion protection was enabled without stopping the instance, and the public tracker health page remained available afterward.
- Instance metadata blocks project-wide SSH keys. OS Login and an instance-specific metadata-key fallback were attempted, but the guest rejected both.
- IAP/network troubleshooting confirmed the cloud-side tunnel path and permissions are available.
- `scripts/cloud_security_inventory.sh` provides a repeatable, read-only export of VM, firewall, IAM, monitoring, and log-metric configuration.

## Maintenance-window change still required

Do not block public SSH until the alternate path is proven. The guest OS is not consuming either OS Login or instance metadata keys. Repair requires a brief, controlled restart using a temporary startup recovery script (or equivalent console procedure) to restore the guest agent/SSH configuration.

After successful IAP login:

1. Confirm OS version/patch state, running/listening services, local accounts/sudo, SSH daemon policy, guest agent, PostgreSQL bind addresses, and disk state.
2. Add a higher-priority deny for public SSH or remove the broad allow rule after confirming IAP access. The public RDP deny is already active.
3. Plan Cloud NAT or another controlled egress path, then remove the VM public IP.
4. Evaluate Secure Boot compatibility separately; do not enable it without a rollback plan.
5. Establish encrypted snapshot/backup separation and perform an isolated restoration test.
6. Configure alerts and test delivery to two program-operator-controlled responders.

## Stop condition

If IAP access cannot be proven after repair, restore the prior metadata state and retain the existing public path temporarily rather than locking out recovery. Do not expose PostgreSQL more broadly as a workaround.
