from core import feedback
trips = feedback.compute_closed_round_trips()
found = False
for t in trips:
    close_ts = str(t.get('close_ts', ''))
    if '2026-10-19' in close_ts:
        print("Found 10/19 round trip:", t)
        found = True
if not found:
    print("No round trip found for 2026-10-19 in computed closed round trips! Total trips:", len(trips))
    for t in trips[-5:]:
        print("Tail trip:", t)
