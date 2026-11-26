import os
from app.factory import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("端口", "8500"))
    # hook-05：双会话并发脚本要同时打「自动重投 / 作废」，必须多线程受理，
    # 串行化交给 sqlite 同锁 + 争用闸 CAS，而不是靠服务器单线程排队。
    app.run(host="0.0.0.0", port=port, threaded=True)
