# PostgreSQL

A PostgreSQL 17 database server at `<project>.<domain>`. Each install is a separate server in its own project, so you can add the plugin as many times as you need (for example `pg-shop` and `pg-blog`). Each one gets its own hostname and its own data folder.

## Connecting

PostgreSQL is not HTTP, so freeholdy routes it through nginx's **stream** (raw TCP) module rather than a website. During the install you pick how clients reach the server.

| Mode | Address | Clients | TLS |
|---|---|---|---|
| **sni** | `<project>.<domain>:5432` | libpq / psql **17+** with `sslnegotiation=direct` | Let's Encrypt cert, terminated by nginx |
| **port** | `<project>.<domain>:<your port>` | any PostgreSQL client | postgres's own self-signed cert (`sslmode=require`) |
| **both** | both of the above | | |

**sni** lets every database share port 5432: nginx reads the hostname from the TLS handshake and forwards the connection to the matching project. Only clients that start with TLS directly send that hostname. That means PostgreSQL 17+ libpq with `sslnegotiation=direct`, plus drivers built on it. Older clients (psql 16 and earlier, psycopg2 on an old libpq, many JDBC/ORM setups) open with a plaintext `SSLRequest` that nginx cannot route. Those clients need **port** mode.

```bash
# sni (psql 17+)
psql "host=pg-shop.example.com port=5432 dbname=shop user=shop sslmode=verify-full sslnegotiation=direct sslrootcert=system"

# port (any client)
psql "host=pg-shop.example.com port=15432 dbname=shop user=shop sslmode=require"
```

The exact connection strings are saved to `postgresql-credentials` (mode 600) in the project directory once the server is up.

**Firewall:** open TCP 5432 (sni) and/or your chosen port in your cloud firewall and in ufw, if it is enabled. **DNS:** `<project>.<domain>` must resolve to this server. The Let's Encrypt certificate that sni mode depends on is issued over HTTP on port 80, exactly as for any other project.

**Server prerequisite:** nginx's stream module must be wired in. `install.sh` and `update.sh` do this automatically. If it was skipped, run `sudo bash scripts/setup_nginx_stream.sh` from the freeholdy directory. You need nginx 1.21.4 or newer.

## Data

The cluster lives on the host in **`<freeholdy data dir>/postgresql/<project>`**, which is mounted into the container as the docker volume `<project>_data`. The volume list, download/upload and backups in freeholdy all work on it.

- **Deleting the project keeps the folder.** Removing the docker volume does not delete the files behind a bind mount. To delete the data for good, run `sudo rm -rf <data dir>/postgresql/<project>`.
- Adding the plugin again **under the same project name** finds the old folder and asks whether to keep the cluster, with its databases, user and password, or wipe it.
- A volume download or backup copies the files of a **running** cluster. That copy is not guaranteed to be consistent. For a reliable logical backup, use `pg_dump`:
  `docker exec freeholdy_<project>_db pg_dump -U <user> <db> > dump.sql`
- **Major-version upgrades** (17 → 18) need a dump and restore. Do not change the image tag in place, because the data folder format differs between majors.

## Notes

- `POSTGRES_DB/USER/PASSWORD` only take effect the first time the server starts with an empty data folder. To change the password later, use SQL: `ALTER USER … PASSWORD …`.
- Postgres sees connections coming from nginx, not from the real client IP.
- Port mode also accepts unencrypted connections. Always use `sslmode=require` or stronger.
- If nginx crashes (rather than stopping cleanly), it can leave stale sockets in `/var/lib/freeholdy-nginx/`. Remove `*.sock` there if nginx refuses to start.
