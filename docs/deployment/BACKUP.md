# Backups

| category | what | how |
| --- | --- | --- |
| **Critical, non-re-creatable** | human corrections and span annotations (`annotation_*`), dataset manifests (`training_dataset*`), model pedigree and qualification, and the database rows linking labels to evidence (segments, results, agreement) | nightly `pg_dump -Fc` (`deploy/linux/backup.sh`, `aerochorus-backup.timer`), with row counts and fingerprints (`counts.tsv`) and a `SHA256SUMS` |
| Expensive but re-creatable | ASR outputs (in the DB dump), raw CrispASR artifacts (`/srv/aerochorus/artifacts`), agreement | the dump covers results; artifacts are included with `AEROCHORUS_BACKUP_ARTIFACTS=1` (weekly is enough) |
| Re-creatable | model files (pinned by sha256; `worker models pull`), the CrispASR binary and CUDA 12 wheels (`crispasr.lock`), UI builds, caches | not backed up |
| Not AeroChorus's | collector source audio | never copied, never written |

```bash
sudo systemctl start aerochorus-backup.service     # run now
ls /srv/aerochorus/backups/                         # <stamp>/aerochorus.dump counts.tsv SHA256SUMS …
AEROCHORUS_BACKUP_KEEP_DAYS=30 deploy/linux/backup.sh
```

**Off-host copy:** a backup on the same disk protects against mistakes, not
disk loss. Copy `/srv/aerochorus/backups` elsewhere, for example with
`rsync -a /srv/aerochorus/backups/ <user>@<5080-box>:aerochorus-backups/`
from a user cron job. Never put backups on the collector's archive share.

**Restore** (the procedure is tested; see MIGRATION.md):

```bash
docker compose -f docker-compose.yml -f deploy/linux/compose.linux.yml up -d postgres
docker compose … cp <stamp>/aerochorus.dump postgres:/tmp/a.dump
docker compose … exec -T postgres pg_restore -U aerochorus -d aerochorus --clean --if-exists --no-owner /tmp/a.dump
docker compose … exec -T postgres psql -U aerochorus -d aerochorus -At -F $'\t' -f - < deploy/migrate/counts.sql | diff - <stamp>/counts.tsv
```
