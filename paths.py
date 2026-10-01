"""資料檔存放位置。

Railway 的容器檔案系統每次重新部署都會被清空，因此購物、筆記、生日、記帳、
聊天室狀態等 JSON 檔要放在掛載的永久磁碟（Volume）。
在 Railway 把 Volume 掛到 /data，並設定環境變數 DATA_DIR=/data 即可；
沒設定時沿用程式所在資料夾（本機開發）。
"""
import os

_BASE = os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    d = os.environ.get("DATA_DIR", "").strip() or _BASE
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = _BASE
    return d


def data_path(filename: str) -> str:
    return os.path.join(data_dir(), filename)


def is_persistent() -> bool:
    """DATA_DIR 有設定且可寫入，才算資料會永久保存。"""
    d = os.environ.get("DATA_DIR", "").strip()
    return bool(d) and os.path.isdir(d) and os.access(d, os.W_OK)
