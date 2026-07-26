from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import json
import os
import uvicorn

app = FastAPI()

state = {}
receipts_log = []

@app.post("/v2/incidents")
async def start_incident(req: Request):
    data = await req.json()
    run_id = data.get("runId")
    
    # Dump to file safely
    dump = []
    if os.path.exists("incidents.json"):
        try:
            with open("incidents.json", "r") as f:
                dump = json.load(f)
        except: pass
        
    if not any(x.get("runId") == run_id for x in dump):
        dump.append(data)
        with open("incidents.json", "w") as f:
            json.dump(dump, f, indent=2)
            
    res = {
      "runId": run_id, 
      "status": "waiting",
      "diagnosis": {"rootCause": "dns_failure", "evidence": ["ev_1", "ev_2"]},
      "dispatches": [{
        "actionId": "action_test", 
        "callId": "call_test",
        "phase": "diagnostic", 
        "toolName": "query_metrics",
        "arguments": {}, 
        "evidence": ["ev_1"], 
        "attempt": 1,
        "traceparent": "00-00000000000000000000000000000001-0000000000000001-01"
      }],
      "approvals": []
    }
    state[run_id] = res
    return JSONResponse(res)

@app.post("/v2/incidents/{run_id}/receipts")
async def handle_receipt(run_id: str, req: Request):
    data = await req.json()
    with open("receipts.json", "a") as f:
        f.write(json.dumps(data) + "\n")
        
    receipts_log.append(data)
    
    res = {
      "runId": run_id, 
      "status": "completed",
      "diagnosis": {"rootCause": "dns_failure", "evidence": ["ev_1", "ev_2"]},
      "chosenEffect": "scale_service",
      "suppressed": [],
      "actionLog": [],
      "receiptLog": [],
      "otlp": {"resourceSpans": [{"scopeSpans": [{"spans": []}]}]}
    }
    state[run_id] = res
    return JSONResponse(res)

@app.get("/v2/incidents/{run_id}")
def get_incident(run_id: str):
    if run_id in state: return JSONResponse(state[run_id])
    return JSONResponse({"error": "not found"}, status_code=404)

@app.get("/dump")
def get_dump():
    if os.path.exists("incidents.json"):
        with open("incidents.json", "r") as f:
            return json.load(f)
    return []

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
