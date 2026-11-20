# cvmfs-infrastructure

Configuration-as-code for the CernVM-FS **Stratum 1** servers that replicate
`neurodesk.ardc.edu.au`. Managed with Ansible.

## Layout

| Path | Purpose |
|---|---|
| `inventory/hosts.yml` | The Stratum 1 servers |
| `inventory/group_vars/stratum1/main.yml` | Shared settings (repository, Stratum 0, schedules, ports) |
| `inventory/group_vars/stratum1/vault.yml` | Secrets, **ansible-vault encrypted** (see `vault.yml.example`) |
| `roles/common` | EPEL, fail2ban, chrony, monit (disk-space alerts by e-mail) |
| `roles/cvmfs_stratum1` | cvmfs-server, replica creation, snapshot + GC cron, httpd on `127.0.0.1:8080` |
| `roles/squid` | squid reverse proxy on ports 80/8000 in front of httpd, log rotation |
| `roles/access_stats` | Public, privacy-preserving usage statistics at `http://<server>/stats/` |

Architecture of every server:

```
clients / site proxies ──► squid :80,:8000 (accel, access log = source of stats)
                              └──► httpd 127.0.0.1:8080 ──► /srv/cvmfs/<repo>
                                                        └──► geo API (mod_wsgi)
```

## Usage

```bash
pip install ansible-core
# dry run against one server: shows what would change, changes nothing
ansible-playbook site.yml --limit stratum1-brisbane --check --diff --ask-vault-pass
# apply
ansible-playbook site.yml --limit stratum1-brisbane --ask-vault-pass
# roll out only the statistics to every server
ansible-playbook site.yml --tags access_stats --ask-vault-pass
```

Adding a new Stratum 1: add it to `inventory/hosts.yml` and run the playbook
for that host. The first run creates the replica and starts the initial
snapshot in the background (this can take many hours).

## Usage statistics (`roles/access_stats`)

`cvmfs-access-stats` runs nightly from a systemd timer (idle CPU/IO priority,
sandboxed). It parses squid's access log incrementally and counts downloads of
each container's nested catalog (`/containers/<tool>_<version>_<date>`) as a
"container open". Catalog hashes are resolved from the `root_prefix` property of
the catalog objects on local disk.

Privacy guarantees of the published page (`/stats/index.html`, `/stats/stats.json`):

* Raw IP addresses are never written to disk. Each client is reduced on
  arrival to continent + country and a salted HMAC (used only to count distinct
  clients; secret and hashes stay in `/var/lib/cvmfs-access-stats`, mode 0700,
  hashes purged after 400 days).
* Only continent/country aggregates are published; no cities, IPs, host names
  or hashes.
* k-anonymity: any published group with fewer than `access_stats_min_networks`
  (default 3) distinct clients is merged into "Other countries in …" /
  "Undisclosed".
* Geolocation: DB-IP City Lite as shipped/updated by cvmfs-server
  (CC BY 4.0, attribution on the page).

Any change to the report must keep these guarantees. Before merging, generate
the report and check it, e.g. `grep -E '([0-9]{1,3}\.){3}[0-9]{1,3}'` should
only match container version numbers.

## Secrets

Never commit plaintext secrets. CI rejects a `vault.yml` that is not
ansible-vault encrypted. Currently required: `vault_monit_smtp_password`.
