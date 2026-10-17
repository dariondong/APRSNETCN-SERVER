"""进程入口：TCP SERVER + HTTP API + 清理线程。"""

from __future__ import annotations

import signal
import sys
import threading
import time

from . import api, auth, config, hub as hub_module
from .ingest import Ingest
from .store import Store


def bootstrap(store):
    """库里还没有名单时，按环境变量建第一条授权。只做一次。"""
    if store.accounts():
        return
    call = (config.BOOTSTRAP_CALL or "").upper().strip()
    if not call:
        return
    store.save_account(
        call,
        account=config.BOOTSTRAP_ACCOUNT or "",
        pass_hash=auth.hash_password(config.BOOTSTRAP_PASSWORD) if config.BOOTSTRAP_PASSWORD else "",
        status="authorized",
        note="bootstrap",
    )
    store.bump_whitelist_version()
    store.audit("bootstrap", {"call": call, "has_password": bool(config.BOOTSTRAP_PASSWORD)})


def self_check(store):
    """草案 7.4：起来先验数据，再把端口交出去。"""
    report = {"db": "unknown", "accounts": 0, "version": 0, "last_sync": None}
    try:
        report["db"] = store.quick_check()
    except Exception as error:                             # noqa: BLE001
        report["db"] = "failed: %s" % error
    report["accounts"] = len(store.accounts())
    report["version"] = store.whitelist_version()
    report["last_sync"] = store.meta("whitelist_synced_at")
    store.audit("startup-check", report)
    print("启动自检：库 %s，名单 %d 条、版本 %s、最近同步 %s"
          % (report["db"], report["accounts"], report["version"],
             report["last_sync"] or "无"), flush=True)
    return report


def purge_loop(store, stop):
    while not stop.is_set():
        try:
            removed = store.purge(config.RETENTION_HOURS)
            if removed:
                store.audit("purge", {"removed": removed,
                                      "retention_hours": config.RETENTION_HOURS})
        except Exception:                                  # noqa: BLE001
            pass
        stop.wait(config.PURGE_SECONDS)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    store = Store(config.DB_PATH)
    bootstrap(store)
    self_check(store)
    node = hub_module.Hub(store)
    node.start()
    feeder = Ingest(store, node)
    feeder.start()
    server, api_state = api.make_server(store, node, feeder)
    threading.Thread(target=server.serve_forever, name="api", daemon=True).start()
    stop = threading.Event()
    threading.Thread(target=purge_loop, args=(store, stop), name="purge",
                     daemon=True).start()
    ports = node.stats()["ports"]
    print("APRSNETCN SERVER %s 已启动：TCP %s，HTTP %s，API %s"
          % (config.PROTO_VERSION, ports, config.HTTP_PORT,
             "开" if config.ADMIN_PASSWORD else "关"), flush=True)
    if config.INGEST_ENABLED:
        print("外部数据：只读拉 %s:%d 的 %s"
              % (config.INGEST_HOST, config.INGEST_PORT, config.INGEST_FILTER),
              flush=True)

    def shutdown(signum, frame):                           # noqa: ARG001
        stop.set()
        feeder.stop()
        node.stop()
        server.shutdown()
        store.audit("shutdown", {"signal": signum})
        store.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
