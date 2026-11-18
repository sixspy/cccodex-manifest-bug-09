from app.db import locked_write
from app.services.objects import ServiceError


def close_batch(batch_id: int, db_path: str | None = None) -> dict:
    # 与登记共用立即写锁：闭合持锁期间任何登记都排队，锁内读到“已闭合”即被拒
    with locked_write(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已闭合":
            raise ServiceError("批次已闭合", 409)
        if batch["status"] != "收集中":
            raise ServiceError("仅收集中批次可闭合", 409)
        objs = conn.execute(
            "SELECT id FROM landing_objects WHERE batch_id=? AND status='已登记'",
            (batch_id,),
        ).fetchall()
        if not objs:
            raise ServiceError("空批不可闭合", 400)
        n = len(objs)
        conn.execute(
            "UPDATE landing_batches SET status='已闭合', closed_object_count=? WHERE id=?",
            (n, batch_id),
        )
        conn.execute(
            "UPDATE landing_objects SET status='已闭合' WHERE batch_id=? AND status='已登记'",
            (batch_id,),
        )
        conn.execute(
            "INSERT INTO close_runs(batch_id, object_count, ok, detail) VALUES (?,?,1,?)",
            (batch_id, n, f"闭合 {n} 个对象"),
        )
        run = conn.execute(
            "SELECT * FROM close_runs WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return dict(run)
