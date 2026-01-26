---
# Umbrel BCHN + Miningcore Backup & Restore Guide

**Date:** 2026-01-26
**Host:** umbrel (Linux 6.12.48+deb13-amd64)

## 1. Backup Procedure

**Backup directory and timestamp:**
```bash
BKDIR=/home/umbrel/umbrel/network/MyCloud-2DD6R9.local/Public/umbrel-backups
TS=$(date +%Y%m%d_%H%M%S)
```

**System Backup (configs + persistent data):**
```bash
tar --use-compress-program=zstd -cvf $BKDIR/umbrel_sysbackup_$TS.tar.zst \
    /home/umbrel/miningcore \
    /home/umbrel/loki \
    /etc/prometheus \
    /etc/systemd/system/bchn.service \
    /home/umbrel/bchnode/data/bitcoin.conf
```

**Miningcore PostgreSQL Database Backup:**
```bash
PGC=miningcore_postgres
OUT=$BKDIR/miningcore_postgres_$TS.sql.zst
sudo docker exec -t $PGC pg_dumpall -U postgres | zstd -T0 -9 -o $OUT
```

**Miningcore internal DB (if using container user `miningcore`):**
```bash
PGC=miningcore_postgres
OUT=$BKDIR/miningcore_db_$TS.sql.zst
sudo docker exec -t $PGC pg_dumpall -U miningcore | zstd -T0 -9 -o $OUT
```

**Verify backup size and integrity:**
```bash
ls -lah $BKDIR
zstd -t $OUT  # test zstd compressed DB
```

## 2. Restore Procedure

**Extract full system backup:**
```bash
cd /home/umbrel/umbrel/network/MyCloud-2DD6R9.local/Public/umbrel-backups
tar -I zstd -xvf umbrel_sysbackup_<TS>.tar.zst -C /
```

**Restore PostgreSQL databases:**
```bash
PGC=miningcore_postgres
zstd -dc umbrel-backups/miningcore_postgres_<TS>.sql.zst | sudo docker exec -i $PGC psql -U postgres
```

**Notes:**
- Always stop containers before restoring data.
- After restore, restart services:
```bash
sudo systemctl restart bchn
sudo docker restart miningcore miningcore_postgres loki promtail
```

## 3. Disk / Filesystem Checks

**Check mounted partitions (read-only fsck recommended on unmounted disks):**
```bash
sudo fsck.ext4 -n /dev/sda2
sudo fsck.ext4 -n /dev/sda4
sudo fsck.vfat -n /dev/sda1
```

**SMART health check:**
```bash
sudo smartctl -a /dev/sda | egrep 'SMART overall|Model|Serial'
```

**Disk usage:**
```bash
sudo du -xh /var/lib/docker | sort -h | tail -n 20
sudo du -xh /home/umbrel/umbrel/app-data | sort -h | tail -n 20
```

## 4. Docker / Service Info

**Running containers and ports:**
- bchn_node_exporter (4ops/bitcoin-exporter:stable) 127.0.0.1:9133
- promtail (grafana/promtail:3.0.0) 0.0.0.0:9080
- loki (grafana/loki:3.0.0) 0.0.0.0:3100
- miningcore (theretromike/miningcore:latest)
- miningcore_postgres (postgres:16) 127.0.0.1:5432
- grafana_web_1 (grafana/grafana:12.3.1) 3000/tcp

**Compose files:**
- /home/umbrel/miningcore/docker-compose.yml
- /home/umbrel/loki/docker-compose.yml
- /home/umbrel/loki/promtail/promtail.yaml

**BCHN node service:** enabled, active

## 5. Observations / Gotchas

- PCIe Bus Errors (rtw_8821ce) were correctable.
- FAT sda1 dirty bit cleared automatically.
- EXT4 partitions sda2 and sda4 clean.
- No OOM errors observed during crash.
- Loki/Alloy logs wiped on crash; backups now exist.

---
**End of document**

