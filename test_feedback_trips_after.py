from core import feedback
trips = feedback.compute_closed_round_trips()
print("Computed closed round trips after inserting trade:", len(trips))
for t in trips:
    if '2026-10-19' in str(t.get('close_ts')):
        print("Found 10/19 round trip:", t)
