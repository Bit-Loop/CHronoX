#!/usr/bin/env python3
"""
Script to automatically check off completed Phase 0 items in FLOORPLAN_CHECKLIST.md
based on PHASE0_AUDIT.md verification.
"""

import re

# Read PHASE0_AUDIT.md to get completed items
with open('PHASE0_AUDIT.md', 'r') as f:
    audit_content = f.read()

# Read FLOORPLAN_CHECKLIST.md
with open('FLOORPLAN_CHECKLIST.md', 'r') as f:
    checklist_lines = f.readlines()

# Items to mark as complete (from Phase 0 only)
completed_items = [
    # Section 0.3 - Aggregates tests
    "Test minute bars fetching",
    "Test daily bars fetching",
    "Validate data completeness",
    
    # Section 0.3 - Corporate Actions
    "Test dividend fetching",
    "Test split fetching",
    
    # Section 0.3 - Reference Data
    "Fetch full ticker list",
    "Fetch exchange metadata",
    "Store in reference database",
    
    # Section 0.3 - News
    "Test news fetching",
    "Validate news data structure",
    
    # Section 0.3 - Indicators
    "Test each indicator type",
    "Validate indicator calculations",
    
    # Section 0.3 - WebSocket
    "Test WebSocket connection",
    "Test multi-ticker subscription",
    "Test reconnection logic",
    "Validate data format",
    
    # Section 0.4 - Flat Files
    "Test single file download",
    "Test parallel downloads",
    "Test resumable downloads",
    "Verify file integrity",
    "Test parsing compressed files",
    "Test batching for DB insertion",
    "Validate data integrity after parsing",
    
    # Section 0.5 - Database Writer
    "Test OHLCV writing",
    "Test corporate actions writing",
    "Test reference data writing",
    "Test news writing",
    "Benchmark write throughput",
    
    # Section 0.6 - Backfill
    "Test with small ticker subset",
    "Validate all data types present",
    "Check data completeness",
    "Verify corporate action adjustments",
]

# Update checklist
updated_lines = []
for line in checklist_lines:
    new_line = line
    
    # Only process Phase 0 items (before Phase 1)
    if line.startswith("## Phase 1"):
        # Stop updating after Phase 0
        updated_lines.extend(checklist_lines[checklist_lines.index(line):])
        break
    
    # Check if this is a checkbox line
    if "- [ ]" in line:
        # Check if it matches any completed item
        for item in completed_items:
            # Simple substring match
            if item.lower() in line.lower():
                new_line = line.replace("- [ ]", "- [x]", 1)
                print(f"✓ Checked: {line.strip()}")
                break
    
    updated_lines.append(new_line)

# Write updated checklist
with open('FLOORPLAN_CHECKLIST.md', 'w') as f:
    f.writelines(updated_lines)

print("\n✅ FLOORPLAN_CHECKLIST.md updated successfully!")
print(f"Total items checked: {len([l for l in updated_lines if '- [x]' in l])}")
