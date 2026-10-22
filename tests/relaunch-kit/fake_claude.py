"""Stand-in for the `claude` binary, driven by a JSON script.

Run it through a stub executable (see conftest.py). Environment:
  FAKE_CLAUDE_STATE   directory holding calls.jsonl and a call counter
  FAKE_CLAUDE_SCRIPT  path of a JSON list; entry N drives call N

Every call appends {argv, cwd, token, dir, shim} to calls.jsonl. A script entry
may hold:
  sleep    seconds to wait before acting
  request  object merged over the default token file and written to
           $RELAUNCH_KIT_DIR/$RELAUNCH_KIT_TOKEN.json (the value "<drop>" removes a key;
           "age_s" back-dates written_at; "raw" writes that text verbatim)
  exit     exit status (default 0)
A call past the end of the script exits 99 and says so on stderr.
"""

import datetime
import json
import os
import sys
import time

state = os.environ["FAKE_CLAUDE_STATE"]
counter = os.path.join(state, "count")
try:
    with open(counter) as handle:
        index = int(handle.read())
except FileNotFoundError:
    index = 0
with open(counter, "w") as handle:
    handle.write(str(index + 1))

record = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "token": os.environ.get("RELAUNCH_KIT_TOKEN"),
    "dir": os.environ.get("RELAUNCH_KIT_DIR"),
    "shim": os.environ.get("RELAUNCH_KIT_SHIM"),
}
with open(os.path.join(state, "calls.jsonl"), "a") as handle:
    handle.write(json.dumps(record) + "\n")

with open(os.environ["FAKE_CLAUDE_SCRIPT"]) as handle:
    steps = json.load(handle)
if index >= len(steps):
    sys.stderr.write("fake claude: script exhausted at call %d\n" % index)
    sys.exit(99)
step = steps[index]
time.sleep(step.get("sleep", 0))

request = step.get("request")
if request is not None:
    path = os.path.join(os.environ["RELAUNCH_KIT_DIR"], os.environ["RELAUNCH_KIT_TOKEN"] + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if "raw" in request:
        text = request["raw"]
    else:
        request = dict(request)
        age = request.pop("age_s", 0)
        when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age)
        body = {
            "schema": 1,
            "token": os.environ["RELAUNCH_KIT_TOKEN"],
            "action": "resume",
            "session_id": "0b7e6c2a-1111-4222-8333-444455556666",
            "cwd": os.getcwd(),
            "prompt": None,
            "reason": "restart",
            "written_at": when.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (when.microsecond // 1000),
            "writer": "fake 0",
        }
        body.update(request)
        text = json.dumps({k: v for k, v in body.items() if v != "<drop>"})
    with open(path, "w") as handle:
        handle.write(text)
sys.exit(step.get("exit", 0))
