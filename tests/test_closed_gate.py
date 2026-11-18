import os
import sqlite3
import threading

import pytest

from app.db import init_db
from app.services import close_batch, get_batch, list_objects, register_object, run_scan
from app.services.objects import READ_SOURCE, ServiceError


def _open_batch_id(authed, zone_code="bronze_orders"):
    items = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    return next(b["id"] for b in items if b["zone_code"] == zone_code)


def test_closed_batch_rejects_register_with_reason_and_source(authed):
    """已闭合批再登记：服务端一次拦截，409 + 拒因 + 读态来源，且无新行落库。"""
    bid = _open_batch_id(authed)
    ok = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/a.parquet", "content_hash": "h1", "byte_size": 10},
    )
    assert ok.status_code == 200
    assert authed.post(f"/api/batches/{bid}/close", json={}).status_code == 200

    before = authed.get(f"/api/batches/{bid}").json()["objects"]
    denied = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/evil.parquet", "content_hash": "h2", "byte_size": 11},
    )
    assert denied.status_code == 409
    payload = denied.json()
    assert payload["ok"] is False
    assert payload["reason"] == "BATCH_CLOSED"
    assert payload["read_source"] == READ_SOURCE
    assert "已闭合" in payload["error"]  # 失败吐短句

    after = authed.get(f"/api/batches/{bid}").json()["objects"]
    assert len(after) == len(before) == 1
    assert all(o["object_key"] != "raw/evil.parquet" for o in after)
    assert all(o["status"] == "已闭合" for o in after)


def test_detail_gate_uses_same_source_as_register(authed):
    """登记详情的门槛与登记接口同源：同读态来源、同拒因码。"""
    bid = _open_batch_id(authed)
    authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/a.parquet", "content_hash": "h1", "byte_size": 1},
    )
    authed.post(f"/api/batches/{bid}/close", json={})

    detail = authed.get(f"/api/batches/{bid}").json()
    gate = detail["register_gate"]
    assert gate["register_allowed"] is False
    assert gate["batch_status"] == "已闭合"
    assert gate["reason"] == "BATCH_CLOSED"
    assert gate["read_source"] == READ_SOURCE


def test_db_trigger_blocks_bypass_insert(tmp_path):
    """存储层兜底：绕开应用层直插已闭合批，触发器同样拒写。"""
    db = tmp_path / "t.db"
    init_db(str(db))
    with sqlite3.connect(str(db)) as conn:
        bid = conn.execute(
            "SELECT id FROM landing_batches WHERE status='已闭合'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,'已登记')",
                (bid, "bypass.parquet", "hx", 9),
            )


def test_concurrent_register_vs_close_no_double_write(tmp_path):
    """并发灌数抢闭合：写锁串行化后，闭合批内不得残留任何“已登记”新行。"""
    db = tmp_path / "race.db"
    init_db(str(db))
    with sqlite3.connect(str(db)) as conn:
        bid = conn.execute(
            "SELECT id FROM landing_batches WHERE zone_code='bronze_orders'"
        ).fetchone()[0]

    # 先放一个对象，保证批次非空可闭合
    register_object(bid, "seed.parquet", "h-seed", 1, db_path=str(db))

    errors: list[ServiceError] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(8)  # 7 个登记线程 + 1 个闭合线程同时放行

    def worker_register(i):
        barrier.wait()
        try:
            register_object(bid, f"r{i}.parquet", f"h{i}", i + 1, db_path=str(db))
        except ServiceError as exc:
            with errors_lock:
                errors.append(exc)

    def worker_close():
        barrier.wait()
        try:
            close_batch(bid, db_path=str(db))
        except ServiceError:
            pass

    threads = [threading.Thread(target=worker_register, args=(i,)) for i in range(7)]
    threads.append(threading.Thread(target=worker_close))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    batch = get_batch(bid, db_path=str(db))
    assert batch["status"] == "已闭合"
    objs = list_objects(bid, db_path=str(db))
    # 关键不变量：闭合后对象数固化，且没有任何“已登记”行残留（无并发双写）
    assert len(objs) == batch["closed_object_count"]
    assert all(o["status"] == "已闭合" for o in objs)
    # 闭合之后到达的登记必须带 BATCH_CLOSED 拒因（若全部登记先于闭合则该列表可为空）
    assert all(e.reason == "BATCH_CLOSED" for e in errors)


def test_scan_flags_dirty_rows_and_rerun_is_stable(tmp_path):
    """扫库台：脏行按登记同因拒收；护栏下复跑不会再吃到新已登记行。"""
    db = tmp_path / "scan.db"
    init_db(str(db))
    with sqlite3.connect(str(db)) as conn:
        closed_id = conn.execute(
            "SELECT id FROM landing_batches WHERE status='已闭合'"
        ).fetchone()[0]
        # 仅本测试内临时摘掉触发器，模拟护栏修复前漏进库的历史脏行
        conn.execute("DROP TRIGGER trg_landing_objects_gate")
        conn.execute(
            "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
            "VALUES (?,?,?,?,'已登记')",
            (closed_id, "legacy-dirty.parquet", "h-dirty", 3),
        )
        conn.execute(
            "CREATE TRIGGER trg_landing_objects_gate BEFORE INSERT ON landing_objects "
            "WHEN NEW.status='已登记' AND "
            "(SELECT status FROM landing_batches WHERE id = NEW.batch_id) != '收集中' "
            "BEGIN SELECT RAISE(ABORT, 'x'); END"
        )

    first = run_scan("2026-11-16", db_path=str(db))
    assert first["rejected_row_count"] == 1
    rejected = first["detail_json"]["rejected"][0]
    assert rejected["reason"] == "BATCH_CLOSED"  # 与登记接口同一拒因
    assert rejected["read_source"] == READ_SOURCE

    # 护栏在位：向该闭合批补写必然被拒
    with pytest.raises(ServiceError) as exc_info:
        register_object(closed_id, "new-dirty.parquet", "h9", 4, db_path=str(db))
    assert exc_info.value.reason == "BATCH_CLOSED"

    second = run_scan("2026-11-16", db_path=str(db))
    assert second["rejected_row_count"] == first["rejected_row_count"]
    assert second["detail_json"]["rejected"] == first["detail_json"]["rejected"]


def test_scan_dedup_groups_only_eligible_rows(tmp_path):
    """同指纹去重只统计可入库行（收集中批内的已登记行）。"""
    db = tmp_path / "dedup.db"
    init_db(str(db))
    with sqlite3.connect(str(db)) as conn:
        b1, b2 = conn.execute(
            "SELECT id FROM landing_batches WHERE zone_code IN ('bronze_orders','bronze_payments') "
            "ORDER BY id"
        ).fetchall()
        b1, b2 = b1[0], b2[0]
    register_object(b1, "a.parquet", "same-hash", 1, db_path=str(db))
    register_object(b2, "b.parquet", "same-hash", 1, db_path=str(db))
    register_object(b2, "c.parquet", "unique-hash", 1, db_path=str(db))

    scan = run_scan("2026-11-16", db_path=str(db))
    assert scan["eligible_count"] == 3
    assert scan["duplicate_group_count"] == 1
    assert scan["detail_json"]["duplicate_groups"][0]["content_hash"] == "same-hash"
    assert scan["rejected_row_count"] == 0
