def calculate(rows, payload):
    event = payload["event"]; ordered = sorted(rows, key=lambda r: r["at"])
    baseline_rows = ordered[-(event["baseline_window"]+1):-1]
    baseline = sum(float(r["kw"]) for r in baseline_rows) / len(baseline_rows)
    actual = float(ordered[-1]["kw"]); delivered = max(0.0, baseline - actual)
    amount = round(delivered * float(event["rate"]), 2)
    return {"baseline_kw": round(baseline, 3), "actual_kw": actual, "delivered_kw": round(delivered, 3), "settlement_amount": amount, "explanation": {"baseline_rows": len(baseline_rows), "rate": event["rate"]}}
