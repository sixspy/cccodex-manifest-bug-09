import json

from app.db import get_db
from app.services.objects import (
    READ_SOURCE,
    REASON_BATCH_CLOSED,
    REASON_BATCH_VOID,
    REASON_MESSAGES,
)


def list_scans(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM dedup_scans ORDER BY id DESC"
        ).fetchall()
        return [_decode(dict(r)) for r in rows]


def get_scan(scan_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM dedup_scans WHERE id=?", (scan_id,)
        ).fetchone()
        return _decode(dict(row)) if row else None


def _decode(scan: dict) -> dict:
    try:
        scan["detail_json"] = json.loads(scan.get("detail") or "{}")
    except json.JSONDecodeError:
        scan["detail_json"] = {}
    return scan


def run_scan(as_of_date: str, db_path: str | None = None) -> dict:
    """去重扫库：清洗入库口径只认“收集中”批内的“已登记”行。

    - eligible：可进入清洗入库的行；跨批同指纹在 eligible 内聚组去重。
    - rejected：已闭合/已作废批里冒出的“已登记”脏行，拒因与登记接口完全同源
      （BATCH_CLOSED / BATCH_VOID + 同一读态来源），清洗口径绝不收这些行。
    护栏生效后闭合批无法再写入，复跑不会再吃到新的已登记脏行。
    """
    day = (as_of_date or "").strip() or "2026-11-16"
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT o.id AS object_id, o.batch_id, b.zone_name, b.status AS batch_status, "
            "o.object_key, o.content_hash, o.byte_size, o.status AS object_status "
            "FROM landing_objects o JOIN landing_batches b ON b.id = o.batch_id "
            "ORDER BY o.id"
        ).fetchall()

        eligible: list[dict] = []
        rejected: list[dict] = []
        for r in rows:
            item = dict(r)
            if item["object_status"] != "已登记":
                continue
            if item["batch_status"] == "收集中":
                eligible.append(item)
            else:
                reason = (
                    REASON_BATCH_CLOSED
                    if item["batch_status"] == "已闭合"
                    else REASON_BATCH_VOID
                )
                rejected.append(
                    {
                        "object_id": item["object_id"],
                        "batch_id": item["batch_id"],
                        "zone_name": item["zone_name"],
                        "object_key": item["object_key"],
                        "content_hash": item["content_hash"],
                        "reason": reason,
                        "message": REASON_MESSAGES[reason],
                        "read_source": READ_SOURCE,
                    }
                )

        # 跨批同内容指纹 = 清洗去重命中组（同批对象键已有 UNIQUE，不会重复）
        buckets: dict[str, list[dict]] = {}
        for item in eligible:
            buckets.setdefault(item["content_hash"], []).append(item)
        duplicate_groups = [
            {
                "content_hash": digest,
                "count": len(members),
                "members": [
                    {
                        "object_id": m["object_id"],
                        "batch_id": m["batch_id"],
                        "zone_name": m["zone_name"],
                        "object_key": m["object_key"],
                        "byte_size": m["byte_size"],
                    }
                    for m in members
                ],
            }
            for digest, members in buckets.items()
            if len(members) > 1
        ]

        detail_json = {
            "read_source": READ_SOURCE,
            "eligible": eligible,
            "rejected": rejected,
            "duplicate_groups": duplicate_groups,
        }
        detail_text = json.dumps(detail_json, ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO dedup_scans(as_of_date, eligible_count, duplicate_group_count, "
            "rejected_row_count, detail) VALUES (?,?,?,?,?)",
            (
                day,
                len(eligible),
                len(duplicate_groups),
                len(rejected),
                detail_text,
            ),
        )
        scan = conn.execute(
            "SELECT * FROM dedup_scans WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        result = dict(scan)
        result["detail_json"] = detail_json
        return result
