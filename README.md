# plab-register

Email-account creation + PornoLab registration/activation, split out of
[pornolab-seeder](https://github.com/gauravsuman007/plab-seeder) so that
repo can stay a lean seeding app with no browser dependencies. This service
owns the only camoufox/patchright browser in the deployment.

Once a PornoLab account is registered (and, where the provider supports it,
activated), its credentials are handed to pornolab-seeder via
`POST {SEEDER_URL}/api/accounts/import`. pornolab-seeder remains the system
of record for accounts; this service keeps no long-term record of finished
accounts, only an in-flight "emails created but not yet registered" registry.

## Deploy

```bash
cp .env.example .env   # set APP_PASSWORD, SEEDER_URL, ANTHROPIC_API_KEY
docker compose up -d --build
```

UI: `http://<host>:8020` (configurable via `REGISTER_PORT`).

`SEEDER_URL` must point at wherever pornolab-seeder's API is actually
reachable from this host -- this service runs in its own compose project
with its own Tailscale sidecar, on a separate Docker network from
pornolab-seeder's stack, so a bare container-name hostname will not
resolve. The simplest choice is pornolab-seeder's published port on the
docker host's LAN IP (e.g. `http://192.168.2.100:8010`).

## Tailscale

Optional, same pattern as pornolab-seeder's own integration: log in and
select an exit node from the Settings UI to route this service's
registration traffic (camoufox + the registration HTTP calls) through it.
With no exit node selected, egress is the ordinary network.
