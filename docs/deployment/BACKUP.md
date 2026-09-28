# Backups

| category | what | how |
| --- | --- | --- |
| **Critical, non-re-creatable** | human corrections and span annotations (`annotation_*`), dataset manifests (`training_dataset*`), model pedigree and qualification, and the database rows linking labels to evidence (segments, results, agreement); the settings in `/srv/aerochorus/config` | the `backup` container (`deploy/linux/backup.sh`) runs a nightly `pg_dump -Fc` with row counts and fingerprints (`counts.tsv`), the config and dataset exports (`config-exports.tgz`), and a `SHA256SUMS` |
| Expensive but re-creatable | ASR outputs (in the DB dump), raw CrispASR artifacts (`/srv/aerochorus/artifacts`), agreement | the dump covers results. Copy the artifacts directory off-host now and then (rsync). |
| Re-creatable | model files (pinned by sha256; `aerochorus worker models pull`), container images (rebuilt from the repository; the bundle is on the PC), caches | not backed up |
| Not AeroChorus's | collector source audio | never copied, never written |

The job runs daily at `AEROCHORUS_BACKUP_HOUR` (03:00 local by default) and
keeps `AEROCHORUS_BACKUP_KEEP_DAYS` days (14). Both are set in
`/srv/aerochorus/config/aerochorus.env`.

```bash
/srv/aerochorus/deploy/deploy.sh backup       # one now
ls /srv/aerochorus/backups/                    # <stamp>/aerochorus.dump counts.tsv config-exports.tgz SHA256SUMS
/srv/aerochorus/deploy/deploy.sh logs backup   # last runs
```

The backup includes `aerochorus.env`, which holds secrets, so the directory is
`chmod 700`. Treat off-host copies the same way.

**Off-host copy:** a backup on the same disk protects against mistakes, not
disk loss. Copy `/srv/aerochorus/backups` elsewhere, for example with
`rsync -a /srv/aerochorus/backups/ <user>@<5080-box>:aerochorus-backups/`
from a user cron job. Never put backups on the collector's archive share.

**Restore** (the same procedure as the migration import, which is tested; see
MIGRATION.md): `import.sh` accepts a backup directory. It recreates the
database only when the current one has no segments. To replace a live
database deliberately, stop everything and move `/srv/aerochorus/postgres`
aside first:

```bash
D=/srv/aerochorus/deploy/deploy.sh
$D down && sudo mv /srv/aerochorus/postgres /srv/aerochorus/postgres.old && mkdir /srv/aerochorus/postgres
$D up                                             # empty database at head
/srv/aerochorus/deploy/import.sh /srv/aerochorus/backups/<stamp>   # restore + verify counts/fingerprints
```
