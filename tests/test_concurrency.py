import threading

from interleave.clock import RealClock
from interleave.normalize import normalize
from interleave.store import OperationStore, connect


def test_one_operation_per_key_under_concurrency(tmp_path, base_request):
    db_path = str(tmp_path / "conc.db")
    connect(db_path).close()
    results = []
    errors = []

    def worker():
        conn = connect(db_path)
        store = OperationStore(conn, RealClock())
        try:
            res = store.get_or_create(base_request, normalize(base_request))
            results.append(res.operation.id)
        except Exception as exc:  # noqa: BLE001 - test needs to see any failure
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(set(results)) == 1
