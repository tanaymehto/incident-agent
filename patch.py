import re
with open("app.py", "r") as f: content = f.read()
content = content.replace(
'''def get_trace_id(req: Request):
    tp = req.headers.get("traceparent")
    if tp and len(tp) == 55 and tp.startswith("00-"):
        parts = tp.split("-")
        return parts[1]
    return "00000000000000000000000000000001"''',
'''def get_trace_id(req: Request):
    tp = req.headers.get("traceparent")
    if tp and len(tp) == 55 and tp.startswith("00-"):
        parts = tp.split("-")
        return parts[1], parts[2]
    return "01020304050607080910111213141516", ""'''
)
content = content.replace("trace_id = get_trace_id(req)", "trace_id, parent_span_id = get_trace_id(req)")
content = content.replace('"trace_id": trace_id', '"trace_id": trace_id, "parent_span_id": parent_span_id')
with open("app.py", "w") as f: f.write(content)

with open("build_otlp.py", "r") as f: content = f.read()
content = content.replace(
'''    spans.append({
        "traceId": trace_id, "spanId": server_span_id, "parentSpanId": "",
        "name": "POST /v2/incidents", "kind": 2,''',
'''    spans.append({
        "traceId": trace_id, "spanId": server_span_id, "parentSpanId": state.get("parent_span_id", ""),
        "name": "POST /v2/incidents", "kind": 2,'''
)
with open("build_otlp.py", "w") as f: f.write(content)
