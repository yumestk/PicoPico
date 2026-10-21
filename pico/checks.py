"""Views of recorded shell facts; never infer tests from model text or stdout."""


def version_status(before, after):
    if not before or not after or not before.get("complete") or not after.get("complete"):
        return "inconclusive"
    return "matched" if all(before.get(key) == after.get(key) for key in ("fingerprint", "version", "scope", "git")) else "stale"


def check_view(call, target):
    result = call.get("result") or {}
    data = result.get("data", {})
    args = call["args"]
    if data.get("cancelled") or result.get("status") == "cancelled":
        outcome = "cancelled"
    elif data.get("timed_out"):
        outcome = "timed_out"
    elif result.get("error_code") == "outcome_unknown" or call["phase"] == "outcome_unknown":
        outcome = "unknown"
    elif call["phase"] == "executing":
        outcome = "running"
    elif type(data.get("exit_code")) is int:
        outcome = "passed" if data["exit_code"] == 0 else "failed"
    elif call["phase"] == "proposed":
        outcome = "not_executed"
    else:
        outcome = "unknown" if data.get("started_at") else "not_executed"
    binding = version_status(data.get("before_scope"), data.get("after_scope"))
    if binding == "matched":
        binding = version_status(data.get("after_scope"), target)
    view = {"run_id": call["run_id"], "call_id": call["call_id"],
            "command": args.get("command", "")[:8192], "command_truncated": len(args.get("command", "")) > 8192,
            "purpose": "verification", "cwd": data.get("cwd"),
            "started_at": data.get("started_at"), "finished_at": data.get("finished_at"),
            "exit_code": data.get("exit_code"), "timed_out": bool(data.get("timed_out")),
            "cancelled": bool(data.get("cancelled")), "command_outcome": outcome,
            "version_status": binding, "status": binding if outcome == "passed" and binding != "matched" else outcome,
            "before_scope": data.get("before_scope"), "after_scope": data.get("after_scope"),
            "claim": "Only this command's outcome; coverage is not assessed."}
    for stream in ("stdout", "stderr"):
        text = data.get(stream, "")
        view[stream] = text[:8192]
        view[stream + "_truncated"] = bool(data.get(stream + "_truncated")) or len(text) > 8192
    return view


def summary(checks):
    counts = {}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    # Conservative across all declared checks; no model summary can erase a failure.
    state = next((s for s in ("failed", "timed_out", "cancelled", "unknown", "running", "not_executed",
                             "stale", "inconclusive", "passed") if counts.get(s)), "not_run")
    return {"status": state, "counts": counts, "total": len(checks),
            "scope": "bounded_review_files", "coverage_assessed": False}
