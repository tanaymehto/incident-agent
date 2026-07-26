def KV(k, v, t):
    if t == "int": return {"key": k, "value": {"intValue": v}}
    return {"key": k, "value": {"stringValue": str(v)}}

def build_otlp(state):
    trace_id = state["trace_id"]
    marker = state["input"]["publicMarker"]
    run_id = state["input"]["runId"]
    
    server_span_id = gen_span_id(f"server_{run_id}", 1)
    agent_span_id = gen_span_id(f"agent_{run_id}", 1)
    model_span_id = gen_span_id(f"model_{run_id}", 1)
    
    spans = []
    # 1. SERVER POST
    spans.append({
        "traceId": trace_id, "spanId": server_span_id, "parentSpanId": state.get("parent_span_id", ""),
        "name": "POST /v2/incidents", "kind": 2,
        "attributes": [KV("ga5.run.id", run_id, "str"), KV("ga5.public.marker", marker, "str")]
    })
    
    # 2. INTERNAL agent
    spans.append({
        "traceId": trace_id, "spanId": agent_span_id, "parentSpanId": server_span_id,
        "name": "invoke_agent", "kind": 1,
        "attributes": [KV("ga5.run.id", run_id, "str"), KV("ga5.public.marker", marker, "str")]
    })
    
    # 3. CLIENT chat model
    spans.append({
        "traceId": trace_id, "spanId": model_span_id, "parentSpanId": agent_span_id,
        "name": "incident-plan", "kind": 3,
        "attributes": [KV("ga5.run.id", run_id, "str"), KV("ga5.public.marker", marker, "str"), KV("gen_ai.operation.name", "chat", "str"), KV("gen_ai.request.model", "gpt-4", "str")]
    })
    
    diag_links = []
    
    # 4. Diagnostics Tools
    def build_tool_spans(act_id, tool_name, phase):
        acts = [x for x in state["actionLog"] if x["actionId"] == act_id]
        if not acts: return
        
        tool_span_id = gen_span_id(f"exec_{act_id}", 1)
        spans.append({
            "traceId": trace_id, "spanId": tool_span_id, "parentSpanId": agent_span_id,
            "name": "execute_tool", "kind": 1,
            "attributes": [KV("ga5.run.id", run_id, "str"), KV("ga5.public.marker", marker, "str"), KV("ga5.action.id", act_id, "str"), KV("gen_ai.tool.name", tool_name, "str"), KV("gen_ai.tool.call.id", act_id, "str"), KV("gen_ai.operation.name", "execute_tool", "str")]
        })
        
        if phase == "diagnostic": diag_links.append({"traceId": trace_id, "spanId": tool_span_id})
        
        for attempt_data in acts:
            attempt = attempt_data["attempt"]
            rcpt = next((r for r in state["receiptLog"] if r.get("actionId") == act_id and r.get("attempt") == attempt), None)
            
            client_span_id = gen_span_id(act_id, attempt)
            attrs = [KV("ga5.run.id", run_id, "str"), KV("ga5.public.marker", marker, "str"), KV("ga5.action.id", act_id, "str"), KV("ga5.attempt", attempt, "int"), KV("http.request.method", "POST", "str"), KV("http.request.resend_count", attempt-1, "int")]
            
            status = {}
            if rcpt:
                attrs.append(KV("ga5.receipt.id", rcpt["receiptId"], "str"))
                attrs.append(KV("ga5.receipt.nonce", rcpt["nonce"], "str"))
                
                if rcpt.get("status") == 503:
                    status = {"code": 2}
                    attrs.append(KV("error.type", "503", "str"))
                elif rcpt.get("errorType") == "timeout":
                    status = {"code": 2}
                    attrs.append(KV("error.type", "timeout", "str"))
                else: pass
            else:
                pass # Should always have rcpt at end
                
            spans.append({
                "traceId": trace_id, "spanId": client_span_id, "parentSpanId": tool_span_id,
                "name": f"POST tool/{tool_name}", "kind": 3,
                "attributes": attrs,
                "status": status
            })
            
    for dg in state["diags"]:
        build_tool_spans(dg["actionId"], dg["toolName"], "diagnostic")
        
    # 5. Join
    if len(state["diags"]) > 1:
        join_span_id = gen_span_id(f"join_{run_id}", 1)
        spans.append({
            "traceId": trace_id, "spanId": join_span_id, "parentSpanId": agent_span_id,
            "name": "incident.join", "kind": 1,
            "attributes": [KV("ga5.run.id", run_id, "str"), KV("ga5.public.marker", marker, "str")],
            "links": diag_links
        })
        
    # 6. Approval
    if state["needs_approval"]:
        appr_rcpt = next((r for r in state["receiptLog"] if r.get("approvalId")), None)
        appr_span_id = gen_span_id(f"appr_{run_id}", 1)
        attrs = [KV("ga5.run.id", run_id, "str"), KV("ga5.public.marker", marker, "str")]
        if appr_rcpt:
            attrs.append(KV("ga5.approval.id", state["approval_id"], "str"))
            attrs.append(KV("ga5.receipt.nonce", appr_rcpt["nonce"], "str"))
        spans.append({
            "traceId": trace_id, "spanId": appr_span_id, "parentSpanId": agent_span_id,
            "name": "approval_gate", "kind": 1,
            "attributes": attrs
        })
        
    # 7. Effect
    if state["effect_action_id"] not in state["suppressed"]:
        build_tool_spans(state["effect_action_id"], state["effect_tool"]["toolName"], "effect")
        
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
