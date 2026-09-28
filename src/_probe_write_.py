import os, sys, io

BASE = r"Z:\【01】自研软件\【26-11】微博bot小助手（正式版）\src"
TARGET = os.path.join(BASE, "weibo_client.py")
PROBE_NEW = TARGET + ".probe"
PROBE_BAK = TARGET + ".bak2"

def log(msg):
    print(msg, flush=True)

# 1. read
try:
    with io.open(TARGET, "r", encoding="utf-8") as f:
        data = f.read()
    log("READ_OK len=%d" % len(data))
except Exception as e:
    log("READ_FAIL: %r" % e)
    data = None

# 2. write new file
try:
    if data is not None:
        with io.open(PROBE_NEW, "w", encoding="utf-8") as f:
            f.write(data)
    log("WRITE_NEW_OK")
except Exception as e:
    log("WRITE_NEW_FAIL: %r" % e)

# 3. overwrite existing (in place)
try:
    if data is not None:
        with io.open(TARGET, "w", encoding="utf-8") as f:
            f.write(data)
    log("OVERWRITE_OK")
except Exception as e:
    log("OVERWRITE_FAIL: %r" % e)

# 4. os.replace new -> target
try:
    if data is not None:
        with io.open(PROBE_BAK, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(PROBE_BAK, TARGET)
    log("REPLACE_OK")
except Exception as e:
    log("REPLACE_FAIL: %r" % e)

# 5. can we delete?
try:
    if os.path.exists(PROBE_NEW):
        os.remove(PROBE_NEW)
    log("DELETE_NEW_OK")
except Exception as e:
    log("DELETE_NEW_FAIL: %r" % e)

log("DONE")
