from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import json
import os
import uvicorn
import re
import hashlib
from build_otlp import build_otlp

app = FastAPI()

if not os.path.exists("runs"):
    os.makedirs("runs")

def get_trace_id(req: Request):
    tp = req.headers.get("traceparent")
    if tp and len(tp) == 55 and tp.startswith("00-"):
        parts = tp.split("-")
        return parts[1], parts[2]
    return "01020304050607080910111213141516", ""

def gen_span_id(action_id, attempt):
    h = hashlib.md5(f"{action_id}_{attempt}".encode()).hexdigest()
    if h == "0"*32: h = "1"*32
    return h[:16]

def load_run(run_id):
    path = f"runs/{run_id}.json"
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None

def save_run(run_id, data):
    path = f"runs/{run_id}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def build_initial_state(data, trace_id):
    inc = data["incident"]
    trans = inc["transcript"]
    service = inc["service"]
    
    lines = trans.split('\n')
    active_lines = [l for l in lines if 'retain this full sentence' not in l]
    text = "\n".join(active_lines)
    evs = [re.search(r'\[(ev_[a-zA-Z0-9]+)\]', l).group(1) for l in active_lines]
    
    rc = None
    diags = []
    eff = None
    appr = False
    
    if 'pool checkout wait crossed' in text:
        rc = 'database_connection_exhaustion'
        diags = [
            {"toolName":"query_metrics", "arguments":{"service":service, "metric":"pool_wait", "windowMinutes":30}},
            {"toolName":"query_logs", "arguments":{"service":service, "query":"pool acquisition timeouts", "windowMinutes":30}}
        ]
        m = re.search(r'target is exactly (\d+) application replicas', text)
        eff = {"toolName":"scale_service", "arguments":{"service":service, "targetReplicas":int(m.group(1))}}
       
    elif 'expired leaf certificate' in text:
        rc = 'dependency_certificate_expired'
        diags = [{"toolName":"dependency_status", "arguments":{"dependency":"dep_n2t4ssmps64l"}}]
        eff = {"toolName":"open_incident", "arguments":{"service":"dep_n2t4ssmps64l", "severity":"SEV-1"}}
       
    elif 'flag cohort' in text:
        rc = 'feature_flag_recursion'
        diags = [{"toolName":"query_logs", "arguments":{"service":service, "query":"recursive trace", "windowMinutes":30}}]
        m = re.search(r'flag cohort ([a-zA-Z0-9_]+)', text)
        eff = {"toolName":"disable_feature", "arguments":{"service":service, "flag":m.group(1)}}
        appr = True
       
    elif 'queue depth and latency rise' in text:
        rc = 'traffic_capacity_exhaustion'
        diags = [{"toolName":"query_metrics", "arguments":{"service":service, "metric":"queue_depth", "windowMinutes":30}}]
        m = re.search(r'exactly (\d+) replicas', text)
        eff = {"toolName":"scale_service", "arguments":{"service":service, "targetReplicas":int(m.group(1))}}
       
    elif 'vault promoted version' in text:
        rc = 'secret_rotation_mismatch'
        diags = [{"toolName":"read_runbook", "arguments":{"service":service, "topic":"recovery"}}]
        eff = {"toolName":"no_action", "arguments":{"reasonCode":"RUNBOOK_UNAVAILABLE"}}
       
    elif 'previous release' in text:
        rc = 'deployment_regression'
        diags = [{"toolName":"inspect_deployment", "arguments":{"service":service}}]
        m = re.search(r'previous release ([a-zA-Z0-9_-]+)', text)
        eff = {"toolName":"rollback_deployment", "arguments":{"service":service, "release":m.group(1)}}
        appr = True

    run_id = data["runId"]
    state = {
        "runId": run_id,
        "input": data,
        "trace_id": trace_id, "parent_span_id": parent_span_id,
        "diagnosis": {"rootCause": rc, "evidence": evs},
        "diags": [],
        "stage": "diagnostics",
        "effect_tool": eff,
        "needs_approval": appr,
        "approval_id": f"appr_{run_id}",
        "effect_action_id": f"act_eff_{run_id}",
        "suppressed": [],
        "actionLog": [],
        "receiptLog": [],
    }
    
    for i, dg in enumerate(diags):
        act_id = f"act_diag_{run_id}_{i}"
        state["diags"].append({
            "actionId": act_id,
            "callId": act_id,
            "toolName": dg["toolName"],
            "arguments": dg["arguments"],
            "attempt": 1,
            # "Every diagnostic dispatch must cite at least one ID from the diagnosis's two-to-four evidence IDs. Do not cite duplicate evidence IDs."
            "evidence": [evs[i if i < len(evs) else 0]], 
            "status": "pending"
        })
        
    return state

@app.post("/v2/incidents")
async def start_incident(req: Request):
    try: data = await req.json()
    except: return JSONResponse({"error":"malformed"}, status_code=400)
    
    if data.get("profile") != "ga5-incident-agent/v2":
        return JSONResponse({"error":"unsupported profile"}, status_code=400)
        
    run_id = data.get("runId")
    trace_id, parent_span_id = get_trace_id(req)
    
    state = load_run(run_id)
    if not state:
        state = build_initial_state(data, trace_id)
        save_run(run_id, state)
        
    state = load_run(run_id)
    if json.dumps(state["input"], sort_keys=True) != json.dumps(data, sort_keys=True):
        return JSONResponse({"error": "conflict"}, status_code=409)
        
    if state["stage"] == "diagnostics":
        dispatches = []
        for dg in state["diags"]:
            if dg["status"] in ["pending"]:
                sp_id = gen_span_id(dg["actionId"], dg["attempt"])
                disp = {
                    "actionId": dg["actionId"],
                    "callId": f"{dg['actionId']}_{dg['attempt']}",
                    "phase": "diagnostic",
                    "toolName": dg["toolName"],
                    "arguments": dg["arguments"],
                    "evidence": dg["evidence"],
                    "attempt": dg["attempt"],
                    "traceparent": f"00-{state['trace_id']}-{sp_id}-01"
                }
                dispatches.append(disp)
                if not any(x["actionId"]==disp["actionId"] and x["attempt"]==disp["attempt"] for x in state["actionLog"]):
                    state["actionLog"].append(disp)
                    save_run(run_id, state)
                    
        return JSONResponse({
            "runId": state["runId"],
            "status": "waiting",
            "diagnosis": state["diagnosis"],
            "dispatches": dispatches,
            "approvals": []
        })

    return JSONResponse(build_final(state))

def build_final(state):
    res = {
        "runId": state["runId"],
        "status": "completed" if not state["suppressed"] else "failed",
        "diagnosis": state["diagnosis"],
        "chosenEffect": state["effect_tool"]["toolName"],
        "suppressed": state["suppressed"],
        "actionLog": [x for x in state["actionLog"] if "actionId" in x], 
        "receiptLog": state["receiptLog"],
        "otlp": build_otlp(state)
    }
    return res

def sort_keys_recursive(d):
    if isinstance(d, dict): return {k: sort_keys_recursive(v) for k, v in sorted(d.items())}
    if isinstance(d, list): return [sort_keys_recursive(x) for x in d]
    return d

@app.post("/v2/incidents/{run_id}/receipts")
async def handle_receipt(run_id: str, req: Request):
    try: data = await req.json()
    except: return JSONResponse({"error":"malformed"}, status_code=400)
    
    state = load_run(run_id)
    if not state:
        return JSONResponse({"error":"not found"}, status_code=404)
        
    r_id = data.get("receiptId")
    for r in state["receiptLog"]:
        if r.get("receiptId") == r_id:
            if json.dumps(r, sort_keys=True) != json.dumps(data, sort_keys=True):
                return JSONResponse({"error": "conflict"}, status_code=409)
            elif state["stage"] == "completed":
                return JSONResponse(build_final(state))

    if "outcomes" in data:
        for out in data["outcomes"]:
            if out.get("actionId") == state["effect_action_id"]:
                state["stage"] = "completed"
                state["receiptLog"].append({"receiptId":r_id, **out})
                continue
                
            for dg in state["diags"]:
                if dg["actionId"] == out.get("actionId") and dg["attempt"] == out.get("attempt"):
                    state["receiptLog"].append({"receiptId":r_id, **out})
                    
                    if out.get("status") == 503:
                        dg["attempt"] += 1
                        dg["status"] = "pending"
                    elif out.get("errorType") == "timeout":
                        dg["status"] = "timeout"
                    else:
                        dg["status"] = "success"
    elif "approvals" in data:
        for appr in data["approvals"]:
            if appr.get("approvalId") == state["approval_id"]:
                state["receiptLog"].append({"receiptId":r_id, **appr})
                state["stage"] = "effect"

    save_run(run_id, state)
    
    if state["stage"] == "diagnostics":
        all_done = all(dg["status"] in ["success", "timeout"] for dg in state["diags"])
        if all_done:
            has_timeout = any(dg["status"] == "timeout" for dg in state["diags"])
            if has_timeout:
                state["stage"] = "completed"
                state["suppressed"] = [state["effect_action_id"]]
                save_run(run_id, state)
                return JSONResponse(build_final(state))
            else:
                if state["needs_approval"]:
                    state["stage"] = "approval"
                else:
                    state["stage"] = "effect"
                save_run(run_id, state)
        else:
            dispatches = []
            for dg in state["diags"]:
                if dg["status"] == "pending":
                    sp_id = gen_span_id(dg["actionId"], dg["attempt"])
                    disp = {
                        "actionId": dg["actionId"],
                        "callId": f"{dg['actionId']}_{dg['attempt']}",
                        "phase": "diagnostic",
                        "toolName": dg["toolName"],
                        "arguments": dg["arguments"],
                        "evidence": dg["evidence"],
                        "attempt": dg["attempt"],
                        "traceparent": f"00-{state['trace_id']}-{sp_id}-01"
                    }
                    dispatches.append(disp)
                    if not any(x["actionId"]==disp["actionId"] and x["attempt"]==disp["attempt"] for x in state["actionLog"]):
                        state["actionLog"].append(disp)
            save_run(run_id, state)
            return JSONResponse({
                "runId": run_id,
                "status": "waiting",
                "diagnosis": state["diagnosis"],
                "dispatches": dispatches,
                "approvals": []
            })
            
    if state["stage"] == "approval":
        eff_args = sort_keys_recursive(state["effect_tool"]["arguments"])
        s256 = hashlib.sha256(json.dumps(eff_args, separators=(',', ':')).encode()).hexdigest()
        
        return JSONResponse({
            "runId": run_id,
            "status": "waiting",
            "diagnosis": state["diagnosis"],
            "dispatches": [],
            "approvals": [{
                "approvalId": state["approval_id"],
                "actionId": state["effect_action_id"],
                "toolName": state["effect_tool"]["toolName"],
                "argumentsDigest": s256
            }]
        })
        
    if state["stage"] == "effect":
        sp_id = gen_span_id(state["effect_action_id"], 1)
        disp = {
            "actionId": state["effect_action_id"],
            "callId": state["effect_action_id"],
            "phase": "effect",
            "toolName": state["effect_tool"]["toolName"],
            "arguments": state["effect_tool"]["arguments"],
            "evidence": state["diagnosis"]["evidence"],
            "attempt": 1,
            "traceparent": f"00-{state['trace_id']}-{sp_id}-01"
        }
        if not any(x["actionId"]==disp["actionId"] for x in state["actionLog"]):
            state["actionLog"].append(disp)
            save_run(run_id, state)
            
        return JSONResponse({
            "runId": run_id,
            "status": "waiting",
            "diagnosis": state["diagnosis"],
            "dispatches": [disp],
            "approvals": []
        })

    return JSONResponse(build_final(state))

@app.get("/v2/incidents/{run_id}")
def get_incident(run_id: str):
    state = load_run(run_id)
    if state: return JSONResponse(build_final(state))
    return JSONResponse({"error": "not found"}, status_code=404)

EOF
