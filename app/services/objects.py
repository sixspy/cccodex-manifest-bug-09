from app.db import get_db, locked_write

# 拒因码：登记详情、批次卡片、对象列表、去重扫库台四处共用同一套口径
REASON_BATCH_NOT_FOUND = "BATCH_NOT_FOUND"
REASON_BATCH_CLOSED = "BATCH_CLOSED"
REASON_BATCH_VOID = "BATCH_VOID"
REASON_DUPLICATE_KEY = "DUPLICATE_KEY"
REASON_INVALID_INPUT = "INVALID_INPUT"

# 读态来源（拍板口径 A：提交瞬间读库内最新批次态，禁用开页快照态）
READ_SOURCE = "提交瞬间库内最新批次态"

# 拒因 → 短句（服务端一次拦截失败即吐此句）
REASON_MESSAGES = {
    REASON_BATCH_NOT_FOUND: "批次不存在",
    REASON_BATCH_CLOSED: "批次已闭合，拒绝登记新对象",
    REASON_BATCH_VOID: "批次已作废，拒绝登记新对象",
    REASON_DUPLICATE_KEY: "同批对象键已存在",
    REASON_INVALID_INPUT: "对象键与内容指纹不能为空",
}


class ServiceError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        reason: str | None = None,
        read_source: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason = reason
        self.read_source = read_source


def list_objects(batch_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM landing_objects WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def register_object(
    batch_id: int,
    object_key: str,
    content_hash: str,
    byte_size: int,
    db_path: str | None = None,
) -> dict:
    key = (object_key or "").strip()
    digest = (content_hash or "").strip()
    if not key or not digest:
        raise ServiceError(
            REASON_MESSAGES[REASON_INVALID_INPUT], 400, REASON_INVALID_INPUT
        )
    try:
        size = int(byte_size)
    except (TypeError, ValueError):
        raise ServiceError("字节数必须为整数", 400, REASON_INVALID_INPUT)
    if size < 0:
        raise ServiceError("字节数不能为负", 400, REASON_INVALID_INPUT)

    # 单一写锁 + 同事务读态：批次态与可登记门槛同源，禁止混用开页快照态
    with locked_write(db_path) as conn:
        batch = conn.execute(
            "SELECT id, status FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError(
                REASON_MESSAGES[REASON_BATCH_NOT_FOUND],
                404,
                REASON_BATCH_NOT_FOUND,
                READ_SOURCE,
            )
        # 钉死的唯一门槛：仅“收集中”可登记。已闭合仍写入即在此停。
        if batch["status"] == "已闭合":
            raise ServiceError(
                REASON_MESSAGES[REASON_BATCH_CLOSED],
                409,
                REASON_BATCH_CLOSED,
                READ_SOURCE,
            )
        if batch["status"] != "收集中":
            raise ServiceError(
                REASON_MESSAGES[REASON_BATCH_VOID],
                409,
                REASON_BATCH_VOID,
                READ_SOURCE,
            )
        exists = conn.execute(
            "SELECT id FROM landing_objects WHERE batch_id=? AND object_key=?",
            (batch_id, key),
        ).fetchone()
        if exists:
            raise ServiceError(
                REASON_MESSAGES[REASON_DUPLICATE_KEY],
                409,
                REASON_DUPLICATE_KEY,
                READ_SOURCE,
            )
        cur = conn.execute(
            "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
            "VALUES (?,?,?,?, '已登记')",
            (batch_id, key, digest, size),
        )
        row = conn.execute(
            "SELECT * FROM landing_objects WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
