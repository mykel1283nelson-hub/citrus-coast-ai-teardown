"""Startup diagnostic: tests imports, writes results to diag.txt, serves HTTP."""
import sys
import os

lines = []
lines.append("sys.version=" + sys.version.replace("\n", " "))
lines.append("sys.executable=" + sys.executable)
lines.append("PORT=" + repr(os.environ.get("PORT")))
lines.append("cwd=" + os.getcwd())
lines.append("sys.path=" + os.pathsep.join(sys.path))

for mod in ["fastapi", "uvicorn", "httpx", "teardown"]:
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "?")
        lines.append(f"import {mod}: OK version={ver}")
    except Exception as e:
        lines.append(f"import {mod}: FAIL {type(e).__name__}: {e}")

# also try importing server (the real app)
try:
    import server
    lines.append("import server: OK")
except Exception as e:
    import traceback
    lines.append(f"import server: FAIL {type(e).__name__}: {e}")
    lines.append(traceback.format_exc().replace("\n", " | ")[:2000])

with open("diag.txt", "w") as f:
    f.write("\n".join(lines))

print("\n".join(lines), flush=True)

# serve the directory so the diag file is fetchable
port = int(os.environ.get("PORT", "10000"))
from http.server import HTTPServer, SimpleHTTPRequestHandler
HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()
