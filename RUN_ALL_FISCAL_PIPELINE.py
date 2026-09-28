from __future__ import annotations
import subprocess, sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
cmd=[sys.executable,'-u',str(HERE/'03_run_annual_refresh.py'),*sys.argv[1:]]
raise SystemExit(subprocess.call(cmd,cwd=HERE))
