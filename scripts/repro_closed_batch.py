#!/usr/bin/env python3
"""拒登复现单（独立可跑，不依赖 pytest）。

用法：
  python -I scripts/repro_closed_batch.py            # 用临时库跑完整复现
  python -I scripts/repro_closed_batch.py --db PATH  # 指定 SQLite 库路径

复现链：建开放批 → 登记 1 个对象 → 闭合 → 再登记（期望被一次拦截），
并额外验证存储层触发器对绕路直插的兜底，以及去重扫库复跑口径稳定。
"""
import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.services import close_batch, list_objects, register_object, run_scan  # noqa: E402
from app.services.objects import READ_SOURCE, ServiceError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="已闭合批拒登复现单")
    parser.add_argument("--db", help="SQLite 库路径；不传则用临时库")
    args = parser.parse_args()

    tmp_dir = None
    if args.db:
        db_path = args.db
    else:
        tmp_dir = tempfile.mkdtemp(prefix="repro-closed-")
        db_path = str(Path(tmp_dir) / "repro.db")
    init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        bid = conn.execute(
            "SELECT id FROM landing_batches WHERE zone_code='bronze_orders'"
        ).fetchone()[0]

    print(f"[1] 目标批次 id={bid}，读态口径：{READ_SOURCE}")

    obj = register_object(bid, "raw/a.parquet", "hash-a", 10, db_path=db_path)
    print(f"[2] 开放批登记成功：id={obj['id']} status={obj['status']}")

    run = close_batch(bid, db_path=db_path)
    print(f"[3] 清单闭合：object_count={run['object_count']}")

    print("[4] 闭合后再次登记，期望 409 拒登：")
    try:
        register_object(bid, "raw/evil.parquet", "hash-evil", 11, db_path=db_path)
    except ServiceError as exc:
        print(f"    拦截成功 status={exc.status_code} reason={exc.reason} "
              f"read_source={exc.read_source} error={exc.message}")
        if exc.status_code != 409 or exc.reason != "BATCH_CLOSED":
            print("    失败：拒因码或状态码不符")
            return 1
    else:
        print("    失败：已闭合批竟然写入成功，清洗入库口径已崩")
        return 1

    objs = list_objects(bid, db_path=db_path)
    if any(o["object_key"] == "raw/evil.parquet" for o in objs):
        print("[5] 失败：对象列表出现已登记脏行")
        return 1
    print(f"[5] 对象列表干净：共 {len(objs)} 行，状态均为 {objs[0]['status']}")

    print("[6] 触发器兜底（绕开应用层直插）：")
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,'已登记')",
                (bid, "bypass.parquet", "hx", 9),
            )
    except sqlite3.IntegrityError as exc:
        print(f"    存储层拒写成功：{exc}")
    else:
        print("    失败：触发器未拦住绕路直插")
        return 1

    s1 = run_scan("2026-11-16", db_path=db_path)
    s2 = run_scan("2026-11-16", db_path=db_path)
    if s1["rejected_row_count"] != s2["rejected_row_count"]:
        print("[7] 失败：扫库复跑拒登行数变化，复跑吃到了新脏行")
        return 1
    print(f"[7] 扫库复跑稳定：两次拒登脏行均为 {s1['rejected_row_count']} 行")

    print("PASS：已闭合批拒登复现单全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
