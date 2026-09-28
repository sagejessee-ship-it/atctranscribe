# AeroChorus deploy bundle (Linux GPU host, containers only)

Made on the Windows PC by `deploy\linux\package.ps1`. The design is in
`docs/adr/0023-container-only-linux-deployment.md`, and the full guide is
`docs/deployment/LINUX_DEPLOYMENT_RUNBOOK.md` (both in the repository).

## First install

```bash
cd ~/aerochorus-deploy-<tag>
sudo bash host-setup.sh --stop-frigate  # once: driver check, Docker, NVIDIA Container Toolkit,
                                      # read-only CIFS mount of the archive, /srv/aerochorus
# log out and back in if host-setup added you to the docker group
bash deploy.sh up                     # load images, write config, start, smoke-test
```

`deploy.sh up` prints the review UI address: `http://<this host>:8080/review`.
Settings and secrets live in `/srv/aerochorus/config/aerochorus.env` (OpenSky,
OpenRouter). Edit that file, then run
`/srv/aerochorus/deploy/deploy.sh up` again.

Files copied from Windows usually lose their executable bit, which is why
the bundle's scripts are started with `bash`. The installed copies in
`/srv/aerochorus/deploy` are executable.

## Every update

Make a new bundle on the PC, copy it over, then:

```bash
cd ~/aerochorus-deploy-<new tag> && bash deploy.sh up
```

The previous kit stays in `/srv/aerochorus/deploy.prev`, and the previous images
stay loaded. To roll back, set `AEROCHORUS_TAG` to the old tag in
`aerochorus.env`, then run `/srv/aerochorus/deploy/deploy.sh compose up -d`.

## Daily use

| task | command |
| --- | --- |
| state, UI address | `/srv/aerochorus/deploy/deploy.sh status` |
| health checks | `… deploy.sh smoke` |
| logs | `… deploy.sh logs worker` (or `api`, `edge`, `adjudicator`, `backup`) |
| CLI | `aerochorus <command>`, e.g. `aerochorus airport airspace KBWI` |
| backup now | `… deploy.sh backup` (nightly anyway, into `/srv/aerochorus/backups`) |
| stop / start | `… deploy.sh down` / `… deploy.sh up` |
