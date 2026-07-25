import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.performance_auditor import compile_daily_report_embeds

def test_performance_auditor():
    print("Testing End-of-Day Performance Auditor...")
    
    # Run compiler for today's date
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"Compiling report for {today_str}...")
    
    res = compile_daily_report_embeds(today_str)
    assert res is not None, "Performance auditor returned None"
    
    summary, details = res
    
    # Verify Summary Embed Structure
    assert "title" in summary, "Summary embed missing 'title'"
    assert "description" in summary, "Summary embed missing 'description'"
    assert "color" in summary, "Summary embed missing 'color'"
    assert "fields" in summary, "Summary embed missing 'fields'"
    assert len(summary["fields"]) == 6, f"Expected 6 fields in summary embed, got: {len(summary['fields'])}"
    
    print("Summary Embed verified successfully.")
    
    # Verify Details Embed Structure
    assert "title" in details, "Details embed missing 'title'"
    assert "description" in details, "Details embed missing 'description'"
    assert "color" in details, "Details embed missing 'color'"
    
    print("Details Embed verified successfully.")
    print("Performance Auditor test passed successfully.")

if __name__ == "__main__":
    test_performance_auditor()
